"""Where the report reads tiles from: the output folder, or a release folder.

``FolderSource`` reads ``out/strata-s12-sc/`` as ``complete`` leaves it. ``ReleaseSource``
reads a folder made by ``package``: the tiles stay inside its tars and are read by byte
range through ``sc_member_index.csv``, each checked against its sha256. Both give paths
as in the archives, the bytes of a file, and a key that changes when a file changes.
"""

from __future__ import annotations

import csv
import hashlib
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

MEMBER_INDEX = "sc_member_index.csv"  # written by package (package.MEMBER_INDEX)


def is_release(folder: Path) -> bool:
    return (folder / MEMBER_INDEX).is_file()


class FolderSource:
    """Tiles as files under a folder."""

    writable = True

    def __init__(self, root: Path) -> None:
        self.root = root

    def describe(self) -> str:
        return str(self.root)

    def sidecars(self) -> list[str]:
        """S2 output sidecars, relative to the root (``S2_*.json``, not ``*.prov.json``)."""
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("S2_*.json")
            if not p.name.endswith(".prov.json")
        )

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def key(self, rel: str) -> float | None:
        """Changes when the file changes (modification time); None if absent."""
        try:
            return (self.root / rel).stat().st_mtime
        except FileNotFoundError:
            return None


class ReleaseSource:
    """Tiles inside the tars of a release, read through its member index."""

    writable = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._fds: dict[str, int] = {}
        self._index: dict[str, tuple[str, int, int, str]] = {}
        with (root / MEMBER_INDEX).open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                self._index[r["path"]] = (
                    r["archive"],
                    int(r["offset_data"]),
                    int(r["size"]),
                    r["sha256"],
                )

    def describe(self) -> str:
        return f"{self.root} (release, read from its tars)"

    def _names(self, prefix: str) -> list[str]:
        return sorted(
            p
            for p in self._index
            if p.rsplit("/", 1)[-1].startswith(prefix)
            and p.endswith(".json")
            and not p.endswith(".prov.json")
        )

    def sidecars(self) -> list[str]:
        return self._names("S2_")

    def exists(self, rel: str) -> bool:
        return rel in self._index

    def read(self, rel: str) -> bytes:
        archive, offset, size, want = self._index[rel]
        with self._lock:
            fd = self._fds.get(archive)
            if fd is None:
                fd = self._fds[archive] = os.open(self.root / archive, os.O_RDONLY)
        data = os.pread(fd, size, offset)
        if hashlib.sha256(data).hexdigest() != want:
            msg = f"{rel} in {archive}: sha256 differs from {MEMBER_INDEX}"
            raise OSError(msg)
        return data

    def key(self, rel: str) -> str | None:
        hit = self._index.get(rel)
        return hit[3] if hit else None


Source = FolderSource | ReleaseSource


def open_source(folder: Path) -> Source:
    return ReleaseSource(folder) if is_release(folder) else FolderSource(folder)
