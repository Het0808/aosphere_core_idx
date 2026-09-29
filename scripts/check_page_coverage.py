#!/usr/bin/env python3
"""check_page_coverage.py — Test 10: is every PAGE of the source represented?

The gap this closes
-------------------
Every other completeness signal is relative to what the extractor decided to look
at. Word coverage diffs the whole PDF against the whole tree, but a document whose
extractor skipped 90% of the pages still produces a tree that is internally
consistent, and the per-section checks only inspect sections that EXIST — a page no
section ever claimed is invisible to them.

Curaçao 164302 is the worked example: pdf2mdtree started at the first bookmark on
page 37 of 40, so pages 1-36 were never read. Page health reported "20 ok, 20
silent" and the section checks found one section, clean. Nothing said "36 pages are
not in this document at all".

So this walks the PAGES, not the sections, and asks of each one: does the extracted
tree account for it? Two independent ways, because either alone has a blind spot:

  declared  — some content file's "pages N-M" range covers it. Structural, cheap,
              and exactly what a skipped page range fails.
  present   — a distinctive sample of the page's own words is findable in the tree
              text. Catches a page that a section CLAIMS but silently dropped.

A page counts as covered if either holds. Pages with no extractable text (blank
separators, pure images) are counted apart and never penalised: there is no text to
be missing, and unreadable-but-image pages are already scored by the unreadable-page
term in completeness.

compute_page_coverage() is the importable core (lib_validate runs it as one of the
checks); main() is the CLI wrapper.

Usage:
    python scripts/check_page_coverage.py out/corpus/104_Shareholding_Disclosure/Curaçao__164302
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (  # noqa: E402
    BOLD, DIM, GREEN, RED, YELLOW, banner, clean_index_markdown, clean_markdown,
    iter_content_files, iter_index_files, parse_page_range, pdf_page_texts, resolve_pdf,
    resolve_stage_dir, run_cli, tokenize,
)

# A page needs enough of its own words to be worth asserting anything about. Below
# this it is a cover sheet or a section divider, and a "missing" verdict on it would
# be noise rather than a finding.
MIN_PAGE_TOKENS = 12
# Share of a page's sampled distinctive words that must be findable in the tree for
# the page to count as present. Not 100%: hyphenation, ligatures and table
# reflowing legitimately change a few tokens per page.
PRESENT_RATIO = 0.5
# How many of a page's rarest words to look for. Rare words are the discriminating
# ones — "the" appears in every tree ever written.
SAMPLE_WORDS = 8
# Cover sheet and table of contents are not body content and no tree extracts them,
# so the leading pages are excused. CAPPED, and additionally capped at the first page
# any section claims: a document whose extractor started at page 37 must not have
# pages 1-36 excused as "front matter" — that is the very failure this test exists to
# catch. Two pages is a cover plus a TOC.
FRONT_MATTER_MAX = 2


def _page_fingerprint(tokens: list[str], global_freq: dict[str, int]) -> list[str]:
    """The page's most distinctive words: longest-and-rarest first, deduped.

    The word itself is the final sort key so the ordering is TOTAL. Without it the key
    ties constantly — most words on a page occur once and share a length — and since the
    candidates come out of a set, whose iteration order depends on PYTHONHASHSEED, the
    sample differed from run to run. A page whose hit rate sits on PRESENT_RATIO then
    flipped between present and missing by luck: Shareholding Disclosure Denmark 167272
    scored page coverage 100% on five hash seeds and 98.7% on the sixth, moving its
    completeness 97.8 <-> 96.5 with no change to the document or the code."""
    uniq = {t for t in tokens if len(t) >= 5}
    return sorted(uniq, key=lambda w: (global_freq.get(w, 0), -len(w), w))[:SAMPLE_WORDS]


def compute_page_coverage(out_root: Path, pdf: str | None = None, stage: int = 3,
                          min_coverage: float = 100.0) -> dict:
    pdf_path = resolve_pdf(out_root, pdf)
    tree_root = resolve_stage_dir(out_root, stage)
    pages = pdf_page_texts(pdf_path)          # 1-based: pages[0] is unused padding
    n_pages = max(len(pages) - 1, 0)

    # what the tree says it covers, and what it actually contains.
    #
    # INDEX FILES COUNT HERE. iter_content_files skips README.md and 00-section.md
    # because they are not DIFF targets — a generated link index has no page range of
    # its own to diff, and a subchunked section's landing page holds a body that
    # belongs to its children. Neither reason applies to this question. This one asks
    # "does the tree account for this page ANYWHERE", and a stage-5 00-section.md is
    # very much somewhere: it carries the section's own prose and the deferred MinerU
    # table, and it is the only file declaring that section's page range.
    #
    # Marketing Restrictions Hungary 167023 is the worked example. Section 7's
    # 00-section.md holds a 100-row table spanning pages 47-63 and declares
    # "page 47-64"; nothing else in the tree declares 48-52. Skipping the file took
    # both the declaration and the text out of this check at once, so page 52 — whose
    # content is right there in the shipped tree, on the page the reader is looking at —
    # was reported as accounted for nowhere, and charged to completeness as a silent
    # loss. Pages 48-51 scraped over PRESENT_RATIO on incidental overlap with other
    # sections and were merely mislabelled "present but not declared".
    declared: set[int] = set()
    tree_tokens: set[str] = set()
    seen: set[Path] = set()
    for f, cleaner in ([(f, clean_markdown) for f in iter_content_files(tree_root)]
                       + [(f, clean_index_markdown) for f in iter_index_files(tree_root)]):
        if f in seen:
            continue
        seen.add(f)
        raw = f.read_text(encoding="utf-8", errors="ignore")
        rng = parse_page_range(raw)
        if rng:
            declared.update(range(rng[0], rng[1] + 1))
        tree_tokens.update(tokenize(cleaner(raw)))

    page_tokens = {p: tokenize(pages[p]) for p in range(1, n_pages + 1)}
    global_freq: dict[str, int] = {}
    for toks in page_tokens.values():
        for t in set(toks):
            global_freq[t] = global_freq.get(t, 0) + 1

    first_declared = min(declared) if declared else n_pages + 1
    front_matter = list(range(1, min(FRONT_MATTER_MAX, first_declared - 1) + 1))

    covered, uncovered, blank, present_only, declared_only = [], [], [], [], []
    for p in range(1, n_pages + 1):
        if p in front_matter:
            continue
        toks = page_tokens[p]
        if len(toks) < MIN_PAGE_TOKENS:
            blank.append(p)                    # nothing to be missing
            continue
        sample = _page_fingerprint(toks, global_freq)
        hits = sum(1 for w in sample if w in tree_tokens)
        present = bool(sample) and hits / len(sample) >= PRESENT_RATIO
        is_declared = p in declared
        if present or is_declared:
            covered.append(p)
            if present and not is_declared:
                present_only.append(p)
            elif is_declared and not present:
                declared_only.append(p)
        else:
            uncovered.append(p)

    checkable = len(covered) + len(uncovered)
    pct = round(100.0 * len(covered) / checkable, 2) if checkable else 100.0
    return {
        "pdf": str(pdf_path),
        "tree_root": str(tree_root),
        "pages": n_pages,
        "pages_with_text": checkable,
        "pages_blank_or_image": blank,
        "pages_front_matter": front_matter,
        "pages_covered": len(covered),
        # THE number: a page in here has text in the PDF that the tree accounts for
        # nowhere — neither claimed by a section nor findable in the output.
        "pages_missing": uncovered,
        "missing_count": len(uncovered),
        # claimed by a section's page range but its words are not in the output:
        # the section exists and silently dropped the page's content
        "pages_declared_not_present": declared_only,
        # in the output but no section claims the page — usually a page range the
        # extractor never wrote, not a loss
        "pages_present_not_declared": present_only,
        "coverage_pct": pct,
        "passed": pct >= min_coverage,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--stage", type=int, default=3)
    ap.add_argument("--min-coverage", type=float, default=100.0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    r = compute_page_coverage(Path(args.out_root), args.pdf, args.stage, args.min_coverage)
    banner("Test 10 — page coverage (is every page of the source represented?)")
    col = GREEN if r["passed"] else RED
    blank_n = len(r["pages_blank_or_image"])
    front_n = len(r["pages_front_matter"])
    covered, checkable, pct = r["pages_covered"], r["pages_with_text"], r["coverage_pct"]
    excused = DIM(f"({blank_n} blank/image, {front_n} front matter, not scored)")
    print(f"  pages                {BOLD(str(r['pages']))}   {excused}")
    print(f"  covered              {col(f'{covered} of {checkable}  ({pct}%)')}")
    if r["pages_missing"]:
        print(f"  {RED('MISSING pages')}        {r['pages_missing']}")
    if r["pages_declared_not_present"]:
        print(f"  {YELLOW('claimed but empty')}    {r['pages_declared_not_present']}")
    if args.json:
        Path(args.json).write_text(json.dumps(r, indent=2))
        print(f"\n  wrote {args.json}")


if __name__ == "__main__":
    run_cli(main)
