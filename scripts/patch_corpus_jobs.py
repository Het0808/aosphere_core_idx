#!/usr/bin/env python3
"""patch_corpus_jobs.py — re-run Stage 1/2/3 + validation for a SPECIFIC list
of already-extracted corpus jobs, in place.

Built for a targeted fix that only changes SOME documents' output (e.g. the
floating-footer fix in pdf2mdtree.py — see scan_floating_footers.py): rather
than re-running the whole corpus, re-extract just the affected jobs. Reuses
run_corpus.py's run_one(), so a patched job is scored/validated exactly like
a full corpus run would produce — same code path, just scoped.

Stage 1 (pdf2mdtree.py) always re-runs — that's the point, it picks up the
code fix. Stage 2 (MinerU) is cheap here on a re-run: run_stage2's own cache
(keyed on the combined-tables PDF's bytes + backend/effort) hits again
whenever the fix didn't change which pages have tables, so this rarely
touches MinerU at all.

Usage:
    DISABLE_MINERU_FALLBACK=1 python scripts/patch_corpus_jobs.py --jobs affected.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hybrid_extract as he  # noqa: E402  (reused by run_corpus's run_one)
from run_corpus import run_one  # noqa: E402
from lib_content_compare import BOLD, DIM, GREEN, RED, YELLOW, banner  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", required=True, help="JSON produced by scan_floating_footers.py --json")
    ap.add_argument("--mineru-backend", default=None)
    ap.add_argument("--mineru-effort", default=None)
    args = ap.parse_args()

    affected = json.loads(Path(args.jobs).read_text())
    banner(f"Patching {len(affected)} corpus job(s)")

    results = []
    for i, entry in enumerate(affected, 1):
        dest = Path(entry["root"])
        meta_path = dest / "corpus_meta.json"
        if not meta_path.exists():
            print(f"  [{i}/{len(affected)}] {RED(f'SKIP {entry[\"job\"]} — no corpus_meta.json')}")
            continue
        meta = json.loads(meta_path.read_text())
        job = {"label": dest.name, "jurisdiction": meta["jurisdiction"],
              "doc_id": meta["doc_id"], "pdf": Path(meta["source_pdf"])}
        t0 = time.time()
        try:
            r = run_one(job, meta["product"], dest, stage1_only=False, force=True,
                       backend=args.mineru_backend, effort=args.mineru_effort)
        except Exception as e:  # noqa: BLE001 — one bad document must not sink the batch
            print(f"  [{i}/{len(affected)}] {RED(f'ERROR {entry[\"job\"]}: {e}')}")
            results.append({"job": entry["job"], "status": "error", "detail": str(e)})
            continue
        colour = {"pass": GREEN, "review": YELLOW, "fail": RED}.get(r.get("gate"), DIM)
        print(f"  [{i}/{len(affected)}] {entry['job']:<55} "
              f"{colour(str(r.get('gate')))}  worst={r.get('worst')}  {round(time.time()-t0, 1)}s")
        results.append({"job": entry["job"], **r})

    ok = sum(1 for r in results if r.get("status") == "done")
    print(f"\n{BOLD(f'{ok}/{len(affected)} patched successfully')}")
    return results


if __name__ == "__main__":
    main()
