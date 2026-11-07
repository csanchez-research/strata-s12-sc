"""Notes on a release, as part of its data (W3C Web Annotation, JSON-LD).

One file next to the archives, ``sc_notes.jsonld``: an ``AnnotationCollection`` (W3C
Recommendation, 23 February 2017). Each annotation targets the member path of a
Sentinel-2 tile: ``commenting`` for a note, ``assessing`` for a flag. A change is a new
annotation, so the file keeps the history; the creator is ``SC_NOTES_CREATOR`` when
set. ``seal`` checks the file, writes ``sc_notes.csv`` and records both in
``release.json`` and ``SHA256SUMS.txt``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from strata_complete import config

if TYPE_CHECKING:
    from pathlib import Path

NOTES = "sc_notes.jsonld"
NOTES_CSV = "sc_notes.csv"
CONTEXT = "http://www.w3.org/ns/anno.jsonld"
FLAGGED, NOT_FLAGGED = "flagged", "not flagged"
MAX_NOTE = 2000
CSV_COLUMNS = ["created", "pair", "tile", "motivation", "value", "creator", "id"]
_LOCK = threading.Lock()


class NotesError(ValueError):
    """The notes file of a release is not valid."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _empty(release: Path) -> dict:
    return {
        "@context": CONTEXT,
        "id": f"urn:strata-s12-sc:notes:{release.resolve().name}",
        "type": "AnnotationCollection",
        "label": f"Notes on the pairs of {config.OUT_DATASET} ({release.resolve().name})",
        "total": 0,
        "first": {"type": "AnnotationPage", "items": []},
    }


def load(release: Path) -> dict:
    path = release / NOTES
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else _empty(release)


def items(release: Path) -> list[dict]:
    return list(load(release).get("first", {}).get("items", []))


def _pair_of(tile: str) -> str:
    """The pair id is the last part of the tile's name (``..._<pair_id>.tif``)."""
    return tile.rsplit("/", 1)[-1].removesuffix(".tif").rsplit("_", 1)[-1]


def current(release: Path) -> dict[str, dict]:
    """Latest flag and note of every tile that has one, by tile name (as the report keys)."""
    state: dict[str, dict] = {}
    for a in items(release):
        tile = a["target"]["source"]
        s = state.setdefault(tile.rsplit("/", 1)[-1], {"flag": False, "note": "", "at": None})
        value = a["body"]["value"]
        if a["motivation"] == "assessing":
            s["flag"] = value == FLAGGED
        elif a["motivation"] == "commenting":
            s["note"] = value
        s["at"] = a["created"]
    return {k: v for k, v in state.items() if v["flag"] or v["note"]}


def _annotation(n: int, tile: str, motivation: str, value: str, created: str) -> dict:
    a: dict[str, object] = {
        "id": f"urn:strata-s12-sc:note:{_pair_of(tile)}:{n}",
        "type": "Annotation",
        "motivation": motivation,
        "created": created,
        "body": {"type": "TextualBody", "value": value, "format": "text/plain"},
        "target": {"source": tile},
    }
    name = os.environ.get("SC_NOTES_CREATOR", "").strip()
    if name:
        a["creator"] = {"type": "Person", "name": name}
    return a


def add(release: Path, tile: str, *, flag: bool, note: str) -> tuple[dict[str, object], bool]:
    """Record a flag and a note for ``tile`` (member path of its S2 tif); only what changed.

    Returns the tile's state ``{flag, note, at}``, as the report shows it, and whether
    anything was written (saving the same flag and note again adds no annotation).
    """
    note = note.strip()[:MAX_NOTE]
    name = tile.rsplit("/", 1)[-1]
    with _LOCK:
        doc = load(release)
        page = doc.setdefault("first", {"type": "AnnotationPage", "items": []})
        before = current(release).get(name, {"flag": False, "note": "", "at": None})
        created = _now()
        new = []
        if flag != before["flag"]:
            new.append(("assessing", FLAGGED if flag else NOT_FLAGGED))
        if note != before["note"]:
            new.append(("commenting", note))
        for motivation, value in new:
            page["items"].append(
                _annotation(len(page["items"]) + 1, tile, motivation, value, created)
            )
        doc["total"] = len(page["items"])
        if new:
            tmp = release / (NOTES + ".part")
            tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(release / NOTES)
    if not new:
        return {"flag": before["flag"], "note": before["note"], "at": before["at"]}, False
    return {"flag": flag, "note": note, "at": created}, True


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(release: Path) -> list[str]:
    """Problems of the notes file; empty if it is valid."""
    doc = load(release)
    problems = []
    if doc.get("@context") != CONTEXT or doc.get("type") != "AnnotationCollection":
        problems.append(f"{NOTES}: not a Web Annotation AnnotationCollection")
    found = doc.get("first", {}).get("items", [])
    if doc.get("total") != len(found):
        problems.append(f"{NOTES}: total {doc.get('total')} but {len(found)} annotations")
    index = release / "sc_member_index.csv"
    with index.open(encoding="utf-8", newline="") as fh:
        members = {r["path"] for r in csv.DictReader(fh)}
    ids: set[str] = set()
    for i, a in enumerate(found, 1):
        where = f"{NOTES} annotation {i}"
        if a.get("type") != "Annotation" or a.get("motivation") not in ("commenting", "assessing"):
            problems.append(f"{where}: type or motivation not allowed")
        if a.get("id") in ids:
            problems.append(f"{where}: id {a.get('id')} repeated")
        ids.add(a.get("id"))
        source = (a.get("target") or {}).get("source")
        if source not in members:
            problems.append(f"{where}: target {source} is not a file of the release")
        body = a.get("body") or {}
        if body.get("type") != "TextualBody" or not isinstance(body.get("value"), str):
            problems.append(f"{where}: body is not a TextualBody")
        if a.get("motivation") == "assessing" and body.get("value") not in (FLAGGED, NOT_FLAGGED):
            problems.append(f"{where}: a flag must be '{FLAGGED}' or '{NOT_FLAGGED}'")
    return problems


def seal(release: Path) -> dict[str, object]:
    """Check the notes, write the CSV, record both in release.json and SHA256SUMS.txt."""
    if not (release / NOTES).is_file():
        (release / NOTES).write_text(json.dumps(_empty(release), indent=2) + "\n", encoding="utf-8")
    problems = check(release)
    if problems:
        raise NotesError("; ".join(problems[:10]))
    found = items(release)
    with (release / NOTES_CSV).open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        for a in found:
            tile = a["target"]["source"]
            w.writerow(
                {
                    "created": a["created"],
                    "pair": _pair_of(tile),
                    "tile": tile,
                    "motivation": a["motivation"],
                    "value": a["body"]["value"],
                    "creator": (a.get("creator") or {}).get("name", ""),
                    "id": a["id"],
                }
            )
    meta_path = release / "release.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    state = current(release)
    meta["notes"] = {
        "file": NOTES,
        "table": NOTES_CSV,
        "standard": "W3C Web Annotation Data Model (JSON-LD)",
        "n_annotations": len(found),
        "n_pairs_with_notes": sum(1 for s in state.values() if s["note"]),
        "n_pairs_flagged": sum(1 for s in state.values() if s["flag"]),
        "sha256": _sha256(release / NOTES),
        "sealed": _now(),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    sums_path = release / config.SHA256SUMS_NAME
    lines = sums_path.read_text(encoding="utf-8").splitlines() if sums_path.is_file() else []
    renew = {"release.json", NOTES, NOTES_CSV}
    kept = [ln for ln in lines if ln.split(maxsplit=1)[-1].strip() not in renew]
    kept += [f"{_sha256(release / n)}  {n}" for n in sorted(renew)]
    kept.sort(key=lambda ln: ln.split(maxsplit=1)[-1])
    sums_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return meta["notes"]
