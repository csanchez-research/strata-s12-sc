"""From the D_lite record to the STRATA-S12-SC release (``pipeline`` command).

Steps: ``sample``, ``complete``, ``bands``, ``prov``, ``package``; each resumes on its
own, so after any stop the same command carries on. ``sample``, ``complete``, ``prov``
and ``package`` stop the chain when they fail. A step that ran to the end with some
tiles in error returns ``SOME_FAILED`` and the chain goes on: ``package`` lists those
pairs with the reason. With ``--input`` the ``sample`` step is skipped; with
``--provider pc`` the release is made with Planetary Computer
(``strata-s12-sc_<class>_pc.tar``).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from strata_complete.runtime import logs, runlog
from strata_complete.runtime.progress import say

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

ORDER = ("sample", "complete", "bands", "prov", "package")
BLOCKING = frozenset({"sample", "complete", "prov", "package"})
SOME_FAILED = 3  # exit code of a step that ran to the end with some tiles in error


def run(
    steps: Mapping[str, Callable[[], int]],
    settings: Mapping[str, object],
    skip: frozenset[str] = frozenset(),
    until: str | None = None,
) -> int:
    """Run ``steps`` (name -> function returning an exit code) in ``ORDER``, up to
    ``until`` included if given."""
    order = ORDER[: ORDER.index(until) + 1] if until else ORDER
    names = [n for n in order if n not in skip]
    record: list[dict[str, object]] = []
    code = 0
    for i, name in enumerate(names, start=1):
        say(f"\n=== pipeline {i}/{len(names)}: {name} ===")
        t0, started = time.perf_counter(), runlog.now()
        step_code = steps[name]()
        record.append(
            {
                "step": name,
                "code": step_code,
                "started": started,
                "ended": runlog.now(),
                "seconds": round(time.perf_counter() - t0, 1),
            }
        )
        if step_code and name in BLOCKING and step_code != SOME_FAILED:
            say(
                f"[STOPPED] {name} ended with code {step_code}. Fix the cause and run the same "
                "command again: finished work is kept and skipped."
            )
            code = step_code
            break
        if step_code:
            say(f"[WARN] {name} reported problems (code {step_code}); details in log/. Going on.")
    report = logs.write("pipeline", {"settings": dict(settings), "steps": record, "code": code})
    if not code:
        say(f"\n=== pipeline done === record: {report}")
    return code
