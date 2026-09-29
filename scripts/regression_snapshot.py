#!/usr/bin/env python3
"""regression_snapshot — freeze what the pipeline currently produces, so a change to
the extractor can be proved harmless before it is trusted.

Two modes:

    snapshot   read every finished job dir and record the numbers that define its
               result (verdict, per-dimension scores, coverage, section shape). Reads
               only what is already on disk — no extraction, seconds for the corpus.

    compare    diff two snapshots and classify every document as unchanged, improved,
               or REGRESSED. A regression is a lower verdict or a lower critical
               dimension; anything else is reported but not alarming.

The snapshot is the baseline; after changing the extractor, re-extract a sample, take
a second snapshot and compare. What matters is not that numbers move — a real fix
moves them — but that they only move where the fix was supposed to apply.

    scripts/regression_snapshot.py snapshot out/corpus -o /tmp/before.json
    scripts/regression_snapshot.py sample   out/corpus -n 30 -o /tmp/sample.txt
    scripts/regression_snapshot.py compare  /tmp/before.json /tmp/after.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

GATE_RANK = {"pass": 3, "review": 2, "fail": 1, None: 0}
# the dimensions that can set a verdict — a drop in one of these is the thing to catch
CRITICAL = ("completeness", "placement", "fidelity")


def one(job: Path) -> dict | None:
    """Everything that defines this document's result, from artifacts already on disk."""
    try:
        sc = json.loads((job / "scorecard.json").read_text())
    except (OSError, ValueError):
        return None
    dims = {k: (v or {}).get("score") for k, v in (sc.get("dimensions") or {}).items()}
    comp = ((sc.get("dimensions") or {}).get("completeness") or {}).get("detail") or {}
    row = {
        "doc": f"{job.parent.name}/{job.name}",
        "gate": sc.get("gate"), "worst": sc.get("worst_score"),
        "dims": dims,
        "coverage_pct": comp.get("coverage_pct"),
        "page_coverage_pct": comp.get("page_coverage_pct"),
        "pages_missing": comp.get("pages_missing"),
        "sections": comp.get("files_scanned"),
        "gap_sections": comp.get("files_with_silent_gap"),
        "silent_spans": comp.get("silent_spans"),
        "findings": sc.get("active_finding_count"),
    }
    try:
        s1 = json.loads((job / "01_stage1_extract/stage1_report.json").read_text())
        row.update(headings_total=s1.get("headings_total"),
                   headings_matched=s1.get("headings_matched"),
                   md_words=s1.get("md_words"), pdf_words=s1.get("pdf_words"),
                   files_written=s1.get("files_written"),
                   structure_source=s1.get("structure_source"))
    except (OSError, ValueError):
        pass
    # the tree's SHAPE: a boundary bug moves content between files without changing
    # totals, so the per-file word counts are what actually catches it
    tree = job / "03_stage3_final"
    if tree.is_dir():
        files = {str(f.relative_to(tree)): len(f.read_text(errors="ignore").split())
                 for f in sorted(tree.rglob("*.md"))}
        row["tree_files"] = len(files)
        row["tree_words"] = sum(files.values())
        row["files"] = files
    return row


def snapshot(root: Path) -> dict:
    rows = {}
    for sc in sorted(root.glob("*/*/scorecard.json")):
        r = one(sc.parent)
        if r:
            rows[r["doc"]] = r
    return rows


def compare(before: dict, after: dict) -> int:
    common = [d for d in after if d in before]
    print(f"{len(common)} document(s) in both snapshots "
          f"({len(before)} before, {len(after)} after)\n")
    regressed, improved, changed, same = [], [], [], 0
    for d in common:
        b, a = before[d], after[d]
        gate_delta = GATE_RANK[a["gate"]] - GATE_RANK[b["gate"]]
        dim_drops = {k: (b["dims"].get(k), a["dims"].get(k)) for k in CRITICAL
                     if isinstance(b["dims"].get(k), (int, float))
                     and isinstance(a["dims"].get(k), (int, float))
                     and a["dims"][k] < b["dims"][k] - 0.05}
        worse = gate_delta < 0 or dim_drops
        better = gate_delta > 0 or (a.get("worst") or 0) > (b.get("worst") or 0) + 0.05
        moved = (a.get("tree_files") != b.get("tree_files")
                 or a.get("tree_words") != b.get("tree_words"))
        if worse:
            regressed.append((d, b, a, dim_drops))
        elif better:
            improved.append((d, b, a))
        elif moved:
            changed.append((d, b, a))
        else:
            same += 1
    print(f"  unchanged           {same}")
    print(f"  improved            {len(improved)}")
    print(f"  tree moved, score unchanged {len(changed)}")
    print(f"  REGRESSED           {len(regressed)}")
    for label, rows in (("REGRESSED", regressed), ("improved", improved),
                        ("tree moved", changed)):
        if not rows:
            continue
        print(f"\n--- {label} ---")
        for item in rows[:20]:
            d, b, a = item[0], item[1], item[2]
            extra = ""
            if label == "REGRESSED" and item[3]:
                extra = "  " + ", ".join(f"{k} {x}->{y}" for k, (x, y) in item[3].items())
            print(f"  {d[:52]:52s} {b['gate']:6s} {b['worst']:5} -> {a['gate']:6s} {a['worst']:5}"
                  f"  files {b.get('tree_files')}->{a.get('tree_files')}"
                  f"  words {b.get('tree_words')}->{a.get('tree_words')}{extra}")
    return len(regressed)


def sample(root: Path, n: int) -> list[str]:
    """A spread worth re-extracting: every product represented, every verdict, both
    structure sources, and the extremes of size. Deterministic — the same sample before
    and after, or the comparison is meaningless."""
    rows = [r for r in snapshot(root).values()]
    picked, seen_product = [], {}
    def key(r):
        return (r.get("structure_source", ""), r.get("gate", ""), r["doc"].split("/")[0])
    for r in sorted(rows, key=lambda r: (r["doc"])):
        p = r["doc"].split("/")[0]
        if seen_product.get(p, 0) < 2:          # up to 2 per product
            picked.append(r); seen_product[p] = seen_product.get(p, 0) + 1
    # make sure each verdict and each structure source appears
    for bucket in ("pass", "review", "fail"):
        if not any(r["gate"] == bucket for r in picked):
            extra = next((r for r in rows if r["gate"] == bucket), None)
            if extra: picked.append(extra)
    return [r["doc"] for r in picked[:n]]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("root"); s.add_argument("-o", "--out", required=True)
    c = sub.add_parser("compare"); c.add_argument("before"); c.add_argument("after")
    m = sub.add_parser("sample"); m.add_argument("root"); m.add_argument("-n", type=int, default=30)
    m.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    if args.cmd == "snapshot":
        rows = snapshot(Path(args.root))
        Path(args.out).write_text(json.dumps(rows, indent=1))
        print(f"snapshot: {len(rows)} document(s) -> {args.out}")
    elif args.cmd == "compare":
        n = compare(json.loads(Path(args.before).read_text()),
                    json.loads(Path(args.after).read_text()))
        raise SystemExit(1 if n else 0)
    else:
        docs = sample(Path(args.root), args.n)
        text = "\n".join(docs)
        print(text)
        if args.out:
            Path(args.out).write_text(text + "\n")
            print(f"\n{len(docs)} document(s) -> {args.out}")


if __name__ == "__main__":
    main()
