#!/usr/bin/env python3
"""Score the post-AI tree of jobs that already ran stage 4/5, into scorecard_post_ai.json.

run_corpus writes this file itself from now on (see _score_post_ai), so this exists for
the jobs that finished BEFORE it did. Nothing here calls a model: stage 4's output is
already on disk, and this only re-runs the eight tree-vs-PDF checks against it. So it is
free to run over a whole corpus and safe to re-run.

scorecard.json is never touched. It is the extraction gate and the --resume completion
marker; this writes beside it.

Usage:
    python scripts/backfill_post_ai_scorecard.py out/corpus_bahamas_e2e
    python scripts/backfill_post_ai_scorecard.py out/corpus --force
    python scripts/backfill_post_ai_scorecard.py out/corpus --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lib_validate import final_stage, run_and_gate  # noqa: E402


def jobs_under(root: Path):
    """Every job dir under a corpus root — the dirs holding a corpus_meta.json.

    Matches the dashboard's own discovery (_corpus_jobs globs <root>/<product>/<job>), so
    a job this scores is a job that shows up there, and vice versa."""
    for meta in sorted(root.glob("*/*/corpus_meta.json")):
        yield meta.parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="a corpus root, e.g. out/corpus")
    ap.add_argument("--force", action="store_true",
                    help="re-score jobs that already have a scorecard_post_ai.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would be scored, score nothing")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 2

    scored = skipped = failed = 0
    for job in jobs_under(root):
        stage = final_stage(job)
        out = job / "scorecard_post_ai.json"
        if stage <= 3:
            skipped += 1
            continue                      # never ran stage 4 — nothing extra to score
        if out.exists() and not args.force:
            skipped += 1
            continue
        label = f"{job.parent.name}/{job.name}"
        if args.dry_run:
            print(f"  would score  {label}  (stage {stage})")
            scored += 1
            continue
        try:
            t = time.time()
            val, sc = run_and_gate(job, stage=stage)
            (job / "validation_post_ai.json").write_text(json.dumps(val, indent=2))
            out.write_text(json.dumps(sc, indent=2))
            # The extraction gate beside it, for the comparison that is the whole point:
            # a document can extract cleanly and still be degraded by the AI pass.
            pre = {}
            if (job / "scorecard.json").exists():
                pre = json.loads((job / "scorecard.json").read_text())
            print(f"  {label}  stage {stage}: "
                  f"{pre.get('gate', '?')} {pre.get('worst_score', '?')} -> "
                  f"{sc.get('gate')} {sc.get('worst_score')} "
                  f"({sc.get('weakest_dimension')})  {time.time() - t:.1f}s")
            scored += 1
        except Exception as e:  # noqa: BLE001 — one bad job must not stop the sweep
            print(f"  {label}  FAILED — {type(e).__name__}: {e}")
            failed += 1

    print(f"\nscored {scored}, skipped {skipped}, failed {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
