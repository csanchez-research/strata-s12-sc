"""Reproducible tar archives and file hashes (used by ``package``)."""

from __future__ import annotations

import hashlib
import tarfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from pathlib import Path

FIXED_MTIME = 0  # reproducible archives: every member carries the same time
BUF = 1 << 20


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(BUF):
            h.update(chunk)
    return h.hexdigest()


class _Hashing:
    """Read-only file wrapper that hashes what tarfile copies into the archive."""

    def __init__(self, fh: BinaryIO) -> None:
        self._fh, self.sha = fh, hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._fh.read(size)
        self.sha.update(data)
        return data


@dataclass(frozen=True)
class Member:
    """One file of a written archive: where its bytes are, and their sha256."""

    path: str
    offset_data: int
    size: int
    sha256: str


def write_tar(path: Path, members: list[tuple[str, Path]]) -> tuple[str, int, list[Member]]:
    """Deterministic tar (sorted members, fixed owner/mode/time); read back and verified.

    Returns the sha256 and size of the archive and, for every member, its byte
    offset, size and sha256 (hashed while it is written), for an index like the
    ``member_index.csv`` of D_lite.
    """
    tmp = path.with_name(path.name + ".part")
    members = sorted(members)
    digests: list[str] = []
    with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tar:
        for arcname, src in members:
            info = tarfile.TarInfo(arcname)
            info.size, info.mtime, info.mode = src.stat().st_size, FIXED_MTIME, 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with src.open("rb") as fh:
                reader = _Hashing(fh)
                tar.addfile(info, reader)  # type: ignore[arg-type]
                digests.append(reader.sha.hexdigest())
    with tarfile.open(tmp) as tar:
        back = [m for m in tar.getmembers() if m.isfile()]
    if [m.name for m in back] != [a for a, _ in members]:
        msg = f"{path.name}: members read back differ from the ones written"
        raise RuntimeError(msg)
    tmp.replace(path)
    index = [Member(m.name, m.offset_data, m.size, d) for m, d in zip(back, digests, strict=True)]
    return sha256(path), path.stat().st_size, index
