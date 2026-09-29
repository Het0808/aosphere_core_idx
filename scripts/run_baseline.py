#!/usr/bin/env python3
"""run_baseline.py — did a change help, across the whole test set?

Every comparison so far has been one document at a time, by hand. That cannot
answer the question that actually matters: a fix that improves one document and
breaks fifty others looks like a success. This runs a folder of PDFs through the
pipeline, stores every scorecard, and diffs against the stored baseline — so
"I think that helped" becomes a number.

    # first run: establish the baseline
    python scripts/run_baseline.py "/path/to/test folder" --save

    # after changing the pipeline or a checker: re-run and diff
    python scripts/run_baseline.py "/path/to/test folder"

    # re-score from the extractions already on disk (no MinerU, seconds not minutes)
    python scripts/run_baseline.py "/path/to/test folder" --rescore-only

--rescore-only is the one to reach for when iterating on a CHECK rather than on
the pipeline: extraction is unchanged, so re-running MinerU proves nothing and
costs minutes per document.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hybrid_extract as he  # noqa: E402
from check_content_localized import compute_content_localized  # noqa: E402
from check_engine_agreement import compute_engine_agreement  # noqa: E402
from check_heading_hierarchy import compute_heading_hierarchy  # noqa: E402
from check_numeric_integrity import compute_numeric_integrity  # noqa: E402
from check_scorecard import compute_scorecard  # noqa: E402
from check_semantic_integrity import compute_semantic_integrity  # noqa: E402
from check_table_placement import compute_table_placement  # noqa: E402
from check_table_presence import compute_table_presence  # noqa: E402
from check_word_coverage import compute_word_coverage  # noqa: E402
from lib_content_compare import BOLD, DIM, GREEN, RED, YELLOW, banner  # noqa: E402

OUT_ROOT = HERE.parent / "out" / "baseline"
BASELINE_FILE = OUT_ROOT / "baseline.json"

# The numbers a change is judged on. Higher is better for scores; lower is
# better for counts, hence the direction flag.
TRACKED = [
    ("gate", "gate", None),
    ("completeness", "score", "up"),
    ("placement", "score", "up"),
    ("fidelity", "score", "up"),
    ("uniqueness", "score", "up"),
    ("integrity", "score", "up"),
    ("silent_pages", "count", "down"),
    ("tables_failed", "count", "down"),
    ("negation_flips", "count", "down"),
    ("hierarchy_errors", "count", "down"),
    ("tables_lost", "count", "down"),
    ("unreadable_pages", "count", "down"),
]


# Canonical 9-check validator (this used to be a hand-kept 8-check copy that had
# silently lost footnote_integrity — exactly the drift lib_validate exists to prevent).
from lib_validate import validate as _validate  # noqa: E402


def _summarise(sc: dict, val: dict) -> dict:
    dims = sc.get("dimensions", {})
    tb = {}
    for t in sc.get("tables", []):
        tb[t["bucket"]] = tb.get(t["bucket"], 0) + 1
    hier = val.get("heading_hierarchy") or {}
    # A CRASHED check reports no findings, and every count below is "lower is
    # better" — so without this a checker that starts throwing shows up as a green
    # improvement on every one of its metrics. That is precisely the silent
    # regression this whole harness exists to catch, so surface it explicitly and
    # let it fail the run (see compare/print_report).
    errored = sorted(k for k, v in val.items()
                     if isinstance(v, dict) and v.get("error"))
    return {
        "errored_checks": errored,
        "gate": sc.get("gate"),
        "worst_score": sc.get("worst_score"),
        "weakest": sc.get("weakest_dimension"),
        **{k: (dims.get(k, {}) or {}).get("score") for k in
           ("completeness", "placement", "fidelity", "uniqueness", "integrity")},
        "silent_pages": (sc.get("pages", {}).get("counts", {}) or {}).get("silent", 0),
        "tables_total": len(sc.get("tables", [])),
        "tables_failed": tb.get("failed", 0),
        "tables_continuation": tb.get("continuation", 0),
        "negation_flips": (val.get("semantic_integrity") or {}).get("negation_flips", 0),
        "hierarchy_errors": len([f for f in hier.get("flags", [])
                                 if f.get("kind") in ("wrong_parent", "wrong_depth")]),
        "unreadable_pages": (val.get("engine_agreement") or {}).get("unreadable_count", 0),
        "tables_lost": (val.get("table_presence") or {}).get("missing_count", 0),
        "findings": sc.get("active_finding_count"),
    }


def run_one(pdf: Path, rescore_only: bool, backend=None, effort=None) -> dict:
    root = OUT_ROOT / pdf.stem
    root.mkdir(parents=True, exist_ok=True)
    local_pdf = root / "source.pdf"
    if not rescore_only or not local_pdf.exists():
        local_pdf.write_bytes(pdf.read_bytes())

    t0 = time.time()
    if not rescore_only:
        manifest = he.run_stage1(local_pdf, root / "01_stage1_extract")
        s2 = he.run_stage2(local_pdf, manifest, root / "02_stage2_mineru_tables", backend, effort)
        he.run_stage3(root / "01_stage1_extract", root / "02_stage2_mineru_tables",
                      root / "03_stage3_final", s2)
    elif not (root / "03_stage3_final").exists():
        return {"pdf": pdf.name, "error": "no extraction on disk to re-score"}

    val = _validate(root)
    sc = compute_scorecard(root, val)
    return {"pdf": pdf.name, "seconds": round(time.time() - t0, 1),
            "summary": _summarise(sc, val)}


def _delta(new, old, direction):
    if new is None or old is None or isinstance(new, str) or isinstance(old, str):
        return None
    d = round(new - old, 1)
    if d == 0:
        return {"delta": 0, "verdict": "same"}
    better = (d > 0) if direction == "up" else (d < 0)
    return {"delta": d, "verdict": "better" if better else "worse"}


def compare(current: list[dict], baseline: dict) -> dict:
    old_by_pdf = {r["pdf"]: r.get("summary", {}) for r in baseline.get("runs", [])}
    rows, better, worse = [], 0, 0
    for r in current:
        s = r.get("summary")
        if not s:
            rows.append({"pdf": r["pdf"], "error": r.get("error")})
            continue
        old = old_by_pdf.get(r["pdf"])
        changes = {}
        for name, kind, direction in TRACKED:
            new_v = s.get(name)
            old_v = (old or {}).get(name)
            if kind == "gate":
                if old and new_v != old_v:
                    changes["gate"] = {"from": old_v, "to": new_v}
                continue
            d = _delta(new_v, old_v, direction) if old else None
            if d and d["verdict"] != "same":
                changes[name] = {**d, "from": old_v, "to": new_v}
                better += d["verdict"] == "better"
                worse += d["verdict"] == "worse"
        rows.append({"pdf": r["pdf"], "summary": s, "had_baseline": old is not None,
                     "changes": changes})
    # A crashed check makes every one of its counts read as 0, i.e. as an
    # improvement. Never let that pass as green: it fails the run on its own,
    # independently of any delta.
    broken = [{"pdf": row["pdf"], "checks": row["summary"]["errored_checks"]}
              for row in rows if row.get("summary", {}).get("errored_checks")]
    return {"rows": rows, "n_better": better, "n_worse": worse,
            "broken_checks": broken,
            "regressed": worse > 0 or bool(broken),
            "no_baseline": not baseline.get("runs")}


def print_report(current: list[dict], cmp_: dict):
    hdr = f"  {'document':<26} {'gate':<7} {'compl':>6} {'place':>6} {'fidel':>6} " \
          f"{'silent':>7} {'tbl-fail':>9} {'neg':>4} {'hier':>5}"
    print(BOLD(hdr))
    print(DIM("  " + "─" * (len(hdr) - 2)))
    gate_colour = {"pass": GREEN, "review": YELLOW, "fail": RED}
    for row in cmp_["rows"]:
        if row.get("error"):
            print(f"  {row['pdf'][:26]:<26} {RED('ERROR')}  {DIM(str(row['error'])[:60])}")
            continue
        s = row["summary"]
        g = gate_colour.get(s["gate"], DIM)
        def f(v):
            return "  n/a" if v is None else f"{v:6.1f}"
        print(f"  {row['pdf'][:26]:<26} {g(str(s['gate'])[:7].ljust(7))} "
              f"{f(s['completeness'])} {f(s['placement'])} {f(s['fidelity'])} "
              f"{s['silent_pages']:>7} {s['tables_failed']:>9} "
              f"{s['negation_flips']:>4} {s['hierarchy_errors']:>5}")

    # Printed BEFORE the deltas and before the no-baseline early return: a broken
    # checker invalidates every number above it, so it must never be scrolled past.
    if cmp_["broken_checks"]:
        print()
        print(RED(BOLD("  ✗ CHECKS CRASHED — their metrics read as 0, i.e. as an improvement.")))
        print(RED("    Every number above is unreliable until these are fixed:"))
        for b in cmp_["broken_checks"]:
            print(RED(f"      {b['pdf']}: {', '.join(b['checks'])}"))

    if cmp_["no_baseline"]:
        print(YELLOW("\n  no baseline stored yet — re-run with --save to record this as the baseline"))
        return
    print()
    any_change = False
    for row in cmp_["rows"]:
        ch = row.get("changes") or {}
        if not ch:
            continue
        any_change = True
        print(f"  {BOLD(row['pdf'])}")
        for name, c in ch.items():
            if name == "gate":
                print(f"      gate {c['from']} → {BOLD(c['to'])}")
                continue
            col = GREEN if c["verdict"] == "better" else RED
            sign = "+" if c["delta"] > 0 else ""
            print(f"      {name:<20} {c['from']} → {c['to']}  {col(sign + str(c['delta']))}")
    if cmp_["broken_checks"]:
        print()
        print(RED(BOLD("  ✗ FAIL — a check crashed; results are not trustworthy")))
    elif not any_change:
        print(GREEN("  ✓ identical to baseline on every tracked number"))
    else:
        print()
        if cmp_["regressed"]:
            print(RED(BOLD(f"  ✗ {cmp_['n_worse']} number(s) got WORSE "
                           f"({cmp_['n_better']} better) — inspect before keeping this change")))
        else:
            print(GREEN(BOLD(f"  ✓ {cmp_['n_better']} number(s) improved, none regressed")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="folder of PDFs to use as the test set")
    ap.add_argument("--save", action="store_true", help="record this run as the new baseline")
    ap.add_argument("--rescore-only", action="store_true",
                    help="reuse extractions on disk; only re-run the checks")
    ap.add_argument("--mineru-backend", default=None)
    ap.add_argument("--mineru-effort", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    pdfs = sorted(p for p in folder.glob("*.pdf") if not p.name.startswith("."))
    if not pdfs:
        sys.exit(f"no PDFs in {folder}")

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    banner(f"Baseline · {len(pdfs)} document(s) from {folder.name}")
    mode = "re-scoring existing extractions" if args.rescore_only else "full pipeline (MinerU)"
    print(DIM(f"  mode: {mode}\n"))

    current = []
    for i, pdf in enumerate(pdfs, 1):
        print(DIM(f"  [{i}/{len(pdfs)}] {pdf.name} …"), flush=True)
        try:
            current.append(run_one(pdf, args.rescore_only, args.mineru_backend, args.mineru_effort))
        except Exception as e:  # noqa: BLE001 — one bad document must not sink the run
            traceback.print_exc()
            current.append({"pdf": pdf.name, "error": str(e)})
    print()

    baseline = {}
    if BASELINE_FILE.exists():
        try:
            baseline = json.loads(BASELINE_FILE.read_text())
        except json.JSONDecodeError:
            baseline = {}
    cmp_ = compare(current, baseline)
    print_report(current, cmp_)

    if args.save and cmp_["broken_checks"]:
        # Saving now would bake this run's zeros in as the reference, so the very
        # next run would compare clean against them and look unchanged.
        print(RED(BOLD("\n  refusing to --save: a check crashed this run")))
    elif args.save:
        BASELINE_FILE.write_text(json.dumps(
            {"saved_at": time.time(), "folder": str(folder), "runs": current}, indent=2))
        print(DIM(f"\n  baseline saved to {BASELINE_FILE}"))
    if args.json:
        Path(args.json).write_text(json.dumps({"runs": current, "comparison": cmp_}, indent=2))
        print(DIM(f"  full report written to {args.json}"))

    sys.exit(1 if cmp_["regressed"] else 0)


if __name__ == "__main__":
    main()
