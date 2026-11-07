"""complete: nothing is added unless verified; the original is never changed."""

from __future__ import annotations

import json

import numpy as np
import rasterio

from strata_complete import complete, config, provenance
from strata_complete.runtime import logs
from tests.conftest import pixel_hash


def _run(ws):
    results, skipped = complete.complete_folder(ws.in_dir, ws.record())
    return {name.removesuffix(".json"): (sc, err) for name, sc, err in results}, skipped


def _groups(ws, key: str, provider: str = "gee") -> set[str]:
    return {
        g
        for p, k, bands in ws.requests
        if p == provider and k == f"S2_{key}"
        for g, gb in config.S2_REQUEST_GROUPS.items()
        if set(bands) <= set(gb)
    }


def test_verified_tile_gets_every_band(ws) -> None:
    ws.add_pair("ok")
    out, _ = _run(ws)
    sc, err = out["S2_ok"]
    assert err is None
    assert sc.verdict == "COMPLETE"
    assert [b.name for b in sc.bands] == list(config.S2_ALL_BANDS)
    assert sc.not_added == []
    assert sc.source.dlite_checksum_ok is True
    assert _groups(ws, "ok") == set(config.S2_REQUEST_GROUPS)
    with rasterio.open(ws.out_tif("ok")) as o, rasterio.open(ws.in_tif("ok")) as i:
        tile, dlite = o.read(), i.read()
    for j, name in enumerate(("B4", "B3", "B2")):
        assert np.array_equal(tile[config.S2_ALL_BANDS.index(name)], dlite[j])


def test_misaligned_keeps_the_original_and_fetches_only_the_reference(ws) -> None:
    ws.add_pair("mis", gee="shift")
    out, _ = _run(ws)
    sc, _ = out["S2_mis"]
    assert sc.verdict == "MISALIGNED"
    assert sc.complete is False
    assert [b.name for b in sc.bands] == ["B4", "B3", "B2"]
    assert sorted(n.name for n in sc.not_added) == sorted(
        set(config.S2_ALL_BANDS) - {"B4", "B3", "B2"}
    )
    assert _groups(ws, "mis") == {"s2_10m"}
    assert sc.integrity.checksum == pixel_hash(ws.in_tif("mis"))
    assert sc.fallback is None  # a reference that differs never falls back


def test_unavailable_keeps_the_original_and_is_retried(ws) -> None:
    ws.add_pair("none", gee="none", pc="none")
    out, _ = _run(ws)
    sc, _ = out["S2_none"]
    assert sc.verdict == "UNAVAILABLE"
    assert [b.name for b in sc.bands] == ["B4", "B3", "B2"]
    assert sc.integrity.checksum == pixel_hash(ws.in_tif("none"))
    assert complete.done_tile(ws.row("none"), ws.record()) is None


def test_resume_skips_final_verdicts(ws) -> None:
    ws.add_pair("ok")
    ws.add_pair("mis", gee="shift")
    _run(ws)
    out, skipped = _run(ws)
    assert out == {}
    assert skipped == 2


def test_gee_failures_stay_partial_and_no_other_provider_is_asked(ws) -> None:
    ws.add_pair("fb")
    ws.gee_fail_groups["S2_fb"] = set(config.S2_REQUEST_GROUPS["s2_20m"])
    out, _ = _run(ws)
    sc, _ = out["S2_fb"]
    assert sc.verdict == "PARTIAL"
    assert sc.provider == "gee"
    assert {p for p, _, _ in ws.requests} == {"gee"}


def test_pc_finishes_only_what_gee_left_and_only_when_complete(ws) -> None:
    ws.add_pair("ok")
    ws.add_pair("fb")
    ws.gee_fail_groups["S2_fb"] = set(config.S2_REQUEST_GROUPS["s2_20m"])
    _run(ws)
    ws.requests.clear()
    results, skipped = complete.complete_folder(ws.in_dir, ws.record(), provider="pc")
    assert skipped == 1  # S2_ok is final: not asked again
    sc = {n: x for n, x, _ in results}["S2_fb.json"]
    assert sc.verdict == "COMPLETE"
    assert sc.provider == "pc"
    assert {b.provider for b in sc.bands if b.origin == "provider"} == {"pc"}
    assert {k for _, k, _ in ws.requests} == {"S2_fb"}


def test_pc_tile_has_provenance_when_gee_does_not_know_the_scene(ws, monkeypatch) -> None:
    # as when a scene left Earth Engine: no GEE properties, PC finds it by the ESA name
    ws.add_pair("gone")
    ws.gee_fail_groups["S2_gone"] = set(config.S2_REQUEST_GROUPS["s2_20m"])
    _run(ws)
    monkeypatch.setattr(complete, "_scene", lambda _id: ({}, "EEException: not found"))
    results, _ = complete.complete_folder(ws.in_dir, ws.record(), provider="pc")
    sc = results[0][1]
    assert sc.verdict == "COMPLETE"
    assert sc.radiometry.product_id is None
    assert sc.source.product_id
    n, _ = provenance.write_folder(config.SC_OUT_DIR)
    assert n == 1
    doc = json.loads(next(config.SC_OUT_DIR.rglob("*.prov.json")).read_text())
    assert any(k.startswith("pc:") for k in doc["entity"])


def test_pc_that_does_not_give_a_complete_tile_changes_nothing(ws) -> None:
    ws.add_pair("fbx", pc="shift")
    ws.gee_fail_groups["S2_fbx"] = set(config.S2_REQUEST_GROUPS["s2_20m"])
    _run(ws)
    before = ws.out_tif("fbx").with_suffix(".json").read_bytes()
    results, _ = complete.complete_folder(ws.in_dir, ws.record(), provider="pc")
    assert results[0][1] is None
    assert results[0][2].startswith("NotUsedError: pc did not give a complete tile")
    assert ws.out_tif("fbx").with_suffix(".json").read_bytes() == before  # still GEE PARTIAL


def test_scene_outside_the_pinned_collection_is_refused(ws) -> None:
    ws.add_pair("other", image_id="COPERNICUS/S2/TEST")
    out, _ = _run(ws)
    sc, err = out["S2_other"]
    assert sc is None
    assert "is not in COPERNICUS/S2_SR" in err
    assert ws.requests == []


def test_s2_checksum_is_recorded(ws) -> None:
    ws.add_pair("s2bad", bad_s2_checksum=True)
    out, _ = _run(ws)
    assert out["S2_s2bad"][0].source.dlite_checksum_ok is False


def test_paired_s1_is_copied_only_when_intact(ws) -> None:
    ws.add_pair("ok")
    ws.add_pair("s1bad", bad_s1_checksum=True)
    _run(ws)
    ok_s1 = config.SC_OUT_DIR / ws.row("ok")["s1_file"]
    bad_s1 = config.SC_OUT_DIR / ws.row("s1bad")["s1_file"]
    assert ok_s1.is_file()
    assert ok_s1.read_bytes() == (ws.staging / ws.row("ok")["s1_file"]).read_bytes()
    assert not bad_s1.exists()
    log = logs.latest("complete")
    problems = json.loads(log.read_text(encoding="utf-8"))["s1_not_copied"]
    assert list(problems) == [ws.out_tif("s1bad").name]
    assert "pixel checksum does not match" in problems[ws.out_tif("s1bad").name]


def test_output_is_encoded_like_dlite_and_names_itself(ws) -> None:
    ws.add_pair("ok")
    out, _ = _run(ws)
    sc = out["S2_ok"][0]
    assert sc.source.dlite_file_sha256 == ws.record().member(ws.row("ok")["s2_file"]).sha256
    with rasterio.open(ws.out_tif("ok")) as src:
        # the encoding of the D_lite 2.0.0 tiles: lossless DEFLATE, floating-point
        # predictor, pixel interleave, 256 x 256 blocks, float32, nodata -inf
        assert src.tags(ns="IMAGE_STRUCTURE") == {
            "COMPRESSION": "DEFLATE",
            "INTERLEAVE": "PIXEL",
            "PREDICTOR": "3",
        }
        assert src.block_shapes == [(256, 256)] * len(config.S2_ALL_BANDS)
        assert set(src.dtypes) == {"float32"}
        assert src.nodata == float("-inf")
        assert list(src.descriptions) == list(config.S2_ALL_BANDS)
        tags = src.tags()
    assert tags["STRATA_DATASET"] == config.OUT_DATASET
    assert tags["STRATA_PIXEL_XXHASH64"] == sc.integrity.checksum
    assert tags["STRATA_TILE"] == "S2_ok"


def test_a_member_that_differs_from_the_record_is_not_used(ws) -> None:
    ws.add_pair("ok")
    record = ws.record()
    s1 = record.member(ws.row("ok")["s1_file"])
    tar = ws.record_dir / s1.archive
    data = bytearray(tar.read_bytes())
    data[s1.offset + s1.size // 2] ^= 0xFF  # one byte of the S1 GeoTIFF inside the tar
    tar.write_bytes(bytes(data))
    complete.complete_folder(ws.in_dir, record)
    assert not (config.SC_OUT_DIR / ws.row("ok")["s1_file"]).exists()
    log = logs.latest("complete")
    problems = json.loads(log.read_text(encoding="utf-8"))["s1_not_copied"]
    assert "sha256" in problems[ws.out_tif("ok").name]


def test_resume_redoes_a_tile_whose_dlite_file_changed(ws) -> None:
    ws.add_pair("ok")
    _run(ws)
    side = ws.out_tif("ok").with_suffix(".json")
    data = json.loads(side.read_text(encoding="utf-8"))
    data["source"]["dlite_file_sha256"] = "0" * 64
    side.write_text(json.dumps(data), encoding="utf-8")
    assert complete.done_tile(ws.row("ok"), ws.record()) is None
