"""Remove what the tool can make again (``clean`` command).

Only these, under the working folders:

* ``data/pc/`` and ``data/gee/``: provider copies kept by ``crosscheck --keep``
  and ``compare --keep`` for inspection;
* ``data/pieces/``: per-tile downloads of ``complete`` left by an interruption;
* ``*.part`` files: writes cut short (a tile, a sidecar, a download); the
  command that wrote them starts them again.

Never touched: ``in/`` (the inputs; only its ``*.part`` files), ``out/`` (the
products; only its ``*.part`` files), ``log/`` and the record files in
``data/zenodo/``. Without ``--yes`` nothing is deleted: the command lists what it
would remove and how much space that frees.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from strata_complete import config

if TYPE_CHECKING:
    from pathlib import Path


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def targets() -> list[tuple[Path, int]]:
    """What ``clean`` would remove, with its size in bytes."""
    found = [
        d
        for d in (config.DATA_DIR / "pc", config.DATA_DIR / "gee", config.PIECES_DIR)
        if d.is_dir()
    ]
    for root in (config.DATA_DIR, config.IN_DIR, config.OUT_DIR):
        if root.is_dir():
            found += [
                p for p in root.rglob("*.part") if not any(p.is_relative_to(d) for d in found)
            ]
    return [(p, _size(p)) for p in sorted(found)]


def remove(paths: list[Path]) -> None:
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink(missing_ok=True)
