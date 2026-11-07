"""Provenance report of STRATA-S12-SC (``report`` command, and the page of ``serve``).

The page holds the figures of the run and one short row per pair, so a release of
20,000 pairs still opens in a browser. In the live page, the full detail of a tile
(``detail``) and JPEG quicklooks (``preview``: true colour B4/B3/B2 and SWIR
B12/B8A/B4 with a fixed stretch, Sentinel-1 VV, VH and VV-VH in dB; 256 px) are made on
request. Built only from what the tool wrote: sidecars, GeoTIFFs, PROV documents and
the latest reports in ``log/``. Curator marks go to ``review.json``.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import warnings
from datetime import UTC, datetime
from importlib import resources
from typing import TYPE_CHECKING

import numpy as np
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile

from strata_complete import __version__, config, notes
from strata_complete.providers import granule_utm_zone, utm_zone
from strata_complete.report.source import Source, open_source
from strata_complete.runtime import logs
from strata_complete.runtime.progress import bar
from strata_complete.schema import DliteSidecar, ScSidecar

if TYPE_CHECKING:
    from pathlib import Path

    from strata_complete.schema import BandRecord

THUMB_MAX_PX = 256  # 512 px tiles: 2 x 2 pixel blocks averaged
PAGE_SIZE = 24  # tiles per page in the page; previews are made for one page at a time
THUMB_JPEG_QUALITY = 85
# Nominal central wavelength (nm) of each band (ESA Sentinel-2 MSI specification).
WAVELENGTH_NM = {
    "B1": 443,
    "B2": 490,
    "B3": 560,
    "B4": 665,
    "B5": 705,
    "B6": 740,
    "B7": 783,
    "B8": 842,
    "B8A": 865,
    "B9": 945,
    "B11": 1610,
    "B12": 2190,
}
REVIEW_FILE = "review.json"
_REVIEW_LOCK = threading.Lock()
COMPOSITES = {"rgb": (("B4", "B3", "B2"), 0.3), "swir": (("B12", "B8A", "B4"), 0.5)}


def _jpeg(rgb: np.ndarray) -> bytes:
    """uint8 array (3, h, w) to JPEG bytes (5 to 7 times smaller than PNG)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with MemoryFile() as mem:
            _, h, w = rgb.shape
            with mem.open(
                driver="JPEG",
                width=w,
                height=h,
                count=3,
                dtype="uint8",
                QUALITY=str(THUMB_JPEG_QUALITY),
            ) as dst:
                dst.write(rgb)
            return mem.read()


def _shrink(layer: np.ndarray, step: int) -> np.ndarray:
    """Mean of each step x step block of finite pixels (NaN where a block has none)."""
    if step == 1:
        return layer
    h, w = (layer.shape[0] // step) * step, (layer.shape[1] // step) * step
    blocks = np.where(np.isfinite(layer[:h, :w]), layer[:h, :w], np.nan)
    blocks = blocks.reshape(h // step, step, w // step, step)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN blocks
        return np.nanmean(blocks, axis=(1, 3))


def _reflectance(stack: np.ndarray, sc: ScSidecar, band: str) -> np.ndarray | None:
    rec = next((b for b in sc.bands if b.name == band and b.status == "ok"), None)
    if rec is None:
        return None
    offset = sc.radiometry.boa_add_offset.get(band, 0.0)
    return (stack[rec.index - 1].astype(np.float64) + offset) / config.S2_QUANTIFICATION


def _read(src: Source, rel: str) -> np.ndarray:
    with MemoryFile(src.read(rel)) as mem, mem.open() as ds:
        return ds.read()


def _composite(stack: np.ndarray, sc: ScSidecar, kind: str) -> bytes | None:
    bands, top = COMPOSITES[kind]
    layers = [_reflectance(stack, sc, b) for b in bands]
    if any(layer is None for layer in layers):
        return None
    step = max(1, math.ceil(max(stack.shape[1:]) / THUMB_MAX_PX))
    rgb = np.stack([_shrink(layer, step) for layer in layers])  # type: ignore[arg-type]
    rgb = np.where(np.isfinite(rgb), rgb, 0.0)
    return _jpeg((np.clip(rgb / top, 0, 1) * 255).round().astype(np.uint8))


# Sentinel-1 quicklook (dB): R = VV in [-25, 0], G = VH in [-30, -5], B = VV-VH in [-10, 10].
S1_STRETCH = {"VV": (-25.0, 0.0), "VH": (-30.0, -5.0), "VV_VH_DIFF": (-10.0, 10.0)}


def _s1_quicklook(stack: np.ndarray, names: list[str]) -> bytes | None:
    step = max(1, math.ceil(max(stack.shape[1:]) / THUMB_MAX_PX))
    layers = []
    for name, (lo, hi) in S1_STRETCH.items():
        if name not in names:
            return None
        band = _shrink(stack[names.index(name)].astype(np.float64), step)
        layers.append(np.clip((band - lo) / (hi - lo), 0, 1))
    rgb = np.where(np.isfinite(np.stack(layers)), np.stack(layers), 0.0)
    return _jpeg((rgb * 255).round().astype(np.uint8))


def s1_of(rel: str) -> str:
    """The S1 member of a pair from its S2 member: matching ``s1/``/``s2/`` paths and names."""
    folder, name = rel.rsplit("/", 1)
    return "s1/" + folder.removeprefix("s2/") + "/S1_" + name.removeprefix("S2_")


PREVIEW_KINDS = ("rgb", "swir", "s1")


def preview(src: Source, tif: str, kind: str) -> bytes | None:
    """One quicklook (JPEG) of the pair whose S2 tile is ``tif``, made now."""
    side = tif[: -len(".tif")] + ".json"
    if kind == "s1":
        s1_tif = s1_of(tif)
        if not src.exists(s1_tif):
            return None
        meta = DliteSidecar.model_validate_json(src.read(s1_of(side)))
        return _s1_quicklook(_read(src, s1_tif), meta.dimensions.bands)
    sc = ScSidecar.model_validate_json(src.read(side))
    return _composite(_read(src, tif), sc, kind)


def _spectrum(src: Source, tif: str, sc: ScSidecar) -> list[float | None]:
    """Median reflectance of each band: from the sidecar medians, else from the GeoTIFF."""
    by_name: dict[str, BandRecord] = {b.name: b for b in sc.bands if b.status == "ok"}
    if all(b.median is not None for b in by_name.values()):
        return [
            round((by_name[n].median + sc.radiometry.boa_add_offset.get(n, 0.0))  # type: ignore[operator]
                  / config.S2_QUANTIFICATION, 4) if n in by_name else None
            for n in config.S2_ALL_BANDS
        ]  # fmt: skip
    stack = _read(src, tif)
    values: list[float | None] = []
    for b in config.S2_ALL_BANDS:
        refl = _reflectance(stack, sc, b)
        finite = refl[np.isfinite(refl)] if refl is not None else np.empty(0)
        values.append(round(float(np.median(finite)), 4) if finite.size else None)
    return values


def band_codes(sc: ScSidecar) -> str:
    """One letter per S2 band, in ``S2_ALL_BANDS`` order: d D_lite, g GEE, p Planetary
    Computer, f failed, u not added (reference check failed), - not in the tile."""
    by_name = {b.name: b for b in sc.bands}
    out = []
    for name in config.S2_ALL_BANDS:
        b = by_name.get(name)
        if b is None:
            out.append("u" if any(n.name == name for n in sc.not_added) else "-")
        elif b.status != "ok":
            out.append("f")
        elif b.origin == "dlite":
            out.append("d")
        else:
            out.append("p" if b.provider == "pc" else "g")
    return "".join(out)


def _row(src: Source, side: str) -> dict[str, object]:
    """The short record of one pair, embedded in the page for every pair."""
    sc = ScSidecar.model_validate_json(src.read(side))
    tif = side[: -len(".json")] + ".tif"
    s1_side = s1_of(side)
    s1 = None
    if src.exists(s1_side) and src.exists(s1_of(tif)):
        meta = DliteSidecar.model_validate_json(src.read(s1_side))
        s1 = {
            "name": s1_of(tif).rsplit("/", 1)[-1],
            "acquired": meta.acquisition.satellite_acquisition_time.isoformat(),
            "gap_days": round(meta.quality_metrics.temporal_gap_days, 3),
        }
    rad, sar, cls, fb = sc.radiometry, sc.paired_sar, sc.classification, sc.fallback
    return {
        "name": tif.rsplit("/", 1)[-1],
        "path": tif,
        "state": sc.verdict,
        "class": cls.land_cover_class,
        "season": cls.season,
        "region": cls.geographic_region,
        "purity": round(cls.label_purity, 4),
        "product_id": rad.product_id,
        "baseline": rad.processing_baseline,
        "offsets": sorted(set(rad.boa_add_offset.values())),
        "sar": {"pass": sar.orbit_pass, "track": sar.relative_orbit_number},
        "codes": band_codes(sc),
        "failed": [b.name for b in sc.bands if b.status != "ok"],
        "dlite_checksum_ok": sc.source.dlite_checksum_ok,
        "fallback": {"used": fb.used, "provider": fb.provider} if fb else None,
        "provider": sc.provider or ("pc" if fb and fb.used else "gee"),
        "timings": {k: round(v, 1) for k, v in sc.timings_s.items()},
        "created": sc.created.isoformat(timespec="seconds"),
        "prov": src.exists(side[: -len(".json")] + ".prov.json"),
        "s1": s1,
        "spectrum": _spectrum(src, tif, sc),
    }


def detail(src: Source, tif: str) -> dict[str, object]:
    """Everything about one tile, for the detail view (read when it is opened)."""
    side = tif[: -len(".tif")] + ".json"
    sc = ScSidecar.model_validate_json(src.read(side))
    rad, sar, cls = sc.radiometry, sc.paired_sar, sc.classification
    prov = side[: -len(".json")] + ".prov.json"
    s1 = None
    if src.exists(s1_of(tif)) and src.exists(s1_of(side)):
        meta = DliteSidecar.model_validate_json(src.read(s1_of(side)))
        stack = _read(src, s1_of(tif)).astype(np.float64)
        stats = []
        for i, name in enumerate(meta.dimensions.bands):
            finite = stack[i][np.isfinite(stack[i])]
            stats.append(
                {
                    "name": name,
                    "min": round(float(finite.min()), 2) if finite.size else None,
                    "median": round(float(np.median(finite)), 2) if finite.size else None,
                    "max": round(float(finite.max()), 2) if finite.size else None,
                    "nonfinite": int(stack[i].size - finite.size),
                }
            )
        acq = meta.acquisition
        s1 = {
            "name": s1_of(tif).rsplit("/", 1)[-1],
            "image_id": meta.provenance.sentinel1_image_id,
            "acquired": acq.satellite_acquisition_time.isoformat(),
            "spacecraft": acq.spacecraft,
            "gap_days": meta.quality_metrics.temporal_gap_days,
            "checksum": meta.integrity.checksum,
            "bands": stats,
        }
    return {
        "hemisphere": cls.hemisphere,
        "s2_orbit": rad.sensing_orbit_direction,
        "radiometry_error": rad.error,
        "sar": {
            "pass": sar.orbit_pass,
            "track": sar.relative_orbit_number,
            "platform": sar.platform,
            "mode": sar.instrument_mode,
            "image_id": sar.image_id,
        },
        "crs": sc.grid.crs,
        "size": [sc.grid.width, sc.grid.height],
        "tile_zone": utm_zone(sc.grid.crs),
        "granule_zone": granule_utm_zone(rad.product_id or ""),
        "dlite": {"doi": sc.source.dlite_doi, "file": sc.source.dlite_filename},
        "reference": [r.model_dump() for r in sc.reference_check],
        "bands": [
            {
                "name": b.name,
                "origin": b.origin,
                "provider": b.provider,
                "status": b.status,
                "res": b.native_resolution_m,
                "group": b.request_group,
                "attempts": b.attempts,
                "min": b.min,
                "max": b.max,
                "median": b.median,
                "nonfinite": b.n_nonfinite,
                "hash": b.xxhash64,
                "error": b.error,
            }
            for b in sc.bands
        ],
        "not_added": [n.model_dump() for n in sc.not_added],
        "fallback": sc.fallback.model_dump() if sc.fallback else None,
        "timings": sc.timings_s,
        "tool": sc.tool.model_dump(),
        "checksum": sc.integrity.checksum,
        "s1": s1,
        "prov_doc": json.loads(src.read(prov)) if src.exists(prov) else None,
    }


def crosscheck_index(report: dict | None) -> dict[str, dict]:
    """The crosscheck report by tile file name, reduced to what the page shows."""
    if not report:
        return {}
    index = {}
    for t in report.get("tiles", []):
        bands = t.get("bands", [])
        index[t["filename"]] = {
            "error": t.get("error"),
            "identical": t.get("error") is None
            and bool(bands)
            and all(b["n_agree"] == b["n_pixels"] for b in bands),
            "differ": [b["band"] for b in bands if b["n_agree"] != b["n_pixels"]],
            "share_differ_max": max(
                ((b["n_pixels"] - b["n_agree"]) / b["n_pixels"] for b in bands), default=0.0
            ),
            "baseline": t.get("pc_processing_baseline"),
            "seconds": t.get("seconds"),
        }
    return index


def load_review(folder: Path) -> dict[str, dict]:
    path = folder / REVIEW_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_review(
    folder: Path, name: str, *, flag: bool, note: str
) -> tuple[dict[str, object], bool]:
    """Record a curator mark for tile ``name`` (atomic write; tiles are never touched).

    Returns the mark and whether it changed: saving the same flag and note again writes
    nothing and keeps the time of the last real change.
    """
    mark = {"flag": flag, "note": note.strip()[:2000], "at": datetime.now(UTC).isoformat()}
    with _REVIEW_LOCK:
        marks = load_review(folder)
        old = marks.get(name, {"flag": False, "note": "", "at": None})
        if (old["flag"], old["note"]) == (mark["flag"], mark["note"]):
            return old, False
        if mark["flag"] or mark["note"]:
            marks[name] = mark
        else:
            marks.pop(name, None)
        tmp = folder / (REVIEW_FILE + ".part")
        tmp.write_text(json.dumps(marks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(folder / REVIEW_FILE)
    return mark, True


TileCache = dict[str, tuple[tuple[object, ...], dict[str, object]]]


def collect(src: Source, cache: TileCache | None = None, *, progress: bool = True) -> list[dict]:
    """Rows of every pair; with ``cache``, only new or changed tiles are read again."""
    cache = {} if cache is None else cache
    rows = []
    for side in bar(src.sidecars(), desc="report", unit="tile", disable=not progress or None):
        tif = side[: -len(".json")] + ".tif"
        stamp = tuple(
            src.key(r) for r in (side, tif, side[:-5] + ".prov.json", s1_of(side), s1_of(tif))
        )
        if stamp[1] is None:
            continue  # being written: the next refresh takes it
        hit = cache.get(side)
        if hit is None or hit[0] != stamp:
            try:
                hit = (stamp, _row(src, side))
            except (FileNotFoundError, ValueError):
                continue  # half written: the next refresh takes it
            cache[side] = hit
        rows.append(hit[1])
    return rows


def _signature(tiles: list[dict], *reports: Path | None) -> str:
    """Changes when a tile, a mark or a report changes (the live page polls it)."""
    parts = [
        f"{t['name']}|{t['created']}|{(t['review'] or {}).get('at')}|{t['prov']}|{bool(t['s1'])}"
        for t in tiles
    ]
    parts += [r.name if r else "-" for r in reports]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _s1_not_copied() -> dict[str, str]:
    """Why an S1 tile was not copied, from every complete run (the newest one wins).

    One provider per run: the pairs of a Planetary Computer run are not the ones of the
    Earth Engine run before it, so the last run alone would lose the earlier reasons.
    """
    found: dict[str, str] = {}
    for path in logs.reports("complete"):
        found.update((logs.load(path) or {}).get("s1_not_copied") or {})
    return found


def latest_crosscheck(src: Source) -> Path | None:
    """Most recent report of the second-provider study made on this folder."""
    root = str(src.root.resolve())
    for path in sorted(config.LOG_DIR.glob("crosscheck_pc_*.json"), reverse=True):
        if (logs.load(path) or {}).get("folder") == root:
            return path
    return None


def payload(
    src: Source,
    cache: TileCache | None = None,
    *,
    progress: bool = True,
    with_crosscheck: bool = True,
) -> dict:
    """The data of the page. ``with_crosscheck``: show the second-provider study made on
    this folder, if any (the report inside a release is built without it)."""
    cross_path = latest_crosscheck(src) if with_crosscheck else None
    bands_path = logs.latest("bands")
    complete_path = logs.latest("complete")  # the last run, any provider
    timed_path = logs.latest_with("complete_gee", "n_tiles")  # timings: last GEE run with work
    cross = logs.load(cross_path)
    index = crosscheck_index(cross)
    # Output folder: curator marks (review.json, used by package). Release: notes that
    # are part of its data (sc_notes.jsonld, W3C Web Annotation; notes.py).
    marks = load_review(src.root) if src.writable else notes.current(src.root)
    tiles = [
        {**t, "crosscheck": index.get(t["name"]), "review": marks.get(t["name"])}
        for t in collect(src, cache, progress=progress)
    ]
    counts: dict[str, int] = {}
    for t in tiles:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    timed = logs.load(timed_path)
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool_version": __version__,
        "folder": src.describe(),
        "writable": src.writable,
        "notes": not src.writable,  # marks on a release are notes in sc_notes.jsonld
        "page_size": PAGE_SIZE,
        "bands_order": list(config.S2_ALL_BANDS),
        "wavelength_nm": WAVELENGTH_NM,
        "dlite_bands": list(config.S2_DLITE_BANDS),
        "counts": counts,
        "n_second_provider": sum(1 for t in tiles if t["provider"] != "gee"),
        "crosscheck": {
            "report": cross_path.name if cross_path else None,
            **{
                k: cross.get(k)
                for k in (
                    "n_tiles_checked",
                    "n_tiles_identical",
                    "n_tiles_error",
                    "by_zone",
                    "s_per_tile_overall",
                    "workers",
                )
            },
        }
        if cross
        else None,
        "bands_check": {
            "report": bands_path.name,
            **{k: v for k, v in (logs.load(bands_path) or {}).items() if k != "flags"},
        }
        if bands_path
        else None,
        "complete_run": {
            "report": timed_path.name,
            **{k: timed.get(k) for k in ("workers", "seconds_per_tile", "provider", "n_tiles")},
        }
        if timed and timed_path
        else None,
        "n_flagged": sum(1 for t in tiles if t["review"] and t["review"]["flag"]),
        "s1_not_copied": _s1_not_copied(),
        "signature": _signature(tiles, cross_path, bands_path, complete_path),
        "tiles": tiles,
    }


def render(data: dict, *, live: bool = False) -> str:
    """The HTML page with ``data`` embedded; ``live`` turns on refresh and review (serve)."""
    template = (
        resources.files("strata_complete.report")
        .joinpath("templates", "report.html")
        .read_text(encoding="utf-8")
    )
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    page = template.replace("/*__DATA__*/null", blob, 1)
    return page.replace("/*__LIVE__*/false", "true" if live else "false", 1)


def build(folder: Path, out: Path, *, with_crosscheck: bool = True) -> tuple[Path, dict[str, int]]:
    data = payload(open_source(folder), with_crosscheck=with_crosscheck)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data), encoding="utf-8")
    return out, data["counts"]
