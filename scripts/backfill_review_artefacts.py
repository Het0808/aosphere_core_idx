#!/usr/bin/env python3
"""Build the missing viewer.html / inspect.html for documents already extracted to S3.

Why this is needed rather than a re-extraction: run 2026-08-21-02 finished 986 documents, and
123 of them have no viewer. The cause is timing, not content — every one of those 123 finished
between 08-21 14:53 and 20:50, before build_review_artefacts existed in the worker image, and
every document that finished from 23:08 onwards has one. Zero overlap. (It LOOKS like a
page-count cliff — nothing above 166 pages has a viewer — because sharding is longest-first, so
the biggest documents ran first and landed in the pre-feature window.)

Everything those artefacts are built FROM is already in S3: the stage-3 tree, source.pdf,
scorecard.json and validation.json. So this downloads a job, runs the worker's own
build_review_artefacts on it, and uploads just the two HTML files — seconds per document,
against ~20 GPU-minutes each to re-extract.

    # what is missing, and what it would cost
    python scripts/backfill_review_artefacts.py --run 2026-08-21-02 --plan

    # build them (add --limit to do a few first)
    python scripts/backfill_review_artefacts.py --run 2026-08-21-02 --limit 2
    python scripts/backfill_review_artefacts.py --run 2026-08-21-02

    # after the PAGE changes (new tab, new embedded payload) — rebuild docs that have one
    python scripts/backfill_review_artefacts.py --run 2026-08-21-02 --rebuild --limit 2

Only ever WRITES viewer.html and inspect.html: the extraction itself is never touched. By
default it writes only where they are absent; --rebuild overwrites existing ones, which is what
a change to the page itself needs, since a frozen page cannot pick one up on its own.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ARTEFACTS = ("viewer.html", "inspect.html")
# What build_review_artefacts reads. The stage-1/2 directories and the page snapshots are the
# bulk of a job dir and are not needed, so they are not downloaded.
NEEDED = ("source.pdf", "scorecard.json", "scorecard_post_ai.json", "validation.json",
          "corpus_meta.json", "summary_ai_rule.json")
# scorecard_post_ai.json and summary_ai_rule.json are here because the PAGE grew tabs that read
# them, not because build_review_artefacts changed. build_inspect._scorecards embeds Scorecard 2
# only when the post-AI file sits beside the job, so without it a rebuilt page silently shows
# the frozen stage-3 verdict with no switcher to reach the one that actually gated the document
# -- the exact fault the embed was added to fix. summary_ai_rule.json is how
# check_stage4.stage4_dashboard tells a summary-AI job (no stage 4 at all) from a normal one
# that simply has no report; absent, a summary document's Stage 4 tab names the wrong reason.
# Both are best-effort copies, like corpus_meta.json: a job that has neither is not an error.


def _aws(*args: str, profile: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["aws", *args]
    if profile:
        cmd += ["--profile", profile]
    return subprocess.run(cmd, capture_output=True, text=True)


def job_dirs(bucket: str, run_prefix: str, profile: str | None) -> dict[str, set[str]]:
    """<product>/<label> -> the artefact filenames it already has.

    One delimiter listing per level, never a recursive walk of the run: a finished run is
    ~380,000 objects and listing it that way took 64 seconds.
    """
    from concurrent.futures import ThreadPoolExecutor

    def prefixes(p: str) -> list[str]:
        r = _aws("s3", "ls", f"s3://{bucket}/{p}", profile=profile)
        return [f"{p}{ln.split('PRE ', 1)[1]}" for ln in r.stdout.splitlines() if "PRE " in ln]

    products = [p for p in prefixes(run_prefix)
                if not p.rstrip("/").rsplit("/", 1)[-1].startswith("_")]
    with ThreadPoolExecutor(max_workers=16) as ex:
        jobs = [j for part in ex.map(prefixes, products) for j in part]

    def files(job: str) -> tuple[str, set[str]]:
        """Filenames AND sub-directory names (the latter keeping their trailing "/").

        The directories come back because the caller has to know which stage trees a job
        holds -- see backfill_one. They cost nothing: this listing already returns them and
        they were being thrown away. Keeping the slash is what stops "04_stage4_ai/" from
        ever being mistaken for a file in the membership tests below.
        """
        r = _aws("s3", "ls", f"s3://{bucket}/{job}", profile=profile)
        names = {ln.split(maxsplit=3)[3] for ln in r.stdout.splitlines()
                 if ln.strip() and "PRE " not in ln and len(ln.split(maxsplit=3)) == 4}
        names |= {ln.split("PRE ", 1)[1].strip() for ln in r.stdout.splitlines() if "PRE " in ln}
        return job[len(run_prefix):].rstrip("/"), names

    with ThreadPoolExecutor(max_workers=32) as ex:
        return dict(ex.map(files, jobs))


def backfill_one(bucket: str, run_prefix: str, job: str, files: set[str], profile: str | None,
                 dry_run: bool) -> tuple[bool, str]:
    """Returns (built, detail). Downloads what is needed, builds, uploads the two HTML files.

    `files` is the job's own top-level listing (from job_dirs()) and is how a summary-AI job
    is told apart from an ordinary one: it never has 03_stage3_final, only 04_stage4_ai, and
    summary_ai_rule.json is written the moment the route is taken -- the same marker
    pipeline_monitor and run_corpus already key off, so a fourth definition of "is this a
    summary-AI job" is not invented here."""
    import corpus_worker as W

    product, label = job.split("/", 1)
    src = f"s3://{bucket}/{run_prefix}{job}"
    is_summary_ai = "summary_ai_rule.json" in files
    # EVERY STAGE TREE THE JOB HOLDS, not just the one the viewer draws. This used to fetch a
    # single directory -- 03_stage3_final, or 04_stage4_ai for the summary route -- which was
    # right when the page had one tree in it, and is wrong now in two ways at once. First,
    # build_review_artefacts hands build_viewer the NEWEST stage present (corpus_worker.py:870),
    # so a document that went through stage 4/5 was getting a viewer of the AI pass's INPUT
    # rather than of what ships. Second, the Stage 4 tab's numbers come from
    # check_stage4.stage4_dashboard, which reads 04_stage4_ai/stage4_report.json and CONSERVATION
    # -- a cell-by-cell diff of the stage-3 and stage-4 trees -- so with only stage 3 on disk it
    # reports "AI post-processing has not been run for this document" about a document that paid
    # for it. Both are silent: the page renders, fully, saying something untrue.
    trees = [d for d in W.STAGE_DIRS.values() if f"{d}/" in files]
    if not trees:                          # a listing that predates the prefix capture
        trees = [W.STAGE_DIRS[4] if is_summary_ai else "03_stage3_final"]
    tmp = Path(tempfile.mkdtemp(prefix="backfill-"))
    try:
        dest = tmp / label
        dest.mkdir(parents=True)
        for tree_dir in trees:
            (dest / tree_dir).mkdir(exist_ok=True)
            r = _aws("s3", "cp", f"{src}/{tree_dir}/", str(dest / tree_dir),
                     "--recursive", "--only-show-errors", profile=profile)
            if r.returncode != 0:
                return False, f"{tree_dir} download failed: {r.stderr.strip()[:100]}"
        for f in NEEDED:
            _aws("s3", "cp", f"{src}/{f}", str(dest / f), "--only-show-errors", profile=profile)
        if not (dest / "source.pdf").exists():
            return False, "no source.pdf in the job — cannot build a viewer"
        if dry_run:
            return True, "would build"
        made = W.build_review_artefacts(dest, product, label,
                                        route="summary_ai" if is_summary_ai else None)
        if not made:
            return False, "build_review_artefacts produced nothing"
        for name in made:
            r = _aws("s3", "cp", str(dest / name), f"{src}/{name}", "--only-show-errors",
                     profile=profile)
            if r.returncode != 0:
                return False, f"upload of {name} failed: {r.stderr.strip()[:120]}"
        sizes = ", ".join(f"{n} {(dest / n).stat().st_size / 1e6:.1f}MB" for n in made)
        return True, sizes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default=os.environ.get("ACI_EXTRACTION_BUCKET",
                                                       "aosphere-tenant-dev1-core-index"))
    ap.add_argument("--prefix", default=os.environ.get("ACI_EXTRACTION_PREFIX", "corpus"))
    ap.add_argument("--run", required=True, help="the run id, e.g. 2026-08-21-02")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    ap.add_argument("--product", action="append", default=None, metavar="DIR_OR_NAME",
                    help="restrict to a product directory, repeatable. Matches the run's "
                         "product dir by substring, case-insensitively (e.g. 'Data_Privacy'). "
                         "Default: every product in the run.")
    ap.add_argument("--live-products", action="store_true",
                    help="restrict to the products that are actually served — "
                         "region_map.PRODUCTS — instead of every product in the run")
    ap.add_argument("--limit", type=int, default=None, help="only the first N (for a trial)")
    ap.add_argument("--plan", action="store_true", help="list what is missing and stop")
    ap.add_argument("--rebuild", action="store_true",
                    help="also rebuild documents that ALREADY have both artefacts — use after "
                         "the page itself changes (a new tab, a new embedded payload), which "
                         "a frozen page cannot pick up on its own")
    ap.add_argument("--dry-run", action="store_true", help="download and check, never write")
    args = ap.parse_args()

    run_prefix = f"{args.prefix.strip('/')}/{args.run}/"
    print(f"  scanning s3://{args.bucket}/{run_prefix}", flush=True)
    t0 = time.time()
    jobs = job_dirs(args.bucket, run_prefix, args.profile)
    done = {j for j, f in jobs.items() if "scorecard.json" in f}
    # THE PAGE IS FROZEN, SO IT CAN ONLY CHANGE BY BEING REBUILT. Selecting on absence is right
    # for the gap this script was written for (123 documents that finished before the worker
    # built viewers at all), and it is exactly wrong when the PAGE gains something -- the Stage 4
    # · AI tab, the Scorecard 1/2 switcher -- because every already-published document has an
    # inspect.html and is therefore skipped forever. The new payload then reaches new extractions
    # only, and the corpus splits in two: documents that show the AI pass and documents that
    # cannot, with nothing on screen to say which kind you are looking at. --rebuild is opt-in
    # because it is not free (a full tree download and ~20MB uploaded per document), and because
    # overwriting a good artefact should be something someone asked for by name.
    targets = sorted(done) if args.rebuild else sorted(j for j in done
                                                       if not set(ARTEFACTS) <= jobs[j])

    # Product filter. A run carries ~36 product directories and only three are served, so
    # backfilling all of them builds viewers nobody can reach. Applied to the product dir,
    # which is the first path component of a job.
    if args.live_products:
        # The SAME resolver the promotion selects with, not a substring match, so this
        # filter and the promotion provably agree on what "a live product" is. It matters
        # here: the run carries '170_Data_Privacy_(US_States)' and '165_Data_Privacy_-
        # _Snippets', which a substring match on 'Data_Privacy' would wrongly include.
        sys.path.insert(0, str(HERE.parent / "src"))
        from aosphere_core_index.regions.region_map import is_indexable_product_dir
        before = len(targets)
        targets = [j for j in targets if is_indexable_product_dir(j.split("/", 1)[0])]
        print(f"  live products only -> {len(targets)} of {before} selected")
    if args.product:
        norm = [q.lower().replace(" ", "_") for q in args.product]
        before = len(targets)
        targets = [j for j in targets
                   if any(q in j.split("/", 1)[0].lower() for q in norm)]
        print(f"  product filter {args.product} -> {len(targets)} of {before} selected")
    what = "to rebuild" if args.rebuild else "missing an artefact"
    print(f"  {len(jobs)} job dirs, {len(done)} finished, {len(targets)} {what} "
          f"({time.time() - t0:.1f}s)")
    if not targets:
        print("  nothing to do")
        return
    for j in targets[:10]:
        print(f"      {j}   has: {sorted(jobs[j] & set(ARTEFACTS)) or 'neither'}")
    if len(targets) > 10:
        print(f"      ... and {len(targets) - 10} more")
    if args.plan:
        return

    todo = targets[:args.limit] if args.limit else targets
    print(f"\n  building for {len(todo)} document(s){' (dry run)' if args.dry_run else ''}\n",
          flush=True)
    ok = bad = 0
    for i, job in enumerate(todo, 1):
        t = time.time()
        built, detail = backfill_one(args.bucket, run_prefix, job, jobs[job], args.profile,
                                     args.dry_run)
        ok, bad = (ok + 1, bad) if built else (ok, bad + 1)
        print(f"  [{i:3d}/{len(todo)}] {'ok ' if built else 'FAIL'} {job[:64]:64s} "
              f"{detail[:60]:60s} {time.time() - t:.0f}s", flush=True)
    print(f"\n  {ok} built, {bad} failed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
