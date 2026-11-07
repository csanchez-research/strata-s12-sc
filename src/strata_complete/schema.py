"""Single definition of the file formats strata-complete reads and writes.

Every field carries its meaning, and the JSON Schema files in ``schema/`` are exported
from these models (``python -m strata_complete schema-export``), never written by hand.

Models:

* ``DliteSidecar``: the ``.json`` sidecar shipped with every D_lite ``.tif``
  (STRATA-S12 D_lite 2.0.0). Fields are exactly those of the published sidecars,
  as described in the README of the record.
  Validation is strict: an unknown or missing field is an error, and the fields
  of one sensor are refused in the sidecar of the other.
* ``TiffInfo``: what is read from the GeoTIFF itself, compared with the sidecar.
* ``ScSidecar``: the sidecar of every STRATA-S12-SC tile written by ``complete``.
"""

from __future__ import annotations

import json
from datetime import datetime  # noqa: TCH003 - pydantic needs it at runtime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION = "1.5.0"

S1_BANDS = ("VV", "VH", "VV_VH_DIFF")
S2_BANDS = ("B4", "B3", "B2")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- D_lite sidecar ----------------------------------------------------------


class Integrity(_Strict):
    checksum: str = Field(
        description="xxHash64 (hex) of the decoded pixel array, read whole and in band "
        "order (bands, height, width), float32, C order. Not a hash of the file bytes.",
    )
    algorithm: Literal["xxhash64"]
    note: str


class Coordinates(_Strict):
    lat: float = Field(description="Latitude of the tile centre, WGS84.")
    lon: float = Field(description="Longitude of the tile centre, WGS84.")
    crs: str = Field(description="Native projected CRS of the raster (local UTM zone).")
    datum: str
    bbox: tuple[float, float, float, float] = Field(
        description="Footprint in WGS84, [west, south, east, north].",
    )


class Dimensions(_Strict):
    width: int = Field(description="Pixels.")
    height: int = Field(description="Pixels.")
    pixel_resolution_m: int
    bands: list[str] = Field(description="Band names, in storage order.")
    dtype: Literal["float32"]
    nodata: Literal["-inf"] = Field(description="The string '-inf': JSON has no infinity.")


class LabelShare(_Strict):
    class_id: int = Field(description="ESA WorldCover class code.")
    class_name: str
    ratio: float = Field(description="Fraction of the tile covered by this class.")


class Classification(_Strict):
    season: str = Field(description="Meteorological season, hemisphere-corrected.")
    hemisphere: Literal["north", "south"]
    season_raw: str
    land_cover_class: str = Field(description="Dominant ESA WorldCover 2021 v200 class.")
    class_id: int
    geographic_region: str
    label_purity: float = Field(description="Fraction covered by the dominant class.")
    label_composition: list[LabelShare]
    label_source: str


class DataSource(_Strict):
    provider: str
    mission: str
    collection: str = Field(description="Source collection, e.g. COPERNICUS/S2_SR (GEE).")
    processing_level: str


class Acquisition(_Strict):
    """Common fields, then the fields of one sensor (the others are absent)."""

    download_timestamp: datetime
    satellite_acquisition_time: datetime = Field(description="Sensing time of the scene.")
    spacecraft: str = Field(description="Platform, e.g. 'Sentinel-2B'.")
    max_cloud_percentage: float | None = Field(description="Cloud threshold (null for S1).")
    temporal_pairing_tolerance_days: int
    relative_orbit: int
    product_id: str | None = Field(default=None, description="S2: ESA product name.")
    processing_baseline: str | None = Field(default=None, description="S2: e.g. 05.11.")
    boa_add_offset: dict[str, float] | None = Field(
        default=None, description="S2: BOA_ADD_OFFSET of each band in the tile."
    )
    orbit_direction: Literal["ASCENDING", "DESCENDING"] | None = Field(
        default=None, description="S1."
    )
    absolute_orbit: int | None = Field(default=None, description="S1.")


class License(_Strict):
    source: str
    terms: str
    url: str
    citation: str


class Provenance(_Strict):
    dataset_version: str
    processing_timestamp: datetime = Field(description="Build date of the D_lite version.")
    sentinel2_image_id: str = Field(
        description="GEE system:index of the Sentinel-2 scene, with collection prefix.",
    )
    sentinel1_image_id: str = Field(
        description="GEE id of the Sentinel-1 scene; the last part is the full ESA product name.",
    )


class S2Seam(_Strict):
    """Straight-seam indicator of a Sentinel-2 tile (an indication, not a verdict)."""

    found: bool
    step_dn: float | None
    angle_deg: float | None
    offset_px: float | None
    in_s1: bool | None
    method: str
    version: str


class S1Seam(_Strict):
    """Straight-seam indicator of a Sentinel-1 tile (an indication, not a verdict)."""

    found: bool
    step_db: float | None
    band: str | None
    angle_deg: float | None
    offset_px: float | None
    in_s2: bool | None
    method: str
    version: str


class SarCross(_Strict):
    """SAR sidelobe cross indicator of a Sentinel-1 tile (an indication, not a verdict)."""

    n_crosses: int
    longest_arm_px: int
    band: str
    method: str
    version: str


class QualityMetrics(_Strict):
    temporal_gap_days: float = Field(description="|S1 - S2| sensing time difference, days.")
    note: str
    straight_seam: S2Seam | S1Seam
    sar_cross: SarCross | None = Field(default=None, description="S1 only.")


class DliteSidecar(_Strict):
    """Sidecar of one D_lite tile (one sensor of one pair)."""

    tile_id: str = Field(description="Geographic cell; repeats across pairs on purpose.")
    pair_id: str = Field(description="This S1-S2 pairing. A pair is tile_id + pair_id.")
    filename: str
    sensor: Literal["S1", "S2"]
    integrity: Integrity
    coordinates: Coordinates
    dimensions: Dimensions
    classification: Classification
    data_source: DataSource
    acquisition: Acquisition
    license: License
    provenance: Provenance
    quality_metrics: QualityMetrics
    stac_version: str

    @model_validator(mode="after")
    def _fields_match_sensor(self) -> DliteSidecar:
        s2 = self.sensor == "S2"
        want = S2_BANDS if s2 else S1_BANDS
        if tuple(self.dimensions.bands) != want:
            msg = f"{self.sensor} sidecar bands {self.dimensions.bands} != {list(want)}"
            raise ValueError(msg)
        acq, qm = self.acquisition, self.quality_metrics
        given = {
            "product_id": acq.product_id is not None,
            "processing_baseline": acq.processing_baseline is not None,
            "boa_add_offset": acq.boa_add_offset is not None,
            "orbit_direction": acq.orbit_direction is not None,
            "absolute_orbit": acq.absolute_orbit is not None,
            "sar_cross": qm.sar_cross is not None,
        }
        s2_only = {"product_id", "processing_baseline", "boa_add_offset"}
        expected = s2_only if s2 else set(given) - s2_only
        wrong = sorted(k for k, v in given.items() if v != (k in expected))
        if wrong or isinstance(qm.straight_seam, S2Seam) != s2:
            msg = f"{self.sensor} sidecar: fields of the other sensor, or missing: {wrong}"
            raise ValueError(msg)
        return self


# --- GeoTIFF -------------------------------------------------------------------


class BandStats(_Strict):
    name: str
    min: float
    max: float
    n_zero: int = Field(description="Pixels exactly equal to 0.")
    n_nonfinite: int = Field(description="NaN or infinite pixels.")


class TiffInfo(_Strict):
    """What the GeoTIFF itself says. Used instead of the sidecar where they differ."""

    path: str
    width: int
    height: int
    count: int
    dtype: str
    nodata: float | None
    crs: str
    transform: tuple[float, float, float, float, float, float] = Field(
        description="Affine geotransform (a, b, c, d, e, f) as in rasterio/affine.",
    )
    bounds_native: tuple[float, float, float, float]
    bounds_wgs84: tuple[float, float, float, float]
    descriptions: list[str | None]
    xxhash64: str = Field(description="Recomputed per-pixel checksum (same rule as Integrity).")
    bands: list[BandStats]


# --- STRATA-S12-SC sidecar (output of strata-complete) ------------------------
# Verified tile: every band in a stable order, a band that could not be obtained
# marked failed. Tile not verified: the D_lite bands only, the rest in not_added.
# Everything the provider returns about the scene is kept in ``scene_properties``.

BandStatus = Literal["ok", "failed"]
Verdict = Literal["COMPLETE", "MISALIGNED", "PARTIAL", "UNAVAILABLE"]
BandOrigin = Literal["dlite", "provider"]


class BandRecord(_Strict):
    """One band of the output tile."""

    name: str = Field(description="Band name as in the source collection (e.g. B8A, VV).")
    index: int = Field(description="1-based band index in the output GeoTIFF.")
    origin: BandOrigin = Field(
        description="dlite: copied from the D_lite tile; provider: downloaded by this tool.",
    )
    derived: bool = Field(
        description="True only for channels computed from others. The tool never derives; "
        "the only derived channel is VV_VH_DIFF, inherited from D_lite.",
    )
    status: BandStatus
    native_resolution_m: int | None = Field(description="Native pixel size of the band.")
    provider: str | None = Field(description="Provider module that returned the band.")
    request_group: str | None = Field(
        description="Download request the band came from (bands are fetched in groups "
        "and composed locally; see attempts).",
    )
    attempts: int = Field(description="Download attempts for this band.")
    error: str | None = Field(description="Last error if status is failed.")
    dtype: str | None
    nodata: float | None
    min: float | None
    max: float | None
    median: float | None = Field(
        default=None,
        description="Median of the finite pixels, in DN. "
        "Reflectance = (median + boa_add_offset) / 10000.",
    )
    n_nonfinite: int | None
    xxhash64: str | None = Field(description="xxHash64 of this band's decoded pixel array.")


class ReferenceCheck(_Strict):
    """A band already in D_lite, downloaded again and compared pixel by pixel."""

    band: str
    available: bool = Field(
        description="False when the provider did not return the band: nothing was compared.",
    )
    n_pixels: int
    n_equal: int
    max_abs_diff: float | None
    identical: bool


class GridInfo(_Strict):
    crs: str
    transform: tuple[float, float, float, float, float, float]
    width: int
    height: int


class SourceInfo(_Strict):
    dlite_doi: str = Field(description="Version DOI of the D_lite record used as input.")
    dlite_filename: str
    dlite_file_sha256: str | None = Field(
        default=None,
        description="sha256 of the D_lite GeoTIFF as listed in member_index.csv of the record.",
    )
    dlite_checksum: str = Field(description="integrity.checksum of the D_lite tile.")
    dlite_checksum_ok: bool | None = Field(
        default=None,
        description="Whether that checksum matches the xxHash64 of the D_lite pixels as read "
        "by complete.",
    )
    tile_id: str
    pair_id: str
    sensor: Literal["S1", "S2"]
    image_id: str = Field(description="Scene id as recorded in the D_lite sidecar.")
    product_id: str | None = Field(
        default=None,
        description="ESA product name as recorded in the D_lite sidecar "
        "(acquisition.product_id), by which Planetary Computer finds the scene. New in 1.5.0: "
        "absent in sidecars of schema 1.4.0, such as those of STRATA-S12-SC 1.0.0.",
    )


class Radiometry(_Strict):
    """Radiometric provenance of the Sentinel-2 scene, promoted from ``scene_properties``.

    Read from the GEE image properties of COPERNICUS/S2_SR (``PRODUCT_ID``,
    ``PROCESSING_BASELINE``, ``BOA_ADD_OFFSET_<band>``, ``SENSING_ORBIT_DIRECTION``).
    Reflectance = (DN + offset) / 10000, with the offset as published by ESA.
    """

    product_id: str | None = Field(description="ESA product name (GEE PRODUCT_ID).")
    processing_baseline: str | None = Field(description="GEE PROCESSING_BASELINE, e.g. 05.11.")
    boa_add_offset: dict[str, float] = Field(
        description="GEE BOA_ADD_OFFSET_<band> for each band of the output tile. "
        "The DN stored in the tile still contain it (as in D_lite).",
    )
    sensing_orbit_direction: str | None = Field(description="GEE SENSING_ORBIT_DIRECTION.")
    error: str | None = Field(description="Why fields are null or missing, if they are.")


class PairedSar(_Strict):
    """Acquisition geometry of the Sentinel-1 scene paired with this tile.

    Read from the GEE image properties of COPERNICUS/S1_GRD (property names as
    in the GEE catalog, read 2026-10-25). Useful to split or filter pairs by
    look direction: the same ground seen ascending and descending differs in
    layover, shadow and local incidence angle.
    """

    image_id: str = Field(description="provenance.sentinel1_image_id of the D_lite pair.")
    orbit_pass: Literal["ASCENDING", "DESCENDING"] | None = Field(
        description="GEE orbitProperties_pass: orbit direction at the start of the product.",
    )
    relative_orbit_number: int | None = Field(
        description="GEE relativeOrbitNumber_start (track).",
    )
    absolute_orbit_number: int | None = Field(description="GEE orbitNumber_start.")
    platform: str | None = Field(description="GEE platform_number (A, B, C...).")
    instrument_mode: str | None = Field(description="GEE instrumentMode (IW for D_lite).")
    error: str | None = Field(description="Why the fields are null, if they are.")
    properties: dict[str, object] = Field(
        description="All GEE properties of the S1 scene, as received.",
    )


class FallbackInfo(_Strict):
    """A tile asked again from the fallback provider because GEE left bands missing.

    The whole tile is requested (never single bands), so a tile never mixes
    providers. The copy is used only if every band came back and its own reference
    check is identical; otherwise the GEE result stays and this records why.
    """

    provider: str = Field(description="Fallback provider module, e.g. pc.")
    reason: str = Field(description="Bands GEE did not return.")
    product_id: str | None = Field(description="ESA product name used to find the scene.")
    used: bool = Field(description="True if the bands of the tile come from this provider.")
    outcome: str
    reference_check: list[ReferenceCheck] | None = Field(
        default=None, description="Reference check of the fallback copy, if it was fetched."
    )


class ToolInfo(_Strict):
    name: Literal["strata-complete"]
    version: str
    base_image: str = Field(description="Base image with digest the tool ran on.")
    provider_versions: dict[str, str] = Field(description="e.g. earthengine-api version.")


class OutputIntegrity(_Strict):
    checksum: str
    algorithm: Literal["xxhash64"]
    scope: Literal["decoded pixel array (bands, height, width)"]


class NotAdded(_Strict):
    """A band the tool did not add to the tile, and why."""

    name: str
    reason: str


class ScSidecar(_Strict):
    """Sidecar of one STRATA-S12-SC tile."""

    schema_version: str = Field(description=f"Version of this schema ({SCHEMA_VERSION}).")
    created: datetime
    tool: ToolInfo
    source: SourceInfo
    grid: GridInfo
    provider: Literal["gee", "pc"] | None = Field(
        default=None,
        description="Provider of this tile: the bands added and the reference check. "
        "One provider per tile.",
    )
    complete: bool = Field(
        description="True when the reference check passed and every band has status ok."
    )
    reference_check: list[ReferenceCheck] = Field(
        description="D_lite bands re-downloaded and compared; all identical means the "
        "provider returned the same scene on the same grid.",
    )
    bands: list[BandRecord] = Field(
        description="The bands in the output GeoTIFF. If the reference check did not pass, "
        "only the D_lite bands, unchanged and in their D_lite order."
    )
    not_added: list[NotAdded] = Field(
        default=[],
        description="Bands not added because the reference check did not pass; the tile "
        "is then the D_lite tile as published.",
    )
    scene_properties: dict[str, object] = Field(
        description="Scene metadata exactly as returned by the provider (GEE image properties).",
    )
    fallback: FallbackInfo | None = Field(
        default=None,
        description="Not set by this version: a second provider is a separate run, recorded "
        "in ``provider``.",
    )
    radiometry: Radiometry
    classification: Classification = Field(
        description="Copied unchanged from the D_lite sidecar (inherited, not recomputed).",
    )
    paired_sar: PairedSar
    timings_s: dict[str, float] = Field(
        description="Wall-clock seconds per step of this tile: fetch_<group> (retries "
        "included), fallback_<provider>_<group> if the fallback was tried, "
        "scene_properties_s2, scene_properties_s1, write, total. Measured with the "
        "number of workers recorded in the run summary (log/).",
    )
    integrity: OutputIntegrity

    @property
    def verdict(self) -> Verdict:
        """COMPLETE, MISALIGNED, PARTIAL or UNAVAILABLE.

        MISALIGNED: a D_lite band came back different from the published pixels, so
        the provider's bands cannot be shown to match them; nothing is added.
        UNAVAILABLE: the provider did not return the D_lite bands; nothing is verified
        and nothing is added.
        """
        refs = self.reference_check
        if any(c.available and not c.identical for c in refs):
            return "MISALIGNED"
        if not refs or not all(c.available for c in refs):
            return "UNAVAILABLE"
        return "COMPLETE" if self.complete else "PARTIAL"


EXPORTED: dict[str, type[BaseModel]] = {
    "dlite_sidecar": DliteSidecar,
    "tiff_info": TiffInfo,
    "sc_sidecar": ScSidecar,
}


def json_schema(key: str) -> dict:
    """The JSON Schema of ``EXPORTED[key]``, with an ``$id`` naming this schema version."""
    doc = EXPORTED[key].model_json_schema()
    doc["$id"] = f"strata-complete/{key}/{SCHEMA_VERSION}"
    return doc


def export(out_dir: Path) -> list[Path]:
    """Write every JSON Schema to ``out_dir/<key>.schema.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for key in EXPORTED:
        path = out_dir / f"{key}.schema.json"
        path.write_text(json.dumps(json_schema(key), indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
