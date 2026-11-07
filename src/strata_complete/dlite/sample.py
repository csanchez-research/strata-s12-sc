"""Which pairs of D_lite to process (``sample`` command): a stratified draw, or all.

Selection: ``lite_manifest.csv`` of the record lists every pair with its stratum
axes (``class_name``, ``season``, ``region``). Within each stratum the rows are
sorted by ``pair_id`` and ``per_stratum`` pairs are drawn with a seeded
``random.Random`` (all of them if the stratum has fewer). ``all_pairs`` takes
every pair of the manifest instead.

Output: ``in/sample_n<per_stratum>_seed<seed>/`` (or ``in/all/``) with the
manifest rows of the chosen pairs (``lite_manifest.csv``) and ``selection.json``
(design, seed, size, the record read and the sha256 of its manifest, the pairs).
No tile is copied: the next steps read each tile from the record by its byte
range, checked against its sha256 (``record.Record``), so the same selection can
be drawn again by anyone and costs no disk.
"""

from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from strata_complete import config

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from strata_complete.dlite.record import Record

MANIFEST = "lite_manifest.csv"
SELECTION = "selection.json"


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def select(rows: Sequence[dict[str, str]], per_stratum: int, seed: int) -> list[dict[str, str]]:
    """Seeded draw of ``per_stratum`` pairs in every (class, season, region) stratum."""
    strata: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        strata[(r["class_name"], r["season"], r["region"])].append(r)
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling, not security
    chosen: list[dict[str, str]] = []
    for key in sorted(strata):
        members = sorted(strata[key], key=lambda r: r["pair_id"])
        chosen.extend(rng.sample(members, min(per_stratum, len(members))))
    return chosen


def sidecar_of(tif: str) -> str:
    return tif[: -len(".tif")] + ".json"


def folder_name(
    per_stratum: int | None,
    seed: int | None,
    classes: Sequence[str] | None = None,
    *,
    all_pairs: bool,
) -> str:
    only = "_" + "-".join(sorted(classes)) if classes else ""
    if all_pairs:
        return "all" + only
    per = per_stratum or config.SAMPLE_PER_STRATUM
    return f"sample_n{per}_seed{config.SAMPLE_SEED if seed is None else seed}{only}"


def build(
    record: Record,
    per_stratum: int | None = None,
    seed: int | None = None,
    classes: Sequence[str] | None = None,
    *,
    all_pairs: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Write the selection to ``in/<folder_name>/``.

    ``None`` means the value in ``config`` (``SAMPLE_PER_STRATUM``, ``SAMPLE_SEED``).
    """
    per_stratum = per_stratum or config.SAMPLE_PER_STRATUM
    seed = config.SAMPLE_SEED if seed is None else seed
    manifest_path = record.file(MANIFEST)
    rows = read_manifest(manifest_path)
    chosen = (
        sorted(rows, key=lambda r: r["s2_file"]) if all_pairs else select(rows, per_stratum, seed)
    )
    if classes:
        chosen = [r for r in chosen if r["class_name"] in set(classes)]
    index = record.index()
    files = [f for r in chosen for k in ("s2_file", "s1_file") for f in (r[k], sidecar_of(r[k]))]
    missing = [f for f in files if f not in index]
    out_dir = config.IN_DIR / folder_name(per_stratum, seed, classes, all_pairs=all_pairs)
    out_dir.mkdir(parents=True, exist_ok=True)
    selection = {
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": {
            "dlite_doi": config.DLITE_VERSION_DOI,
            "dlite_version": config.DLITE_VERSION,
            "record": record.describe(),
            "sha256sums_sha256": config.RECORD_SHA256SUMS_SHA256,
            "manifest": MANIFEST,
            "manifest_sha256": record.sums()[MANIFEST],
        },
        "design": "every pair of the manifest"
        if all_pairs
        else "seeded random draw within each (class_name, season, region) stratum; "
        "rows sorted by pair_id before drawing",
        "per_stratum": None if all_pairs else per_stratum,
        "seed": None if all_pairs else seed,
        "classes": sorted(classes) if classes else "all",
        "n_strata": len({(r["class_name"], r["season"], r["region"]) for r in chosen}),
        "n_pairs": len(chosen),
        "missing_in_record": sorted(missing),
        "pairs": [
            {k: r[k] for k in ("pair_id", "tile_id", "class_name", "season", "region")}
            for r in chosen
        ],
    }
    (out_dir / SELECTION).write_text(json.dumps(selection, indent=2) + "\n", "utf-8")
    with (out_dir / MANIFEST).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(chosen)
    return out_dir, selection
