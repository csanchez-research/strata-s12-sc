"""What a run was made with, for its report.

Tool and library versions, the settings of the provider reads and the input, pinned by
sha256. Each study adds its own parameters (seed, fraction, workers) next to it.
"""

from __future__ import annotations

import platform
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import rasterio

from strata_complete import __version__, config
from strata_complete.runtime import archive

if TYPE_CHECKING:
    from pathlib import Path


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def run_context(release: Path | None = None) -> dict[str, object]:
    """Command, tool and library versions, record and provider settings of this run, for
    the report it writes (and the sha256 of a release's ``SHA256SUMS.txt``, if given)."""
    from strata_complete.providers import pc  # imported here: only when a report is written

    sums = release / config.SHA256SUMS_NAME if release else None
    return {
        "command": " ".join(["python -m strata_complete", *sys.argv[1:]]),
        "tool": {"name": "strata-complete", "version": __version__},
        "python": platform.python_version(),
        "libraries": {
            "numpy": np.__version__,
            "rasterio": rasterio.__version__,
            "gdal": rasterio.__gdal_version__,
        },
        "dlite": {
            "version": config.DLITE_VERSION,
            "doi": config.DLITE_VERSION_DOI,
            "sha256sums_sha256": config.RECORD_SHA256SUMS_SHA256,
        },
        "release_sha256sums_sha256": archive.sha256(sums) if sums and sums.is_file() else None,
        "planetary_computer": {
            "stac": pc.STAC_URL,
            "collection": pc.COLLECTION,
            "band_threads_per_tile": pc.BAND_THREADS,
            "gdal_env": dict(pc.GDAL_ENV),
            "max_attempts_per_request": config.MAX_ATTEMPTS_PER_REQUEST,
            "retry_wait_s": config.RETRY_WAIT_S,
        },
    }
