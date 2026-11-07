"""A small record of ten real pairs to show the whole tool (``demo-pack`` command).

Built from the D_lite record (local copy or Zenodo, every byte checked) into
``in/demo-pack/``, laid out like the record itself: one tar per class with the
D_lite member paths, ``member_index.csv``, ``lite_manifest.csv``, ``README.md``,
``pack.json`` and ``SHA256SUMS.txt``. The tool reads it as it reads D_lite, with
``SC_RECORD_DIR`` and ``SC_RECORD_SHA256SUMS_SHA256`` (written to ``demo.env``).

The ten pairs (``PAIRS``) cover every outcome:

* five are copied as published: Earth Engine completes them;
* two name, in their Sentinel-2 sidecar, a scene that Earth Engine does not hold (the
  scene identifier with ``_REMOVED`` appended), as when a scene leaves an archive. Earth
  Engine cannot give them; Planetary Computer finds the same ESA product by its name
  (``acquisition.product_id``, unchanged) and completes them;
* three are changed on purpose, each in one way: the Sentinel-2 pixels (they no
  longer match the checksum of their sidecar), the Sentinel-2 grid (one pixel to
  the east) and the Sentinel-1 pixels. The tool must refuse to release them.

This is a demonstration record, not a version of D_lite: ``pack.json`` and
``README.md`` say which pairs were changed and how.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
from rasterio.io import MemoryFile
from rasterio.transform import Affine

from strata_complete import config
from strata_complete.dlite import sample
from strata_complete.runtime import archive

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from strata_complete.dlite.record import Record

FOLDER_NAME = "demo-pack"
ENV_NAME = "demo.env"
REPORT_NAME = "pack.json"

# Chosen among pairs of D_lite 2.0.0 that Planetary Computer rebuilt bit for bit in the
# 10 % demonstration (same UTM zone, not Sentinel-2C), with label purity above 0.8 and
# no quality indicator set; one per class, season and region shown.
# (pair_id, role, change): role "complete", "second provider" or "refused"; change None,
# "s2_scene", "s2_pixels", "s2_shift" or "s1_pixels".
PAIRS: tuple[tuple[str, str, str | None], ...] = (
    ("72e3c0ec3d1ac9f5", "complete", None),  # tree cover, Europe, summer
    ("408e2e264bb68070", "complete", None),  # cropland, Asia, spring
    ("51acd7bed215963a", "complete", None),  # water, North America, autumn
    ("2e531540d85ba75d", "complete", None),  # built-up, Asia, autumn
    ("9838288d8c58effb", "complete", None),  # grassland, South America, summer
    ("25418e0fbad634e0", "second provider", "s2_scene"),  # shrubland, Oceania, autumn
    ("b2dd7a19eca7c8a8", "second provider", "s2_scene"),  # wetland, North America, summer
    ("24eba4fb9bf0541e", "refused", "s2_pixels"),  # bare, Africa, spring
    ("406b5b4a5557f593", "refused", "s2_shift"),  # tree cover, South America, winter
    ("1a31c2c43c6219ae", "refused", "s1_pixels"),  # cropland, Europe, autumn
)

WHAT = {
    None: "copied as published",
    "s2_scene": "Sentinel-2 sidecar: scene identifier with _REMOVED appended (a scene Earth "
    "Engine does not hold); pixels and ESA product name unchanged",
    "s2_pixels": "Sentinel-2: 32 x 32 pixels of B4 raised by 100 DN; sidecar unchanged",
    "s2_shift": "Sentinel-2: georeference moved one pixel (10 m) east; pixels unchanged",
    "s1_pixels": "Sentinel-1: 32 x 32 pixels of VV raised by 3 dB; sidecar unchanged",
}
EXPECTED = {
    None: "COMPLETE with Earth Engine; released",
    "s2_scene": "UNAVAILABLE with Earth Engine; COMPLETE with Planetary Computer",
    "s2_pixels": "not released: pixel checksum of the D_lite tile, and MISALIGNED",
    "s2_shift": "not released: MISALIGNED (the published bands do not match on that grid)",
    "s1_pixels": "not released: the Sentinel-1 tile fails its pixel checksum, not copied",
}


def _what(change: str | None) -> str:
    return WHAT[change]


def _expected(change: str | None) -> str:
    return EXPECTED[change]


def _rewrite_sidecar(data: bytes) -> bytes:
    """The sidecar with a scene identifier Earth Engine does not hold."""
    meta = json.loads(data)
    meta["provenance"]["sentinel2_image_id"] += "_REMOVED"
    return (json.dumps(meta, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _rewrite(data: bytes, change: str) -> bytes:
    """The GeoTIFF with one change, same encoding, band names and tags."""
    with MemoryFile(data) as mem, mem.open() as src:
        arr, profile = src.read(), src.profile
        tags, names = src.tags(), src.descriptions
    cy, cx = arr.shape[1] // 2, arr.shape[2] // 2
    block = (0, slice(cy - 16, cy + 16), slice(cx - 16, cx + 16))  # 32 x 32, first band
    if change == "s2_pixels":
        arr[block] = arr[block] + 100.0
    elif change == "s1_pixels":
        arr[block] = arr[block] + 3.0
    elif change == "s2_shift":
        profile["transform"] = profile["transform"] @ Affine.translation(1, 0)
    else:
        msg = f"unknown change {change!r}"
        raise ValueError(msg)
    with MemoryFile() as out:
        with out.open(**profile) as dst:
            dst.write(arr.astype(np.float32))
            dst.descriptions = names
            dst.update_tags(**tags)
        return out.read()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy_pairs(
    record: Record, by_id: dict[str, dict[str, str]], pairs: Sequence, staging: Path
) -> tuple[dict[str, list[tuple[str, Path]]], list[dict]]:
    """Every file of the pairs, read from the record and changed where asked, in ``staging``.

    Returns the tar members per class and the pairs report.
    """
    members: dict[str, list[tuple[str, Path]]] = {}
    report = []
    for pair_id, role, change in pairs:
        row = by_id[pair_id]
        files = []
        for key in ("s2_file", "s1_file"):
            changed = (change in ("s2_pixels", "s2_shift", "s2_scene") and key == "s2_file") or (
                change == "s1_pixels" and key == "s1_file"
            )
            for rel in (row[key], sample.sidecar_of(row[key])):
                data = record.read(rel)  # checked against the record
                source_sha = _sha(data)
                if changed and change == "s2_scene" and rel.endswith(".json"):
                    data = _rewrite_sidecar(data)
                elif changed and change != "s2_scene" and rel.endswith(".tif"):
                    data = _rewrite(data, change)
                path = staging / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                members.setdefault(row["class_name"], []).append((rel, path))
                files.append({"path": rel, "dlite_sha256": source_sha, "pack_sha256": _sha(data)})
        report.append(
            {k: row[k] for k in ("pair_id", "tile_id", "class_name", "season", "region")}
            | {
                "role": role,
                "change": _what(change),
                "expected": _expected(change),
                "files": files,
            }  # fmt: skip
        )
    return members, report


def _write_index(out: Path, members: dict[str, list[tuple[str, Path]]]) -> None:
    """One tar per class with the D_lite member paths, and its ``member_index.csv``."""
    rows = []
    for cls in sorted(members):
        name = config.dlite_tar_name(cls)
        _, _, index = archive.write_tar(out / name, members[cls])
        rows += [
            {"archive": name, "path": m.path, "offset_data": m.offset_data, "size": m.size,
             "sha256": m.sha256}
            for m in index
        ]  # fmt: skip
    with (out / config.MEMBER_INDEX_NAME).open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["archive", "path", "offset_data", "size", "sha256"],
                           lineterminator="\n")  # fmt: skip
        w.writeheader()
        w.writerows(rows)


def build(
    record: Record,
    out: Path | None = None,
    pairs: Sequence[tuple[str, str, str | None]] = PAIRS,
) -> dict[str, object]:
    """Write the pack and ``demo.env``; returns what ``pack.json`` says."""
    out = out or config.IN_DIR / FOLDER_NAME
    rows = sample.read_manifest(record.file(sample.MANIFEST))
    by_id = {r["pair_id"]: r for r in rows}
    missing = [p for p, _, _ in pairs if p not in by_id]
    if missing:
        msg = f"pairs not in the manifest of the record: {', '.join(missing)}"
        raise ValueError(msg)
    foreign = foreign_outputs()
    if foreign:
        msg = (
            "this folder already holds outputs of the real record "
            f"({', '.join(foreign[:3])}): build the demonstration in a fresh clone"
        )
        raise ValueError(msg)
    if out.exists():
        shutil.rmtree(out)
    staging = out / ".staging"
    members, report_pairs = _copy_pairs(record, by_id, pairs, staging)
    _write_index(out, members)
    shutil.rmtree(staging)
    chosen = sorted((by_id[p] for p, _, _ in pairs), key=lambda r: r["s2_file"])
    with (out / sample.MANIFEST).open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(chosen)
    report: dict[str, object] = {
        "what": "demonstration record: ten pairs of D_lite, five of them changed on purpose; "
        "not a version of D_lite",
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": {
            "dlite_doi": config.DLITE_VERSION_DOI,
            "dlite_version": config.DLITE_VERSION,
            "record": record.describe(),
            "sha256sums_sha256": config.RECORD_SHA256SUMS_SHA256,
        },
        "pairs": report_pairs,
    }
    (out / REPORT_NAME).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "README.md").write_text(_readme(report), encoding="utf-8")
    names = sorted(p.name for p in out.iterdir() if p.is_file())
    sums = "".join(f"{archive.sha256(out / n)}  {n}\n" for n in names)
    (out / config.SHA256SUMS_NAME).write_text(sums, encoding="utf-8")
    pin = _sha(sums.encode())
    (out / ENV_NAME).write_text(
        f"SC_RECORD_DIR={out}\nSC_RECORD_SHA256SUMS_SHA256={pin}\n",
        encoding="utf-8",
    )
    report["sha256sums_sha256"] = pin
    return report


def _readme(report: dict) -> str:
    lines = [
        "# Demonstration record of strata-complete",
        "",
        "Ten pairs of STRATA-S12 (D_lite) "
        f"{report['source']['dlite_version']} (DOI {report['source']['dlite_doi']}), laid out "
        "like the record, to run every step of the tool in a few minutes. **Five pairs are "
        "changed on purpose** to show how the tool behaves; this is not a version of D_lite "
        "and nothing built from it is a release of STRATA-S12-SC.",
        "",
        "| Pair | Class | Region | Season | What was done | Expected |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| `{p['pair_id']}` | {p['class_name']} | {p['region']} | {p['season']} | "
        f"{p['change']} | {p['expected']} |"
        for p in report["pairs"]
    ]
    lines += [
        "",
        "`demo.env` points the tool at this folder (`SC_RECORD_DIR`, "
        "`SC_RECORD_SHA256SUMS_SHA256`); nothing else changes in the tool. "
        "`pack.json` gives, for every file, its sha256 in D_lite and here.",
        "",
    ]
    return "\n".join(lines)


def foreign_outputs(pin: str | None = None) -> list[str]:
    """Outputs here that were not made from the demonstration record (``pin``: its sums).

    With ``pin`` None: any release or input at all, or more output tiles than the pack
    has. The demonstration writes to out/, in/ and log/, so it runs only where nothing
    else lives (a fresh clone of the repository), never next to a real release.
    """
    found = []
    for f in [
        *config.OUT_DIR.glob("release_*/release.json"),
        *config.IN_DIR.glob("*/selection.json"),
    ]:
        src = json.loads(f.read_text(encoding="utf-8")).get("source", {})
        if "sha256sums_sha256" in src and src["sha256sums_sha256"] != pin:
            found.append(str(f))
    tiles = [t for t in config.SC_OUT_DIR.rglob("S2_*.json") if not t.name.endswith(".prov.json")]
    if len(tiles) > len(PAIRS):
        found.append(f"{config.SC_OUT_DIR} ({len(tiles)} tiles)")
    return found
