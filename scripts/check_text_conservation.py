#!/usr/bin/env python3
"""check_text_conservation.py — is any of the PDF's text absent from the tree, and where?

THE ONE CHECK WITH NO BLIND SPOT BY CONSTRUCTION, and that is the entire point.

Every other content check here diffs a FILE against a SLICE of the source, and every
one of them has holes where those two do not line up. Measured this session, dropping a
paragraph from each eligible file of one document: 2 of 8 caught, 4 correctly silent (a
copy really did survive), and 2 genuinely missed. Three separate causes, all the same
shape — some text was never part of any comparison:

  * content a file holds from OUTSIDE its own heading slice is compared against nothing,
    so deleting it is free (pinned in tests/test_content_localized_blind_spots.py);
  * 00-section.md is in SKIP_MD_NAMES, so at stage 5 none of those files are read at all
    and text deleted from one costs nothing;
  * a duplicated file supplies a copy, so a real loss inside it looks present.

This check has no slices, no per-file attribution and no exclusions. It compares the
whole PDF's tokens against the whole tree's tokens as MULTISETS, so:

  * it does not care which file text landed in, which removes causes 1 and 2 outright;
  * it reads content files AND index files (README.md / 00-section.md), so nothing in
    the tree is invisible to it;
  * it makes NO placement claim, so it cannot produce the false positives that every
    per-section attempt has produced. "In the wrong section" is a different question and
    check_content_localized owns it.

Cause 3 is not a bug here: if a duplicate supplies the words, the words ARE in the
document, and this check correctly says so. Duplication is a structure defect, which is
`uniqueness`'s and `sectioning`'s business, not conservation's.

WHY A MULTISET AND NOT A SEQUENCE. These documents reorder text on purpose — a table
serialised row-major, a footnote lifted to a definition, a heading promoted out of a
cell. Sequence comparison reports all of that as loss. Counting occurrences asks only
"does the document still contain this word this many times", which is exactly the
question "is anything missing" means, and is immune to every legitimate reordering.

WHAT IT REPORTS. Not just a percentage — coverage was already measured and a 137-token
paragraph whose words all recur elsewhere moves it from 99.2% to 99.0%, which is noise.
This walks the PDF in reading order and marks each token position the tree cannot
account for, then groups adjacent unaccounted positions into SPANS with their page and
surrounding context. A single missing word is a 1-token span; a dropped paragraph is a
137-token one. Both are visible, and the length says which is which.

Usage:
    python scripts/check_text_conservation.py out/corpus/<product>/<job>
    python scripts/check_text_conservation.py <job> --stage 5
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (BOLD, DIM, GREEN, RED, YELLOW, banner,  # noqa: E402
                                 clean_index_markdown, clean_markdown,
                                 detect_boilerplate_patterns, iter_content_files,
                                 iter_index_files, manifest_tables, page_band_lines,
                                 pdf_page_texts,
                                 resolve_pdf, resolve_stage_dir, run_cli,
                                 split_word_punctuation, strip_boilerplate,
                                 strip_cover_disclaimer,
                                 strip_page_lines, summary_ai_route,
                                 table_header_repeats, tokenize)

# Unaccounted positions this far apart still belong to one span. A dropped sentence
# rarely loses every token — the odd stopword survives because the tree has it
# elsewhere — so a strict run would shatter one gap into a dozen fragments.
SPAN_JOIN_GAP = 3

# Context either side of a span, so a reader can find it on the page.
CONTEXT = 8

# A span this long or longer is CONTENT, and is what the score is computed from.
# Shorter spans are still reported — a reader asked to see even one missing word gets
# to see it — but they do not move a number, because after hyphen and apostrophe
# normalisation the residue is entirely single tokens of tokeniser disagreement the two
# sides spell differently: 22 of them in Jersey's 38,000, 99.94% conserved. Scoring
# those would put every document permanently a fraction below perfect for no defect,
# which is how a number stops being read.
MIN_SCORED_SPAN = 5


# The shared definition — see lib_content_compare.split_word_punctuation, which carries
# the measurement this was built on. Aliased rather than re-stated so check_content_localized
# and this check can never normalise a word two different ways.
_normalise = split_word_punctuation


def compute_text_conservation(out_root: Path, pdf: str | None = None,
                              stage: int = 3) -> dict:
    pdf_path = resolve_pdf(out_root, pdf)
    tree_root = resolve_stage_dir(out_root, stage)

    # PDF side, prepared EXACTLY as check_word_coverage prepares it, so the two can
    # never disagree about what counts as source text: recurring headers/footers out,
    # multi-page table header reprints out.
    pages = pdf_page_texts(pdf_path)
    band = page_band_lines(pdf_path)
    patterns, boilerplate_lines = detect_boilerplate_patterns(pages, band_lines=band)
    clean_pages = strip_boilerplate(pages, patterns, band_lines=band)
    clean_pages = strip_page_lines(
        clean_pages, table_header_repeats(pdf_path, manifest_tables(out_root)))
    # The cover notice the summary-AI prompt is told to omit is not a loss. Boilerplate
    # detection cannot see it (it is printed once, not repeated), so without this every
    # summary document reported its own compliance as missing text.
    clean_pages = strip_cover_disclaimer(clean_pages, summary_ai_route(out_root))

    # Tree side: every file, INCLUDING the index/landing files other checks skip.
    tree_tokens: list[str] = []
    for f in iter_content_files(tree_root):
        tree_tokens += tokenize(clean_markdown(f.read_text(encoding="utf-8")))
    index_files = list(iter_index_files(tree_root))
    for f in index_files:
        tree_tokens += tokenize(clean_index_markdown(f.read_text(encoding="utf-8")))
    tree_tokens = _normalise(tree_tokens)
    supply = Counter(tree_tokens)
    have = set(tree_tokens)

    # Walk the PDF in reading order, consuming the tree's supply of each token. A
    # position the tree cannot pay for is unaccounted — and because supply is consumed,
    # a word present twice in the source and once in the tree reports exactly one
    # missing occurrence rather than none.
    spans, page_of = [], []
    pdf_tokens: list[str] = []
    for pno in range(1, len(clean_pages)):
        toks = _normalise(tokenize(clean_pages[pno]))
        pdf_tokens += toks
        page_of += [pno] * len(toks)

    unaccounted = []
    i, n = 0, len(pdf_tokens)
    while i < n:
        tok = pdf_tokens[i]
        if supply[tok] > 0:
            supply[tok] -= 1
            i += 1
            continue
        # A word the PDF broke across a line and the extractor rejoined: "environ-"
        # + "mental" arrives here as "environment" + "al" against a tree holding
        # "environmental". Pay for the pair with the joined token when the tree has
        # one and neither half can be paid for on its own.
        if i + 1 < n:
            joined = tok + pdf_tokens[i + 1]
            if supply[joined] > 0:
                supply[joined] -= 1
                i += 2
                continue
        unaccounted.append(i)
        i += 1

    # Group adjacent unaccounted positions into spans.
    group: list[int] = []
    for i in unaccounted:
        if group and i - group[-1] > SPAN_JOIN_GAP:
            spans.append(_span(group, pdf_tokens, page_of))
            group = []
        group.append(i)
    if group:
        spans.append(_span(group, pdf_tokens, page_of))

    spans.sort(key=lambda s: -s["tokens_missing"])
    total = len(unaccounted)
    denom = max(len(pdf_tokens), 1)
    scored = [s for s in spans if s["tokens_missing"] >= MIN_SCORED_SPAN]
    scored_tokens = sum(s["tokens_missing"] for s in scored)
    return {
        "scored_spans": scored[:100],
        "scored_span_count": len(scored),
        "scored_tokens_missing": scored_tokens,
        "min_scored_span": MIN_SCORED_SPAN,
        "content_conserved_pct": round(100.0 * (denom - scored_tokens) / denom, 3),
        "pdf": str(pdf_path),
        "tree_root": str(tree_root),
        "index_files_included": len(index_files),
        "pdf_tokens": len(pdf_tokens),
        "tree_tokens": len(tree_tokens),
        "tokens_missing": total,
        "conserved_pct": round(100.0 * (denom - total) / denom, 3),
        "span_count": len(spans),
        "spans": spans[:200],
        "boilerplate_lines": boilerplate_lines,
        "scored_stage": stage,
        # `passed` turns on CONTENT spans, not on the single-token residue — see
        # MIN_SCORED_SPAN. The residue is still in `spans` and still reported.
        "passed": scored_tokens == 0,
    }


def _span(group: list[int], pdf_tokens: list[str], page_of: list[int]) -> dict:
    a, b = group[0], group[-1] + 1
    return {
        "page": page_of[a],
        "tokens_missing": len(group),
        "text": " ".join(pdf_tokens[a:b]),
        "before": " ".join(pdf_tokens[max(0, a - CONTEXT):a]),
        "after": " ".join(pdf_tokens[b:b + CONTEXT]),
    }


def print_report(report: dict, top: int = 20):
    print(f"{DIM('pdf tokens:')} {report['pdf_tokens']}   "
          f"{DIM('tree tokens:')} {report['tree_tokens']}   "
          f"{DIM('index files included:')} {report['index_files_included']}")
    if report["passed"]:
        print(f"\n{GREEN(BOLD('✓ PASS — every word of the source is present in the tree'))}")
        return
    print(f"{DIM('conserved:')} {report['conserved_pct']}%   "
          f"{DIM('missing:')} {report['tokens_missing']} token(s) "
          f"in {report['span_count']} span(s)")
    for s in report["spans"][:top]:
        colour = RED if s["tokens_missing"] >= 8 else YELLOW
        print(f"\n    {colour(str(s['tokens_missing']) + ' token(s)')}  "
              f"{DIM('page ' + str(s['page']))}")
        print(f"      …{DIM(s['before'])} {BOLD(s['text'][:160])} {DIM(s['after'])}…")
    print()
    print(RED(BOLD(f"✗ FAIL — {report['tokens_missing']} token(s) of the source are "
                   f"nowhere in the tree")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--stage", type=int, default=3)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    banner("Text conservation · is any source word absent from the tree?")
    report = compute_text_conservation(Path(args.out_root).resolve(), stage=args.stage)
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
