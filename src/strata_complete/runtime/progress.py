"""Progress bars for every long run (folders of tiles, tar streams).

One place for the settings, so all commands look the same. Bars go to stderr
and turn themselves off when stderr is not a terminal (``disable=None``), so
redirected logs stay clean. Lines printed while a bar is running go through
``say`` so they do not break the bar. Every line starts with its UTC time
(``runlog``), so bars are made that much narrower than the terminal.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, Any

from tqdm import tqdm

from strata_complete.runtime.runlog import STAMP_WIDTH

if TYPE_CHECKING:
    from collections.abc import Iterable


def bar(iterable: Iterable[Any] | None = None, **kwargs: Any) -> tqdm:  # noqa: ANN401
    """A tqdm bar with the project settings."""
    kwargs.setdefault("ncols", max(40, shutil.get_terminal_size().columns - STAMP_WIDTH - 1))
    kwargs.setdefault("disable", None)
    kwargs.setdefault("mininterval", 0.5)
    return tqdm(iterable, **kwargs)


def bytes_bar(total: int | None, desc: str, initial: int = 0) -> tqdm:
    """A byte-counting bar (MB, GB, speed, time left)."""
    return bar(
        total=total,
        initial=initial,
        desc=desc,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
    )


def say(line: str) -> None:
    """Print a line without breaking a running bar."""
    tqdm.write(line)
