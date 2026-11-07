"""The demonstration record: built like D_lite, read like D_lite, every outcome shown."""

from __future__ import annotations

import csv
import json

import pytest

from strata_complete import cli, config
from strata_complete.dlite import demopack
from strata_complete.dlite.record import Record

PAIRS = (
    ("pair-ok", "complete", None),
    ("pair-gone", "second provider", "s2_scene"),
    ("pair-none", "second provider", "s2_scene"),
    ("pair-px", "refused", "s2_pixels"),
    ("pair-grid", "refused", "s2_shift"),
    ("pair-s1", "refused", "s1_pixels"),
)


def _pack(ws, monkeypatch):
    for key in ("ok", "gone", "none", "px", "grid", "s1"):
        # the fake Earth Engine does not read scene names: "none" stands for a scene it lacks
        ws.add_pair(key, gee="none" if key in ("gone", "none") else "same")
    report = demopack.build(ws.record(), ws.root / "in" / "demo-pack", PAIRS)
    env = dict(
        line.split("=", 1)
        for line in (ws.root / "in" / "demo-pack" / "demo.env").read_text().splitlines()
    )
    monkeypatch.setattr(config, "RECORD_DIR", env["SC_RECORD_DIR"])
    monkeypatch.setattr(config, "RECORD_SHA256SUMS_SHA256", env["SC_RECORD_SHA256SUMS_SHA256"])
    return report, env


def test_pack_is_a_record_and_says_what_was_changed(ws, monkeypatch) -> None:
    report, env = _pack(ws, monkeypatch)
    pack = Record(ws.root / "in" / "demo-pack")
    assert len(pack.index()) == 4 * len(PAIRS)  # every file checked by its sha256
    changed = {f["path"] for p in report["pairs"] for f in p["files"]
               if f["dlite_sha256"] != f["pack_sha256"]}  # fmt: skip
    assert {p.split("/")[-1] for p in changed} == {
        "S2_px.tif",
        "S2_grid.tif",
        "S1_s1.tif",
        "S2_gone.json",
        "S2_none.json",
    }
    side = json.loads(pack.read(next(p for p in changed if p.endswith("S2_gone.json"))))
    assert side["provenance"]["sentinel2_image_id"].endswith("_REMOVED")
    assert set(env) == {"SC_RECORD_DIR", "SC_RECORD_SHA256SUMS_SHA256"}


def test_every_outcome_of_the_demonstration(ws, monkeypatch) -> None:
    _pack(ws, monkeypatch)
    assert cli.main(["pipeline", "--all", "--workers", "1"]) == 0
    with (config.OUT_DIR / "release_all" / "sc_excluded.csv").open(encoding="utf-8") as fh:
        why = {r["pair_id"]: r["reason"] for r in csv.DictReader(fh)}
    assert set(why) == {"pair-gone", "pair-none", "pair-px", "pair-grid", "pair-s1"}
    assert why["pair-gone"] == why["pair-none"] == "S2 UNAVAILABLE"
    assert "pixel checksum does not match its sidecar" in why["pair-px"]
    assert why["pair-grid"].startswith("S2 ")  # the fake provider ignores the grid
    assert why["pair-s1"].startswith("S1 not copied")
    pc_input = str(config.IN_DIR / "all_pc")
    assert cli.main(["pipeline", "--input", pc_input, "--provider", "pc", "--workers", "1"]) == 0
    pc_release = json.loads((config.OUT_DIR / "release_all_pc" / "release.json").read_text())
    assert pc_release["n_pairs_released"] >= 2  # gone and none (and grid, with the fake)


def test_pack_is_never_built_next_to_a_real_release(ws) -> None:
    ws.add_pair("ok")
    record = ws.record()
    real = config.OUT_DIR / "release_all"
    real.mkdir(parents=True)
    (real / "release.json").write_text(json.dumps({"source": {"sha256sums_sha256": "real"}}))
    with pytest.raises(ValueError, match="fresh clone"):
        demopack.build(record, ws.root / "in" / "demo-pack", PAIRS[:1])
    assert not (ws.root / "in" / "demo-pack").exists()
