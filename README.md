# strata-complete

`strata-complete` adds every Sentinel-2 Level-2A band that is missing from the
published Sentinel-1/Sentinel-2 pairs of STRATA-S12 (D_lite) 2.0.0, and checks each
added band against the bands already published. The output is the dataset
**STRATA-S12 Spectral Completion** (STRATA-S12-SC): every Sentinel-2 tile with its
twelve spectral bands, the Sentinel-1 tile of its pair, a sidecar per tile validated
against a JSON Schema, and a W3C PROV document per tile.

- Input: STRATA-S12 (D_lite) 2.0.0, [10.5281/zenodo.23837956](https://doi.org/10.5281/zenodo.23837956),
  read from Zenodo or from a local copy of the record, tile by tile, every byte checked.
- Output: STRATA-S12-SC, DOI 10.5281/zenodo.23888967.
- Providers: Google Earth Engine (`COPERNICUS/S2_SR`) and Microsoft Planetary Computer
  (`sentinel-2-l2a`).

Step-by-step operation: [RUNBOOK.md](RUNBOOK.md).

## Principle

The tool adds **measured bands** at the product level of the original data, and
nothing else. It never adds derived products (indices, band differences,
classifications); they can be computed from the bands. The VV−VH channel of D_lite is
the one exception, kept because it is part of the published pairs.

A band is added only after a check: the provider's copy of the bands D_lite already
publishes (B4, B3, B2) must be identical to them, pixel for pixel. Published bands are
never replaced.

## Quick start

```bash
docker compose run --rm --build --service-ports dev      # host: build and open the console
python -m pytest                                         # container: tests, no network
python -m strata_complete env-check                      # record, Earth Engine, Planetary Computer
python -m strata_complete pipeline --workers 3           # a stratified sample, up to its release
python -m strata_complete serve                          # viewer: http://localhost:8765 on the host
```

Earth Engine needs a Google Cloud project and a credentials file (RUNBOOK, step 4).
Planetary Computer needs nothing.

## Commands

`python -m strata_complete <command> --help` gives the options of each.

| Command | What it does |
|---|---|
| `env-check` | which record is read, and whether Earth Engine and Planetary Computer answer |
| `fetch`, `verify-tar` | small files of the record, and whole archives, against `SHA256SUMS.txt` |
| `sample` | choose the pairs: a stratified draw with a seed, or `--all` |
| `check` | validate the D_lite tiles of an input folder against their sidecars |
| `compare` | download the D_lite bands again from Earth Engine and compare them |
| `complete` | add the missing Sentinel-2 bands |
| `bands` | quality check of the added bands |
| `prov` | W3C PROV document (PROV-JSON) of every output tile |
| `package` | release tars laid out like D_lite, with manifest, member index and checksums |
| `pipeline` | `sample → complete → bands → prov → package` in one resumable command |
| `report`, `serve` | the provenance report as one HTML file, and the live viewer |
| `notes` | seal the notes written on a release, before publishing it |
| `crosscheck` | study: does Planetary Computer give the same tiles as the release? |
| `reproduce` | demonstration: rebuild a stratified share of the release with Planetary Computer |
| `db` | build `out/studies.sqlite` again from the study files in `log/` |
| `clean` | remove what the tool can make again (lists first) |
| `demo-pack` | ten real pairs, five changed on purpose, to run every step in minutes |
| `schema-export` | write the JSON Schemas of `schema.py` to `schema/` |

## Layout

```
strata-complete/
├── src/strata_complete/
│   ├── cli.py             the commands
│   ├── pipeline.py        the whole chain, resumable
│   ├── config.py          every setting (record, bands, paths, network)
│   ├── schema.py          the file formats, source of the JSON Schemas
│   ├── complete.py        spectral completion of the S2 tiles
│   ├── provenance.py      W3C PROV documents
│   ├── package.py         release tars
│   ├── notes.py           notes on a release (W3C Web Annotation)
│   ├── clean.py           removes what can be made again
│   ├── dlite/             the D_lite record, choice of pairs, tile checks, demo pack
│   ├── providers/         one module per provider (gee, pc), same contract
│   ├── verify/            compare, bands, crosscheck, reproduce, study database
│   ├── report/            report page, live server, tile sources, template
│   └── runtime/           network pauses, progress, logs, run context, tars
├── tests/                 pytest: synthetic record, fake providers, no network
├── schema/                JSON Schemas, generated from schema.py
├── docker/Dockerfile      the environment (no source copied in)
├── docker-compose.yml     project at /workspace, console open
├── .devcontainer/, .vscode/
├── pyproject.toml         pinned dependencies and ruff settings
├── requirements.txt       lock, every package pinned with hashes
├── .env.example           host folders, container name, viewer port
├── gee.env.example        GEE_PROJECT_ID
├── nexus.env.example      optional PyPI proxy
├── RUNBOOK.md
├── CITATION.cff
└── LICENSE
```

Working folders, created on use and never versioned:

| Folder | Content |
|---|---|
| `data/` | small record files read from Zenodo (`data/zenodo/23837956/`); pieces of a tile while it is made (`data/pieces/`); provider copies kept with `--keep` |
| `in/` | the chosen pairs, one folder per choice (`sample_n1_seed22/`, `all/`): `lite_manifest.csv` and `selection.json`, no tiles; `demo-pack/`, the demonstration record |
| `log/` | the text log and JSON report of every run |
| `out/` | `out/strata-s12-sc/` (output tiles, sidecars, PROV documents, `report.html`, `review.json`), `out/release_<input>/` (a release), `out/reproduce_*/` (a demonstration), `out/studies.sqlite` |

`out/` can live in another folder (`SC_OUT_HOST_DIR`), and the outputs of another project can be
mounted read-only at `/ro` (`SC_RO_HOST_DIR`); RUNBOOK, step 1.

## Input: the D_lite record

The input is D_lite 2.0.0 (all versions: concept DOI
[10.5281/zenodo.22646276](https://doi.org/10.5281/zenodo.22646276)). It is read in one
of two ways, with the same checks (`dlite/record.py`):

- **Zenodo** (default). The small files are downloaded to `data/zenodo/23837956/`;
  each tile is fetched with an HTTP Range request on its archive.
- **A local copy of the record** (`SC_RECORD_DIR`): a folder with the files of the
  record under their record names. `docker-compose.yml` mounts it read-only at
  `/data/dlite_v2` (RUNBOOK, step 2). Nothing is ever written there.

Either way:

- **The record is pinned** by the sha256 of its `SHA256SUMS.txt`
  (`RECORD_SHA256SUMS_SHA256` in `config.py`). That file holds the sha256 of every
  other file, so the manifest and `member_index.csv` are checked before use. A new
  version of the record needs a new pin, set on purpose.
- **Tiles are read one by one, by byte range.** `member_index.csv` gives the archive,
  offset, size and sha256 of every file. Each tile and sidecar is checked against its
  sha256 before it is used. Archives are never unpacked or copied, so the input needs
  no extra disk.
- **Only the eight class archives are read.** `strata-s12_lite_rejected.tar` (pairs
  removed by the visual review of D_lite) is never used.

## File formats

`src/strata_complete/schema.py` is the single definition of the formats the tool reads
and writes; every field carries its meaning. The JSON Schemas in `schema/` are exported
from it (`schema-export`) and never edited by hand, so a sidecar can be validated in any
language without the tool.

- `dlite_sidecar`: the sidecar of a D_lite 2.0.0 tile. Validation is strict: an unknown
  or missing field is an error, and so is a field of the other sensor.
- `tiff_info`: what is read from the GeoTIFF itself.
- `sc_sidecar`: the sidecar of a STRATA-S12-SC tile.

`check <input folder>` reads both tiles of every pair, validates the sidecar,
recomputes the pixel checksum (xxHash64 of the array, in band order, as float32) and
reports every field where sidecar and TIFF disagree. `complete` runs the same checks
on every tile it reads.

## Providers

Each provider is a module in `providers/` with one contract: given a scene, a list of
bands and the **exact grid of the D_lite tile** (CRS, affine transform, width and height
read from its GeoTIFF), return those bands on that grid as float32. The checks against
D_lite are common to all providers.

**Google Earth Engine** (`providers/gee.py`) is the source of D_lite. The request uses
the scene id of the sidecar, `.toFloat()`, the default nearest-neighbour resampling,
and fixes the grid with `crs`, `crs_transform` and two-element `dimensions`, with no
`region`. Only `COPERNICUS/S2_SR`, the collection D_lite was built from, is used:
`complete` refuses a scene of any other collection (`GEE_S2_COLLECTION` in
`config.py`), because another collection need not hold the same numbers. Earth Engine
needs `GEE_PROJECT_ID` in `gee.env` and the credentials file written by
`earthengine authenticate`, kept outside the project (RUNBOOK, step 4).

**Microsoft Planetary Computer** (`providers/pc.py`), collection `sentinel-2-l2a`: one
Cloud-Optimised GeoTIFF per band, read through the public STAC API with a short-lived
SAS token (no account). The scene is found by the ESA product name: the item id is the
product name without its `_Nxxxx` baseline field, and an item is accepted only if its
`s2:product_uri` is exactly `PRODUCT_ID.SAFE`, so a reprocessed product is never taken
for the same scene. Each band is read through a `WarpedVRT` onto the tile grid with
nearest-neighbour resampling. The assets are uint16 with nodata 0 and still contain
`BOA_ADD_OFFSET`, as in Earth Engine; nodata becomes −inf, as in Earth Engine float
downloads, so a tile looks the same whichever provider served it.

## Spectral completion (`complete`)

For the Sentinel-2 tile of every pair of an input folder, `complete` writes a GeoTIFF
with the 12 L2A bands in order (B1, B2, B3, B4, B5, B6, B7, B8, B8A, B9, B11, B12) on
the grid of the D_lite tile, and its sidecar.

- **Reference check first.** The request group that holds the D_lite bands (`s2_10m`:
  B2, B3, B4, B8) is downloaded first, and its B4/B3/B2 compared with D_lite. Only if
  they are identical are the other bands added; B4/B3/B2 themselves are always copied
  from D_lite.
- **Verdicts.** `COMPLETE`: reference identical and every band obtained.
  `PARTIAL`: reference identical, some band failed (written as nodata and marked
  `failed`, with its error and attempts). `MISALIGNED`: the reference differs; nothing
  is added, and the output is the D_lite tile as published, with the bands not added
  listed in `not_added`. `UNAVAILABLE`: the reference did not come back, so nothing
  could be verified. `MISALIGNED` is final; `PARTIAL` and `UNAVAILABLE` are tried again
  on the next run.
- **Requests.** Bands are requested in groups by native resolution
  (`S2_REQUEST_GROUPS`), each with retries; a group that keeps failing is retried band
  by band. Every piece must come back on exactly the tile grid.
- **One provider per run.** `complete` uses Earth Engine. `complete --provider pc`
  is a separate run on the pairs Earth Engine could not finish (`in/<input>_pc/`,
  written by `package`): same checks, and its tile is written only if it is
  `COMPLETE`; otherwise it prints `[NOT USED]` and the existing output stays.
- **Metadata.** `radiometry` holds the ESA product name, processing baseline,
  `BOA_ADD_OFFSET` of each band and orbit direction; reflectance =
  (DN + offset) / 10000. `classification` is copied from the D_lite sidecar.
  `paired_sar` holds the orbit direction, relative and absolute orbit, platform and
  mode of the Sentinel-1 scene of the pair. Everything the provider returns about the
  scene is kept in `scene_properties`; `timings_s` records the seconds of every step.
- **Integrity.** The file is written to `.part`, read back and its checksum compared,
  then renamed; the sidecar is validated before it is written, and written last.
- **Encoding.** As D_lite: DEFLATE with the floating-point predictor, level 9,
  256 × 256 blocks, band names in the file, and tags that identify the tile outside
  its archive (`STRATA_DATASET`, `STRATA_SOURCE_DOI`, `STRATA_TILE`, `STRATA_SENSOR`,
  `STRATA_PIXEL_XXHASH64`).
- **Pairs.** The Sentinel-1 tile of every pair is copied byte for byte from the record
  next to its Sentinel-2 output, after its sha256 and pixel checksum are checked. The
  S1 tiles not copied, and why, are listed in the run report.

**Grid spacing is not resolution.** Every added band is delivered on the 10 m grid of
the tile (512 × 512 pixels), but the 20 m bands (B5, B6, B7, B8A, B11, B12) and the 60 m
bands (B1, B9) keep the information of their native pixel: nearest-neighbour assignment
repeats each native pixel over about 2 × 2 or 6 × 6 tile pixels. Each band records its
`native_resolution_m`. One grid keeps every band on the same ground as the D_lite bands
and the Sentinel-1 tile, and nearest neighbour keeps every value an original L2A
measurement.

Sentinel-1 tiles get no new bands: VV and VH are the only polarisations of their IW
dual-polarisation scenes.

## Checking the added bands (`bands`)

`bands` reads every complete tile and summarises each band over all of them
(`log/bands_<UTC>.json`): DN range, reflectance percentiles, share of reflectance below
0 or above 1, non-finite pixels, constant bands, and the share of pixels equal to their
right and lower neighbour. Nearest-neighbour assignment from a native pixel of `r`
metres gives about `(k−1)/k` equal neighbours with `k = r/10`: about 0.5 at 20 m and
0.83 at 60 m. The report prints the expected and the measured value. Pixels the provider
returns masked stay nodata (−inf) and are counted per band; they are never filled.

## Release (`package`) and the whole chain (`pipeline`)

`package <input folder>` builds the release in `out/release_<folder>/`:

| File | Content |
|---|---|
| `strata-s12-sc_<class>.tar` | one per class, with the member paths of the D_lite tar: the S2 tile and sidecar, the S1 tile and sidecar of the pair, and the PROV document of the S2 tile |
| `sc_manifest.csv` | the D_lite manifest row of every released pair, plus verdict, bands, added bands, provider, checksums and PROV document |
| `sc_member_index.csv` | every file inside every tar with byte offset, size and sha256, so one tile can be read alone |
| `sc_excluded.csv` | every pair not released, with every reason that applies |
| `report.html`, `schema/` | the provenance report and the JSON Schemas |
| `release.json` | tool version, D_lite DOI, design of the selection, counts by reason, sha256 of every tar |
| `SHA256SUMS.txt` | sha256 of every file above (`sha256sum -c SHA256SUMS.txt`) |

A pair is released when its D_lite S2 tile matched its checksum, its S1 copy is
identical to the record, its verdict is `COMPLETE` and it is not flagged by a curator.
The checks run in that order, and an excluded pair lists every one it fails. Tars are
reproducible (members sorted, owner `0/0`, mode `0644`, fixed time) and read back after
writing. `--move` removes each class from `out/strata-s12-sc/` once its tar is written
and checked, so the peak disk is the output plus one tar; `package_state.json` lets a
stopped run carry on.

**One provider per release.** The default release holds the tiles made with Earth
Engine and writes `in/<input>_pc/` with the pairs it left out only because their S2
tile was not finished. `pipeline --input in/<input>_pc --provider pc` makes a separate
release of those pairs with Planetary Computer, in marked tars
(`strata-s12-sc_<class>_pc.tar`). The two never mix.

`pipeline` runs `sample → complete → bands → prov → package` with the same options as
the steps (`--per-stratum`, `--seed`, `--all`, `--classes`, `--workers`, `--move`).
Every step resumes; `--until <step>` stops after a step. It stops at a failing
`sample`, `complete`, `prov` or `package`; `bands`, and tiles in error at the end of
`complete`, are warnings. Each run is recorded in `log/pipeline_<UTC>.json`.

## Viewer, review and notes (`report`, `serve`, `notes`)

`report` writes one HTML page that needs no network and no external files, built from
the sidecars and the latest reports in `log/`. It holds the figures of the run and one
short row per pair (with 20,772 pairs: a 17 MB page that opens in about a second).

`serve` shows the same page over HTTP, kept up to date while other commands run, and
makes on request what is too heavy to store: quicklooks of the tiles on screen
(Sentinel-2 true colour and SWIR, Sentinel-1 VV, VH and VV−VH; 256 × 256 JPEG, fixed
stretch, for viewing only) and the full detail of a tile: scene and radiometry,
reference check, every band with its origin, attempts and checksum, the Sentinel-1
tile, timings, and the PROV document drawn as a graph. It opens the output folder or
a release, read from its tars by byte range. Filters by verdict, class, region, season
and review, and a search by tile or product name. Open `http://localhost:8765` on the
host; `docker-compose.yml` publishes the port on the host's loopback only, because the
page has no login.

**Review.** On the output folder a curator can flag a pair and write a note
(`out/strata-s12-sc/review.json`); `package` leaves flagged pairs out and lists them
with the note.

**Notes on a release.** On a release, flags and notes become part of the data, kept
next to the archives and never inside them, in one file: `sc_notes.jsonld`, an
`AnnotationCollection` of the W3C Web Annotation Data Model (JSON-LD, context
`http://www.w3.org/ns/anno.jsonld`).

| Field | Content |
|---|---|
| `target.source` | member path of the S2 tile in the release; it also names the pair |
| `motivation` | `commenting` for a note, `assessing` for a flag |
| `body` | `TextualBody`: the note, or `flagged` / `not flagged` |
| `created`, `creator` | UTC time, and the name in `SC_NOTES_CREATOR` when set |
| `id` | `urn:strata-s12-sc:note:<pair_id>:<n>` |

A change is a new annotation, so the file keeps the whole history; the state of a pair
is its latest note and latest flag. `notes <release>` seals them before publishing: it
checks the file, writes `sc_notes.csv` (one row per annotation), records both in
`release.json` and updates `SHA256SUMS.txt`. A note written after sealing needs the
command again.

## Provenance in W3C PROV (`prov`)

`prov` writes `<tile>.prov.json` next to every output sidecar: the provenance of the
sidecar in PROV-JSON (W3C Member Submission, 24 April 2013) for the PROV data model
(PROV-DM, W3C Recommendation, 30 April 2013). It is rebuilt from the sidecar, so
running `prov` again gives the same document, plus any new curator mark.

| PROV | In a STRATA-S12-SC tile |
|---|---|
| `entity` | the D_lite tile, the S2 scene as served by each provider, the output tile and each of its bands, the S1 tile of the pair and its copy, the pair, the curator mark |
| `activity` | the `complete` run of the tile, the curator review |
| `agent` | the tool (version, base image), the providers, the curator |
| relations | the run `used` the D_lite tile and the scene; the tile `wasGeneratedBy` the run; a band `wasDerivedFrom` its source and `wasAttributedTo` its provider |

Identifiers are relative to `SC_PROV_NAMESPACE` (default `urn:strata-s12-sc:`). The
tests read every document back with the W3C PROV reference library for Python (`prov`).

## Second provider: study and demonstration

Whether another provider gives the same tiles is measured on the release; the release
does not depend on it.

**Study (`crosscheck`).** Reads every released tile (or every complete tile of the
output folder) again from Planetary Computer, all twelve bands, on the same grid, and
compares them pixel by pixel. Two pixels agree when they are equal or both nodata;
nodata is also counted apart. Each tile records the UTM zone of its grid and of its
Sentinel-2 granule, and the summary splits the results by whether the two zones match.
Both providers serve the same ESA product, so the study tests the download chain
(scene, grid, resampling, masking), not ESA's processing. Results per tile go to
`log/crosscheck_pc_<folder>.jsonl` as they finish (the same command resumes), and the
statistics to `log/crosscheck_pc_<UTC>.json`.

**Demonstration (`reproduce`).** `reproduce <release> --fraction 0.10 --seed 77` draws
10 % of every (class, season, region) stratum of the released pairs (rows sorted by
`pair_id`, `max(1, round(0.10 × size))` per stratum, `random.Random(seed).sample`), or
`--n N` pairs from the whole release. Every band of each drawn tile is read again from
Planetary Computer, written with the encoding of the release, and archived next to the
released tile: `out/reproduce_pc_10pct_seed77/`, one tar per class with `gee/` and
`pc/` inside, `reproduce.csv` and `SHA256SUMS.txt`. The result is a count ("X of N
rebuilt identical", by UTM zone); a pair that differs or that the provider cannot give
is part of it, with its reason. Network errors are retried in up to 3 rounds, 5
minutes apart; if any is left, nothing is archived and the command ends with
`[NOT FINISHED]` (exit 3). A finished demonstration is final; `--redo` makes it again.
Every rebuilt pair also counts for the study of the same release, so `crosscheck` does
not read it twice.

**Study database.** The base of each study is its JSON-lines file in `log/`.
`crosscheck` and `reproduce` keep `out/studies.sqlite` in step with it, and `db` builds
it again from the files at any time.

| Table | One row per | Content |
|---|---|---|
| `tiles` | tile and study | product, provider item and baseline, UTM zones, identical, pixels compared and differing, error and whether it is final, command, seconds |
| `bands` | tile, study and band | origin, pixels, agreeing, differing, largest difference, nodata |
| `history` | line of a study file | every attempt in order |
| `manifest` | released pair | its row of `sc_manifest.csv`, to group by stratum |
| `runs` | run | command, times, workers, seed, fraction, counts, and the report's context |
| `studies` | study file | kind, file, release, how far it was imported |

Reports of both commands carry the run context: command, tool and library versions,
GDAL settings of the reads, the pin of the D_lite record and the sha256 of the
release's `SHA256SUMS.txt`.

## Demonstration on ten pairs (`demo-pack`)

`demo-pack` builds `in/demo-pack/`, a small record laid out like D_lite, from ten real
pairs of the D_lite record (read and checked like any other input), so every step of the
tool runs in minutes, for a live demonstration or a first try:

| Pairs | What was done | Expected |
|---|---|---|
| 5 | copied as published | `COMPLETE` with Earth Engine, released |
| 2 | Sentinel-2 sidecar: the scene identifier with `_REMOVED` appended, a scene Earth Engine does not hold; pixels and ESA product name unchanged | `UNAVAILABLE` with Earth Engine, listed in `in/all_pc/`; `COMPLETE` with Planetary Computer, which finds the product by its name |
| 1 | Sentinel-2: 32 × 32 pixels of B4 raised by 100 DN, sidecar unchanged | not released: pixel checksum, and `MISALIGNED` |
| 1 | Sentinel-2: georeference moved one pixel east, pixels unchanged | not released: `MISALIGNED` |
| 1 | Sentinel-1: 32 × 32 pixels of VV raised by 3 dB, sidecar unchanged | not released: the Sentinel-1 tile fails its checksum |

The ten were chosen among pairs that Planetary Computer rebuilt bit for bit, with high
label purity and no quality indicator set (`PAIRS` in `dlite/demopack.py`). The pack is
not a version of D_lite: its `README.md` and `pack.json` say which files were changed
and give the sha256 of each in D_lite and in the pack. `in/demo-pack/demo.env` points
the tool at it (`SC_RECORD_DIR`, `SC_RECORD_SHA256SUMS_SHA256`); nothing else in the tool
changes, so every outcome comes from the data alone.

The pack is not downloaded: `demo-pack` builds it from the D_lite record, read from
wherever the tool reads it, Zenodo by default or a local copy (`SC_RECORD_DIR`), and
checked against its pinned `SHA256SUMS.txt` like any other input. From Zenodo it fetches
the record's small files and, from the class archives, only the bytes of the ten pairs.

`demo-pack` refuses to build if the folder holds outputs of the real record: the
demonstration belongs in a fresh clone, never next to a release. The steps to run on
the pack are in RUNBOOK, *Demonstration on ten pairs*.

## Connection cuts and resume

- **Pause.** When a request fails, the tool checks whether the network is up. If not,
  the run prints `[PAUSED]`, checks again with growing waits and prints `[RESUMED]`
  when it is back. A pause never uses up download attempts. After 6 hours offline
  (`MAX_PAUSE_S`) the command stops with `[STOPPED]` and keeps all finished work.
- **Same byte.** Zenodo downloads and `verify-tar` continue from the byte where they
  stopped, and the sha256 keeps running over the same bytes. A server that does not
  honour the range is an error, never a silent restart.
- **Between runs.** Run the same command again. `complete` skips every tile whose output
  is final and comes from the same D_lite file; `--redo` recomputes all.

## Logs and progress

Everything a command prints, on screen and in `log/<command>_<UTC>.txt`, starts with
its UTC time. The first line says what started (command, arguments, tool version), the
last the exit code and the seconds. Long commands show a progress bar on stderr, which
switches off when stderr is not a terminal. JSON reports go to
`log/<command>_<UTC>.json`.

## Performance

The run is bound by the network. On all 20,772 pairs of D_lite 2.0.0 with 5 workers,
`complete` took 9.1 s per tile overall (31.1 s median per tile), 57.2 hours in total.
Tiles run in parallel with `--workers N`; Earth Engine API calls share one session
under a lock, and the pixel downloads run in parallel. The request groups of one tile
can also run in parallel (`SC_GROUPS_IN_PARALLEL`, default 1); Earth Engine limits
concurrent requests per project, so measure before changing it (RUNBOOK,
Maintenance). Planetary Computer reads four bands of a tile at a time, with GDAL
settings for remote COGs (`GDAL_ENV` in `pc.py`).

Kept for integrity, at a cost: every GeoTIFF and every tar is read back after writing,
and every D_lite file is hashed when it is read.

## Tests

```bash
python -m pytest
```

The suite needs no network, no Earth Engine account and no download. It builds a small
record laid out like the real one (synthetic 64 × 64 tiles with valid sidecars,
`member_index.csv`, `lite_manifest.csv` and a pinned `SHA256SUMS.txt`) and replaces the
providers with fakes.

| File | What is checked |
|---|---|
| `test_complete.py` | verdicts and what each one writes; D_lite bands unchanged; only the reference group asked for a `MISALIGNED` tile; resume and `--redo`; the second provider only for unfinished tiles, with provenance even when Earth Engine does not know the scene; scenes outside `COPERNICUS/S2_SR` refused; S1 copy only when intact; a changed byte caught by its sha256; encoding and tags |
| `test_release.py` | report and viewer (rows, previews and detail on request, releases read from their tars); `package` releases only usable pairs with every reason for the others; reproducible tars and checksums; `--move`; PROV documents parse with `prov`; the second-provider release; `reproduce`; notes on a release; the study database |
| `test_contracts.py` | UTC time on every printed line; strict sidecar validation; the record pin; `sample`; JSON Schemas match the code; the pipeline's stopping rule; `clean`; every command answers `--help` |
| `test_errors.py` | the cause of an error is kept, and access tokens are stripped from it |
| `test_demopack.py` | the demonstration record is read like D_lite; every outcome (complete, finished by Planetary Computer, not released for its pixels, its grid or its Sentinel-1 tile); never built next to a real release |

## Environment

- **Base image pinned by digest** (`docker/Dockerfile`): the tag is for reading, the
  digest decides.
- **Every package pinned and hash-checked.** `pyproject.toml` lists what is used with
  `==`; `requirements.txt`, generated with `uv`, holds every package with its sha256,
  and the build installs with `--require-hashes`.
- **One environment** for development, tests and runs. The image holds no code: the
  project folder is mounted at `/workspace` and imported from `/workspace/src`.
- **LF line endings** everywhere (`.gitattributes`): a `\r` breaks scripts in the
  container.
- **Optional PyPI proxy.** With `nexus.env` (copy `nexus.env.example`) packages are
  installed through a Nexus proxy, at build time and in the container; without it,
  from PyPI. The hashes are checked either way.
- **Build cache.** The package layer depends only on `requirements.txt` and `nexus.env`:
  two copies of the project share it when both have the same two files, or the same
  `requirements.txt` and no `nexus.env`.

### Settings files

Three files, none versioned; each has an `.example` to copy:

| File | Read by | Needed | What it holds |
|---|---|---|---|
| `.env` | Docker Compose, when the container starts | no | where things are on the host: record copy, outputs, Earth Engine credentials, container name and viewer port (`.env.example` lists them with their defaults) |
| `gee.env` | the container | for Earth Engine | `GEE_PROJECT_ID`, the Google Cloud project registered for Earth Engine |
| `nexus.env` | the build and the container | no | a Nexus PyPI proxy (`PIP_INDEX_URL`, `PIP_TRUSTED_HOST`, `UV_DEFAULT_INDEX`) |

Inside the container the tool reads `SC_*` variables; the defaults suit a normal run and
most never need changing:

| Variable | Default | What |
|---|---|---|
| `SC_RECORD_DIR` | empty: Zenodo | a local copy of the record (set through `.env`) |
| `SC_ZENODO_RECORD_ID`, `SC_ZENODO_BASE_URL` | `23837956`, `https://zenodo.org` | the record on Zenodo, read by byte range |
| `SC_RECORD_SHA256SUMS_SHA256` | D_lite 2.0.0 | sha256 of the record's `SHA256SUMS.txt`; a record that does not match is refused |
| `SC_HTTP_TIMEOUT_S`, `SC_MAX_ATTEMPTS_PER_REQUEST`, `SC_RETRY_WAIT_S` | `60`, `3`, `2` | requests to the providers |
| `SC_PAUSE_FIRST_WAIT_S`, `SC_PAUSE_MAX_WAIT_S`, `SC_MAX_PAUSE_S` | `5`, `60`, `21600` | waiting through connection cuts (*Connection cuts and resume*) |
| `SC_SAMPLE_SEED`, `SC_SAMPLE_PER_STRATUM` | `22`, `1` | the `sample` selection |
| `SC_SERVE_PORT`, `SC_SERVE_REFRESH_S` | `8765`, `15` | the viewer's port, set by Compose to `SC_SERVE_HOST_PORT` so it is the same inside and on the host; how often the viewer refreshes |

## Sources

- D_lite 2.0.0: README, CHANGELOG, `SHA256SUMS.txt`, `lite_manifest.csv` of the record,
  DOI 10.5281/zenodo.23837956.
- Earth Engine catalog, `COPERNICUS/S2_SR` and `COPERNICUS/S1_GRD`:
  https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR,
  https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S1_GRD.
- Earth Engine guide, resampling: https://developers.google.com/earth-engine/guides/resample.
- L2A offset, `L2A_BOA = (L2A_DN + BOA_ADD_OFFSET) / QUANTIFICATION_VALUE`: Copernicus,
  "Copernicus Sentinel-2: Major Products Upgrade Upcoming", 29 September 2021.
- Planetary Computer, `sentinel-2-l2a`:
  https://planetarycomputer.microsoft.com/api/stac/v1/collections/sentinel-2-l2a.
- W3C PROV-DM: https://www.w3.org/TR/2013/REC-prov-dm-20130430/; PROV-JSON:
  https://www.w3.org/Submission/2013/SUBM-prov-json-20130424/.
- W3C Web Annotation Data Model: https://www.w3.org/TR/annotation-model/.
- HTTP range requests: RFC 9110. SHA-256: FIPS 180-4. xxHash:
  https://github.com/Cyan4973/xxHash.

## Citation and licence

Cite with `CITATION.cff`, together with the D_lite dataset. Code: MIT (`LICENSE`).
Data: the licence of each Zenodo record.

STRATA-S12-SC 1.0.0 (DOI 10.5281/zenodo.23888967) was built with version 0.1.0.dev0 of
this code, as its sidecars record. Version 1.0.0 adds `demo-pack` and changes nothing in
how tiles are made. Its sidecars follow schema 1.5.0, which adds one optional field to the
1.4.0 of the release: `source.product_id`, the ESA product name by which Planetary Computer
finds a scene, so the provenance of a Planetary Computer tile holds even when Earth Engine no
longer lists that scene.
