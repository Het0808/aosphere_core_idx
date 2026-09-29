#!/usr/bin/env python3
"""Re-score finished extractions against the CURRENT checks, without re-extracting.

A scorecard is written once, when the document is extracted, and then it is what the Doc
Library shows forever. So every fix to a verification check leaves the corpus displaying the
verdict of the code that has since been corrected: Hungary Data Privacy 180716 still reads
"fail 62.1 — fidelity" from before the continuation-stub fix, when five reprinted "Term
Meaning" header strips were each scored 0% and counted as dropped rows. The extraction was
always intact; the check was wrong, and 455 documents carry verdicts from a mix of vintages.

Re-verification needs no GPU and no model: the stage-3 tree and source.pdf are already on
disk, so this re-runs the nine checks and recomputes the gate over what is there. What it
must never do is touch the extraction — if a tree is bad, that is a job for run_corpus, and
silently re-extracting here would hide it.

    scripts/reverify_corpus.py --dry-run                      # what would change, corpus-wide
    scripts/reverify_corpus.py --job "155_Data_Privacy/Hungary (Data Privacy)__180716"
    scripts/reverify_corpus.py --product 155_Data_Privacy --changed-only

Writes validation.json + scorecard.json in place (unless --dry-run) and prints every verdict
that moved, so a re-score is auditable rather than a silent rewrite of the corpus's grades.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lib_validate import run_and_gate  # noqa: E402  — the canonical validate + gate

REPO = HERE.parent
CORPUS = REPO / "out" / "corpus"


def jobs(product: str | None, job: str | None, corpus: Path = CORPUS) -> list[Path]:
    if job:
        d = corpus / job
        return [d] if (d / "scorecard.json").exists() else []
    pat = f"{product}/*/scorecard.json" if product else "*/*/scorecard.json"
    return [p.parent for p in sorted(corpus.glob(pat))]


def is_summary_ai(dest: Path) -> bool:
    """A summary-AI job writes its tree straight into 04_stage4_ai/ and has no 03_* tree.
    run_and_gate scores stage 3 by default, so re-scoring one here finds nothing to read
    and reports a false completeness 0.0 / fail — scripts/backfill_post_ai_scorecard.py
    scores these at their real stage instead."""
    if (dest / "summary_ai_rule.json").exists():
        return True
    return (next(iter(dest.glob("03_*")), None) is None
            and next(iter(dest.glob("04_*")), None) is not None)


def reverify(dest: Path, write: bool) -> dict:
    """-> {job, before, after, moved}. Never re-extracts; scores what is on disk."""
    old = json.loads((dest / "scorecard.json").read_text())
    val, sc = run_and_gate(dest)
    # Carry forward anything the EXTRACTION recorded and verification does not
    # recompute. run_and_gate scores the tree on disk; it knows nothing about how that
    # tree came to exist, so `fallback` (which tier rescued the document, and the TOC
    # verdict that triggered it) and `timing` (what the extraction cost) are absent from
    # its output. Writing that output verbatim therefore DELETED both from every
    # scorecard it touched, and the dashboard's route and took columns went blank across
    # the corpus — a re-score silently discarding the extraction's own record of itself.
    for k, v in old.items():
        if k not in sc:
            sc[k] = v
    before = (old.get("gate"), round(float(old.get("worst_score") or 0), 1))
    after = (sc.get("gate"), round(float(sc.get("worst_score") or 0), 1))
    if write:
        # Written even when the gate is unchanged: the FINDINGS behind it move too, and a
        # false positive that no longer fires must stop being listed in the UI.
        (dest / "validation.json").write_text(json.dumps(val, indent=2))
        (dest / "scorecard.json").write_text(json.dumps(sc, indent=2))
    return {"job": f"{dest.parent.name}/{dest.name}", "before": before, "after": after,
            "moved": before != after,
            "weakest": sc.get("weakest_dimension"),
            "findings": len(sc.get("findings") or [])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # The dashboard is started with its own --corpus-root, so re-scoring "the corpus"
    # has to be able to mean the same tree the UI is actually reading. Defaulting to
    # out/corpus keeps every existing invocation working.
    ap.add_argument("--corpus-root", default=str(CORPUS), type=Path,
                    help="corpus tree to re-score (default out/corpus)")
    ap.add_argument("--product", default=None)
    ap.add_argument("--job", default=None, help="<product>/<job>")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-newer-than", type=float, default=0, metavar="MINUTES",
                    help="skip jobs whose scorecard was rewritten this recently — makes a "
                         "corpus run RESUMABLE after a kill (a 469-job run is hours, and "
                         "the first one died at 307 with its buffered output lost)")
    args = ap.parse_args()

    corpus = args.corpus_root if args.corpus_root.is_absolute() else (REPO / args.corpus_root)
    if not corpus.is_dir():
        sys.exit(f"no such corpus root: {corpus}")
    print(f"corpus root: {corpus}")
    todo = jobs(args.product, args.job, corpus)
    if args.skip_newer_than:
        cutoff = time.time() - args.skip_newer_than * 60
        before = len(todo)
        todo = [d for d in todo if (d / "scorecard.json").stat().st_mtime < cutoff]
        print(f"  skipping {before - len(todo)} job(s) already re-scored", flush=True)
    summary_ai = [d for d in todo if is_summary_ai(d)]
    if summary_ai:
        todo = [d for d in todo if d not in summary_ai]
        print(f"  skipping {len(summary_ai)} summary-AI job(s) (no stage-3 tree) — score "
              f"them with scripts/backfill_post_ai_scorecard.py", flush=True)
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("no finished jobs matched")
        return

    t0 = time.time()
    moved, gates = [], {}
    for i, dest in enumerate(todo, 1):
        try:
            r = reverify(dest, write=not args.dry_run)
        except Exception as e:                      # noqa: BLE001
            print(f"  ✗ {dest.parent.name}/{dest.name}: {e}", flush=True)
            continue
        gates[r["after"][0]] = gates.get(r["after"][0], 0) + 1
        if r["moved"]:
            moved.append(r)
            b, a = r["before"], r["after"]
            print(f"  {b[0]:6s} {b[1]:5.1f}  ->  {a[0]:6s} {a[1]:5.1f}   {r['job'][:58]}",
                  flush=True)
        if i % 25 == 0:
            print(f"  … {i}/{len(todo)} ({time.time() - t0:.0f}s)", file=sys.stderr, flush=True)

    print(f"\n{len(todo)} job(s) re-scored in {time.time() - t0:.0f}s"
          f"{' (DRY RUN — nothing written)' if args.dry_run else ''}")
    print(f"  verdict moved: {len(moved)}")
    up = sum(1 for r in moved if r["after"][1] > r["before"][1])
    print(f"    improved {up}, worsened {len(moved) - up}")
    print("  gates now: " + ", ".join(f"{k}={v}" for k, v in sorted(gates.items())))


if __name__ == "__main__":
    main()
