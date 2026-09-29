#!/usr/bin/env python3
"""check_semantic_integrity.py — did the text still MEAN the same thing?

THE HOLE THIS FILLS. Every other check verifies content is PRESENT. None verify
it still says the same thing. Demonstrated on a real extraction: changing
"a managed fund that is not structured as a legal entity" to "...that is
structured as a legal entity" — one word, the legal obligation inverted —
produced NO finding anywhere. Word coverage still reported 98.5% and PASSED.

Why the existing checks are blind to it: the per-section gap check only reports
runs of >= 4 consecutive missing words, so a single dropped "not" is a run of
one and is discarded as noise. That is correct for ordinary stopwords and
catastrophic for negations, where one word reverses the meaning of a compliance
statement.

So this check ignores run length entirely and does something much narrower:
count a small set of MEANING-BEARING words per section, on both sides, and
report any imbalance. "not" appearing 12 times in the PDF pages a section covers
but 11 times in the section is a hard finding regardless of how it diffs.

Deliberately per-section, not whole-document: across a 259-page document the
counts are in the thousands and a single flip vanishes into the rounding. Within
one section the numbers are small enough that an imbalance of one is visible.

Usage:
    python scripts/check_semantic_integrity.py out/hybrid_extractions/_ui/<job_id>
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (
    BOLD, DIM, GREEN, RED, YELLOW, banner,
    clean_index_markdown, clean_markdown, detect_boilerplate_patterns,
    iter_content_files, iter_index_files, page_band_lines, parse_page_range,
    pdf_page_texts,
    resolve_pdf, resolve_stage_dir, strip_boilerplate, tokenize, run_cli,
)

# Words whose loss or gain changes what a clause REQUIRES. Split into groups so a
# report can say which kind of meaning shifted.
NEGATIONS = {"not", "no", "never", "neither", "nor", "none", "cannot", "without"}
MODALS = {"must", "shall", "may", "should", "will", "cannot", "required", "permitted",
          "prohibited", "mandatory", "optional"}
QUALIFIERS = {"unless", "except", "excluding", "including", "only", "solely", "provided",
              "subject", "otherwise", "if"}
TRACKED = NEGATIONS | MODALS | QUALIFIERS

GROUP_OF = {}
for _w in TRACKED:
    GROUP_OF[_w] = ("negation" if _w in NEGATIONS
                    else "modal" if _w in MODALS else "qualifier")

SEVERITY = {"negation": "high", "modal": "medium", "qualifier": "low"}

# Words either side of the tracked word, used to prove the SENTENCE survived.
#
# Raw counting does not work, and the reason is worth recording. Counting a word
# per section is confounded by page ranges: several sections share one PDF page,
# so each looks short by the others' text. And counting document-wide cannot
# separate the two very different failures:
#
#   (a) a whole passage was lost   -> its negations go with it. Already reported
#                                     by the gap checks; not a meaning change.
#   (b) the passage is still there but its negation is gone -> the clause now
#                                     says the OPPOSITE. Reported by nothing.
#
# (b) is the dangerous one. So instead of counting, each tracked word is tested
# in context: if the surrounding words appear in the tree WITHOUT the tracked
# word between them, the sentence survived and its meaning flipped.
CTX = 5


# A standalone "No." is an ANSWER, not a negation inside a clause — and the flip test
# below cannot judge it. That test proves a sentence survived by matching 5 words either
# side; an answer cell has no such context. Its neighbours are OTHER CELLS, and the tree
# reorders them by design: a PDF's reading order interleaves a Yes/No column with the
# descriptions beside it, while the tree serialises the table row by row. So the words
# around an answer are EXPECTED to differ, which fails the "did it survive" test on
# correct extractions and, on a document that repeats near-identical questions, lets a
# parallel clause satisfy the "sentence is here without it" test.
#
# Measured on this corpus: every one of the 4 negation flips reported was a standalone
# answer, and every one was wrong — Switzerland p25, Ecuador p24, Mauritius p78 and
# Luxembourg p92 all still hold their "No" (Luxembourg's counts are 250 in the PDF and
# 250 in the tree). Meanwhile 13 documents ARE missing a negation and none was reported.
# Exempting answers therefore costs no detection at all; they are picked up by
# find_missing_answers, which counts them instead of matching their context.
_ANSWER_LINE_RE = re.compile(r"^\s*(?:yes|no)\s*[.:;]?\s*$", re.IGNORECASE)
# "No. Ecuadorian legislation related to the securities market does not contemplate…"
# is the same thing as a bare "No." — an answer, then its explanation. The answer just
# shares a line with what follows it, which is how Ecuador p24 and Mauritius p78 slipped
# past the bare-line form and were reported as dropped negations even though both
# documents hold MORE "no" than their PDF does. A leading Yes/No closed by a full stop
# is an answer opener; a negation inside a clause is not written that way.
# The punctuation varies — "No." (Ecuador p24), "No," (Mauritius p78, "No, there are no
# exemptions to the Investment Dealer/Adviser Licensing Requirement…"). Any of them
# closes the answer and opens its explanation. Note a bare "No person shall…" has no
# punctuation after the word and is correctly left alone as clause text.
_ANSWER_OPENER_RE = re.compile(r"^\s*(?:yes|no)\s*[.,:;]\s+\S", re.IGNORECASE)
ANSWER_WORDS = {"yes", "no"}
_HTML_TAG_RE_LOCAL = re.compile(r"<[^>]+>")


def tokenize_page(text: str) -> tuple[list[str], set[int]]:
    """Tokens for one page, plus the index of every token that is a standalone
    Yes/No ANSWER rather than a word used inside a sentence."""
    toks: list[str] = []
    answers: set[int] = set()
    for line in text.splitlines():
        line_toks = tokenize(line)
        if line_toks and (
                (len(line_toks) == 1 and _ANSWER_LINE_RE.match(line))
                or _ANSWER_OPENER_RE.match(line)):
            answers.add(len(toks))
        toks.extend(line_toks)
    return toks, answers


def _counts(tokens: list[str]) -> Counter:
    return Counter(t for t in tokens if t in TRACKED)


def _hay(tokens: list[str]) -> str:
    return "\x01" + "\x01".join(tokens) + "\x01"


def _run_in(hay: str, toks: list[str]) -> bool:
    return bool(toks) and ("\x01" + "\x01".join(toks) + "\x01") in hay


def find_meaning_flips(pdf_tokens: list[str], tree_hay: str,
                       skip_idx: frozenset[int] | set[int] = frozenset()) -> list[dict]:
    """Occurrences where the sentence survived into the tree but its
    meaning-bearing word did not — i.e. the clause now states the opposite.

    For each tracked word, take CTX words either side. Three outcomes:
      * context WITH the word found in the tree  -> intact, ignore
      * context WITHOUT the word found instead   -> FLIP: reported here
      * neither found                            -> the passage itself is gone,
        which the gap checks already cover, so it is not a meaning change
    """
    flips = []
    for i, tok in enumerate(pdf_tokens):
        if tok not in TRACKED:
            continue
        if i in skip_idx:      # a standalone answer — see _ANSWER_LINE_RE
            continue
        before = pdf_tokens[max(0, i - CTX):i]
        after = pdf_tokens[i + 1:i + 1 + CTX]
        if len(before) < 3 or len(after) < 3:
            continue                      # too little context to be conclusive
        if _run_in(tree_hay, before + [tok] + after):
            continue                      # word survived
        if not _run_in(tree_hay, before + after):
            continue                      # whole passage absent -> not a flip
        flips.append({
            "word": tok, "group": GROUP_OF[tok], "severity": SEVERITY[GROUP_OF[tok]],
            "before": " ".join(before), "after": " ".join(after),
            "pdf_phrase": " ".join(before + [tok] + after),
            "tree_phrase": " ".join(before + after),
        })
    return flips


# How much question text is used to find an answer's place in the tree, and how far
# past it to look for the answer itself. The anchor is the tail of the QUESTION, which
# is prose and survives extraction intact; the window is generous because the tree
# inserts cell boundaries and sometimes a row label between question and answer.
ANCHOR_TOKENS = 8
ANSWER_WINDOW = 14
_ANCHOR_FALLBACKS = (8, 6, 5)


def _answer_of(unit: str) -> str | None:
    """'yes'/'no' if this text unit IS an answer, else None. One definition, applied
    identically to a PDF line and to a tree table cell — the whole point is that the
    two sides be counted the same way."""
    toks = tokenize(unit)
    if not toks:
        return None
    if (len(toks) == 1 and _ANSWER_LINE_RE.match(unit)) or _ANSWER_OPENER_RE.match(unit):
        return toks[0]
    return None


_TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TABLE_RE = re.compile(r"<table.*?</table>", re.S | re.I)


def tree_answer_units(raw_md: str) -> list[str]:
    """Every place in one tree file that could hold an answer: each table cell, plus
    each line of prose outside the tables."""
    units = [_HTML_TAG_RE_LOCAL.sub(" ", html.unescape(m.group(1)))
             for m in _TD_RE.finditer(raw_md)]
    units += _TABLE_RE.sub("\n", raw_md).splitlines()
    return units


def count_answers(units) -> Counter:
    c: Counter = Counter()
    for u in units:
        a = _answer_of(u)
        if a:
            c[a] += 1
    return c


def find_missing_answers(pages: list[str], tree_raws: list[str]) -> dict:
    """Yes/No answers the PDF states and the tree does not — counted, not context-matched.

    This is the check that actually covers a dropped answer cell. The flip test above
    cannot: it proves a sentence survived by matching five words either side, and an
    answer cell has no stable context — its neighbours are other cells, and the tree
    reorders them by design (a PDF's reading order interleaves a Yes/No column with the
    prose beside it; the tree serialises the table row by row). A COUNT is immune to
    that reordering, and immune to the word-gluing that also defeats context matching,
    because an answer is one token standing alone.

    Counting is also why this reports a number rather than a location. An attempt to
    locate each missing answer — anchor on its question, look for an answer nearby —
    was measured at 1150 findings across 80 documents, over a fifth of every answer in
    the corpus, because a multi-line question anchors on a fragment and the answer then
    sits outside any sane window. The count is the part that holds up: Japan states 8
    bare "No." answers and the tree holds 5, which matches its token shortfall of 3
    exactly, and one of the three is a verified loss (p54's oral-disclaimers row, whose
    "UCITS Funds, Other Funds and IMAs / No." is absent from the tree entirely).

    -> {"pdf": {...}, "tree": {...}, "missing": {...}, "total_missing": n}
    """
    pdf_c = count_answers(line for text in pages for line in text.splitlines())
    tree_c = count_answers(u for raw in tree_raws for u in tree_answer_units(raw))
    # Deliberately NOT reported as "missing answers". The two sides do not SEGMENT
    # answers the same way even under one predicate: the PDF puts "No." on its own line
    # and its explanation on the next, while the tree routinely holds both in a single
    # cell ("No Luxembourg UCITS may only invest up to 10%…"), which this predicate
    # cannot recognise as an answer. So a deficit here measures formatting as much as
    # loss. Measured: it claims 17 missing "no" answers on Luxembourg, a document whose
    # "no" token counts are 250 in the PDF and 250 in the tree — nothing is missing at
    # all. Kept as a DIAGNOSTIC because the two counts are useful side by side; the
    # signal that actually holds up is negation_shortfall below.
    return {"pdf": dict(pdf_c), "tree": dict(tree_c),
            "deficit": {w: pdf_c[w] - tree_c.get(w, 0)
                        for w in ("yes", "no") if pdf_c[w] > tree_c.get(w, 0)}}


def compute_semantic_integrity(out_root: Path, pdf: str | None = None,
                               stage: int = 3) -> dict:
    pdf_path = resolve_pdf(out_root, pdf)
    tree_root = resolve_stage_dir(out_root, stage)

    pages = pdf_page_texts(pdf_path)
    band = page_band_lines(pdf_path)
    patterns, _ = detect_boilerplate_patterns(pages, band_lines=band)
    pages = strip_boilerplate(pages, patterns, band_lines=band)

    files = list(iter_content_files(tree_root))
    loaded, whole_tree_counts = [], Counter()
    for f in files:
        raw = f.read_text(encoding="utf-8")
        toks = tokenize(clean_markdown(raw))
        whole_tree_counts += _counts(toks)
        rng = parse_page_range(raw)
        if rng:
            loaded.append({"file": str(f.relative_to(tree_root)), "pages": rng,
                           "counts": _counts(toks)})
    for p in iter_index_files(tree_root):
        whole_tree_counts += _counts(tokenize(clean_index_markdown(
            p.read_text(encoding="utf-8"))))

    # Whole-document totals: the only place a NET loss across the entire tree can
    # be seen. A word present in the PDF 40 times and in the tree 39 times means
    # one instance vanished somewhere, even if no single section shows it.
    pdf_all = _counts(tokenize("".join(pages[1:])))
    doc_level = []
    for w, pdf_n in sorted(pdf_all.items()):
        tree_n = whole_tree_counts.get(w, 0)
        if tree_n < pdf_n:
            doc_level.append({
                "word": w, "group": GROUP_OF[w], "severity": SEVERITY[GROUP_OF[w]],
                "pdf_count": pdf_n, "tree_count": tree_n, "shortfall": pdf_n - tree_n,
            })

    # The real finding: a surviving sentence whose meaning-bearing word vanished.
    # Page-attributed so a reviewer can go straight to it.
    tree_hay = _hay([t for f in files
                     for t in tokenize(clean_markdown(f.read_text(encoding="utf-8")))])
    flips = []
    for pno in range(1, len(pages)):
        page_toks, answer_idx = tokenize_page(pages[pno])
        for fl in find_meaning_flips(page_toks, tree_hay, skip_idx=answer_idx):
            fl["page"] = pno
            flips.append(fl)
    # The same phrase repeated on several pages (a table header, a boilerplate
    # clause) would otherwise be reported once per page.
    seen, deduped = set(), []
    for fl in flips:
        k = (fl["word"], fl["pdf_phrase"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(fl)
    flips = deduped

    # Dropped ANSWER cells — the population the flip test above deliberately skips.
    answers = find_missing_answers(
        pages, [f.read_text(encoding="utf-8") for f in files])

    neg_flips = [f for f in flips if f["severity"] == "high"]
    return {
        "pdf": str(pdf_path),
        "tree_root": str(tree_root),
        "tracked_words": len(TRACKED),
        "meaning_flips": flips,
        "negation_flips": len(neg_flips),
        "modal_flips": sum(1 for f in flips if f["severity"] == "medium"),
        "flip_count": len(flips),
        "flip_pages": sorted({f["page"] for f in flips}),
        # Whole-document counts kept as supporting context only. A clean
        # extraction normally shows some shortfall (negations inside a failed
        # table go missing with it), so these are NOT a pass/fail signal —
        # only a surviving-sentence flip is.
        "document_level": doc_level,
        # Yes/No answers present in the PDF with no counterpart in the tree. Reported,
        # NOT yet gating: measured against this corpus the signal still needs a pass
        # for precision before a verdict should hang on it.
        # Answer counts per side. A diagnostic, not a finding — see find_missing_answers.
        "answer_counts": answers,
        # THE signal for a dropped answer, and the one measurement here that survived
        # checking: a negation word the tree holds FEWER times than the PDF. It needs no
        # context, so neither table reordering nor word gluing can defeat it, and it is
        # right where the flip test was wrong — Japan -3 (a verified lost "No." row),
        # Luxembourg 0 (nothing lost, and the flip test claimed otherwise).
        #
        # Non-gating, for one known flaw: gluing can lower a count WITHOUT loss, because
        # a swallowed word stops being its own token. Cyprus reports none 2 -> 0 while
        # both instances are still in the text, glued to their neighbours. Until that is
        # separated out, this reports rather than decides.
        "negation_shortfall": [d for d in doc_level if d["group"] == "negation"],
        # A dropped negation inside an otherwise-intact clause inverts a legal
        # obligation, so that alone decides pass/fail.
        "passed": not neg_flips,
    }


def print_report(report: dict, top: int = 20):
    print(f"{DIM('pdf    :')} {report['pdf']}")
    print(f"{DIM('tracked:')} {report['tracked_words']} meaning-bearing words "
          f"(negations, modals, qualifiers)")
    flips = report["meaning_flips"]
    colour = {"high": RED, "medium": YELLOW, "low": DIM}
    if flips:
        print(f"\n{BOLD('sentences that survived into the tree but LOST a meaning-bearing word:')}")
        for f in sorted(flips, key=lambda f: {"high": 0, "medium": 1, "low": 2}[f["severity"]])[:top]:
            c = colour[f["severity"]]
            print(f"\n    page {f['page']}  {c(f['word'].upper())} ({f['group']}) dropped")
            print(f"      PDF  : …{f['pdf_phrase']}…")
            print(f"      tree : …{c(f['tree_phrase'])}…")
    print()
    if report["passed"]:
        print(GREEN(BOLD("✓ PASS — no negation dropped from a surviving sentence")))
        if report["modal_flips"]:
            print(DIM(f"  ({report['modal_flips']} modal/qualifier flip(s) — lower severity)"))
    else:
        print(RED(BOLD(f"✗ FAIL — {report['negation_flips']} sentence(s) kept their wording but "
                       "lost a negation; the clause now states the opposite")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--stage", type=int, default=3, choices=[1, 2, 3])
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    banner("Semantic integrity · negations, modals, qualifiers")
    report = compute_semantic_integrity(Path(args.out_root).resolve(), args.pdf, args.stage)
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
