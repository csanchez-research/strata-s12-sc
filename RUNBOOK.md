# RUNBOOK

Run the steps in order and check the expected result before going on. Background:
[README.md](README.md).

Every command keeps what it prints in `log/<command>_<UTC>.txt`, each line with its
UTC time; most also write a JSON report (`log/<command>_<UTC>.json`). Keep `log/`: it is
the record of every run.

| What | Where | Command |
|---|---|---|
| Open the console | host | `docker compose run --rm --build --service-ports dev` (step 1) |
| Check the code | container | `python -m pytest` |
| First time only: Earth Engine | host and container | step 4 |
| A sample, up to its release | container | `python -m strata_complete pipeline --workers 3` |
| Every pair | container | [Full run](#full-run-every-pair) |
| See the result | container | `python -m strata_complete serve`, then `http://localhost:8765` on the host |
| Free space | container | `python -m strata_complete clean`, then `clean --yes` |
| Show every step on ten pairs | host and container | [Demonstration](#demonstration-on-ten-pairs) |

## 1. Build and open the console (host)

Next to the project folder there must be `../.credentials/earthengine/` (step 4); the
container does not start without it.

```bash
docker compose run --rm --build --service-ports dev
```

Expected: a `bash` prompt in `/workspace`. `--service-ports` publishes the viewer port
on `127.0.0.1:8765`. Only one console can hold the port: open more consoles with
`docker exec -it strata-complete bash`.

Settings in a `.env` file next to `docker-compose.yml` (all optional; `.env.example` lists
them with their defaults):

| Variable | Default | What |
|---|---|---|
| `SC_EARTHENGINE_DIR` | `../.credentials/earthengine` | folder with the Earth Engine `credentials` file |
| `SC_GEE_ENV` | `gee.env` | file with `GEE_PROJECT_ID` |
| `SC_RECORD_HOST_DIR`, `SC_RECORD_DIR` | Zenodo | a local copy of the record (step 2) |
| `SC_CONTAINER_NAME`, `SC_SERVE_HOST_PORT` | `strata-complete`, `8765` | to run a second copy of the project at the same time |
| `SC_SERVE_BIND` | `127.0.0.1` | host address the viewer port is published on |
| `SC_OUT_HOST_DIR` | `./out` | where `out/` lives, for example on a larger disk |
| `SC_RELEASE_HOST_DIR` | `./out/release_all` | where the release `out/release_all` lives, for example a folder shared with another project |
| `SC_RO_HOST_DIR` | none | outputs of another project, read-only at `/ro` (for example `crosscheck /ro/release_all`) |

## 2. The D_lite record (host, then container)

By default the record is read from Zenodo: small files go to `data/zenodo/23837956/`
and tiles are fetched by byte range. To read a local copy of the record instead (a
folder with its files under their record names), create a `.env` file next to
`docker-compose.yml`:

```
SC_RECORD_HOST_DIR=/path/to/the/record
SC_RECORD_DIR=/data/dlite_v2
```

and open the console again. The folder is mounted read-only.

Check it (container):

```bash
python -m strata_complete env-check
```

Expected first line: `[OK] D_lite 2.0.0: ... SHA256SUMS.txt verified`. `not the
SHA256SUMS.txt of D_lite 2.0.0` means the folder holds another build of the record:
check which one. The pin (`RECORD_SHA256SUMS_SHA256` in `config.py`) is never edited
to make a check pass.

Optional, to hash every archive whole (not needed for a run: every tile is checked
when it is read):

```bash
python -m strata_complete verify-tar all
```

## 3. Choose the pairs and check them (container)

```bash
python -m strata_complete sample
python -m strata_complete check in/sample_n1_seed22
```

Expected: `192 pairs in 192 strata (seed 22) ... 0 files not in the index`, then
`384 tiles checked, 0 failed`. The folder holds `lite_manifest.csv` and
`selection.json`; no tile is copied. Another draw: `sample --seed 7` (its own folder,
`in/sample_n1_seed7/`). `--classes <name>` limits the choice to some classes.

## 4. Earth Engine: first time only

On the host, next to the project folder (not inside it):

```
../.credentials/earthengine/credentials     # written by `earthengine authenticate`
```

In the project folder:

```bash
cp gee.env.example gee.env      # set GEE_PROJECT_ID
docker compose run --rm --build --service-ports dev
```

Check it (container):

```bash
python -m strata_complete env-check
```

Expected:

```
[OK] D_lite 2.0.0: ... SHA256SUMS.txt verified
[OK] GEE: GEE_PROJECT_ID = <your project>
[OK] GEE: credentials file present at /root/.config/earthengine/credentials
[OK] GEE: Earth Engine reachable
```

## 5. Compare the published bands with Earth Engine (optional)

```bash
python -m strata_complete compare in/sample_n1_seed22
```

Expected: one line per tile and one per published band (B4/B3/B2, VV/VH) with
`equal N/262144`. Only those bands are downloaded; each copy is deleted once compared
(`--keep` keeps them in `data/gee/`).

## 6. Complete a few tiles (container)

```bash
python -m strata_complete complete in/sample_n1_seed22 --limit 3
```

Expected, one line per S2 tile:

```
[COMPLETE] <tile>.json | baseline <NN.NN> offset [-1000.0] | paired S1 ASCENDING|DESCENDING track <n>
```

then `S1 of the pairs: 3 copied to the output, 0 already there, 0 problems`. Output in
`out/strata-s12-sc/`, with the paths of the record; summary in
`log/complete_gee_<UTC>.json`.

## 7. Complete the sample and check the bands (container)

```bash
python -m strata_complete complete in/sample_n1_seed22 --workers 3
python -m strata_complete bands
```

A `MISALIGNED` tile gets no new bands: its output is the D_lite tile unchanged.
`PARTIAL` and `UNAVAILABLE` are tried again when step 7 runs again. `bands` prints one
line per band with DN range, reflectance percentiles, non-finite pixels and the
expected and measured share of equal neighbours.

Disk for a full run: scale `du -sm out/strata-s12-sc` by 20,772 / 192.

## 8. W3C PROV documents (container)

```bash
python -m strata_complete prov
```

Expected: `[OK] N W3C PROV documents ...`. Run it again after curator marks.

## 9. Report and viewer (container)

```bash
python -m strata_complete report       # out/strata-s12-sc/report.html, works offline
python -m strata_complete serve        # live, http://localhost:8765 on the host
```

In the viewer, *Tiles* → *Show previews* makes the quicklooks of the tiles on screen.
Open a tile for its detail. To flag a pair (output folder): open it, tick the flag,
write why, *Save*; `package` leaves it out. End with *Stop server* or Ctrl+C.

## 10. Release (container)

```bash
python -m strata_complete package in/sample_n1_seed22
cd out/release_sample_n1_seed22 && sha256sum -c SHA256SUMS.txt && cd /workspace
```

Expected: one `[OK] strata-s12-sc_<class>.tar ...` per class, then the released and
excluded pairs by reason; `sha256sum -c` prints `OK` for every file.

## All of it in one command

Steps 3, 7, 8 and 10:

```bash
python -m strata_complete pipeline --workers 3
```

Expected: a `=== pipeline i/n: <step> ===` header per step, then `=== pipeline done ===`.
After any stop, run the same command again: finished work is skipped. `check` and
`crosscheck` are not part of it.

## Full run: every pair

Run a sample first and check its release.

**Disk.** Without `--move` the output and then the tars, so twice the output; with
`--move`, the output plus the largest class tar.

**Time.** Bound by the network. On D_lite 2.0.0 with 5 workers `complete` took 9.1 s
per tile overall, 57.2 hours for 20,772 pairs.

### 1. Complete every pair, up to the release

```bash
python -m strata_complete pipeline --all --workers 5 --move
```

The release goes to `out/release_all/`. Stopping and running the same command again is
safe at any point. Do not move or delete a tar of `out/release_all/` before the run
ends: with `--move` its tiles are no longer in `out/strata-s12-sc/`.

Expected at the end: `[OK] out/release_all: X of 20772 pairs released ...`. Every pair
left out is in `sc_excluded.csv`; `in/all_pc/` lists the ones a second provider could
still finish.

### 2. The pairs Earth Engine could not finish (only if `in/all_pc/` lists any)

```bash
python -m strata_complete pipeline --input in/all_pc --provider pc --workers 3 --move
```

A tile is written only if it comes out `COMPLETE` with B4/B3/B2 identical to D_lite.
Its release is `out/release_all_pc/`, with tars `strata-s12-sc_<class>_pc.tar`.

### 3. Study: does Planetary Computer give the same tiles?

```bash
python -m strata_complete crosscheck out/release_all --workers 2
```

One line per tile (`[IDENTICAL]` or `[DIFFERS ...]`), then a table per band and the
split by UTM zone. It resumes where it stopped; `--limit N` does part of it.

### 4. Demonstration: 10 % of the release rebuilt with Planetary Computer

```bash
python -m strata_complete reproduce out/release_all --fraction 0.10 --seed 77 --workers 2
```

One line per pair: `[IDENTICAL]`, `[DIFFERS ...]` or `[NOT REBUILT] ...: <reason>`, then
`X of N released pairs rebuilt identical`, by UTM zone. Both versions of every pair go
to `out/reproduce_pc_10pct_seed77/`. `[RETRY LATER]` pairs are retried at the end;
`[NOT FINISHED]` (exit 3) means some are left: run the same command again.

Both studies keep `out/studies.sqlite` up to date; `python -m strata_complete db`
builds it again from `log/`.

### 5. Seal the notes of the release, before publishing it

Notes written in `serve out/release_all` (`SC_NOTES_CREATOR="Your Name"` signs them)
are in `out/release_all/sc_notes.jsonld`. Seal them, also when there are none:

```bash
python -m strata_complete notes out/release_all
cd out/release_all && sha256sum -c SHA256SUMS.txt | grep -v ': OK$'; cd /workspace
```

Expected: `[OK] N annotations ...`; the check prints nothing.

## Demonstration on ten pairs

Everything the tool does, on ten real pairs, in a few minutes (README, `demo-pack`).
Use a **fresh clone** of the repository: the demonstration writes to `in/`, `log/` and
`out/`, and `demo-pack` refuses to build where outputs of the real record exist.

1. Host: clone the repository into a new folder and add a `.env` next to
   `docker-compose.yml` with the Earth Engine settings (step 1) and another container
   name and port if the project already runs elsewhere, for example
   `SC_CONTAINER_NAME=sc-demo` and `SC_SERVE_HOST_PORT=8767`. A local copy of D_lite
   (`SC_RECORD_HOST_DIR`, `SC_RECORD_DIR`) makes the next step faster; without it the
   twenty files of the ten pairs are read from Zenodo by byte range.
2. Container: build the pack.

   ```bash
   python -m strata_complete demo-pack
   ```

   Expected: the ten pairs with their role, `[OK] /workspace/in/demo-pack`, and the
   line that points the tool at the pack.
3. Container: run every step on the pack.

   ```bash
   set -a; . in/demo-pack/demo.env; set +a
   python -m strata_complete env-check
   python -m strata_complete pipeline --all --workers 4
   python -m strata_complete pipeline --input in/all_pc --provider pc --workers 4
   python -m strata_complete reproduce out/release_all --workers 4
   python -m strata_complete serve
   ```

   Expected, in order: `env-check` reads the pack as the record; the Earth Engine
   pipeline releases 5 of 10 pairs and lists 2 in `in/all_pc/`; the Planetary Computer
   pipeline releases those 2 in `out/release_all_pc/`; `reproduce` rebuilds the released
   pairs from Planetary Computer, identical; the viewer opens on
   `http://localhost:8765` (or the port set in `.env`). `out/release_all/sc_excluded.csv`
   gives the reason for each of the other 5.
4. Start again from zero (after Ctrl+C on the viewer): remove `log/`, `out/` and
   everything in `in/` except `in/demo-pack/`, then repeat step 3.

## Connection cuts and interruptions

- **Connection lost:** nothing to do. The run prints `[PAUSED]`, waits and prints
  `[RESUMED]`. After 6 hours offline it stops with `[STOPPED]`.
- **Run interrupted** (Ctrl+C, `[STOPPED]`, machine off): run the same command again.
  `clean` lists the pieces and `.part` files a cut leaves behind.

## If a step fails

| Message | Do |
|---|---|
| `[STOPPED] no network for ...` | Check the connection, then run the same command again. |
| `server did not resume at byte ...` / `no byte range from ...` | Zenodo did not honour the range request: run the same command again. |
| `not the SHA256SUMS.txt of D_lite 2.0.0` | The record folder holds another build of the record. Never edit the pin. |
| `SHA256SUMS.txt not found: SC_RECORD_DIR must be the folder of the record` | The mounted folder is empty: check `SC_RECORD_HOST_DIR` in `.env` (step 2). |
| `sha256 ... != ...` | That byte range is not what the record lists. Run `verify-tar <tar>`; if it fails, the local copy is damaged: copy it again. |
| `N files not in the index` (step 3) | A pair of the manifest has no file in `member_index.csv`: report it with `selection.json`. |
| `THESE PACKAGES DO NOT MATCH THE HASHES` | Build again without `nexus.env`. Never edit a hash. |
| `GEE_PROJECT_ID is not set` | Step 4: create `gee.env`, start the container again. |
| Earth Engine asks to authenticate, or `invalid_grant` | The credentials file is missing or expired: run `earthengine authenticate` again (step 4). |
| `pixel checksum does not match its sidecar` | The published tile does not match its own checksum: the pair is excluded and listed. Report it with the tile name. |
| `ValidationError ... for DliteSidecar` | The sidecar is not a D_lite 2.0.0 sidecar. Do not edit it; report it with the tile name. |
| `reference UNAVAILABLE` | The provider returned nothing for the D_lite bands: check `env-check`, then run step 7 again. |
| `MISALIGNED (reference differs)` | Nothing was added; the output is the D_lite tile unchanged and is not released. |
| `PARTIAL (failed: ...)` | Run step 7 again later. If the same band keeps failing, report the `error` field of the sidecar. |
| `no Planetary Computer item` or `is another product` | Planetary Computer does not have that exact ESA product. Not an error of the tile. |
| `[NOT USED] ...: pc did not give a complete tile` | Planetary Computer could not finish that tile either; nothing was written. |
| `serve`: page does not open | The console was opened without `--service-ports`, or port 8765 is taken (`SC_SERVE_HOST_PORT=8766` in `.env`). From another computer: `ssh -L 8765:127.0.0.1:8765 user@host`. |
| `package`: `lite_manifest.csv not found` | Pass a folder made by `sample`. |
| `package`: `written and moved by an earlier run, now missing or changed` | A tar of a `--move` run was deleted or changed, and its tiles are only in it. Put it back. |
| `package`: `belongs to another input or record` | Pass `--out` with a new folder. |
| `sha256sum -c`: `FAILED` | A release file changed after it was written: run `package` again. |
| `crosscheck`: HTTP 403 or SSL error | The network blocks Planetary Computer: `env-check` shows it. |
| `ModuleNotFoundError` | You are outside the container: step 1. |

## Maintenance

Update a dependency (edit the `==` pin in `pyproject.toml`, then in the container):

```bash
uv pip compile pyproject.toml --python-version 3.13 --python-platform x86_64-manylinux_2_28 --generate-hashes --no-header -o requirements.txt
```

Put back the comment header of `requirements.txt`, rebuild (step 1) and run the tests.

Add a package without moving the pins already in the lock, and only in a release at
least two weeks old:

```bash
CUTOFF=$(date -u -d '14 days ago' +%F); TODAY=$(date -u +%F)
KEEP=$(grep -oE '^[a-z0-9_.-]+==' requirements.txt | tr -d = | sed "s/.*/--exclude-newer-package &=$TODAY/" | tr '\n' ' ')
uv pip compile pyproject.toml --python-version 3.13 --python-platform x86_64-manylinux_2_28 \
  --generate-hashes --no-header --exclude-newer "$CUTOFF" $KEEP -o requirements.txt
```

Only the new package and its dependencies may appear; no existing pin may change.

Update the base image (host): `docker pull python:3.13-slim`, read the digest with
`docker images --digests python`, replace it on the `FROM` line of `docker/Dockerfile`
and rebuild.

After editing `schema.py`: `python -m strata_complete schema-export`.

Request groups in parallel, before changing the default:

```bash
python -m strata_complete complete in/sample_n1_seed22 --limit 20 --redo --workers 3
SC_GROUPS_IN_PARALLEL=3 python -m strata_complete complete in/sample_n1_seed22 --limit 20 --redo --workers 3
```

Compare `seconds_per_tile` in the last two `log/complete_gee_*.json`, and check that no
tile came out `PARTIAL`.

Lint and test:

```bash
ruff check . && ruff format --check . && python -m pytest
```
