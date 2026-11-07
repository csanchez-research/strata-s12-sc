"""Microsoft Planetary Computer: a second provider of the same ESA Sentinel-2 products.

Collection ``sentinel-2-l2a``: one Cloud-Optimised GeoTIFF per band, read through the
public STAC API with a short-lived SAS token (no account). A scene is found by its ESA
product name (``image_id`` in :func:`fetch`): the item id is the name without its
``_Nxxxx`` baseline field, and the item is accepted only if ``s2:product_uri`` is
exactly ``PRODUCT_ID.SAFE``, so a reprocessed product is never taken for the same
scene. Each band is read through a ``WarpedVRT`` onto the tile grid with nearest
neighbour. The assets are uint16 with nodata 0 and still contain ``BOA_ADD_OFFSET``;
nodata becomes -inf, as in Earth Engine float downloads.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import TYPE_CHECKING

import numpy as np
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

from strata_complete import config

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from strata_complete.providers import Grid

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"
SAS_URL = f"https://planetarycomputer.microsoft.com/api/sas/v1/token/{COLLECTION}"
NODATA = 0  # nodata of the assets
MASKED = float("-inf")  # how Earth Engine float downloads carry masked pixels
TOKEN_MAX_AGE_S = 30 * 60  # renewed well before it expires
BAND_THREADS = 4  # bands of one tile read in parallel

# GDAL settings for reading remote COGs: no directory listing on open (the
# blob container is huge), merged range requests, a block cache.
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_TIMEOUT": str(int(config.HTTP_TIMEOUT_S)),
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "VSI_CACHE": "TRUE",
}

_BASELINE = re.compile(r"_N\d{4}_")
_token_lock = threading.Lock()
_token: tuple[str, float] | None = None


class PcError(RuntimeError):
    """Planetary Computer did not return the expected scene or pixels."""


def asset_key(band: str) -> str:
    """GEE band name to Planetary Computer asset key (B2 -> B02, B8A -> B8A)."""
    suffix = band[1:]
    return f"B{suffix.zfill(2)}" if suffix.isdigit() else band


def item_id(product_id: str) -> str:
    """Planetary Computer item id of an ESA product name (baseline field dropped)."""
    return _BASELINE.sub("_", product_id, count=1)


def _get(url: str) -> requests.Response:
    return requests.get(
        url, timeout=config.HTTP_TIMEOUT_S, headers={"User-Agent": config.USER_AGENT}
    )


@lru_cache(maxsize=256)
def item_for_product(product_id: str) -> dict:
    """STAC item of ``product_id``; ``PcError`` unless it is exactly that product."""
    iid = item_id(product_id)
    resp = _get(f"{STAC_URL}/collections/{COLLECTION}/items/{iid}")
    if resp.status_code == requests.codes.not_found:
        msg = f"{product_id}: no Planetary Computer item {iid}"
        raise PcError(msg)
    resp.raise_for_status()
    item = resp.json()
    uri = item.get("properties", {}).get("s2:product_uri")
    if uri != f"{product_id}.SAFE":
        msg = f"{product_id}: item {iid} is another product ({uri})"
        raise PcError(msg)
    return item


def _sas_token() -> str:
    global _token  # noqa: PLW0603 - one token per process, shared by threads
    with _token_lock:
        if _token is None or time.monotonic() - _token[1] > TOKEN_MAX_AGE_S:
            resp = _get(SAS_URL)
            resp.raise_for_status()
            _token = (resp.json()["token"], time.monotonic())
        return _token[0]


def _read_band(href: str, grid: Grid) -> np.ndarray:
    with (
        rasterio.Env(**GDAL_ENV),
        rasterio.open(f"/vsicurl/{href}?{_sas_token()}") as src,
        WarpedVRT(
            src,
            crs=grid.crs,
            transform=rasterio.Affine(*grid.transform),
            width=grid.width,
            height=grid.height,
            resampling=Resampling.nearest,
        ) as vrt,
    ):
        data = vrt.read(1)
    out = data.astype(np.float32)
    out[data == NODATA] = MASKED
    return out


def fetch(image_id: str, bands: Sequence[str], grid: Grid, out_path: Path) -> Path:
    """Read ``bands`` of the ESA product ``image_id`` on ``grid`` to ``out_path``, as float32."""
    assets = item_for_product(image_id)["assets"]
    missing = [b for b in bands if asset_key(b) not in assets]
    if missing:
        msg = f"{image_id}: no asset for {missing}"
        raise PcError(msg)
    hrefs = [assets[asset_key(b)]["href"] for b in bands]
    with ThreadPoolExecutor(max_workers=min(BAND_THREADS, len(hrefs))) as pool:
        arrays = list(pool.map(lambda h: _read_band(h, grid), hrefs))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": len(arrays),
        "crs": grid.crs,
        "transform": rasterio.Affine(*grid.transform),
        "width": grid.width,
        "height": grid.height,
        "nodata": MASKED,
        "compress": "deflate",
    }
    with rasterio.open(out_path, "w", **profile) as dst:
        for i, (band, arr) in enumerate(zip(bands, arrays, strict=True), start=1):
            dst.write(arr, i)
            dst.set_band_description(i, band)
    return out_path


def preflight() -> list[tuple[bool, str]]:
    """Check that the STAC API and the token endpoint answer (no credentials involved)."""
    checks: list[tuple[bool, str]] = []
    for label, url in (
        ("STAC API", f"{STAC_URL}/collections/{COLLECTION}"),
        ("SAS token", SAS_URL),
    ):
        try:
            ok = _get(url).status_code == requests.codes.ok
        except requests.RequestException as exc:
            checks.append((False, f"{label} not reachable: {type(exc).__name__}: {exc}"))
            return checks
        checks.append((ok, f"{label} reachable" if ok else f"{label}: unexpected answer"))
    return checks


def version() -> str:
    gdal = rasterio.__gdal_version__
    return f"STAC {STAC_URL} ({COLLECTION}); rasterio {rasterio.__version__}, GDAL {gdal}"
