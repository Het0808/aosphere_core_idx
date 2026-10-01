#!/usr/bin/env python3
"""check_content_localized.py — Test 2: per-section sequence diff.

Test 1 (check_word_coverage.py) tells you THAT words are missing; this tells
you WHERE. For every content .md file, reads its "*Source: `x.pdf`, page A-B*"
breadcrumb, pulls the PDF's own text for exactly that page range, and runs a
token-level sequence diff (difflib) between the two. Contiguous dropped runs
of >= --min-gap tokens are reported with surrounding context.

Each finding is cross-referenced against the file's own [TABLE EXTRACTION
FAILED]/[TABLE PENDING] markers: if the file already carries one, the gap is
tagged ACKNOWLEDGED (a known, flagged limitation). If not, it's tagged SILENT
— content missing with no flag anywhere, which is the actionable case: either
a bug, or a section boundary that needs a wider page range.

Caveat: page ranges are section-level, not exact split points within a page,
so a real gap can appear right at a boundary just from where one section's
claimed range ends and the next begins. Short single-token diffs near a
boundary are noise; longer runs are signal.

Usage:
    python scripts/check_content_localized.py out/hybrid/172099
    python scripts/check_content_localized.py out/hybrid/172099 --min-gap 6 --json report.json
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (
    BOLD, DIM, GREEN, RED, YELLOW, banner,
    clean_index_markdown, clean_markdown, detect_boilerplate_patterns,
    has_flagged_gap_marker, iter_content_files, iter_index_files, manifest_tables,
    page_band_lines, parse_page_range, pdf_page_texts, resolve_pdf,
    resolve_stage_dir, split_word_punctuation, strip_boilerplate,
    table_header_units,
    tokenize, toc_page_numbers, run_cli,
)

CONTEXT = 6
RELOC_WINDOW = 8
RELOC_STRIDE = 4
RELOC_THRESHOLD = 0.6

# Last-resort search of the WHOLE tree before calling a span lost.
#
# The relocation pool above only covers this file plus siblings whose page range
# overlaps, so content rendered somewhere further away reads as missing even
# though it is plainly present. The commonest cause is a repeated table header:
# a multi-page comparison table reprints its column headers on every page (29
# times in the pilot document), the tree deliberately writes them ONCE, and the
# other 27 occurrences were reported as lost content.
#
# The bar here is deliberately much higher than RELOC_THRESHOLD: excusing a span
# because it turns up ANYWHERE costs sensitivity, so only a span found
# essentially in full is excused. A partial match stays reported, because that is
# the shape of a genuine partial loss.
PRESENT_ELSEWHERE_THRESHOLD = 0.9

# A span short enough that requiring EVERY token to individually exist somewhere
# in the tree is still a meaningful bar, not a coincidence. Measured on
# Jersey/181919: a document heading immediately followed by a table's own
# "Questions"/"Answers" header can never form one contiguous run once subchunking
# splits the section into separate nodes -- decomposed_coverage saw 5 of 7 tokens
# (the heading) and stopped, because "questions"/"answers" never sit next to that
# heading anywhere in the tree, only next to a DIFFERENT table's cells. And a
# glossary entry whose label and definition were transposed by Stage 1 fails the
# identical way: every word is in the file, just not in reading order. Capped
# short on purpose -- "every word in this span exists somewhere in the document"
# is unremarkable at 30 tokens (the document has thousands of words to draw from)
# and only becomes evidence of presence, not loss, when the span itself is this
# short.
REORDER_MAX_SPAN = 10

# ---- HOLLOW SECTIONS -----------------------------------------------------
#
# Everything above answers "is this text anywhere in the tree?". Nothing answers
# "does this section still hold its OWN body?" — and the two excuses above are
# exactly what hides the difference. A section whose rows MinerU merged into the
# table above it keeps its heading, keeps its page range, and keeps nothing else:
# every one of its words is found in the sibling, so `relocated` swallows the
# span and no dimension is charged. The words are all present, the document is
# 99% complete, and the node a reader clicks is empty.
#
# The `changed` bucket hides the same thing by a second route. find_gaps only
# ever penalised `delete` opcodes; when a file retains so little that its few
# scaffolding tokens align INSIDE the page, difflib emits `replace` instead, and
# `replace` was recorded as "reworded/reordered, lower severity" and read by no
# scorer at all. Measured on 124/ADGM__170680: section 8.9 Arranging is a
# 243-PDF-token `replace` against the single token "advice" — its entire body —
# and the document gated PASS at 95.7.
#
# So: measure what each file KEPT (tokens that matched the source on its own
# pages) against what is ABSENT from it, and require the absent body to be
# findable under a NAMED sibling before calling it hollow. That last condition is
# what separates this from content loss — the content is not gone, it is filed
# under the wrong heading — and it drops the cover pages, printed contents
# listings and genuinely-blank sections that keep nothing because there was
# nothing to keep.
# A RATIO, not a token count: how much of its own body the section still holds.
# An absolute bar was tried first and is not survivable — a section that kept
# nothing still matches the page furniture that survived boilerplate stripping
# plus its own title, which put 124/ADGM's section 7.3 at 125 "kept" tokens
# against a 1,690-token body it does not contain a word of.
HOLLOW_MAX_RETENTION = 0.15
HOLLOW_MIN_BODY = 150       # source tokens between this heading and the next
# Naming the absorber uses the same windowed measure the relocation excuse uses,
# for the same reason: the tree re-serializes a table row-major, so the body is
# present but never contiguous, and an exact-substring probe misses it (it missed
# 8.10, whose 1,347-token body is plainly inside 8.8's table).
HOLLOW_ATTRIB_RATIO = 0.6
# A section that declares its pages as snapshots is hollow only if another file holds at
# least this share of its body -- stricter than HOLLOW_ATTRIB_RATIO, because a snapshot
# declaration is the pipeline saying "I could not extract this", and that is only wrong
# when the extracted text is demonstrably sitting elsewhere in the tree.
HOLLOW_SNAPSHOT_MIN_ABSORBED = 0.9

# ---- restructured content: tables and footnotes --------------------------
#
# The two remaining false-positive classes have one cause: this check compares
# CONTIGUOUS word runs, and the pipeline deliberately RE-ORDERS two kinds of
# content, so a run comparison over them is invalid by construction.
#
#   TABLES    fitz reads a table's text roughly line-by-line across columns; the
#             tree serializes it row-major into HTML cells. A window spanning a
#             cell boundary therefore exists in the PDF and nowhere in the tree
#             even when every cell is present. Measured: 14 of 17 spans reported
#             lost on the table-heavy pilot document had 100% of their words in
#             the tree — ~82% false.
#   FOOTNOTES a page's footnote area runs "43 … 44 … 45 …" continuously; the tree
#             emits separate `[^n]: …` definitions, often in different files. A
#             window straddling two footnotes matches nothing. Measured: 140 of
#             288 pages flagged on a document whose word delta is 0.22%.
#
# So a span is tested against those two kinds of content AS UNITS — each table
# cell and each footnote definition is its own haystack, joined by a sentinel so
# no window can match across two units. A span whose windows are largely found
# INSIDE such units is restructured content, not loss.
#
# Direction matters. The first attempt slid an 8-token window from the span and
# looked for it INSIDE a unit — which is impossible whenever the unit is shorter
# than the window, and these units are short ("[^34]: Recital 26 UK GDPR" is four
# tokens). It therefore only ever matched the handful of long footnotes, which is
# why it reclassified 23 spans out of 191.
#
# The right direction is the reverse: look for each WHOLE unit inside the span,
# and measure how much of the span is accounted for. A span that is three
# concatenated footnote definitions is fully covered by three units; a prose span
# is covered by none. No window size is involved, so unit length is irrelevant.
UNIT_THRESHOLD = 0.5
_MIN_UNIT_TOKENS = 3   # shorter units ("No", "n/a") match by coincidence
_TABLE_BLOCK_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^[^\]]+\]:[ \t]*(.*)$", re.MULTILINE)
_FOOTNOTE_NUM_RE = re.compile(r"^\[\^(\d{1,4})\]:", re.MULTILINE)

# A footnote marker printed flush against the first word of its own body, which fitz
# reads as ONE token: "312Part 3, Section 16(1) DPA" -> ['312part', '3', ...]. The tree
# writes the same footnote as "[^312]: Part 3, Section 16(1) DPA" -> ['part', '3', ...],
# so the unit never matches and a footnote block that IS present reads as a silent gap.
# Measured on 155_Data_Privacy/Cayman Islands__174081: 26 of its 38 dropped spans, every
# one going from ratio 0.00 to 1.00 — i.e. not "closer to excused" but exactly covered.
_GLUED_MARKER_RE = re.compile(r"^(\d{1,4})([a-z][a-z'\-]*)$")


def footnote_marker_numbers(raw_md_texts: list[str]) -> set[str]:
    """The footnote numbers the tree actually DEFINES (`[^312]: ...`).

    This set is what makes the unglue safe. Splitting every digit-glued token was
    tried corpus-wide and reverted — it turned contents-page noise ("3Structure of
    Part B") into well-formed runs and invented gaps in documents whose TOC page is
    correctly not extracted (5 regressions in 57 documents, one review -> 0.0; see
    the NOTE in lib_content_compare.tokenize). Gating on a number the tree defines as
    a footnote confines the split to the one construct it was written for, and makes
    the check REFUSE to excuse a marker whose body never made it into the output."""
    nums: set[str] = set()
    for raw in raw_md_texts:
        nums.update(_FOOTNOTE_NUM_RE.findall(raw))
    return nums


def unglue_footnote_markers(tokens: list[str], marker_nums: set[str]) -> list[str]:
    """Split '312part' -> '312', 'part' when 312 is a footnote the tree defines.

    The marker digits are KEPT rather than dropped. They stay uncovered by the unit
    match (the unit is the body alone), so a one-footnote span scores len/(len+1)
    instead of 1.0 — deliberately under-excusing rather than over-excusing, with ample
    room under UNIT_THRESHOLD."""
    if not marker_nums:
        return tokens
    out: list[str] = []
    for t in tokens:
        m = _GLUED_MARKER_RE.match(t)
        if m and m.group(1) in marker_nums:
            out.append(m.group(1))
            out.append(m.group(2))
        else:
            out.append(t)
    return out


# A span that STARTS OR ENDS with a reprinted table header is the seam case named in
# relocation_ratio's note below: the PDF glues the header of a continuation page onto the
# first cell under it, and the tree holds the header once, at the top of the reconstructed
# table, far from that cell. No sliding window lies wholly inside either piece, so the
# ratio collapses even though every word is present.
#
# Measured on Germany 179874 page 29, where "Questions / Answers" is reprinted above the
# continuation of a question: the 15-token span scored 0.50 against its own file, one
# window short of RELOC_THRESHOLD, purely because RELOC_STRIDE=4 gives a 15-token span
# only two windows and the seam poisons one of them. Its 13-token tail scores 1.0.
#
# The units test below cannot rescue it either: this header is two one-word lines, under
# _MIN_UNIT_TOKENS, and 2 of 15 tokens would not reach UNIT_THRESHOLD if it were.
#
# Stripping happens on the SPAN, never on the pages — the distinction table_header_repeats
# insists on. The diff has already run; removing a prefix here cannot re-align it or
# invent a span elsewhere, exactly as unglue_footnote_markers cannot.
def _strip_reprinted_header(span: list[str], header_units: list[list[str]]) -> list[str]:
    """`span` with whole reprinted-header runs removed from its head and tail."""
    core = span
    for _ in range(2):                       # a span can carry one at each end
        before = core
        for u in header_units:
            n = len(u)
            if n and len(core) > n:
                if core[:n] == u:
                    core = core[n:]
                elif core[-n:] == u:
                    core = core[:-n]
        if core is before:
            break
    return core


def _units_ratio(span_tokens: list[str], units: list[list[str]]) -> float:
    """How much of `span_tokens` is accounted for by WHOLE units appearing
    contiguously inside it. 1.0 means the span is entirely made of table cells or
    footnote definitions the tree already contains."""
    if not units or not span_tokens:
        return 0.0
    span_hay = "\x01" + "\x01".join(span_tokens) + "\x01"
    first = set(span_tokens)
    covered = 0
    for u in units:
        if u[0] not in first:
            continue                      # cheap pre-filter over thousands of units
        if ("\x01" + "\x01".join(u) + "\x01") in span_hay:
            covered += len(u)
    return min(covered / len(span_tokens), 1.0)


def restructured_units(raw_md_texts: list[str]) -> tuple[list[list[str]], list[list[str]]]:
    """-> (table cells, footnote definitions) for the whole tree, each as its own
    token list.

    Built from RAW markdown, before clean_markdown flattens the HTML away, since
    the cell boundaries are exactly what has to be preserved here."""
    cells, notes = [], []
    for raw in raw_md_texts:
        for block in _TABLE_BLOCK_RE.findall(raw):
            for cell in _CELL_RE.findall(block):
                toks = tokenize(html.unescape(_TAG_RE.sub(" ", cell)))
                if len(toks) >= _MIN_UNIT_TOKENS:
                    cells.append(toks)
        for body in _FOOTNOTE_DEF_RE.findall(raw):
            toks = tokenize(html.unescape(_TAG_RE.sub(" ", body)))
            if len(toks) >= _MIN_UNIT_TOKENS:
                notes.append(toks)
    return cells, notes


def relocation_ratio(span_tokens, md_tokens) -> float:
    """Fraction of `span_tokens` (in sliding windows) that reappear as a
    contiguous run somewhere in `md_tokens` — high ratio means this span was
    RELOCATED within the file (classic footnote-reference vs. footnote-
    definition displacement), not actually dropped."""
    window = min(RELOC_WINDOW, len(span_tokens))
    if window == 0:
        return 1.0
    stride = max(1, min(RELOC_STRIDE, window))
    windows = [span_tokens[i:i + window] for i in range(0, len(span_tokens) - window + 1, stride)]
    if not windows:
        windows = [span_tokens]
    hay = "\x01" + "\x01".join(md_tokens) + "\x01"
    hits = sum(1 for w in windows if ("\x01" + "\x01".join(w) + "\x01") in hay)
    return hits / len(windows)


# The window measure above cannot see a span that STRADDLES A SEAM. These documents
# glue two kinds of text together in the PDF's reading order — a reprinted table header
# followed immediately by the first cell under it, a footnote reference followed by the
# next footnote's body — and the tree holds the two pieces far apart. No 8-token window
# lies wholly inside either piece, so the ratio collapses even though every word is
# present. Measured on the US States survey: the ten spans still reported after the
# header-unit fix scored 0.00-0.62 by window and 0.86-1.00 by decomposition, and each was
# text plainly present in the tree ("law and scope requirements" + "card debit card and or
# financial account number...", two runs, both there).
#
# So the span is covered greedily by MAXIMAL runs instead of fixed windows. Runs shorter
# than DECOMPOSE_MIN_RUN do not count: any two or three tokens ("of the", "and or") occur
# by coincidence in a document this size, and counting them would excuse anything.
DECOMPOSE_MIN_RUN = 4


def decomposed_coverage(span_tokens, hay: str, min_run: int = DECOMPOSE_MIN_RUN) -> float:
    """Fraction of `span_tokens` accounted for by maximal runs present in `hay`.

    `hay` is a sentinel-joined token string (see _hay), so a match is always on whole
    token boundaries. Presence is monotone in run length — if a run is present, so is
    every prefix of it — which is what makes bisection valid here instead of scanning
    every length at every position."""
    n = len(span_tokens)
    if not n or not hay:
        return 0.0
    covered, i = 0, 0
    while i <= n - min_run:
        if ("\x01" + "\x01".join(span_tokens[i:i + min_run]) + "\x01") not in hay:
            i += 1
            continue
        lo, hi = i + min_run, n          # lo is known present, hi is the open bound
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ("\x01" + "\x01".join(span_tokens[i:mid]) + "\x01") in hay:
                lo = mid
            else:
                hi = mid - 1
        covered += lo - i
        i = lo
    return covered / n


def _hay(tokens) -> str:
    return "\x01" + "\x01".join(tokens) + "\x01" if tokens else ""


# A printed contents page is NAVIGATION, and Stage 1 drops its rows on purpose
# (pdf2mdtree._is_toc_row) so that section titles are not duplicated as sections of
# their own. This validator used to bill that deliberate suppression as silent content
# loss: Bermuda__166524's `07-part-b-contents.md` keeps 10 words with 571 tokens of
# contents rows "missing", and 3 of its 8 silent gaps were contents listings.
#
# _is_toc_row cannot be reused here — it matches leader dots and trailing page numbers
# on a LINE, and by this point the span is normalised tokens with no punctuation. The
# token-level signature is a high density of bare integers that ASCEND, because a
# contents listing is title-then-page-number repeated with page numbers in order.
# Calibrated on Bermuda's nine dropped spans:
#
#   span kind   digit ratio   ascending ratio
#   contents    0.48 0.41 0.19   0.64 0.65 0.51
#   real prose  0.01 0.00 0.00   0.00 0.00 0.00
#   footer      0.33 0.20        0.00 0.00      <- ascending is what separates these
#
# Ascending is the discriminator: a footer's digits are a document reference and do not
# climb, a contents page's do.
NAV_MIN_TOKENS = 8
NAV_DIGIT_RATIO = 0.15
NAV_ASCENDING_RATIO = 0.4


def looks_like_contents_rows(span) -> bool:
    """Is this dropped span a printed table-of-contents listing?"""
    if len(span) < NAV_MIN_TOKENS:
        return False
    digits = [t for t in span if t.isdigit()]
    if len(digits) / len(span) < NAV_DIGIT_RATIO:
        return False
    # Page numbers only: a document-reference like "24320065" is not a page.
    pages = [int(t) for t in digits if len(t) <= 4]
    if len(pages) < 2:
        return False
    rising = sum(1 for a, b in zip(pages, pages[1:]) if b >= a) / (len(pages) - 1)
    return rising >= NAV_ASCENDING_RATIO


def find_gaps(pdf_tokens, md_tokens, min_gap, haystack_tokens=None, whole_tokens=None,
              table_units=None, note_units=None, note_nums=None, stats_out=None,
              header_units=None):
    """Diffs pdf_tokens against md_tokens (this file's own content, in order —
    the real comparison). haystack_tokens (defaults to md_tokens) is the wider
    pool searched when deciding whether a dropped span was actually relocated
    (e.g. into a sibling file sharing the same PDF page). whole_tokens is every
    token in the tree — the final check that content really is absent from the
    output rather than merely absent from nearby files.

    table_units / note_units: the tree's table cells and footnote definitions (see restructured_units) — content the pipeline
    re-orders on purpose, where a run comparison cannot be valid.

    stats_out: optional dict, filled with this file's RETENTION — how much of its
    own pages it kept vs. how much is absent from it, counted over every opcode
    rather than only the ones that became findings. See HOLLOW_MAX_KEPT: the
    buckets below say what happened to content, this says how much of the section
    is still in the section.

    -> (dropped, relocated, present_elsewhere, restructured, changed)."""
    haystack_tokens = md_tokens if haystack_tokens is None else haystack_tokens
    whole_hay = _hay(whole_tokens) if whole_tokens is not None else ""
    # Built once per file: the punctuation-insensitive last look below asks the ordinary
    # relocation question of a span whose hyphens and apostrophes have been spelled the
    # same way on both sides. Normalising per span would re-walk the whole haystack for
    # every finding.
    norm_haystack = split_word_punctuation(haystack_tokens)
    norm_whole = split_word_punctuation(whole_tokens) if whole_tokens is not None else None
    norm_whole_hay = _hay(norm_whole) if norm_whole is not None else ""
    whole_token_set = set(whole_tokens) if whole_tokens is not None else None
    sm = SequenceMatcher(a=pdf_tokens, b=md_tokens, autojunk=False)
    dropped, relocated, present_elsewhere, restructured, changed = [], [], [], [], []
    kept_n = absent_n = 0
    absent_spans: list[list[str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        # Retention accounting, independent of the finding buckets below: `equal`
        # is source text this file actually holds; `delete`/`replace` is source
        # text on its pages that it does not, whatever the reason turns out to be.
        if tag == "equal":
            kept_n += i2 - i1
        elif tag in ("delete", "replace"):
            absent_n += i2 - i1
            if (i2 - i1) >= min_gap:
                absent_spans.append(pdf_tokens[i1:i2])
        if tag == "delete" and (i2 - i1) >= min_gap:
            span = pdf_tokens[i1:i2]
            before = " ".join(pdf_tokens[max(0, i1 - CONTEXT):i1])
            after = " ".join(pdf_tokens[i2:i2 + CONTEXT])
            entry = {"tokens": span, "before": before, "after": after}
            if relocation_ratio(span, haystack_tokens) >= RELOC_THRESHOLD:
                relocated.append(entry)
                continue
            whole_ratio = (relocation_ratio(span, whole_tokens)
                           if whole_tokens is not None else 0.0)
            entry["found_ratio"] = round(whole_ratio, 2)
            if whole_ratio >= PRESENT_ELSEWHERE_THRESHOLD:
                present_elsewhere.append(entry)
                continue
            t_ratio = _units_ratio(span, table_units or [])
            # Only the FOOTNOTE test sees unglued tokens. Everything above — the
            # relocation window, the whole-tree search, the diff that produced this
            # span — still runs on the tokens as the PDF actually reads, so this can
            # move a span from `dropped` to `restructured` and can never create one.
            n_ratio = _units_ratio(unglue_footnote_markers(span, note_nums or set()),
                                   note_units or [])
            if max(t_ratio, n_ratio) >= UNIT_THRESHOLD:
                entry["unit_kind"] = "table cell" if t_ratio >= n_ratio else "footnote definition"
                entry["unit_ratio"] = round(max(t_ratio, n_ratio), 2)
                restructured.append(entry)
                continue
            # A reprinted header is restructured content in exactly the sense the two
            # unit tests above mean it, so it is asked here and not among the
            # last-resort tests below. Those only establish THAT a span's words are in
            # the tree; they cannot say why it moved, and the reader is shown the
            # difference — a span excused by them is reported as "found elsewhere",
            # i.e. content filed under the wrong heading, which this is not. Measured on
            # the 16-09-26 run: 79 of these seams were reported that way, on 42 of 54
            # documents, all of them the "Questions | Answers" column header reprinted
            # at a page break. Ordering it after the vaguer tests was an accident of the
            # order the tests were written in, not a judgement.
            #
            # It still cannot manufacture a finding: it only ever moves a span OUT of
            # `dropped`, and the diff that produced the span is untouched.
            core = _strip_reprinted_header(span, header_units or [])
            if len(core) < len(span) and len(core) >= min_gap:
                core_here = relocation_ratio(core, haystack_tokens)
                core_tree = (relocation_ratio(core, whole_tokens)
                             if whole_tokens is not None else 0.0)
                # ...and the seam-proof measure too, on the spelling-normalised tree, for
                # the same reason decomposed_coverage exists at all: a header reprint at a
                # page break is rarely the span's ONLY seam. The row it interrupts is
                # frequently split across the break as well, so what is left after the
                # strip is two runs rather than one and the window measure still collapses.
                # Measured on 124/Denmark 169459, the 29-token span
                # "questions answers details such as whether the restriction only applies
                # to investors that were the recipients of the cbdf pre marketing
                # activities or to all investors in your jurisdiction": every word is in
                # the tree, in two cells either side of the page break, and the stripped
                # core scores 0.40 by window against 0.96 by decomposition.
                core_dec = (decomposed_coverage(split_word_punctuation(core), norm_whole_hay)
                            if norm_whole is not None else 0.0)
                if (core_here >= RELOC_THRESHOLD or core_tree >= PRESENT_ELSEWHERE_THRESHOLD
                        or core_dec >= PRESENT_ELSEWHERE_THRESHOLD):
                    entry["unit_kind"] = "reprinted table header"
                    entry["unit_ratio"] = round(max(core_here, core_tree, core_dec), 2)
                    entry["header_tokens_stripped"] = len(span) - len(core)
                    restructured.append(entry)
                    continue
            # Last: the same "is it in the tree at all" question the window measure
            # already asked, but asked in a way a seam cannot defeat. The unit tests
            # above run first because they say WHY the text moved; this only says that
            # it did. Same threshold as the window measure — a span found essentially in
            # full is excused, a partly-found one stays reported, because that is the
            # shape of a genuine partial loss.
            if whole_tokens is not None:
                dec = decomposed_coverage(span, whole_hay)
                if dec >= PRESENT_ELSEWHERE_THRESHOLD:
                    entry["decomposed_ratio"] = round(dec, 2)
                    present_elsewhere.append(entry)
                    continue
                # decomposed_coverage needs a run of REORDER_MAX_SPAN-or-more matching
                # tokens in a row, so it cannot see a span whose words are all present
                # but never adjacent to each other in the tree -- a heading immediately
                # followed by a DIFFERENT node's table header, or a glossary label and
                # its definition printed out of order. Every token individually, not a
                # run of them, is the bar here -- see REORDER_MAX_SPAN for why that is
                # still meaningful evidence rather than coincidence.
                if (whole_token_set is not None and len(span) <= REORDER_MAX_SPAN
                        and all(t in whole_token_set for t in span)):
                    entry["reorder_note"] = "every word present, not contiguously"
                    present_elsewhere.append(entry)
                    continue
            # Deliberately suppressed navigation, not lost content. Filed as
            # restructured — the bucket for text the pipeline handles on purpose,
            # where a run comparison cannot be valid — so it stops reading as a
            # silent gap while staying visible and counted.
            if looks_like_contents_rows(span):
                entry["unit_kind"] = "printed contents row"
                restructured.append(entry)
                continue
            # Last of all, so it can only ever rescue a span that every other test has
            # already declined: a word the two sides spell differently is not a missing
            # word. Germany 179874 page 128 breaks a compound across a line -- "formal \u201cblack-/letter
            # law\u201d" -- so the PDF yields "black" + "letter" while the tree holds
            # "black-letter" whole, and a 14-token span every word of which is present
            # read as content loss. Splitting both sides settles it without deciding
            # which spelling is right.
            #
            # Placed among the last-resort tests, on the SPAN, so it can only move a span
            # out of `dropped` and never create one: the diff that produced this span ran
            # on the tokens as they actually read, and is untouched.
            # Unconditional: the span may be unchanged by normalising and still match,
            # because it is the HAYSTACK that carries the other spelling.
            norm_span = split_word_punctuation(span)
            if relocation_ratio(norm_span, norm_haystack) >= RELOC_THRESHOLD or (
                    norm_whole is not None
                    and relocation_ratio(norm_span, norm_whole) >= PRESENT_ELSEWHERE_THRESHOLD):
                entry["punctuation_note"] = "every word present; hyphen/apostrophe spelled differently"
                present_elsewhere.append(entry)
                continue

            dropped.append(entry)
        elif tag == "replace" and (i2 - i1) >= min_gap * 2:
            changed.append({
                "pdf_tokens": pdf_tokens[i1:i2],
                "md_tokens": md_tokens[j1:j2],
            })
    if stats_out is not None:
        stats_out.update({
            "kept_tokens": kept_n,
            "absent_tokens": absent_n,
            "largest_absent": max(absent_spans, key=len, default=[]),
        })
    return dropped, relocated, present_elsewhere, restructured, changed


_H1_RE = re.compile(r"^#\s+(.*)$", re.MULTILINE)


def _title_tokens(raw_md_text: str) -> list[str]:
    m = _H1_RE.search(raw_md_text)
    return tokenize(m.group(1)) if m else []


def _find_sub(hay: list[str], needle: list[str], start: int = 0) -> int | None:
    """Index of `needle` in `hay` at or after `start` (plain scan — needles here
    are heading-length, haystacks are one section's page range)."""
    if not needle or len(needle) > len(hay):
        return None
    first = needle[0]
    n = len(needle)
    for i in range(start, len(hay) - n + 1):
        if hay[i] == first and hay[i:i + n] == needle:
            return i
    return None


# A heading's NUMBER is not reliably adjacent to its words in the text layer. These
# documents set the clause number in a narrow left-hand column, so reading order can
# emit "4." and "[SECTION INTENTIONALLY LEFT BLANK]" as separate blocks with other
# text between them — page 16 of the Jersey MRAM memo tokenises as
# "... section intentionally left blank passive marketing reverse-enquiry ..." with
# neither number in the run, while page 76 of the same PDF has them inline
# ("9 section intentionally left blank 10 licence"). Anchoring on the tree's
# "# 4 [SECTION INTENTIONALLY LEFT BLANK]" therefore failed on 10 of this document's
# 16 headings, and a heading that cannot be located is a section that cannot be cut
# at its own boundaries.
#
# So the number is dropped and the title words retried. The words are what identify a
# heading; the number is the part the layout mangles. The >= 2 token floor still
# applies AFTER stripping, and the search stays inside one section's own page window,
# which is what keeps a repeated title (this document has two "[SECTION INTENTIONALLY
# LEFT BLANK]"s) from matching the wrong one.
_NUM_PREFIX_RE = re.compile(r"^[0-9][0-9.]*$")


def _title_needles(title_tokens: list[str]) -> list[list[str]]:
    """The title as written, then the title without its leading number."""
    needles = [title_tokens]
    i = 0
    while i < len(title_tokens) and _NUM_PREFIX_RE.match(title_tokens[i]):
        i += 1
    if i and len(title_tokens) - i >= 2:
        needles.append(title_tokens[i:])
    return [n for n in needles if len(n) >= 2]


# A PDF's text layer can put a SPACE INSIDE A WORD. These memos are typeset with
# per-character kerning on their headings, and the extractor faithfully reports what
# the layer says: page 4 of 124_Marketing_Restrictions/India__34712 reads
# "Pre -Market ing of Funds", tokenising as `pre` `market` `ing` against a tree
# holding `pre-marketing`. The same seam appears the other way round — the PDF spaces
# a compound the tree hyphenates, or vice versa — so neither a token-for-token scan
# nor a hyphen-splitting one can match both sides.
#
# What IS stable across the seam is the letters. A heading is matched here on its
# GLYPH RUN: drop the hyphens and apostrophes, concatenate, and ask whether the next
# few haystack tokens spell the same thing. "pre-marketing of funds" and
# "pre market ing of funds" both read `premarketingoffunds`.
#
# This runs ONLY after the exact scan has failed on every needle, so no heading that
# locates today can move. It cannot match by accident either: a whole heading's letter
# sequence reproducing itself is not something a document does twice by chance, and
# the search is still confined to the section's own page window.
#
# The cost of NOT doing this is not a cosmetic one. A heading that cannot be located is
# a section that never enters document order, so the PRECEDING section is cut at the
# wrong boundary and swallows it whole — on India, "Exchange Control Restrictions"
# absorbed the entire Pre-Marketing section and every word of it was then reported as a
# completeness gap, in text reproduced verbatim one file away.
_GLYPH_STRIP = str.maketrans("", "", "-'")
# A token the PDF split is still one word: allow it to arrive in several pieces, but
# not so many that the run stops being a heading-shaped thing.
LOOSE_EXTRA_TOKENS = 6


def _glyphs(tokens: list[str]) -> str:
    return "".join(tokens).translate(_GLYPH_STRIP)


def _loose_match_len(hay: list[str], needle: list[str], at: int) -> int | None:
    """How many `hay` tokens from `at` spell exactly `needle`, ignoring where the
    two sides happen to put their token boundaries. None if they do not."""
    want = _glyphs(needle)
    if not want:
        return None
    acc = ""
    for j in range(at, min(len(hay), at + len(needle) + LOOSE_EXTRA_TOKENS)):
        acc += _glyphs([hay[j]])
        if len(acc) >= len(want):
            return j - at + 1 if acc == want else None
        if not want.startswith(acc):
            return None
    return None


def _find_sub_loose(hay: list[str], needle: list[str], start: int = 0) -> int | None:
    for i in range(start, len(hay)):
        if _loose_match_len(hay, needle, i) is not None:
            return i
    return None


def _locate_title(hay: list[str], title_tokens: list[str], start: int = 0,
                  line_starts: set | None = None) -> int | None:
    """Position of this heading in `hay`, tolerating a detached number.

    Returns the index of the title WORDS (not of the number), which is what both
    boundary uses want: a section starts at its title and the previous one ends there.

    A LINE-START MATCH WINS over an earlier mid-line one. A heading is printed on its
    own line; the same words inside a sentence are a cross-reference, and taking the
    first match put sections in the wrong order whenever a document mentions a heading
    before reaching it. Measured on 124_Marketing_Restrictions/Poland__36411:
    "Advertising Requirements" is referred to twice in the body of page 6 (token 2578,
    2700) and is the heading on page 7 (token 3073); the file declares pages 6-7, so the
    first match won and the section was ordered before "Fly-in meetings" instead of
    after "Cold Calling Restriction". Every slice downstream was then cut at the wrong
    boundary — the section was compared against 278 tokens of intermediary text it has
    nothing to do with, reported HOLLOW at 7.6% retention, and its body was attributed
    to a file that does not contain a word of it.

    Falls back to the first match when nothing lands on a line start, so a heading the
    layout ran together with the preceding line is still found.
    """
    fallback = None
    for needle in _title_needles(title_tokens):
        i = _find_sub(hay, needle, start)
        while i is not None:
            if line_starts is None or i in line_starts:
                return i
            if fallback is None:
                fallback = i
            i = _find_sub(hay, needle, i + 1)
    if fallback is not None:
        return fallback
    # Nothing spelled the heading the way the tree spells it. Retry on letters alone,
    # which is the only thing a kerned text layer preserves (see _glyphs).
    for needle in _title_needles(title_tokens):
        i = _find_sub_loose(hay, needle, start)
        while i is not None:
            if line_starts is None or i in line_starts:
                return i
            if fallback is None:
                fallback = i
            i = _find_sub_loose(hay, needle, i + 1)
    return fallback


def _title_len(hay: list[str], title_tokens: list[str], at: int) -> int:
    """How many tokens of `title_tokens` actually matched at `at`."""
    for needle in _title_needles(title_tokens):
        if hay[at:at + len(needle)] == needle:
            return len(needle)
    # A heading located on letters alone spans a DIFFERENT number of haystack tokens
    # than it has words ("pre-marketing of funds" is five tokens of "Pre -Market ing of
    # Funds"), and this is what the body slice starts after — so it has to be the
    # matched extent, not the title's own length.
    for needle in _title_needles(title_tokens):
        n = _loose_match_len(hay, needle, at)
        if n is not None:
            return n
    return 0


def page_line_starts(page_text: str) -> set:
    """Token indices within this page that begin a LINE — where a heading sits.

    Tokenising line by line gives the same sequence as tokenising the page whole (a
    token cannot span a newline), so these indices line up with page_tokens exactly.

    A line start is necessary for a heading, not sufficient: a wrapped body line starts
    one too. Tightening it further (excluding lines that end in sentence punctuation)
    was tried against 124_Marketing_Restrictions/Poland__36411 and made that document's
    slices worse rather than better, so the weaker, checkable rule is what is kept."""
    n, out = 0, set()
    for line in (page_text or "").splitlines():
        lt = tokenize(line)
        if lt:
            out.add(n)
            n += len(lt)
    return out


def _document_order(entries: list[dict], page_tokens: list[list[str]],
                    line_starts: list[set] | None = None) -> None:
    """Locate every heading in the source and record where it falls, so each
    section can be cut at the heading that genuinely FOLLOWS it.

    Sets `abs_pos` on each entry (None when its title is not in the source).
    Ordering has to come from the source, not from the tree: file order is
    lexicographic, which puts 8.10 before 8.2, and cutting each section at
    whichever OTHER title happens to match earliest is worse still — one 1-token
    heading ("Into", a fragment of this corpus's cover page) matches inside
    ordinary prose and truncates every section in the document to nothing."""
    offset, n = [], 0
    for toks in page_tokens:
        offset.append(n)
        n += len(toks)
    for e in entries:
        a, b = e["pages"]
        window, starts, base = [], set(), 0
        for p in range(a, min(b, len(page_tokens) - 1) + 1):
            if line_starts:
                starts |= {base + s for s in line_starts[p]}
            window += page_tokens[p]
            base = len(window)
        i = _locate_title(window, e["title_tokens"], line_starts=starts or None)
        e["abs_pos"] = None if i is None else offset[a] + i


def _own_source_slice(pdf_tokens: list[str], entry: dict,
                      next_title: list[str] | None,
                      require_end: bool = True) -> list[str] | None:
    """The source text that belongs to THIS section: from its own heading up to
    the next heading in the document.

    This is what makes a hollow-section test possible at all. Measuring a section
    against its whole declared PAGE RANGE cannot work — several short subsections
    routinely share one page, so each reads as missing everything its siblings
    correctly hold, and 1.1 Introduction (which kept its 24 words) looks exactly
    like 8.9 Arranging (which kept none of its 219). Cutting at heading
    boundaries takes the siblings out of the comparison, so what remains is only
    ever this section's own body.

    Returns None when the heading cannot be located in this section's own pages —
    a normalised or reflowed title, a heading the tree invented. No slice, no
    finding: this check would rather miss a hollow section than invent one.

    require_end carries that same preference to the CLOSING boundary, and the two
    callers want opposite answers. When the following heading cannot be located the
    slice runs to the end of the page range, which is not this section's own body
    but its body plus everything after it — and a slice that is too WIDE means
    opposite things to the two checks:

      * the hollow test (require_end=True) would read a section as having lost a
        body it never had. That is how "[SECTION INTENTIONALLY LEFT BLANK]" came to
        be reported as hollow on ten documents: the blank section is bounded by
        "10 LICENCE", whose only anchor is a single word once the number is gone, so
        the slice swallowed the licence section, retention fell to 0%, and the body
        was duly "found" in 11-licence.md — satisfying all three conditions with a
        slice that was wrong. It abstains instead.
      * the gap diff (require_end=False) is no worse off than before: its fallback
        was the whole page range, and a slice missing only its end boundary is a
        subset of that. Losing the start boundary alone still removes the
        predecessor's text, which is most of what it was being charged for.

    A last section legitimately has no next_title; running to the end is correct
    there and both callers get the same slice."""
    if entry["abs_pos"] is None:
        return None
    start = _locate_title(pdf_tokens, entry["title_tokens"])
    if start is None:
        return None
    i = start + _title_len(pdf_tokens, entry["title_tokens"], start)
    end = len(pdf_tokens)
    if next_title:
        j = _locate_title(pdf_tokens, next_title, i)
        if j is None and require_end:
            return None
        if j is not None:
            end = j
    return pdf_tokens[i:end]


# The pipeline's own admission that a page could not be extracted. Rendered as an
# image with this line above it, so a reader sees the content is missing rather than
# being told nothing — which is what separates it from a silent gap.
_PAGE_SNAPSHOT_RE = re.compile(
    r"^>\s*Page \d+ of the source PDF contains a complex table/diagram", re.MULTILINE)


def _declares_snapshot_pages(raw_md_text: str) -> bool:
    return bool(_PAGE_SNAPSHOT_RE.search(raw_md_text))


# How much of a file's own text must be findable in the SOURCE, as contiguous runs,
# before it counts as having a body of its own. A section that genuinely lost its body
# holds a heading and a pipeline-written pointer ("the rows for this section are inside
# the table shown under 8.8") — text the PDF does not contain, so it scores near zero.
# A section that kept its body holds the document's own prose and scores near one.
HOLLOW_OWN_BODY_COVERAGE = 0.8


def _hollow_finding(rel: str, pages: tuple[int, int], own_slice: list[str],
                    md_tokens: list[str], others: dict[str, list[str]],
                    source_hay: str = "") -> dict | None:
    """A section node that kept its heading and lost its body to a sibling.

    Returns None unless all three hold: this section's own source slice is a body
    worth having, the file retains almost none of it, and that body is findable
    under a DIFFERENT file. The third condition is the load-bearing one — without
    it the check also fires on cover pages, printed contents listings and
    '[SECTION INTENTIONALLY LEFT BLANK]', which keep nothing because there was
    nothing to keep. With it, every hit is a section whose content is in the tree
    under the wrong heading: a real defect, and a fixable one."""
    if len(own_slice) < HOLLOW_MIN_BODY:
        return None
    # DOES THIS FILE HAVE A BODY OF ITS OWN, from the document? Asked before anything
    # else, because everything below is relative to `own_slice`, and the slice is only
    # as good as the heading placement and the page breadcrumb that produced it. When
    # either is wrong the section is measured against someone else's text and reported
    # empty while holding a full, correct body.
    #
    # Measured on 124_Marketing_Restrictions/Poland__36411, where every hollow finding
    # in a 43-document corpus came from this: 07-definitions/02-professional-investors
    # holds the complete MiFID II definition and its bullet list, and was reported at
    # 6.0% retention because its breadcrumb says page 7 while its body runs onto page 8;
    # 03-retail-investors holds "Means an investor which is not a Professional
    # Investor." — its entire, correct content — and was compared against 189 tokens of
    # cold-calling prose. Both name an `absorbed_by` file that contains none of their
    # text.
    #
    # "Published with no content of its own" has to mean the file has no content, not
    # that a computed slice failed to match it.
    if source_hay and decomposed_coverage(md_tokens, source_hay) >= HOLLOW_OWN_BODY_COVERAGE:
        return None
    sm = SequenceMatcher(a=own_slice, b=md_tokens, autojunk=False)
    kept = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in sm.get_opcodes() if tag == "equal")
    retention = kept / len(own_slice)
    if retention > HOLLOW_MAX_RETENTION:
        return None
    scored = sorted(
        ((relocation_ratio(own_slice, toks), k) for k, toks in others.items() if k != rel),
        reverse=True)
    holders = [(k, round(r, 2)) for r, k in scored if r >= HOLLOW_ATTRIB_RATIO]
    if not holders:
        return None
    return {
        "file": rel,
        "pages": [pages[0], pages[1]],
        "kept_tokens": kept,
        "body_tokens": len(own_slice),
        "retention_pct": round(100.0 * retention, 1),
        "absorbed_by": [k for k, _r in holders[:3]],
        "absorbed_ratio": holders[0][1],
    }


def compute_content_localized(out_root: Path, pdf: str | None = None, stage: int = 3, min_gap: int = 4) -> dict:
    pdf_path = resolve_pdf(out_root, pdf)
    tree_root = resolve_stage_dir(out_root, stage)

    pages = pdf_page_texts(pdf_path)
    band = page_band_lines(pdf_path)
    patterns, boilerplate_lines = detect_boilerplate_patterns(pages, band_lines=band)
    pages = strip_boilerplate(pages, patterns, band_lines=band)

    # A printed contents listing belongs to no section. Left in, every section whose
    # page range covers it is charged for not containing it — and stripping the
    # recurring header makes it worse, because the listing's rows then join into one
    # coherent run instead of header-separated fragments.
    toc_pages = toc_page_numbers(pdf_path)
    for pno in toc_pages:
        if 0 < pno < len(pages):
            pages[pno] = ""
    files = list(iter_content_files(tree_root))

    # Pass 1: load every file's page range + tokens. Several short subsections
    # commonly share one PDF page (e.g. 4.1..4.8 all landing on page 24) — a
    # per-file diff against that whole page would see each sibling as missing
    # everything the OTHER siblings actually captured. So relocation search
    # for a file's dropped span checks its own tokens PLUS every sibling file
    # whose declared page range overlaps this file's.
    loaded = []
    skipped_no_range = 0
    for f in files:
        raw = f.read_text(encoding="utf-8")
        rng = parse_page_range(raw)
        if not rng:
            skipped_no_range += 1
            continue
        a, b = rng
        b = min(b, len(pages) - 1)
        md_tokens = tokenize(clean_markdown(raw))
        loaded.append({"file": f, "raw": raw, "pages": (a, b), "md_tokens": md_tokens,
                       "title_tokens": _title_tokens(raw)})

    # Index/README files: not diffed, but their headings are real tree content
    # for the relocation search. A section's own title ("A. Substantial
    # Shareholding", "D. Issuer-initiated disclosure") lives only in its README
    # while sitting on a PDF page that belongs to a LEAF file's range — without
    # this pool every such ancestor title was reported as a silent gap.
    index_pool = []
    for p in iter_index_files(tree_root):
        raw = p.read_text(encoding="utf-8")
        rng = parse_page_range(raw)
        if not rng:
            continue
        a, b = rng
        index_pool.append({"pages": (a, min(b, len(pages) - 1)),
                           "md_tokens": tokenize(clean_index_markdown(raw)),
                           "title_tokens": _title_tokens(raw)})

    def overlaps(r1, r2):
        return r1[0] <= r2[1] and r2[0] <= r1[1]

    # Every token anywhere in the tree, including index files. Used only as the
    # last check before declaring a span lost (see PRESENT_ELSEWHERE_THRESHOLD).
    whole_tokens = [t for e in loaded for t in e["md_tokens"]]
    for idx in index_pool:
        whole_tokens.extend(idx["md_tokens"])
    # Table cells and footnote definitions, each kept as its own unit.
    table_units, note_units = restructured_units([e["raw"] for e in loaded])
    note_nums = footnote_marker_numbers([e["raw"] for e in loaded])
    # A multi-page table reprints its header row on every continuation page; the
    # reconstructed table carries it once. Those reprints are restructured content, not
    # loss — offered as units (never stripped from the pages, which would re-align the
    # diff and invent spans elsewhere; see table_header_repeats' docstring).
    table_units = table_units + table_header_units(pdf_path, manifest_tables(out_root))
    # The same reprinted headers with NO token floor, for the seam strip only. The floor
    # on table_units exists because a short unit ("No", "n/a") matches by coincidence
    # inside a span; these are matched only as a whole run at a span's very start or end,
    # and every one of them is geometry-verified as a header reprinted from that table's
    # own first page — so "Questions / Answers" is usable here and unusable above.
    header_units = table_header_units(pdf_path, manifest_tables(out_root), min_tokens=1)

    # Each file's own tokens as a searchable string, for naming the sibling a
    # hollow section's body ended up under (see HOLLOW_MAX_KEPT). Built once —
    # the answer "which node swallowed it" is what makes the finding fixable
    # rather than just a number.
    file_tokens = {str(e["file"].relative_to(tree_root)): e["md_tokens"] for e in loaded}

    # Where each heading actually sits in the source, so a section can be cut at
    # the one that follows it (see _document_order). Index/README headings are
    # boundaries too: the last subsection of "8 Marketing Activities" runs up to
    # "9 Licence", which is a README title.
    page_tokens = [tokenize(p) for p in pages]
    ordered = loaded + [{"pages": ix["pages"], "title_tokens": ix["title_tokens"]}
                        for ix in index_pool]
    _document_order(ordered, page_tokens, [page_line_starts(p) for p in pages])
    # The whole source as one run-searchable string, so the hollow test can ask whether
    # a file has a body of its own before judging it against a slice (see
    # HOLLOW_OWN_BODY_COVERAGE).
    source_hay = _hay([t for toks in page_tokens for t in toks])
    placed = sorted((e for e in ordered if e["abs_pos"] is not None),
                    key=lambda e: e["abs_pos"])
    next_title = {}
    for k, e in enumerate(placed):
        next_title[id(e)] = placed[k + 1]["title_tokens"] if k + 1 < len(placed) else None

    results = []
    hollow = []
    for entry in loaded:
        a, b = entry["pages"]
        pdf_chunk = "".join(pages[a:b + 1])
        pdf_tokens = tokenize(pdf_chunk)
        md_tokens = entry["md_tokens"]

        sibling_pool = list(md_tokens)
        for other in loaded:
            if other is not entry and overlaps(other["pages"], entry["pages"]):
                sibling_pool.extend(other["md_tokens"])
        for idx in index_pool:
            if overlaps(idx["pages"], entry["pages"]):
                sibling_pool.extend(idx["md_tokens"])

        rel = str(entry["file"].relative_to(tree_root))
        nxt = next_title.get(id(entry))
        own_slice = _own_source_slice(pdf_tokens, entry, nxt)
        gap_slice = _own_source_slice(pdf_tokens, entry, nxt, require_end=False)

        # Diff against the section's OWN body — its heading up to the heading that
        # genuinely follows it — not against its whole declared PAGE RANGE.
        #
        # This is the same argument _own_source_slice already makes for the hollow
        # test, and it applies with equal force here: a section's page range also
        # covers its NEIGHBOURS' text, and every word of that is charged to this
        # file as a gap it can never close. It shows up as short spans stitched out
        # of adjacent heading text — "[SECTION INTENTIONALLY LEFT BLANK] PASSIVE
        # MARKETING (REVERSE-ENQUIRY)" billed to two different files, or 02
        # Background accused of dropping "management advisory services investment"
        # while holding all four words many times over. None of the excusals above
        # can rescue them: they are 4-7 tokens assembled from two neighbours at
        # once, so no single contiguous run covers enough of the span, and lowering
        # the run floor to reach them would excuse genuine losses instead (measured:
        # a real 41-token dropped answer reaches 0.88 at a 2-token floor).
        #
        # Cutting at heading boundaries removes the cause rather than widening an
        # excuse, and it does NOT soften a real gap: content still inside a
        # section's own body is still compared. The page-range chunk stays the
        # fallback for a heading that cannot be located in the source, where no
        # slice can be trusted — no slice, no change in behaviour.
        if gap_slice is not None:
            pdf_tokens = gap_slice

        stats: dict = {}
        dropped, relocated, present_elsewhere, restructured, changed = find_gaps(
            pdf_tokens, md_tokens, min_gap,
            haystack_tokens=sibling_pool, whole_tokens=whole_tokens,
            table_units=table_units, note_units=note_units, note_nums=note_nums,
            stats_out=stats, header_units=header_units)

        # A section that DECLARED its pages as snapshots is not a hollow one. Where
        # the pipeline cannot extract a page it says so in the output ("complex
        # table/diagram; snapshot for reference") and renders the page as an image:
        # the content is declared missing, not silently placed elsewhere, which is
        # the same distinction the dropped/acknowledged split already turns on.
        # (has_flagged_gap_marker covers only the TABLE markers, and widening it
        # would move the silent/acknowledged line for the gap check on every
        # document, so this asks the narrower question separately.)
        #
        # The hollow test had no such exemption, so widening the set of locatable
        # headings made it fire on exactly the sections carrying those markers:
        # Bermuda's "5 Passive Marketing", whose seven pages are all snapshots, and
        # Kuwait's "[SECTION INTENTIONALLY LEFT BLANK]", which has no body to lose
        # in the first place. Neither is a section whose content went under the wrong
        # heading, which is the only thing this finding is allowed to mean.
        acknowledged = has_flagged_gap_marker(entry["raw"])
        if own_slice is not None:
            h = _hollow_finding(rel, (a, b), own_slice, md_tokens, file_tokens,
                                source_hay)
            # The exemption above holds only while the snapshots are the content's
            # LAST resort. When another file holds (nearly) all of the section's body,
            # the pages were not unextractable -- the table was extracted and filed
            # under the wrong heading, and the snapshots are just what was left behind
            # (Spain__176285, Norway__171186: "10 LICENCE" is snapshots only while its
            # whole 26-row table sits at the end of "9 PROSPECTUS REGULATION"). Measured
            # over 29 documents this adds exactly those two findings, and the
            # all-snapshot sections the exemption was written for (Bermuda "5 Passive
            # Marketing") have no holder, so they stay exempt.
            if h and (not _declares_snapshot_pages(entry["raw"])
                      or h["absorbed_ratio"] >= HOLLOW_SNAPSHOT_MIN_ABSORBED):
                hollow.append(h)

        if not (dropped or relocated or present_elsewhere or restructured or changed):
            continue
        results.append({
            "file": rel,
            "pages": [a, b],
            "kept_tokens": stats.get("kept_tokens", 0),
            "absent_tokens": stats.get("absent_tokens", 0),
            "acknowledged": acknowledged,
            "dropped": dropped,
            "relocated": relocated,
            "present_elsewhere": present_elsewhere,
            "restructured": restructured,
            "changed": changed,
        })
    total_files = len(loaded) + skipped_no_range

    silent = [r for r in results if not r["acknowledged"] and r["dropped"]]
    ack = [r for r in results if r["acknowledged"] and r["dropped"]]
    passed = len(silent) == 0

    return {
        "pdf": str(pdf_path),
        "tree_root": str(tree_root),
        "files_scanned": total_files,
        "skipped_no_range": skipped_no_range,
        "boilerplate_lines": boilerplate_lines,
        "results": results,
        "silent_count": len(silent),
        "acknowledged_count": len(ack),
        "total_dropped_spans": sum(len(r["dropped"]) for r in results),
        "total_relocated_spans": sum(len(r["relocated"]) for r in results),
        # Reported separately, NOT as loss: found in full elsewhere in the tree —
        # overwhelmingly a table header the tree correctly writes once while the
        # PDF reprints it on every page of the table.
        "total_present_elsewhere_spans": sum(len(r.get("present_elsewhere", [])) for r in results),
        # Content the pipeline re-orders by design (table cells, footnote
        # definitions). Reported, never counted as loss.
        "total_restructured_spans": sum(len(r.get("restructured", [])) for r in results),
        "total_changed_spans": sum(len(r["changed"]) for r in results),
        # Sections present in the tree by name and empty in substance — their body
        # is under a named sibling (see _hollow_finding). Deliberately NOT folded
        # into `passed` or into any count above: nothing here is lost content, and
        # re-using the loss counters would both change what every existing number
        # means and mis-describe the defect. It is a SECTIONING failure — the tree
        # divided, then filed the content under the wrong heading — and it is
        # scored as its own dimension (check_scorecard._score_sectioning).
        "hollow_sections": hollow,
        "hollow_count": len(hollow),
        "sections_checked": len(loaded),
        "passed": passed,
    }


def print_report(report: dict):
    print(f"{DIM('pdf   :')} {report['pdf']}")
    print(f"{DIM('tree  :')} {report['tree_root']}")
    if report["boilerplate_lines"]:
        print(DIM(f"stripped {len(report['boilerplate_lines'])} recurring header/footer line(s) before diffing"))
    print(f"{DIM('files :')} {report['files_scanned']} scanned, {report['skipped_no_range']} skipped (no page breadcrumb)")

    results = report["results"]
    silent = [r for r in results if not r["acknowledged"] and r["dropped"]]
    ack = [r for r in results if r["acknowledged"] and r["dropped"]]

    print(f"\n{BOLD('files with a dropped span')}  : {len([r for r in results if r['dropped']])} "
          f"({DIM(str(len(ack)) + ' acknowledged')}, "
          + (RED if silent else GREEN)(f"{len(silent)} silent"))
    print(f"{BOLD('total dropped spans')}       : {report['total_dropped_spans']}")
    print(f"{BOLD('total relocated spans')}     : {report['total_relocated_spans']}  {DIM('(found elsewhere in the same file — e.g. footnote def moved from its inline ref; not a real loss)')}")
    print(f"{BOLD('total reworded/changed')}    : {report['total_changed_spans']}  {DIM('(likely reordering, lower severity)')}")
    n_hollow = report.get("hollow_count", 0)
    print(f"{BOLD('hollow sections')}           : "
          + (RED if n_hollow else GREEN)(str(n_hollow))
          + f"  {DIM('(heading kept, body is under another node)')}")
    for h in report.get("hollow_sections", []):
        a, b = h["pages"]
        print(f"  {RED('HOLLOW')} {h['file']} {DIM(f'pages {a}-{b}')} — kept "
              f"{h['retention_pct']}% of its {h['body_tokens']}-token body; "
              f"the rest is under {h['absorbed_by'][0]}")

    if silent:
        print(f"\n{RED(BOLD('⚠ SILENT gaps (no failure/pending marker in the file — investigate):'))}")
        for r in silent:
            print(f"\n  {BOLD(r['file'])}  {DIM('pages ' + str(r['pages'][0]) + '-' + str(r['pages'][1]))}")
            for d in r["dropped"]:
                snippet = " ".join(d["tokens"][:20]) + (" …" if len(d["tokens"]) > 20 else "")
                n = len(d["tokens"])
                tag = YELLOW(f"[{n} tokens dropped]")
                print(f"    {tag} …{d['before']} {RED(snippet)} {d['after']}…")

    if ack:
        print(f"\n{GREEN(BOLD('✓ ACKNOWLEDGED gaps (file already has a TABLE EXTRACTION FAILED/PENDING marker):'))}")
        for r in ack:
            print(f"  {DIM(r['file'])}  pages {r['pages'][0]}-{r['pages'][1]}  ({len(r['dropped'])} dropped span(s))")

    print()
    if report["passed"]:
        print(GREEN(BOLD("✓ PASS — no silent (unflagged) content gaps found")))
    else:
        print(RED(BOLD(f"✗ FAIL — {report['silent_count']} file(s) have silent content gaps")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--stage", type=int, default=3, choices=[1, 2, 3])
    ap.add_argument("--min-gap", type=int, default=4, help="min consecutive dropped tokens to report")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    out_root = Path(args.out_root).resolve()
    banner("Test 2 · section-localized content diff")
    report = compute_content_localized(out_root, args.pdf, args.stage, args.min_gap)
    print_report(report)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))

    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
