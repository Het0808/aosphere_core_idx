#!/usr/bin/env python3
"""check_word_coverage.py — Test 1: whole-document word-multiset diff.

Tokenizes the source PDF and the extracted markdown tree into word counts
(Counter) and diffs them. Order-independent by design: a table or footnote
block that legitimately gets relocated during extraction should NOT show up
as a false positive here. What it catches:
  - MISSING words: present in the PDF, absent (or under-counted) in the tree
    -> real content loss.
  - EXTRA words: present in the tree, absent (or over-counted) in the PDF
    -> hallucination, or duplication (e.g. a rowspan cell repeated 3x).

It cannot tell you WHERE a miss happened — pair with check_content_localized.py
for that.

compute_word_coverage() is the importable core (used by hybrid_extract_ui.py
to run this right after Stage 3); main() is the CLI wrapper around it.

Usage:
    python scripts/check_word_coverage.py out/hybrid/172099
    python scripts/check_word_coverage.py out/hybrid/172099 --stage 1   # pre-table baseline
    python scripts/check_word_coverage.py out/hybrid/172099 --pdf some.pdf --json report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (
    BOLD, CYAN, DIM, GREEN, RED, YELLOW, banner,
    clean_markdown, detect_boilerplate_patterns, iter_content_files, manifest_tables,
    page_band_lines, pdf_page_texts, resolve_pdf, resolve_stage_dir, strip_boilerplate,
    strip_cover_disclaimer, strip_page_lines, summary_ai_route,
    table_header_repeats, tokenize, run_cli,
)


def compute_word_coverage(out_root: Path, pdf: str | None = None, stage: int = 3, min_coverage: float = 97.0) -> dict:
    pdf_path = resolve_pdf(out_root, pdf)
    tree_root = resolve_stage_dir(out_root, stage)

    pages = pdf_page_texts(pdf_path)
    band = page_band_lines(pdf_path)
    patterns, boilerplate_lines = detect_boilerplate_patterns(pages, band_lines=band)
    clean_pages = strip_boilerplate(pages, patterns, band_lines=band)
    # A multi-page table's header row is reprinted on every continuation page but the
    # reconstructed table carries it once — those reprints are not missing content.
    header_repeats = table_header_repeats(pdf_path, manifest_tables(out_root))
    before = sum(len(p.split()) for p in clean_pages[1:])
    clean_pages = strip_page_lines(clean_pages, header_repeats)
    # Prepared EXACTLY as check_text_conservation prepares it — the two must never
    # disagree about what counts as source text — so the omitted cover notice goes here
    # too. See lib_content_compare.strip_cover_disclaimer.
    clean_pages = strip_cover_disclaimer(clean_pages, summary_ai_route(out_root))
    header_repeat_words = before - sum(len(p.split()) for p in clean_pages[1:])

    pdf_counter_raw = Counter(tokenize("".join(pages[1:])))
    pdf_counter = Counter(tokenize("".join(clean_pages[1:])))

    files = list(iter_content_files(tree_root))
    md_tokens = []
    for f in files:
        md_tokens.extend(tokenize(clean_markdown(f.read_text(encoding="utf-8"))))
    md_counter = Counter(md_tokens)

    missing_real = pdf_counter - md_counter          # boilerplate already stripped from pdf side
    missing_raw = pdf_counter_raw - md_counter        # for the "raw" headline number only
    extra = md_counter - pdf_counter

    total_pdf = sum(pdf_counter_raw.values())
    total_missing_raw = sum(missing_raw.values())
    total_missing_real = sum(missing_real.values())
    total_boilerplate = sum(pdf_counter_raw.values()) - sum(pdf_counter.values())
    total_extra = sum(extra.values())

    coverage_raw = 100 * (total_pdf - total_missing_raw) / total_pdf if total_pdf else 100.0
    adj_denom = sum(pdf_counter.values())
    coverage_adj = 100 * (adj_denom - total_missing_real) / adj_denom if adj_denom else 100.0
    passed = coverage_adj >= min_coverage

    return {
        "pdf": str(pdf_path),
        "tree_root": str(tree_root),
        "files_scanned": len(files),
        "pdf_word_occurrences": total_pdf,
        "md_word_occurrences": sum(md_counter.values()),
        "coverage_raw_pct": round(coverage_raw, 3),
        "coverage_adjusted_pct": round(coverage_adj, 3),
        "boilerplate_lines": boilerplate_lines,
        "boilerplate_word_occurrences": total_boilerplate,
        "table_header_repeat_pages": len(header_repeats),
        "table_header_repeat_words": header_repeat_words,
        "missing_total_raw": total_missing_raw,
        "missing_total_real": total_missing_real,
        "extra_total": total_extra,
        "missing_words_real": dict(missing_real.most_common()),
        "extra_words": dict(extra.most_common()),
        "min_coverage": min_coverage,
        "passed": passed,
    }


def print_report(report: dict, top: int = 25):
    print(f"{DIM('pdf   :')} {report['pdf']}")
    print(f"{DIM('tree  :')} {report['tree_root']}")
    print(f"{DIM('files :')} {report['files_scanned']} content files scanned")
    print(f"\n{BOLD('pdf words')}         : {report['pdf_word_occurrences']}")
    print(f"{BOLD('md words')}          : {report['md_word_occurrences']}")
    bl = report["boilerplate_lines"]
    if bl:
        print(f"{BOLD('detected boilerplate')} : {DIM(str(len(bl)) + ' recurring header/footer line(s), ' + str(report['boilerplate_word_occurrences']) + ' word-occurrences (excluded below)')}")
        for b in sorted(bl, key=lambda x: -x["pages"])[:6]:
            print(DIM(f"    on {b['pages']} pages: \"{b['example'][:70]}\""))
    min_cov = report["min_coverage"]
    print(f"\n{BOLD('coverage (raw)')}      : {report['coverage_raw_pct']:.2f}%  {DIM('— includes boilerplate as missing')}")
    print(f"{BOLD('coverage (adjusted)')} : " + (GREEN if report['coverage_adjusted_pct'] >= min_cov else RED)(f"{report['coverage_adjusted_pct']:.2f}%")
          + DIM(f"  — excludes detected boilerplate, {report['missing_total_real']} real missing occurrences / {len(report['missing_words_real'])} distinct words"))
    print(f"{BOLD('extra')}               : " + (GREEN if report['extra_total'] == 0 else YELLOW)(f"{report['extra_total']} occurrences, {len(report['extra_words'])} distinct words"))

    if report["missing_words_real"]:
        print(f"\n{YELLOW('top missing, boilerplate excluded (in PDF, not in tree):')}")
        for w, c in list(report["missing_words_real"].items())[:top]:
            print(f"    {DIM(str(c).rjust(5))}  {w}")
    if report["extra_words"]:
        print(f"\n{YELLOW('top extra (in tree, not in PDF — possible duplication/hallucination):')}")
        for w, c in list(report["extra_words"].items())[:top]:
            print(f"    {DIM(str(c).rjust(5))}  {w}")

    print()
    if report["passed"]:
        print(GREEN(BOLD(f"✓ PASS — adjusted coverage {report['coverage_adjusted_pct']:.2f}% >= {min_cov}%")))
    else:
        print(RED(BOLD(f"✗ FAIL — adjusted coverage {report['coverage_adjusted_pct']:.2f}% < {min_cov}%")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root", help="pipeline output dir, e.g. out/hybrid/172099")
    ap.add_argument("--pdf", default=None, help="source PDF (default: inferred from stage1 manifest)")
    ap.add_argument("--stage", type=int, default=3, choices=[1, 2, 3], help="which stage's tree to check (default 3, final)")
    ap.add_argument("--top", type=int, default=25, help="how many missing/extra words to print")
    ap.add_argument("--min-coverage", type=float, default=97.0, help="fail below this coverage pct")
    ap.add_argument("--json", default=None, help="write full report to this path")
    args = ap.parse_args()

    out_root = Path(args.out_root).resolve()
    banner("Test 1 · word-multiset coverage")
    report = compute_word_coverage(out_root, args.pdf, args.stage, args.min_coverage)
    print_report(report, args.top)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))

    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
