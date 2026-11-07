"""Every line a command prints carries its UTC time, on screen and in ``log/``.

``session`` replaces ``sys.stdout`` and ``sys.stderr`` for the length of one
command with a writer that puts the time (``2026-11-01T17:05:09Z``) at the start
of every line, and copies every finished line to ``log/<command>_<UTC>.txt``.
Nothing has to remember to do it: every ``print``, ``say`` and progress bar goes
through it.

Progress bars redraw one line with ``\\r``: on screen each redraw carries the time
of that redraw; in the text log only the final state of the bar is kept (the
line as it is when the bar ends), so the log stays readable. The first line says
what started (command, arguments, tool version), the last one the exit code and
the seconds it took.
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING

from strata_complete import __version__, config
from strata_complete.runtime import logs

if TYPE_CHECKING:
    from collections.abc import Iterator

STAMP_WIDTH = len("2026-11-01T17:05:09Z ")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Stamped:
    """Text stream that stamps every line and copies finished lines to a file."""

    def __init__(self, screen: IO[str], file: IO[str], lock: threading.Lock) -> None:
        self._screen, self._file, self._lock = screen, file, lock
        self._at_start = True  # next character starts a line (or a redraw)
        self._line = ""  # current line, last redraw only, for the file

    def write(self, text: str) -> int:
        with self._lock:
            out = []
            for ch in text:
                if self._at_start and ch not in "\r\n":
                    stamp = now() + " "
                    out.append(stamp)
                    self._line, self._at_start = stamp, False
                out.append(ch)
                if ch == "\n":
                    self._file.write(self._line.rstrip() + "\n")
                    self._line, self._at_start = "", True
                elif ch == "\r":
                    self._line, self._at_start = "", True
                else:
                    self._line += ch
            self._screen.write("".join(out))
            return len(text)

    def flush(self) -> None:
        with self._lock:
            self._screen.flush()
            self._file.flush()

    def isatty(self) -> bool:
        return self._screen.isatty()

    def fileno(self) -> int:
        return self._screen.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._screen, "encoding", "utf-8")


@contextmanager
def session(command: str, argv: list[str]) -> Iterator[dict[str, object]]:
    """Stamp and record everything printed while the command runs.

    Yields a dict: ``path`` (the text log) and ``code``, which the caller sets to
    the exit code of the command.
    """
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    run: dict[str, object] = {"path": config.LOG_DIR / f"{command}_{logs.stamp()}.txt"}
    lock = threading.Lock()
    old_out, old_err = sys.stdout, sys.stderr
    started = time.perf_counter()
    with Path(str(run["path"])).open("w", encoding="utf-8") as fh:
        sys.stdout = _Stamped(old_out, fh, lock)  # type: ignore[assignment]
        sys.stderr = _Stamped(old_err, fh, lock)  # type: ignore[assignment]
        run["code"] = "interrupted"
        try:
            print(f"start: strata-complete {__version__} {' '.join(argv)} | log {run['path']}")
            yield run
        finally:
            seconds = time.perf_counter() - started
            print(f"end: {command}, exit {run['code']}, {seconds:.1f} s")
            sys.stdout.flush()
            sys.stdout, sys.stderr = old_out, old_err
