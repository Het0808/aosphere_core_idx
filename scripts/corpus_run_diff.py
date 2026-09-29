#!/usr/bin/env python3
"""corpus_run_diff.py — snapshot a corpus run's numbers, then diff two snapshots.

Built for comparing a full out/corpus/ re-run against the run it replaced: take
a snapshot before wiping the corpus, take another after the fresh run
finishes (or partway through — only documents present in BOTH snapshots are
diffed, so a still-running corpus just yields a partial report), and get a
report of what actually moved: gate flips, score deltas, and the footnote-
specific counts (dangling/orphan/gap/excluded) this rerun exists to fix.

Reads validation.json + scorecard.json per job — never re-scores anything.

Usage:
    # before wiping/re-running:
    python scripts/corpus_run_diff.py snapshot out/corpus /tmp/before.json

    # after (or during) the fresh run:
    python scripts/corpus_run_diff.py snapshot out/corpus /tmp/after.json
    python scripts/corpus_run_diff.py diff /tmp/before.json /tmp/after.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import BOLD, DIM, GREEN, RED, YELLOW, banner


def _job_key(product_dir: Path, job_dir: Path) -> str:
    return f"{product_dir.name}/{job_dir.name}"


def snapshot(out_root: Path) -> dict:
    jobs = {}
    for product_dir in sorted(p for p in out_root.iterdir() if p.is_dir()):
        for job_dir in sorted(p for p in product_dir.iterdir() if p.is_dir()):
            sc_p, val_p = job_dir / "scorecard.json", job_dir / "validation.json"
            if not sc_p.exists():
                continue
            try:
                sc = json.loads(sc_p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            val = {}
            if val_p.exists():
                try:
                    val = json.loads(val_p.read_text())
                except (OSError, json.JSONDecodeError):
                    val = {}
            fn = val.get("footnote_integrity") or {}
            ni = val.get("numeric_integrity") or {}
            dims = sc.get("dimensions") or {}
            fidelity_detail = (dims.get("fidelity") or {}).get("detail") or {}
            tables = sc.get("tables") or []
            jobs[_job_key(product_dir, job_dir)] = {
                "gate": sc.get("gate"),
                "worst_score": sc.get("worst_score"),
                "weakest_dimension": sc.get("weakest_dimension"),
                "active_finding_count": sc.get("active_finding_count"),
                "dimensions": {k: (v or {}).get("score") for k, v in dims.items()},
                "tables_total": fidelity_detail.get("tables_total", len(tables)),
                "tables_failed": sum(1 for t in tables if t.get("bucket") == "failed"),
                "fn_refs_total": fn.get("refs_total"),
                "fn_defs_total": fn.get("defs_total"),
                "fn_dangling_count": fn.get("dangling_count"),
                "fn_orphan_count": fn.get("orphan_definition_count"),
                "fn_sequence_gap_count": fn.get("sequence_gap_count"),  # None on a pre-fix snapshot
                "ni_missing_count": len(ni.get("missing") or {}),
                "ni_missing_error_count": ni.get("missing_error_count"),
                "ni_transposition_count": len(ni.get("transpositions") or []),
                "ni_footnote_marker_excluded_count": ni.get("footnote_marker_excluded_count"),  # None pre-fix
            }
    return {"out_root": str(out_root), "job_count": len(jobs), "jobs": jobs}


def _fmt_delta(before, after) -> str:
    if before is None or after is None:
        return f"{before} -> {after}"
    d = after - before
    if d == 0:
        return f"{after}"
    colour = GREEN if d < 0 else RED  # fewer issues = good, shown green
    return f"{before} -> {after}  ({colour(f'{d:+d}')})"


def diff(before: dict, after: dict, top: int = 25) -> None:
    b, a = before["jobs"], after["jobs"]
    both = sorted(set(b) & set(a))
    only_before = sorted(set(b) - set(a))
    only_after = sorted(set(a) - set(b))

    banner(f"Corpus rerun diff · {len(both)} document(s) in both snapshots "
           f"({len(only_before)} only in before, {len(only_after)} only in after)")

    gate_flips = [(k, b[k]["gate"], a[k]["gate"]) for k in both if b[k]["gate"] != a[k]["gate"]]
    print(f"\n{BOLD('Gate changes')}: {len(gate_flips)} of {len(both)}")
    order = {"fail": 0, "review": 1, "pass": 2, None: 3}
    for k, gb, ga in sorted(gate_flips, key=lambda x: order.get(x[2], 3)):
        colour = GREEN if order.get(ga, 3) < order.get(gb, 3) else RED
        print(f"    {k:<55} {DIM(str(gb)):>8} -> {colour(str(ga))}")

    def _sum(field):
        vb = sum(b[k].get(field) or 0 for k in both)
        va = sum(a[k].get(field) or 0 for k in both)
        return vb, va

    print(f"\n{BOLD('Aggregate counts across the shared document set')}:")
    for label, field in [
        ("footnote dangling refs (cited, no body)", "fn_dangling_count"),
        ("footnote orphan bodies (body, no citation — marker lost)", "fn_orphan_count"),
        ("footnote sequence gaps (missing from numbering entirely)", "fn_sequence_gap_count"),
        ("numeric-integrity 'missing' entries", "ni_missing_count"),
        ("numeric-integrity 'missing' — error severity", "ni_missing_error_count"),
        ("digit transpositions", "ni_transposition_count"),
        ("numbers excluded as known footnote markers (new)", "ni_footnote_marker_excluded_count"),
    ]:
        vb, va = _sum(field)
        print(f"    {label:<58} {_fmt_delta(vb, va)}")

    score_deltas = []
    for k in both:
        wb, wa = b[k].get("worst_score"), a[k].get("worst_score")
        if wb is not None and wa is not None:
            score_deltas.append((k, wb, wa, wa - wb))
    score_deltas.sort(key=lambda x: x[3])
    print(f"\n{BOLD('Biggest worst-score changes')} (top {top}, most improved first):")
    for k, wb, wa, d in score_deltas[:top]:
        colour = GREEN if d > 0 else (RED if d < 0 else DIM)
        print(f"    {k:<55} {wb:>5} -> {wa:<5} {colour(f'{d:+.1f}')}")
    if any(d < -0.5 for *_, d in score_deltas):
        regressed = [k for k, *_, d in score_deltas if d < -0.5]
        print(f"\n{YELLOW(BOLD(f'{len(regressed)} document(s) got WORSE — check these by hand:'))}")
        for k in regressed[:top]:
            print(f"    {k}")

    if only_before or only_after:
        print(f"\n{YELLOW('Document set changed:')}")
        if only_before:
            print(f"    only in BEFORE (missing from the new run): {', '.join(only_before[:20])}")
        if only_after:
            print(f"    only in AFTER (new in this run): {', '.join(only_after[:20])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot")
    sp.add_argument("out_root")
    sp.add_argument("json_path")

    dp = sub.add_parser("diff")
    dp.add_argument("before_json")
    dp.add_argument("after_json")
    dp.add_argument("--top", type=int, default=25)

    args = ap.parse_args()
    if args.cmd == "snapshot":
        result = snapshot(Path(args.out_root).resolve())
        Path(args.json_path).write_text(json.dumps(result, indent=2))
        print(f"snapshotted {result['job_count']} document(s) from {args.out_root} -> {args.json_path}")
    else:
        before = json.loads(Path(args.before_json).read_text())
        after = json.loads(Path(args.after_json).read_text())
        diff(before, after, args.top)


if __name__ == "__main__":
    main()
