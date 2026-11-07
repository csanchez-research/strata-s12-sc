"""Settings of strata-complete: input record, paths, bands, network.

Every value read through ``_env`` can be overridden with an environment variable
prefixed ``SC_`` (for example ``SC_OUT_DIR``).

The input is one version of STRATA-S12 (D_lite), pinned by the sha256 of its
``SHA256SUMS.txt``: that file lists the sha256 of every other file of the record,
and ``member_index.csv`` (one of them) the sha256 of every file inside every
archive. If any byte differs, verification fails instead of using other data.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"SC_{name}", default)


# --- Input: STRATA-S12 (D_lite) 2.0.0 ----------------------------------------
# All versions: concept DOI 10.5281/zenodo.22646276. Version 2.0.0: record 23837956.
ZENODO_CONCEPT_DOI = "10.5281/zenodo.22646276"
ZENODO_RECORD_ID = _env("ZENODO_RECORD_ID", "23837956")
ZENODO_BASE_URL = _env("ZENODO_BASE_URL", "https://zenodo.org")
DLITE_VERSION = "2.0.0"
DLITE_VERSION_DOI = f"10.5281/zenodo.{ZENODO_RECORD_ID}"

SHA256SUMS_NAME = "SHA256SUMS.txt"
# sha256 of the SHA256SUMS.txt of version 2.0.0. Everything else is verified through it.
RECORD_SHA256SUMS_SHA256 = _env(
    "RECORD_SHA256SUMS_SHA256", "dd4b6f6eeae47e58de332ef5584a68355220ad7b306d20ac8057ebbce9a94c9c"
)
MEMBER_INDEX_NAME = "member_index.csv"
# The eight class archives. The record also holds strata-s12_lite_rejected.tar
# (pairs removed by the visual review, not for training): never read.
DLITE_CLASSES = (
    "bare_sparse_vegetation",
    "built_up",
    "cropland",
    "grassland",
    "herbaceous_wetland",
    "permanent_water_bodies",
    "shrubland",
    "tree_cover",
)


def dlite_tar_name(class_name: str) -> str:
    return f"strata-s12_lite_{class_name}.tar"


def file_url(name: str) -> str:
    """Direct download URL of one file of the pinned record."""
    return f"{ZENODO_BASE_URL}/records/{ZENODO_RECORD_ID}/files/{name}?download=1"


# --- Paths -------------------------------------------------------------------
# Working folders at the project root (mounted at /workspace), never in the
# image and never versioned (.gitignore):
#   data/ = downloads (Zenodo files, provider downloads)
#   in/   = inputs for the processes
#   log/  = logs of every run
#   out/  = finished products of the processes
ROOT = Path(_env("ROOT", "/workspace"))
DATA_DIR = Path(_env("DATA_DIR", str(ROOT / "data")))
IN_DIR = Path(_env("IN_DIR", str(ROOT / "in")))
LOG_DIR = Path(_env("LOG_DIR", str(ROOT / "log")))
OUT_DIR = Path(_env("OUT_DIR", str(ROOT / "out")))
SC_OUT_DIR = OUT_DIR / "strata-s12-sc"  # output tiles of complete
PIECES_DIR = DATA_DIR / "pieces"  # per-tile downloads while complete runs, then removed
ZENODO_DIR = DATA_DIR / "zenodo" / ZENODO_RECORD_ID  # small record files from Zenodo
# Where the record is read from: a folder with its files (verified as if they came from
# Zenodo), or empty to read from Zenodo.
RECORD_DIR = _env("RECORD_DIR", "")

# --- Network -----------------------------------------------------------------
HTTP_TIMEOUT_S = float(_env("HTTP_TIMEOUT_S", "60"))
HTTP_CHUNK_BYTES = int(_env("HTTP_CHUNK_BYTES", str(1 << 20)))
USER_AGENT = f"strata-complete (+https://doi.org/{ZENODO_CONCEPT_DOI})"

# Pause on connection problems (network.py). Any HTTP answer from a probe URL
# means the network is up. Waits double from the first to the maximum; after
# MAX_PAUSE_S without network the command stops and can be run again to resume.
ONLINE_PROBE_URLS = ("https://zenodo.org", "https://earthengine.googleapis.com")
PAUSE_FIRST_WAIT_S = float(_env("PAUSE_FIRST_WAIT_S", "5"))
PAUSE_MAX_WAIT_S = float(_env("PAUSE_MAX_WAIT_S", "60"))
MAX_PAUSE_S = float(_env("MAX_PAUSE_S", str(6 * 3600)))


# --- Source collection -------------------------------------------------------
# The collection D_lite's S2 tiles come from: ESA's L2A digital numbers, BOA_ADD_OFFSET
# included. complete refuses a scene of any other collection.
GEE_S2_COLLECTION = "COPERNICUS/S2_SR"

# --- Bands -------------------------------------------------------------------
# Measured bands at the product level of D_lite (never derived products).
# Sentinel-2 L2A: the 12 spectral bands of COPERNICUS/S2_SR (no B10 in L2A).
S2_ALL_BANDS = ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B11", "B12")
# L2A digital numbers to reflectance: (DN + BOA_ADD_OFFSET) / S2_QUANTIFICATION.
S2_QUANTIFICATION = 10_000
# Bands already published in D_lite: the reference for validating every provider.
S2_DLITE_BANDS = ("B4", "B3", "B2")
S1_DLITE_BANDS = ("VV", "VH")  # every D_lite pair is IW dual-pol VV+VH: nothing to add

# --- Google Earth Engine -------------------------------------------------------
# Project id from gee.env (GEE_PROJECT_ID). The credentials file written by
# `earthengine authenticate` lives in ../.credentials/earthengine/ next to the
# project and is mounted at /root/.config/earthengine (docker-compose.yml).
GEE_PROJECT_ID = os.environ.get("GEE_PROJECT_ID", "")

# Native resolution of each band (Earth Engine catalog of COPERNICUS/S2_SR; S1 GRD IW: 10 m).
NATIVE_RESOLUTION_M = {
    "B1": 60,
    "B2": 10,
    "B3": 10,
    "B4": 10,
    "B5": 20,
    "B6": 20,
    "B7": 20,
    "B8": 10,
    "B8A": 20,
    "B9": 60,
    "B11": 20,
    "B12": 20,
    "VV": 10,
    "VH": 10,
}

# Bands are requested in groups and composed locally, so no single request has
# to carry every band (providers limit the size of one request). See complete.py.
S2_REQUEST_GROUPS: dict[str, tuple[str, ...]] = {
    "s2_10m": ("B2", "B3", "B4", "B8"),
    "s2_20m": ("B5", "B6", "B7", "B8A", "B11", "B12"),
    "s2_60m": ("B1", "B9"),
}
# Request groups of one tile downloaded at the same time (threads); 1 = one after the other.
GROUPS_IN_PARALLEL = int(_env("GROUPS_IN_PARALLEL", "1"))
MAX_ATTEMPTS_PER_REQUEST = int(_env("MAX_ATTEMPTS_PER_REQUEST", "3"))
RETRY_WAIT_S = float(_env("RETRY_WAIT_S", "2"))  # wait before retry n: n * RETRY_WAIT_S

# Stratified sample (sample.py). The seed fixes which pair is drawn in each
# stratum, so anyone gets the same sample; it is recorded in selection.json.
# Override for one run with --seed / --per-stratum; --all takes every pair.
SAMPLE_SEED = int(_env("SAMPLE_SEED", "22"))
SAMPLE_PER_STRATUM = int(_env("SAMPLE_PER_STRATUM", "1"))

# W3C PROV documents (prov): namespace of the identifiers of the output.
PROV_NAMESPACE = _env("PROV_NAMESPACE", "urn:strata-s12-sc:")

# GeoTIFF encoding of the output tiles: the one of D_lite 2.0.0 (lossless DEFLATE
# with the floating-point predictor, 256 x 256 blocks), so the two tiles of a pair
# are stored alike.
OUT_TIFF = {
    "compress": "deflate",
    "predictor": 3,
    "zlevel": 9,
    "tiled": True,
    "blockxsize": 256,
    "blockysize": 256,
}
OUT_DATASET = "STRATA-S12-SC"

# Live report (serve): port inside the container, published on the host's
# 127.0.0.1 by docker-compose.yml; seconds between re-reads of the output folder.
SERVE_PORT = int(_env("SERVE_PORT", "8765"))
SERVE_REFRESH_S = float(_env("SERVE_REFRESH_S", "15"))

# Base image of the running container (set in docker/Dockerfile), recorded in
# every output sidecar.
BASE_IMAGE = os.environ.get("STRATA_BASE_IMAGE", "unknown (not running in the project image)")

# reproduce: share of every (class, season, region) stratum of the release rebuilt with
# the second provider.
REPRODUCE_FRACTION = 0.10
