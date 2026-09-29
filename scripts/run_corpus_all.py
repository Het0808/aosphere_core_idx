#!/usr/bin/env python3
"""run_corpus_all.py — drive run_corpus.py one folder at a time, cheapest first.

Why a driver instead of one long run: the folders are processed as separate
sequential invocations, so the run can be killed between folders without losing
anything, and each folder's results are complete and inspectable the moment it
finishes. Folder names in this corpus contain `&` and `()`, which makes passing
them through a shell loop fragile — hence Python.

Ordered by MinerU page count ascending: the cheap folders land within minutes so
quality problems surface early, and the expensive ones (170_Data_Privacy_(US_States)
alone is ~794 pages) come last, by which point you already know whether to bother.

Usage:
    python scripts/run_corpus_all.py "/path/to/Advanced_Search_All"
    python scripts/run_corpus_all.py <root> --from 104_Shareholding_Disclosure
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_corpus import discover  # noqa: E402


def mineru_cost(product_dir: Path) -> int:
    """Pages that would reach MinerU, read from an existing Stage 1 probe if one
    is on disk; else 0 so unprobed folders sort first (they are cheap to find out
    about)."""
    import json
    total = 0
    probe = HERE.parent / "out" / "corpus_probe" / product_dir.name
    if not probe.exists():
        return 0
    for d in probe.iterdir():
        mp = d / "01_stage1_extract" / "tables_manifest.json"
        if mp.exists():
            try:
                tabs = json.loads(mp.read_text()).get("tables", [])
                total += len({p for t in tabs for p in t["pages"]})
            except (OSError, json.JSONDecodeError):
                pass
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--out", default=str(HERE.parent / "out" / "corpus"))
    ap.add_argument("--start-from", default=None,
                    help="skip folders until this one (to resume a killed run)")
    ap.add_argument("--by-cost", action="store_true",
                    help="cheapest MinerU folders first instead of folder order")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    tree = discover(root)
    # Natural folder order (104_, 106_, 107_, …) so progress on screen matches the
    # order the folders appear on disk and it is obvious what has and has not run.
    # --by-cost instead sorts cheapest-MinerU-first, which surfaces more results
    # sooner but makes "where has it got to" harder to read.
    order = (sorted(tree, key=lambda n: mineru_cost(root / n)) if args.by_cost
             else sorted(tree))
    if args.start_from:
        if args.start_from not in order:
            sys.exit(f"--start-from {args.start_from!r} is not a folder in this corpus")
        order = order[order.index(args.start_from):]

    print(f"{len(order)} folder(s), "
          + ("cheapest MinerU first" if args.by_cost else "in folder order") + "\n",
          flush=True)
    t_all = time.time()
    for i, product in enumerate(order, 1):
        cost = mineru_cost(root / product)
        print(f"=== [{i}/{len(order)}] {product}  "
              f"({len(tree[product])} docs, ~{cost} MinerU pages) ===", flush=True)
        t0 = time.time()
        r = subprocess.run([sys.executable, str(HERE / "run_corpus.py"), str(root),
                            "--out", args.out, "--only", product])
        mins = (time.time() - t0) / 60
        print(f"=== {product} finished in {mins:.1f} min "
              f"(exit {r.returncode}) ===\n", flush=True)
    print(f"ALL FOLDERS DONE in {(time.time()-t_all)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
