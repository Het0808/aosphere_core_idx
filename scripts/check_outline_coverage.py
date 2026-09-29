#!/usr/bin/env python3
"""check_outline_coverage.py — does every heading the outline names reach the tree?

The weaker, deeper half of the sectioning question. `heading_hierarchy`'s census asks
whether each promised section became its own NODE, and is capped at census_levels()
depth so a product whose rule builds three levels is not charged for the fourth. This
asks only whether the heading's TEXT survives as a heading ANYWHERE in the shipped
tree — a sub-heading inside a file counts. Nothing is being asked about structure, so
the depth cap does not apply and the sub-sections the census never promises are in
scope.

That gap is not theoretical. Belgium 163341 ships with no "4.5 Provision of Non-Core
Services by an AIFM", no "4.6 Small AIFMs" and no "4.7 Sub-funds" anywhere in its
stage-5 tree; its census promises 17 sections and reports 0 missing, and the document
gates on that. Finland and Luxembourg each lose "4.1 Gold-plating/Super-equivalence",
Greece "6.4 Investment Management & Advisory Services", Hungary "7.1 Private Placement
Regime".

Matching is `compare_stage_completeness._norm_heading` on both sides — the pipeline's
own tokenizer, with the group-label word ("Appendix 3" -> "3") folded away because the
outline and the post-AI tree disagree about it by design and neither is wrong. Before
that fold, 187 of 197 headings this reported were that rewrite alone, in all 54
documents of the 16-09-26 corpus.

Product-scoped (product_rules.OUTLINE_COVERAGE_PRODUCTS); any other product scores None
and passes.

Usage:
    python scripts/check_outline_coverage.py out/corpus/<product>/<job> [--stage 5]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import BOLD, DIM, GREEN, RED, banner, run_cli  # noqa: E402
from product_rules import outline_coverage_checked, product_for_job  # noqa: E402


def ai_ran(job_root: Path) -> bool:
    """Did this job go through the paid AI stages?

    The product allowlist above stays the rule for a DETERMINISTIC job, and the comment
    on OUTLINE_COVERAGE_PRODUCTS says why: the five flat-tree products flag 100% of their
    jobs, so switching them on wholesale would report every document as missing headings.

    A job that ran stage 4 is a different case and is checked whatever its product. Stage
    4 rewrites the tree the headings live in -- it is the stage that can drop one -- and it
    is the stage that was paid for, so "did every heading survive it" is worth a false
    positive to answer. A flat-tree product that now flags is telling you its tree was
    never audited, which is the thing the allowlist was deferring, not hiding.

    Read from corpus_meta's own marker, which _mark_ai_processed writes when the pass
    completes, so a crashed stage 4 that left a half-written 04_ dir behind does not count
    as having run.
    """
    try:
        meta = json.loads((Path(job_root) / "corpus_meta.json").read_text())
        return bool(meta.get("ai_processed"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def compute_outline_coverage(out_root: Path, stage: int = 3) -> dict:
    # Lazy: compare_stage_completeness imports lib_validate, which imports this module.
    from compare_stage_completeness import heading_coverage

    out_root = Path(out_root)
    product = product_for_job(out_root)
    if not (outline_coverage_checked(product) or ai_ran(out_root)):
        return {"available": False, "passed": True, "score": None, "product": product,
                "outline_count": 0, "missing": [], "missing_count": 0,
                "reason": f"product {product!r} is not in OUTLINE_COVERAGE_PRODUCTS "
                          "and this job did not run the AI stages"}
    # from_stage is the deterministic tree, to_stage the one being scored. Only the
    # latter is reported on: a heading already absent at stage 3 is the extraction's
    # problem and is reported when stage 3 is the scored stage.
    d = heading_coverage(out_root, 3, stage)
    if d.get("error"):
        return {"available": False, "passed": True, "score": None, "product": product,
                "outline_count": 0, "missing": [], "missing_count": 0,
                "reason": d["error"]}
    missing = [{"title": h.get("title"), "level": h.get("level"), "page": h.get("page")}
               for h in (d.get("missing_at_to") or [])]
    total = d.get("outline_count") or 0
    return {
        "available": True,
        "product": product,
        "scored_stage": stage,
        "outline_count": total,
        "delivered": total - len(missing),
        "missing": missing[:40],
        "missing_count": len(missing),
        "score": round(100.0 * (total - len(missing)) / total, 1) if total else 100.0,
        "passed": not missing,
    }


def print_report(report: dict) -> None:
    if not report.get("available"):
        print(DIM(f"skipped — {report.get('reason')}"))
        return
    print(f"{DIM('outline headings:')} {report['outline_count']}"
          f"    {DIM('reaching the tree:')} {report['delivered']}")
    if report["missing"]:
        print(f"\n{RED(BOLD('ABSENT — named by the outline, no heading anywhere in the tree:'))}")
        for m in report["missing"]:
            pg = f"p{m['page']}" if m.get("page") is not None else "p?"
            print(f"    {pg:>6}  {m['title']}")
    print()
    if report["passed"]:
        print(GREEN(BOLD("✓ PASS — every outline heading reaches the tree")))
    else:
        print(RED(BOLD(f"✗ FAIL — {report['missing_count']} outline heading(s) absent")))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--stage", type=int, default=3)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    banner("Outline coverage · outline headings vs the shipped tree")
    report = compute_outline_coverage(Path(args.out_root).resolve(), stage=args.stage)
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
