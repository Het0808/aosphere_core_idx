"""Does each scored dimension actually MOVE when the thing it measures breaks?

Nothing tested this. `grep -l _score_ tests/` matched six files before this one and
not one of them called a scoring function — every dimension's arithmetic, every
weight, and the gate thresholds were unpinned, so a refactor could silently flatten
any of them to a constant and the suite would stay green.

The shape of every test here is the same, and it is the question the scorecard
exists to answer: score a CLEAN input, break exactly one thing, assert the score
moved by the amount the formula in HELP promises. That makes the published formula
executable rather than decorative — HELP['completeness']['formula'] says a silent
gap costs 100x(sections with a gap / sections), and test_one_silent_gap_section...
is that sentence, run.

Two properties are pinned deliberately because they are counter-intuitive and a
future reader would "fix" them:

  * missing numbers do NOT move Integrity (see _score_integrity's comment) — they
    are overwhelmingly footnote/list markers, and scoring them pinned the dimension
    at 0, which makes it useless for telling better from worse.
  * a flagged loss costs a QUARTER of a silent one (FLAGGED_WEIGHT). If honest
    failure markers cost as much as silent disappearance, the rational move would
    be to hide failures.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_scorecard import (  # noqa: E402
    FLAGGED_WEIGHT, GATE_PASS, GATE_REVIEW, NEGATION_FLIP_PENALTY, STRUCTURE_FAIL_SCORE,
    _score_completeness, _score_fidelity, _score_integrity, _score_placement,
    _score_sectioning, _score_stage4, _score_toc, _score_uniqueness,
)
from check_structure_profile import BLOB_MIN_PAGES  # noqa: E402


# ---------------- fixtures: a clean document of each shape ----------------
def wc(coverage=100.0, extra=0, tree_words=10_000):
    return {"coverage_adjusted_pct": coverage, "md_word_occurrences": tree_words,
            "extra_total": extra, "extra_words": {}}


def gap(file="07-marketing.md", tokens=40, acknowledged=False, pages=(20, 24)):
    """One section carrying one dropped span."""
    return {"file": file, "pages": list(pages), "acknowledged": acknowledged,
            "dropped": [{"tokens": [f"w{i}" for i in range(tokens)]}],
            "present_elsewhere": []}


def cl(results=(), files=10, hollow=(), sections_checked=10):
    return {"files_scanned": files, "results": list(results),
            "hollow_sections": list(hollow), "hollow_count": len(hollow),
            "sections_checked": sections_checked}


def pc(pages=50, missing=0):
    return {"pages_with_text": pages, "missing_count": missing, "coverage_pct": 100.0}


def census(promised=10, missing_nodes=0, hollow_nodes=0):
    """A section map with `promised` entries, some never built, some built empty."""
    sections, hollow = [], []
    for i in range(promised):
        node = None if i < missing_nodes else f"{i:02d}-section.md"
        sections.append({"title": f"Section {i}", "level": 1, "page": i + 1, "node": node})
    for i in range(missing_nodes, missing_nodes + hollow_nodes):
        hollow.append({"file": f"{i:02d}-section.md", "pages": [i + 1, i + 1],
                       "retention_pct": 3.0, "body_tokens": 200,
                       "absorbed_by": ["99-elsewhere.md"]})
    return ({"census": {"available": True, "promised": promised, "sections": sections,
                        "missing": [s for s in sections if s["node"] is None],
                        "extra_count": 0, "extra": []},
             "flags": [], "headings_total": promised},
            hollow)


def table(tid="table_001", ok=True, status="confident", method="iou", **kw):
    return {"table_id": tid, "ok": ok, "match_status": status, "match_method": method,
            "pages": [10], **kw}


# ---------------- completeness ----------------
def test_a_clean_document_scores_100():
    score, _ = _score_completeness(wc(), cl(), n_pages=50, pc=pc())
    assert score == 100.0


def test_one_silent_gap_section_costs_its_full_share():
    """HELP formula: -100 x (sections with silent gap / sections). 1 of 10 -> -10."""
    score, detail = _score_completeness(wc(), cl([gap()], files=10), n_pages=50, pc=pc())
    assert score == 90.0
    assert detail["files_with_silent_gap"] == 1


def test_a_flagged_gap_costs_a_quarter_of_a_silent_one():
    """The output SAYS content is missing there, so it keeps partial credit. If this
    ever equals the silent cost, hiding failures becomes the rational move."""
    silent, _ = _score_completeness(wc(), cl([gap()], files=10), n_pages=50, pc=pc())
    flagged, _ = _score_completeness(wc(), cl([gap(acknowledged=True)], files=10),
                                     n_pages=50, pc=pc())
    assert flagged == 97.5
    assert (100.0 - flagged) == (100.0 - silent) * FLAGGED_WEIGHT


def test_word_coverage_deficit_is_doubled():
    """2% of a legal document's words is a lot of prose, not a rounding error."""
    score, _ = _score_completeness(wc(coverage=98.0), cl(), n_pages=50, pc=pc())
    assert score == 96.0


def test_a_page_missing_from_the_tree_is_scored_as_silent_loss():
    score, detail = _score_completeness(wc(), cl(), n_pages=50, pc=pc(50, missing=2))
    assert score == 96.0
    assert detail["pages_missing"] == 2


def test_an_unreadable_page_costs_even_though_coverage_cannot_see_it():
    """Nothing expected, nothing found, coverage 100% — a scanned page is invisible
    to a fitz-vs-fitz comparison and must be charged here or it scores perfect."""
    score, _ = _score_completeness(wc(), cl(), n_pages=50, pc=pc(), unread_silent=1)
    assert score == 98.0


def test_a_snapshotted_unreadable_page_costs_a_quarter_as_much():
    score, _ = _score_completeness(wc(), cl(), n_pages=50, pc=pc(), unread_flagged=4)
    assert score == 98.0            # 100 - 100 x (4/50) x 0.25


def test_one_inverted_clause_drops_a_clean_document_out_of_pass():
    """An obligation read backwards is the highest-consequence defect here.

    NOTE a documented-intent mismatch, pinned here as it actually behaves rather than
    as it is described. NEGATION_FLIP_PENALTY's comment claims "one flip drops a clean
    document to REVIEW, two to FAIL" — but 2 x 15 lands on exactly 70.0, and the gate
    is `worst >= GATE_REVIEW -> review`, so two flips still REVIEW and it takes three
    to fail. Either the comment or the penalty is wrong; changing the penalty moves
    every score in the corpus, so it is reported, not silently adjusted."""
    one, _ = _score_completeness(wc(), cl(), n_pages=50, pc=pc(), negation_flips=1)
    two, _ = _score_completeness(wc(), cl(), n_pages=50, pc=pc(), negation_flips=2)
    three, _ = _score_completeness(wc(), cl(), n_pages=50, pc=pc(), negation_flips=3)
    assert one == 100.0 - NEGATION_FLIP_PENALTY == 85.0
    assert GATE_REVIEW <= one < GATE_PASS
    assert two == 70.0 and two >= GATE_REVIEW        # still REVIEW, not FAIL
    assert three < GATE_REVIEW


def test_a_one_file_tree_is_not_charged_the_per_section_ratios():
    """The MinerU last-resort tier emits its raw markdown whole, on purpose. With
    files == 1 a single imperfect span is 100 x 1/1 and takes the entire score —
    measured on 172_G20/Canada (Ontario)__176861: 106% coverage, completeness 0.0."""
    score, detail = _score_completeness(wc(), cl([gap()], files=1), n_pages=50, pc=pc())
    assert score == 100.0
    assert "not scored" in detail["stats"][2]["value"]


def test_a_short_document_is_not_charged_the_per_section_ratios_either():
    score, _ = _score_completeness(wc(), cl([gap()], files=10),
                                   n_pages=BLOB_MIN_PAGES - 1, pc=pc(BLOB_MIN_PAGES - 1))
    assert score == 100.0


def test_coverage_and_gaps_accumulate_rather_than_masking_each_other():
    score, _ = _score_completeness(wc(coverage=98.0), cl([gap()], files=10),
                                   n_pages=50, pc=pc())
    assert score == 86.0            # 100 - 4 (coverage) - 10 (one silent gap of ten)


# ---------------- sectioning ----------------
def test_a_tree_that_built_every_promised_section_scores_100():
    hier, hollow = census(promised=10)
    score, _ = _score_sectioning(cl(hollow=hollow), hier)
    assert score == 100.0


def test_a_section_the_outline_promises_but_the_tree_never_built_is_charged():
    hier, hollow = census(promised=10, missing_nodes=1)
    score, detail = _score_sectioning(cl(hollow=hollow), hier)
    assert score == 90.0
    assert detail["stats"]


def test_a_section_published_with_no_body_costs_the_same_as_one_never_built():
    """From where a reader stands these are one defect — the clause cannot be reached
    under its own name — so they share a denominator and a price."""
    never, _ = _score_sectioning(*_sectioning_args(promised=10, missing_nodes=1))
    empty, _ = _score_sectioning(*_sectioning_args(promised=10, hollow_nodes=1))
    assert never == empty == 90.0


def _sectioning_args(promised=10, missing_nodes=0, hollow_nodes=0):
    hier, hollow = census(promised, missing_nodes, hollow_nodes)
    return cl(hollow=hollow), hier


def outl(total=40, absent=()):
    """check_outline_coverage's report: the uncapped question, over its own denominator."""
    return {"available": True, "outline_count": total,
            "delivered": total - len(absent),
            "missing": [{"title": t, "level": 2, "page": 47} for t in absent],
            "missing_count": len(absent)}


def test_an_outline_heading_that_reaches_no_heading_in_the_tree_is_charged():
    """Marketing Restrictions Hungary 167023. The census is capped at the product's build
    depth, so a level-2 heading it never promised cannot show up there: 15 sections
    promised, 15 built, sectioning 100.0 — while the tree carried no heading anywhere for
    "7.1 Private Placement Regime", and the scorecard raised that as a sectioning finding
    which moved nothing."""
    hier, hollow = census(promised=15)
    clean, _ = _score_sectioning(cl(hollow=hollow), hier, None, outl(total=47))
    score, detail = _score_sectioning(cl(hollow=hollow), hier, None,
                                      outl(total=47, absent=["7.1 Private Placement Regime"]))
    assert clean == 100.0
    assert score == round(100.0 - 100.0 / 47, 1) == 97.9
    assert detail["outline_absent_count"] == 1
    assert any("reaching no heading" in st["label"] and st.get("bad")
               for st in detail["stats"])


def test_the_outline_term_keeps_its_own_denominator():
    """Not folded into the census rate. The two populations are different sizes — 47
    outline headings against 15 promised sections — and merging them would dilute the
    census term fourfold: three unbuilt sections would go from 80.0 to 93.6."""
    hier, hollow = census(promised=15, missing_nodes=3)
    score, _ = _score_sectioning(cl(hollow=hollow), hier, None, outl(total=47))
    assert score == 80.0


def test_one_heading_lost_both_ways_is_charged_once():
    """The outline check re-reports a section the census already counted as never built.
    One defect, one charge — kept on the census rate, which is the harsher denominator."""
    hier, hollow = census(promised=10, missing_nodes=1)
    both, detail = _score_sectioning(cl(hollow=hollow), hier, None,
                                     outl(total=40, absent=["Section 0"]))
    assert both == 90.0
    assert detail["outline_absent_count"] == 0


def test_sectioning_is_unchanged_when_no_outline_coverage_ran():
    hier, hollow = census(promised=10, missing_nodes=1)
    assert _score_sectioning(cl(hollow=hollow), hier, None, None)[0] == 90.0
    assert _score_sectioning(cl(hollow=hollow), hier, None,
                             {"available": False, "outline_count": 0})[0] == 90.0


def test_a_document_that_never_divided_cannot_score_above_the_failure_mark():
    """The blob test `structure` used to own, surviving as a hard floor: however few
    individual holes are countable, an undivided tree is unusable."""
    hier, hollow = census(promised=10)
    blob = {"chunks": 3, "pages": 50, "concentration_pct": 95.0,
            "second_chunk_words": 10, "tiny_chunks": 2}
    score, detail = _score_sectioning(cl(hollow=hollow), hier, blob)
    assert score == STRUCTURE_FAIL_SCORE
    assert detail["stats"][0]["value"] == "NO"


def test_without_an_outline_the_denominator_falls_back_to_what_the_tree_holds():
    """No outline means no independent statement of what the document contains, so
    'never built' is unknowable and only the hollow test can run."""
    hollow = [{"file": "a.md", "pages": [1, 2], "retention_pct": 2.0,
               "body_tokens": 300, "absorbed_by": ["b.md"]}]
    score, _ = _score_sectioning(cl(hollow=hollow, sections_checked=20),
                                 {"census": {"available": False}})
    assert score == 95.0            # 1 hollow of the 20 sections actually checked


# ---------------- placement ----------------
def test_every_table_under_the_right_heading_scores_100():
    score, _ = _score_placement({"flags": [], "tables_total": 10, "headings_total": 20},
                                10, {"flags": [], "headings_total": 20})
    assert score == 100.0


def test_a_table_outside_its_section_page_range_is_charged():
    tp = {"flags": [{"table_id": "table_003", "pages": [40], "detail": "p40 vs 12-18"}],
          "tables_total": 10, "headings_total": 20}
    score, detail = _score_placement(tp, 10, {"flags": [], "headings_total": 20})
    assert score == 90.0
    assert detail["flagged_tables"] == 1


def test_a_heading_filed_under_the_wrong_parent_is_the_same_defect_as_a_moved_table():
    hier = {"flags": [{"kind": "wrong_parent", "title": "2.1 Scope", "page": 9}],
            "headings_total": 20}
    score, _ = _score_placement({"flags": [], "tables_total": 10, "headings_total": 20},
                                10, hier)
    assert score == 95.0            # one wrongly-nested heading of twenty


def test_placement_is_absent_not_zero_when_the_check_could_not_run():
    score, detail = _score_placement({"skipped": True, "reason": "no manifest"}, 0)
    assert score is None and detail["available"] is False


# ---------------- fidelity ----------------
def test_every_table_matched_cleanly_scores_100():
    score, _ = _score_fidelity([table(f"t{i}") for i in range(5)])
    assert score == 100.0


def test_a_failed_table_keeps_quarter_credit_for_being_flagged():
    tables = [table(f"t{i}") for i in range(4)] + [table("t4", ok=False)]
    score, detail = _score_fidelity(tables)
    assert score == 85.0            # (4 x 1.0 + 0.25) / 5
    assert detail["buckets"]["failed"] == 1


def test_a_table_extracted_then_LOST_forfeits_all_its_credit():
    """The one outcome worth strictly less than an honest failure marker: Stage 2 got
    the table, and it never reached the tree, so the reader sees neither it nor a
    warning that it is gone."""
    tables = [table(f"t{i}") for i in range(5)]
    score, detail = _score_fidelity(tables, {"flags": [{"table_id": "t0", "pages": [3]}]})
    assert score == 80.0
    assert detail["tables_lost_after_extraction"] == 1


def test_an_uncertain_geometric_match_costs_less_than_a_failure():
    tables = [table(f"t{i}") for i in range(4)] + [table("t4", status="uncertain")]
    score, _ = _score_fidelity(tables)
    assert score == 92.0            # (4 + 0.6) / 5


def test_fidelity_is_absent_not_zero_for_a_document_with_no_tables():
    score, detail = _score_fidelity([])
    assert score is None and detail["available"] is False


def test_cell_level_grid_damage_does_NOT_move_fidelity():
    """Briefly it did, and that was wrong — pinned here so it is not re-wired without
    fixing the detector first.

    check_table_cells asks the right question (does the rendered grid still match the
    PDF's?) and its corridor detection works, but its cell-LOCATION step does not: it
    anchors on a cell's first and last words and takes every page word between them,
    while fitz returns words in READING order, which interleaves a two-column table's
    columns line by line. Measured on Jersey p76, a 34-word answer cell resolved to a
    121-word segment spanning the full page width and starting a row too early.

    The failure is selected for, which is what made it convincing: the straddle test can
    only fire when a segment crosses a corridor, and a segment can only cross one when
    the lookup went wrong. 77 of Jersey's 81 locatable cells resolve correctly (median
    ratio 0.98); the 3 that resolve to page-wide slabs are the ones that were reported.
    Scoring that moved 9 of 40 corpus gates from pass to review on nothing."""
    tables = [table(f"t{i}") for i in range(4)]
    tcells = {"per_table": {"t0": {"located": 10, "merged": 5, "merge_rate": 0.5}},
              "merge_count": 5, "column_merges": 5, "row_merges": 0,
              "cells_located": 40, "cells_checked": 80}
    assert _score_fidelity(tables, None, tcells)[0] == _score_fidelity(tables)[0] == 100.0


def test_fidelity_still_charges_the_table_defects_it_can_actually_measure():
    """Reverting the cell term must not have taken the real ones with it."""
    tables = [table(f"t{i}") for i in range(4)]
    lost, _ = _score_fidelity(tables, {"flags": [{"table_id": "t1", "pages": [3]}]})
    assert lost == 75.0


# ---------------- integrity ----------------
def test_a_clean_document_has_perfect_integrity():
    score, _ = _score_integrity({"transpositions": [], "missing": {}}, {}, {})
    assert score == 100.0


def test_one_digit_transposition_drops_to_review_and_two_to_fail():
    """A 30% threshold read as 3% is the highest-consequence error class in a
    compliance document — one is a review item, two must not be able to pass."""
    one, _ = _score_integrity({"transpositions": [{"missing": "30%", "extra": "03%"}],
                               "missing": {}}, {}, {})
    two, _ = _score_integrity({"transpositions": [{"missing": "30%", "extra": "03%"},
                                                  {"missing": "150", "extra": "105"}],
                               "missing": {}}, {}, {})
    assert one == 85.0 and GATE_REVIEW <= one < GATE_PASS
    assert two == 70.0


def test_orphan_footnote_references_are_capped_so_one_broken_scheme_cannot_zero_it():
    """A document with a broken numbering scheme would otherwise be driven to the
    floor by that alone, and a score pinned at 0 cannot tell better from worse."""
    few, _ = _score_integrity({"transpositions": [], "missing": {}},
                              {"orphan_footnote_refs": [1, 2, 3, 4]}, {})
    many, _ = _score_integrity({"transpositions": [], "missing": {}},
                               {"orphan_footnote_refs": list(range(40))}, {})
    assert few == 88.0
    assert many == 70.0             # capped at 10 refs -> -30, never worse


def test_a_missing_number_is_reported_but_deliberately_not_scored():
    """Counter-intuitive and load-bearing: measurement showed these are overwhelmingly
    footnote/list/TOC markers, and scoring them pinned Integrity at 0.0. A number lost
    WITH its sentence is content loss and Completeness already measures it."""
    ni = {"transpositions": [],
          "missing": {"25750": {"absent": True, "surface": "25,750", "pages": [12],
                                "pdf_count": 1, "tree_count": 0}}}
    score, detail = _score_integrity(ni, {}, {})
    assert score == 100.0
    assert detail["absent_count"] == 1          # reported, just not charged


# ---------------- uniqueness ----------------
def test_a_tree_with_no_invented_words_is_unique():
    score, _ = _score_uniqueness(wc())
    assert score == 100.0


def test_duplicated_content_shows_up_as_words_the_pdf_never_had():
    """x5 multiplier: a 2% excess costs ~10 points — visible without dominating the
    gate, which is right for a proxy this coarse."""
    score, detail = _score_uniqueness(wc(extra=200, tree_words=10_000))
    assert score == 90.0
    assert detail["excess_rate_pct"] == 2.0


# ---------------- toc ----------------
def test_toc_passes_through_the_quality_checks_verdict():
    assert _score_toc({"score": 100, "status": "proper"})[0] == 100.0
    assert _score_toc({"score": 35, "status": "improper"})[0] == 35.0


def test_toc_is_absent_not_zero_for_a_document_too_short_to_need_one():
    score, detail = _score_toc({"score": None, "detail": "under 8 pages"})
    assert score is None and detail["available"] is False


# ---------------- ai_postprocess ----------------
def test_stage4_is_absent_not_zero_when_the_ai_pass_never_ran():
    """Most documents never run stage 4. Scoring those 0 would fail every one."""
    score, detail = _score_stage4({"skipped": True, "reason": "no 04_* directory"})
    assert score is None and detail["ran"] is False


def test_stage4_reports_the_integrity_verdict_it_was_given():
    s4 = {"score": 0, "integrity_ok": False, "batches_total": 12, "batches_rejected": 1,
          "flags": [{"severity": "critical", "kind": "content_added"}]}
    score, detail = _score_stage4(s4)
    assert score == 0
    assert len(detail["critical_flags"]) == 1


# ------------------------------------------------- naming the pages, not just counting

def test_missing_pages_are_rendered_as_runs():
    """They are overwhelmingly contiguous — a range the extractor skipped, not scattered
    losses — and the run is the thing worth seeing. Germany 179874's six are 63-68."""
    from check_scorecard import _page_runs
    assert _page_runs([63, 64, 65, 66, 67, 68]) == "63–68"
    assert _page_runs([12, 40, 41, 88]) == "12, 40–41, 88"
    assert _page_runs([]) == ""
    assert _page_runs([7]) == "7"


def test_every_absent_page_becomes_its_own_finding():
    """The count drove the completeness score but had no finding, so neither the findings
    list, the page review nor the heat map — all three read the findings list — could say
    which page, or let anyone open one."""
    from check_scorecard import _collect_findings
    out = _collect_findings({}, {}, None, [], pc={"pages_missing": [63, 64]})
    absent = [f for f in out if f["kind"] == "page_absent"]
    assert [f["pages"] for f in absent] == [[63], [64]]
    assert all(f["dimension"] == "completeness" for f in absent)
    # distinct keys, so dismissing one page does not dismiss the other
    assert len({f["key"] for f in absent}) == 2


def test_an_absent_page_and_an_unreadable_page_key_separately():
    """A page can be both; dismissing one must not silently dismiss the other."""
    from lib_dismissals import page_absent_key, unreadable_key
    assert page_absent_key(63) != unreadable_key(63)
