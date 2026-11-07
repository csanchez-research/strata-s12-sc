"""Quality check of the added bands (``bands`` command).

For every complete tile and every band: DN and reflectance percentiles (reflectance =
(DN + BOA_ADD_OFFSET) / 10000), share of reflectance below 0 or above 1, non-finite
pixels, constant bands, and the share of pixels equal to their right and lower
neighbour (nearest neighbour from a native pixel of ``r`` metres gives about
``(k - 1) / k`` with ``k = r / 10``). Report: ``log/bands_<UTC>.json``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import rasterio

from strata_complete import config
from strata_complete.complete import output_sidecars
from strata_complete.runtime import logs
from strata_complete.runtime.progress import bar
from strata_complete.schema import ScSidecar

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class BandSummary:
    band: str
    native_resolution_m: int | None
    n_tiles: int = 0
    dn_min: float = float("inf")
    dn_max: float = float("-inf")
    reflectance_p1_median: float | None = None
    reflectance_p50_median: float | None = None
    reflectance_p99_median: float | None = None
    share_below_0: float = 0.0
    share_above_1: float = 0.0
    n_nonfinite: int = 0
    n_constant_tiles: int = 0
    equal_neighbour_expected: float | None = None
    equal_neighbour_right: float | None = None
    equal_neighbour_down: float | None = None


@dataclass
class _Acc:
    """Per-band accumulators over tiles."""

    p1: list[float] = field(default_factory=list)
    p50: list[float] = field(default_factory=list)
    p99: list[float] = field(default_factory=list)
    eq_right: list[float] = field(default_factory=list)
    eq_down: list[float] = field(default_factory=list)
    n_pixels: int = 0
    n_below: int = 0
    n_above: int = 0


def _expected_equal(native_m: int | None) -> float | None:
    if not native_m:
        return None
    k = native_m / 10
    return round((k - 1) / k, 3)


def _measure(  # noqa: PLR0913 - one band of one tile and where its numbers go
    name: str, arr: np.ndarray, offset: float, s: BandSummary, a: _Acc, flags: list[str]
) -> None:
    """Add one band of one tile to the band's summary and accumulators."""
    finite = np.isfinite(arr)
    s.n_tiles += 1
    n_bad = int((~finite).sum())
    s.n_nonfinite += n_bad
    if n_bad:
        flags.append(f"{name}: {n_bad} non-finite (masked by the provider, kept as nodata)")
    vals = arr[finite]
    if vals.size == 0:
        flags.append(f"{name}: no finite pixel")
        return
    if vals.min() == vals.max():
        s.n_constant_tiles += 1
        flags.append(f"{name}: constant ({vals.min():g})")
    s.dn_min, s.dn_max = min(s.dn_min, vals.min()), max(s.dn_max, vals.max())
    refl = (vals + offset) / config.S2_QUANTIFICATION
    p1, p50, p99 = np.percentile(refl, [1, 50, 99])
    a.p1.append(p1)
    a.p50.append(p50)
    a.p99.append(p99)
    a.n_pixels += refl.size
    a.n_below += int((refl < 0).sum())
    a.n_above += int((refl > 1).sum())
    a.eq_right.append(float((arr[:, 1:] == arr[:, :-1]).mean()))
    a.eq_down.append(float((arr[1:, :] == arr[:-1, :]).mean()))


def _finish(s: BandSummary, a: _Acc) -> BandSummary:
    if a.n_pixels:
        s.reflectance_p1_median = round(float(np.median(a.p1)), 4)
        s.reflectance_p50_median = round(float(np.median(a.p50)), 4)
        s.reflectance_p99_median = round(float(np.median(a.p99)), 4)
        s.share_below_0 = round(a.n_below / a.n_pixels, 6)
        s.share_above_1 = round(a.n_above / a.n_pixels, 6)
        s.equal_neighbour_right = round(float(np.mean(a.eq_right)), 3)
        s.equal_neighbour_down = round(float(np.mean(a.eq_down)), 3)
    return s


def check_folder(folder: Path) -> tuple[list[BandSummary], dict[str, object], Path]:
    """Value ranges, reflectance percentiles and equal neighbours of every band, over the
    COMPLETE tiles of ``folder``; returns the summaries, the counts and the report path."""
    sidecars = output_sidecars(folder)
    summaries: dict[str, BandSummary] = {}
    acc: dict[str, _Acc] = defaultdict(_Acc)
    skipped, flags = 0, []
    for path in bar(sidecars, desc="bands", unit="tile"):
        sc = ScSidecar.model_validate_json(path.read_text(encoding="utf-8"))
        if sc.verdict != "COMPLETE":
            skipped += 1
            continue
        with rasterio.open(path.with_suffix(".tif")) as src:
            stack = src.read().astype(np.float64)
        for rec in sc.bands:
            s = summaries.setdefault(
                rec.name,
                BandSummary(
                    rec.name,
                    rec.native_resolution_m,
                    equal_neighbour_expected=_expected_equal(rec.native_resolution_m),
                ),
            )
            offset = sc.radiometry.boa_add_offset.get(rec.name, 0.0)
            _measure(
                f"{path.name}: {rec.name}", stack[rec.index - 1], offset, s, acc[rec.name], flags
            )
    order = {b: i for i, b in enumerate(config.S2_ALL_BANDS)}
    out = [
        _finish(summaries[b], acc[b])
        for b in sorted(summaries, key=lambda b: order.get(b, len(order)))
    ]
    meta: dict[str, object] = {
        "folder": str(folder),
        "n_tiles_checked": len(sidecars) - skipped,
        "n_tiles_skipped_not_verified": skipped,
        "flags": flags,
    }
    report = logs.write("bands", {**meta, "bands": [asdict(s) for s in out]})
    return out, meta, report
