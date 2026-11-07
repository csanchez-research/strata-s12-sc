"""Data providers. One module per provider, same contract.

Every provider implements::

    fetch(image_id: str, bands: Sequence[str], grid: Grid, out_path: Path) -> Path

and writes a GeoTIFF with exactly ``grid`` (CRS, affine transform, width,
height) and the bands in the requested order, as float32.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rasterio.io import DatasetReader


@dataclass(frozen=True)
class Grid:
    """Exact pixel grid of a D_lite tile, read from its GeoTIFF."""

    crs: str
    transform: tuple[float, float, float, float, float, float]
    width: int
    height: int

    @classmethod
    def from_dataset(cls, src: DatasetReader) -> Grid:
        return cls(
            crs=str(src.crs),
            transform=tuple(src.transform)[:6],
            width=src.width,
            height=src.height,
        )


def utm_zone(crs: str) -> int | None:
    """UTM zone of an ``EPSG:326zz`` or ``EPSG:327zz`` CRS string, else None."""
    m = re.fullmatch(r"EPSG:32[67](\d\d)", crs)
    return int(m.group(1)) if m else None


def granule_utm_zone(product_id: str) -> int | None:
    """UTM zone of the Sentinel-2 granule, from the MGRS tile in the ESA product name."""
    m = re.search(r"_T(\d\d)[A-Z]{3}_", product_id)
    return int(m.group(1)) if m else None
