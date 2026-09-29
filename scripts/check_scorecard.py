#!/usr/bin/env python3
"""check_scorecard.py — one at-a-glance verdict for an extraction job.

Aggregates every existing check (word coverage, per-section gaps, numeric
integrity, table placement, Stage 2 geometric table matching, Stage 3 combine)
into five scored dimensions plus a single gate: PASS / REVIEW / FAIL.

Two design decisions worth understanding before reading the numbers:

1. THE GATE IS THE WORST DIMENSION, NOT THE AVERAGE. For legal/compliance
   documents an average is a vanity metric — a 99%-complete extraction that
   dropped one risk-disclosure table is a failed extraction, and averaging lets
   four green dimensions hide it.

2. SILENT LOSS IS PENALIZED FAR MORE THAN FLAGGED LOSS (see SILENT_WEIGHT /
   FLAGGED_WEIGHT). The pipeline's core philosophy is that a table it couldn't
   extract is marked in the output, never dropped quietly. If the score
   punished a visible `[TABLE EXTRACTION FAILED]` marker as hard as a silent
   disappearance, the rational move would be to hide failures — so flagged
   losses keep partial credit for being honest and reviewable.

Scoring is derived from signals the pipeline ALREADY produces — this module
computes no new analysis of its own. Thresholds (see GATE_PASS/GATE_REVIEW) are
provisional: they are reasonable defaults, NOT calibrated against a
human-labelled gold set, so treat a score as "where to look first", not as a
measurement. See docs/GEOMETRY_MATCHING_DESIGN.md.

Usage:
    python scripts/check_scorecard.py out/hybrid_extractions/_ui/<job_id>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib_dismissals as dis
import product_rules as pr
from lib_content_compare import (BOLD, DIM, GREEN, RED, YELLOW, StageDirectoryNotFoundError,
                                  banner, parse_page_range, resolve_pdf, resolve_stage_dir,
                                  run_cli)

# A loss the reader can SEE in the output (a failure marker, a page snapshot, a
# gap the file already acknowledges) still costs points — it is still missing
# content — but only a fraction of what a silent disappearance costs.
SILENT_WEIGHT = 1.0
FLAGGED_WEIGHT = 0.25

# Weighted the same as a digit transposition in Integrity, and for the same
# reason: both silently change what a clause REQUIRES. One flip drops a clean
# document to REVIEW, two to FAIL — an inverted obligation must not be able to
# pass, but a single one is a review item rather than an automatic rejection.
NEGATION_FLIP_PENALTY = 15.0

# A discrete, per-instance loss the same way a negation flip is — see
# find_empty_answer_segments — but a smaller one: a dropped short boilerplate
# answer ("N/a. Please see X above.") is a real gap, not an inverted legal
# obligation, so it costs a fraction of what a negation flip does rather than
# being ignored (the coverage-percentage term alone cannot see a handful of
# missing words on a multi-thousand-word document).
EMPTY_ANSWER_SEGMENT_PENALTY = 5.0

# check_source_fidelity.py's cell-cited findings (section_boundary_leak,
# duplicated_content, the table_shape_defects group, missing_cell_answer) are each
# confirmed against the source PDF, not a proxy — so unlike the rate-based terms
# elsewhere in this file, ONE of these should cost the same whether it is the only
# defect in a 20-cell document or one of hundreds in a 600-cell one. A rate-based
# cost buried a confirmed defect under a near-100 score on any document past a
# couple hundred cells (measured: 1 of 423 -> -0.24pts, unreadable as a signal).
# 4.0/defect was tried first and reverted: real documents routinely carry 5-10+
# genuine table-shape findings, which at 4.0 each (capped at 10) drove most of a
# 14-document corpus straight to review/fail on volume alone — steeper than
# intended. 1.0/defect, still capped, is deliberately conservative for now.
CELL_DEFECT_PENALTY = 1.0
CELL_DEFECT_CAP = 10        # -> at most 10 points from any one finding kind

GATE_PASS = 90      # every CRITICAL dimension at or above this -> PASS
GATE_REVIEW = 70    # every CRITICAL dimension at or above this -> REVIEW, else FAIL

# Only these set the verdict. The question this dashboard exists to answer is
# "is a whole paragraph or a whole table missing / in the wrong place?" — that
# is what makes an extraction unusable. Number-level and duplication findings
# are still scored and shown, but they must not drive the gate: percentage and
# footnote-marker noise is expected in this corpus, and letting it set the
# verdict trains everyone to ignore the verdict.
# "toc" gates because a document whose structure came from an outline that does
# not describe it is unusable as a tree however complete its text is — and no
# other dimension can see that: they all compare output against the PDF, and the
# words are all present, just never divided into the document's real sections.
# Failing is also what makes it actionable: it is the trigger the fallback chain
# needs to try the printed contents page (see mineru_fallback.should_fallback).
# Gates on the OUTCOME — what we produced — never on the input.
#   completeness  are the words there?
#   structure     did it actually divide into usable sections?
#   placement/fidelity  are tables where they belong, and intact?
#
# `toc` is deliberately NOT here. It describes the SOURCE of the structure ("this
# PDF has no bookmarks"), which is a fact about the input, not about our output.
# Measured across 113 documents: the 39 with no outline at all had median
# completeness 90.2 against 94.3 for those with a proper one — the font-size
# heuristic handles them. Gating on it failed 39 documents that had extracted
# correctly (Australia 141949: completeness 96.8, fidelity 99.5, marked FAIL) and
# sent them through a MinerU re-parse that read 9.8% of the document. It is kept as
# a DIAGNOSIS — it explains why a bad result is bad, and picks the remedy — but it
# never sets a verdict.
# `sectioning` REPLACED `structure` here. They asked overlapping questions and
# neither answer was the one a reader needs: `structure` tested only whether the
# tree divided AT ALL — one chunk holding >=80% of the words — so a document that
# divided into 41 sections with five of them empty scored 100.0. "Did it divide"
# is the degenerate case of "do the sections hold their own content", so the blob
# test now lives inside _score_sectioning as a hard floor and nothing it used to
# catch can slip through.
#
# The structure PROFILE is still computed and still stored at the top level of
# the scorecard — mineru_fallback.should_fallback and the dashboard's cohort
# panel read it from there, not from this dimension, so the fallback chain is
# untouched.
CRITICAL_DIMENSIONS = ("completeness", "placement", "fidelity", "sectioning")
ADVISORY_DIMENSIONS = ("uniqueness", "integrity", "ai_postprocess")

# ---- SHORT-DOCUMENT MODE -----------------------------------------------------
# A document this short is scored on WORD COVERAGE ALONE, and routed straight to
# MinerU rather than through outline/heuristic structuring. Imported from
# check_structure_profile so routing, splitting and the structure test can never
# disagree about what "short" means — they did, and 8-9 page documents failed for
# being the single chunk the splitter had deliberately given them.
#
# Why: the structural dimensions need a document long enough to HAVE a structure.
# On a 6-page memo there is no meaningful hierarchy to get right, often no printed
# contents page, and frequently no tables — so `toc` and `fidelity` measure noise
# and then fail the document on it. Worse, the silent-gap term below is a ratio of
# FILES: a short document extracted as one file scores 100 x 1/1 on a single
# imperfect span and lands on zero, while the same three spans across a 60-file
# tree cost under two points. Measured on 172_G20/Brazil__182791: 97.2% word
# coverage, every word present, completeness scored 0.0.
#
# So below this page count: gate on completeness only, and compute completeness
# without the file-ratio terms that a one-file tree cannot survive.
# A tree with this few files has no per-section ratio worth computing — see the
# note in _score_completeness. The MinerU last-resort tier always produces one.
UNCHUNKED_MAX_FILES = 2

# A failed structure scores here: below GATE_REVIEW so it fails, but not 0, because
# the document may still be perfectly complete — the verdict should say "unusable
# shape", not "nothing extracted".
STRUCTURE_FAIL_SCORE = 40.0

from check_structure_profile import BLOB_MIN_PAGES as SHORT_DOC_MAX_PAGES  # noqa: E402
from product_rules import DEFAULT_SECTION_DEPTH  # noqa: E402
SHORT_DOC_DIMENSIONS = ("completeness",)

# Per-table credit toward the Fidelity score. A table MinerU never produced is
# still flagged in the tree, so it keeps FLAGGED_WEIGHT credit for honesty.
CREDIT_CONFIDENT = 1.0     # geometric IoU match above the confident bar
CREDIT_UNCERTAIN = 0.6     # matched, but low overlap / possible split-or-merge
CREDIT_RANK_FALLBACK = 0.75  # no bbox to match on (multi-page table) — unverified, not wrong
CREDIT_RECLASSIFIED = 1.0  # correctly identified as prose, not a table
# A merged page-spanning table is now VERIFIED rather than assumed: its region's
# text is measured against the table cells the tree actually holds (see
# check_table_presence._verify_continuations). Verified means nothing is
# missing, so it earns full credit — penalising the pipeline for behaving
# correctly only teaches people to ignore the score. A merge that DROPPED its
# rows is a real loss and falls to CREDIT_FAILED. The middle value applies only
# where there is no bbox to measure, so the honest answer is 'unverifiable'.
CREDIT_CONTINUATION = 0.75  # unverifiable only (multi-page table, no bbox)
CREDIT_ABSORBED = 0.75      # content confirmed present, position not geometrically verified
CREDIT_FAILED = FLAGGED_WEIGHT

# A row Stage 2's own stitcher (hybrid_extract.stitch_table_html) could not safely
# rejoin after a page break — every character survives, but the source's ONE row
# now renders as two adjacent <tr>s, the second an orphan with an empty question
# and an empty answer. Not a per-table credit loss like CREDIT_FAILED: nothing is
# missing and the table may hold dozens of other, perfectly-stitched rows, so
# scoring one page-break glitch like a whole failed table would drown it out.
#
# Scored in POINTS, severity-weighted, and not as a share of table credit: it used to
# be 0.15 table-credit, which divided by the table count and so cost ~5 points in a
# 3-table document and ~0.4 in a 35-table one for the same broken row — the size
# dilution CELL_DEFECT_PENALTY exists to avoid. Now the cost is proportional to how
# many rows are split and how certain each one is:
#
#   points = min(ROW_SPLIT_CAP, ROW_SPLIT_POINTS * sum(severity))
#
# with severity 1.0 for a confirmed split (kept_as_separate_row) — the same unit as
# one confirmed cell defect — and less for an unconfirmed one (below).
ROW_SPLIT_POINTS = 1.0
ROW_SPLIT_CAP = 10.0

# A row hybrid_extract's stitcher couldn't even check for a continuation, because the
# new page's leading cell already holds real text (usually the next sub-question's own
# label) rather than the blank marker the check above looks for — see
# TABLE_ROW_SPLIT_UNFLAGGED_CONTINUATION. The signal is the same "previous row ends
# mid-sentence" evidence, but this shape hasn't been checked against the corpus the way
# the blank-cell one has, and a length + not-a-heading filter still leaves real
# false-positive risk (a genuinely new sub-section that happens to follow an answer
# ending mid-list). At most half a confirmed split, scaled further by the stitcher's
# own continuation confidence (0-100), floored so a detected one is never free:
#
#   severity = ROW_SPLIT_UNCONFIRMED_WEIGHT * clamp(confidence / 100, 0.2, 1.0)
ROW_SPLIT_UNCONFIRMED_WEIGHT = 0.5

# Every structural term Fidelity charges in points — source-cited shape defects plus
# row splits — shares this ceiling, so one badly stitched document cannot lose more
# than this from structure alone however many heuristics fire on it.
FIDELITY_STRUCTURE_CAP = 15.0

# A table-level "rows have inconsistent widths" finding is the SYMPTOM a page-break row
# split leaves behind (the orphaned continuation row is wider or narrower than its
# table). Measured on this corpus: all 27 unresolved row splits sat inside a table
# check_source_fidelity also flagged inconsistent_table_columns. Charging both billed
# one physical defect twice, so the specific row-level finding is charged and the
# generic table-level one it explains is not (it stays listed, marked absorbed).
_ROW_SPLIT_SYMPTOM_KINDS = frozenset({"inconsistent_table_columns"})

# Explains every dimension in the UI: what it measures, where the number comes
# from, what to do when it's low, and where it is still weak. Kept beside the
# scoring logic so the two can never drift apart.
HELP = {
    "completeness": {
        "formula": "100 − 2×(100−coverage%) − 100×(pages missing from the tree ÷ pages) − 100×(sections with silent gap ÷ sections) − 25×(sections with flagged gap ÷ sections) − 100×(unreadable pages ÷ pages) − 25×(unreadable-but-snapshotted pages ÷ pages) − 15×(clauses that lost a negation)",
        "label": "Completeness",
        "what": "Is all the source content present in the extracted tree?",
        "from": "Whole-document word coverage (Test 1) + per-PAGE presence in the tree (Test 10: a page no section claims and whose words appear nowhere in the output) + per-section dropped spans (Test 2) + pages no text extractor can read, cross-checked against pypdf — an independent parser, which is the only input here NOT derived from fitz (the engine the extractor itself uses).",
        "advice": "Open Page Review and work through the red pages — each one is a section "
                  "where the source PDF has text the tree doesn't. Silent gaps matter most: "
                  "nothing in the output tells a reader that content is missing there.",
        "caveat": "Coverage counts words; MEANING is covered separately by the negation "
                  "check (a clause that kept its wording but lost a 'not' is reported as a "
                  "'meaning changed' finding). Reading ORDER is still unchecked — text can be "
                  "complete, correctly placed, and still scrambled.",
    },
    "sectioning": {
        "formula": "100 − 100×(sections the outline promises that the tree never built, "
                   "plus sections it built with no body of their own ÷ sections promised) "
                   "− 100×(outline headings whose text reaches no heading anywhere in the "
                   "tree ÷ outline headings); floored at 40 if the tree never divided into "
                   "sections at all",
        "label": "Sectioning",
        "what": "Did the tree build the document's sections, and do they hold their own content?",
        "from": "Two signals against one denominator, plus a third against its own. "
                "check_heading_hierarchy's section census "
                "compares the PDF's own bookmark outline against the nodes on disk, in both "
                "directions. check_content_localized's hollow-section test then diffs each "
                "heading's own slice of the source (heading to next heading) against the file "
                "that claims it, and searches for which OTHER file holds the body. The census "
                "is capped at the product's build depth, so check_outline_coverage adds the "
                "uncapped question — does each outline heading's TEXT survive as a heading "
                "ANYWHERE in the shipped tree — charged over the outline's own count.",
        "advice": "Open the finding's file and the node named beside it. The body is in the "
                  "second one. The usual cause is a page-spanning table whose rows MinerU "
                  "merged into the table above, taking every heading printed inside those rows "
                  "with it — so one section ends up holding four sections' answers and the "
                  "other three are published as headings with nothing under them.",
        "caveat": "The census needs a PDF bookmark outline to compare against; without one "
                  "there is no independent statement of what the document contains and only the "
                  "hollow test runs. Sections the tree built that the outline does NOT name are "
                  "reported but not scored — an over-split and a deliberate front-matter "
                  "recovery look alike and are not yet calibrated apart. The hollow test only "
                  "sees a section whose heading can be found in its own pages, so a reflowed "
                  "title is invisible to it, and it requires the body to be findable under a "
                  "NAMED sibling — hollow AND genuinely absent is a Completeness finding. "
                  "Counts sections, not their size. Replaced the old `structure` dimension, "
                  "whose blob test survives here as a floor; the structure PROFILE is still "
                  "computed and stored, and the fallback chain still reads it.",
    },
    "placement": {
        "formula": "100 − 100×(tables outside their section page range ÷ tables) − 100×(headings nested wrongly ÷ headings)",
        "label": "Placement",
        "what": "Did each table land under the correct heading?",
        "from": "Page-range invariant: a table's actual PDF page vs. the page range of the "
                "section it was rendered under (check_table_placement.py), PLUS heading-tree "
                "shape vs the PDF outline's own levels (check_heading_hierarchy.py) — a heading "
                "filed under the wrong parent is the same defect as a misplaced table.",
        "advice": "For each flagged table, open the Geometry panel in MinerU Inspector and "
                  "compare the table's page against the section it appears in.",
        "caveat": "Phase 1 check only compares PAGE RANGES, so it cannot catch a table that "
                  "landed on the wrong side of a heading that shares its page. Content-level "
                  "placement purity is Phase 2.",
    },
    "toc": {
        "formula": "proper 100 · lacking 55 · improper 35, +10 when the document's PRINTED contents page verifies (repairable). Not scored under 8 pages.",
        "label": "TOC",
        "what": "Was the table of contents Stage 1 built the tree from actually fit for the job?",
        "from": "check_toc_quality.py — the PDF's embedded bookmark outline (is it present, and does it start where the document does?), cross-read against the document's PRINTED contents page, parsed and verified page-by-page with the same reader the rescue stage uses.",
        "advice": "IMPROPER means an outline exists but names sections that are not this document's spine (classically: only the appendix is bookmarked), so the tree is flat or mis-split. LACKING means no outline at all and structure was guessed from font size. If the printed contents page is usable, the fallback chain will rebuild the outline from it — check the rescued tree in fallback/.",
        "caveat": "Judges the SOURCE of the structure, not the structure itself: a proper outline can still be extracted badly, and a document with no TOC may be perfectly well structured by font size. Documents under 8 pages are not judged.",
    },
    "fidelity": {
        "formula": "100 × (sum of per-table credit ÷ tables). Credit: clean geometric match 1.0, verified page-spanning merge 1.0, uncertain match 0.6, no-bbox/position-matched 0.75, unverifiable merge 0.75, reclassified as prose 1.0, failed-but-flagged 0.25, extracted-but-lost-before-output 0. Each table's credit is then multiplied by (1 − the share of its checkable cells whose content does not match the PDF's own grid), so a merged column or row costs that table in proportion to how much of its grid is wrong.",
        "label": "Fidelity",
        "what": "Were tables extracted with the right content and structure?",
        "from": "Stage 2 per-table outcome + geometric IoU match status (confident / uncertain "
                "/ missing), Stage 3 combine results, a structural check that every "
                "successfully-extracted table's cells actually appear in the final tree "
                "(check_table_presence.py), and a cell-by-cell comparison of the rendered grid "
                "against the SOURCE PDF's own word geometry (check_table_cells.py) — the one "
                "table check that does not take MinerU's extraction as its answer key, because "
                "MinerU is often the thing that merged the cells.",
        "advice": "A cell that does not match the PDF's grid is the one to open first: nothing "
                  "is missing, so no other check can see it, but two of the source's cells are "
                  "in one and the question/answer pairing is gone. The finding names the table "
                  "and page — compare that page against the rendered table side by side. After "
                  "that, check 'uncertain' tables in MinerU Inspector, where the bbox overlay "
                  "shows what the extractor saw vs. what MinerU returned. Failed tables keep "
                  "their page snapshot, so their content is still readable by eye.",
        "caveat": "COVERAGE IS PARTIAL and the panel states it: only cells that can be located "
                  "in the page's word geometry are compared against the PDF (roughly a quarter "
                  "of them corpus-wide, limited by how many tables MinerU gives per-page "
                  "bounding boxes for). So '0 cells do not match' means 'none of the cells we "
                  "could check', never 'none at all'. A table with no geometry is scored on its "
                  "match status alone, exactly as before. Table content is otherwise compared "
                  "as an unordered CELL SET, never as a word sequence — row-major serialization "
                  "reorders it on purpose, so sequence comparison over tables is invalid.",
    },
    "uniqueness": {
        "formula": "100 − 500×(words in tree but not in PDF ÷ tree words)",
        "label": "Uniqueness",
        "what": "Is any content duplicated in the output?",
        "from": "Words appearing in the tree but not in the source PDF (Test 1 'extra' words).",
        "advice": "Usually caused by a block emitted twice (once as prose, once inside a "
                  "table) or by header/footer text leaking into the body.",
        "caveat": "ADVISORY — does not set the verdict, and PROVISIONAL: a word-level proxy "
                  "only. Real repeated-block detection is Phase 2, so a low score here is a "
                  "hint, not a diagnosis.",
    },
    "ai_postprocess": {
        "formula": "0 if any content-integrity flag, else 100 − 60×(batches rejected ÷ batches) − 10×(batches reprojected ÷ batches)",
        "label": "AI post-processing",
        "what": "Did Stage 4 change anything it was not allowed to change?",
        "from": "Stage 4's tree compared against Stage 3's, plus stage4_report.json. Not "
                "against the PDF: Stage 3 is Stage 4's input and the only thing it may "
                "differ from, in three known ways — whitespace/<br> moved, cell and row "
                "boundaries moved, and a duplicate label row promoted to a heading.",
        "advice": "A zero means content was ADDED, removed without a surviving duplicate, "
                  "or markup was invented — the Stage 4 tree should be discarded and Stage "
                  "3 used instead. A high rejection rate means the gate is working but the "
                  "model or prompt has drifted and the run is buying little.",
        "caveat": "ADVISORY — never sets the verdict. ABSENT (score null) whenever no "
                  "04_* directory exists, which is most documents; scoring those 0 would "
                  "fail every job that never ran Stage 4.",
    },
    "integrity": {
        "formula": "100 − 15×transpositions − 3×min(orphan footnote refs, 10). The orphan term is capped at 10 refs (−30 max), so a document with a broken numbering scheme can't be driven to 0 by that alone. Missing numbers are reported but NOT scored — measurement showed they are overwhelmingly footnote/list/TOC markers, and penalising them pinned this dimension at 0. A number lost together with its sentence is content loss and is scored under Completeness.",
        "label": "Integrity",
        "what": "Are numbers, thresholds and footnote references intact?",
        "from": "Numeric integrity (Test 3): digit-transpositions, numbers absent from the "
                "tree entirely, and orphan footnote references. A missing number that the "
                "tree already resolves elsewhere as a footnote id — [^n] reference or body —"
                " is excluded before any of this runs (see check_numeric_integrity."
                "is_known_footnote_marker): it isn't a content loss, it's a marker the "
                "extractor correctly relocated into `[^n]` syntax.",
        "advice": "Verify every transposition by hand — those are the highest-consequence "
                  "errors in a compliance document (a 30% threshold read as 3%). The missing-"
                  "number list is informational: scan it for a real threshold or amount, but "
                  "expect footnote and list markers to dominate it. A 'gap in the footnote "
                  "numbering' finding means a footnote number never appears anywhere at all — "
                  "neither reference nor body — which is the one loss mode nothing else here "
                  "can see; check the source PDF at that number by hand.",
        "caveat": "ADVISORY — does not set the verdict. Percentage and footnote-marker noise "
                  "is expected in this corpus; only whole missing paragraphs and tables gate "
                  "a job. Also cannot see a number extracted correctly into the wrong row. The "
                  "numbering-gap check itself is an inference from sequence, not a directly "
                  "observed defect, and needs 4+ footnotes in the document before it trusts "
                  "the sequential-numbering assumption at all.",
    },
}

PAGE_STATE_HELP = {
    "ok": "No issue detected — text on this page reconciles with the source PDF.",
    "flagged": "Content is missing or a table failed HERE, but the output says so "
               "(failure marker or page snapshot) — reviewable, not lost silently.",
    "silent": "The source PDF has content on this page that the tree doesn't, with "
              "nothing in the output indicating it. Look at these first.",
    "unvalidatable": "Visual-only page (complex table or diagram, kept as a snapshot). "
                     "Text-conservation checks cannot judge it — needs a human or a "
                     "vision pass, so it is deliberately NOT counted as a failure.",
}


def _clamp(v: float) -> float:
    return round(max(0.0, min(100.0, v)), 1)


def _cell_defect_penalty(count: int) -> float:
    """Flat cost per confirmed cell-cited source-fidelity finding (see
    CELL_DEFECT_PENALTY): each one is independently confirmed against the source
    PDF, so it costs the same regardless of document size, capped so a pile-up in
    one kind cannot alone zero the dimension."""
    return min(count, CELL_DEFECT_CAP) * CELL_DEFECT_PENALTY


_ROW_SPLIT_ACTIONS = ("kept_as_separate_row", "unflagged_continuation")


def _stitch_page(a: dict) -> int | None:
    """The page a stitch anomaly's row sits on: `pages` is the whole table's range and
    `block` the row's offset into it (the stitcher checks the first row of every page
    after the table's first)."""
    pages, block = a.get("pages") or [], a.get("block")
    return pages[block] if isinstance(block, int) and 0 <= block < len(pages) else None


def _stitch_key(a: dict) -> str:
    """The dismissal key of the finding _collect_findings builds for this anomaly —
    one definition, so a dismissal made in the UI is the one the scorer honours."""
    prefix = ("row-split-unflagged" if a.get("action") == "unflagged_continuation"
              else "row-split" if a.get("action") == "kept_as_separate_row" else "row-merge")
    return dis.hierarchy_key(f"{prefix}-{a.get('table_id')}-{_stitch_page(a)}-{a.get('block')}")


def _row_split_severity(a: dict) -> float:
    if a.get("action") == "kept_as_separate_row":
        return 1.0
    if a.get("action") == "unflagged_continuation":
        conf = a.get("confidence")
        frac = 0.5 if not isinstance(conf, (int, float)) else max(0.2, min(1.0, conf / 100.0))
        return ROW_SPLIT_UNCONFIRMED_WEIGHT * frac
    return 0.0


def _dedupe_row_splits(anomalies: list[dict] | None) -> list[dict]:
    """One entry per PHYSICAL row (table, page, block): two heuristics — or a report
    that logged the same row twice — describing one broken row are one defect. The
    most severe reading of that row is the one kept."""
    best: dict[tuple, dict] = {}
    for a in anomalies or []:
        if a.get("action") not in _ROW_SPLIT_ACTIONS:
            continue
        loc = (a.get("table_id"), _stitch_page(a), a.get("block"))
        if loc not in best or _row_split_severity(a) > _row_split_severity(best[loc]):
            best[loc] = a
    return list(best.values())


def _row_split_penalty(anomalies: list[dict] | None) -> float:
    """Severity-weighted, capped points for unresolved row splits (see ROW_SPLIT_POINTS)."""
    return min(ROW_SPLIT_CAP,
               ROW_SPLIT_POINTS * sum(_row_split_severity(a)
                                      for a in _dedupe_row_splits(anomalies)))


def _dedupe_by_key(findings: list[dict]) -> list[dict]:
    """A finding reported twice under the same content-derived key is one defect."""
    seen, out = set(), []
    for f in findings:
        k = f.get("key")
        if k is not None and k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def _table_files(tree_dir: Path | None) -> dict[str, str]:
    """table_id -> the tree file that holds it, from the MinerU badge line
    ("**⚙ MinerU-extracted table** — table_011, pages 88–90, ..."). Lets a stitch
    anomaly (which knows only its table) be matched to a source-fidelity finding (which
    knows only its file)."""
    out: dict[str, str] = {}
    if not tree_dir or not Path(tree_dir).is_dir():
        return out
    for p in sorted(Path(tree_dir).rglob("*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(r"\b(table_\d+),\s*pages?\b", text):
            out.setdefault(m.group(1), str(p.relative_to(tree_dir)).replace("\\", "/"))
    return out


def _absorbed_by_row_splits(shape_defects: list[dict], splits: list[dict],
                            table_files: dict[str, str]) -> list[dict]:
    """The table-level symptom findings (see _ROW_SPLIT_SYMPTOM_KINDS) that a charged
    row split in the SAME table already accounts for. Matched on file + page range; a
    split whose table cannot be mapped to a file matches only when exactly one symptom
    finding's page range holds its page, so an ambiguous match never absorbs anything."""
    symptoms = [f for f in shape_defects if f.get("kind") in _ROW_SPLIT_SYMPTOM_KINDS
                and f.get("pages")]
    absorbed: dict[int, dict] = {}
    for a in splits:
        page = _stitch_page(a)
        if page is None:
            continue
        in_range = [f for f in symptoms if min(f["pages"]) <= page <= max(f["pages"])]
        file = table_files.get(a.get("table_id"))
        hits = ([f for f in in_range if f.get("file") == file] if file
                else in_range if len(in_range) == 1 else [])
        for f in hits:
            absorbed[id(f)] = f
    return list(absorbed.values())


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _significant_number(tok: str) -> bool:
    """Bare 1-2 digit integers are overwhelmingly footnote/list markers reused
    across a document; percentages and 3+ digit numbers are the meaningful ones
    (thresholds, statute references, amounts). Same rule the page-issue builder
    in hybrid_extract_ui.py uses, kept consistent so the dashboard and the
    score never disagree about what counts."""
    digits = "".join(c for c in tok if c.isdigit())
    return tok.endswith("%") or len(digits) >= 3


# ---------------- dimensions ----------------
def _orphan_is_duplicate(block_text: str, tree_text: str) -> bool:
    """Is this orphan's text ALREADY in the tree?

    "No region claimed this block" and "this content is absent from the output"
    are different questions, and conflating them made the scorecard lie. MinerU
    often emits a block covering a whole page's table area, overlapping content
    that one or two regions already emit -- Luxembourg__181768 p78 holds the
    section 6.4 continuation, the PLEASE NOTE paragraph AND question (g), every
    sentence of which is in the tree. Measured after the fixes: all three
    remaining "lost" orphans on the MRAM corpus were duplicates of this shape
    (Luxembourg 11/11 sentences present, Mexico 3/3, Saudi Arabia 7/7), so the
    reported loss count was 3 when the real one was 0.

    Adopting such a block would DUPLICATE, not recover -- which is why declining
    it is right, and why it must not be reported as loss.
    """
    norm = lambda t: re.sub(r"[^a-z0-9]", "", (t or "").lower())   # noqa: E731
    hay = norm(tree_text)
    if not hay:
        return False
    sents = [x.strip() for x in re.split(r"(?<=[.?])\s+", block_text or "") if len(x.strip()) > 40]
    if not sents:
        frag = norm(block_text)[:120]
        return bool(frag) and frag in hay
    return all(norm(x) in hay for x in sents)


# A multi-part table cell holds several <br><br>-separated answers, one per
# sub-question the paired question cell enumerates. The normal separator between
# two populated segments is exactly one blank line — two <br> tags. Four or more
# in a row means one or more of those segments is BLANK: content that belongs
# between two real answers and isn't there.
#
# Measured on Cayman Islands__183333 p46, "7.1(i)": the question cell lists
# seven sub-items (i)-(vii); the answer cell holds six "N/a. Please see 7.1(a)
# above." segments and a bare <br><br><br><br> where the seventh — the one
# paired with (iii) — should be. The word-coverage check never sees it: the
# SAME six-word phrase already appears six times elsewhere in this exact cell,
# so "is this text anywhere in the tree" is satisfied and the loss is invisible
# to every check built on that question. This one asks a different question —
# does the answer cell hold as many segments as the question cell asks for —
# which is exactly what a repeated-boilerplate answer defeats for every other
# check here. Corpus-wide: 1 file in 15 documents, zero false positives.
_BLANK_SEGMENT_RE = re.compile(r"(?:<br\s*/?>\s*){4,}", re.I)
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.I)


def _locate_page(pdf_path: Path | None, pages_range: tuple[int, int] | None,
                 anchor_tokens: list[str]) -> int | None:
    """Which page in `pages_range` actually holds `anchor_tokens` — reuses the
    same per-page token index _readable_span builds, so a page found here is
    exactly the page a readable-title lookup would also match against. Tries
    every page in the file's own range rather than the +6 cap _readable_span
    uses for a dropped SPAN's page: an empty-segment anchor is anchored to a
    specific cell, wherever in a long section that cell happens to fall."""
    if not anchor_tokens or not pdf_path or not pages_range:
        return None
    lo, hi = pages_range
    for pno in range(lo, hi + 1):
        got = _page_token_offsets(pdf_path, pno)
        if not got:
            continue
        toks = [t for t, _s, _e in got[1]]
        n = len(anchor_tokens)
        for i in range(len(toks) - n + 1):
            if toks[i:i + n] == anchor_tokens:
                return pno
    return None


def find_empty_answer_segments(out_root: Path, stage: int | None) -> list[dict]:
    try:
        tree_dir = resolve_stage_dir(out_root, stage or 3)
    except Exception:                                        # noqa: BLE001
        return []
    try:
        pdf_path = resolve_pdf(out_root, None)
    except Exception:                                        # noqa: BLE001
        pdf_path = None
    out = []
    for p in sorted(tree_dir.rglob("*.md")):
        try:
            raw = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        file_pages = parse_page_range(raw)
        for m in _BLANK_SEGMENT_RE.finditer(raw):
            # Guard against a run that happens to straddle a cell/row boundary —
            # <br> is cell content here and never a row separator in this
            # pipeline's own HTML, but a defect elsewhere could make that false.
            if "</td>" in m.group(0) or "<tr" in m.group(0):
                continue
            n_brs = len(_BR_TAG_RE.findall(m.group(0)))
            empty_segments = n_brs // 2 - 1
            if empty_segments < 1:
                continue
            before = re.sub(r"<[^>]+>", " ", raw[max(0, m.start() - 200):m.start()]).split()
            after = re.sub(r"<[^>]+>", " ", raw[m.end():m.end() + 200]).split()
            # Located on the source PDF the same way a readable-title quote is: the
            # text right before the gap is tokenised the same way tokenize() would
            # and matched against each candidate page's own token index. Without
            # this the finding had no page at all (an empty `pages: []`), which is
            # why it never appeared on the Page Review tab — everything else there
            # is keyed by page number, so a finding with none is invisible there
            # even though it still counts against the score.
            anchor = [t.lower() for t in before[-8:] if re.match(r"^[A-Za-z0-9]", t)]
            page = _locate_page(pdf_path, file_pages, anchor) if anchor else None
            out.append({
                "file": str(p.relative_to(tree_dir)).replace("\\", "/"),
                "empty_segments": empty_segments,
                "before": " ".join(before[-14:]),
                "after": " ".join(after[:14]),
                "page": page,
                # The anchor is a strict contiguous token match, which a table cell's
                # PDF reading order routinely breaks (columns extract in a different
                # order than the markdown's row-major cells) — `page` comes back None
                # more often here than for prose. Keep the file's own page range as a
                # fallback so a pinpoint miss still puts the finding somewhere in Page
                # Review instead of vanishing (see the "pages: []" note below).
                "file_pages": list(file_pages) if file_pages else None,
            })
    return out


def orphan_summary(stage2: dict | None, tree_text: str = "") -> dict:
    """Orphan tables, from the Stage 2 report: MinerU extracted them correctly and
    no placeholder claimed them.

    An UNRECOVERED orphan is content loss that every other signal here can miss.
    Word coverage is a bag of words, so a question whose wording mirrors a parallel
    question elsewhere still counts as covered; and the per-section diff only fires
    if the span lands inside a section's page range. Measured on Bahrain__169749:
    3,543 characters of section 7.5 absent from the tree, completeness 98.6, ZERO
    silent spans, gate PASS. That is the case these numbers exist to make visible.

    A RECOVERED orphan is not loss — it is a heuristic adoption (see
    hybrid_extract's orphan recovery), and the thing worth checking by eye is
    whether it was adopted into the right region. So it is reported separately and
    never as a defect.
    """
    s2 = stage2 or {}
    unclaimed = s2.get("orphan_blocks") or []
    got = s2.get("orphans_recovered") or []
    # Judge on the FULL block text where Stage 2 recorded it; the 200-char preview
    # is only a fallback for reports written before that field existed. Preview-only
    # judging is what wrongly cleared Luxembourg p78 as a duplicate.
    dup = [o for o in unclaimed
           if _orphan_is_duplicate(o.get("text") or o.get("preview") or "", tree_text)]
    dup_ids = {id(o) for o in dup}
    lost = [o for o in unclaimed if id(o) not in dup_ids]
    return {
        "lost_count": len(lost),
        "lost_chars": sum(o.get("chars") or 0 for o in lost),
        "lost_pages": sorted({o.get("page") for o in lost if o.get("page") is not None}),
        "duplicate_count": len(dup),
        "duplicate_chars": sum(o.get("chars") or 0 for o in dup),
        "recovered_count": len(got),
        "recovered_chars": sum(o.get("chars") or 0 for o in got),
        "recovered_into": sorted({o.get("table_id") for o in got if o.get("table_id")}),
        "_lost": lost, "_dup": dup,
    }


def _score_completeness(wc: dict, cl: dict, unread_silent: int = 0, unread_flagged: int = 0,
                        n_pages: int = 0, negation_flips: int = 0,
                        pc: dict | None = None,
                        unchunked: bool = False,
                        orphans: dict | None = None,
                        tcons: dict | None = None,
                        empty_segment_count: int = 0,
                        missing_answers: list | None = None,
                        source_cells_checked: int | None = None) -> tuple[float, dict]:
    coverage = wc.get("coverage_adjusted_pct", 0.0)
    files = max(cl.get("files_scanned", 0), 1)
    silent_files = len([r for r in cl.get("results", []) if not r["acknowledged"] and r["dropped"]])
    ack_files = len([r for r in cl.get("results", []) if r["acknowledged"] and r["dropped"]])
    # Rate-based so the score means the same thing on a 57-page and a 259-page
    # document. Coverage deficit is doubled: a 4% whole-document word loss is a
    # lot of prose, not a rounding error.
    # Pages no text extractor can read are a loss the fitz-vs-fitz comparison is
    # structurally blind to: nothing expected, nothing found, coverage 100%. They
    # must cost points here or a scanned page would score perfect. Weighted by
    # whether the output at least SHOWS the page (a snapshot) — same silent-vs-
    # flagged principle used everywhere else.
    pages = max(n_pages, 1)
    # A page the tree accounts for NOWHERE — no section claims it, none of its words
    # are findable in the output. Every other signal here is relative to the sections
    # that exist, so a document whose extractor skipped a page range scored clean on
    # all of them (Curaçao 164302: 36 of 40 pages never read, section checks happy).
    # Weighted as a silent loss, which is what it is: nothing in the output says the
    # page was dropped.
    pc = pc or {}
    checkable = max(pc.get("pages_with_text", 0), 1)
    missing_pages = pc.get("missing_count", 0)
    short_doc = 0 < n_pages < SHORT_DOC_MAX_PAGES
    # The per-FILE gap ratios cannot be applied to a tree that is ONE file, and the
    # MinerU last-resort tier deliberately emits exactly that: its raw markdown,
    # whole, because re-chunking is what loses the content at that tier. With
    # files == 1 a single unmatched span is 100 x 1/1 and takes the entire score,
    # so the document fails no matter how complete it is. Measured on
    # 172_G20/Canada (Ontario)__176861: 106% word coverage, completeness 0.0.
    # The ratios are a fair measure of "how much of the tree is affected" only when
    # there is a tree to speak of; below that they measure the chunking, not the
    # content. Coverage, unreadable pages and negation flips all still apply —
    # those are measured against the DOCUMENT, not against the file count.
    unchunked_tree = unchunked or files <= UNCHUNKED_MAX_FILES
    skip_gap_terms = short_doc or unchunked_tree
    gap_terms = 0.0 if skip_gap_terms else (
        100.0 * (silent_files / files) * SILENT_WEIGHT
        + 100.0 * (ack_files / files) * FLAGGED_WEIGHT)
    # missing_cell_answer (check_source_fidelity.py) is a source-cited content loss --
    # a short answer ("No"/"N/a") the source's own question expects that the output
    # row does not have, even counting occurrences elsewhere -- exactly this
    # dimension's question ("is all the source content present?"), just proven at
    # cell grain instead of the whole-document word diff above. Each one is
    # independently confirmed against the source PDF, so it costs a flat amount
    # (see CELL_DEFECT_PENALTY), same reasoning as Placement's boundary_leaks term.
    ma = missing_answers or []
    score = (100.0
             - (100.0 - coverage) * 2.0
             - 100.0 * (missing_pages / checkable) * SILENT_WEIGHT
             - gap_terms
             - 100.0 * (unread_silent / pages) * SILENT_WEIGHT
             - 100.0 * (unread_flagged / pages) * FLAGGED_WEIGHT
             - NEGATION_FLIP_PENALTY * negation_flips
             - EMPTY_ANSWER_SEGMENT_PENALTY * empty_segment_count
             - _cell_defect_penalty(len(ma)))
    silent_spans = sum(len(r["dropped"]) for r in cl.get("results", []) if not r["acknowledged"])
    stats = [
        {"label": "word coverage", "value": f"{coverage:.1f}%"},
        # The page NUMBERS, not just how many. The count alone said a page had been
        # lost and gave a reader no way to look at it -- and these are the pages with
        # no representation anywhere in the output, so there is nothing in the tree to
        # search for either. Germany 179874 read "6 of 153" and the six are 63-68.
        {"label": "pages missing from the tree",
         "value": (f"{missing_pages} of {checkable}"
                   + (f" \u2014 pp. {_page_runs(pc.get('pages_missing') or [])}"
                      if missing_pages else "")),
         "bad": missing_pages > 0},
        {"label": "sections with SILENT gap",
         "value": (f"{silent_files} of {files} \u2014 not scored "
                   f"({'unchunked MinerU output' if unchunked_tree else 'short document'}: "
                   f"a per-section ratio cannot be applied to a {files}-file tree)"
                   if skip_gap_terms else f"{silent_files} of {files}"),
         "bad": silent_files > 0 and not skip_gap_terms,
         "warn": silent_files > 0 and skip_gap_terms},
        {"label": "sections with flagged gap", "value": f"{ack_files} of {files}",
         "warn": ack_files > 0},
        {"label": "silent dropped spans", "value": silent_spans, "bad": silent_spans > 0},
        # The one loss measure that does not depend on WHERE content is: the whole PDF
        # against the whole tree. Stated separately from the per-section ratios above
        # because it answers a different question — "is this text in the document at
        # all" rather than "is it in the section that should hold it" — and it is the
        # one with no blind spot. Single-token residue is excluded from the number and
        # still listed in the check's own output; see MIN_SCORED_SPAN.
        {"label": "source text absent from the tree ENTIRELY",
         "value": (f"{(tcons or {}).get('scored_tokens_missing', 0)} word(s) in "
                   f"{(tcons or {}).get('scored_span_count', 0)} span(s)"
                   if tcons else "not measured"),
         "bad": bool((tcons or {}).get("scored_tokens_missing"))},
        {"label": "answer cells missing from their question's row (source-cited)",
         "value": f"{len(ma)} of {source_cells_checked or 0}",
         "bad": len(ma) > 0},
    ]
    excused = sum(len(r.get("present_elsewhere", [])) for r in cl.get("results", []))
    if excused:
        stats.append({"label": "excused — found in full elsewhere in the tree", "value": excused})
    if negation_flips:
        stats.insert(0, {"label": "clauses that LOST a negation (meaning inverted)",
                         "value": negation_flips, "bad": True})
    if empty_segment_count:
        stats.insert(0, {"label": "answer segment(s) blank between two others "
                                  "(repeated boilerplate hid the gap)",
                         "value": empty_segment_count, "bad": True})
    if unread_silent or unread_flagged:
        stats.append({"label": "pages no text extractor can read",
                      "value": f"{unread_silent + unread_flagged} of {pages}",
                      "bad": unread_silent > 0, "warn": unread_flagged > 0})
    orph = orphans or {}
    # Reported, deliberately NOT scored. The loss is real, but on a document where
    # the per-section diff already caught it the gap terms above have charged for it
    # once (Malta__181816: the same 321 tokens appear as a silent span in
    # 08-private-placement-regime.md), and charging twice for one loss would make the
    # number mean less, not more. Where NOTHING else catches it — Bahrain, 0 silent
    # spans — this stat is the only thing that says so, which is the point.
    if orph.get("lost_count"):
        stats.append({"label": "orphan tables LOST — extracted by MinerU, in no section",
                      "value": f"{orph['lost_count']} ({orph['lost_chars']} chars, "
                               f"page(s) {', '.join(str(p) for p in orph['lost_pages'])})",
                      "bad": True})
    if orph.get("duplicate_count"):
        stats.append({"label": "orphan tables that DUPLICATE text already in the tree",
                      "value": f"{orph['duplicate_count']} ({orph['duplicate_chars']} chars) "
                               f"— nothing lost, adopting them would duplicate"})
    if orph.get("recovered_count"):
        stats.append({"label": "orphan tables RECOVERED into a region",
                      "value": f"{orph['recovered_count']} ({orph['recovered_chars']} chars "
                               f"-> {', '.join(orph['recovered_into'])})",
                      "warn": True})
    return _clamp(score), {
        "orphan_tables_lost": orph.get("lost_count", 0),
        "orphan_tables_duplicate": orph.get("duplicate_count", 0),
        "orphan_chars_duplicate": orph.get("duplicate_chars", 0),
        "orphan_chars_lost": orph.get("lost_chars", 0),
        "orphan_tables_recovered": orph.get("recovered_count", 0),
        "orphan_chars_recovered": orph.get("recovered_chars", 0),
        "coverage_pct": coverage,
        "files_scanned": files,
        "files_with_silent_gap": silent_files,
        "files_with_flagged_gap": ack_files,
        "silent_spans": silent_spans,
        "empty_answer_segments": empty_segment_count,
        "unreadable_silent_pages": unread_silent,
        "unreadable_flagged_pages": unread_flagged,
        "pages_missing": missing_pages,
        "pages_checkable": checkable,
        "page_coverage_pct": pc.get("coverage_pct"),
        "stats": stats,
    }


def _roll_up(section_map: list[dict]) -> list[dict]:
    """Annotate each entry with what happened to its SUBSECTIONS, and each node
    with the subsections it swallowed (in place). Returns the damaged branches,
    worst first.

    A per-leaf mark alone does not say the thing a reader most needs to hear.
    "8.9, 8.10 and 8.11 are empty" are three lines that scroll past; "8 MARKETING
    ACTIVITIES: 4 of its 12 subsections hold no content" and "8.8 Investment
    Advice absorbed 8.9, 8.10, 8.11" are the two sentences that identify a single
    page-spanning table as the cause. The failure is almost never one clause — it
    is a run of consecutive siblings whose rows merged into one table — and only
    a rollup makes that shape visible."""
    absorbed: dict[str, list[str]] = {}
    for s in section_map:
        if s["status"] == "hollow" and s.get("absorbed_by"):
            absorbed.setdefault(s["absorbed_by"], []).append(s["title"])

    branches = []
    for i, s in enumerate(section_map):
        kids = []
        for j in range(i + 1, len(section_map)):
            if section_map[j]["level"] <= s["level"]:
                break
            kids.append(section_map[j])
        s["children"] = len(kids)
        s["children_defective"] = sum(1 for k in kids if k["status"] != "built")
        s["absorbed"] = absorbed.get(s["node"], []) if s.get("node") else []
        if s["children_defective"]:
            # Which nodes ate this branch's children — named here, where the
            # parent/child relation is already in hand.
            eaters = sorted({k["absorbed_by"] for k in kids
                             if k["status"] == "hollow" and k.get("absorbed_by")})
            branches.append({"title": s["title"], "level": s["level"],
                             "children": len(kids), "defective": s["children_defective"],
                             "absorbers": eaters[:3]})
    branches.sort(key=lambda b: (-b["defective"], b["title"]))
    return branches


def _apply_census_dismissals(hier: dict | None, dismissed: set) -> dict | None:
    """Remove dismissed missing-section findings from the census, for SCORING only.

    A dismissed `section_missing` asserts the outline entry is not a section at all,
    so it leaves the ratio ENTIRELY — numerator and denominator — the same treatment
    _apply_dismissals gives a dismissed table. Anything less would be a trap: the
    finding disappears from the list while the score keeps charging for it.

    This is the escape hatch for outline defects, which are common and are not
    extraction defects. Australia__181814's printed contents page wraps section
    7.4's title across three lines, and the TOC rescue parsed each line as its own
    entry — so 'Services)' and 'Vehicles, Segregated Managed Accounts and Family
    Offices' are reported as two missing top-level clauses when the first is a
    fragment and the second is section 7.4's tail. Dismissing them takes `promised`
    from 18 to 16 and the dimension from 83.3 to ~94, which is the truth."""
    census = (hier or {}).get("census") or {}
    if not dismissed or not census.get("available"):
        return hier

    def kept(title: str) -> bool:
        return dis.hierarchy_key("absent-" + title) not in dismissed

    sections = [s for s in census.get("sections", [])
                if s.get("node") or kept(s["title"])]
    if len(sections) == len(census.get("sections", [])):
        return hier
    missing = [m for m in census.get("missing", []) if kept(m["title"])]
    return dict(hier, census=dict(
        census, sections=sections, missing=missing,
        promised=len(sections), missing_count=len(missing),
        delivered=sum(1 for s in sections if s.get("node"))))


def _norm_section_title(title: str) -> str:
    """The pipeline's own heading key, so the census and the outline check agree.

    Imported lazily and defensively: compare_stage_completeness pulls in lib_validate,
    and the scorecard is imported from there. A failure to import must not take the
    dimension down — falling back to a casefolded compare only risks charging one
    defect twice, which is strictly better than scoring nothing."""
    try:
        from compare_stage_completeness import _norm_heading
        return _norm_heading(title)
    except Exception:
        return " ".join(title.split()).casefold()


def _score_sectioning(cl: dict, hier: dict | None = None,
                      sp: dict | None = None,
                      outl: dict | None = None,
                      unchunked: bool = False) -> tuple[float | None, dict]:
    """Did the sections the tree PUBLISHES actually keep their own bodies?

    Nothing else here can answer that, which is the whole reason this exists.
    `completeness` counts words against the source and a hollow section loses
    none — its body is in the tree, one node away, so word coverage stays at 99%.
    `placement` divides by the headings that exist and a hollow heading exists.
    Worse, the two excuses in check_content_localized that keep those numbers
    honest — `relocated` and `present_elsewhere` — are precisely what swallows
    this: both ask "is this text ANYWHERE in the tree?", and for a hollow section
    the answer is yes.

    This dimension REPLACED `structure`, which asked whether the tree divided at
    all and nothing more. That test survives here as a hard floor (see
    STRUCTURE_FAIL_SCORE below): a document that arrived as one blob has no
    sections holding their own content, which is this same failure at its
    extreme, so it still cannot pass — and it is now measured on the same scale
    as the partial cases instead of on a separate pass/fail of its own.

    Measured on 124_Marketing_Restrictions/ADGM__170680, the document that
    prompted this: sections 8.5, 8.9, 8.10 and 8.11 are published as headings
    with no body, their rows having been merged into the table under 8.8, and the
    document gated PASS at 95.7 with completeness 96.1 and placement 100.0. What
    it costs downstream is the thing the index is FOR — a search for "Arranging"
    reaches a node containing the sentence "the rows for this section are inside
    the table shown under 8.8"."""
    from check_structure_profile import structure_is_acceptable
    blob_ok, blob_why = (True, "")
    if sp and sp.get("chunks"):
        blob_ok, blob_why = structure_is_acceptable(sp)

    if not cl or "hollow_count" not in cl:
        # The per-section tests could not run. The blob test still can, and must:
        # it is the case this dimension inherited from `structure`, and letting it
        # fall through as "no data" would silently un-gate every document whose
        # heading detection collapsed.
        if not blob_ok:
            return STRUCTURE_FAIL_SCORE, {
                "available": True, "blob": True, "reason": blob_why,
                "stats": [{"label": "did it divide into sections?", "value": "NO", "bad": True},
                          {"label": "why", "value": blob_why, "bad": True}]}
        return None, {"available": False, "reason": "no hollow-section data"}
    checked = cl.get("sections_checked") or 0
    if checked <= 0:
        return None, {"available": False, "reason": "no sections to check"}
    hollow = cl.get("hollow_sections") or []
    census = (hier or {}).get("census") or {}

    # The document's own outline with a mark against every entry. Built here
    # rather than in either check because it needs both halves: the census knows
    # which sections became nodes, only content_localized knows which of those
    # nodes are empty. Three states, and the middle one is the whole reason this
    # map exists — "built" and "built with nothing in it" are indistinguishable
    # in every other view the scorecard offers.
    by_node = {h["file"]: h for h in hollow}
    section_map = []
    for s in census.get("sections", []):
        h = by_node.get(s["node"]) if s["node"] else None
        section_map.append({
            "title": s["title"], "level": s["level"], "page": s["page"],
            "status": "missing" if not s["node"] else ("hollow" if h else "built"),
            "node": s["node"],
            "retention_pct": (h or {}).get("retention_pct"),
            "absorbed_by": ((h or {}).get("absorbed_by") or [None])[0],
        })
    branches = _roll_up(section_map)

    # A section the document promises and the tree never built, and a section the
    # tree built with nothing in it, are ONE defect from where a reader stands:
    # the clause cannot be reached under its own name. So they share a
    # denominator — the sections the document says it has — and cost the same.
    #
    # Counted off the map rather than by adding the two checks' totals, so every
    # number in this panel divides the same 39 and a node that is BOTH hollow and
    # absent from the outline cannot be charged twice.
    if census.get("available"):
        promised = census["promised"]
        empty = sum(1 for s in section_map if s["status"] == "hollow")
        never = sum(1 for s in section_map if s["status"] == "missing")
        intact = sum(1 for s in section_map if s["status"] == "built")
        denom, lost = max(promised, 1), empty + never
    else:
        # No outline: nothing states what the document should contain, so the only
        # honest denominator is what the tree holds, and "never built" is unknowable.
        promised = never = intact = None
        empty, denom, lost = len(hollow), checked, len(hollow)

    # THE OUTLINE TERM. The census above is capped at the product's build depth, so a
    # sub-heading it never promised cannot be counted missing there however completely
    # it is gone: Marketing Restrictions Hungary 167023 promises 15 sections, builds all
    # 15, and scored a flat 100 while "7.1 Private Placement Regime (AIFs and
    # Non-Passported UCITS Funds)" reached no heading anywhere in its stage-5 tree —
    # the outline_absent finding was raised on this very dimension and moved nothing.
    # check_outline_coverage answers the uncapped question (does the heading's TEXT
    # survive as a heading ANYWHERE), product_rules.outline_coverage_checked already
    # documents it as "scored", and this is where that is true.
    #
    # Charged as a SEPARATE rate over its own denominator, not folded into the census
    # one. The two populations are different sizes — 47 outline headings against 15
    # promised sections here — and merging them would dilute the census term fourfold,
    # turning three genuinely unbuilt sections from 80.0 into 93.6.
    #
    # Deduped against the census misses, which the outline check re-reports whenever a
    # section that was never built also left no heading behind. One defect, one charge:
    # the census rate keeps it, because that is the harsher denominator.
    census_missing_norm = {_norm_section_title(m.get("title") or "")
                           for m in census.get("missing", [])}
    # The raw-MinerU-markdown fallback tier (mineru_full_extract.RAW_MINERU_OUTPUT)
    # writes ONE synthetic "heading" into headings_manifest.json — the PDF's own
    # filename stem, standing in for a node the raw dump can live under, never a
    # real section title (see that module's own comment: "it makes every
    # per-section ratio meaningless"). Treated as a real outline entry here, that
    # single placeholder can never "reach a heading in the tree" under its own
    # name, so outline_lost/outline_total came out 1/1 and zeroed sectioning for
    # every document this tier ever touches — Spain__138135: worst_score 0.0,
    # weakest "sectioning", despite a cleanly divided 19-section tree. Skip the
    # term entirely here, the same carve-out compute_scorecard already gives this
    # tier everywhere else (see its own `_unchunked`).
    outline_total = 0 if unchunked else ((outl or {}).get("outline_count") or 0)
    outline_absent = [] if unchunked else [
        m for m in ((outl or {}).get("missing") or [])
        if _norm_section_title(m.get("title") or "") not in census_missing_norm]
    outline_lost = len(outline_absent) if ((outl or {}).get("available") and outline_total) else 0

    score = 100.0 - 100.0 * lost / denom - 100.0 * outline_lost / max(outline_total, 1)
    if not blob_ok:
        # A tree that never divided cannot score above the old structure failure
        # mark however few individual holes are countable in it.
        score = min(score, STRUCTURE_FAIL_SCORE)

    # ---- the panel -----------------------------------------------------------
    # One denominator, and the GOOD number stated rather than left to be worked
    # out by subtraction. The previous version put "sections the outline promises:
    # 39" next to "sections checked for their own body: 41" — two different
    # populations with near-identical labels, which is unreadable however correct
    # each one is on its own. The 41 is still in the detail below for anything
    # that wants it; it is not a headline.
    stats = []
    if not blob_ok:
        stats.append({"label": "did it divide into sections at all?", "value": "NO",
                      "bad": True})
    if census.get("available"):
        stats += [
            {"label": "the document promises", "value": f"{promised} sections"},
            {"label": "built, and holding their own content", "value": intact,
             "bad": intact < promised},
            {"label": "built but EMPTY — body is under another node", "value": empty,
             "bad": empty > 0},
            {"label": "never built at all", "value": never, "bad": never > 0},
            # REPORTED, NOT SCORED. A boundary the outline does not have is sometimes
            # a real over-split and sometimes a deliberate recovery (front matter a
            # publisher never bookmarked), and there is no calibration yet to tell
            # them apart. Charging for it would penalise the recovery.
            {"label": "extra nodes the outline does not name (not scored)",
             "value": census["extra_count"], "warn": census["extra_count"] > 0},
        ]

    else:
        stats += [
            {"label": "sections in the tree", "value": checked},
            {"label": "built but EMPTY — body is under another node", "value": empty,
             "bad": empty > 0},
            {"label": "never built at all",
             "value": "unknown — this PDF has no outline to compare against",
             "warn": True},
        ]
    # Stated whenever the check ran, pass or fail: the census line above says
    # "15 of 15 built" and on its own reads as a clean bill of structural health for a
    # tree that has silently dropped a heading two levels down.
    if (outl or {}).get("available") and outline_total:
        stats.append({
            "label": "outline headings reaching no heading in the tree (any depth)",
            "value": (f"{outline_lost} of {outline_total}"
                      + (" \u2014 " + "; ".join(str(m.get("title")) for m in outline_absent[:3])
                         if outline_absent else "")),
            "bad": outline_lost > 0})

    # One line per damaged branch, worst first — the answer to "what is wrong with
    # this document". An inspector needs "8 MARKETING ACTIVITIES lost 4 of its 12
    # subsections" before they need any score.
    for b in branches[:6]:
        stats.append({"label": f"↳ inside {b['title'][:46]}",
                      "value": f"{b['defective']} of its {b['children']} subsections "
                               f"have no content of their own", "bad": True})
    return _clamp(score), {
        "available": True,
        "blob": not blob_ok,
        "blob_reason": blob_why,
        "concentration_pct": (sp or {}).get("concentration_pct"),
        "chunks": (sp or {}).get("chunks"),
        "section_map": section_map,
        "branches": branches,
        # Counted off the section map (see above), so these divide the same
        # denominator as the score: promised == intact + empty + never.
        "promised": promised,
        "intact": intact,
        "empty_count": empty,
        "missing_count": never,
        "extra_count": census.get("extra_count"),
        # The outline term, kept beside the census numbers it is deliberately not
        # merged with (see the score above).
        "outline_count": outline_total or None,
        "outline_absent_count": outline_lost,
        "outline_absent": outline_absent[:25],
        # Diagnostics, not headlines: `sections_checked` is the number of FILES
        # body-checked, a different population from `promised`, and showing the
        # two side by side is what made this panel unreadable.
        "hollow_count": len(hollow),
        "sections_checked": checked,
        "delivered": census.get("delivered"),
        "census_available": bool(census.get("available")),
        "census_reason": census.get("reason"),
        "missing": census.get("missing", [])[:25],
        "extra": census.get("extra", [])[:25],
        "hollow": [{"file": h["file"], "pages": h["pages"],
                    "retention_pct": h.get("retention_pct"),
                    "body_tokens": h.get("body_tokens"),
                    "absorbed_by": (h.get("absorbed_by") or [None])[0]} for h in hollow[:25]],
        "stats": stats,
    }


def _score_placement(tp: dict | None, tables_total: int, hier: dict | None = None,
                     boundary_leaks: list | None = None,
                     source_cells_checked: int | None = None) -> tuple[float | None, dict]:
    if not tp or tp.get("skipped"):
        return None, {"available": False,
                      "reason": (tp or {}).get("reason", "placement check did not run")}
    flags = tp.get("flags", [])
    total = max(tp.get("tables_total", tables_total), 1)
    # A wrongly-nested heading is the same class of defect as a wrongly-placed
    # table — content filed under the wrong parent — so it belongs here.
    hflags = [f for f in (hier or {}).get("flags", [])
              if f.get("kind") in ("wrong_parent", "wrong_depth")]
    htotal = max((hier or {}).get("headings_total") or 1, 1)
    # Same question as the whole-table flags above, at CELL grain: a row that
    # landed under the wrong heading, cited against the source PDF rather than
    # inferred from a page-range mismatch. Each one is independently confirmed
    # against the source PDF, so it costs a flat amount (see CELL_DEFECT_PENALTY)
    # rather than a rate diluted by however many cells this document happens to
    # have — a leaked cell is exactly as real in a 20-cell document as a 500-cell one.
    leaks = boundary_leaks or []
    return _clamp(100.0 - 100.0 * len(flags) / total
                  - 100.0 * len(hflags) / htotal
                  - _cell_defect_penalty(len(leaks))), {
        "available": True, "flagged_tables": len(flags), "tables_total": total,
        "headings_total": tp.get("headings_total"),
        "flags": [{"table_id": f["table_id"], "pages": f["pages"], "detail": f["detail"]}
                  for f in flags],
        "stats": [
            {"label": "tables outside their section's page range",
             "value": f"{len(flags)} of {total}", "bad": len(flags) > 0},
            {"label": "headings resolved", "value": tp.get("headings_total")},
            {"label": "headings nested wrongly", "value": len(hflags), "bad": len(hflags) > 0},
            {"label": "cells landed under the wrong section (source-cited)",
             "value": f"{len(leaks)} of {source_cells_checked or 0}",
             "bad": len(leaks) > 0},
        ],
    }


def _table_credit(t: dict) -> tuple[float, str]:
    """-> (credit, bucket). Bucket names are what the UI's table strip colours."""
    if t.get("false_positive"):
        # Covers both false-positive outcomes, which share this verdict — "not a
        # table" — and so share full credit: the region whose prose Stage 2 recovered
        # and emitted, and the `stage1_duplicate` region whose text Stage 1 had
        # already emitted (nothing is rendered for that one; see run_stage2).
        return CREDIT_RECLASSIFIED, "reclassified"
    if t.get("continuation"):
        v = t.get("continuation_verified")
        if v is True:
            return CREDIT_CONFIDENT, "continuation"
        if v is False:
            return CREDIT_FAILED, "failed"      # the merge lost these rows
        return CREDIT_CONTINUATION, "continuation"
    if t.get("absorbed"):
        # No table of its own, but ≥85% of the region's source text was confirmed
        # present in MinerU's output for those pages. Content-verified rather than
        # geometry-verified, so it scores like a rank-matched table: clearly better
        # than a failure, clearly short of a clean geometric match.
        return CREDIT_ABSORBED, "absorbed"
    if not t.get("ok"):
        return CREDIT_FAILED, "failed"
    if t.get("match_method") == "rank_fallback":
        return CREDIT_RANK_FALLBACK, "unverified"
    if t.get("match_status") == "uncertain":
        return CREDIT_UNCERTAIN, "uncertain"
    return CREDIT_CONFIDENT, "confident"


def _score_fidelity(tables: list[dict], tpres: dict | None = None,
                    tcells: dict | None = None,
                    stitch_anomalies: list[dict] | None = None,
                    stage: int | None = None,
                    shape_defects: list | None = None,
                    source_cells_checked: int | None = None) -> tuple[float | None, dict]:
    # A row/column-shape defect (inconsistent_table_columns, cell_split, row_merge,
    # column_merge, block_merge) answers this dimension's own question -- "were tables
    # extracted with the right structure?" -- at cell grain, cited against the source
    # PDF, and is recomputed fresh against whichever tree is being audited, so it is
    # applied as its own flat penalty at every stage (see CELL_DEFECT_PENALTY) rather
    # than folded into the per-table credit.
    defects = shape_defects or []
    shape_stat = {"label": "table cells with distorted rows/columns (source-cited)",
                  "value": f"{len(defects)} of {source_cells_checked or 0}",
                  "bad": len(defects) > 0}
    if not tables:
        if not defects:
            return None, {"available": False, "reason": "no tables detected in this document"}
        return _clamp(100.0 - _cell_defect_penalty(len(defects))), {
            "available": True, "tables_total": 0, "buckets": {},
            "tables_lost_after_extraction": 0, "stats": [shape_stat]}
    buckets: dict[str, int] = {}
    # `stage` truthy means this is scoring the POST-AI tree. The geometric bbox credit
    # below is Stage 2's own IoU match against MinerU's raw output, frozen before Stage
    # 4 ever touched the tree — exactly the "stale and misleading as a METRIC" data the
    # whole dimension used to be blanked out to avoid (see compute_scorecard). Give
    # every table full credit here instead of re-deriving a stale bucket for it; the
    # bucket is still computed and shown below (for a reviewer's eye), it just is not
    # SCORED post-AI. What still counts either way: a table actually missing from the
    # CURRENT tree (checked against whichever stage `validate()` ran on — never stale),
    # and a row split across a page break (a Stage 2/3 stitching defect that Stage 4
    # does not undo merely by existing — see ROW_SPLIT_POINTS).
    earned = float(len(tables)) if stage else 0.0
    # NOT scored on cell-level grid damage, though it was briefly. check_table_cells
    # cannot currently locate a cell's own words on the page reliably enough to make
    # the claim: it anchors on the cell's first and last few words and takes every
    # page word between them, but fitz returns words in READING order, which in a
    # two-column table interleaves the columns line by line — and the anchor itself
    # matches the first row whose text starts the same way, which on a questionnaire
    # is routinely the wrong row. Measured on Jersey p76: a 34-word answer cell
    # resolved to a 121-word segment spanning x=55–769, the full page width.
    #
    # That failure is not random, it is SELECTED FOR: the straddle test can only fire
    # when the segment spans a corridor, which is exactly when the lookup went wrong.
    # Across Jersey's 81 locatable cells the segment matches the cell for 77 of them
    # (median ratio 0.98) and is a page-wide slab for 3 — and those are the ones that
    # were reported. Every finding was the detector reporting its own lookup failure.
    #
    # Doing this properly means reconstructing the PDF's OWN cells from the corridor
    # grid and comparing cell-to-cell, never touching reading order. Until then this
    # dimension scores what it did before.
    for t in tables:
        credit, bucket = _table_credit(t)
        if not stage:
            earned += credit
        buckets[bucket] = buckets.get(bucket, 0) + 1
    # A table Stage 2 extracted successfully but that never reached the tree is a
    # total loss of that table, so it forfeits the credit already counted above. Post-AI,
    # every table STARTED at full (1.0) credit above, so a lost one gives up that same
    # 1.0 rather than its stale stage-2 bucket credit.
    lost = [f for f in (tpres or {}).get("flags", [])]
    lost_ids = {f["table_id"] for f in lost}
    for t in tables:
        if t["table_id"] in lost_ids:
            earned -= 1.0 if stage else _table_credit(t)[0]
    # A row the stitcher could not safely rejoin after a page break (see
    # ROW_SPLIT_POINTS): every character survives, but the row is split across two
    # adjacent <tr>s in the shipped tree. Only the unresolved ones count —
    # "realigned_and_merged" / "widened_and_merged" means Stage 2 already put the row
    # back together, so there is nothing left here to penalise. The caller has already
    # removed dismissed ones; deduplicated here per physical row.
    splits = _dedupe_row_splits(stitch_anomalies)
    row_splits = [a for a in splits if a.get("action") == "kept_as_separate_row"]
    unconfirmed_splits = [a for a in splits if a.get("action") == "unflagged_continuation"]
    split_points = _row_split_penalty(splits)
    structure_points = min(FIDELITY_STRUCTURE_CAP,
                           _cell_defect_penalty(len(defects)) + split_points)
    return _clamp(100.0 * max(earned, 0.0) / len(tables) - structure_points), {
        "available": True, "tables_total": len(tables), "buckets": buckets,
        "tables_lost_after_extraction": len(lost),
        "rows_split_across_page_break": len(row_splits),
        "rows_possibly_split_unflagged": len(unconfirmed_splits),
        "row_split_points": round(split_points, 2),
        "structure_points": round(structure_points, 2),
        "stats": [
            {"label": "tables detected", "value": len(tables)},
            {"label": "clean geometric match", "value": buckets.get("confident", 0)},
            {"label": "uncertain match (verify)", "value": buckets.get("uncertain", 0),
             "warn": buckets.get("uncertain", 0) > 0},
            {"label": "no bbox — matched by position only",
             "value": buckets.get("unverified", 0), "warn": buckets.get("unverified", 0) > 0},
            {"label": "reclassified as prose (not a table)", "value": buckets.get("reclassified", 0)},
            {"label": "merged page-spanning table (verified complete)",
             "value": buckets.get("continuation", 0)},
            {"label": "failed — flagged in output", "value": buckets.get("failed", 0),
             "bad": buckets.get("failed", 0) > 0},
            {"label": "row split across a page break, not rejoined", "value": len(row_splits),
             "bad": len(row_splits) > 0},
            {"label": "row possibly split at a page break (lower confidence)",
             "value": len(unconfirmed_splits), "warn": len(unconfirmed_splits) > 0},
            {"label": "extracted but LOST before the output", "value": len(lost),
             "bad": len(lost) > 0},
            # check_table_cells' own cell-location approach is still not scored here —
            # see the note above _table_credit's loop, that detector's own findings
            # were wrong often enough to be worthless. This is check_source_fidelity's
            # SEPARATE, purely-structural row/column-width check (ragged_tables et al):
            # no fuzzy text matching, so no shared failure mode with that one.
            shape_stat,
        ],
    }


def _score_stage4(s4: dict) -> tuple[float | None, dict]:
    """Stage 4's own score, straight from check_stage4 — no re-derivation here.

    Returns (None, ...) when stage 4 never ran, which is the normal case. None means the
    dimension is ABSENT rather than failing: the rollup below already skips None scores,
    so a corpus of documents that never used AI post-processing is unaffected.
    """
    if not s4 or s4.get("skipped"):
        return None, {"ran": False,
                      "note": s4.get("reason", "AI post-processing not run for this document")}
    return s4.get("score"), {
        "ran": True,
        "integrity_ok": s4.get("integrity_ok"),
        "critical_flags": [f for f in s4.get("flags", []) if f["severity"] == "critical"],
        "warnings": [f for f in s4.get("flags", []) if f["severity"] != "critical"],
        "batches_total": s4.get("batches_total"),
        "batches_changed": s4.get("batches_changed"),
        "batches_rejected": s4.get("batches_rejected"),
        "rejection_rate": s4.get("rejection_rate"),
        "reprojection_rate": s4.get("reprojection_rate"),
        "column_uniformity_before": s4.get("column_uniformity_before"),
        "column_uniformity_after": s4.get("column_uniformity_after"),
        "subsections_created": s4.get("subsections_created"),
        "tokens": s4.get("tokens"),
        "cost_usd": s4.get("cost_usd"),
    }


def _score_uniqueness(wc: dict, source_duplicates: list | None = None,
                      source_cells_checked: int | None = None) -> tuple[float, dict]:
    md_words = max(wc.get("md_word_occurrences", 0), 1)
    extra = wc.get("extra_total", 0)
    rate = extra / md_words
    # Cell-cited duplicates: content the source PDF prints once, that survives
    # in the output twice (typically a row Stage 4 folded into its rightful
    # section while leaving the bare original behind elsewhere). Each one is
    # independently confirmed against the source PDF, so it costs a flat amount
    # (see CELL_DEFECT_PENALTY), same reasoning as _score_placement's boundary_leaks.
    dups = source_duplicates or []
    # x5 so a 2% excess costs ~10 points — visible without dominating the gate,
    # which is right for a proxy signal this coarse.
    return _clamp(100.0 - rate * 100.0 * 5.0 - _cell_defect_penalty(len(dups))), {
        "extra_word_occurrences": extra, "tree_word_occurrences": md_words,
        "excess_rate_pct": round(rate * 100.0, 2),
        "top_extra": list(wc.get("extra_words", {}).items())[:12],
        "stats": [
            {"label": "words in tree but not in PDF", "value": extra, "warn": rate > 0.01},
            {"label": "as share of tree text", "value": f"{rate * 100.0:.2f}%"},
            {"label": "cells duplicated from elsewhere (source-cited)",
             "value": f"{len(dups)} of {source_cells_checked or 0}",
             "bad": len(dups) > 0},
        ],
    }


def _score_toc(tq: dict) -> tuple[float | None, dict]:
    """Is the document's table of contents fit to build a tree from?

    None (not scored) for documents too short to require an outline — failing a
    3-page memo for having no contents page would punish correct output."""
    if not tq or tq.get("score") is None:
        return None, {"available": False,
                      "reason": (tq or {}).get("detail") or "not judged for this document"}
    p = tq.get("printed_toc") or {}
    status = tq.get("status")
    return _clamp(float(tq["score"])), {
        "available": True,
        "status": status,
        "outline_entries": tq.get("outline_entries"),
        "outline_note": tq.get("outline_note"),
        "printed_toc_pages": p.get("pages") or [],
        "printed_toc_entries": p.get("entries"),
        "printed_toc_verified": p.get("verified"),
        "printed_toc_usable": bool(p.get("usable")),
        "printed_toc_reason": p.get("reason"),
        "rescuable": bool(tq.get("rescuable")),
        "explanation": tq.get("detail"),
        "stats": [
            {"label": "table of contents", "value": (status or "?").upper(),
             "bad": status == "improper", "warn": status == "lacking"},
            {"label": "bookmark entries in the PDF", "value": tq.get("outline_entries"),
             "bad": status == "improper"},
            {"label": "where the outline starts", "value": tq.get("outline_note")},
            {"label": "printed contents page usable?",
             "value": ("yes — " + str(p.get("verified")) + "/" + str(p.get("entries")) + " verified")
                      if p.get("usable") else "no",
             "warn": not p.get("usable") and status != "proper"},
        ],
    }


def _score_integrity(ni: dict, stage1: dict | None,
                     fn: dict | None = None) -> tuple[float, dict]:
    transpositions = ni.get("transpositions", [])
    missing = ni.get("missing") or {}
    # A number the tree has NOWHERE is a real finding. A number present but with
    # fewer occurrences than the PDF usually means one repeated value lost some
    # instances inside an already-flagged failed table — reported, barely
    # penalised. Display the surface form ("25,750"), never the normalised key
    # ("25750"), which appears in neither document.
    absent, reduced = [], []
    for key, info in missing.items():
        if not _significant_number(key):
            continue
        shown = info.get("surface", key) if isinstance(info, dict) else key
        (absent if (isinstance(info, dict) and info.get("absent")) else reduced).append(shown)
    orphans = (stage1 or {}).get("orphan_footnote_refs", []) or []
    fn_excluded = ni.get("footnote_marker_excluded_count", 0)
    seq_gaps = (fn or {}).get("sequence_gaps") or []
    # Missing numbers no longer cost anything.
    #
    # They are reported — a reader should see them — but they do not move the
    # score, because measurement showed essentially all of them are markers, not
    # values: footnote references, list markers, TOC page numbers, and footnote
    # markers glued onto a preceding number ("Terrorism Act 2000" + note "506" ->
    # "2000506"). Penalising them saturated Integrity to 0.0 on one document,
    # which makes the dimension useless: a score pinned at the floor cannot tell
    # you whether anything got better or worse.
    #
    # A number vanishing along with the sentence around it IS real, but that is
    # CONTENT loss and Completeness already measures it with a detector hardened
    # for exactly this (table/footnote restructuring). Re-deriving "is content
    # missing?" here would just be a second, weaker copy of it.
    #
    # What still scores: a TRANSPOSITION (30% -> 3%, a value silently changed —
    # the genuinely dangerous class) and an orphan footnote reference.
    score = (100.0
             - 15.0 * len(transpositions)      # highest-consequence error class
             - 3.0 * min(len(orphans), 10))
    return _clamp(score), {
        "transpositions": transpositions[:10],
        "transposition_count": len(transpositions),
        "absent_numbers": absent[:20], "absent_count": len(absent),
        "reduced_numbers": reduced[:20], "reduced_count": len(reduced),
        "orphan_footnote_refs": orphans[:10],
        "stats": [
            {"label": "possible digit transpositions", "value": len(transpositions),
             "bad": len(transpositions) > 0},
            {"label": "numbers absent from the tree (reported, not scored)",
             "value": len(absent), "warn": len(absent) > 0},
            {"label": "numbers present but fewer times than the PDF", "value": len(reduced)},
            {"label": "footnotes cited with no body", "value": (fn or {}).get("dangling_count", 0),
             "warn": (fn or {}).get("dangling_count", 0) > 0},
            {"label": "footnote bodies whose marker was LOST",
             "value": (fn or {}).get("orphan_definition_count", 0),
             "warn": (fn or {}).get("orphan_definition_count", 0) > 0},
            {"label": "gaps in the footnote numbering (possible fully-lost footnote)",
             "value": len(seq_gaps), "warn": len(seq_gaps) > 0},
            {"label": "numbers excluded — already resolved in the tree as a footnote id",
             "value": fn_excluded},
        ],
    }


def _page_runs(pages: list[int], limit: int = 12) -> str:
    """"63-68" rather than "63, 64, 65, 66, 67, 68".

    Missing pages are overwhelmingly CONTIGUOUS -- a page range the extractor skipped,
    not six unrelated pages -- and the run is the thing worth seeing: Germany 179874's
    six are 63-68, one block, which reads as a dropped span and not as scattered noise.
    """
    ps = sorted({int(p) for p in pages if p})
    if not ps:
        return ""
    runs, start, prev = [], ps[0], ps[0]
    for p in ps[1:]:
        if p == prev + 1:
            prev = p
            continue
        runs.append((start, prev))
        start = prev = p
    runs.append((start, prev))
    out = [f"{a}\u2013{b}" if b > a else str(a) for a, b in runs]
    if len(out) > limit:
        return ", ".join(out[:limit]) + f", +{len(out) - limit} more"
    return ", ".join(out)


# ---------------- human-readable quotes for a dropped/relocated token span --------
# `gap` and `found_elsewhere` findings (below) title themselves with the raw tokens
# lib_content_compare.tokenize() produced for the diff — lowercased, punctuation
# collapsed to nothing ("Article 6(4)" -> "article", "6", "4"). Correct for the
# comparison; unreadable as a quote, and NOT safe to fix at the source: tokenize()'s
# own normalisation is calibrated across the whole validator suite (see its docstring
# for two regressions a smaller change than this one caused). Reconstructed here
# instead, purely for display: re-tokenize the cited PDF page with the SAME regex but
# keep each match's character span, find the finding's own token run inside it, and
# quote the untouched substring between those two offsets — real punctuation and
# casing, because it never left the page.
_READABLE_DOC_CACHE: dict[str, list[str] | None] = {}
_READABLE_PAGE_CACHE: dict[tuple[str, int], tuple[str, list[tuple[str, int, int]]] | None] = {}


def _cleaned_pages(pdf_path: Path) -> list[str] | None:
    """Header/footer-stripped page texts (1-indexed, [0] a blank sentinel) — the
    SAME pipeline lib_content_compare's own comparison tokenizes, reused rather
    than re-detected so this lookup never disagrees with the diff it is explaining
    about what counts as running text. Without this, a running header/footer
    ("CONFIDENTIAL", a doc-control stamp, a page number) sits between the last
    word of one page and the first of the next, and a span that crosses the page
    break — as most do, page breaks falling wherever they fall — never matches as
    contiguous. Cached per document: this reads every page once, needed or not,
    so it must not repeat per finding."""
    key = str(pdf_path)
    if key in _READABLE_DOC_CACHE:
        return _READABLE_DOC_CACHE[key]
    result = None
    try:
        from lib_content_compare import (detect_boilerplate_patterns, page_band_lines,
                                         pdf_page_texts, strip_boilerplate)
        pages = pdf_page_texts(pdf_path)
        band_lines = page_band_lines(pdf_path)
        patterns, _detected = detect_boilerplate_patterns(pages, band_lines=band_lines)
        result = strip_boilerplate(pages, patterns, band_lines=band_lines)
    except Exception:                                       # noqa: BLE001
        result = None
    _READABLE_DOC_CACHE[key] = result
    return result


def _page_token_offsets(pdf_path: Path, pno: int):
    """-> (cleaned page text, [(normalised token, start, end), ...]) or None.
    Cached per (pdf, page): a document's findings routinely repeat a page."""
    key = (str(pdf_path), pno)
    if key in _READABLE_PAGE_CACHE:
        return _READABLE_PAGE_CACHE[key]
    result = None
    pages = _cleaned_pages(pdf_path)
    if pages and 1 <= pno < len(pages):
        try:
            from lib_content_compare import TOKEN_RE, _CURLY_APOSTROPHES, _LIGATURES
            translated = pages[pno].translate(_LIGATURES).translate(_CURLY_APOSTROPHES)
            offsets = [(m.group().lower().strip("-"), m.start(), m.end())
                      for m in TOKEN_RE.finditer(translated)]
            result = (translated, [o for o in offsets if o[0]])
        except Exception:                                   # noqa: BLE001
            result = None
    _READABLE_PAGE_CACHE[key] = result
    return result


def _readable_span(pdf_path: Path | None, pages: list[int], tokens: list[str]) -> str | None:
    if not tokens or not pages or pdf_path is None:
        return None
    lo, hi = min(pages), max(pages)
    # A capped search window: the FILE's own page range (what `pages` actually holds
    # here) can run to dozens of pages, but the dropped span itself is always a few
    # sentences, so it is on one of the first several pages of that range or none.
    combined: list[tuple[str, int, int, int]] = []          # (token, page, start, end)
    for pno in range(lo, min(hi, lo + 6) + 1):
        got = _page_token_offsets(pdf_path, pno)
        if got:
            combined.extend((t, pno, s, e) for t, s, e in got[1])
    toks = [c[0] for c in combined]
    n = len(tokens)
    for i in range(len(toks) - n + 1):
        if toks[i:i + n] != tokens:
            continue
        # Reconstruct verbatim, page by page, joined with a single space wherever
        # the run itself crosses a page boundary (the two sides never share a
        # sentence there once boilerplate is stripped, so a real space belongs).
        parts, seg_page, seg_start = [], combined[i][1], combined[i][2]
        for j in range(i, i + n):
            _tok, pno, _s, e = combined[j]
            if pno != seg_page:
                text, _ = _page_token_offsets(pdf_path, seg_page)
                parts.append(text[seg_start:combined[j - 1][3]])
                seg_page, seg_start = pno, combined[j][2]
        text, _ = _page_token_offsets(pdf_path, seg_page)
        parts.append(text[seg_start:combined[i + n - 1][3]])
        return " ".join(" ".join(p.split()) for p in parts if p.strip())
    return None


def _attach_readable_titles(findings: list[dict], out_root: Path) -> None:
    """Rewrites a `gap`/`found_elsewhere` finding's token-join title with a real
    quote wherever the cited page still turns up the same words. Leaves the title
    exactly as it was (the plain token join) when the page can't be opened or the
    run can't be relocated — never a worse or fabricated answer, just the old one."""
    try:
        pdf_path = resolve_pdf(out_root, None)
    except Exception:                                        # noqa: BLE001
        return
    for f in findings:
        if f["kind"] not in ("gap", "found_elsewhere") or not f.get("pages"):
            continue
        truncated = f["title"].endswith("…")
        tokens = (f["title"][:-1] if truncated else f["title"]).split()
        readable = _readable_span(pdf_path, f["pages"], tokens)
        if readable:
            f["title"] = readable + ("…" if truncated else "")


# ---------------- individually dismissable findings ----------------
def _collect_findings(cl: dict, ni: dict, tp: dict | None, tables: list[dict],
                      eng: dict | None = None, snapshotted: set | None = None,
                      sem: dict | None = None, hier: dict | None = None,
                      tpres: dict | None = None, fn: dict | None = None,
                      build_mismatch: list | None = None,
                      stage2: dict | None = None,
                      orphans: dict | None = None,
                      tcells: dict | None = None,
                      tcons: dict | None = None,
                      appx: dict | None = None,
                      outl: dict | None = None,
                      pc: dict | None = None,
                      empty_segments: list[dict] | None = None) -> list[dict]:
    """Every finding a reviewer can judge one-by-one, each with a content-derived
    key stable across re-extractions (see lib_dismissals). Aggregate signals
    (word coverage %, duplication rate) are deliberately NOT here: they are
    statistics over the whole document, not individual claims, so there is
    nothing coherent to dismiss."""
    out = []
    for r in cl.get("results", []):
        for d in r.get("dropped", []):
            toks = d.get("tokens", [])
            out.append({
                "key": dis.gap_key(r["file"], toks),
                "kind": "gap", "dimension": "completeness",
                "severity": "flagged" if r.get("acknowledged") else "silent",
                "file": r["file"], "pages": r.get("pages", []),
                "title": " ".join(toks[:18]) + ("…" if len(toks) > 18 else ""),
                "detail": f"{len(toks)} token(s) present in the source PDF pages "
                          f"{r['pages'][0]}–{r['pages'][1]} but not in this section"
                          + ("" if not r.get("acknowledged") else
                             " (already acknowledged by a failure marker in the file)"),
            })
    # Source text this section's own pages carry that the section does NOT hold — but
    # which exists elsewhere in the tree. Nothing is lost, so it is not a completeness
    # defect and is deliberately advisory; what it describes is content sitting under
    # the wrong heading, which is the same class as `hollow` a span at a time. It was
    # previously counted in the Completeness panel ("excused — found in full elsewhere")
    # and then dropped, so a reader could see that three spans had been excused and had
    # no way to find out which three or where.
    for r in cl.get("results", []):
        for e in r.get("present_elsewhere", []) or []:
            toks = e.get("tokens", [])
            why = e.get("reorder_note") or (
                f"found {e.get('decomposed_ratio', e.get('found_ratio'))} of it in one "
                "run elsewhere" if e.get("decomposed_ratio") or e.get("found_ratio")
                else "found elsewhere in the tree")
            out.append({
                "key": dis.gap_key("elsewhere|" + r["file"], toks),
                "kind": "found_elsewhere", "dimension": "sectioning",
                "severity": "advisory",
                "file": r["file"], "pages": r.get("pages", []),
                "title": " ".join(toks[:18]) + ("…" if len(toks) > 18 else ""),
                "detail": (f"{len(toks)} token(s) the source prints on pages "
                           f"{r['pages'][0]}–{r['pages'][1]} are not in {r['file']}, but "
                           f"ARE elsewhere in the tree ({why}). Nothing is missing from "
                           "the document — this is content filed under a different "
                           "heading, so a reader or a search that lands on this section "
                           "will not find it here."
                           + (f" Context: “…{e['before']} ⟪{' '.join(toks)}⟫ "
                              f"{e['after']}…”" if e.get("before") or e.get("after")
                              else "")),
            })
    # A section the tree publishes with nothing under it. Listed per-section
    # rather than as a rate because each one is a separate, checkable claim
    # ("open this file and the one named beside it") and because the rate alone
    # reads as noise next to a 99% word coverage.
    # Built under the wrong rule. Not a defect in the content — a defect in HOW the
    # document was produced, which changes what every ratio below means. Reported as
    # a finding rather than a dimension because there is nothing to score: the tree
    # either matches its product's rule or it does not.
    for why in (build_mismatch or []):
        out.append({
            "key": dis.hierarchy_key("buildrule-" + why),
            "kind": "build_rule", "dimension": "sectioning", "severity": "silent",
            "file": None, "pages": [],
            "title": "extracted under the wrong build rule for its product",
            "detail": (why + ". Only run_corpus.py resolves a product's rule; a document "
                       "rebuilt through the dashboard upload, run_hybrid_one or "
                       "run_baseline comes out with the pipeline default. Re-extract it "
                       "through run_corpus.py, or dismiss this if the deviation is "
                       "deliberate."),
        })
    # A section the document's own outline names that never became a node at all.
    # Distinct from `hollow` below only in degree — there the node exists and is
    # empty, here it does not exist — so both are `sectioning` findings.
    for m in ((hier or {}).get("census") or {}).get("missing", []):
        out.append({
            "key": dis.hierarchy_key("absent-" + m["title"]),
            "kind": "section_missing", "dimension": "sectioning", "severity": "silent",
            "file": None, "pages": [m["page"]] if m.get("page") is not None else [],
            "title": f"“{m['title']}” is in the outline but has no node in the tree",
            "detail": ("Stage 1 could not locate this heading in the page text, so no section "
                       "was created and its content merged into the section above it. The "
                       "usual cause is a heading the source prints INSIDE a table: the table "
                       "region is deferred to MinerU and the heading goes with it. Dismiss if "
                       "this outline entry is not a real section."),
        })
    # An appendix the memo PRINTS that never became a node. Closely related to
    # `section_missing` above and on the same dimension, but found a different way and
    # that difference is the point: section_missing compares the tree against the
    # OUTLINE, and these divisions are absent from the outline -- that is why they were
    # missed. Found instead by the "APPENDIX n" label the page itself prints, which is
    # present in the text whether or not anything bookmarked it.
    for sw in ((appx or {}).get("swallowed") or []):
        out.append({
            "key": dis.hierarchy_key("appendix-" + sw["label"]),
            "kind": "appendix_swallowed", "dimension": "sectioning", "severity": "silent",
            "file": sw["absorbed_into"], "pages": [],
            "title": f"{sw['label'].upper()} is printed as a division but has no chunk",
            "detail": (f"The page prints a \u201c{sw['label'].upper()}\u201d label opening this "
                       f"division, but no section was created for it and its "
                       f"{sw['prose_chars']} character(s) of content sit inside "
                       f"{sw['absorbed_into']}. Stage 1 splits on headings and this label "
                       "is not one, so the split depended on the outline having found the "
                       "TITLE line beneath it, and it did not. Dismiss if this label is a "
                       "reference rather than a division of this document."),
        })
    # An outline heading whose TEXT reaches no heading in the shipped tree. The
    # `section_missing` findings above come from the census, which is capped at the
    # product's build depth — so a sub-section it never promised cannot appear there
    # however completely it is gone. This is that blind spot: same dimension, found by
    # comparing the outline's own words against every heading in the tree.
    for m in ((outl or {}).get("missing") or []):
        out.append({
            "key": dis.hierarchy_key("outline-" + str(m.get("title"))),
            "kind": "outline_absent", "dimension": "sectioning", "severity": "silent",
            "file": None, "pages": [m["page"]] if m.get("page") is not None else [],
            "title": f"\u201c{m.get('title')}\u201d is in the outline but reaches no heading in the tree",
            "detail": ("The source outline names this heading and no heading anywhere in the "
                       "shipped tree carries its text — not as a section of its own and not "
                       "inside another file. Distinct from the census above, which only "
                       "promises sections down to this product's build depth. Dismiss if this "
                       "outline entry is not a real heading."),
        })
    # A source page the tree accounts for NOWHERE. This drives the completeness score
    # already -- it is the term that caught Curaçao 164302's 36 skipped pages -- but it
    # had no finding, so the panel said "6 of 153" and neither the findings list, the
    # page review nor the heat map could say WHICH six or let anyone open one. Page
    # review and the heat map both read this list, so emitting here puts the page in
    # all three at once.
    for pno in ((pc or {}).get("pages_missing") or []):
        out.append({
            "key": dis.page_absent_key(pno),
            "kind": "page_absent", "dimension": "completeness", "severity": "silent",
            "file": None, "pages": [pno],
            "title": f"page {pno} has text in the PDF and is in no section of the tree",
            "detail": ("No section claims this page and none of its words are findable "
                       "anywhere in the output, so nothing in the tree records that it "
                       "was dropped. Open the page image beside this to see what is on "
                       "it. Dismiss if the page carries nothing that belongs in the "
                       "extraction (a cover sheet, a divider, a blank verso)."),
        })
    for h in cl.get("hollow_sections", []):
        into = (h.get("absorbed_by") or [None])[0]
        out.append({
            "key": dis.hierarchy_key("hollow-" + h["file"]),
            "kind": "hollow", "dimension": "sectioning", "severity": "silent",
            "file": h["file"], "pages": h.get("pages", []),
            "title": f"{h['file']} is published with no content of its own",
            "detail": (f"keeps {h.get('retention_pct')}% of the {h.get('body_tokens')} token(s) "
                       f"the source prints under this heading; the body is in {into}. "
                       "Nothing is lost from the document — it is filed under the wrong "
                       "heading, so a reader or a search that lands here finds an empty node. "
                       "Dismiss if this heading is not a section in its own right."),
        })
    if tp and not tp.get("skipped"):
        for f in tp.get("flags", []):
            out.append({
                "key": dis.placement_key(f["table_id"], f.get("pages", [])),
                "kind": "placement", "dimension": "placement", "severity": "silent",
                "file": None, "pages": f.get("pages", []),
                "title": f"{f['table_id']} may be under the wrong heading",
                "detail": f.get("detail", ""),
            })
    for t in tables:
        if _table_credit(t)[1] != "failed":
            continue
        out.append({
            "key": dis.table_key(t["table_id"], t.get("pages", [])),
            "kind": "table", "dimension": "fidelity", "severity": "flagged",
            "file": None, "pages": t.get("pages", []),
            "title": f"{t['table_id']} could not be extracted",
            "detail": (t.get("reason") or "MinerU produced no usable table.")
                      + " Dismiss only if this region is NOT actually a table.",
        })
    for tok, info in (ni.get("missing") or {}).items():
        if not (_significant_number(tok) and info.get("absent")):
            continue
        out.append({
            "key": dis.number_key(info.get("surface", tok)),
            "kind": "number", "dimension": "integrity", "severity": "advisory",
            "file": None, "pages": info.get("pages", []),
            "title": f"{info.get('surface', tok)} absent from the tree",
            "detail": f"appears {info.get('pdf_count')}× in the PDF, "
                      f"{info.get('tree_count')}× in the tree",
        })
    for t in ni.get("transpositions", []):
        out.append({
            "key": dis.transposition_key(t.get("missing", ""), t.get("extra", "")),
            "kind": "transposition", "dimension": "integrity", "severity": "advisory",
            "file": None, "pages": t.get("missing_pages", []),
            "title": f"{t.get('missing')} → {t.get('extra')}",
            "detail": "same digits, different value — possible digit transposition",
        })
    # Circularity findings: these are the only ones NOT derived from fitz, so
    # they are the only ones that can catch a fitz blind spot (see
    # check_engine_agreement.py). Ranked as silent loss unless the page at least
    # survives as a snapshot in the output.
    snapshotted = snapshotted or set()
    for u in (eng or {}).get("unreadable_pages", []):
        shown = u["page"] in snapshotted
        out.append({
            "key": dis.unreadable_key(u["page"]),
            "kind": "unreadable", "dimension": "completeness",
            "severity": "flagged" if shown else "silent",
            "file": None, "pages": [u["page"]],
            "title": f"Page {u['page']} — no text extractor can read this page",
            "detail": u["why"] + (
                ". The output keeps a page snapshot, so a reader can still see it."
                if shown else
                ". Nothing in the output indicates this page exists — a text-only "
                "comparison cannot see this loss, which is exactly why this check exists."),
        })
    # A clause that kept its wording but lost a negation now states the
    # OPPOSITE. Highest-consequence finding in the whole system for a compliance
    # document, and nothing else in the stack can see it.
    for f in (sem or {}).get("meaning_flips", []):
        out.append({
            "key": dis.meaning_key(f.get("pdf_phrase", "")),
            "kind": "meaning", "dimension": "integrity" if f["severity"] != "high" else "completeness",
            "severity": "silent" if f["severity"] == "high" else "advisory",
            "file": None, "pages": [f.get("page")] if f.get("page") else [],
            "title": f"'{f['word']}' dropped — clause meaning changed",
            "detail": f"PDF: \u201c…{f.get('pdf_phrase','')}…\u201d  ||  tree: \u201c…{f.get('tree_phrase','')}…\u201d",
        })
    # A negation the tree holds FEWER times than the PDF. Unlike the flip above this
    # needs no context, so neither a table's reordering nor glued words can hide it —
    # and it is the only thing that sees a dropped Yes/No ANSWER, which the flip test
    # deliberately skips because an answer cell has no stable context to match.
    #
    # ADVISORY, on the advisory `integrity` dimension, so it is visible without moving
    # a verdict: a shortfall can also be produced by gluing, where a swallowed word
    # stops being its own token while its text is still on the page (Cyprus reports
    # none 2 -> 0 with both instances present). Until those are told apart this points
    # a reviewer at a page; it does not decide anything.
    for d in (sem or {}).get("negation_shortfall", []):
        out.append({
            "key": dis.meaning_key(f"shortfall|{d['word']}"),
            "kind": "negation_shortfall", "dimension": "integrity", "severity": "advisory",
            "file": None, "pages": [],
            "title": f"'{d['word']}' appears {d['shortfall']}\u00d7 fewer in the tree than the PDF",
            "detail": (f"PDF {d['pdf_count']}\u00d7, tree {d['tree_count']}\u00d7. A dropped "
                       f"negation or Yes/No answer reads as the OPPOSITE obligation. "
                       f"Advisory: word-gluing can also lower a count without losing text."),
        })
    for f in (tpres or {}).get("flags", []):
        out.append({
            "key": dis.table_key(f.get("table_id", ""), f.get("pages", [])),
            "kind": "table_lost", "dimension": "fidelity", "severity": "silent",
            "file": None, "pages": f.get("pages", []),
            "title": f"{f.get('table_id')} was extracted but is not in the output",
            "detail": f.get("detail", ""),
        })
    # Cell-vs-PDF grid findings are deliberately NOT collected: check_table_cells
    # cannot yet locate a cell's words on the page without straying into the
    # neighbouring column, and the straddle test only fires WHEN that happens, so
    # every finding it produced was its own lookup failure rather than a merged cell.
    # See the note in _score_fidelity. `tcells` is threaded through so the check's
    # output stays reachable while the location step is rebuilt.
    #
    # Source text absent from the tree ANYWHERE. The only loss finding that does not
    # depend on locating content in a particular file, which is why it catches what the
    # per-section diff cannot: measured, 2 of 8 dropped paragraphs vs none missed of 15.
    # Only spans of MIN_SCORED_SPAN+ tokens become findings — below that the residue is
    # tokeniser disagreement, reported in the check's own output but not as a defect.
    for s in (tcons or {}).get("scored_spans", []):
        out.append({
            "key": dis.gap_key("<document>", s["text"].split()),
            "kind": "text_absent", "dimension": "completeness", "severity": "silent",
            "file": None, "pages": [s["page"]] if s.get("page") else [],
            "title": f"{s['tokens_missing']} word(s) on page {s['page']} appear "
                     f"nowhere in the tree",
            "detail": (f"“…{s.get('before', '')} ⟪{s['text']}⟫ {s.get('after', '')}…”. "
                       "Compared against the WHOLE tree, so this is not a misplacement: "
                       "these words are absent from the document, not merely from the "
                       "section that should hold them."),
        })
    # Both halves of a broken footnote. The uncited-body case is the interesting
    # one: the body survived, so the page still LOOKS right, but the ~5pt inline
    # marker was lost, which nothing else would notice.
    for x in (fn or {}).get("dangling_refs", []):
        out.append({
            "key": dis.hierarchy_key("fn-dangling-" + str(x["id"])),
            "kind": "fn_dangling", "dimension": "integrity", "severity": "advisory",
            "file": (x.get("cited_in") or [None])[0], "pages": [],
            "title": f"[^{x['id']}] is cited but has no body anywhere",
            "detail": f"cited {x.get('cite_count')}x in "
                      + ", ".join(x.get("cited_in") or []) + " — a reader cannot follow it",
        })
    # Orphan tables. Two different claims, so two different severities: a LOST orphan
    # is content the reader will never see, a RECOVERED one is content that WAS
    # rescued and only needs an eye on where it landed.
    for o in (orphans or {}).get("_lost", []) or []:
        out.append({
            "key": dis.hierarchy_key("orphan-lost-" + str(o.get("page")) + "-"
                                     + (o.get("preview") or "")[:60]),
            "kind": "orphan_table", "dimension": "completeness", "severity": "silent",
            "file": None, "pages": [o.get("page")] if o.get("page") else [],
            "title": f"table on page {o.get('page')} reached no section "
                     f"({o.get('chars')} chars, {o.get('rows')} rows)",
            "detail": "MinerU extracted this table correctly, but no region in its own "
                      "section could take it, so nothing spliced it into the tree and "
                      "its rows are absent from the output"
                      + (f" — {o['declined_because']}" if o.get("declined_because") else "")
                      + ": " + (o.get("preview") or "")[:180],
        })
    for o in (orphans or {}).get("_dup", []) or []:
        out.append({
            "key": dis.hierarchy_key("orphan-dup-" + str(o.get("page")) + "-"
                                     + (o.get("preview") or "")[:60]),
            "kind": "orphan_duplicate", "dimension": "completeness", "severity": "advisory",
            "file": None, "pages": [o.get("page")] if o.get("page") else [],
            "title": f"table on page {o.get('page')} reached no section, but its text is "
                     f"already in the tree ({o.get('chars')} chars)",
            "detail": "MinerU emitted a block overlapping content other regions already "
                      "emit, so nothing is lost and adopting it would duplicate: "
                      + (o.get("preview") or "")[:180],
        })
    for o in (stage2 or {}).get("orphans_recovered", []) or []:
        out.append({
            "key": dis.hierarchy_key("orphan-recovered-" + str(o.get("table_id")) + "-"
                                     + str(o.get("page"))),
            "kind": "orphan_recovered", "dimension": "completeness", "severity": "advisory",
            "file": None, "pages": [o.get("page")] if o.get("page") else [],
            "title": f"table on page {o.get('page')} was recovered into "
                     f"{o.get('table_id')} ({o.get('chars')} chars, "
                     f"{o.get('rows_added')} row(s) added)",
            "detail": "No region claimed this table. The region that took it is the "
                      "nearest one in the SAME section, judged by the page's own "
                      "reading order, and only rows absent from that section were "
                      "added — so it cannot land under the previous heading and cannot "
                      "duplicate what is already there"
                      + (", displacing " + ", ".join(
                          f"{r['rows']} row(s) misfiled in {r['table_id']}"
                          for r in (o.get("removed_from") or []))
                         if o.get("removed_from") else "")
                      + ": " + (o.get("preview") or "")[:180],
        })
    # A row split across a page break that Stage 2's stitcher (hybrid_extract.
    # stitch_table_html) tried to rejoin. Every occurrence is logged whether or not
    # the rejoin succeeded — see ROW_SPLIT_POINTS for why only the unresolved ones
    # cost anything. `pages[block]` recovers the actual page: `pages` is the whole
    # table's page range and `block` is this row's offset into it, since the
    # stitcher checks exactly the first row of every page after the table's first.
    for a in (stage2 or {}).get("stitch_anomalies", []) or []:
        block = a.get("block")
        page = _stitch_page(a)
        preview = " ".join(a.get("row_text") or a.get("surplus_text") or [])[:160]
        quoted = f" The row: “{preview}…”" if preview else ""
        table_id = a.get("table_id")
        if a.get("action") == "kept_as_separate_row":
            out.append({
                "key": _stitch_key(a),
                "kind": "row_split", "dimension": "fidelity", "severity": "silent",
                "file": None, "pages": [page] if page else [],
                "title": f"{table_id} page {page}: one source row was rendered as two",
                "detail": (f"This row continues from the previous page "
                          f"({a.get('previous_cells')} cell(s) there vs "
                          f"{a.get('new_cells')} here), but no column alignment read "
                          f"confidently enough ({a.get('confidence')}/100"
                          + (f" — {a['reason']}" if a.get("reason") else "")
                          + ") to rejoin it without risking fusing the wrong cells "
                            "together, so it was kept as its own row instead of "
                            "guessed at. Nothing is missing, but the tree now shows "
                            "two adjacent rows where the source has one — the second "
                            "with an empty question and an empty answer."
                          + quoted),
            })
        elif a.get("action") == "unflagged_continuation":
            # The new page's first row had its OWN non-empty leading cell (usually a
            # sub-question label), so the blank-cell check above never even looked at
            # it — this is caught only because the previous row's own last cell stops
            # mid-sentence. Weaker evidence than row_split above (see
            # ROW_SPLIT_UNCONFIRMED_WEIGHT): a real split reads this way, but so can
            # a genuinely new row that happens to follow an answer ending mid-list, so
            # this is reported at lower confidence and costs less.
            out.append({
                "key": _stitch_key(a),
                "kind": "row_split_unflagged", "dimension": "fidelity",
                "severity": "silent",
                "file": None, "pages": [page] if page else [],
                "title": f"{table_id} page {page}: possible row split, unconfirmed",
                "detail": ("The previous row's own last cell stops mid-sentence "
                          f"(“…{a.get('previous_row_tail', '')[-120:]}”) and "
                          "this row's leading cell already holds text of its own, so "
                          "the usual blank-leading-cell continuation check never "
                          "considered it. This READS like the same one-row-rendered-"
                          "as-two defect, but nothing here rules out a genuinely new "
                          "row that happens to follow an answer ending without a "
                          f"period — confidence {a.get('confidence')}/100."
                          + quoted),
            })
        else:
            # realigned_and_merged / widened_and_merged: Stage 2 already put the row
            # back together. Advisory only — same status as orphan_recovered above —
            # because a continuation merge is exactly the kind of automatic repair
            # worth a reviewer's eye even when it worked.
            out.append({
                "key": _stitch_key(a),
                "kind": "row_continuation_merged", "dimension": "fidelity",
                "severity": "advisory",
                "file": None, "pages": [page] if page else [],
                "title": f"{table_id} page {page}: continuation row rejoined "
                        f"({a.get('action')})",
                "detail": ("This row continued from the previous page and was "
                          "automatically merged back into it — confirm the pairing "
                          "reads correctly rather than fusing the wrong cells."
                          + quoted),
            })
    for x in (fn or {}).get("orphan_definitions", []):
        out.append({
            "key": dis.hierarchy_key("fn-orphan-" + str(x["id"])),
            "kind": "fn_orphan", "dimension": "integrity", "severity": "advisory",
            "file": (x.get("defined_in") or [None])[0], "pages": [],
            "title": f"[^{x['id']}] has a body but nothing cites it",
            "detail": "the inline marker (a ~5pt superscript) was lost, so the footnote is "
                      "unreachable from the text: " + ", ".join(x.get("defined_in") or []),
        })
    # Two different footnote numbers with byte-identical bodies. Sometimes a
    # running footer/doc-control line misread as a footnote body (see
    # pdf2mdtree.py's repeated_text); sometimes just a legal document citing
    # the same short provision twice, which is legitimate. The output alone
    # can't tell which, so this is a "look closer" flag, not an accusation.
    for x in (fn or {}).get("duplicate_bodies", []):
        ids = x["ids"]
        out.append({
            "key": dis.hierarchy_key("fn-dup-" + "-".join(ids)),
            "kind": "fn_duplicate", "dimension": "integrity", "severity": "advisory",
            "file": None, "pages": [],
            "title": f"{', '.join(f'[^{i}]' for i in ids)} share identical body text",
            "detail": f"could be a misread footer, or a genuinely repeated citation — verify by hand: “{x['text'][:120]}”",
        })
    # A footnote whose reference AND body are BOTH gone leaves no [^n] anywhere
    # for the two findings above to catch — only the hole in the numbering
    # does. Weaker evidence (an inference, not a directly observed [^n]), so
    # still advisory, same as every other footnote finding here.
    seq_range = (fn or {}).get("sequence_range")
    for n in (fn or {}).get("sequence_gaps", []):
        out.append({
            "key": dis.hierarchy_key("fn-gap-" + str(n)),
            "kind": "fn_gap", "dimension": "integrity", "severity": "advisory",
            "file": None, "pages": [],
            "title": f"[^{n}] missing from the footnote sequence",
            "detail": f"footnotes here run {seq_range[0]}–{seq_range[1]}, but {n} never appears "
                      "as either a reference or a body anywhere in the tree — likely lost "
                      "entirely rather than just its marker or just its body.",
        })
    for f in (hier or {}).get("flags", []):
        if f.get("kind") not in ("wrong_parent", "wrong_depth"):
            continue
        out.append({
            "key": dis.hierarchy_key(f.get("title", "")),
            "kind": "hierarchy", "dimension": "placement", "severity": "silent",
            "file": f.get("path"), "pages": [f["page"]] if f.get("page") else [],
            "title": f"{f.get('title','')} is nested wrongly",
            "detail": f.get("detail", ""),
        })
    for d in (eng or {}).get("disagreements", []):
        out.append({
            "key": dis.engine_key(d["page"]),
            "kind": "engine", "dimension": "completeness", "severity": "advisory",
            "file": None, "pages": [d["page"]],
            "title": f"Page {d['page']} — an independent parser reads {d['ratio']}× more text than fitz",
            "detail": f"fitz {d['fitz_chars']} chars vs pypdf {d['pypdf_chars']}. Every "
                      "fitz-derived number for this page (coverage, gaps, numeric checks) is "
                      "unreliable here, because the extractor is built on fitz too.",
        })
    # A multi-part answer cell with a blank segment where a sub-question's own answer
    # should be — see find_empty_answer_segments. Silent: the repeated boilerplate this
    # always happens with ("N/a. Please see X above.") still appears elsewhere in the
    # SAME cell, so nothing about the output hints that one copy of it is missing.
    for e in empty_segments or []:
        # A precise page hit stays a single page; a missed anchor (routine on table
        # cells, whose PDF reading order scrambles a contiguous token match) falls
        # back to the file's own page range rather than an empty list — an empty
        # `pages` silently drops the finding everywhere Page Review is built from it
        # (see find_empty_answer_segments), even though it is still an active,
        # non-dismissed finding that counts against the score.
        pages = [e["page"]] if e.get("page") else (e.get("file_pages") or [])
        out.append({
            "key": dis.hierarchy_key(f"empty-segment-{e['file']}-{e['before'][-40:]}"),
            "kind": "empty_answer_segment", "dimension": "completeness", "severity": "silent",
            "file": e["file"], "pages": pages,
            "title": f"{e['file']}: {e['empty_segments']} answer segment(s) blank "
                    "between two others",
            "detail": ("This cell holds several blank-line-separated answers, one per "
                      "sub-question in the paired question cell, and one of them is "
                      "empty where the source has an answer — most often a repeated "
                      "short boilerplate line ('N/a. Please see X above.') that still "
                      "appears elsewhere in this SAME cell, so word coverage reads 100% "
                      "and no other check sees the gap. "
                      f"…{e['before']} ⟦MISSING⟧ {e['after']}…"),
        })
    return out


def _apply_dismissals(cl: dict, ni: dict, tp: dict | None, tables: list[dict],
                      dismissed: set) -> tuple[dict, dict, dict | None, list[dict]]:
    """Return copies with dismissed findings removed, for SCORING only. The
    originals are still reported so nothing disappears from view."""
    if not dismissed:
        return cl, ni, tp, tables

    cl2 = dict(cl)
    results = []
    for r in cl.get("results", []):
        kept = [d for d in r.get("dropped", [])
                if dis.gap_key(r["file"], d.get("tokens", [])) not in dismissed]
        if kept or r.get("relocated") or r.get("present_elsewhere") or r.get("changed"):
            r2 = dict(r)
            r2["dropped"] = kept
            results.append(r2)
    cl2["results"] = results
    cl2["hollow_sections"] = [h for h in cl.get("hollow_sections", [])
                              if dis.hierarchy_key("hollow-" + h["file"]) not in dismissed]

    tp2 = tp
    if tp and not tp.get("skipped"):
        tp2 = dict(tp)
        tp2["flags"] = [f for f in tp.get("flags", [])
                        if dis.placement_key(f["table_id"], f.get("pages", [])) not in dismissed]

    # A dismissed table is asserted NOT to be a table at all, so it leaves the
    # Fidelity ratio entirely (numerator and denominator) rather than counting
    # as a success it never was.
    tables2 = [t for t in tables
               if dis.table_key(t["table_id"], t.get("pages", [])) not in dismissed]

    ni2 = dict(ni)
    ni2["missing"] = {k: v for k, v in (ni.get("missing") or {}).items()
                      if dis.number_key(v.get("surface", k) if isinstance(v, dict) else k)
                      not in dismissed}
    ni2["transpositions"] = [t for t in ni.get("transpositions", [])
                             if dis.transposition_key(t.get("missing", ""), t.get("extra", ""))
                             not in dismissed]
    return cl2, ni2, tp2, tables2


# ---------------- per-page heat-map ----------------
def _page_states(cl: dict, ni: dict, tables: list[dict], stage1: dict | None,
                 eng: dict | None = None, dismissed: set | None = None,
                 hier: dict | None = None) -> dict:
    """One state per PDF page for the heat-map. 'unvalidatable' is deliberately
    a separate state from 'silent': an image-only or diagram page scores ~0% on
    every text-conservation check while being perfectly fine, and painting
    those red would make the whole map cry wolf. Takes the DISMISSAL-FILTERED
    cl/ni so a dismissed finding stops colouring its page too — otherwise the
    map would keep accusing a page the reviewer already cleared."""
    n_pages = (stage1 or {}).get("pages") or 0
    # Did the MinerU tier hand its markdown through whole, rather than re-chunking it?
    # That is a deliberate choice at the last-resort tier (mineru_full_extract's
    # RAW_MINERU_OUTPUT), and it makes every per-section ratio meaningless.
    _unchunked = "raw" in str((stage1 or {}).get("structure_source", "")).lower()

    silent, flagged = set(), set()
    # Attribute a finding to ONE page (the first of its section's range) rather
    # than every page it could touch — smearing a single gap across a 6-page
    # range is what made an earlier version flag nearly every page.
    for r in cl.get("results", []):
        if not r["dropped"]:
            continue
        (silent if not r["acknowledged"] else flagged).add(r["pages"][0])

    # A section the outline promises that never became a node is a loss on the page
    # where that section starts. Without this the heat-map showed a clean page for a
    # finding the list was reporting — the census is the only signal here that does
    # not come from cl/ni/tables, so it had to be passed in explicitly.
    for m in ((hier or {}).get("census") or {}).get("missing", []):
        if m.get("page") and dis.hierarchy_key("absent-" + m["title"]) not in (dismissed or set()):
            silent.add(m["page"])

    ok_table_pages, failed_table_pages = set(), set()
    for t in tables:
        credit, bucket = _table_credit(t)
        for pg in t.get("pages", []):
            (failed_table_pages if bucket == "failed" else ok_table_pages).add(pg)
    flagged |= failed_table_pages

    # Numeric findings only paint a page red when the number is ABSENT from the
    # tree entirely AND nothing on that page already announces a loss. A number
    # merely present fewer times than in the PDF is usually one repeated value
    # that lost instances inside an already-flagged failed table — and a page
    # whose table failed loudly is explained by that marker, so colouring it red
    # would double-report one problem and make honestly-flagged pages look like
    # silent data loss.
    for tok, info in (ni.get("missing") or {}).items():
        if (_significant_number(tok) and info.get("absent") and info.get("pages")
                and info["pages"][0] not in flagged):
            silent.add(info["pages"][0])

    snapshotted = set((stage1 or {}).get("pages_snapshotted") or [])
    # A page fitz cannot read is definitely missing from the tree — not merely
    # unjudgeable — so it is a real loss, silent unless a snapshot shows it.
    dismissed = dismissed or set()
    for u in (eng or {}).get("unreadable_pages", []):
        if dis.unreadable_key(u["page"]) in dismissed:
            continue
        (flagged if u["page"] in snapshotted else silent).add(u["page"])

    states = {}
    for pg in range(1, n_pages + 1):
        if pg in silent:
            states[pg] = "silent"
        elif pg in flagged:
            states[pg] = "flagged"
        elif pg in snapshotted and pg not in ok_table_pages:
            states[pg] = "unvalidatable"
        else:
            states[pg] = "ok"
    return states


# ---------------- top level ----------------
def compute_scorecard(out_root: Path, validation: dict | None = None,
                      stage: int | None = None) -> dict:
    """validation: the dict produced by the UI's _run_validation. Computed here if
    not supplied, so this module also works standalone from the CLI.

    The standalone set must stay IDENTICAL to _run_validation's. It used to run
    only five checks, so `python check_scorecard.py <dir>` silently scored a
    different document from the dashboard: no negation penalty, no heading
    penalty, no lost-table penalty, and — because table_presence never ran to
    verify them — every merged continuation scored 0.75 instead of 1.0. On the
    pilot that was Fidelity 91.8 from the CLI vs 96.4 from the UI, on the same
    files, with the weakest dimension reported as a different one."""
    out_root = Path(out_root)
    if validation is None:
        # Standalone CLI path: score on the ONE canonical set (lib_validate), the
        # same nine checks the dashboard + run_corpus + gallery publish all use, so
        # `python check_scorecard.py <dir>` can never diverge from them again.
        from lib_validate import validate as _validate  # lazy: lib_validate.run_and_gate imports us back
        validation = _validate(out_root, stage=stage)

    # A job can legitimately have an OUTPUT tree and no stage 1 at all: the summary AI
    # route (product_rules.AI_PIPELINE) is chosen before Stage 1 and never runs it, so
    # there is no 01_* directory to resolve. Every use of `stage1` below is already written
    # `(stage1 or {}).get(...)` -- the value has always been optional -- and this resolve
    # was the one line that turned "no stage 1" into a crash instead of a None.
    #
    # Narrow on purpose: it only changes a path that previously raised, so no job that
    # scores today can score differently. The dimensions that DESCRIBE a stage-1 tree
    # (placement, sectioning) simply score on the empty evidence they already handle.
    try:
        stage1_dir = resolve_stage_dir(out_root, 1)
    except StageDirectoryNotFoundError:
        stage1_dir = None
    stage1 = _load(stage1_dir / "stage1_report.json") if stage1_dir else None
    import_meta = _load(out_root / "corpus_meta.json") or {}
    if stage1 is None and import_meta.get("import_kind") == "exported_chunks":
        stage1 = {"pages": import_meta.get("pages"), "structure_source": "imported_export"}
    stage2 = next((_load(p) for p in sorted(out_root.glob("0*_*mineru*/stage2_report.json"))), None)
    stage3 = next((_load(p) for p in sorted(out_root.glob("0*_*final*/stage3_report.json"))), None)
    tables = (stage2 or {}).get("tables", []) or []

    wc = validation.get("word_coverage") or {}
    pcov = validation.get("page_coverage") or {}
    cl = validation.get("content_localized") or {}
    ni = validation.get("numeric_integrity") or {}
    tp = validation.get("table_placement")

    # Reviewer-dismissed false positives: excluded from SCORING and from the
    # heat-map, but still returned (flagged dismissed) and restorable — hiding a
    # finding without a trace is how a validation dashboard becomes a rubber
    # stamp. Keyed by source-PDF hash, so dismissals survive re-running the doc.
    eng = validation.get("engine_agreement") or {}
    snapshotted = set((stage1 or {}).get("pages_snapshotted") or [])
    dkey = dis.doc_key(resolve_pdf(out_root, None))
    dismissals = dis.load_for_doc(out_root, dkey)
    dismissed_keys = set(dismissals)
    tpres = validation.get("table_presence") or {}
    # Annotate each table with its merge-verification result so _table_credit can
    # score a verified merge at full credit and a dropped one as the loss it is.
    _cv = {c["table_id"]: c.get("verified") for c in (tpres.get("continuations") or [])}
    for _t in tables:
        if _t["table_id"] in _cv:
            _t["continuation_verified"] = _cv[_t["table_id"]]
    sem = validation.get("semantic_integrity") or {}
    hier = validation.get("heading_hierarchy") or {}
    fnote = validation.get("footnote_integrity") or {}
    # Did this tree get built the way its product says it should be? Stage 1 records
    # what it did (build_depth / clause_tables in headings_manifest.json), so a
    # document produced by an entry point that does not consult product_rules can be
    # caught here even though nothing complained at extraction time.
    product = pr.product_for_job(out_root)
    build_mismatch = pr.build_matches_rule(
        product, hier.get("build_depth"), hier.get("clause_tables"))
    _tree = " ".join(
        re.sub(r"<[^>]+>", " ", p.read_text(errors="ignore"))
        for p in sorted(out_root.glob("0*_*final*/*.md")))
    orphans = orphan_summary(stage2, _tree)
    empty_segments = find_empty_answer_segments(out_root, stage)
    tcells = validation.get("table_cells") or {}
    tcons = validation.get("text_conservation") or {}
    pc = validation.get("page_coverage") or {}
    appx = validation.get("appendix_integrity") or {}
    outl = validation.get("outline_coverage") or {}
    findings = _collect_findings(cl, ni, tp, tables, eng, snapshotted, sem, hier,
                                tpres, fnote, build_mismatch, stage2, orphans, tcells,
                                tcons, appx, outl, pc, empty_segments)
    _attach_readable_titles(findings, out_root)
    source_fidelity = validation.get("source_fidelity") or {}
    findings.extend(source_fidelity.get("findings", []))
    for f in findings:
        f["dismissed"] = f["key"] in dismissed_keys
        if f["dismissed"]:
            f["dismissal"] = dismissals.get(f["key"], {})
    # section_boundary_leak answers the SAME question _score_placement does --
    # "did this land under the right heading?" -- just at cell grain instead of
    # whole-table grain. Previously it only forced the GATE down (source_review,
    # below), leaving the Placement card reading a clean 100.0/"0 outside range"
    # for a document with a real, cited leaked cell: the number a reviewer looks
    # at first said nothing had moved. Advisory findings and dismissed ones are
    # excluded here for the same reason they are excluded from the gate override.
    boundary_leaks = [f for f in source_fidelity.get("findings", [])
                     if f.get("kind") == "section_boundary_leak" and not f["dismissed"]
                     and f.get("evidence", {}).get("confidence") != "advisory"]
    # Stage 4 absorbing an orphaned row into its rightful section (see
    # section_boundary_leak above) can leave the bare original behind as a
    # verbatim duplicate rather than a misplacement -- same root cause, cited
    # against the source PDF the same way, just classified duplicated_content
    # once the content has a home elsewhere. Uniqueness's own question is
    # "is any content duplicated in the output?", so it belongs there, the same
    # way section_boundary_leak belongs in Placement.
    source_duplicates = [f for f in source_fidelity.get("findings", [])
                        if f.get("kind") == "duplicated_content" and not f["dismissed"]
                        and f.get("evidence", {}).get("confidence") != "advisory"]
    # A cell that came out split, merged with its neighbour, or left its table's rows
    # at inconsistent logical widths is a structural defect at the SAME grain as the
    # two above, just about the table's own shape rather than where it landed or
    # whether it repeats -- Fidelity's own question ("were tables extracted with the
    # right content and structure?"). ragged_tables/cell_split/row_merge/column_merge/
    # block_merge do their own geometry/text-matching, with no shared failure mode
    # with check_table_cells' abandoned cell-location approach (see _score_fidelity).
    table_shape_defects = [f for f in source_fidelity.get("findings", [])
                          if f.get("kind") in ("inconsistent_table_columns", "cell_split",
                                               "row_merge", "column_merge", "block_merge",
                                               "row_alignment", "table_as_text")
                          and not f["dismissed"]
                          and f.get("evidence", {}).get("confidence") != "advisory"]
    # A source table whose cells survive as bare text, never becoming an output
    # table at all -- the confident direction of this call (the uncertain, opposite
    # direction is table_without_source_grid, kept advisory-only above). Same
    # "right structure?" question as the shape defects above.
    source_cells_checked = (source_fidelity.get("coverage") or {}).get("source_cells")
    missing_answer_findings = [f for f in source_fidelity.get("findings", [])
                              if f.get("kind") == "missing_cell_answer" and not f["dismissed"]
                              and f.get("evidence", {}).get("confidence") != "advisory"]
    # DETECTION vs SCORING. Everything above is detection: every finding stays in
    # `findings`, dismissed or not, charged or not. What follows decides which of them
    # cost points — only an active (not dismissed), non-duplicate instance does, and a
    # table-level symptom already explained by a charged row split is not billed again.
    boundary_leaks = _dedupe_by_key(boundary_leaks)
    source_duplicates = _dedupe_by_key(source_duplicates)
    missing_answer_findings = _dedupe_by_key(missing_answer_findings)
    # Empty answer segments and row splits are built by _collect_findings above, so
    # their dismissal state lives on those finding objects / their shared key.
    empty_segment_count = len(_dedupe_by_key(
        [f for f in findings if f["kind"] == "empty_answer_segment" and not f["dismissed"]]))
    active_splits = _dedupe_row_splits(
        [a for a in (stage2 or {}).get("stitch_anomalies", []) or []
         if _stitch_key(a) not in dismissed_keys])
    try:
        _tree_dir = resolve_stage_dir(out_root, stage or 3)
    except Exception:                                        # noqa: BLE001
        _tree_dir = None
    absorbed = _absorbed_by_row_splits(table_shape_defects, active_splits,
                                       _table_files(_tree_dir))
    for f in absorbed:
        f["charged"] = False
        f["absorbed_by"] = "row_split"
    absorbed_ids = {id(f) for f in absorbed}
    table_shape_defects = _dedupe_by_key([f for f in table_shape_defects
                                          if id(f) not in absorbed_ids])
    cl_s, ni_s, tp_s, tables_s = _apply_dismissals(cl, ni, tp, tables, dismissed_keys)
    hier_s = _apply_census_dismissals(hier, dismissed_keys)

    n_pages = (stage1 or {}).get("pages") or 0
    # Did the MinerU tier hand its markdown through whole, rather than re-chunking it?
    # That is a deliberate choice at the last-resort tier (mineru_full_extract's
    # RAW_MINERU_OUTPUT), and it makes every per-section ratio meaningless.
    _unchunked = "raw" in str((stage1 or {}).get("structure_source", "")).lower()
    # A surviving clause that lost its negation now states the OPPOSITE of the
    # source. It was previously reported as a finding but moved no score, so a
    # document with an inverted legal obligation could still gate as PASS.
    negation_flips = len([f for f in (sem.get("meaning_flips") or [])
                          if f.get("severity") == "high"
                          and dis.meaning_key(f.get("pdf_phrase", "")) not in dismissed_keys])
    unread_active = [u for u in eng.get("unreadable_pages", [])
                     if dis.unreadable_key(u["page"]) not in dismissed_keys]
    unread_silent = sum(1 for u in unread_active if u["page"] not in snapshotted)
    unread_flagged = len(unread_active) - unread_silent

    dims = {}
    for key, (score, detail) in {
        "completeness": _score_completeness(wc, cl_s, unread_silent, unread_flagged, n_pages,
                                           negation_flips, pcov, orphans=orphans,
                                           tcons=tcons,
                                           empty_segment_count=empty_segment_count,
                                           missing_answers=missing_answer_findings,
                                           source_cells_checked=source_cells_checked),
        "placement": _score_placement(tp_s, len(tables_s), hier,
                                      boundary_leaks=boundary_leaks,
                                      source_cells_checked=source_cells_checked),
        "fidelity": _score_fidelity(tables_s, tpres, tcells,
                                    active_splits, stage,
                                    shape_defects=table_shape_defects,
                                    source_cells_checked=source_cells_checked),
        "uniqueness": _score_uniqueness(wc, source_duplicates=source_duplicates,
                                        source_cells_checked=source_cells_checked),
        "integrity": _score_integrity(ni_s, stage1, fnote),
        "ai_postprocess": _score_stage4(validation.get("stage4") or {}),
        "toc": _score_toc(validation.get("toc_quality") or {}),
        "sectioning": _score_sectioning(cl_s, hier_s,
                                        validation.get("structure_profile") or {}, outl,
                                        _unchunked),
    }.items():
        dims[key] = {"score": score, "detail": detail, **HELP[key]}
    if absorbed and isinstance(dims["fidelity"].get("detail"), dict):
        dims["fidelity"]["detail"]["shape_findings_absorbed_by_row_split"] = len(absorbed)
    # `fidelity` used to be blanked out entirely on the post-AI re-score, because its
    # geometric bbox counts (see _score_fidelity's table-credit loop) are a re-skin of
    # Stage 2's IoU match, frozen before Stage 4 ever ran, and genuinely stale once the
    # tree has been rewritten. But the row-split terms in that same score
    # (ROW_SPLIT_POINTS / ROW_SPLIT_UNCONFIRMED_WEIGHT) describe a Stage 2/3 STITCHING
    # defect, not a Stage 4 one — whether a row was split across a page break has
    # nothing to do with what the AI pass did afterward, so blanking the whole
    # dimension hid a real, still-current defect behind a stale-bbox excuse that didn't
    # apply to it. Scored post-AI now, stale bbox noise and all, because a document
    # with a genuine unresolved row split showing a clean post-AI gate is the worse
    # failure mode.

    # SHORT-DOCUMENT MODE (see SHORT_DOC_MAX_PAGES): a document too short to have a
    # structure is gated on word coverage alone. `toc` and `fidelity` are still
    # COMPUTED and shown — they are useful description — they just don't set the
    # verdict, because on a 6-page memo they measure the absence of something that
    # was never supposed to be there and then fail the document for it.
    short_doc = 0 < n_pages < SHORT_DOC_MAX_PAGES
    # `sectioning` gates only where the expected node count is KNOWN — see
    # product_rules.SECTIONING_GATES for the measurement that made this product-scoped.
    # Everywhere else it stays computed and displayed but advisory.
    sect_gates = pr.sectioning_gates(product)
    critical = tuple(k for k in CRITICAL_DIMENSIONS
                     if (k != "sectioning" or sect_gates))
    gating_set = SHORT_DOC_DIMENSIONS if short_doc else critical
    special_mode = {
        "mode": "short_document",
        "pages": n_pages,
        "threshold": SHORT_DOC_MAX_PAGES,
        "gated_on": list(gating_set),
        "not_gating": [k for k in critical if k not in gating_set],
        "why": (f"This document is {n_pages} pages — under the {SHORT_DOC_MAX_PAGES}-page bar "
                f"for structural scoring. It is extracted by MinerU directly and judged on "
                f"WORD COVERAGE alone. The structure dimensions (TOC, fidelity) and the "
                f"per-section gap ratios are still shown but do not set the verdict: a memo "
                f"this short has no hierarchy to get right, often no printed contents page and "
                f"no tables, so those measures report the absence of things that were never "
                f"there — and the gap ratios are per-FILE, which pins a one-file tree to zero "
                f"on a single imperfect span."),
    } if short_doc else None

    # A tree built under a PRODUCT RULE rather than the pipeline default has to say
    # so, for the same reason the short-document rule does: every gating dimension
    # here is a ratio, and building flat shrinks all three denominators at once, so
    # the number means something different from the identical number on a sibling.
    # Measured on Australia__181814, depth 3 against depth 0:
    #   sectioning   /40 sections -> /18   one miss costs 2.5 -> 5.6
    #   completeness /48 files    -> /22   one silent gap    2.1 -> 4.5
    #   fidelity     /92 tables   -> /12   one weak match    1.1 -> 8.3
    # Nothing is re-weighted to compensate — a correction factor would be invented,
    # not measured. The scorecard states the rule and leaves the arithmetic honest.
    build_depth = (hier or {}).get("build_depth")
    if special_mode is None and build_depth is not None and build_depth != DEFAULT_SECTION_DEPTH:
        bits = [f"subsections are NOT extracted (build depth {build_depth}): every "
                f"level-1 clause is one node holding everything beneath it"]
        if (hier or {}).get("clause_tables"):
            bits.append("a clause's tables are grouped into one region and handed to "
                        "MinerU whole, rather than split at subsection boundaries")
        special_mode = {
            "mode": "product_rule_flat_sections",
            "pages": n_pages,
            "build_depth": build_depth,
            "clause_tables": bool((hier or {}).get("clause_tables")),
            "gated_on": list(gating_set),
            "not_gating": [],
            "why": ("This document was built under a rule for its product, not the "
                    "pipeline default: " + "; ".join(bits) + ". Every gating dimension "
                    "is a RATIO, and this rule shrinks all of their denominators — "
                    "sections, files and tables are each far fewer — so one defect "
                    "costs several times what it would on a default build. Read these "
                    "scores against this document's own history, NOT against a sibling "
                    "extracted the normal way."),
        }

    for key, d in dims.items():
        d["critical"] = key in gating_set
    gating = [k for k in gating_set if dims.get(k, {}).get("score") is not None]
    worst = min((dims[k]["score"] for k in gating), default=None)
    weakest = min(gating, key=lambda k: dims[k]["score"], default=None)
    gate = ("unknown" if worst is None else
            "pass" if worst >= GATE_PASS else
            "review" if worst >= GATE_REVIEW else "fail")
    # A source-fidelity finding no longer forces review by itself the way it
    # used to -- now that section_boundary_leak/duplicated_content cost real
    # points in Placement and Uniqueness (see their scorers), enough of them
    # already pull `worst` below GATE_PASS on their own. A single cited cell
    # still shows up in `findings` and in gate_reasons below, just without
    # silently overriding a genuine high score -- the numeric threshold is
    # what decides pass vs review here.
    #
    # An *error* is a different situation: it means the cell-level audit
    # could not run at all (unreadable PDF, etc), so the dimension scores
    # above were never told anything about boundary leaks/duplicates one way
    # or the other -- a clean `worst` here reflects "we didn't look", not
    # "we looked and it's fine". That still forces review.
    source_review = [f for f in source_fidelity.get("findings", [])
                     if f["key"] not in dismissed_keys
                     and f.get("evidence", {}).get("confidence") != "advisory"]
    gate_reasons = []
    if source_review:
        gate_reasons.append(f"Source fidelity: {len(source_review)} structural/content finding(s) require review.")
    if source_fidelity.get("error"):
        gate_reasons.append("Source fidelity could not be checked: " + str(source_fidelity["error"]))
        if gate in {"pass", "unknown"}:
            gate = "review"
    # Surfaced separately so an advisory dimension scoring badly is still
    # visible without silently changing the verdict.
    advisory_low = [k for k in ADVISORY_DIMENSIONS
                    if dims[k]["score"] is not None and dims[k]["score"] < GATE_REVIEW]

    states = _page_states(cl_s, ni_s, tables_s, stage1, eng, dismissed_keys, hier_s)
    counts: dict[str, int] = {}
    for st in states.values():
        counts[st] = counts.get(st, 0) + 1

    n_dismissed = sum(1 for f in findings if f["dismissed"])
    return {
        "gate": gate,
        "input_kind": import_meta.get("import_kind", "pipeline"),
        "gate_reasons": gate_reasons,
        "source_fidelity": {"review_count": len(source_review),
                            "error": source_fidelity.get("error"),
                            "coverage": source_fidelity.get("coverage", {}),
                            "limitations": source_fidelity.get("limitations", [])},
        "worst_score": worst,
        "weakest_dimension": weakest,
        "advisory_low": advisory_low,
        "critical_dimensions": list(gating_set),
        # Non-null only when a special scoring rule applied — the UI must SAY so,
        # because a verdict reached by different rules than its neighbours is
        # misleading if it looks identical to them.
        # The single source of truth for "which dimensions set the verdict", so the
        # fallback chain cannot disagree with the scorecard about it.
        "gating_dimensions": list(gating_set),
        "sectioning_advisory": not sect_gates,
        "special_mode": special_mode,
        "gate_thresholds": {"pass": GATE_PASS, "review": GATE_REVIEW},
        "findings": findings,
        "dismissed_count": n_dismissed,
        "active_finding_count": len(findings) - n_dismissed,
        "doc_key": dkey,
        "dimensions": dims,
        "pages": {"total": (stage1 or {}).get("pages") or 0,
                  "states": {str(k): v for k, v in states.items()},
                  "counts": counts, "state_help": PAGE_STATE_HELP},
        "tables": [{"table_id": t["table_id"], "pages": t.get("pages", []),
                    "bucket": _table_credit(t)[1], "match_iou": t.get("match_iou"),
                    "match_status": t.get("match_status"), "rows": t.get("rows"),
                    "cols": t.get("cols"), "reason": t.get("reason")} for t in tables],
        "stage3": stage3 or {},
        "headings": {"total": (stage1 or {}).get("headings_total"),
                     "matched": (stage1 or {}).get("headings_matched"),
                     "structure_source": (stage1 or {}).get("structure_source")},
        # This document's own shape. Surfaced here (not just in validation.json)
        # so the dashboard can compare it against its product's other
        # jurisdictions without re-reading every sibling's validation file. The
        # COMPARISON itself is never stored — see check_structure_profile's
        # module docstring.
        "structure": validation.get("structure_profile") or {},
        "calibrated": False,   # thresholds are defaults, not validated against a gold set
        # Which tree these scores describe. None means the extraction gate (stage 3),
        # the historical and default meaning of this file. A number means the content
        # checks were re-pointed at that stage — a post-AI re-score. It is stamped
        # rather than inferred from the filename because both scorecards have the same
        # shape, and a reader who cannot tell them apart will compare a post-AI score
        # against a neighbour's extraction score and conclude the wrong thing.
        "scored_stage": stage,
        "passed": gate == "pass",
    }


GATE_COLOR = {"pass": GREEN, "review": YELLOW, "fail": RED, "unknown": DIM}


def print_report(report: dict):
    gate = report["gate"]
    color = GATE_COLOR[gate]
    print(color(BOLD(f"  {gate.upper()}  ")) + DIM(
        f"  weakest gating dimension: {report['weakest_dimension'] or 'n/a'}"
        f" @ {report['worst_score'] if report['worst_score'] is not None else '?'}"))
    print(DIM("  (score-based verdict is set by the worst of "
              + ", ".join(report.get("critical_dimensions", [])) + ")"))
    for reason in report.get("gate_reasons", []):
        print(YELLOW("  " + reason))
    # The special rule comes before any number, because it changes what the numbers
    # MEAN. The dashboard has always rendered this; the CLI did not, which made the
    # terminal the one place a document scored under a different rule looked
    # identical to one scored normally.
    sm = report.get("special_mode")
    if sm:
        print("  " + YELLOW(BOLD(f"⚡ SPECIAL RULE — {str(sm.get('mode','')).replace('_',' ')}")))
        for line in textwrap.wrap(sm.get("why", ""), 92):
            print(DIM("     " + line))
        if sm.get("not_gating"):
            print(DIM("     not setting the verdict: " + ", ".join(sm["not_gating"])))
    print_structural_damage(report)
    print()
    for key, d in report["dimensions"].items():
        score = d["score"]
        tag = "" if d.get("critical") else DIM(" advisory")
        if score is None:
            print(f"  {DIM(d['label'].ljust(14))} {DIM('n/a  ' + d['detail'].get('reason', ''))}")
            continue
        c = GREEN if score >= GATE_PASS else (YELLOW if score >= GATE_REVIEW else RED)
        bar = "█" * int(score / 5) + "·" * (20 - int(score / 5))
        print(f"  {BOLD(d['label'].ljust(14))} {c(str(score).rjust(5))}  {c(bar)}{tag}")
        print(f"  {DIM(' ' * 14 + d['what'])}")
    if report.get("advisory_low"):
        print(DIM(f"\n  advisory dimensions below {GATE_REVIEW} (not gating): "
                  + ", ".join(report["advisory_low"])))
    pages = report["pages"]
    print(f"\n  {BOLD('pages')}  " + "  ".join(
        f"{k}: {v}" for k, v in sorted(pages["counts"].items())) + f"   (of {pages['total']})")
    if report["tables"]:
        tb: dict[str, int] = {}
        for t in report["tables"]:
            tb[t["bucket"]] = tb.get(t["bucket"], 0) + 1
        print(f"  {BOLD('tables')} " + "  ".join(f"{k}: {v}" for k, v in sorted(tb.items())))
    print_section_map(report)
    print(DIM("\n  Thresholds are provisional defaults, not calibrated against a "
              "human-labelled set — read scores as 'where to look first'."))


# How many promised sections are printed in full before the map switches to
# showing only the branches that have a hole in them. A 39-section memorandum is
# readable whole; the 471-section privacy surveys are not, and a wall of ✓ is
# where a reader stops looking — which defeats the point of drawing it.
SECTION_MAP_FULL_MAX = 80

MARKS = {
    "built": ("✓", GREEN),
    "hollow": ("○", YELLOW),
    "missing": ("✗", RED),
}


def print_structural_damage(report: dict, out=print) -> None:
    """The headline this dashboard exists to produce, printed with the verdict.

    A score is a summary; an inspector needs the sentence. The verdict line can
    say PASS while four consecutive subsections are unreachable, because
    `sectioning` is advisory — so the damage is stated next to the verdict rather
    than left to a number further down that a reader has no reason to open."""
    d = (report.get("dimensions", {}).get("sectioning") or {}).get("detail") or {}
    if not d.get("available"):
        return
    hollow, missing = d.get("hollow_count") or 0, d.get("missing_count") or 0
    if not (hollow or missing):
        return
    bits = []
    if hollow:
        bits.append(f"{hollow} are EMPTY")
    if missing:
        bits.append(f"{missing} were never built")
    promised, intact = d.get("promised"), d.get("intact")
    lead = (f"of the {promised} sections this document has, {intact} came through "
            f"with their content — {', '.join(bits)}"
            if promised is not None else
            f"{', '.join(bits)} of {d.get('sections_checked')} sections in the tree")
    out("  " + RED(BOLD("⚠ SECTIONS  ")) + RED(lead))
    for b in d.get("branches", [])[:4]:
        line = (f"      inside {b['title'][:52]} — {b['defective']} of its "
                f"{b['children']} subsections have no content of their own")
        if b.get("absorbers"):
            line += DIM("  (their text is in "
                        + ", ".join(Path(a).stem for a in b["absorbers"]) + ")")
        out(line)
    out(DIM("      full map below · an EMPTY section still exists by name, so a "
            "reader or a search that lands on it finds nothing"))


def _section_map_rows(smap: list[dict]) -> tuple[list[dict], int]:
    """(rows to show, count elided). Everything, unless the outline is too long
    to read whole — then only the branches with a hole in them: each defective
    entry plus its nearest shallower ancestors, so a subsection still reads under
    the section it belongs to."""
    if len(smap) <= SECTION_MAP_FULL_MAX:
        return smap, 0
    keep: set[int] = set()
    for i, s in enumerate(smap):
        if s["status"] == "built":
            continue
        keep.add(i)
        lvl = s["level"]
        for j in range(i - 1, -1, -1):
            if smap[j]["level"] < lvl:
                keep.add(j)
                lvl = smap[j]["level"]
                if lvl <= 1:
                    break
    rows = [s for i, s in enumerate(smap) if i in keep]
    return rows, len(smap) - len(rows)


def print_section_map(report: dict, out=print) -> None:
    """The document's own outline, one line per section, marked.

    ✓ built and holds its own content   ○ built but EMPTY   ✗ never built

    Indentation is the outline's own level, so a subsection reads as a
    subsection: the question this answers is not just how many sections are gone
    but WHERE — four consecutive holes under one heading is a table that
    swallowed a branch, one hole on its own is a heading that failed to match."""
    d = (report.get("dimensions", {}).get("sectioning") or {}).get("detail") or {}
    smap = d.get("section_map") or []
    if not smap:
        return
    holes = [s for s in smap if s["status"] != "built"]
    out("")
    out(f"  {BOLD('section map')}  " + DIM(
        f"{MARKS['built'][0]} built   {MARKS['hollow'][0]} built but EMPTY   "
        f"{MARKS['missing'][0]} never built"))
    if not holes:
        out(DIM(f"    all {len(smap)} section(s) the outline promises are built "
                f"and hold their own content"))
        return

    show, elided = _section_map_rows(smap)
    for s in show:
        mark, color = MARKS[s["status"]]
        pad = "    " + "  " * max(0, s["level"] - 1)
        title = s["title"][:66]
        note = ""
        if s["status"] == "hollow":
            note = DIM(f"  — keeps {s.get('retention_pct')}% of its own body; "
                       f"the rest is under {s.get('absorbed_by')}")
        elif s["status"] == "missing":
            note = DIM(f"  — no node in the tree (page {s.get('page')})")
        # The two rollups. A parent that lost children says WHERE the hole is;
        # the node that swallowed them says WHY.
        if s.get("children_defective"):
            note += RED(f"  ⚠ {s['children_defective']} of {s['children']} "
                        f"subsection(s) hold no content of their own")
        if s.get("absorbed"):
            note += YELLOW("  ← absorbed " + ", ".join(
                t.split(" ")[0] for t in s["absorbed"]))
        out(f"{pad}{color(mark)} {title}{note}")
    if elided:
        out(DIM(f"    … {elided} section(s) with no defect not shown"))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    out_root = Path(args.out_root).resolve()
    banner("Extraction scorecard")
    report = compute_scorecard(out_root)
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
