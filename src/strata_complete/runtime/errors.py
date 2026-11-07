"""One line that says why something failed, with the chain of causes behind it.

rasterio reports a failed read as "Read failed. See previous exception for details.";
GDAL's messages are in the chain of exceptions. Query strings are removed from URLs,
because they hold the provider's short-lived access token.
"""

from __future__ import annotations

import re

# a query string that carries a signed access token (Azure SAS: st, se, sig...)
_QUERY = re.compile(r"\?[^\s'\"]*\b(?:sig|se|st|sv|skoid)=[^\s'\"]*")
_STRIPPED = "?…"
MAX_LEN = 1000


def describe(exc: BaseException) -> str:
    """One line for an exception and its causes, with signed URL queries removed (they
    carry access tokens) and cut to ``MAX_LEN`` characters."""
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = f"{type(cur).__name__}: {_QUERY.sub(_STRIPPED, str(cur))}"
        if text not in parts:
            parts.append(text)
        cur = cur.__cause__ or cur.__context__
    line = " <- ".join(parts)
    return line if len(line) <= MAX_LEN else line[: MAX_LEN - 1] + "…"
