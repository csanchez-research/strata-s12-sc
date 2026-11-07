"""Does a second provider give the same tiles? (``crosscheck`` command).

Runs on the output folder or on a release (read from its tars). For every complete
tile, the same ESA product is read again from Microsoft Planetary Computer on the
tile grid, all bands, and compared pixel by pixel: two pixels agree when they are
equal or both nodata, and nodata is also counted apart. Each tile records the UTM
zone of its grid and of its Sentinel-2 granule. Tiles made by Planetary Computer are
skipped. Results per tile go to ``log/crosscheck_pc_<folder name>.jsonl`` as they
finish, so a new run resumes; the report of the whole study goes to
``log/crosscheck_pc_<UTC>.json``.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from functools import partial
from typing import TYPE_CHECKING

import numpy as np
import rasterio
import rasterio.errors
import requests
from rasterio.io import MemoryFile

from strata_complete import config
from strata_complete.providers import Grid, granule_utm_zone, pc, utm_zone
from strata_complete.report.source import Source, open_source
from strata_complete.runtime import context, errors, logs, network
from strata_complete.runtime.progress import bar
from strata_complete.schema import ScSidecar
from strata_complete.verify import studydb

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PROVIDER = "pc"
_ERRORS = (
    pc.PcError,
    requests.RequestException,
    OSError,
    KeyError,
    rasterio.errors.RasterioError,
)


@dataclass
class BandCross:
    band: str
    origin: str  # dlite (published in D_lite) or provider (added by the tool)
    n_pixels: int
    n_agree: int
    max_abs_diff: float | None
    nodata_both: int
    nodata_tile_only: int
    nodata_pc_only: int

    @property
    def identical(self) -> bool:
        return self.n_agree == self.n_pixels


@dataclass
class TileCross:
    filename: str
    product_id: str | None
    path: str | None = None
    pc_item_id: str | None = None
    pc_processing_baseline: str | None = None
    tile_utm_zone: int | None = None
    granule_utm_zone: int | None = None
    attempts: int = 0
    seconds: float | None = None
    error: str | None = None
    bands: list[BandCross] = field(default_factory=list)
    via: str = "crosscheck"  # the command that measured it: crosscheck or reproduce

    @property
    def identical(self) -> bool:
        return self.error is None and bool(self.bands) and all(b.identical for b in self.bands)


def compare_band(name: str, origin: str, tile: np.ndarray, prov: np.ndarray) -> BandCross:
    """Pixel agreement of one band: equal values, or nodata in both, count as agreeing."""
    nd_t, nd_p = ~np.isfinite(tile), ~np.isfinite(prov)
    both_valid = ~nd_t & ~nd_p
    diff = np.abs(tile[both_valid].astype(np.float64) - prov[both_valid].astype(np.float64))
    return BandCross(
        band=name,
        origin=origin,
        n_pixels=int(tile.size),
        n_agree=int(((tile == prov) | (nd_t & nd_p)).sum()),
        max_abs_diff=float(diff.max()) if diff.size else None,
        nodata_both=int((nd_t & nd_p).sum()),
        nodata_tile_only=int((nd_t & ~nd_p).sum()),
        nodata_pc_only=int((~nd_t & nd_p).sum()),
    )


def read_pc(pid: str, names: list[str], grid: Grid, out: Path) -> tuple[np.ndarray, int]:
    """Read the tile from PC with retries; a network cut pauses without using attempts."""
    failures = 0
    while True:
        try:
            pc.fetch(pid, names, grid, out)
            with rasterio.open(out) as src:
                return src.read(), failures + 1
        except pc.PcError:
            raise  # the scene is not there: retrying does not help
        except _ERRORS as exc:
            error = errors.describe(exc)
            if network.wait_until_online(error) > 0:
                continue
            failures += 1
            if failures >= config.MAX_ATTEMPTS_PER_REQUEST:
                raise
            time.sleep(config.RETRY_WAIT_S * failures)


def cross_tile(src: Source, side: str, *, keep: bool = False) -> TileCross:
    """Compare one tile (its sidecar path ``side`` in ``src``) with Planetary Computer."""
    t0 = time.perf_counter()
    tif = side[: -len(".json")] + ".tif"
    name = tif.rsplit("/", 1)[-1]
    try:
        sc = ScSidecar.model_validate_json(src.read(side))
    except (ValueError, KeyError, OSError) as exc:
        return TileCross(name, None, path=tif, error=errors.describe(exc))
    pid = sc.radiometry.product_id
    g = sc.grid
    result = TileCross(name, pid, path=tif, tile_utm_zone=utm_zone(g.crs))
    if not pid:
        result.error = "no radiometry.product_id in the sidecar"
        return result
    result.granule_utm_zone = granule_utm_zone(pid)
    names = [b.name for b in sc.bands]
    out = config.DATA_DIR / PROVIDER / "s2" / name
    try:
        item = pc.item_for_product(pid)
        result.pc_item_id = item["id"]
        result.pc_processing_baseline = item["properties"].get("s2:processing_baseline")
        grid = Grid(g.crs, g.transform, g.width, g.height)
        prov, result.attempts = read_pc(pid, names, grid, out)
        with MemoryFile(src.read(tif)) as mem, mem.open() as t_src:
            tile = t_src.read()
    except _ERRORS as exc:
        result.error = errors.describe(exc)
        result.seconds = round(time.perf_counter() - t0, 1)
        return result
    finally:
        if not keep:
            out.unlink(missing_ok=True)
    for i, b in enumerate(sc.bands):
        result.bands.append(compare_band(b.name, b.origin, tile[b.index - 1], prov[i]))
    result.seconds = round(time.perf_counter() - t0, 1)
    return result


def progress_file(folder: Path) -> Path:
    """Per-tile results of the study on ``folder``, one JSON line per tile."""
    return config.LOG_DIR / f"crosscheck_{PROVIDER}_{folder.resolve().name}.jsonl"


def _from_json(d: dict) -> TileCross:
    return TileCross(**{**d, "bands": [BandCross(**b) for b in d.get("bands", [])]})


def load_progress(path: Path) -> dict[str, TileCross]:
    """Latest result of each tile in a progress file (a cut-off last line is ignored)."""
    done: dict[str, TileCross] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = _from_json(json.loads(line))
            except (ValueError, TypeError):
                continue
            done[r.path or r.filename] = r
    return done


def band_summary(results: list) -> list[dict[str, object]]:
    """Per band, over the tiles without error."""
    per: dict[str, list[BandCross]] = {}
    for r in results:
        for b in r.bands:
            per.setdefault(b.band, []).append(b)
    order = {b: i for i, b in enumerate(config.S2_ALL_BANDS)}
    rows = []
    for name in sorted(per, key=lambda b: order.get(b, len(order))):
        bs = per[name]
        diffs = [b.max_abs_diff for b in bs if b.max_abs_diff is not None]
        rows.append(
            {
                "band": name,
                "origin": bs[0].origin,
                "n_tiles": len(bs),
                "n_tiles_identical": sum(b.identical for b in bs),
                "n_pixels": sum(b.n_pixels for b in bs),
                "n_agree": sum(b.n_agree for b in bs),
                "max_abs_diff": max(diffs) if diffs else None,
                "nodata_both": sum(b.nodata_both for b in bs),
                "nodata_tile_only": sum(b.nodata_tile_only for b in bs),
                "nodata_pc_only": sum(b.nodata_pc_only for b in bs),
            }
        )
    return rows


def _by_zone(results: list[TileCross]) -> dict[str, dict[str, int]]:
    """Identical / differing tiles, split by whether the tile grid is in the granule's zone."""
    out: dict[str, dict[str, int]] = {}
    for r in results:
        if r.error:
            continue
        same = r.tile_utm_zone is not None and r.tile_utm_zone == r.granule_utm_zone
        key = "same_zone" if same else "other_zone"
        state = "identical" if r.identical else "differs"
        out.setdefault(key, {"identical": 0, "differs": 0})[state] += 1
    return out


def cross_folder(  # noqa: PLR0913 - folder, run settings, hook
    folder: Path,
    workers: int = 1,
    limit: int | None = None,
    on_result: Callable[[TileCross], None] | None = None,
    *,
    keep: bool = False,
    redo: bool = False,
) -> tuple[list[TileCross], list[dict[str, object]], dict[str, object], Path]:
    """Run the study on ``folder`` (output folder or release), resuming from its progress file.

    ``on_result`` is called for the tiles checked in this run; the report covers
    every tile in the progress file.
    """
    started, started_utc = time.perf_counter(), context.now()
    src = open_source(folder)
    progress = progress_file(folder)
    release = folder if (folder / "sc_manifest.csv").is_file() else None
    if redo:
        progress.unlink(missing_ok=True)
    studydb.sync(progress, release)  # the database catches up with the file first
    done = {k: r for k, r in load_progress(progress).items() if r.error is None}
    todo, skipped, from_pc = [], 0, 0
    for side in src.sidecars():
        sc = ScSidecar.model_validate_json(src.read(side))
        if sc.verdict != "COMPLETE":
            skipped += 1
        elif sc.provider == PROVIDER:
            from_pc += 1
        elif side[: -len(".json")] + ".tif" not in done:
            todo.append(side)
    n_left = len(todo)
    todo = todo[:limit] if limit else todo
    print(
        f"{len(done) + n_left} tiles to study; {len(done)} already done; {n_left} left, "
        f"{len(todo)} in this run (the bar counts these)"
    )
    lock = threading.Lock()
    progress.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool, progress.open("a") as fh:
        jobs = pool.map(partial(cross_tile, src, keep=keep), todo)
        for r in bar(jobs, total=len(todo), desc="crosscheck", unit="tile"):
            with lock:
                fh.write(json.dumps(asdict(r)) + "\n")
                fh.flush()
                studydb.sync(progress, release)  # file first, then the database
            if on_result:
                on_result(r)
    results = sorted(load_progress(progress).values(), key=lambda r: r.path or r.filename)
    rows = band_summary(results)
    wall = time.perf_counter() - started
    meta: dict[str, object] = {
        "folder": str(folder.resolve()),
        "source": src.describe(),
        "progress_file": progress.name,
        "provider": PROVIDER,
        "provider_version": pc.version(),
        "workers": workers,
        "started": started_utc,
        "ended": context.now(),
        "context": context.run_context(
            folder if (folder / "sc_member_index.csv").is_file() else None
        ),
        "n_tiles_checked": len(results),
        "n_tiles_this_run": len(todo),
        "n_tiles_identical": sum(r.identical for r in results),
        "n_tiles_error": sum(r.error is not None for r in results),
        "n_tiles_skipped_not_verified": skipped,
        "n_tiles_skipped_completed_by_pc": from_pc,
        "by_zone": _by_zone(results),
        "n_tiles_by_command": {
            v: sum(r.via == v for r in results) for v in sorted({r.via for r in results})
        },
        "wall_s_this_run": round(wall, 1),
        "s_per_tile_overall": round(wall / len(todo), 1) if todo else None,
    }
    payload = {**meta, "bands": rows, "tiles": [asdict(r) for r in results]}
    report = logs.write(f"crosscheck_{PROVIDER}", payload)
    studydb.record_run(progress, report, meta)
    return results, rows, meta, report
