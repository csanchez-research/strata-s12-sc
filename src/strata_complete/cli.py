"""Command line: ``python -m strata_complete <command>``; RUNBOOK.md has the steps."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from strata_complete import clean, complete, config, notes, package, pipeline, provenance, schema
from strata_complete.dlite import checks, demopack, sample
from strata_complete.dlite.record import Record, RecordError
from strata_complete.providers import gee, pc
from strata_complete.report import page, serve, source
from strata_complete.runtime import network, runlog
from strata_complete.runtime.progress import say
from strata_complete.verify import bands, compare, crosscheck, reproduce, studydb

if TYPE_CHECKING:
    from strata_complete.schema import ScSidecar


def _fail(message: str) -> int:
    print(f"[FAIL] {message}", file=sys.stderr)
    return 1


def _cmd_fetch(names: list[str]) -> int:
    record = Record.from_config()
    try:
        for name in names:
            print(f"[OK] {name} -> {record.file(name)} (sha256 verified, {record.describe()})")
    except RecordError as exc:
        return _fail(str(exc))
    return 0


def _cmd_verify_tar(names: list[str]) -> int:
    """Whole archives against SHA256SUMS.txt of the record; nothing is extracted."""
    record = Record.from_config()
    try:
        tars = [n for n in record.names() if n.endswith(".tar")] if names == ["all"] else names
    except RecordError as exc:
        return _fail(str(exc))
    failed = 0
    for name in tars:
        try:
            sha, n = record.verify_archive(name)
        except RecordError as exc:
            failed += _fail(str(exc))
            continue
        print(f"[OK] {name} ({record.describe()}): {n} bytes, sha256 {sha} matches")
    return 1 if failed else 0


def _cmd_demo_pack() -> int:
    """The demonstration record (``dlite/demopack.py``) in in/demo-pack/."""
    record = Record.from_config()
    try:
        report = demopack.build(record)
    except (RecordError, ValueError) as exc:
        return _fail(str(exc))
    out = config.IN_DIR / demopack.FOLDER_NAME
    for p in report["pairs"]:  # type: ignore[union-attr]
        print(f"  {p['pair_id']}  {p['class_name']:23} {p['role']:16} {p['change']}")
    print(
        f"[OK] {out}: {len(report['pairs'])} pairs from {record.describe()}; "  # type: ignore[arg-type]
        f"SHA256SUMS.txt {report['sha256sums_sha256']}\n"
        f"Read it as the record: set -a; . {out / demopack.ENV_NAME}; set +a"
    )
    return 0


def _no_selection(in_dir: Path) -> str | None:
    if not (in_dir / sample.MANIFEST).is_file():
        return f"{in_dir / sample.MANIFEST} not found: run sample first"
    return None


def _cmd_check(in_dir: Path) -> int:
    if msg := _no_selection(in_dir):
        return _fail(msg)
    record = Record.from_config()
    try:
        results, report = checks.check_selection(in_dir, record)
    except RecordError as exc:
        return _fail(str(exc))
    failed = 0
    for r in results:
        problem = checks.problem(r)
        failed += problem is not None
        if problem:
            print(f"[FAIL] {r.sidecar}: {problem}")
        for finding in r.findings:
            print(f"       note: {r.sidecar}: {finding}")
    print(f"{len(results)} tiles checked, {failed} failed. Report: {report}")
    return 1 if failed else 0


def _cmd_compare(in_dir: Path, *, keep: bool) -> int:
    if msg := _no_selection(in_dir):
        return _fail(msg)
    try:
        results, report = compare.compare_selection(in_dir, Record.from_config(), keep=keep)
    except RecordError as exc:
        return _fail(str(exc))
    failed = 0
    for r in results:
        if r.error:
            failed += 1
            print(f"[FAIL] {r.filename}: {r.error}")
            continue
        exact = all(b.n_equal == b.n_pixels for b in r.bands)
        print(f"[{'IDENTICAL' if exact else 'DIFFERS'}] {r.filename}")
        for b in r.bands:
            print(
                f"       {b.band}: equal {b.n_equal}/{b.n_pixels} max|diff| {b.max_abs_diff:.6g} "
                f"non-finite GEE {b.n_nonfinite_provider} D_lite {b.n_nonfinite_dlite}"
            )
    print(f"{len(results)} tiles, {failed} errors. Report: {report}")
    return 1 if failed else 0


_VERDICT_LABEL = {
    "MISALIGNED": "MISALIGNED (reference differs)",
    "UNAVAILABLE": "UNAVAILABLE (reference not returned)",
}


NOT_USED = "NotUsedError: "


def _complete_line(name: str, sc: ScSidecar | None, err: str | None) -> None:
    if sc is None:
        if err and err.startswith(NOT_USED):
            say(f"[NOT USED] {name}: {err.removeprefix(NOT_USED)}")
        else:
            say(f"[ERROR] {name}: {err}")
        return
    state = _VERDICT_LABEL.get(sc.verdict, sc.verdict)
    if sc.verdict == "PARTIAL":
        state += f" (failed: {', '.join(b.name for b in sc.bands if b.status == 'failed')})"
    if sc.provider and sc.provider != "gee":
        state += f" from {sc.provider}"
    sar, rad = sc.paired_sar, sc.radiometry
    orbit = f"{sar.orbit_pass} track {sar.relative_orbit_number}" if sar.orbit_pass else "?"
    offsets = sorted(set(rad.boa_add_offset.values())) or "?"
    say(
        f"[{state}] {name} | baseline {rad.processing_baseline} offset {offsets} "
        f"| paired S1 {orbit}"
    )


def _cmd_complete(
    in_dir: Path, workers: int, limit: int | None, provider: str = "gee", *, redo: bool
) -> int:
    if msg := _no_selection(in_dir):
        return _fail(msg)
    started = time.perf_counter()
    try:
        results, n_skipped = complete.complete_folder(
            in_dir,
            Record.from_config(),
            on_result=_complete_line,
            workers=workers,
            limit=limit,
            redo=redo,
            provider=provider,
        )
    except (gee.GeeError, RecordError) as exc:
        return _fail(str(exc))
    except network.NetworkDownError as exc:
        print(f"[STOPPED] {exc}", file=sys.stderr)
        return 2
    if not results:
        if n_skipped:
            print(f"Nothing to do: {n_skipped} tiles already done (--redo to recompute).")
            return 0
        return _fail(f"no pairs in {in_dir / sample.MANIFEST}")
    done = [sc for _, sc, _ in results if sc is not None]
    verdicts = {v: sum(sc.verdict == v for sc in done) for v in ("PARTIAL", "MISALIGNED")}
    not_used = sum(1 for _, sc, e in results if sc is None and (e or "").startswith(NOT_USED))
    errors = len(results) - len(done) - not_used
    wall = time.perf_counter() - started
    latency = (
        f", {sum(sc.timings_s['total'] for sc in done) / len(done):.1f} s per tile" if done else ""
    )
    print(
        f"{len(results)} tiles, {verdicts['PARTIAL']} partial, {verdicts['MISALIGNED']} "
        f"misaligned, {errors} errors"
        + (f", {not_used} not used (left as they were)" if provider != "gee" else "")
        + f", {n_skipped} skipped (already done) | provider {provider}, "
        f"{workers} worker(s): {wall / len(results):.1f} s/tile "
        f"overall{latency}. Output: {config.SC_OUT_DIR}"
    )
    return pipeline.SOME_FAILED if errors or not_used else 0


def _cmd_bands(folder: Path) -> int:
    summaries, meta, report = bands.check_folder(folder)
    if not summaries:
        return _fail(f"no verified S2 tiles under {folder}")
    print(
        f"{meta['n_tiles_checked']} verified tiles checked, "
        f"{meta['n_tiles_skipped_not_verified']} skipped (not complete or reference differs)"
    )
    print(
        f"{'band':5} {'res':>4} {'DN min':>8} {'DN max':>8} {'refl p1':>8} {'p50':>7} "
        f"{'p99':>7} {'<0':>8} {'>1':>8} {'nonfin':>6} {'eq exp':>6} {'eq →':>6} {'eq ↓':>6}"
    )
    for b in summaries:
        print(
            f"{b.band:5} {b.native_resolution_m or 0:>4} {b.dn_min:>8.0f} {b.dn_max:>8.0f} "
            f"{b.reflectance_p1_median:>8.4f} {b.reflectance_p50_median:>7.4f} "
            f"{b.reflectance_p99_median:>7.4f} {b.share_below_0:>8.5f} {b.share_above_1:>8.5f} "
            f"{b.n_nonfinite:>6} {b.equal_neighbour_expected:>6} {b.equal_neighbour_right:>6} "
            f"{b.equal_neighbour_down:>6}"
        )
    for flag in meta["flags"]:
        print(f"       note: {flag}")
    print(f"Report: {report}")
    return 0


def _cross_line(r: crosscheck.TileCross) -> None:
    if r.error:
        say(f"[ERROR] {r.filename}: {r.error}")
        return
    differ = [b.band for b in r.bands if not b.identical]
    state = "IDENTICAL" if not differ else f"DIFFERS ({', '.join(differ)})"
    say(
        f"[{state}] {r.filename} | {r.product_id} (PC baseline {r.pc_processing_baseline}) "
        f"| {len(r.bands) - len(differ)}/{len(r.bands)} bands | {r.seconds} s"
    )


def _cmd_crosscheck(
    folder: Path, workers: int, limit: int | None, *, keep: bool = False, redo: bool = False
) -> int:
    if not folder.is_dir() or not source.open_source(folder).sidecars():
        return _fail(f"no output sidecars under {folder} (an output folder or a release folder)")
    results, rows, meta, report = crosscheck.cross_folder(
        folder, workers, limit, _cross_line, keep=keep, redo=redo
    )
    if not results:
        return _fail(f"no verified S2 tiles under {folder}")
    print(
        f"{meta['n_tiles_this_run']} tiles checked in this run; study so far "
        f"({meta['progress_file']}): "
        f"{meta['n_tiles_checked']} verified tiles, {meta['n_tiles_identical']} identical in "
        f"every band, {meta['n_tiles_error']} errors, {meta['n_tiles_skipped_not_verified']} "
        f"skipped (not verified), {meta['n_tiles_skipped_completed_by_pc']} skipped (completed "
        f"by PC) | {workers} worker(s): {meta['s_per_tile_overall']} s/tile overall"
    )
    print(
        f"{'band':5} {'origin':8} {'tiles':>5} {'ident':>5} {'agree px':>12} {'of px':>12} "
        f"{'max|d|':>7} {'nd both':>8} {'nd tile':>8} {'nd PC':>8}"
    )
    for b in rows:
        diff = "-" if b["max_abs_diff"] is None else f"{b['max_abs_diff']:g}"
        print(
            f"{b['band']:5} {b['origin']:8} {b['n_tiles']:>5} {b['n_tiles_identical']:>5} "
            f"{b['n_agree']:>12} {b['n_pixels']:>12} {diff:>7} {b['nodata_both']:>8} "
            f"{b['nodata_tile_only']:>8} {b['nodata_pc_only']:>8}"
        )
    for zone, counts in meta["by_zone"].items():
        print(
            f"       {zone.replace('_', ' ')} (tile grid vs S2 granule UTM zone): "
            f"{counts['identical']} identical, {counts['differs']} differ"
        )
    print(f"Report: {report}")
    return 1 if meta["n_tiles_error"] else 0


def _cmd_report(folder: Path, out: Path) -> int:
    if not folder.is_dir() or not source.open_source(folder).sidecars():
        return _fail(f"no output sidecars under {folder}")
    if source.is_release(folder) and out.resolve().is_relative_to(folder.resolve()):
        return _fail(f"{folder} is a release (its SHA256SUMS.txt is final): pass --out elsewhere")
    path, counts = page.build(folder, out)
    summary = ", ".join(f"{n} {k.lower()}" for k, n in sorted(counts.items()))
    print(f"[OK] {path} ({path.stat().st_size / 1e6:.1f} MB): {summary}. Opens in any browser.")
    return 0


def _cmd_prov(folder: Path) -> int:
    if not complete.output_sidecars(folder):
        return _fail(f"no output sidecars under {folder}")
    n, report = provenance.write_folder(folder)
    print(f"[OK] {n} W3C PROV documents (PROV-JSON, <tile>.prov.json) in {folder}. Log: {report}")
    return 0


def _cmd_serve(folder: Path, port: int, refresh: float) -> int:
    if not folder.is_dir():
        return _fail(f"{folder} does not exist: run complete first")
    if not source.open_source(folder).sidecars():
        return _fail(f"no output sidecars under {folder} (an output folder or a release folder)")
    serve.serve(folder, port, refresh)
    return 0


def _cmd_package(
    in_dir: Path, out: Path | None, *, move: bool = False, provider: str = "gee"
) -> int:
    if msg := _no_selection(in_dir):
        return _fail(msg)
    release = out or config.OUT_DIR / f"release_{in_dir.name}"
    try:
        meta = package.build(in_dir, release, Record.from_config(), move=move, provider=provider)
    except (RecordError, RuntimeError) as exc:
        return _fail(str(exc))
    reasons = ", ".join(f"{n} {k}" for k, n in sorted(meta["excluded_by_reason"].items()))
    print(
        f"[OK] {release}: {meta['n_pairs_released']} of {meta['n_pairs_input']} pairs released "
        f"in {len(meta['archives'])} tars; {meta['n_pairs_excluded']} excluded"
        + (f" ({reasons})" if reasons else "")
        + ". See sc_manifest.csv, sc_excluded.csv, release.json, SHA256SUMS.txt."
    )
    if "unfinished" in meta:
        u = meta["unfinished"]
        print(
            f"       {u['n_pairs']} pairs the provider could not finish are listed in "
            f"{config.IN_DIR / str(u['folder'])} (input for a second provider, RUNBOOK)."
        )
    return 0


def _reproduce_line(r: reproduce.TileReproduced) -> None:
    zone = f"tile zone {r.tile_utm_zone}, granule zone {r.granule_utm_zone}"
    if r.identical:
        say(f"[IDENTICAL] {r.s2_file} | {len(r.bands_identical)} bands | {zone} | {r.seconds} s")
    elif r.rebuilt:
        say(
            f"[DIFFERS ({', '.join(r.bands_differ)})] {r.s2_file} | {r.pixels_differ} of "
            f"{r.pixels} band pixels differ | {zone} | {r.seconds} s"
        )
    elif r.error_final:
        say(f"[NOT REBUILT] {r.s2_file}: {r.error} | {zone}")
    else:
        say(f"[RETRY LATER] {r.s2_file}: {r.error} | {zone}")


def _cmd_reproduce(  # noqa: PLR0913 - the settings of the demonstration
    release: Path,
    provider: str,
    n: int | None,
    fraction: float | None,
    seed: int,
    workers: int,
    *,
    redo: bool,
) -> int:
    """Pairs that differ or that the provider cannot give are results: exit 0.

    Exit 3 (``pipeline.SOME_FAILED``) when network errors are left after the retries.
    """
    if not source.is_release(release):
        return _fail(f"{release} is not a release folder (no {source.MEMBER_INDEX})")
    try:
        _, meta, report = reproduce.run(
            release,
            provider,
            n,
            seed,
            _reproduce_line,
            fraction=fraction,
            workers=workers,
            redo=redo,
        )
    except (RecordError, ValueError) as exc:
        return _fail(str(exc))
    if not meta["n_pairs"]:
        return _fail(f"no pair released by GEE in {release}")
    same, other = meta["same_zone"], meta["other_zone"]
    if not meta["finished"]:
        print(
            f"[NOT FINISHED] {meta['n_to_retry']} of {meta['n_pairs']} pairs failed because of "
            f"the network or the server; nothing archived yet. Run the same command again: "
            f"the {meta['n_pairs'] - meta['n_to_retry']} done are kept, those are tried again. "
            f"Report so far: {report}"
        )
        return pipeline.SOME_FAILED
    size = sum(int(a["bytes"]) for a in meta["archives"]) / 1e9  # type: ignore[union-attr]
    print(
        f"{meta['n_identical']} of {meta['n_pairs']} released pairs rebuilt identical with "
        f"{provider} (seed {seed}); {meta['n_differ']} differ, {meta['n_error']} not rebuilt. "
        f"Tile grid in the granule's UTM zone: {same['identical']} of {same['n']} identical; "
        f"other zone: {other['identical']} of {other['n']}. Both versions: {meta['folder']} "
        f"({len(meta['archives'])} tars, {size:.1f} GB). Report: {report}"
    )
    return 0


def _cmd_notes(release: Path) -> int:
    """Close the notes of a release for publication (notes.py)."""
    if not source.is_release(release):
        return _fail(f"{release} is not a release folder (no {source.MEMBER_INDEX})")
    try:
        meta = notes.seal(release)
    except notes.NotesError as exc:
        return _fail(f"notes not sealed: {exc}")
    print(
        f"[OK] {meta['n_annotations']} annotations ({meta['n_pairs_with_notes']} pairs with a "
        f"note, {meta['n_pairs_flagged']} flagged) in {release / notes.NOTES}, "
        f"table {notes.NOTES_CSV}; recorded in release.json and {config.SHA256SUMS_NAME}"
    )
    return 0


def _cmd_db() -> int:
    """Build out/studies.sqlite again from the study files in log/ (they are the base)."""
    counts = studydb.rebuild()
    for name, n in counts.items():
        print(f"[OK] {name}: {n} lines imported")
    print(f"Database: {studydb.db_path()} ({len(counts)} study files)")
    return 0


def _cmd_sample(
    per_stratum: int | None, seed: int | None, classes: list[str] | None, *, all_pairs: bool
) -> int:
    record = Record.from_config()
    try:
        out_dir, sel = sample.build(record, per_stratum, seed, classes, all_pairs=all_pairs)
    except network.NetworkDownError as exc:
        print(f"[STOPPED] {exc}", file=sys.stderr)
        return 2
    except RecordError as exc:
        return _fail(str(exc))
    missing = sel["missing_in_record"]
    design = "every pair" if all_pairs else f"seed {sel['seed']}"
    print(
        f"{sel['n_pairs']} pairs in {sel['n_strata']} strata ({design}) from "
        f"{record.describe()}, manifest sha256 verified; {len(missing)} files not in the "
        f"index. Output: {out_dir}"
    )
    for name in missing:
        print(f"       not in {config.MEMBER_INDEX_NAME}: {name}")
    return 1 if missing else 0


def _cmd_pipeline(args: argparse.Namespace) -> int:
    if args.input:
        in_dir = args.input  # an input folder already made (e.g. in/all_pc): no sample step
        if msg := _no_selection(in_dir):
            return _fail(msg)
    else:
        in_dir = config.IN_DIR / sample.folder_name(
            args.per_stratum, args.seed, args.classes, all_pairs=args.all
        )
    steps = {
        "sample": lambda: _cmd_sample(
            args.per_stratum, args.seed, args.classes, all_pairs=args.all
        ),
        "complete": lambda: _cmd_complete(in_dir, args.workers, None, args.provider, redo=False),
        "bands": lambda: _cmd_bands(config.SC_OUT_DIR),
        "prov": lambda: _cmd_prov(config.SC_OUT_DIR),
        "package": lambda: _cmd_package(in_dir, None, move=args.move, provider=args.provider),
    }
    settings = {
        "all": args.all,
        "per_stratum": None if args.all else args.per_stratum or config.SAMPLE_PER_STRATUM,
        "seed": None if args.all else (config.SAMPLE_SEED if args.seed is None else args.seed),
        "classes": args.classes or "all",
        "workers": args.workers,
        "until": args.until,
        "move": args.move,
        "provider": args.provider,
        "input": str(in_dir),
        "record": Record.from_config().describe(),
    }
    skip = frozenset({"sample"}) if args.input else frozenset()
    return pipeline.run(steps, settings, skip, until=args.until)


def _cmd_env_check() -> int:
    """The record and Earth Engine are needed by ``complete`` (with either provider: the
    scene metadata come from Earth Engine); Planetary Computer by ``crosscheck``,
    ``reproduce`` and ``complete --provider pc``."""
    record = Record.from_config()
    try:
        record.sums()
        print(f"[OK] D_lite {config.DLITE_VERSION}: {record.describe()}, SHA256SUMS.txt verified")
        record_ok = True
    except (RecordError, OSError) as exc:
        print(f"[FAIL] D_lite record: {exc}")
        record_ok = False
    lines = gee.preflight()
    for ok, msg in lines:
        print(f"[{'OK' if ok else 'FAIL'}] GEE: {msg}")
    for ok, msg in pc.preflight():
        print(f"[{'OK' if ok else 'WARN'}] Planetary Computer (crosscheck, --provider pc): {msg}")
    return 0 if record_ok and all(ok for ok, _ in lines) else 1


def _cmd_clean(*, yes: bool) -> int:
    found = clean.targets()
    if not found:
        print("Nothing to clean.")
        return 0
    for path, size in found:
        print(f"{size / 1e6:10.1f} MB  {path}")
    total = sum(size for _, size in found) / 1e6
    if not yes:
        print(f"{total:.1f} MB in {len(found)} items would be removed. Run with --yes to remove.")
        return 0
    clean.remove([p for p, _ in found])
    print(f"[OK] removed {len(found)} items, {total:.1f} MB freed")
    return 0


def _cmd_schema_export(out_dir: Path) -> int:
    for path in schema.export(out_dir):
        print(f"[OK] {path}")
    return 0


MOVE_HELP = "remove each class from out/strata-s12-sc once its tar is written and read back"


def _parser() -> argparse.ArgumentParser:  # noqa: PLR0915 - one statement per option
    parser = argparse.ArgumentParser(prog="strata_complete")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch", help="small files of the record (sha256 verified)")
    p.add_argument("names", nargs="+", help="file names of the record, e.g. README.md")
    p = sub.add_parser("verify-tar", help="whole archives against SHA256SUMS.txt")
    p.add_argument("names", nargs="+", help="tar file names, or 'all'")
    p = sub.add_parser("check", help="validate the D_lite tiles of an input folder")
    p.add_argument("folder", type=Path, help="input folder made by sample")
    p = sub.add_parser("compare", help="download D_lite bands again from GEE and compare")
    p.add_argument("folder", type=Path, help="input folder made by sample")
    p.add_argument("--keep", action="store_true", help="keep the GEE copies in data/gee/")
    p = sub.add_parser("sample", help="choose the pairs: stratified draw, or --all")
    p.add_argument("--per-stratum", type=int, default=None, help="pairs per stratum")
    p.add_argument("--seed", type=int, default=None, help="random seed (recorded)")
    p.add_argument("--all", action="store_true", help="every pair of the record (in/all)")
    p.add_argument("--classes", nargs="+", default=None, help="only these classes")
    p = sub.add_parser("complete", help="add the missing S2 bands (STRATA-S12-SC)")
    p.add_argument("folder", type=Path, help="input folder made by sample")
    p.add_argument("--workers", type=int, default=1, help="tiles in parallel (threads)")
    p.add_argument("--redo", action="store_true", help="recompute tiles already done")
    p.add_argument("--limit", type=int, default=None, help="only the first N tiles")
    p.add_argument(
        "--provider",
        choices=complete.PROVIDERS,
        default="gee",
        help="gee (default); pc only for the pairs GEE could not finish (in/<input>_pc)",
    )
    p = sub.add_parser("bands", help="quality check of the bands of verified tiles")
    p.add_argument("folder", type=Path, nargs="?", default=config.SC_OUT_DIR)
    p = sub.add_parser("crosscheck", help="study: would Planetary Computer give the same tiles?")
    p.add_argument(
        "folder",
        type=Path,
        nargs="?",
        default=config.SC_OUT_DIR,
        help="output folder (default) or a release folder (out/release_<input>)",
    )
    p.add_argument("--workers", type=int, default=1, help="tiles in parallel (threads)")
    p.add_argument("--limit", type=int, default=None, help="only the next N tiles")
    p.add_argument("--keep", action="store_true", help="keep the PC copies in data/pc/")
    p.add_argument("--redo", action="store_true", help="start the study again from zero")
    p = sub.add_parser("prov", help="W3C PROV document (PROV-JSON) of every output tile")
    p.add_argument("folder", type=Path, nargs="?", default=config.SC_OUT_DIR)
    p = sub.add_parser("report", help="provenance report as one HTML file")
    p.add_argument("folder", type=Path, nargs="?", default=config.SC_OUT_DIR)
    p.add_argument("--out", type=Path, default=None, help="default: <folder>/report.html")
    p = sub.add_parser("serve", help="live report: previews on request, curator flags")
    p.add_argument(
        "folder",
        type=Path,
        nargs="?",
        default=config.SC_OUT_DIR,
        help="output folder (default) or a release folder (out/release_<input>)",
    )
    p.add_argument("--port", type=int, default=config.SERVE_PORT)
    p.add_argument("--refresh", type=float, default=config.SERVE_REFRESH_S, help="seconds")
    p = sub.add_parser(
        "reproduce",
        help="demo: rebuild n released pairs with a second provider as it comes, archive both",
    )
    p.add_argument("release", type=Path, help="a release folder (out/release_<input>)")
    p.add_argument("--provider", choices=reproduce.PROVIDERS, default="pc")
    how = p.add_mutually_exclusive_group()
    how.add_argument("--n", type=int, default=None, help="pairs drawn from the whole release")
    how.add_argument(
        "--fraction",
        type=float,
        default=None,
        help="share of every (class, season, region) stratum, e.g. 0.10 (default)",
    )
    p.add_argument("--seed", type=int, default=77, help="random seed of the draw (recorded)")
    p.add_argument("--workers", type=int, default=1, help="pairs at a time")
    p.add_argument("--redo", action="store_true", help="make a finished demonstration again")
    sub.add_parser("db", help="build out/studies.sqlite again from the study files in log/")
    p = sub.add_parser("notes", help="seal the notes of a release (sc_notes.jsonld) to publish")
    p.add_argument("release", type=Path, help="a release folder (out/release_<input>)")
    p = sub.add_parser("package", help="release tars laid out like D_lite")
    p.add_argument("folder", type=Path, help="input folder made by sample")
    p.add_argument("--out", type=Path, default=None, help="default: out/release_<folder>")
    p.add_argument("--move", action="store_true", help=MOVE_HELP)
    p.add_argument(
        "--provider", choices=complete.PROVIDERS, default="gee", help="provider of the release"
    )
    p = sub.add_parser("pipeline", help="D_lite record to STRATA-S12-SC tars, resumable")
    p.add_argument("--per-stratum", type=int, default=None, help="pairs per stratum")
    p.add_argument("--seed", type=int, default=None, help="random seed (recorded)")
    p.add_argument("--all", action="store_true", help="every pair of the record (in/all)")
    p.add_argument("--move", action="store_true", help=MOVE_HELP)
    p.add_argument("--classes", nargs="+", default=None, help="only these classes")
    p.add_argument("--workers", type=int, default=3, help="tiles in parallel (threads)")
    p.add_argument(
        "--input",
        type=Path,
        default=None,
        help="an input folder already made (e.g. in/all_pc): no sample step",
    )
    p.add_argument(
        "--provider",
        choices=complete.PROVIDERS,
        default="gee",
        help="gee; pc for the pairs GEE could not finish (with --input in/<input>_pc)",
    )
    p.add_argument(
        "--until",
        choices=pipeline.ORDER,
        default=None,
        help="stop after this step; the same command without it goes on from there",
    )
    sub.add_parser("env-check", help="check the record, Earth Engine and Planetary Computer")
    sub.add_parser(
        "demo-pack", help="ten real pairs, five changed on purpose, to show every step (in/)"
    )
    p = sub.add_parser("clean", help="remove provider copies and cut-short files (lists first)")
    p.add_argument("--yes", action="store_true", help="remove; without it, only list")
    p = sub.add_parser("schema-export", help="write the JSON Schemas of schema.py")
    p.add_argument("--out", type=Path, default=config.ROOT / "schema")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    commands = {
        "fetch": lambda: _cmd_fetch(args.names),
        "verify-tar": lambda: _cmd_verify_tar(args.names),
        "check": lambda: _cmd_check(args.folder),
        "compare": lambda: _cmd_compare(args.folder, keep=args.keep),
        "sample": lambda: _cmd_sample(
            args.per_stratum, args.seed, args.classes, all_pairs=args.all
        ),
        "complete": lambda: _cmd_complete(
            args.folder, args.workers, args.limit, args.provider, redo=args.redo
        ),
        "bands": lambda: _cmd_bands(args.folder),
        "crosscheck": lambda: _cmd_crosscheck(
            args.folder, args.workers, args.limit, keep=args.keep, redo=args.redo
        ),
        "prov": lambda: _cmd_prov(args.folder),
        "report": lambda: _cmd_report(args.folder, args.out or args.folder / "report.html"),
        "serve": lambda: _cmd_serve(args.folder, args.port, args.refresh),
        "package": lambda: _cmd_package(
            args.folder, args.out, move=args.move, provider=args.provider
        ),
        "db": _cmd_db,
        "notes": lambda: _cmd_notes(args.release),
        "reproduce": lambda: _cmd_reproduce(
            args.release,
            args.provider,
            args.n,
            config.REPRODUCE_FRACTION
            if args.n is None and args.fraction is None
            else args.fraction,
            args.seed,
            args.workers,
            redo=args.redo,
        ),
        "pipeline": lambda: _cmd_pipeline(args),
        "env-check": _cmd_env_check,
        "demo-pack": _cmd_demo_pack,
        "clean": lambda: _cmd_clean(yes=args.yes),
        "schema-export": lambda: _cmd_schema_export(args.out),
    }
    with runlog.session(args.cmd, ["python -m strata_complete", *(argv or sys.argv[1:])]) as run:
        run["code"] = commands[args.cmd]()
    return int(run["code"])  # type: ignore[call-overload]
