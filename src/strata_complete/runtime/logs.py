"""Run reports: one JSON file per command run in ``log/``, named ``<kind>_<UTC>.json``."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from strata_complete import config

if TYPE_CHECKING:
    from pathlib import Path


def stamp() -> str:
    """UTC time as used in report file names, e.g. 20261027T103550Z."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def write(kind: str, payload: dict[str, object]) -> Path:
    """Write ``payload`` to ``log/<kind>_<UTC>.json`` (with ``created``) and return the path."""
    now = stamp()
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = config.LOG_DIR / f"{kind}_{now}.json"
    path.write_text(json.dumps({"created": now, **payload}, indent=2) + "\n", encoding="utf-8")
    return path


def reports(kind: str) -> list[Path]:
    """Reports of ``kind`` in ``log/``, oldest first, by the UTC time in their name
    (``complete`` matches ``complete_gee_<UTC>`` and ``complete_pc_<UTC>``)."""
    return sorted(config.LOG_DIR.glob(f"{kind}_*.json"), key=lambda p: p.stem.rsplit("_", 1)[-1])


def latest(kind: str) -> Path | None:
    """Most recent report of ``kind`` in ``log/``, if any."""
    found = reports(kind)
    return found[-1] if found else None


def latest_with(kind: str, key: str) -> Path | None:
    """Most recent report of ``kind`` whose ``key`` is set and not zero (e.g. a run that
    did some work, not one that only skipped finished tiles)."""
    for path in reversed(reports(kind)):
        if (load(path) or {}).get(key):
            return path
    return None


def load(path: Path | None) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path else None
