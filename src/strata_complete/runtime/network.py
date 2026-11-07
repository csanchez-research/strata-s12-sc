"""Pause and resume on connection problems.

When a request fails, the caller asks ``wait_until_online``: if the network is
reachable it returns at once (the failure was something else and counts as a
normal attempt); if not, the run pauses: it says so, probes the network with
growing waits (``PAUSE_FIRST_WAIT_S`` doubling up to ``PAUSE_MAX_WAIT_S``) and
resumes as soon as a probe gets any HTTP answer. A pause never uses up
download attempts. After ``MAX_PAUSE_S`` without network it gives up with
``NetworkDownError``; finished work is kept and the command can be run again to
resume.

One thread probes at a time; the others wait for its verdict.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

from strata_complete import config
from strata_complete.runtime.progress import say

_LOCK = threading.Lock()


class NetworkDownError(RuntimeError):
    """No network for longer than ``config.MAX_PAUSE_S``."""


def is_online(timeout: float = 10) -> bool:
    """True if any probe URL gives an HTTP answer (any status code)."""
    for url in config.ONLINE_PROBE_URLS:
        req = urllib.request.Request(  # noqa: S310 - https URLs from config
            url, method="HEAD", headers={"User-Agent": config.USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout):  # noqa: S310
                return True
        except urllib.error.HTTPError:
            return True  # the server answered: the network is up
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return False


def wait_until_online(reason: str) -> float:
    """Block while the network is down. Returns the seconds paused (0 if online)."""
    with _LOCK:
        if is_online():
            return 0.0
        started = time.monotonic()
        wait = config.PAUSE_FIRST_WAIT_S
        say(f"[PAUSED] connection lost ({reason[:120]}); waiting for the network")
        while True:
            elapsed = time.monotonic() - started
            if elapsed > config.MAX_PAUSE_S:
                msg = (
                    f"no network for {elapsed:.0f} s (limit {config.MAX_PAUSE_S:.0f} s); "
                    "finished work is kept: run the command again to resume"
                )
                raise NetworkDownError(msg)
            say(f"         still offline after {elapsed:.0f} s; next check in {wait:.0f} s")
            time.sleep(wait)
            if is_online():
                paused = time.monotonic() - started
                say(f"[RESUMED] network back after {paused:.0f} s")
                return paused
            wait = min(wait * 2, config.PAUSE_MAX_WAIT_S)
