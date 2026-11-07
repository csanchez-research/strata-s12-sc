"""Read STRATA-S12 (D_lite) from its record: a local folder or Zenodo, one rule.

The record is pinned by the sha256 of its ``SHA256SUMS.txt``
(``config.RECORD_SHA256SUMS_SHA256``). That file gives the sha256 of every other
file of the record, and ``member_index.csv`` gives, for every file inside every
archive, its byte offset, size and sha256. So every byte used is checked:

* a small file of the record (manifest, index, README...) against
  ``SHA256SUMS.txt``;
* a tile or sidecar inside an archive against ``member_index.csv``. It is read
  alone, by its byte range: the archives are never unpacked and never copied.

Two sources, the same checks:

* ``local`` (``config.RECORD_DIR`` set): a folder with the files of the record,
  for example the folder it was built in, mounted read-only. Nothing is written
  there.
* ``zenodo`` (``config.RECORD_DIR`` empty): small files are downloaded to
  ``data/zenodo/<record>/`` with resume; members are fetched with HTTP Range
  requests (RFC 9110) on the archive URL.

Connection problems pause the run until the network is back
(``network.wait_until_online``) and the request is made again. A file or member
that does not match its sha256 raises ``RecordError``: nothing built from it is
used.
"""

from __future__ import annotations

import csv
import hashlib
import http.client
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from strata_complete import config
from strata_complete.runtime import network
from strata_complete.runtime.progress import bytes_bar

HTTP_PARTIAL_CONTENT = 206
HTTP_RANGE_NOT_SATISFIABLE = 416
_NET_ERRORS = (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError)
TILE_PREFIXES = ("s1/", "s2/")


class RecordError(RuntimeError):
    """A file or member of the record does not match its sha256, or is not in the record."""


@dataclass(frozen=True)
class Member:
    """One file inside one archive of the record (a row of ``member_index.csv``)."""

    archive: str
    path: str
    offset: int
    size: int
    sha256: str


def parse_sha256sums(text: str) -> dict[str, str]:
    """``<hex>  <name>`` per line (``sha256sum`` format)."""
    sums: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            digest, name = line.split(maxsplit=1)
            sums[name.lstrip("*").strip()] = digest.lower()
    return sums


def _sha256_file(path: Path) -> tuple[str, int]:
    sha, n = hashlib.sha256(), 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(config.HTTP_CHUNK_BYTES), b""):
            sha.update(chunk)
            n += len(chunk)
    return sha.hexdigest(), n


def _request(url: str, start: int = 0, end: int | None = None) -> urllib.request.Request:
    headers = {"User-Agent": config.USER_AGENT}
    if start > 0 or end is not None:
        headers["Range"] = f"bytes={start}-{'' if end is None else end}"
    return urllib.request.Request(url, headers=headers)  # noqa: S310 - https URL built from config


def _download_part(url: str, part: Path, label: str) -> None:
    """Append to ``part`` from where it stops (Range). Raises on network errors."""
    start = part.stat().st_size if part.exists() else 0
    try:
        with urllib.request.urlopen(_request(url, start), timeout=config.HTTP_TIMEOUT_S) as resp:  # noqa: S310
            resumed = start > 0 and resp.status == HTTP_PARTIAL_CONTENT
            done = start if resumed else 0  # server ignored Range: start again from zero
            length = resp.headers.get("Content-Length")
            total = done + int(length) if length else None
            with (
                part.open("ab" if resumed else "wb") as out,
                bytes_bar(total, label, initial=done) as progress,
            ):
                for chunk in iter(lambda: resp.read(config.HTTP_CHUNK_BYTES), b""):
                    out.write(chunk)
                    progress.update(len(chunk))
            if total is not None and part.stat().st_size < total:
                msg = f"{label}: connection closed at {part.stat().st_size} of {total} bytes"
                raise ConnectionError(msg)
    except urllib.error.HTTPError as exc:
        if exc.code != HTTP_RANGE_NOT_SATISFIABLE:  # 416: the .part is already complete
            raise


def _download(name: str, want: str) -> Path:
    """One small file of the Zenodo record in ``data/zenodo/<record>/``, sha256 checked."""
    dest = config.ZENODO_DIR / name
    if dest.is_file():
        if _sha256_file(dest)[0] == want:
            return dest
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    while True:
        try:
            _download_part(config.file_url(name), part, name)
            break
        except _NET_ERRORS as exc:
            if isinstance(exc, urllib.error.HTTPError):
                raise
            network.wait_until_online(f"{name}: {type(exc).__name__}: {exc}")
    got = _sha256_file(part)[0]
    if got != want:
        part.unlink()
        msg = f"{name}: sha256 {got} != {want} of the record; partial file removed"
        raise RecordError(msg)
    part.replace(dest)
    return dest


def _read_range(url: str, offset: int, size: int) -> bytes:
    """``size`` bytes from ``offset`` of a remote file; pauses while the network is down."""
    while True:
        try:
            req = _request(url, offset, offset + size - 1)
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_S) as resp:  # noqa: S310
                first = resp.headers.get("Content-Range", "")
                if resp.status != HTTP_PARTIAL_CONTENT or not first.startswith(f"bytes {offset}-"):
                    msg = f"{url}: no byte range from {offset} (status {resp.status}, {first!r})"
                    raise RecordError(msg)
                data = resp.read()
            if len(data) == size:
                return data
            reason = f"{len(data)} of {size} bytes received"
        except _NET_ERRORS as exc:
            if isinstance(exc, urllib.error.HTTPError):
                raise
            reason = f"{type(exc).__name__}: {exc}"
        network.wait_until_online(f"{url} bytes {offset}+{size}: {reason}")


class Record:
    """One version of D_lite, read from a folder or from Zenodo (see the module doc)."""

    def __init__(self, folder: Path | None) -> None:
        self.folder = folder
        self._lock = threading.Lock()
        self._sums: dict[str, str] | None = None
        self._files: dict[str, Path] = {}
        self._index: dict[str, Member] | None = None
        self._fds: dict[str, int] = {}

    @classmethod
    def from_config(cls) -> Self:
        return cls(Path(config.RECORD_DIR) if config.RECORD_DIR else None)

    def describe(self) -> str:
        return (
            f"{self.folder} (local copy)"
            if self.folder
            else f"Zenodo record {config.ZENODO_RECORD_ID}"
        )

    def sums(self) -> dict[str, str]:
        """``SHA256SUMS.txt`` of the record, checked against the pinned sha256."""
        with self._lock:
            if self._sums is not None:
                return self._sums
            want = config.RECORD_SHA256SUMS_SHA256
            if self.folder:
                path = self.folder / config.SHA256SUMS_NAME
                if not path.is_file():
                    msg = f"{path} not found: SC_RECORD_DIR must be the folder of the record"
                    raise RecordError(msg)
                if _sha256_file(path)[0] != want:
                    msg = (
                        f"{path}: not the {config.SHA256SUMS_NAME} of D_lite {config.DLITE_VERSION}"
                    )
                    raise RecordError(msg)
            else:
                path = _download(config.SHA256SUMS_NAME, want)
            self._sums = parse_sha256sums(path.read_text(encoding="utf-8"))
            self._files[config.SHA256SUMS_NAME] = path
            return self._sums

    def file(self, name: str) -> Path:
        """A file of the record, checked against ``SHA256SUMS.txt``; its local path."""
        sums = self.sums()
        with self._lock:
            if name in self._files:
                return self._files[name]
        if name not in sums:
            msg = f"{name!r} is not a file of the record ({config.SHA256SUMS_NAME})"
            raise RecordError(msg)
        if self.folder:
            path = self.folder / name
            if not path.is_file():
                msg = f"{path} not found in the record folder"
                raise RecordError(msg)
            got = _sha256_file(path)[0]
            if got != sums[name]:
                msg = (
                    f"{path}: sha256 {got} != {sums[name]}: the local copy differs from the record"
                )
                raise RecordError(msg)
        else:
            path = _download(name, sums[name])
        with self._lock:
            self._files[name] = path
        return path

    def names(self) -> list[str]:
        return sorted(self.sums())

    def index(self) -> dict[str, Member]:
        """Tiles and sidecars of the class archives, by path (``member_index.csv``)."""
        path = self.file(config.MEMBER_INDEX_NAME)
        with self._lock:
            if self._index is not None:
                return self._index
            archives = {config.dlite_tar_name(c) for c in config.DLITE_CLASSES}
            index: dict[str, Member] = {}
            with path.open(encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    if r["archive"] not in archives or not r["path"].startswith(TILE_PREFIXES):
                        continue
                    if r["path"] in index:
                        msg = f"{r['path']} is in two archives of the record"
                        raise RecordError(msg)
                    index[r["path"]] = Member(
                        r["archive"], r["path"], int(r["offset_data"]), int(r["size"]), r["sha256"]
                    )
            self._index = index
            return index

    def member(self, path: str) -> Member:
        try:
            return self.index()[path]
        except KeyError as exc:
            msg = f"{path} is not in the class archives of the record ({config.MEMBER_INDEX_NAME})"
            raise RecordError(msg) from exc

    def read(self, path: str) -> bytes:
        """The bytes of one file inside an archive, checked against its sha256."""
        m = self.member(path)
        if self.folder:
            data = os.pread(self._fd(m.archive), m.size, m.offset)
        else:
            data = _read_range(config.file_url(m.archive), m.offset, m.size)
        got = hashlib.sha256(data).hexdigest()
        if got != m.sha256:
            msg = f"{path} ({m.archive} at byte {m.offset}): sha256 {got} != {m.sha256}"
            raise RecordError(msg)
        return data

    def _fd(self, archive: str) -> int:
        with self._lock:
            fd = self._fds.get(archive)
            if fd is None:  # one open file per archive: opening is slow on a bind mount
                assert self.folder is not None  # noqa: S101
                fd = self._fds[archive] = os.open(self.folder / archive, os.O_RDONLY)
            return fd

    def verify_archive(self, name: str) -> tuple[str, int]:
        """sha256 and size of a whole archive, read once, compared with ``SHA256SUMS.txt``."""
        want = self.sums().get(name)
        if want is None:
            msg = f"{name!r} is not a file of the record ({config.SHA256SUMS_NAME})"
            raise RecordError(msg)
        if self.folder:
            path = self.folder / name
            with path.open("rb") as fh, bytes_bar(path.stat().st_size, name) as progress:
                sha, n = hashlib.sha256(), 0
                for chunk in iter(lambda: fh.read(config.HTTP_CHUNK_BYTES), b""):
                    sha.update(chunk)
                    n += len(chunk)
                    progress.update(len(chunk))
            got = sha.hexdigest()
        else:
            got, n = _stream_sha256(config.file_url(name), name)
        if got != want:
            msg = f"{name}: sha256 {got} != {want} of the record"
            raise RecordError(msg)
        return got, n

    def close(self) -> None:
        with self._lock:
            for fd in self._fds.values():
                os.close(fd)
            self._fds.clear()


def _stream_sha256(url: str, label: str) -> tuple[str, int]:
    """sha256 of a remote file read once; a dropped connection continues at the same byte."""
    sha, pos, total = hashlib.sha256(), 0, None
    with bytes_bar(None, label) as progress:
        while True:
            try:
                req = _request(url, pos)
                with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_S) as resp:  # noqa: S310
                    if pos and resp.status != HTTP_PARTIAL_CONTENT:
                        msg = f"{url}: server did not resume at byte {pos}"
                        raise RecordError(msg)
                    if total is None and resp.headers.get("Content-Length"):
                        total = int(resp.headers["Content-Length"])
                        progress.total = total
                    for chunk in iter(lambda: resp.read(config.HTTP_CHUNK_BYTES), b""):
                        sha.update(chunk)
                        pos += len(chunk)
                        progress.update(len(chunk))
                if total is None or pos >= total:
                    return sha.hexdigest(), pos
                reason = f"connection closed at byte {pos} of {total}"
            except _NET_ERRORS as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    raise
                reason = f"{type(exc).__name__}: {exc}"
            network.wait_until_online(f"{label}: {reason}")
