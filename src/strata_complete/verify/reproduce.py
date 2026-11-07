"""Rebuild released tiles with a second provider (``reproduce`` command).

A demonstration on part of a release: pairs released by Earth Engine are drawn with a
seed, 10 % of every (class, season, region) stratum by default, at least one each, or
``n`` pairs from the whole release. Every band of each tile is read again from the
provider, from the same ESA product on the tile grid, and written with the encoding
of the release. A pair that differs, or that the provider cannot give, is recorded
with its reason. Both versions are archived side by side in
``out/reproduce_<provider>_<design>_seed<seed>/``: one tar per class (``gee/`` and
``<provider>/``), ``reproduce.csv`` and ``SHA256SUMS.txt``. Report:
``log/reproduce_<provider>_<UTC>.json``.
"""

from __future__ import annotations

import csv
import json
import random
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
from rasterio.io import MemoryFile

from strata_complete import __version__, complete, config
from strata_complete.providers import Grid, granule_utm_zone, pc, utm_zone
from strata_complete.report.source import ReleaseSource
from strata_complete.runtime import archive, context, errors, logs
from strata_complete.runtime.progress import bar
from strata_complete.schema import ScSidecar
from strata_complete.verify import crosscheck, studydb

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


PROVIDERS = ("pc",)
RETRY_ROUNDS = 3  # passes over the pairs that failed by the network, at the end of a run
RETRY_WAIT_S = 300  # seconds before each of those passes


@dataclass
class TileReproduced:
    s2_file: str
    pair_id: str
    product_id: str | None = None
    provider_item_id: str | None = None
    provider_processing_baseline: str | None = None
    tile_utm_zone: int | None = None
    granule_utm_zone: int | None = None
    identical: bool = False
    pixels: int = 0  # band pixels compared
    pixels_differ: int = 0
    bands_identical: list[str] = field(default_factory=list)
    bands_differ: list[str] = field(default_factory=list)
    bands: list[crosscheck.BandCross] = field(default_factory=list)
    error: str | None = None
    error_final: bool = False  # True: the provider does not have it; False: retried
    seconds: float | None = None

    @property
    def rebuilt(self) -> bool:
        return self.error is None and bool(self.bands)


def _released(release: Path) -> list[dict[str, str]]:
    with (release / "sc_manifest.csv").open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("sc_provider") == "gee"]
    return sorted(rows, key=lambda r: r["pair_id"])


def draw(
    release: Path, seed: int, n: int | None = None, fraction: float | None = None
) -> list[dict[str, str]]:
    """Pairs released by GEE, drawn with ``seed``.

    ``fraction``: within every (class_name, season, region) stratum (strata in
    sorted order, rows sorted by pair_id), ``max(1, round(fraction * size))`` pairs,
    so every stratum of the release is in the demonstration. ``n``: ``n`` pairs
    drawn from the whole release (rows sorted by pair_id).
    """
    rows = _released(release)
    rng = random.Random(seed)  # noqa: S311 - reproducible draw, not security
    if fraction is None:
        return rng.sample(rows, min(n or 0, len(rows)))
    strata: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for r in rows:
        strata.setdefault((r["class_name"], r["season"], r["region"]), []).append(r)
    chosen: list[dict[str, str]] = []
    for key in sorted(strata):
        members = strata[key]
        chosen.extend(
            rng.sample(members, min(len(members), max(1, round(fraction * len(members)))))
        )
    return chosen


def design_name(n: int | None, fraction: float | None) -> str:
    return f"{fraction * 100:g}pct" if fraction is not None else f"n{n}"


def _side(tif: str) -> str:
    return tif[: -len(".tif")] + ".json"


def _write_rebuilt(
    result: TileReproduced, released: bytes, prov: np.ndarray, names: list[str], scratch: Path
) -> None:
    """The provider's tile, encoded as the release, and a sidecar saying what it is."""
    with MemoryFile(released) as mem, mem.open() as src:
        profile = dict(src.profile)
    profile.update(count=len(names), dtype="float32", nodata=pc.MASKED, **config.OUT_TIFF)
    out_tif = scratch / result.s2_file
    tags = {
        f"{complete.TAG_PREFIX}DATASET": config.OUT_DATASET,
        f"{complete.TAG_PREFIX}SOURCE_DOI": f"https://doi.org/{config.DLITE_VERSION_DOI}",
        f"{complete.TAG_PREFIX}TILE": out_tif.stem,
        f"{complete.TAG_PREFIX}SENSOR": "S2",
        f"{complete.TAG_PREFIX}PROVIDER": "pc",
    }
    checksum = complete.write_tiff(out_tif, prov.astype(np.float32), profile, names, tags)
    side = {
        "what": "tile rebuilt by a second provider for the reproduce test; not part of a release",
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": {"name": "strata-complete", "version": __version__, "pc": pc.version()},
        "provider": "pc",
        "product_id": result.product_id,
        "provider_item_id": result.provider_item_id,
        "provider_processing_baseline": result.provider_processing_baseline,
        "tile_utm_zone": result.tile_utm_zone,
        "granule_utm_zone": result.granule_utm_zone,
        "bands": names,
        "pixel_xxhash64": checksum,
        "compared_with": f"gee/{result.s2_file}",
        "identical": result.identical,
        "comparison": [asdict(b) for b in result.bands],
    }
    (scratch / _side(result.s2_file)).write_text(json.dumps(side, indent=2) + "\n", "utf-8")


def reproduce_tile(row: dict[str, str], release: ReleaseSource, scratch: Path) -> TileReproduced:
    """Rebuild one released S2 tile from Planetary Computer on its exact grid and compare
    every band with the released one; the rebuilt GeoTIFF is kept under ``scratch``."""
    t0 = time.perf_counter()
    tif = row["s2_file"]
    try:
        sc = ScSidecar.model_validate_json(release.read(_side(tif)))
    except (ValueError, KeyError, OSError) as exc:
        return TileReproduced(tif, row["pair_id"], error=errors.describe(exc), error_final=True)
    pid = sc.radiometry.product_id or sc.source.product_id
    g = sc.grid
    result = TileReproduced(tif, row["pair_id"], pid, tile_utm_zone=utm_zone(g.crs))
    names = [b.name for b in sc.bands]
    tmp = config.DATA_DIR / "reproduce" / "pc_raw" / tif.rsplit("/", 1)[-1]
    if not pid:
        result.error = "no radiometry.product_id in the released sidecar"
        result.error_final = True
        return result
    try:
        result.granule_utm_zone = granule_utm_zone(pid)
        item = pc.item_for_product(pid)
        result.provider_item_id = item["id"]
        result.provider_processing_baseline = item["properties"].get("s2:processing_baseline")
        prov, _ = crosscheck.read_pc(pid, names, Grid(g.crs, g.transform, g.width, g.height), tmp)
        released = release.read(tif)
        with MemoryFile(released) as mem, mem.open() as t_src:
            tile = t_src.read()
        for i, b in enumerate(sc.bands):
            cmp = crosscheck.compare_band(b.name, b.origin, tile[b.index - 1], prov[i])
            result.bands.append(cmp)
            (result.bands_identical if cmp.identical else result.bands_differ).append(b.name)
        result.pixels = sum(b.n_pixels for b in result.bands)
        result.pixels_differ = sum(b.n_pixels - b.n_agree for b in result.bands)
        result.identical = not result.bands_differ
        _write_rebuilt(result, released, prov, names, scratch)
    except pc.PcError as exc:  # the product or its bands are not there: a result
        result.error, result.error_final = errors.describe(exc), True
    except Exception as exc:  # noqa: BLE001 - network, server: tried again on the next run
        result.error = errors.describe(exc)
    finally:
        tmp.unlink(missing_ok=True)
    result.seconds = round(time.perf_counter() - t0, 1)
    return result


CSV_COLUMNS = [
    "pair_id",
    "s2_file",
    "product_id",
    "provider_item_id",
    "tile_utm_zone",
    "granule_utm_zone",
    "identical",
    "pixels",
    "pixels_differ",
    "bands_differ",
    "error",
]


def _pack(  # noqa: PLR0913 - where to, what, and the two sources
    out_dir: Path, name: str, provider: str, scratch: Path, src: ReleaseSource, results: list
) -> list[dict[str, object]]:
    """Per class, the two versions of every pair side by side; a CSV of the results; sums.

    One tar per class (``<name>_<class>.tar``), so no archive grows too big and
    a class can be downloaded alone. The released copies are written for one
    class at a time and removed once its tar is read back.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    released = scratch.parent / "released"
    by_class: dict[str, list[TileReproduced]] = {}
    for r in results:
        by_class.setdefault(r.s2_file.split("/")[1].removeprefix("class_"), []).append(r)
    archives = []
    for cls in sorted(by_class):
        members: list[tuple[str, Path]] = []
        for r in by_class[cls]:
            for rel in (r.s2_file, _side(r.s2_file)):
                copy = released / rel
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(src.read(rel))
                members.append((f"gee/{rel}", copy))
                if r.rebuilt:
                    members.append((f"{provider}/{rel}", scratch / rel))
        tar = out_dir / f"{name}_{cls}.tar"
        sha, size, _ = archive.write_tar(tar, members)
        archives.append(
            {"archive": tar.name, "pairs": len(by_class[cls]), "bytes": size, "sha256": sha}
        )
        shutil.rmtree(released, ignore_errors=True)
    with (out_dir / "reproduce.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in sorted(results, key=lambda r: r.s2_file):
            w.writerow(
                {k: getattr(r, k) for k in CSV_COLUMNS if k != "bands_differ"}
                | {"bands_differ": " ".join(r.bands_differ)}
            )
    files = sorted(f for f in out_dir.iterdir() if f.name != config.SHA256SUMS_NAME)
    (out_dir / config.SHA256SUMS_NAME).write_text(
        "".join(f"{archive.sha256(f)}  {f.name}\n" for f in files), encoding="utf-8"
    )
    return archives


def _zone_counts(results: list[TileReproduced], *, same: bool) -> dict[str, int]:
    part = [
        r
        for r in results
        if (r.tile_utm_zone is not None and r.tile_utm_zone == r.granule_utm_zone) == same
    ]
    return {
        "n": len(part),
        "identical": sum(r.identical for r in part),
        "differ": sum(r.rebuilt and not r.identical for r in part),
        "error": sum(r.error is not None for r in part),
    }


def progress_file(name: str, release: Path) -> Path:
    """Per-pair results of one demonstration on one release, one JSON line per pair."""
    return config.LOG_DIR / f"{name}_{release.resolve().name}.jsonl"


def _load(path: Path) -> dict[str, TileReproduced]:
    done: dict[str, TileReproduced] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                r = TileReproduced(
                    **{**d, "bands": [crosscheck.BandCross(**b) for b in d.get("bands", [])]}
                )
            except (ValueError, TypeError):
                continue  # a line cut short by an interruption
            done[r.s2_file] = r
    return done


def share_with_study(results: list[TileReproduced], release: Path) -> int:
    """Write the rebuilt pairs to the ``crosscheck`` results of this release (``via: reproduce``).

    The read and the comparison are the same, so the study does not read them again.
    """
    path = crosscheck.progress_file(release)
    have = crosscheck.load_progress(path)
    new = [r for r in results if r.rebuilt and (r.s2_file not in have or have[r.s2_file].error)]
    if not new:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in new:
            tile = crosscheck.TileCross(
                filename=r.s2_file.rsplit("/", 1)[-1],
                product_id=r.product_id,
                path=r.s2_file,
                pc_item_id=r.provider_item_id,
                pc_processing_baseline=r.provider_processing_baseline,
                tile_utm_zone=r.tile_utm_zone,
                granule_utm_zone=r.granule_utm_zone,
                attempts=1,
                seconds=r.seconds,
                bands=r.bands,
                via="reproduce",
            )
            fh.write(json.dumps(asdict(tile)) + "\n")
    return len(new)


def run(  # noqa: PLR0913, PLR0915, C901 - the release and the settings of the demonstration
    release: Path,
    provider: str = "pc",
    n: int | None = 10,
    seed: int = 77,
    on_result: Callable[[TileReproduced], None] | None = None,
    *,
    fraction: float | None = None,
    workers: int = 1,
    redo: bool = False,
) -> tuple[list[TileReproduced], dict[str, object], Path]:
    """Resumable: every pair is appended, as it finishes, to the progress file, and
    its rebuilt tile kept in ``data/reproduce/<name>/`` until the archives are
    written; a run cut short goes on from there (pairs in error are tried again).
    Once the archives are written the demonstration is final: running it again
    reports it without downloading anything, and ``redo`` makes it again."""
    if provider not in PROVIDERS:
        msg = f"reproduce: provider {provider!r} not supported (one of {', '.join(PROVIDERS)})"
        raise ValueError(msg)
    if fraction is not None and not 0 < fraction <= 1:
        msg = f"reproduce: fraction {fraction} not in (0, 1]"
        raise ValueError(msg)
    started, t0 = context.now(), time.perf_counter()
    src = ReleaseSource(release)
    rows = draw(release, seed, n, fraction)
    name = f"reproduce_{provider}_{design_name(n, fraction)}_seed{seed}"
    scratch = config.DATA_DIR / "reproduce" / name / provider
    progress = progress_file(name, release)
    wanted = {r["s2_file"] for r in rows}
    packed = progress.with_suffix(".archives.json")  # written once the tars are made
    out_dir = config.OUT_DIR / name
    if redo:
        for f in (progress, packed):
            f.unlink(missing_ok=True)
        shutil.rmtree(scratch.parent, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
    study_file = crosscheck.progress_file(release)
    studydb.sync(progress, release)  # the database catches up with the files first
    studydb.sync(study_file, release)
    saved = {k: r for k, r in _load(progress).items() if k in wanted}
    if packed.is_file() and set(saved) == wanted and (out_dir / config.SHA256SUMS_NAME).is_file():
        # finished before: its results are final (an error is a result); --redo makes it again
        results, new = sorted(saved.values(), key=lambda r: r.s2_file), []
        archives = json.loads(packed.read_text(encoding="utf-8"))
    else:
        done = {
            k: r
            for k, r in saved.items()
            if (r.error is None or r.error_final)
            and (not r.rebuilt or all((scratch / f).is_file() for f in (k, _side(k))))
        }
        todo = [r for r in rows if r["s2_file"] not in done]
        print(
            f"{len(rows)} pairs drawn (seed {seed}); {len(done)} already done; "
            f"{len(todo)} to do now (the bar counts these)"
        )
        lock = threading.Lock()
        progress.parent.mkdir(parents=True, exist_ok=True)

        def one(row: dict[str, str]) -> TileReproduced:
            r = reproduce_tile(row, src, scratch)
            with lock:
                with progress.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(r)) + "\n")
                share_with_study([r], release)  # as it finishes: a cut loses nothing
                studydb.sync(progress, release)  # files first, then the database
                studydb.sync(study_file, release)
            if on_result:
                on_result(r)
            return r

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            new = list(
                bar(
                    pool.map(one, todo),
                    total=len(todo),
                    desc=f"reproduce ({provider})",
                    unit="pair",
                )
            )
        # Pairs that failed because of the network or the server: tried again at the
        # end, a few rounds apart, before the run gives up on them.
        for rnd in range(1, RETRY_ROUNDS + 1):
            again = [r for r in new if r.error and not r.error_final]
            if not again:
                break
            print(f"retry round {rnd} of {RETRY_ROUNDS}: {len(again)} pairs, in {RETRY_WAIT_S} s")
            time.sleep(RETRY_WAIT_S)
            retry_rows = [row for row in rows if row["s2_file"] in {r.s2_file for r in again}]
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                redone = {r.s2_file: r for r in pool.map(one, retry_rows)}
            new = [redone.get(r.s2_file, r) for r in new]
        results = sorted([*done.values(), *new], key=lambda r: r.s2_file)
        if any(r.error and not r.error_final for r in results):
            # not finished: pairs failed because of the network or the server. Nothing
            # is archived yet; the same command tries them again and keeps the rest.
            archives = []
        else:
            archives = _pack(out_dir, name, provider, scratch, src, results)
            packed.write_text(json.dumps(archives, indent=2) + "\n", encoding="utf-8")
            shutil.rmtree(scratch.parent, ignore_errors=True)
    shared = share_with_study(results, release)
    n_released = len(_released(release))
    meta: dict[str, object] = {
        "release": str(release.resolve()),
        "provider": provider,
        "seed": seed,
        "n_requested": None if fraction is not None else n,
        "fraction": fraction,
        "n_released_by_gee": n_released,
        "design": (
            f"stratified: within every (class_name, season, region) stratum of the pairs "
            f"released by GEE (rows sorted by pair_id), max(1, round({fraction} * size)) pairs, "
            "random.Random(seed).sample, strata in sorted order; no other filter"
            if fraction is not None
            else "pairs released by GEE (sc_provider gee), sorted by pair_id, "
            "random.Random(seed).sample; no other filter"
        ),
        "method": "every band of the released tile read again from the provider (same ESA "
        "product, exact grid of the tile), nothing corrected; pixels agree if equal or both nodata",
        "folder": str(out_dir),
        "archives": archives,
        "progress": str(progress),
        "workers": workers,
        "started": started,
        "ended": context.now(),
        "wall_s_this_run": round(time.perf_counter() - t0, 1),
        "s_per_pair_this_run": round((time.perf_counter() - t0) / len(new), 1) if new else None,
        "context": context.run_context(release),
        "n_pairs": len(results),
        "n_pairs_this_run": len(new),
        "n_shared_with_crosscheck_this_run": shared,
        "n_identical": sum(r.identical for r in results),
        "n_differ": sum(r.rebuilt and not r.identical for r in results),
        "finished": bool(archives),
        "n_error": sum(r.error is not None for r in results),
        "n_error_final": sum(r.error is not None and r.error_final for r in results),
        "n_to_retry": sum(r.error is not None and not r.error_final for r in results),
        "same_zone": _zone_counts(results, same=True),
        "other_zone": _zone_counts(results, same=False),
    }
    pairs = [asdict(r) for r in results]
    bands = crosscheck.band_summary([r for r in results if r.rebuilt])
    report = logs.write(f"reproduce_{provider}", {**meta, "bands": bands, "pairs": pairs})
    studydb.sync(study_file, release)
    studydb.record_run(progress, report, meta)
    return results, meta, report
