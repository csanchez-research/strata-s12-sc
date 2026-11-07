"""Google Earth Engine provider (the original source of D_lite).

The tile is requested on the exact grid of the D_lite GeoTIFF: ``crs``,
``crs_transform`` and two-element ``dimensions`` with no ``region``. In
earthengine-api 1.7.49 this combination reprojects to exactly that grid
(checked in ``ee/image.py``). Same ``system:index``, ``.toFloat()`` and GEE's
default resampling as the D_lite pipeline, so the bands D_lite already ships
must come back identical.

Threads: earthengine-api 1.7.49 sends its API calls through one shared HTTP
session (``ee/_cloud_api_utils.py``). As a precaution, the calls to
the Earth Engine API (initialise, ``getInfo``, ``getDownloadURL``) are made one
at a time under a lock; the pixel downloads (plain HTTPS, the long part) run in
parallel.
"""

from __future__ import annotations

import http.client
import threading
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import ee

from strata_complete import config

if TYPE_CHECKING:
    from collections.abc import Sequence

    from strata_complete.providers import Grid

TIFF_MAGIC = (b"II\x2a\x00", b"MM\x00\x2a")
_initialised = False
_API_LOCK = threading.Lock()
CREDENTIALS_FILE = "/root/.config/earthengine/credentials"


class GeeError(RuntimeError):
    """Earth Engine is not usable, or did not return a valid GeoTIFF."""


def init() -> None:
    with _API_LOCK:
        _init_locked()


def _init_locked() -> None:
    global _initialised  # noqa: PLW0603 - one initialisation per process
    if _initialised:
        return
    if not config.GEE_PROJECT_ID:
        msg = "GEE_PROJECT_ID is not set: create gee.env (see gee.env.example)"
        raise GeeError(msg)
    if not Path(CREDENTIALS_FILE).is_file():
        msg = f"no Earth Engine credentials at {CREDENTIALS_FILE} (RUNBOOK step 4)"
        raise GeeError(msg)
    try:
        ee.Initialize(project=config.GEE_PROJECT_ID)
    except Exception as exc:
        msg = (
            f"Earth Engine initialisation failed for project {config.GEE_PROJECT_ID}: "
            f"{type(exc).__name__}: {exc} (credentials expired? RUNBOOK step 4)"
        )
        raise GeeError(msg) from exc
    _initialised = True


def preflight() -> list[tuple[bool, str]]:
    """Check that Earth Engine is usable. Never prints secrets.

    Returns (ok, message) lines; stops at the first failure.
    """
    checks: list[tuple[bool, str]] = []
    if not config.GEE_PROJECT_ID:
        return [(False, "GEE_PROJECT_ID not set: create gee.env (see gee.env.example)")]
    checks.append((True, f"GEE_PROJECT_ID = {config.GEE_PROJECT_ID}"))
    if not Path(CREDENTIALS_FILE).is_file():
        checks.append((False, f"no credentials file at {CREDENTIALS_FILE} (see RUNBOOK step 4)"))
        return checks
    checks.append((True, f"credentials file present at {CREDENTIALS_FILE}"))
    try:
        init()
        reply = ee.String("ok").getInfo()
    except Exception as exc:  # noqa: BLE001 - report any failure, never crash the shell
        checks.append((False, f"Earth Engine not reachable: {type(exc).__name__}: {exc}"))
        return checks
    checks.append((reply == "ok", "Earth Engine reachable"))
    return checks


def get_info(obj: ee.ComputedObject, label: str) -> object:
    """``obj.getInfo()`` under the API lock; Earth Engine errors become ``GeeError``."""
    init()
    try:
        with _API_LOCK:
            return obj.getInfo()
    except ee.EEException as exc:
        msg = f"{label}: {exc}"
        raise GeeError(msg) from exc


def scene_properties(image_id: str) -> dict[str, object]:
    """Image properties exactly as GEE returns them (one metadata request)."""
    info = get_info(ee.Image(image_id), image_id)
    return dict(info.get("properties", {})) if isinstance(info, dict) else {}


def version() -> str:
    return str(ee.__version__)


def fetch(image_id: str, bands: Sequence[str], grid: Grid, out_path: Path) -> Path:
    """Download ``bands`` of ``image_id`` on ``grid`` to ``out_path``, as float32."""
    init()
    image = ee.Image(image_id).select(list(bands)).toFloat()
    return download_image(image, list(bands), f"{image_id} {list(bands)}", grid, out_path)


def download_image(
    image: ee.Image, bands: list[str], label: str, grid: Grid, out_path: Path
) -> Path:
    """Download an already built ``ee.Image`` on ``grid`` as a GeoTIFF ."""
    params = {
        "bands": bands,
        "crs": grid.crs,
        "crs_transform": list(grid.transform),
        "dimensions": [grid.width, grid.height],
        "format": "GEO_TIFF",
    }
    # Request errors (e.g. the request size limit) arrive here as EEException.
    try:
        with _API_LOCK:
            url = image.getDownloadURL(params)
    except ee.EEException as exc:
        msg = f"{label}: {exc}"
        raise GeeError(msg) from exc
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_S) as resp:  # noqa: S310
            data = resp.read()
    except http.client.HTTPException as exc:  # e.g. IncompleteRead: connection cut mid-body
        msg = f"{label}: download cut ({type(exc).__name__})"
        raise GeeError(msg) from exc
    if data[:4] not in TIFF_MAGIC:
        msg = f"{label}: GEE response is not a GeoTIFF: {data[:200]!r}"
        raise GeeError(msg)
    out_path.write_bytes(data)
    return out_path
