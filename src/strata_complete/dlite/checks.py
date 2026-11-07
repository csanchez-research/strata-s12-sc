"""Check D_lite tiles against their sidecars, with the TIFF as ground truth.

For every tile (a sidecar and its GeoTIFF):

1. the sidecar is validated against ``schema.DliteSidecar`` (strict);
2. the TIFF is read whole and the per-pixel xxHash64 is recomputed and compared
   with ``integrity.checksum``;
3. what the sidecar declares (dtype, nodata, CRS, band names, size, footprint) is
   compared with what the TIFF says, and every difference is reported.

``check`` runs this on every pair of an input folder made by ``sample``, reading
the tiles from the record (``record.Record``: each file checked against its
sha256 first). The full report is written as JSON to ``log/``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import xxhash
from rasterio.io import MemoryFile
from rasterio.warp import transform_bounds

from strata_complete.dlite import sample
from strata_complete.dlite.record import Record, RecordError
from strata_complete.runtime import logs
from strata_complete.runtime.progress import bar
from strata_complete.schema import BandStats, DliteSidecar, TiffInfo

if TYPE_CHECKING:
    from pathlib import Path

    from rasterio.io import DatasetReader

BBOX_TOL_DEG = 1e-6


def pixel_hash(arr: np.ndarray) -> str:
    """xxHash64 of a pixel array as D_lite defines it: float32, C order."""
    return xxhash.xxh64(np.ascontiguousarray(arr, dtype=np.float32).tobytes()).hexdigest()


def read_tiff(src: DatasetReader, name: str, band_names: list[str]) -> TiffInfo:
    """Header, per-band statistics and pixel checksum of a GeoTIFF, as the file stores it."""
    arr = src.read()  # (bands, height, width), as stored
    stats = []
    for i, band_name in enumerate(band_names[: arr.shape[0]]):
        band = arr[i]
        finite = np.isfinite(band) if band.dtype.kind == "f" else np.ones(band.shape, bool)
        vals = band[finite]
        stats.append(
            BandStats(
                name=band_name,
                min=float(vals.min()) if vals.size else float("nan"),
                max=float(vals.max()) if vals.size else float("nan"),
                n_zero=int((band == 0).sum()),
                n_nonfinite=int((~finite).sum()),
            ),
        )
    return TiffInfo(
        path=name,
        width=src.width,
        height=src.height,
        count=src.count,
        dtype=str(arr.dtype),
        nodata=src.nodata,
        crs=str(src.crs),
        transform=tuple(src.transform)[:6],
        bounds_native=tuple(src.bounds),
        bounds_wgs84=tuple(transform_bounds(src.crs, "EPSG:4326", *src.bounds)),
        descriptions=list(src.descriptions),
        xxhash64=pixel_hash(arr),
        bands=stats,
    )


@dataclass
class TileCheck:
    sidecar: str
    schema_ok: bool
    schema_error: str | None = None
    checksum_ok: bool | None = None
    findings: list[str] = field(default_factory=list)
    tiff: dict[str, object] | None = None
    record_error: str | None = None  # the file is not what the record lists


def _compare(meta: DliteSidecar, info: TiffInfo) -> list[str]:
    dim, findings = meta.dimensions, []
    if info.dtype != dim.dtype:
        findings.append(f"dtype: sidecar {dim.dtype}, tiff {info.dtype}")
    if info.nodata is None or not (np.isneginf(info.nodata)):
        findings.append(f"nodata: sidecar {dim.nodata}, tiff {info.nodata}")
    if (info.count, info.width, info.height) != (len(dim.bands), dim.width, dim.height):
        findings.append(
            f"shape: sidecar {len(dim.bands)}x{dim.width}x{dim.height}, "
            f"tiff {info.count}x{info.width}x{info.height}",
        )
    if info.descriptions != dim.bands:
        findings.append(f"band names: sidecar {dim.bands}, tiff {info.descriptions}")
    if info.crs != meta.coordinates.crs:
        findings.append(f"crs: sidecar {meta.coordinates.crs}, tiff {info.crs}")
    diff = max(abs(a - b) for a, b in zip(info.bounds_wgs84, meta.coordinates.bbox, strict=True))
    if diff > BBOX_TOL_DEG:
        findings.append(f"bbox: max difference {diff:.3e} deg vs tiff bounds")
    return findings


def check_bytes(name: str, sidecar: bytes, tif: bytes | None) -> TileCheck:
    """Check one tile given the bytes of its sidecar and GeoTIFF (``None``: no GeoTIFF)."""
    result = TileCheck(sidecar=name, schema_ok=False)
    try:
        meta = DliteSidecar.model_validate(json.loads(sidecar))
    except ValueError as exc:
        result.schema_error = str(exc)
        return result
    result.schema_ok = True
    if tif is None:
        result.findings.append("tif missing")
        return result
    with MemoryFile(tif) as mem, mem.open() as src:
        info = read_tiff(src, name.removesuffix(".json") + ".tif", meta.dimensions.bands)
    result.tiff = info.model_dump()
    result.checksum_ok = info.xxhash64 == meta.integrity.checksum
    result.findings += _compare(meta, info)
    return result


def problem(check: TileCheck) -> str | None:
    """Why a tile cannot be used as published (``None`` if it can)."""
    if check.record_error:
        return check.record_error
    if not check.schema_ok:
        lines = (check.schema_error or "").splitlines()
        where = f" ({lines[1]}: {lines[2].strip().split(' [')[0]})" if len(lines) > 2 else ""  # noqa: PLR2004
        return "sidecar does not follow the D_lite schema" + where
    if check.checksum_ok is None:
        return "tif missing"
    if not check.checksum_ok:
        return "pixel checksum does not match its sidecar"
    return None


def check_record_tile(record: Record, tif: str) -> TileCheck:
    """Check one tile of the record by its path (``s2/...tif``); a sha256 mismatch is a finding."""
    try:
        return check_bytes(
            sample.sidecar_of(tif), record.read(sample.sidecar_of(tif)), record.read(tif)
        )
    except RecordError as exc:
        return TileCheck(sidecar=sample.sidecar_of(tif), schema_ok=False, record_error=str(exc))


def check_selection(in_dir: Path, record: Record) -> tuple[list[TileCheck], Path]:
    """Check both tiles of every pair of an input folder made by ``sample``."""
    rows = sample.read_manifest(in_dir / sample.MANIFEST)
    tifs = [r[k] for r in rows for k in ("s2_file", "s1_file")]
    results = [check_record_tile(record, t) for t in bar(tifs, desc="check", unit="tile")]
    report = logs.write(
        "check",
        {"input": str(in_dir), "record": record.describe(), "tiles": [asdict(r) for r in results]},
    )
    return results, report
