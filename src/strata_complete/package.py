"""Release of STRATA-S12-SC as archives laid out like D_lite (``package`` command).

It works on an input folder made by ``sample`` and the output of ``complete`` for it. One tar
per class with the D_lite member paths: the S2 tile and sidecar, the S1 tile and sidecar
of the pair (sha256 checked again) and the PROV document. A pair is released when its
D_lite S2 tile matched its checksum, its S1 copy is identical to the record, its
verdict is COMPLETE and it is not flagged; every other pair goes to ``sc_excluded.csv``
with every reason. Tars are byte-reproducible and read back after writing. One provider
per release; the pairs left out only because their S2 tile was not finished are written
to ``in/<input>_pc/``. ``move`` removes each class from the output once its tar is
written and checked (``package_state.json`` lets a stopped run carry on).
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from strata_complete import __version__, complete, config, schema
from strata_complete.dlite import sample
from strata_complete.dlite.sample import MANIFEST
from strata_complete.report import page
from strata_complete.runtime import archive, logs
from strata_complete.runtime.progress import bar, say

if TYPE_CHECKING:
    from pathlib import Path

    from strata_complete.dlite.record import Record

STATE = "package_state.json"
MEMBER_INDEX = "sc_member_index.csv"
INDEX_COLUMNS = ["archive", "path", "offset_data", "size", "sha256"]

SC_COLUMNS = (
    "sc_verdict",
    "sc_n_bands",
    "sc_added_bands",
    "sc_provider",
    "sc_s2_checksum",
    "sc_s1_checksum",
    "sc_prov",
)


def tar_name(class_name: str, provider: str = "gee") -> str:
    return f"strata-s12-sc_{class_name}{'' if provider == 'gee' else '_' + provider}.tar"


@dataclass
class _Pair:
    row: dict[str, str]
    members: list[tuple[str, Path]] = field(default_factory=list)  # (arcname, source)
    sc: dict[str, str] = field(default_factory=dict)
    reason: str | None = None


def _sidecar(tif: str) -> str:
    return sample.sidecar_of(tif)


def last_complete_errors() -> dict[str, str]:
    """Error of each S2 tile in the most recent ``complete`` run that processed it."""
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for path in reversed(logs.reports("complete")):  # any provider, newest first
        for t in (logs.load(path) or {}).get("tiles", []):
            if t["tile"] not in seen:
                seen.add(t["tile"])
                if t.get("error"):
                    errors[t["tile"]] = t["error"]
    return errors


@dataclass
class _Inputs:
    """What every pair is judged with: the record, curator marks, logs."""

    record: Record
    marks: dict
    errors: dict[str, str]
    s1_problems: dict[str, str]
    provider: str = "gee"


def _reasons(row: dict[str, str], ctx: _Inputs) -> tuple[list[str], schema.ScSidecar | None]:
    """Every reason not to release this pair: integrity of the D_lite tiles, then
    the verdict, then the curator."""
    reasons: list[str] = []
    s2_tif = row["s2_file"]
    out_tif = complete.out_path(s2_tif)
    out_json = out_tif.with_suffix(".json")
    sc = None
    if out_tif.is_file() and out_json.is_file():
        sc = schema.ScSidecar.model_validate_json(out_json.read_text(encoding="utf-8"))
        if sc.source.dlite_file_sha256 != ctx.record.member(s2_tif).sha256:
            reasons.append("S2 output does not come from this D_lite tile: run complete again")
        elif not sc.source.dlite_checksum_ok:
            reasons.append("S2 D_lite tile: pixel checksum does not match its sidecar")
    if not complete.s1_copied(row, ctx.record):
        why = ctx.s1_problems.get(out_tif.name)
        reasons.append(f"S1 not copied ({why})" if why else "S1 not copied")
    elif (
        archive.sha256(complete.out_path(row["s1_file"]))
        != ctx.record.member(row["s1_file"]).sha256
    ):
        reasons.append("S1 copy differs from the record")
    if sc is None:
        error = ctx.errors.get(out_json.name)
        reasons.append(f"S2 not completed ({error})" if error else "S2 not completed")
        return reasons, None
    if sc.verdict != "COMPLETE":
        reasons.append(f"S2 {sc.verdict}")
    elif (sc.provider or "gee") != ctx.provider:
        reasons.append(f"S2 made by {sc.provider or 'gee'}, not by {ctx.provider} (other release)")
    mark = ctx.marks.get(out_tif.name)
    if mark and mark.get("flag"):
        reasons.append(f"flagged by curator: {mark.get('note') or 'no note'}")
    return reasons, sc


def _evaluate(row: dict[str, str], ctx: _Inputs) -> _Pair:
    pair = _Pair(row)
    reasons, sc = _reasons(row, ctx)
    if reasons or sc is None:
        pair.reason = "; ".join(reasons)
        return pair
    s2_tif, s1_tif = row["s2_file"], row["s1_file"]
    s2_json, s1_json = _sidecar(s2_tif), _sidecar(s1_tif)
    out = complete.out_path
    s1_meta = json.loads(out(s1_json).read_text(encoding="utf-8"))
    providers = sorted({b.provider for b in sc.bands if b.provider})
    pair.members = [(s2_tif, out(s2_tif)), (s2_json, out(s2_json)), (s1_tif, out(s1_tif)),
                    (s1_json, out(s1_json))]  # fmt: skip
    prov_file = out(s2_json).with_name(out(s2_json).stem + ".prov.json")
    if prov_file.is_file():
        pair.members.append((s2_tif[: -len(".tif")] + ".prov.json", prov_file))
    pair.sc = {
        "sc_verdict": sc.verdict,
        "sc_n_bands": str(len(sc.bands)),
        "sc_added_bands": " ".join(b.name for b in sc.bands if b.origin == "provider"),
        "sc_provider": " ".join(providers),
        "sc_s2_checksum": sc.integrity.checksum,
        "sc_s1_checksum": s1_meta["integrity"]["checksum"],
        "sc_prov": "yes" if prov_file.is_file() else "no",
    }
    return pair


def _load_state(release_dir: Path, in_dir: Path) -> dict:
    """Classes already written and moved by an earlier ``package --move`` of this input."""
    path = release_dir / STATE
    fresh = {
        "input": in_dir.name,
        "sha256sums_sha256": config.RECORD_SHA256SUMS_SHA256,
        "report": False,
        "archives": {},
    }
    if not path.is_file():
        return fresh
    state = json.loads(path.read_text(encoding="utf-8"))
    if {k: state.get(k) for k in ("input", "sha256sums_sha256")} != {
        k: fresh[k] for k in ("input", "sha256sums_sha256")
    }:
        msg = f"{path} belongs to another input or record: use another release folder"
        raise RuntimeError(msg)
    for name, a in state["archives"].items():
        tar = release_dir / name
        if not tar.is_file() or tar.stat().st_size != a["bytes"]:
            msg = (
                f"{tar}: written and moved by an earlier run, now missing or changed. Its "
                "tiles are no longer in out/: restore the tar (or run complete again)"
            )
            raise RuntimeError(msg)
    return state


def _save_state(release_dir: Path, state: dict) -> None:
    tmp = release_dir / (STATE + ".part")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(release_dir / STATE)


def _remove_sources(members: list[tuple[str, Path]]) -> None:
    """Remove the files of a written class from ``out/strata-s12-sc/`` (``move``)."""
    folders = set()
    for _, src in members:
        if src.is_relative_to(config.SC_OUT_DIR):
            src.unlink(missing_ok=True)
            folders.add(src.parent)
    for leaf in sorted(folders, key=lambda f: len(f.parts), reverse=True):
        folder = leaf
        while folder != config.SC_OUT_DIR and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()  # empty class/season/region folders go too
            folder = folder.parent


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


FINISHABLE = ("S2 PARTIAL", "S2 UNAVAILABLE", "S2 not completed")


def _finishable(reason: str) -> bool:
    """Left out only because the provider could not finish the S2 tile."""
    return all(r.startswith(FINISHABLE) for r in reason.split("; "))


def write_unfinished(in_dir: Path, rows: list[dict[str, str]], excluded: list[_Pair]) -> Path:
    """``in/<input>_pc/``: the pairs a second provider can be asked for."""
    pending = [p for p in excluded if p.reason and _finishable(p.reason)]
    out_dir = in_dir.with_name(in_dir.name + "_pc")
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / MANIFEST, list(rows[0]) if rows else [], [p.row for p in pending])
    selection = {
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "design": f"pairs of {in_dir.name} whose S2 tile the provider could not finish",
        "source": {"input_folder": in_dir.name, "dlite_doi": config.DLITE_VERSION_DOI},
        "n_pairs": len(pending),
        "pairs": [
            {k: p.row[k] for k in ("pair_id", "tile_id", "class_name", "season", "region")}
            | {"reason": p.reason}
            for p in pending
        ],
    }
    (out_dir / sample.SELECTION).write_text(json.dumps(selection, indent=2) + "\n", "utf-8")
    return out_dir


def build(  # noqa: C901, PLR0915 - the release, step by step
    in_dir: Path,
    release_dir: Path,
    record: Record,
    *,
    move: bool = False,
    provider: str = "gee",
) -> dict[str, object]:
    """Write the release of ``in_dir`` to ``release_dir``; returns what ``release.json`` says.

    Every pair is judged first (``_reasons``); then the report, one tar per class read
    back after writing, the member index, ``sc_manifest.csv``, ``sc_excluded.csv``, the
    schemas, ``release.json`` and ``SHA256SUMS.txt``; the pairs another provider could
    finish go to ``in/<input>_pc/``.
    """
    with (in_dir / MANIFEST).open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    selection_path = in_dir / sample.SELECTION
    selection = json.loads(selection_path.read_text("utf-8")) if selection_path.is_file() else {}
    release_dir.mkdir(parents=True, exist_ok=True)
    state = _load_state(release_dir, in_dir)  # also without move: never redo a moved class
    moved = {p for a in state["archives"].values() for p in a["pairs"]}
    complete_log = logs.load(logs.latest("complete")) or {}
    ctx = _Inputs(
        record=record,
        marks=page.load_review(config.SC_OUT_DIR),
        errors=last_complete_errors(),
        s1_problems=complete_log.get("s1_not_copied", {}),
        provider=provider,
    )
    pairs = [
        _evaluate(r, ctx)
        for r in bar([r for r in rows if r["pair_id"] not in moved], desc="package: check",
                     unit="pair")
    ]  # fmt: skip
    for p in pairs:
        if p.reason is None and tar_name(p.row["class_name"], provider) in state["archives"]:
            p.reason = "its class archive was already written and moved (package --move)"
            p.members = []
    kept = [p for p in pairs if p.reason is None]
    excluded = [p for p in pairs if p.reason is not None]

    if not state["report"] or not (release_dir / "report.html").is_file():
        # before anything moves; the release holds the data, not the study of
        # a second provider (crosscheck runs afterwards, on the release)
        page.build(config.SC_OUT_DIR, release_dir / "report.html", with_crosscheck=False)
        state["report"] = move
    by_class: dict[str, list[_Pair]] = {}
    for p in kept:
        by_class.setdefault(p.row["class_name"], []).append(p)
    archives: dict[str, dict[str, object]] = {
        n: {k: a[k] for k in ("sha256", "bytes", "n_files")} for n, a in state["archives"].items()
    }
    released_rows = [r for a in state["archives"].values() for r in a["rows"]]
    index_rows = [m for a in state["archives"].values() for m in a["members"]]
    for cls in bar(sorted(by_class), desc="package: tars", unit="tar"):
        name = tar_name(cls, provider)
        members = [m for p in by_class[cls] for m in p.members]
        sha, size, index = archive.write_tar(release_dir / name, members)
        archives[name] = {"sha256": sha, "bytes": size, "n_files": len(members)}
        tar_index = [
            {"archive": name, "path": m.path, "offset_data": m.offset_data, "size": m.size,
             "sha256": m.sha256}
            for m in index
        ]  # fmt: skip
        index_rows += tar_index
        rows_cls = [p.row | p.sc for p in by_class[cls]]
        released_rows += rows_cls
        say(f"[OK] {name}: {len(by_class[cls])} pairs, {size / 1e6:.1f} MB, read back")
        if move:
            state["archives"][name] = archives[name] | {
                "pairs": [p.row["pair_id"] for p in by_class[cls]],
                "rows": rows_cls,
                "members": tar_index,
            }
            _save_state(release_dir, state)  # recorded before anything is removed
            _remove_sources(members)
            say(f"       {len(members)} files of {cls} removed from {config.SC_OUT_DIR}")
    if move:
        _save_state(release_dir, state)

    fields = list(rows[0]) if rows else []
    released_rows.sort(key=lambda r: r["s2_file"])
    _write_csv(release_dir / "sc_manifest.csv", fields + list(SC_COLUMNS), released_rows)
    _write_csv(
        release_dir / "sc_excluded.csv",
        ["pair_id", "class_name", "season", "region", "s2_file", "reason"],
        [
            {k: p.row[k] for k in ("pair_id", "class_name", "season", "region", "s2_file")}
            | {"reason": p.reason}
            for p in excluded
        ],
    )
    index_rows.sort(key=lambda r: (r["archive"], r["offset_data"]))
    _write_csv(release_dir / MEMBER_INDEX, INDEX_COLUMNS, index_rows)
    schema.export(release_dir / "schema")
    reasons: dict[str, int] = {}
    for p in excluded:
        for r in (p.reason or "").split("; "):
            key = r.split(" (")[0]
            reasons[key] = reasons.get(key, 0) + 1
    meta = {
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": {
            "name": "strata-complete",
            "version": __version__,
            "base_image": config.BASE_IMAGE,
        },
        "schema_version": schema.SCHEMA_VERSION,
        "source": {
            "dlite_doi": config.DLITE_VERSION_DOI,
            "dlite_version": config.DLITE_VERSION,
            "sha256sums_sha256": config.RECORD_SHA256SUMS_SHA256,
            "input_folder": in_dir.name,
            "selection": {
                k: selection.get(k) for k in ("per_stratum", "seed", "design", "n_pairs")
            },
        },
        "provider": provider,
        "rule": f"released pairs: S2 made by {provider}, D_lite S2 pixel checksum ok, "
        "S1 identical to the record, "
        "S2 COMPLETE (D_lite bands identical), not flagged by the curator",
        "n_pairs_input": len(rows),
        "n_pairs_released": len(released_rows),
        "n_pairs_excluded": len(excluded),
        "excluded_by_reason": reasons,
        "archives": archives,
    }
    if provider == "gee":
        pending_dir = write_unfinished(in_dir, rows, excluded)
        meta["unfinished"] = {
            "folder": pending_dir.name,
            "n_pairs": sum(1 for p in excluded if p.reason and _finishable(p.reason)),
        }
    (release_dir / "release.json").write_text(json.dumps(meta, indent=2) + "\n", "utf-8")
    skip = {config.SHA256SUMS_NAME, STATE}
    files = sorted(f for f in release_dir.rglob("*") if f.is_file() and f.name not in skip)
    known = {name: str(a["sha256"]) for name, a in archives.items()}  # tars: hashed once
    lines = [
        f"{known.get(f.name) or archive.sha256(f)}  {f.relative_to(release_dir).as_posix()}\n"
        for f in files
    ]
    (release_dir / config.SHA256SUMS_NAME).write_text("".join(lines), encoding="utf-8")
    return meta
