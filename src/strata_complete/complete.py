"""Spectral completion of the D_lite Sentinel-2 tiles (``complete`` command).

For every pair of an input folder, the S2 tile and its sidecar are read from the
record and checked. The provider is asked first for the request group that holds the
D_lite bands; if its B4/B3/B2 are identical to D_lite, the other bands are added,
otherwise nothing is added and the output is the D_lite tile as published. One
provider per run: ``provider="pc"`` is a separate run for the pairs Earth Engine
could not finish, and writes a tile only if it is COMPLETE. The S1 tile of every
pair is checked and copied next to its S2 output.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import rasterio
import rasterio.errors
import requests
import xxhash
from rasterio.io import MemoryFile

from strata_complete import __version__, config
from strata_complete.dlite import checks, sample
from strata_complete.dlite.record import Record, RecordError
from strata_complete.providers import Grid, gee, pc
from strata_complete.runtime import errors, logs, network
from strata_complete.runtime.progress import bar, say
from strata_complete.schema import (
    SCHEMA_VERSION,
    BandRecord,
    DliteSidecar,
    GridInfo,
    NotAdded,
    OutputIntegrity,
    PairedSar,
    Radiometry,
    ReferenceCheck,
    ScSidecar,
    SourceInfo,
    ToolInfo,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

PROVIDERS = ("gee", "pc")
_FETCHERS: dict[str, Callable[[str, Sequence[str], Grid, Path], Path]] = {
    "gee": gee.fetch,
    "pc": pc.fetch,
}
_FETCH_ERRORS = (
    gee.GeeError,
    pc.PcError,
    requests.RequestException,
    OSError,
    ValueError,
    rasterio.errors.RasterioError,
)
TAG_PREFIX = "STRATA_"


def _no_step(_: str) -> None:
    pass


@contextmanager
def _timed(timings: dict[str, float], key: str) -> Iterator[None]:
    t0 = time.perf_counter()
    yield
    timings[key] = round(time.perf_counter() - t0, 3)


@dataclass
class _Band:
    """Working state of one band while a tile is being completed."""

    name: str
    data: np.ndarray | None = None
    origin: str = "provider"
    group: str | None = None
    attempts: int = 0
    error: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class _Source:
    """Where a tile's bands are requested: the provider and its id of the scene."""

    provider: str
    scene: str


def _hash(arr: np.ndarray) -> str:
    return xxhash.xxh64(np.ascontiguousarray(arr).tobytes()).hexdigest()


def _fetch_once(source: _Source, bands: Sequence[str], grid: Grid, piece: Path) -> np.ndarray:
    _FETCHERS[source.provider](source.scene, bands, grid, piece)
    with rasterio.open(piece) as src:
        got = Grid(str(src.crs), tuple(src.transform)[:6], src.width, src.height)
        if got != grid:
            msg = f"grid mismatch: {got} != {grid}"
            raise ValueError(msg)
        if src.count != len(bands):
            msg = f"expected {len(bands)} bands, got {src.count}"
            raise ValueError(msg)
        return src.read().astype(np.float32)


def _fetch(
    source: _Source, bands: Sequence[str], grid: Grid, piece: Path
) -> tuple[np.ndarray | None, int, str | None]:
    """Fetch ``bands`` on ``grid`` with retries. Returns (array, attempts, last_error).

    A failure while the network is down is not counted: the run pauses until it
    is back and tries again.
    """
    failures, error = 0, None
    while failures < config.MAX_ATTEMPTS_PER_REQUEST:
        try:
            return _fetch_once(source, bands, grid, piece), failures + 1, None
        except _FETCH_ERRORS as exc:
            error = errors.describe(exc)
        if network.wait_until_online(error) > 0:
            continue
        failures += 1
        if failures < config.MAX_ATTEMPTS_PER_REQUEST:
            time.sleep(config.RETRY_WAIT_S * failures)
    return None, failures, error


def _fetch_all(  # noqa: PLR0913 - tile state, progress hook and timings
    source: _Source,
    grid: Grid,
    work: dict[str, _Band],
    piece_dir: Path,
    on_step: Callable[[str], None],
    timings: dict[str, float],
    only: Sequence[str] | None = None,
) -> dict[str, np.ndarray]:
    """Fetch every band (or the groups in ``only``), group by group;
    ``config.GROUPS_IN_PARALLEL`` groups at a time.

    Groups hold disjoint bands, so each one writes only its own entries of ``work``
    and ``got``.
    """
    got: dict[str, np.ndarray] = {}

    def one(group: str, bands: tuple[str, ...]) -> None:
        on_step(group)
        with _timed(timings, f"fetch_{group}"):
            _fetch_group_into(source, grid, work, piece_dir, group, bands, got)

    groups = [(g, b) for g, b in config.S2_REQUEST_GROUPS.items() if only is None or g in only]
    with ThreadPoolExecutor(max_workers=max(1, config.GROUPS_IN_PARALLEL)) as pool:
        for future in [pool.submit(one, g, b) for g, b in groups]:
            future.result()
    return got


def _fetch_group_into(  # noqa: PLR0913 - one group, all its state
    source: _Source,
    grid: Grid,
    work: dict[str, _Band],
    piece_dir: Path,
    group: str,
    bands: Sequence[str],
    got: dict[str, np.ndarray],
) -> None:
    """Fetch one group; if it keeps failing, each of its bands alone."""
    tag = f"{source.provider}_{group}"
    arr, attempts, error = _fetch(source, bands, grid, piece_dir / f"{tag}.tif")
    if arr is not None:
        for i, b in enumerate(bands):
            got[b] = arr[i]
            work[b].group, work[b].attempts = group, attempts
            work[b].provider, work[b].error = source.provider, None
        return
    for b in bands:
        one, n, err = _fetch(source, (b,), grid, piece_dir / f"{tag}_{b}.tif")
        work[b].group, work[b].attempts = f"{group}/{b}", attempts + n
        if one is not None:
            got[b] = one[0]
            work[b].provider, work[b].error = source.provider, None
        else:
            work[b].error = f"group: {error}; alone: {err}"


def _reference_check(ref: np.ndarray, prov: np.ndarray | None, band: str) -> ReferenceCheck:
    if prov is None:
        return ReferenceCheck(
            band=band,
            available=False,
            n_pixels=int(ref.size),
            n_equal=0,
            max_abs_diff=None,
            identical=False,
        )
    both = np.isfinite(ref) & np.isfinite(prov)
    diff = np.abs(ref[both].astype(np.float64) - prov[both].astype(np.float64))
    n_equal = int((ref == prov).sum())
    return ReferenceCheck(
        band=band,
        available=True,
        n_pixels=int(ref.size),
        n_equal=n_equal,
        max_abs_diff=float(diff.max()) if diff.size else None,
        identical=n_equal == ref.size,
    )


def _band_record(idx: int, b: _Band, arr: np.ndarray, fill: float) -> BandRecord:
    ok = b.data is not None
    finite = np.isfinite(arr)
    vals = arr[finite]
    has = ok and vals.size > 0
    return BandRecord(
        name=b.name,
        index=idx,
        origin=b.origin,
        derived=False,
        status="ok" if ok else "failed",
        native_resolution_m=config.NATIVE_RESOLUTION_M.get(b.name),
        provider=b.provider if b.origin == "provider" else None,
        request_group=b.group,
        attempts=b.attempts,
        error=None if ok else b.error,
        dtype="float32",
        nodata=fill,
        min=float(vals.min()) if has else None,
        max=float(vals.max()) if has else None,
        median=float(np.median(vals)) if has else None,
        n_nonfinite=int((~finite).sum()) if ok else None,
        xxhash64=_hash(arr) if ok else None,
    )


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _scene(image_id: str) -> tuple[dict[str, object], str | None]:
    """GEE properties of a scene and the error if they could not be read."""
    try:
        return gee.scene_properties(image_id), None
    except _FETCH_ERRORS as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def _paired_sar(image_id: str) -> PairedSar:
    props, error = _scene(image_id)
    orbit = props.get("orbitProperties_pass")
    if error is None and orbit not in {"ASCENDING", "DESCENDING"}:
        error = f"orbitProperties_pass missing or unexpected: {orbit!r}"
    return PairedSar(
        image_id=image_id,
        orbit_pass=orbit if orbit in {"ASCENDING", "DESCENDING"} else None,
        relative_orbit_number=_optional_int(props.get("relativeOrbitNumber_start")),
        absolute_orbit_number=_optional_int(props.get("orbitNumber_start")),
        platform=props.get("platform_number"),
        instrument_mode=props.get("instrumentMode"),
        error=error,
        properties=props,
    )


def _radiometry(props: dict[str, object], error: str | None, bands: Sequence[str]) -> Radiometry:
    if error is not None:
        return Radiometry(
            product_id=None,
            processing_baseline=None,
            boa_add_offset={},
            sensing_orbit_direction=None,
            error=error,
        )
    offsets: dict[str, float] = {}
    missing = []
    for b in bands:
        value = props.get(f"BOA_ADD_OFFSET_{b}")
        if isinstance(value, int | float):
            offsets[b] = float(value)
        else:
            missing.append(b)
    absent = [k for k in ("PRODUCT_ID", "PROCESSING_BASELINE") if k not in props]
    problems = ([f"no BOA_ADD_OFFSET for {', '.join(missing)}"] if missing else []) + (
        [f"missing {', '.join(absent)}"] if absent else []
    )
    return Radiometry(
        product_id=props.get("PRODUCT_ID"),
        processing_baseline=props.get("PROCESSING_BASELINE"),
        boa_add_offset=offsets,
        sensing_orbit_direction=props.get("SENSING_ORBIT_DIRECTION"),
        error="; ".join(problems) or None,
    )


def write_tiff(
    path: Path, stack: np.ndarray, profile: dict, names: Sequence[str], tags: dict[str, str]
) -> str:
    """Write, read back and compare. Returns the checksum of the decoded array."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    checksum = _hash(stack)
    with rasterio.open(tmp, "w", **{**profile, "driver": "GTiff"}) as dst:
        dst.write(stack)
        dst.descriptions = tuple(names)
        dst.update_tags(**tags, **{f"{TAG_PREFIX}PIXEL_XXHASH64": checksum})
    with rasterio.open(tmp) as back:
        if _hash(back.read()) != checksum:
            msg = f"{path}: written file does not read back identical"
            raise RuntimeError(msg)
    tmp.replace(path)  # atomic: an interruption never leaves a half-written tile
    return checksum


def _compose(  # noqa: PLR0913 - one tile, all its state
    work: dict[str, _Band],
    got: dict[str, np.ndarray],
    checks: list[ReferenceCheck],
    dlite: np.ndarray,
    dlite_bands: Sequence[str],
    fill: float,
) -> tuple[bool, list[_Band], np.ndarray, list[NotAdded]]:
    """The output tile: every band if the reference check passed, else the D_lite tile.

    Returns (verified, bands in output order, stack, bands not added).
    """
    if all(c.available and c.identical for c in checks):
        kept = list(work.values())
        shape = dlite.shape[1:]
        stack = np.stack(
            [b.data if b.data is not None else np.full(shape, fill, np.float32) for b in kept],
            dtype=np.float32,
        )
        return True, kept, stack, []
    # The original is never changed: the output is the D_lite tile as published.
    why = (
        "the provider's copy of the D_lite bands differs from D_lite"
        if any(c.available for c in checks)
        else "the provider did not return the D_lite bands, so nothing could be verified"
    )
    not_added = [
        NotAdded(
            name=b.name,
            reason=f"not added: {why}"
            + ("" if b.name in got or b.data is not None else " (not downloaded)"),
        )
        for b in work.values()
        if b.origin == "provider"
    ]
    return False, [work[n] for n in dlite_bands], dlite.astype(np.float32), not_added


def _scene_id(meta: DliteSidecar, provider: str) -> str:
    """How ``provider`` names the S2 scene: GEE asset id, or the ESA product name."""
    if provider == "gee":
        return meta.provenance.sentinel2_image_id
    if not meta.acquisition.product_id:
        msg = "no acquisition.product_id in the D_lite sidecar: Planetary Computer needs it"
        raise ValueError(msg)
    return meta.acquisition.product_id


def _fetch_tile(  # noqa: PLR0913 - one tile, all its state
    meta: DliteSidecar,
    grid: Grid,
    work: dict[str, _Band],
    piece_dir: Path,
    on_step: Callable[[str], None],
    timings: dict[str, float],
    provider: str,
) -> tuple[dict[str, np.ndarray], list[ReferenceCheck], dict, str | None]:
    """Fetch and verify one tile. Returns (bands fetched, reference check, scene
    properties, scene error)."""
    image_id = meta.provenance.sentinel2_image_id
    # The groups holding the D_lite bands first: if the provider's copy of those
    # differs from the published pixels, nothing else is fetched.
    source = _Source(provider, _scene_id(meta, provider))
    ref_groups = [
        g
        for g, bands in config.S2_REQUEST_GROUPS.items()
        if set(bands) & set(meta.dimensions.bands)
    ]
    got = _fetch_all(source, grid, work, piece_dir, on_step, timings, only=ref_groups)
    checks = [_reference_check(work[n].data, got.get(n), n) for n in meta.dimensions.bands]
    if not any(c.available and not c.identical for c in checks):
        rest = [g for g in config.S2_REQUEST_GROUPS if g not in ref_groups]
        got |= _fetch_all(source, grid, work, piece_dir, on_step, timings, only=rest)
    for name, b in work.items():
        if b.origin == "provider" and name in got:
            b.data = got[name]

    on_step("metadata")
    with _timed(timings, "scene_properties_s2"):
        scene, scene_error = _scene(image_id)  # GEE properties of the scene, any provider
    return got, checks, scene, scene_error


def out_path(rel: str) -> Path:
    """Output path of a record path (``s2/class_.../S2_....tif``)."""
    return config.SC_OUT_DIR / rel


class NotUsedError(RuntimeError):
    """A tile from a second provider that is not COMPLETE: nothing is written."""


def complete_tile(
    row: dict[str, str],
    record: Record,
    on_step: Callable[[str], None] = _no_step,
    provider: str = "gee",
) -> ScSidecar:
    """Complete the S2 tile of one pair, written under ``config.SC_OUT_DIR``."""
    tif = row["s2_file"]
    meta = DliteSidecar.model_validate_json(record.read(sample.sidecar_of(tif)))
    image_id = meta.provenance.sentinel2_image_id
    if provider == "gee" and not image_id.startswith(config.GEE_S2_COLLECTION + "/"):
        msg = (
            f"S2 scene {image_id} is not in {config.GEE_S2_COLLECTION}, the collection of the "
            "D_lite bands"
        )
        raise ValueError(msg)
    with MemoryFile(record.read(tif)) as mem, mem.open() as src:
        grid = Grid.from_dataset(src)
        dlite, nodata, profile = src.read(), src.nodata, src.profile
    fill = float(nodata) if nodata is not None else float("-inf")

    work = {b: _Band(b) for b in config.S2_ALL_BANDS}
    for i, name in enumerate(meta.dimensions.bands):
        work[name].data, work[name].origin = dlite[i], "dlite"

    started = time.perf_counter()
    timings: dict[str, float] = {}
    config.PIECES_DIR.mkdir(parents=True, exist_ok=True)
    piece_dir = Path(tempfile.mkdtemp(prefix=f"{Path(tif).stem}_", dir=config.PIECES_DIR))
    try:
        got, checks, scene, scene_error = _fetch_tile(
            meta, grid, work, piece_dir, on_step, timings, provider
        )
    finally:
        shutil.rmtree(piece_dir, ignore_errors=True)

    verified, kept, stack, not_added = _compose(
        work, got, checks, dlite, meta.dimensions.bands, fill
    )
    records = [_band_record(i, b, stack[i - 1], fill) for i, b in enumerate(kept, 1)]
    names = [b.name for b in kept]
    is_complete = verified and all(r.status == "ok" for r in records)
    if provider != "gee" and not is_complete:
        why = (
            f"failed {', '.join(r.name for r in records if r.status != 'ok')}"
            if verified
            else "reference check: "
            + ("differs" if any(c.available for c in checks) else "not returned")
        )
        msg = f"{provider} did not give a complete tile ({why}): nothing written"
        raise NotUsedError(msg)

    with _timed(timings, "scene_properties_s1"):
        paired_sar = _paired_sar(meta.provenance.sentinel1_image_id)

    out_tif = config.SC_OUT_DIR / tif
    profile.update(count=len(kept), dtype="float32", nodata=fill, **config.OUT_TIFF)
    tags = {
        f"{TAG_PREFIX}DATASET": config.OUT_DATASET,
        f"{TAG_PREFIX}SOURCE_DOI": f"https://doi.org/{config.DLITE_VERSION_DOI}",
        f"{TAG_PREFIX}TILE": out_tif.stem,
        f"{TAG_PREFIX}SENSOR": "S2",
    }
    on_step("write")
    with _timed(timings, "write"):
        checksum = write_tiff(out_tif, stack, profile, names, tags)
    timings["total"] = round(time.perf_counter() - started, 3)

    sidecar = ScSidecar(
        schema_version=SCHEMA_VERSION,
        created=datetime.now(UTC),
        tool=ToolInfo(
            name="strata-complete",
            version=__version__,
            base_image=config.BASE_IMAGE,
            provider_versions={"earthengine-api": gee.version()}
            | ({"planetary-computer": pc.version()} if provider == "pc" else {}),
        ),
        source=SourceInfo(
            dlite_doi=config.DLITE_VERSION_DOI,
            dlite_filename=out_tif.name,
            dlite_file_sha256=record.member(tif).sha256,
            dlite_checksum=meta.integrity.checksum,
            dlite_checksum_ok=_hash(dlite) == meta.integrity.checksum,
            tile_id=meta.tile_id,
            pair_id=meta.pair_id,
            sensor="S2",
            image_id=image_id,
            product_id=meta.acquisition.product_id,
        ),
        grid=GridInfo(crs=grid.crs, transform=grid.transform, width=grid.width, height=grid.height),
        provider=provider,
        complete=is_complete,
        reference_check=checks,
        bands=records,
        not_added=not_added,
        scene_properties=scene,
        radiometry=_radiometry(scene, scene_error, names),
        classification=meta.classification,
        paired_sar=paired_sar,
        timings_s=timings,
        integrity=OutputIntegrity(
            checksum=checksum,
            algorithm="xxhash64",
            scope="decoded pixel array (bands, height, width)",
        ),
    )
    out_json = out_tif.with_suffix(".json")
    tmp = out_json.with_name(out_json.name + ".part")
    tmp.write_text(json.dumps(sidecar.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    tmp.replace(out_json)  # the JSON is written last: it marks the tile as done
    return sidecar


def output_sidecars(folder: Path) -> list[Path]:
    """Sidecars of the output tiles under ``folder`` (``S2_*.json``, not ``*.prov.json``)."""
    return sorted(p for p in folder.rglob("S2_*.json") if not p.name.endswith(".prov.json"))


def paired_s1(folder: Path) -> dict[str, Path]:
    """S1 sidecars under an output folder by pair_id (plain JSON: only the id is needed)."""
    index = {}
    for p in folder.rglob("S1_*.json"):
        if not p.name.endswith(".prov.json"):
            index[json.loads(p.read_text(encoding="utf-8"))["pair_id"]] = p
    return index


def done_tile(row: dict[str, str], record: Record) -> ScSidecar | None:
    """The finished output of this pair's S2 tile, if it is final and from this input.

    From this input: made from the same D_lite file (its sha256 in the record).
    Final: COMPLETE, or MISALIGNED (a property of the D_lite tile). PARTIAL and
    UNAVAILABLE depend on the provider at that moment, so they are tried again.
    """
    out_tif = out_path(row["s2_file"])
    out_json = out_tif.with_suffix(".json")
    if not (out_tif.is_file() and out_json.is_file()):
        return None
    try:
        sc = ScSidecar.model_validate_json(out_json.read_text(encoding="utf-8"))
        want = record.member(row["s2_file"]).sha256
    except (ValueError, RecordError):
        return None  # unreadable output, or not in the record: complete_tile reports why
    if sc.source.dlite_file_sha256 != want:
        return None
    return sc if sc.verdict in ("COMPLETE", "MISALIGNED") else None


Result = tuple[str, ScSidecar | None, str | None]


def _name(row: dict[str, str]) -> str:
    return Path(sample.sidecar_of(row["s2_file"])).name


def _run_one(
    row: dict[str, str], record: Record, step: Callable[[str], None], provider: str
) -> Result:
    try:
        return (_name(row), complete_tile(row, record, step, provider), None)
    except Exception as exc:  # noqa: BLE001 - one bad tile never stops the run
        return (_name(row), None, errors.describe(exc))


def complete_folder(  # noqa: PLR0913 - input, record, hook and run settings
    in_dir: Path,
    record: Record,
    on_result: Callable[[str, ScSidecar | None, str | None], None] | None = None,
    *,
    workers: int = 1,
    limit: int | None = None,
    redo: bool = False,
    provider: str = "gee",
) -> tuple[list[Result], int]:
    """Complete the S2 tile of every pair of ``in_dir``; an error in one tile does not
    stop the run.

    ``workers``: tiles in parallel (threads). ``limit``: only the first N tiles in
    path order. Resume: a tile with a final output from the same D_lite file is
    skipped (``done_tile``; ``redo`` recomputes it). ``on_result`` is called after
    each tile. ``provider``: ``gee``, or ``pc`` for the tiles GEE could not finish
    (only tiles without a final output are tried, and only a COMPLETE tile is
    written). The run summary goes to ``log/complete_<provider>_<UTC>.json``.
    """
    rows = sorted(sample.read_manifest(in_dir / sample.MANIFEST), key=lambda r: r["s2_file"])
    rows = rows[:limit]
    all_rows = list(rows)
    skipped: list[Result] = []
    if not redo:
        todo = []
        for r in bar(rows, desc="resume: outputs", unit="tile"):
            sc = done_tile(r, record)
            if sc is None:
                todo.append(r)
            else:
                skipped.append((_name(r), sc, None))
        if skipped:
            ok = sum(sc.verdict == "COMPLETE" for _, sc, _ in skipped)
            say(
                f"resume: {len(skipped)} tiles already done ({ok} verified, "
                f"{len(skipped) - ok} misaligned), skipped; {len(todo)} to do"
            )
        rows = todo
    if rows:
        gee.init()  # once, before any thread; scene metadata come from GEE for any provider
    results: list[Result] = []
    started = time.perf_counter()
    progress = bar(total=len(rows), desc=f"complete x{workers}", unit="tile")

    def step_for(r: dict[str, str]) -> Callable[[str], None]:
        short = Path(r["s2_file"]).stem[:28]
        return lambda group: progress.set_postfix_str(f"{short} {group}")

    with progress, ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_run_one, r, record, step_for(r), provider) for r in rows]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            progress.update()
            if on_result:
                on_result(*result)
    s1_copied, s1_already, s1_problems = copy_paired_s1(record, all_rows)
    say(
        f"S1 of the pairs: {s1_copied} copied to the output, {s1_already} already there, "
        f"{len(s1_problems)} problems"
    )
    for problem in s1_problems.values():
        say(f"       S1 not copied: {problem}")
    wall = time.perf_counter() - started
    results.sort(key=lambda r: r[0])
    _write_summary(in_dir, results, workers, limit, wall, len(skipped), provider, s1_problems)
    return results, len(skipped)


def _sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def s1_copied(row: dict[str, str], record: Record) -> bool:
    """Whether the output holds the S1 tile of this pair, identical to the record.

    The GeoTIFF is checked by size and the sidecar (written last) by sha256: cheap
    enough to run on every pair at each start. ``package`` checks the sha256 of
    the GeoTIFF before it goes into a tar.
    """
    tif, side = row["s1_file"], sample.sidecar_of(row["s1_file"])
    out_tif, out_side = out_path(tif), out_path(side)
    if not (out_tif.is_file() and out_side.is_file()):
        return False
    return (
        out_tif.stat().st_size == record.member(tif).size
        and _sha256(out_side) == record.member(side).sha256
    )


def copy_paired_s1(
    record: Record, rows: Sequence[dict[str, str]]
) -> tuple[int, int, dict[str, str]]:
    """Copy the D_lite S1 tile (GeoTIFF and sidecar) of each pair next to its S2 output.

    Both files are read from the record (sha256 checked) and the pixel checksum is
    checked against the sidecar; they are written through temporary files, the
    sidecar last. Returns (copied, already there, problems by S2 tile name).
    """
    copied, already, problems = 0, 0, {}
    for row in bar(rows, desc="S1 of the pairs", unit="pair"):
        name = Path(row["s2_file"]).name
        tif, side = row["s1_file"], sample.sidecar_of(row["s1_file"])
        try:
            if s1_copied(row, record):
                already += 1
                continue
            data = {side: record.read(side), tif: record.read(tif)}
        except RecordError as exc:
            problems[name] = f"{Path(tif).name}: {exc}"
            continue
        problem = checks.problem(checks.check_bytes(Path(side).name, data[side], data[tif]))
        if problem:
            problems[name] = f"{Path(side).name}: {problem}"
            continue
        for rel in (tif, side):  # the sidecar last: it marks the copy as done
            dest = out_path(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(data[rel])
            tmp.replace(dest)
        copied += 1
    return copied, already, problems


def _write_summary(  # noqa: PLR0913 - one record of the run
    in_dir: Path,
    results: list[Result],
    workers: int,
    limit: int | None,
    wall: float,
    n_skipped: int,
    provider: str,
    s1_problems: dict[str, str],
) -> Path:
    tiles = [
        {
            "tile": name,
            "error": err,
            "complete": sc.complete if sc else None,
            "failed_bands": [b.name for b in sc.bands if b.status == "failed"] if sc else None,
            "reference_identical": all(c.identical for c in sc.reference_check) if sc else None,
            "orbit_pass": sc.paired_sar.orbit_pass if sc else None,
            "timings_s": sc.timings_s if sc else None,
        }
        for name, sc, err in results
    ]
    summary = {
        "input": str(in_dir),
        "provider": provider,
        "workers": workers,
        "limit": limit,
        "n_tiles": len(results),
        "n_skipped_done": n_skipped,
        "wall_time_s": round(wall, 3),
        "seconds_per_tile": round(wall / len(results), 3) if results else None,
        "tiles": tiles,
        "s1_not_copied": s1_problems,
    }
    return logs.write(f"complete_{provider}", summary)
