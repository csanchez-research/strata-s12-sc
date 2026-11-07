"""Download the published bands again from Earth Engine and compare (``compare`` command).

For both tiles of every pair of an input folder, the bands D_lite publishes (B4/B3/B2
or VV/VH) are requested on the exact grid of the tile and compared pixel by pixel.
Each copy is deleted once compared (``keep=True`` keeps it in ``data/gee/``); the
report goes to ``log/compare_gee_<UTC>.json``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.errors
from rasterio.io import MemoryFile

from strata_complete import config
from strata_complete.dlite import sample
from strata_complete.dlite.record import Record, RecordError
from strata_complete.providers import Grid, gee
from strata_complete.runtime import logs
from strata_complete.runtime.progress import bar
from strata_complete.schema import DliteSidecar


@dataclass
class BandCompare:
    band: str
    n_pixels: int
    n_equal: int
    max_abs_diff: float
    n_nonfinite_provider: int
    n_nonfinite_dlite: int


@dataclass
class TileCompare:
    filename: str
    sensor: str
    image_id: str
    provider_file: str | None
    bands: list[BandCompare]
    error: str | None = None


def _compare(dlite: np.ndarray, prov: np.ndarray, name: str) -> BandCompare:
    both = np.isfinite(dlite) & np.isfinite(prov)
    diff = np.abs(dlite[both].astype(np.float64) - prov[both].astype(np.float64))
    return BandCompare(
        band=name,
        n_pixels=int(dlite.size),
        n_equal=int((dlite == prov).sum()),
        max_abs_diff=float(diff.max()) if diff.size else float("nan"),
        n_nonfinite_provider=int((~np.isfinite(prov)).sum()),
        n_nonfinite_dlite=int((~np.isfinite(dlite)).sum()),
    )


def compare_tile(record: Record, tif: str, *, keep: bool = False) -> TileCompare:
    """Compare one tile of the record (``s2/...tif`` or ``s1/...tif``) with GEE."""
    name = Path(tif).name
    try:
        meta = DliteSidecar.model_validate_json(record.read(sample.sidecar_of(tif)))
    except (RecordError, ValueError) as exc:
        return TileCompare(name, "", "", None, [], error=str(exc))
    if meta.sensor == "S2":
        image_id = meta.provenance.sentinel2_image_id
        ref_bands = config.S2_DLITE_BANDS
    else:
        image_id = meta.provenance.sentinel1_image_id
        ref_bands = config.S1_DLITE_BANDS
    out = config.DATA_DIR / "gee" / meta.sensor.lower() / name
    result = TileCompare(name, meta.sensor, image_id, str(out) if keep else None, [])
    try:
        with MemoryFile(record.read(tif)) as mem, mem.open() as d_src:
            dlite, grid = d_src.read(), Grid.from_dataset(d_src)
        gee.fetch(image_id, ref_bands, grid, out)  # only what is compared
        with rasterio.open(out) as p_src:
            prov = p_src.read()
    except (gee.GeeError, RecordError, OSError, ValueError, rasterio.errors.RasterioError) as exc:
        result.error = str(exc)
        return result
    finally:
        if not keep:
            out.unlink(missing_ok=True)
    for i, band in enumerate(ref_bands):
        stored = dlite[meta.dimensions.bands.index(band)]  # band order from the sidecar
        result.bands.append(_compare(stored, prov[i], band))
    return result


def compare_selection(
    in_dir: Path, record: Record, *, keep: bool = False
) -> tuple[list[TileCompare], Path]:
    """Request the published bands of every S1 and S2 tile of the selection again from
    Earth Engine and compare them with D_lite; returns the comparisons and the report path."""
    rows = sample.read_manifest(in_dir / sample.MANIFEST)
    tifs = [r[k] for r in rows for k in ("s2_file", "s1_file")]
    results = [compare_tile(record, t, keep=keep) for t in bar(tifs, desc="compare", unit="tile")]
    report = logs.write(
        "compare_gee",
        {"input": str(in_dir), "record": record.describe(), "tiles": [asdict(r) for r in results]},
    )
    return results, report
