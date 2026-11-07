"""SQLite copy of the studies (``out/studies.sqlite``), for statistics.

The base of every study is its JSON-lines file in ``log/``. Each run first imports the
lines not yet in the database (the byte offset reached is stored per file), then every
line it writes, so file and database stay in step; ``rebuild`` makes the database again
from the files. Tables: ``studies``, ``tiles`` (latest result per tile), ``bands``,
``history`` (every attempt), ``manifest`` (the release's rows, to group by stratum) and
``runs``.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from strata_complete import config

if TYPE_CHECKING:
    from pathlib import Path

DB_NAME = "studies.sqlite"
_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
  study TEXT PRIMARY KEY, kind TEXT, jsonl TEXT, release TEXT, bytes_imported INTEGER,
  lines_imported INTEGER, updated TEXT);
CREATE TABLE IF NOT EXISTS tiles (
  study TEXT, path TEXT, filename TEXT, pair_id TEXT, product_id TEXT, provider_item_id TEXT,
  provider_baseline TEXT, tile_utm_zone INTEGER, granule_utm_zone INTEGER, same_zone INTEGER,
  identical INTEGER, n_bands INTEGER, n_bands_identical INTEGER, pixels INTEGER,
  pixels_differ INTEGER, error TEXT, error_final INTEGER, via TEXT, seconds REAL,
  attempts INTEGER, line_offset INTEGER, PRIMARY KEY (study, path));
CREATE TABLE IF NOT EXISTS bands (
  study TEXT, path TEXT, band TEXT, origin TEXT, n_pixels INTEGER, n_agree INTEGER,
  n_differ INTEGER, max_abs_diff REAL, nodata_both INTEGER, nodata_tile_only INTEGER,
  nodata_pc_only INTEGER, PRIMARY KEY (study, path, band));
CREATE TABLE IF NOT EXISTS history (
  study TEXT, line_offset INTEGER, path TEXT, identical INTEGER, error TEXT,
  error_final INTEGER, via TEXT, seconds REAL, PRIMARY KEY (study, line_offset));
CREATE TABLE IF NOT EXISTS manifest (
  release TEXT, s2_file TEXT, row_json TEXT, class_name TEXT, season TEXT, region TEXT,
  pair_id TEXT, PRIMARY KEY (release, s2_file));
CREATE TABLE IF NOT EXISTS runs (
  report TEXT PRIMARY KEY, study TEXT, kind TEXT, command TEXT, started TEXT, ended TEXT,
  workers INTEGER, seed INTEGER, fraction REAL, n_requested INTEGER, n_tiles INTEGER,
  n_this_run INTEGER, n_identical INTEGER, n_error INTEGER, meta_json TEXT);
CREATE INDEX IF NOT EXISTS tiles_by_zone ON tiles (study, same_zone, identical);
CREATE INDEX IF NOT EXISTS bands_by_band ON bands (study, band);
"""


def db_path() -> Path:
    return config.OUT_DIR / DB_NAME


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _kind(jsonl: Path) -> str:
    return "reproduce" if jsonl.name.startswith("reproduce_") else "crosscheck"


def _row(study: str, offset: int, d: dict) -> tuple[tuple, list[tuple], tuple]:
    """A line of either study as (tiles row, bands rows, history row)."""
    path = d.get("path") or d.get("s2_file") or d.get("filename")
    bands = d.get("bands") or []
    error = d.get("error")
    final = d.get("error_final")
    if final is None:  # crosscheck lines: a missing product is a result, the rest is not
        final = bool(error) and "PcError" in str(error)
    ident = error is None and bool(bands) and all(b["n_agree"] == b["n_pixels"] for b in bands)
    tz, gz = d.get("tile_utm_zone"), d.get("granule_utm_zone")
    via = d.get("via") or _kind_from_fields(d)
    tile = (
        study,
        path,
        str(path).rsplit("/", 1)[-1],
        d.get("pair_id"),
        d.get("product_id"),
        d.get("provider_item_id") or d.get("pc_item_id"),
        d.get("provider_processing_baseline") or d.get("pc_processing_baseline"),
        tz,
        gz,
        None if tz is None or gz is None else int(tz == gz),
        int(ident),
        len(bands),
        sum(b["n_agree"] == b["n_pixels"] for b in bands),
        sum(b["n_pixels"] for b in bands),
        sum(b["n_pixels"] - b["n_agree"] for b in bands),
        error,
        int(bool(final)),
        via,
        d.get("seconds"),
        d.get("attempts"),
        offset,
    )
    band_rows = [
        (
            study,
            path,
            b["band"],
            b.get("origin"),
            b["n_pixels"],
            b["n_agree"],
            b["n_pixels"] - b["n_agree"],
            b.get("max_abs_diff"),
            b.get("nodata_both"),
            b.get("nodata_tile_only"),
            b.get("nodata_pc_only"),
        )
        for b in bands
    ]
    hist = (study, offset, path, int(ident), error, int(bool(final)), via, d.get("seconds"))
    return tile, band_rows, hist


def _kind_from_fields(d: dict) -> str:
    return "reproduce" if "s2_file" in d else "crosscheck"


def _load_manifest(conn: sqlite3.Connection, release: Path | None) -> None:
    if release is None:
        return
    name = str(release.resolve())
    if conn.execute("SELECT 1 FROM manifest WHERE release = ? LIMIT 1", (name,)).fetchone():
        return
    manifest = release / "sc_manifest.csv"
    if not manifest.is_file():
        return
    with manifest.open(encoding="utf-8", newline="") as fh:
        rows = [
            (
                name,
                r["s2_file"],
                json.dumps(r),
                r.get("class_name"),
                r.get("season"),
                r.get("region"),
                r.get("pair_id"),
            )
            for r in csv.DictReader(fh)
        ]
    conn.executemany("INSERT OR REPLACE INTO manifest VALUES (?,?,?,?,?,?,?)", rows)


def sync(jsonl: Path, release: Path | None = None) -> int:
    """Import the lines of ``jsonl`` not yet in the database; return how many."""
    study = jsonl.stem
    with _LOCK, closing(_connect()) as conn, conn:
        _load_manifest(conn, release)
        got = conn.execute(
            "SELECT bytes_imported, lines_imported FROM studies WHERE study = ?", (study,)
        ).fetchone()
        start, lines = (got[0], got[1]) if got else (0, 0)
        size = jsonl.stat().st_size if jsonl.is_file() else 0
        if size < start:  # the file was started again (--redo): import it again
            for table in ("tiles", "bands", "history"):
                conn.execute(f"DELETE FROM {table} WHERE study = ?", (study,))  # noqa: S608
            start, lines = 0, 0
        n = 0
        if size > start:
            with jsonl.open("rb") as fh:
                fh.seek(start)
                offset = start
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break  # a line still being written: next time
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        offset += len(raw)
                        continue  # a line cut by an interruption, ignored as on resume
                    tile, band_rows, hist = _row(study, offset, d)
                    conn.execute(
                        "INSERT OR REPLACE INTO tiles VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        tile,
                    )
                    conn.execute("DELETE FROM bands WHERE study = ? AND path = ?", (study, tile[1]))
                    conn.executemany("INSERT INTO bands VALUES (?,?,?,?,?,?,?,?,?,?,?)", band_rows)
                    conn.execute("INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?,?)", hist)
                    offset += len(raw)
                    n += 1
            start, lines = offset, lines + n
        conn.execute(
            "INSERT OR REPLACE INTO studies VALUES (?,?,?,?,?,?,?)",
            (
                study,
                _kind(jsonl),
                str(jsonl),
                str(release.resolve()) if release else None,
                start,
                lines,
                _now(),
            ),
        )
    return n


def record_run(jsonl: Path, report: Path, meta: dict) -> None:
    """One row for a run of a study, with its report and its whole meta as JSON."""
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(report),
                jsonl.stem,
                _kind(jsonl),
                (meta.get("context") or {}).get("command"),
                meta.get("started"),
                meta.get("ended"),
                meta.get("workers"),
                meta.get("seed"),
                meta.get("fraction"),
                meta.get("n_requested"),
                meta.get("n_pairs", meta.get("n_tiles_checked")),
                meta.get("n_pairs_this_run", meta.get("n_tiles_this_run")),
                meta.get("n_identical", meta.get("n_tiles_identical")),
                meta.get("n_error", meta.get("n_tiles_error")),
                json.dumps(meta, default=str),
            ),
        )


def rebuild() -> dict[str, int]:
    """Build the database again from every study file in ``log/`` (and their reports)."""
    db_path().unlink(missing_ok=True)
    for extra in ("-wal", "-shm"):
        db_path().with_name(DB_NAME + extra).unlink(missing_ok=True)
    counts: dict[str, int] = {}
    for jsonl in sorted(config.LOG_DIR.glob("*.jsonl")):
        if not jsonl.name.startswith(("crosscheck_", "reproduce_")):
            continue
        release = config.OUT_DIR / ("release_" + jsonl.stem.rsplit("release_", 1)[-1])
        counts[jsonl.name] = sync(jsonl, release if release.is_dir() else None)
    for report in sorted(config.LOG_DIR.glob("*.json")):
        if not report.name.startswith(("crosscheck_pc_", "reproduce_pc_")):
            continue
        meta = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            continue  # e.g. the list of archives of a finished demonstration
        jsonl_name = meta.get("progress_file") or meta.get("progress")
        if jsonl_name:
            record_run(config.LOG_DIR / str(jsonl_name).rsplit("/", 1)[-1], report, meta)
    return counts
