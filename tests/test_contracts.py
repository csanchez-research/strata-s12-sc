"""Schema, pipeline and command-line contracts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from strata_complete import clean, cli, config, pipeline, schema
from strata_complete.dlite import sample
from strata_complete.dlite.record import Record, RecordError
from strata_complete.runtime import runlog
from tests.conftest import sidecar

ROOT = Path(__file__).resolve().parents[1]


def test_unknown_sidecar_field_is_rejected() -> None:
    meta = sidecar("S2", "p", "S2_x", "0" * 16)
    schema.DliteSidecar.model_validate(meta)
    meta["integrity"]["something_new"] = 1
    with pytest.raises(ValidationError):
        schema.DliteSidecar.model_validate(meta)


def test_fields_of_the_other_sensor_are_rejected() -> None:
    meta = sidecar("S2", "p", "S2_x", "0" * 16)
    meta["acquisition"]["orbit_direction"] = "ASCENDING"
    with pytest.raises(ValidationError, match="other sensor"):
        schema.DliteSidecar.model_validate(meta)
    s1 = sidecar("S1", "p", "S1_x", "0" * 16)
    del s1["quality_metrics"]["sar_cross"]
    with pytest.raises(ValidationError, match="other sensor"):
        schema.DliteSidecar.model_validate(s1)


def test_a_sidecar_of_an_earlier_version_is_rejected() -> None:
    meta = sidecar("S2", "p", "S2_x", "0" * 16)
    meta["stac_compliant"] = True
    meta["provenance"]["script"] = "x"
    with pytest.raises(ValidationError):
        schema.DliteSidecar.model_validate(meta)


def test_record_is_pinned(ws) -> None:
    ws.add_pair("ok")
    record = ws.record()
    assert record.sums()
    config.RECORD_SHA256SUMS_SHA256 = "0" * 64
    with pytest.raises(RecordError, match="not the SHA256SUMS.txt"):
        Record(ws.record_dir).sums()


def test_sample_writes_the_selection_and_copies_no_tile(ws) -> None:
    for key in ("a", "b"):
        ws.add_pair(key)
    record = ws.record()
    out_dir, sel = sample.build(record, all_pairs=True)
    assert out_dir.name == "all"
    assert sel["n_pairs"] == 2
    assert sel["missing_in_record"] == []
    assert sorted(p.name for p in out_dir.iterdir()) == ["lite_manifest.csv", "selection.json"]
    _, drawn = sample.build(record, per_stratum=1, seed=22)
    assert drawn["n_pairs"] == 1


@pytest.mark.parametrize("key", sorted(schema.EXPORTED))
def test_exported_schemas_are_up_to_date(key: str) -> None:
    """schema/*.schema.json must be what schema-export writes for this code."""
    on_disk = json.loads((ROOT / "schema" / f"{key}.schema.json").read_text(encoding="utf-8"))
    assert on_disk == schema.json_schema(key)


def _steps(codes: dict[str, int]) -> tuple[dict, list[str]]:
    ran: list[str] = []

    def step(name: str):
        return lambda: (ran.append(name), codes.get(name, 0))[1]

    return {n: step(n) for n in pipeline.ORDER}, ran


def test_pipeline_goes_on_when_some_tiles_failed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("strata_complete.config.LOG_DIR", tmp_path)
    steps, ran = _steps({"complete": pipeline.SOME_FAILED, "crosscheck": 1})
    assert pipeline.run(steps, {}) == 0
    assert ran == list(pipeline.ORDER)


def test_pipeline_stops_at_a_failing_blocking_step(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("strata_complete.config.LOG_DIR", tmp_path)
    steps, ran = _steps({"complete": 1})
    assert pipeline.run(steps, {}) == 1
    assert ran[-1] == "complete"


@pytest.mark.parametrize(
    "command",
    [
        "fetch",
        "verify-tar",
        "check",
        "compare",
        "sample",
        "complete",
        "bands",
        "crosscheck",
        "prov",
        "report",
        "serve",
        "package",
        "pipeline",
        "env-check",
        "clean",
        "schema-export",
    ],
)
def test_every_command_has_help(command: str, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "--help"])
    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_clean_removes_only_what_can_be_made_again(tmp_path, monkeypatch) -> None:
    for name, sub in (("DATA_DIR", "data"), ("IN_DIR", "in"), ("OUT_DIR", "out")):
        monkeypatch.setattr(f"strata_complete.config.{name}", tmp_path / sub)
    monkeypatch.setattr("strata_complete.config.PIECES_DIR", tmp_path / "data" / "pieces")
    files = {
        "data/pc/s2/a.tif": True,
        "data/gee/s2/b.tif": True,
        "data/pieces/t_1/x.tif": True,
        "out/strata-s12-sc/s2/c.tif.part": True,
        "data/zenodo/23837956/lite_manifest.csv": False,
        "in/sample/s2/d.tif": False,
        "out/strata-s12-sc/s2/e.tif": False,
    }
    for rel in files:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"x")
    clean.remove([p for p, _ in clean.targets()])
    for rel, removed in files.items():
        assert (tmp_path / rel).exists() is not removed, rel


def test_every_printed_line_has_its_time_on_screen_and_in_the_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("strata_complete.config.LOG_DIR", tmp_path / "log")
    with runlog.session("demo", ["demo"]) as run:
        print("one line")
        sys.stderr.write("bar 1%\rbar 50%\rbar 100%\n")  # a progress bar that redraws
        run["code"] = 0
    stamp = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ")
    lines = Path(str(run["path"])).read_text(encoding="utf-8").splitlines()
    assert all(stamp.match(line) for line in lines)
    text = [stamp.sub("", line) for line in lines]
    assert text[0].startswith("start: strata-complete")
    assert text[1:3] == ["one line", "bar 100%"]  # the bar: only its final state
    assert text[3].startswith("end: demo, exit 0, ")
    shown = capsys.readouterr()
    assert all(stamp.match(x) for x in shown.out.splitlines())
    assert all(stamp.match(x) for x in shown.err.split("\r") if x.strip())
