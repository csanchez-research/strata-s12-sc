"""prov, report and package on one small run: what is released, what is not, and why."""

from __future__ import annotations

import csv
import hashlib
import json

import pytest

from strata_complete import complete, config, package, provenance, schema
from strata_complete.report import page, source
from strata_complete.report.source import FolderSource


@pytest.fixture
def run(ws):
    ws.add_pair("ok")
    ws.add_pair("mis", gee="shift")
    ws.add_pair("s1bad", bad_s1_checksum=True)
    ws.add_pair("s2bad", bad_s2_checksum=True)
    ws.rec = ws.record()
    complete.complete_folder(ws.in_dir, ws.rec)
    provenance.write_folder(config.SC_OUT_DIR)
    return ws


def test_package_releases_only_usable_pairs(run, tmp_path) -> None:
    meta = package.build(run.in_dir, tmp_path / "release", run.rec)
    assert meta["n_pairs_released"] == 1
    with (tmp_path / "release" / "sc_excluded.csv").open(encoding="utf-8") as fh:
        reasons = {r["pair_id"]: r["reason"] for r in csv.DictReader(fh)}
    assert reasons == {
        "pair-mis": "S2 MISALIGNED",
        "pair-s1bad": "S1 not copied (S1_s1bad.json: pixel checksum does not match its " "sidecar)",
        "pair-s2bad": "S2 D_lite tile: pixel checksum does not match its sidecar",
    }


def test_package_is_reproducible_and_checked(run, tmp_path) -> None:
    a = package.build(run.in_dir, tmp_path / "a", run.rec)["archives"]
    b = package.build(run.in_dir, tmp_path / "b", run.rec)["archives"]
    assert {k: v["sha256"] for k, v in a.items()} == {k: v["sha256"] for k, v in b.items()}
    sums = (tmp_path / "a" / config.SHA256SUMS_NAME).read_text(encoding="utf-8").splitlines()
    for line in sums:
        digest, name = line.split(maxsplit=1)
        assert hashlib.sha256((tmp_path / "a" / name).read_bytes()).hexdigest() == digest


def test_prov_documents_parse_with_the_reference_library(run) -> None:
    prov_model = pytest.importorskip("prov.model")
    docs = sorted(config.SC_OUT_DIR.rglob("*.prov.json"))
    assert len(docs) == 4
    for path in docs:
        doc = prov_model.ProvDocument.deserialize(str(path), format="json")
        assert doc.get_records()
    mis = json.loads(next(p for p in docs if "S2_mis" in p.name).read_text(encoding="utf-8"))
    out = mis["entity"]["sc:S2_mis"]
    assert "B5" in out["sc:bandsNotAdded"]


def test_report_payload_carries_the_funnel_inputs(run) -> None:
    data = page.payload(FolderSource(config.SC_OUT_DIR), progress=False)
    by = {t["name"]: t for t in data["tiles"]}
    assert data["counts"] == {"COMPLETE": 3, "MISALIGNED": 1}
    assert by["S2_s2bad.tif"]["dlite_checksum_ok"] is False
    assert by["S2_s1bad.tif"]["s1"] is None
    assert "S2_s1bad.tif" in data["s1_not_copied"]
    assert by["S2_mis.tif"]["codes"].count("u") == 9
    assert by["S2_ok.tif"]["codes"] == "gdddgggggggg"  # B1, B2-B4 from D_lite, B5-B12
    html = page.render(data)
    assert "From the input to the release" in html
    assert '"S2_ok.tif"' in html


def test_package_move_frees_the_output_and_resumes(run, tmp_path) -> None:
    plain = package.build(run.in_dir, tmp_path / "plain", run.rec)
    s2_ok = config.SC_OUT_DIR / run.row("ok")["s2_file"]
    s2_mis = config.SC_OUT_DIR / run.row("mis")["s2_file"]
    moved = package.build(run.in_dir, tmp_path / "moved", run.rec, move=True)
    assert moved["archives"] == plain["archives"]  # same bytes
    assert not s2_ok.exists()  # released: now only in the tar
    assert s2_mis.exists()  # excluded: kept in out/
    again = package.build(run.in_dir, tmp_path / "moved", run.rec, move=True)
    assert again["archives"] == plain["archives"]
    assert again["n_pairs_released"] == 1
    with (tmp_path / "moved" / "sc_manifest.csv").open(encoding="utf-8") as fh:
        assert [r["pair_id"] for r in csv.DictReader(fh)] == ["pair-ok"]
    sums = (tmp_path / "moved" / config.SHA256SUMS_NAME).read_text(encoding="utf-8")
    assert package.STATE not in sums


def test_package_refuses_a_moved_class_whose_tar_is_gone(run, tmp_path) -> None:
    package.build(run.in_dir, tmp_path / "moved", run.rec, move=True)
    next((tmp_path / "moved").glob("strata-s12-sc_*.tar")).unlink()
    with pytest.raises(RuntimeError, match="restore the tar"):
        package.build(run.in_dir, tmp_path / "moved", run.rec, move=True)


def test_report_rows_are_small_and_carry_no_image(run, monkeypatch) -> None:
    def no_raster(*_args: object) -> None:
        pytest.fail("rows must not read rasters when the sidecar has medians")

    monkeypatch.setattr(page, "_read", no_raster)
    data = page.payload(FolderSource(config.SC_OUT_DIR), progress=False)
    for t in data["tiles"]:
        assert len(json.dumps(t)) < 1500  # 20,000 pairs stay a page a browser opens
        assert "quicklook" not in t
        assert "prov_doc" not in t
    ok = next(t for t in data["tiles"] if t["name"] == "S2_ok.tif")
    assert all(v is not None for v in ok["spectrum"])


def test_a_release_is_shown_from_its_tars_after_move(run, tmp_path) -> None:
    release = tmp_path / "release"
    package.build(run.in_dir, release, run.rec, move=True)
    src = source.open_source(release)
    assert isinstance(src, source.ReleaseSource)
    data = page.payload(src, progress=False)
    assert [t["name"] for t in data["tiles"]] == ["S2_ok.tif"]
    tif = data["tiles"][0]["path"]
    assert not (config.SC_OUT_DIR / tif).exists()  # moved: only in the tar
    assert page.detail(src, tif)["prov_doc"] is not None
    for kind in page.PREVIEW_KINDS:
        assert page.preview(src, tif, kind)[:2] == b"\xff\xd8"  # JPEG
    with (release / package.MEMBER_INDEX).open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["path"] for r in rows} >= {tif, page.s1_of(tif)}


def test_serve_makes_detail_and_previews_only_on_request(run) -> None:
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request
    from http.server import ThreadingHTTPServer

    from strata_complete.report import serve

    state = serve._State(config.SC_OUT_DIR, refresh=3600)
    serve._Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        html = urllib.request.urlopen(base + "/").read()  # noqa: S310 - local test server
        assert b"data:image" not in html
        tif = next(t["path"] for t in state.data["tiles"] if t["name"] == "S2_ok.tif")
        q = urllib.parse.quote(tif)
        assert json.loads(urllib.request.urlopen(f"{base}/tile?path={q}").read())["bands"]  # noqa: S310
        img = urllib.request.urlopen(f"{base}/preview?path={q}&kind=rgb")  # noqa: S310
        assert img.headers["Content-Type"] == "image/jpeg"
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{base}/preview?path=../../etc/passwd&kind=rgb")  # noqa: S310
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_second_provider_study_runs_on_a_release_and_resumes(run, tmp_path, monkeypatch) -> None:
    from strata_complete.verify import crosscheck

    release = tmp_path / "release"
    package.build(run.in_dir, release, run.rec, move=True)
    fake = run.fake("pc")

    def fetch(pid: str, bands: list[str], grid: object, out):
        # the fake provider finds the tile by its temporary folder name
        piece = config.PIECES_DIR / "S2_ok_test"
        piece.mkdir(parents=True, exist_ok=True)
        fake(pid, tuple(bands), grid, piece / "x.tif")
        out.parent.mkdir(parents=True, exist_ok=True)
        (piece / "x.tif").replace(out)
        return out

    monkeypatch.setattr(crosscheck.pc, "fetch", fetch)
    monkeypatch.setattr(
        crosscheck.pc, "item_for_product", lambda pid: {"id": pid, "properties": {}}
    )
    monkeypatch.setattr(crosscheck.pc, "version", lambda: "test")
    results, _, meta, _ = crosscheck.cross_folder(release)
    assert meta["n_tiles_this_run"] == 1
    assert [r.identical for r in results] == [True]
    assert meta["source"].endswith("(release, read from its tars)")
    _, _, again, _ = crosscheck.cross_folder(release)
    assert again["n_tiles_this_run"] == 0  # resumed: nothing left to do
    assert again["n_tiles_checked"] == 1
    assert "sc_crosscheck" not in (release / "sc_manifest.csv").read_text(encoding="utf-8")


def test_gee_release_lists_the_pairs_a_second_provider_can_finish(ws, tmp_path) -> None:
    ws.add_pair("ok")
    ws.add_pair("fb")
    ws.add_pair("mis", gee="shift")
    ws.gee_fail_groups["S2_fb"] = set(config.S2_REQUEST_GROUPS["s2_20m"])
    rec = ws.record()
    complete.complete_folder(ws.in_dir, rec)
    meta = package.build(ws.in_dir, tmp_path / "gee", rec)
    pending = ws.in_dir.with_name(ws.in_dir.name + "_pc")
    with (pending / "lite_manifest.csv").open(encoding="utf-8") as fh:
        assert [r["pair_id"] for r in csv.DictReader(fh)] == ["pair-fb"]  # not MISALIGNED
    assert meta["unfinished"]["n_pairs"] == 1
    # the second run, with the second provider, on those pairs only
    complete.complete_folder(pending, rec, provider="pc")
    pc_meta = package.build(pending, tmp_path / "pc", rec, provider="pc")
    assert pc_meta["n_pairs_released"] == 1
    assert list(pc_meta["archives"]) == ["strata-s12-sc_cropland_pc.tar"]
    assert "unfinished" not in pc_meta
    # the GEE release is not touched by it, and never takes a pc tile
    again = package.build(ws.in_dir, tmp_path / "gee2", rec)
    assert again["n_pairs_released"] == 1
    assert any("not by gee" in k for k in again["excluded_by_reason"])
    # a tile made by Planetary Computer is never compared with Planetary Computer
    from strata_complete.verify import crosscheck

    _, _, study, _ = crosscheck.cross_folder(tmp_path / "pc")
    assert study["n_tiles_this_run"] == 0
    assert study["n_tiles_skipped_completed_by_pc"] == 1
    # and its PROV document derives the added bands from the Planetary Computer scene
    side = complete.out_path(ws.row("fb")["s2_file"]).with_suffix(".json")
    sc = schema.ScSidecar.model_validate_json(side.read_text(encoding="utf-8"))
    doc = provenance.tile_document(sc, None)
    pc_scene = next(k for k in doc["entity"] if k.startswith("pc:"))
    added = {
        d["prov:usedEntity"]
        for d in doc["wasDerivedFrom"].values()
        if d["prov:generatedEntity"].endswith("/B5")
    }
    assert added == {pc_scene}


def test_reproduce_rebuilds_released_pairs_and_archives_both(  # noqa: PLR0915 - one story
    run, tmp_path, monkeypatch
) -> None:
    import json
    import tarfile

    from strata_complete.verify import crosscheck, reproduce

    release = tmp_path / "release"
    package.build(run.in_dir, release, run.rec)
    fake = run.fake("pc")
    shift = {"on": False}

    def fetch(pid: str, bands: list[str], grid: object, out):
        piece = config.PIECES_DIR / "S2_ok_test"
        piece.mkdir(parents=True, exist_ok=True)
        fake(pid, tuple(bands), grid, piece / "x.tif")
        if shift["on"]:  # the provider gives other numbers: a result, not a failure
            import rasterio

            with rasterio.open(piece / "x.tif", "r+") as dst:
                dst.write(dst.read() + 1.0)
        out.parent.mkdir(parents=True, exist_ok=True)
        (piece / "x.tif").replace(out)
        return out

    monkeypatch.setattr(reproduce.pc, "item_for_product", lambda pid: {"id": pid, "properties": {}})

    def cut(*_a: object) -> None:
        msg = "test: connection reset"
        raise OSError(msg)

    monkeypatch.setattr(reproduce, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(crosscheck.pc, "fetch", cut)  # the network fails: not a result
    results, meta, _ = reproduce.run(release, "pc", n=10, seed=77)
    assert not meta["finished"]
    assert meta["n_to_retry"] == 1
    assert not (config.OUT_DIR / "reproduce_pc_n10_seed77").exists()  # nothing archived

    monkeypatch.setattr(crosscheck.pc, "fetch", fetch)  # back: the same command goes on
    results, meta, report = reproduce.run(release, "pc", n=10, seed=77)
    assert meta["finished"]
    shared = crosscheck.load_progress(crosscheck.progress_file(release))
    assert [r.via for r in shared.values()] == ["reproduce"]  # the study will not read it again
    assert next(iter(shared.values())).identical
    assert meta["n_pairs"] == 1  # one pair released in this run
    assert results[0].identical
    assert meta["seed"] == 77
    assert meta["workers"] == 1
    assert meta["context"]["release_sha256sums_sha256"]  # the exact release it read
    assert meta["context"]["planetary_computer"]["gdal_env"]
    assert json.loads(report.read_text())["bands"][0]["band"]
    out = config.OUT_DIR / "reproduce_pc_n10_seed77"
    tif = results[0].s2_file
    side = tif[: -len(".tif")] + ".json"
    cls = tif.split("/")[1].removeprefix("class_")
    with tarfile.open(out / f"reproduce_pc_n10_seed77_{cls}.tar") as tar:
        assert {f"gee/{tif}", f"gee/{side}", f"pc/{tif}", f"pc/{side}"} <= set(tar.getnames())
        rebuilt = json.loads(tar.extractfile(f"pc/{side}").read())
    assert rebuilt["identical"] is True
    assert rebuilt["provider"] == "pc"
    assert (out / "reproduce.csv").is_file()
    assert not (config.DATA_DIR / "reproduce" / "reproduce_pc_n10_seed77").exists()

    _, again, _ = reproduce.run(release, "pc", n=10, seed=77)
    assert again["n_pairs_this_run"] == 0  # resumed: nothing left to do
    assert again["n_identical"] == 1

    shift["on"] = True  # a fraction of every stratum, the provider gives other numbers
    results, meta, _ = reproduce.run(release, "pc", None, 77, fraction=0.1, workers=2)
    assert meta["n_pairs"] == 1  # at least one pair of every stratum
    assert meta["n_differ"] == 1
    assert results[0].pixels_differ == results[0].pixels
    out = config.OUT_DIR / "reproduce_pc_10pct_seed77"
    with tarfile.open(out / f"reproduce_pc_10pct_seed77_{cls}.tar") as tar:
        assert f"pc/{tif}" in tar.getnames()  # a tile that differs is archived too


def test_study_database_follows_the_files_and_can_be_rebuilt(run, tmp_path, monkeypatch) -> None:
    import sqlite3

    from strata_complete.verify import crosscheck, reproduce, studydb

    release = tmp_path / "release_all"
    package.build(run.in_dir, release, run.rec)
    fake = run.fake("pc")

    def fetch(pid: str, bands: list[str], grid: object, out):
        piece = config.PIECES_DIR / "S2_ok_test"
        piece.mkdir(parents=True, exist_ok=True)
        fake(pid, tuple(bands), grid, piece / "x.tif")
        out.parent.mkdir(parents=True, exist_ok=True)
        (piece / "x.tif").replace(out)
        return out

    monkeypatch.setattr(crosscheck.pc, "fetch", fetch)
    monkeypatch.setattr(reproduce.pc, "item_for_product", lambda pid: {"id": pid, "properties": {}})
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)  # the release is found next to the database
    reproduce.run(release, "pc", n=10, seed=77)

    def counts() -> dict[str, int]:
        with sqlite3.connect(studydb.db_path()) as conn:
            return {
                t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
                for t in ("studies", "tiles", "bands", "history", "manifest", "runs")
            }

    live = counts()
    assert live["tiles"] == 2  # the pair in the demonstration and in the study of the release
    assert live["bands"] == 2 * 12
    assert live["manifest"] == 1
    assert live["runs"] == 1
    with sqlite3.connect(studydb.db_path()) as conn:
        via = conn.execute(
            "SELECT study, via, identical, same_zone FROM tiles ORDER BY study"
        ).fetchall()
        stratum = conn.execute(
            "SELECT m.class_name FROM tiles t JOIN manifest m ON m.s2_file = t.path LIMIT 1"
        ).fetchone()
    assert [v[1] for v in via] == ["reproduce", "reproduce"]
    assert all(v[2] == 1 for v in via)
    assert stratum is not None
    studydb.rebuild()  # from the files alone: the same content
    assert counts() == live


def test_notes_on_a_release_are_web_annotations_and_seal_into_the_sums(
    run, tmp_path, monkeypatch
) -> None:
    import hashlib
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from strata_complete import notes
    from strata_complete.report import serve

    release = tmp_path / "release"
    package.build(run.in_dir, release, run.rec, move=True)
    monkeypatch.setenv("SC_NOTES_CREATOR", "Test Curator")
    state = serve._State(release, refresh=3600)
    serve._Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        name = state.data["tiles"][0]["name"]
        for body, saved in (
            ({"name": name, "flag": True, "note": "thin cloud NE"}, True),
            ({"name": name, "flag": True, "note": "thin cloud NE"}, False),  # no change
            ({"name": name, "flag": False, "note": "thin cloud NE corner"}, True),
        ):
            req = urllib.request.Request(  # noqa: S310 - local test server
                base + "/review",
                data=json.dumps(body).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            assert json.loads(urllib.request.urlopen(req).read())["saved"] is saved  # noqa: S310
        page_data = json.loads(urllib.request.urlopen(base + "/data.json").read())  # noqa: S310
    finally:
        server.shutdown()
        server.server_close()
    doc = json.loads((release / notes.NOTES).read_text(encoding="utf-8"))
    assert doc["@context"] == "http://www.w3.org/ns/anno.jsonld"
    assert doc["type"] == "AnnotationCollection"
    got = [(a["motivation"], a["body"]["value"]) for a in doc["first"]["items"]]
    assert got == [
        ("assessing", "flagged"),
        ("commenting", "thin cloud NE"),
        ("assessing", "not flagged"),
        ("commenting", "thin cloud NE corner"),
    ]
    assert doc["first"]["items"][0]["creator"]["name"] == "Test Curator"
    mark = next(t["review"] for t in page_data["tiles"] if t["name"] == name)
    assert mark["note"] == "thin cloud NE corner"
    assert mark["flag"] is False
    assert not any(
        p.name.endswith(".tar") and p.stat().st_mtime > (release / notes.NOTES).stat().st_mtime
        for p in release.iterdir()
    )  # archives untouched

    meta = notes.seal(release)
    assert meta["n_annotations"] == 4
    assert (release / notes.NOTES_CSV).read_text(encoding="utf-8").count("\n") == 5
    sums = dict(
        reversed(line.split(maxsplit=1))
        for line in (release / config.SHA256SUMS_NAME).read_text(encoding="utf-8").splitlines()
    )
    for n in (notes.NOTES, notes.NOTES_CSV, "release.json"):
        assert sums[n] == hashlib.sha256((release / n).read_bytes()).hexdigest()
    assert json.loads((release / "release.json").read_text())["notes"]["n_pairs_with_notes"] == 1

    doc["first"]["items"][0]["target"]["source"] = "s2/not/in/the/release.tif"
    (release / notes.NOTES).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(notes.NotesError, match="not a file of the release"):
        notes.seal(release)


def test_saving_the_same_review_twice_writes_nothing(tmp_path) -> None:
    mark, changed = page.save_review(tmp_path, "t.tif", flag=True, note="cloud")
    assert changed
    again, changed = page.save_review(tmp_path, "t.tif", flag=True, note=" cloud ")
    assert not changed
    assert again["at"] == mark["at"]
    assert page.load_review(tmp_path)["t.tif"]["at"] == mark["at"]
