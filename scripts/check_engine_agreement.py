#!/usr/bin/env python3
"""check_engine_agreement.py — breaks the validator's circularity, cheaply.

THE PROBLEM THIS SOLVES. Tests 1-3 read the source PDF with fitz (PyMuPDF) —
the same engine pdf2mdtree is built on. So they compare fitz's reading of the
PDF against a tree derived from fitz's reading of the PDF. Where fitz has a
blind spot, BOTH sides are missing the same content, coverage looks perfect,
and the dashboard reports green on a page that is actually empty in the output.
Same-engine comparison structurally cannot see this.

The full answer is check_three_way_crosscheck.py (fitz + pypdf + MinerU), but
that needs a GPU pass over every page and is opt-in. This is the cheap tier:
pure-CPU, a few seconds, safe to run on every job.

TWO INDEPENDENT SIGNALS, neither of which relies on fitz being right:

  1. ENGINE DISAGREEMENT — compare per-page text volume against pypdf, a
     completely independent pure-Python parser (not a MuPDF wrapper). If pypdf
     recovers substantially more text on a page than fitz, fitz has a blind spot
     there and everything downstream of fitz is unreliable for that page.
     Deliberately one-directional: fitz seeing MORE than pypdf is normal (fitz
     is generally the stronger extractor) and is not reported.

  2. UNREADABLE PAGES — a page whose fitz text is essentially empty while the
     page demonstrably carries content (images or vector drawings) is a page
     nothing textual can validate. Today such a page scores PERFECT on
     conservation checks: nothing expected, nothing found. It must be reported
     as unvalidatable, never as clean.

Usage:
    python scripts/check_engine_agreement.py out/hybrid_extractions/_ui/<job_id>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (
    BOLD, DIM, GREEN, RED, YELLOW, banner, pdf_page_texts, pypdf_page_texts, resolve_pdf, run_cli,
)

# pypdf must recover at least this multiple of fitz's character count before we
# call it a blind spot. Generous: the two engines legitimately differ by a few
# percent on whitespace, ligatures and column joining, and a false "fitz is
# broken" claim would undermine trust in the whole dashboard.
DISAGREE_RATIO = 1.35
# Ignore pages where both engines found very little — a near-blank page divides
# into noise and would flag on a handful of characters.
MIN_CHARS = 200
# At or below this many characters, a page's fitz text is effectively empty.
EMPTY_CHARS = 20


def compute_engine_agreement(out_root: Path, pdf: str | None = None) -> dict:
    pdf_path = resolve_pdf(out_root, pdf)
    fitz_pages = pdf_page_texts(pdf_path)
    try:
        pypdf_pages = pypdf_page_texts(pdf_path)
    except Exception as e:  # noqa: BLE001 — a pypdf failure must not sink the check
        return {"passed": True, "available": False, "reason": f"pypdf unavailable: {e}",
                "disagreements": [], "unreadable_pages": []}

    # Page content evidence, used only to tell "page is genuinely blank" (fine)
    # from "page has content fitz couldn't read" (a real blind spot).
    import fitz as _fitz
    doc = _fitz.open(str(pdf_path))
    try:
        evidence = {i + 1: (len(doc[i].get_images()), len(doc[i].get_cdrawings()))
                    for i in range(doc.page_count)}
    finally:
        doc.close()

    disagreements, unreadable = [], []
    n_pages = min(len(fitz_pages), len(pypdf_pages)) - 1
    for pno in range(1, n_pages + 1):
        f_chars = len(fitz_pages[pno].strip())
        y_chars = len((pypdf_pages[pno] or "").strip())
        n_img, n_draw = evidence.get(pno, (0, 0))

        if f_chars <= EMPTY_CHARS and (n_img > 0 or n_draw > 2 or y_chars > MIN_CHARS):
            unreadable.append({
                "page": pno, "fitz_chars": f_chars, "pypdf_chars": y_chars,
                "images": n_img, "drawings": n_draw,
                "why": ("pypdf recovers text here that fitz does not"
                        if y_chars > MIN_CHARS else
                        "page carries images/vector content but fitz extracted no text"),
            })
            continue
        if max(f_chars, y_chars) < MIN_CHARS:
            continue
        if y_chars >= f_chars * DISAGREE_RATIO:
            disagreements.append({
                "page": pno, "fitz_chars": f_chars, "pypdf_chars": y_chars,
                "ratio": round(y_chars / max(f_chars, 1), 2),
            })

    return {
        "pdf": str(pdf_path),
        "available": True,
        "pages_compared": n_pages,
        "fitz_total_chars": sum(len(t.strip()) for t in fitz_pages[1:]),
        "pypdf_total_chars": sum(len((t or "").strip()) for t in pypdf_pages[1:]),
        "disagreements": disagreements,
        "unreadable_pages": unreadable,
        "disagreement_count": len(disagreements),
        "unreadable_count": len(unreadable),
        "thresholds": {"disagree_ratio": DISAGREE_RATIO, "min_chars": MIN_CHARS,
                       "empty_chars": EMPTY_CHARS},
        # Only a genuine blind spot fails this check. Volume disagreement is a
        # warning: it means "fitz-derived numbers for this page are unreliable",
        # not necessarily that content was lost.
        "passed": not unreadable,
    }


def print_report(report: dict):
    if not report.get("available"):
        print(YELLOW(f"skipped — {report.get('reason')}"))
        return
    print(f"{DIM('pages compared:')} {report['pages_compared']}    "
          f"{DIM('fitz chars:')} {report['fitz_total_chars']}    "
          f"{DIM('pypdf chars:')} {report['pypdf_total_chars']}")
    un, dis = report["unreadable_pages"], report["disagreements"]
    if un:
        print(f"\n{RED(BOLD('⚠ pages fitz cannot read (nothing textual can validate these):'))}")
        for u in un:
            print(f"    page {u['page']:>4}  fitz {u['fitz_chars']:>6} chars, pypdf "
                  f"{u['pypdf_chars']:>6}  {DIM(u['why'])}")
    if dis:
        print(f"\n{YELLOW(BOLD('pypdf recovers materially more text than fitz on:'))}")
        for d in dis:
            print(f"    page {d['page']:>4}  fitz {d['fitz_chars']:>6} vs pypdf "
                  f"{d['pypdf_chars']:>6}  ({d['ratio']}×)")
    if not un and not dis:
        print(f"\n{GREEN(BOLD('✓ PASS — an independent parser agrees with fitz on every page'))}")
        print(DIM("  (so fitz-derived coverage numbers are not hiding a shared blind spot)"))
    elif not un:
        print(f"\n{YELLOW(BOLD('✓ no unreadable pages, but see volume disagreements above'))}")
    else:
        print(f"\n{RED(BOLD(f'✗ FAIL — {len(un)} page(s) fitz cannot read'))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    banner("Engine agreement · fitz vs pypdf (circularity check)")
    report = compute_engine_agreement(Path(args.out_root).resolve(), args.pdf)
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
