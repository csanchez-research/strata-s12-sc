"""Shared fixtures: a throwaway workspace, a synthetic D_lite record, fake providers.

Nothing here touches the network or Earth Engine. The tiles are small (64 x 64)
synthetic rasters with sidecars that follow the D_lite 2.0.0 schema; their values
are made up for the tests and say so (``test`` ids, ``TEST`` scene names). The
record is a local folder laid out like the real one: a class tar,
``member_index.csv``, ``lite_manifest.csv`` and ``SHA256SUMS.txt``, pinned by the
sha256 of the last.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import pytest
import rasterio
import xxhash
from rasterio.transform import Affine

from strata_complete import complete, config
from strata_complete.dlite.record import Record
from strata_complete.providers import gee, pc

if TYPE_CHECKING:
    from pathlib import Path

SIZE = 64
CRS = "EPSG:32638"
TRANSFORM = Affine(10.0, 0.0, 500_000.0, 0.0, -10.0, 4_500_000.0)
S2_DLITE = ("B4", "B3", "B2")
S1_BANDS = ("VV", "VH", "VV_VH_DIFF")
CLASS = "cropland"
PRODUCT_ID = "S2B_MSIL2A_20250601T000000_N0511_R001_T38TPL_20250601T000000"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pixel_hash(path: Path) -> str:
    with rasterio.open(path) as src:
        return xxhash.xxh64(np.ascontiguousarray(src.read()).tobytes()).hexdigest()


def write_tif(path: Path, data: np.ndarray, names: tuple[str, ...] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "width": SIZE,
        "height": SIZE,
        "count": data.shape[0],
        "dtype": "float32",
        "crs": CRS,
        "transform": TRANSFORM,
        "nodata": float("-inf"),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype("float32"))
        if names:
            dst.descriptions = names


def sidecar(sensor: str, pair_id: str, name: str, checksum: str) -> dict:
    """A D_lite 2.0.0 sidecar with made-up values, valid against ``DliteSidecar``."""
    s2 = sensor == "S2"
    seam = {"found": False, "angle_deg": None, "offset_px": None, "method": "test", "version": "0"}
    acquisition = {
        "download_timestamp": "2026-01-01T00:00:00Z",
        "satellite_acquisition_time": "2025-06-01T08:00:00Z",
        "spacecraft": "Sentinel-2B" if s2 else "Sentinel-1A",
        "max_cloud_percentage": 10.0 if s2 else None,
        "temporal_pairing_tolerance_days": 3,
    }
    if s2:
        acquisition |= {
            "relative_orbit": 1,
            "product_id": PRODUCT_ID,
            "processing_baseline": "05.11",
            "boa_add_offset": {b: -1000 for b in S2_DLITE},
        }
        quality = {"straight_seam": seam | {"step_dn": None, "in_s1": None}}
    else:
        acquisition |= {"orbit_direction": "DESCENDING", "relative_orbit": 1, "absolute_orbit": 1}
        quality = {
            "sar_cross": {
                "n_crosses": 0,
                "longest_arm_px": 0,
                "band": "VV",
                "method": "test",
                "version": "0",
            },
            "straight_seam": seam | {"step_db": None, "band": None, "in_s2": None},
        }
    return {
        "tile_id": f"test-{name}",
        "pair_id": pair_id,
        "filename": f"{name}.tif",
        "sensor": sensor,
        "integrity": {
            "checksum": checksum,
            "algorithm": "xxhash64",
            "note": "xxHash64 of the decoded pixel array (bands, height, width), float32, C order",
        },
        "coordinates": {
            "lat": 40.6,
            "lon": 45.0,
            "crs": CRS,
            "datum": "WGS84",
            "bbox": [45.0, 40.6, 45.01, 40.61],
        },
        "dimensions": {
            "width": SIZE,
            "height": SIZE,
            "pixel_resolution_m": 10,
            "bands": list(S2_DLITE if s2 else S1_BANDS),
            "dtype": "float32",
            "nodata": "-inf",
        },
        "classification": {
            "season": "summer",
            "hemisphere": "north",
            "season_raw": "summer",
            "land_cover_class": CLASS,
            "class_id": 40,
            "geographic_region": "ASIA",
            "label_purity": 0.9,
            "label_composition": [{"class_id": 40, "class_name": CLASS, "ratio": 0.9}],
            "label_source": "ESA_WorldCover_2021_v200",
        },
        "data_source": {
            "provider": "Google Earth Engine",
            "mission": "ESA Copernicus Programme",
            "collection": "COPERNICUS/S2_SR" if s2 else "COPERNICUS/S1_GRD",
            "processing_level": "L2A" if s2 else "GRD",
        },
        "acquisition": acquisition,
        "license": {
            "source": "Copernicus Sentinel data",
            "terms": "CC BY 4.0",
            "url": "https://dataspace.copernicus.eu/terms-and-conditions",
            "citation": "European Space Agency",
        },
        "provenance": {
            "dataset_version": "2.0.0",
            "processing_timestamp": "2026-01-01T00:00:00Z",
            "sentinel2_image_id": f"COPERNICUS/S2_SR/TEST_{pair_id}",
            "sentinel1_image_id": f"COPERNICUS/S1_GRD/TEST_{pair_id}",
        },
        "quality_metrics": {"temporal_gap_days": 0.5, "note": "test"} | quality,
        "stac_version": "1.0.0",
    }


@dataclass
class Workspace:
    """Paths of one test run and the behaviour of the fake providers per tile."""

    root: Path
    in_dir: Path
    record_dir: Path
    staging: Path
    rows: list[dict[str, str]] = field(default_factory=list)
    # tile key -> "same" (copy of D_lite), "shift" (D_lite + 5), "none" (no data)
    gee: dict[str, str] = field(default_factory=dict)
    gee_fail_groups: dict[str, set[str]] = field(default_factory=dict)
    pc: dict[str, str] = field(default_factory=dict)
    requests: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)
    dlite: dict[str, np.ndarray] = field(default_factory=dict)

    def add_pair(
        self,
        key: str,
        *,
        gee: str = "same",
        pc: str = "same",
        bad_s2_checksum: bool = False,
        bad_s1_checksum: bool = False,
        image_id: str | None = None,
    ) -> dict[str, str]:
        pair_id = f"pair-{key}"
        sub = f"class_{CLASS}/season_summer/region_ASIA"
        rel = {
            "S2": f"s2/{sub}/S2_{key}.tif",
            "S1": f"s1/{sub}/S1_{key}.tif",
        }
        rng = np.random.default_rng(len(self.rows) + 1)
        s2 = rng.integers(1000, 5000, (3, SIZE, SIZE)).astype("float32")
        s1 = rng.uniform(-30, 0, (3, SIZE, SIZE)).astype("float32")
        for sensor, data, bad in (("S2", s2, bad_s2_checksum), ("S1", s1, bad_s1_checksum)):
            tif = self.staging / rel[sensor]
            write_tif(tif, data, S2_DLITE if sensor == "S2" else S1_BANDS)
            meta = sidecar(sensor, pair_id, tif.stem, "0" * 16 if bad else pixel_hash(tif))
            if sensor == "S2" and image_id:
                meta["provenance"]["sentinel2_image_id"] = image_id
            tif.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.dlite[f"S2_{key}"] = s2
        self.gee[f"S2_{key}"], self.pc[f"S2_{key}"] = gee, pc
        row = {
            "pair_id": pair_id,
            "tile_id": f"test-S2_{key}",
            "class_name": CLASS,
            "season": "summer",
            "region": "ASIA",
            "s2_file": rel["S2"],
            "s1_file": rel["S1"],
        }
        self.rows.append(row)
        return row

    def record(self) -> Record:
        """Write the record folder (tar, index, manifest, sums) and the input selection."""
        tar_name = config.dlite_tar_name(CLASS)
        files = sorted(p for p in self.staging.rglob("*") if p.is_file())
        self.record_dir.mkdir(parents=True, exist_ok=True)
        tar_path = self.record_dir / tar_name
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
            for f in files:
                tar.add(f, arcname=f.relative_to(self.staging).as_posix())
        index = io.StringIO()
        writer = csv.writer(index, lineterminator="\n")
        writer.writerow(["archive", "path", "offset_data", "size", "sha256"])
        with tarfile.open(tar_path) as tar:
            for m in tar.getmembers():
                data = (self.staging / m.name).read_bytes()
                writer.writerow([tar_name, m.name, m.offset_data, m.size, _sha(data)])
        (self.record_dir / config.MEMBER_INDEX_NAME).write_text(index.getvalue(), "utf-8")
        for folder in (self.record_dir, self.in_dir):
            with (folder / "lite_manifest.csv").open("w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(self.rows[0]), lineterminator="\n")
                w.writeheader()
                w.writerows(self.rows)
        names = sorted(p.name for p in self.record_dir.iterdir() if p.name != "SHA256SUMS.txt")
        sums = "".join(f"{_sha((self.record_dir / n).read_bytes())}  {n}\n" for n in names)
        (self.record_dir / config.SHA256SUMS_NAME).write_text(sums, encoding="utf-8")
        config.RECORD_SHA256SUMS_SHA256 = _sha(sums.encode())
        return Record(self.record_dir)

    def row(self, key: str) -> dict[str, str]:
        return next(r for r in self.rows if r["tile_id"] == f"test-S2_{key}")

    def out_tif(self, key: str) -> Path:
        return config.SC_OUT_DIR / self.row(key)["s2_file"]

    def in_tif(self, key: str) -> Path:
        return self.staging / self.row(key)["s2_file"]

    def fake(self, provider: str):
        def fetch(scene: str, bands: tuple[str, ...], grid: object, out: Path) -> Path:
            # each tile is fetched into its own temporary folder named after it
            key = next(k for k in self.dlite if out.parent.name.startswith(k + "_"))
            self.requests.append((provider, key, tuple(bands)))
            mode = (self.gee if provider == "gee" else self.pc)[key]
            failing = self.gee_fail_groups.get(key, set()) if provider == "gee" else set()
            if mode == "none" or (set(bands) & failing):
                err = gee.GeeError if provider == "gee" else pc.PcError
                msg = f"test: {provider} has no data for {bands}"
                raise err(msg)
            data = self.dlite[key]
            shift = 5.0 if mode == "shift" else 0.0
            layers = [
                data[S2_DLITE.index(b)] + shift if b in S2_DLITE else np.full((SIZE, SIZE), 1500.0)
                for b in bands
            ]
            write_tif(out, np.stack(layers))
            return out

        return fetch


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    for name, sub in (
        ("ROOT", ""),
        ("IN_DIR", "in"),
        ("OUT_DIR", "out"),
        ("LOG_DIR", "log"),
        ("DATA_DIR", "data"),
    ):
        monkeypatch.setattr(config, name, tmp_path / sub)
    monkeypatch.setattr(config, "SC_OUT_DIR", tmp_path / "out" / "strata-s12-sc")
    monkeypatch.setattr(config, "PIECES_DIR", tmp_path / "data" / "pieces")
    monkeypatch.setattr(config, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(config, "MAX_ATTEMPTS_PER_REQUEST", 1)
    w = Workspace(
        root=tmp_path,
        in_dir=tmp_path / "in" / "sample_test",
        record_dir=tmp_path / "record",
        staging=tmp_path / "staging",
    )
    w.in_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "RECORD_DIR", str(w.record_dir))
    monkeypatch.setattr(config, "RECORD_SHA256SUMS_SHA256", "")
    monkeypatch.setattr(gee, "init", lambda: None)
    monkeypatch.setattr(gee, "version", lambda: "test")
    monkeypatch.setattr(pc, "version", lambda: "test")
    monkeypatch.setattr(complete.network, "wait_until_online", lambda _reason: 0)
    monkeypatch.setattr(
        complete,
        "_scene",
        lambda _id: ({"PRODUCT_ID": PRODUCT_ID, "PROCESSING_BASELINE": "05.11"}, None),
    )
    monkeypatch.setitem(complete._FETCHERS, "gee", w.fake("gee"))
    monkeypatch.setitem(complete._FETCHERS, "pc", w.fake("pc"))
    return w
