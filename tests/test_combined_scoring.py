"""Combined validator: every preserved Raj-side detector, each proven twice —
the issue is DETECTED, and it has the CORRECT score impact (including none, when the
finding is advisory, dismissed, or a duplicate of one already charged).

Detection and scoring are kept separate on purpose: a detected issue is always listed,
and only an active, non-duplicate, scorable one costs points. See docs/SCORING_RULES.md.
"""
import json
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_scorecard as cs  # noqa: E402
import hybrid_extract as he  # noqa: E402
import lib_dismissals as dis  # noqa: E402
from check_source_fidelity import audit_source  # noqa: E402

HEADER = "<table><tr><td>Questions</td><td>Answers</td></tr>"


# ---------------------------------------------------------------- fixtures

def _pdf(path, lines=("1 SECTION", "Some body text on the page.")):
    """Three pages, `lines` on page 2. One page would not do: the boilerplate stripper
    (shared with the coverage diff) treats a line present on every page as a running
    header, and on a one-page document that is every line."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for pno in (1, 2, 3):
        page = doc.new_page(width=600, height=800)
        body = lines if pno == 2 else (f"Unrelated filler paragraph for page {pno}.",)
        y = 300
        for line in body:
            page.insert_text((40, y), line)
            y += 30
    doc.save(path)
    doc.close()


def _job(tmp_path, *, anomalies=(), n_tables=10, tree=None, pages=10):
    """A job dir with just enough on disk for compute_scorecard: a PDF (for the
    dismissal doc key), a stage-1 report long enough to gate on Fidelity, a stage-2
    report carrying `tables` + `stitch_anomalies`, and a stage-3 tree."""
    _pdf(tmp_path / "source.pdf")
    (tmp_path / "01_stage1_extract").mkdir()
    (tmp_path / "01_stage1_extract" / "stage1_report.json").write_text(
        json.dumps({"pages": pages}))
    (tmp_path / "02_stage2_mineru_tables").mkdir()
    (tmp_path / "02_stage2_mineru_tables" / "stage2_report.json").write_text(json.dumps({
        "tables": [{"table_id": f"table_{i:03d}", "ok": True, "match_status": "confident",
                    "match_method": "iou", "pages": [i + 1]} for i in range(n_tables)],
        "stitch_anomalies": list(anomalies)}))
    t3 = tmp_path / "03_stage3_final"
    t3.mkdir()
    for name, text in (tree or {"01-a.md": "# 1 A\n\nbody\n"}).items():
        (t3 / name).write_text(text)
    return tmp_path


def _validation(findings=()):
    return {"word_coverage": {"coverage_adjusted_pct": 100},
            "table_placement": {"flags": [], "tables_total": 10},
            "source_fidelity": {"findings": list(findings), "coverage": {"source_cells": 500}}}


def _split(block=1, table_id="table_003", pages=(3, 4), action="kept_as_separate_row",
           confidence=None):
    a = {"kind": "TABLE_CONTINUATION_WIDER_ROW", "table_id": table_id, "pages": list(pages),
         "block": block, "action": action, "previous_cells": 2, "new_cells": 3}
    if action == "unflagged_continuation":
        a.update(kind="TABLE_ROW_SPLIT_UNFLAGGED_CONTINUATION",
                 confidence=confidence if confidence is not None else 55,
                 previous_row_tail="and the answer continues onto the following page without",
                 row_text=["(ii) Do the national rules apply here"])
    return a


def _dismiss(job, key):
    dis.dismiss(job, dis.doc_key(cs.resolve_pdf(job, None)), key)


def _fid(job, findings=()):
    return cs.compute_scorecard(job, _validation(findings))["dimensions"]["fidelity"]


# ---------------------------------------------------------------- stitcher DETECTION

def test_unflagged_continuation_is_detected_by_the_stitcher():
    """The new page's first row has its OWN leading text, so the blank-cell check never
    runs — only the previous row stopping mid-sentence gives it away (Liechtenstein
    180334 p71->72)."""
    a = (HEADER + "<tr><td>(i) Is marketing permitted?</td><td>Yes, subject to the rules "
         "set out in the long distance distribution of financial services and the</td></tr>"
         "</table>")
    b = ("<table><tr><td>(ii) Do the national rules apply to it</td>"
         "<td>legislation for financial services applies</td></tr></table>")
    anoms = []
    he.stitch_table_html([a, b], table_meta={"table_id": "table_011", "pages": [71, 72]},
                         anomalies_out=anoms)
    hit = [x for x in anoms if x["kind"] == "TABLE_ROW_SPLIT_UNFLAGGED_CONTINUATION"]
    assert len(hit) == 1
    assert hit[0]["action"] == "unflagged_continuation"
    assert hit[0]["row_text"][0].startswith("(ii)")
    assert "financial services and the" in hit[0]["previous_row_tail"]


def test_unflagged_continuation_ignores_a_short_canned_answer():
    """Noise guard: 'Yes' has no reason to carry a period — not a split."""
    a = HEADER + "<tr><td>(i) Is it permitted?</td><td>Yes</td></tr></table>"
    b = "<table><tr><td>(ii) Do the national rules apply to it</td><td>No</td></tr></table>"
    anoms = []
    he.stitch_table_html([a, b], anomalies_out=anoms)
    assert not [x for x in anoms if x["kind"] == "TABLE_ROW_SPLIT_UNFLAGGED_CONTINUATION"]


def test_a_repeated_header_no_longer_hides_the_continuation_row_behind_it():
    """Liechtenstein 180334 table_011 p88->89: the later block repeats the header at
    ri == 0, so the real continuation (blank leading cell) sat at ri == 1 and was never
    treated as the block's first row — it was appended as an orphan row instead of
    being rejoined."""
    a = HEADER + "<tr><td>(a) Question text</td><td>The answer starts here and</td></tr></table>"
    b = HEADER + "<tr><td></td><td>continues on the next page.</td></tr></table>"
    html, rows, _cols = he.stitch_table_html([a, b])
    assert rows == 2, "header once + the one rejoined row"
    assert any("The answer starts here and continues on the next page." in c
               for c in __import__("re").findall(r"<td[^>]*>(.*?)</td>", html, 16))


def test_a_kept_separate_row_is_detected_and_carries_its_own_text():
    """row_text is what the scorecard finding quotes — present even when the surplus
    column is at the leading edge and surplus_text is empty."""
    a = HEADER + "<tr><td>Ref</td><td>Short Label</td></tr></table>"
    b = "<table><tr><td></td><td>New Heading Here</td><td>Another Column Value</td></tr></table>"
    anoms = []
    he.stitch_table_html([a, b], anomalies_out=anoms)
    assert anoms[0]["action"] == "kept_as_separate_row"
    assert anoms[0]["row_text"] == ["New Heading Here", "Another Column Value"]


# ---------------------------------------------------------------- row-split SCORING

def test_a_confirmed_row_split_costs_one_point(tmp_path):
    job = _job(tmp_path, anomalies=[_split()])
    fid = _fid(job)
    assert fid["score"] == 99.0
    assert fid["detail"]["rows_split_across_page_break"] == 1
    sc = cs.compute_scorecard(job, _validation())
    assert any(f["kind"] == "row_split" for f in sc["findings"])


def test_row_split_cost_does_not_depend_on_how_many_tables_the_document_has(tmp_path):
    """The size dilution this replaced: 0.15 table-credit cost ~5 pts at 3 tables and
    ~0.4 at 35 for the same broken row."""
    few = _fid(_job(tmp_path / "few", anomalies=[_split()], n_tables=3))
    many = _fid(_job(tmp_path / "many", anomalies=[_split()], n_tables=35))
    assert few["score"] == many["score"] == 99.0


@pytest.mark.parametrize("confidence,points", [(100, 0.5), (60, 0.3), (0, 0.1), (None, 0.25)])
def test_an_unconfirmed_split_costs_less_scaled_by_confidence(tmp_path, confidence, points):
    a = _split(action="unflagged_continuation")
    a["confidence"] = confidence
    fid = _fid(_job(tmp_path, anomalies=[a]))
    assert fid["detail"]["row_split_points"] == points
    assert fid["score"] == round(100.0 - points, 1)


def test_row_splits_are_proportional_to_count_and_capped(tmp_path):
    three = _fid(_job(tmp_path / "3", anomalies=[_split(block=b, pages=(1, 2, 3, 4))
                                                   for b in (1, 2, 3)]))
    assert three["score"] == 97.0
    many = _fid(_job(tmp_path / "30", anomalies=[_split(block=b, pages=range(1, 32))
                                                   for b in range(1, 31)]))
    assert many["detail"]["row_split_points"] == cs.ROW_SPLIT_CAP
    assert many["score"] == 100.0 - cs.ROW_SPLIT_CAP


def test_resolved_continuations_are_detected_but_free(tmp_path):
    a = dict(_split(), action="realigned_and_merged")
    job = _job(tmp_path, anomalies=[a])
    sc = cs.compute_scorecard(job, _validation())
    assert any(f["kind"] == "row_continuation_merged" for f in sc["findings"])
    assert sc["dimensions"]["fidelity"]["score"] == 100.0


# ---------------------------------------------------------------- DEDUPLICATION

def test_the_same_row_reported_twice_is_charged_once(tmp_path):
    fid = _fid(_job(tmp_path, anomalies=[_split(), _split()]))
    assert fid["detail"]["rows_split_across_page_break"] == 1
    assert fid["score"] == 99.0


def test_two_heuristics_on_one_row_charge_the_more_severe_reading_once(tmp_path):
    fid = _fid(_job(tmp_path, anomalies=[_split(action="unflagged_continuation",
                                                confidence=100), _split()]))
    assert fid["detail"]["row_split_points"] == 1.0
    assert fid["score"] == 99.0


def _ragged(file="05-x.md", pages=(3, 6), key="k-ragged"):
    return {"key": key, "kind": "inconsistent_table_columns", "file": file,
            "pages": list(pages), "severity": "silent", "evidence": {"output_table": 0}}


BADGED = {"05-x.md": "# 5 X\n\n**⚙ MinerU-extracted table** — table_003, pages 3–6, "
                     "9 rows × 2 cols\n\n<table><tr><td>a</td></tr></table>\n"}


def test_a_ragged_table_explained_by_its_row_split_is_not_billed_twice(tmp_path):
    """All 27 unresolved row splits in this corpus sat inside a table also flagged
    inconsistent_table_columns — the split's own symptom."""
    job = _job(tmp_path, anomalies=[_split()], tree=BADGED)
    sc = cs.compute_scorecard(job, _validation([_ragged()]))
    assert sc["dimensions"]["fidelity"]["score"] == 99.0          # not 98.0
    ragged = next(f for f in sc["findings"] if f["kind"] == "inconsistent_table_columns")
    assert ragged["charged"] is False and ragged["absorbed_by"] == "row_split"
    assert sc["dimensions"]["fidelity"]["detail"]["shape_findings_absorbed_by_row_split"] == 1


def test_a_ragged_table_in_a_different_table_is_still_charged(tmp_path):
    job = _job(tmp_path, anomalies=[_split()], tree=BADGED)
    sc = cs.compute_scorecard(job, _validation([_ragged(file="09-other.md", pages=(20, 25))]))
    assert sc["dimensions"]["fidelity"]["score"] == 98.0          # split 1.0 + ragged 1.0


def test_a_shape_finding_reported_twice_under_one_key_is_charged_once(tmp_path):
    job = _job(tmp_path)
    cell_split = {"key": "same", "kind": "cell_split", "file": "a.md", "pages": [2],
                  "evidence": {}}
    assert _fid(job, [cell_split, dict(cell_split)])["score"] == 99.0


def test_all_structural_terms_share_one_fidelity_cap(tmp_path):
    shapes = [{"key": f"s{i}", "kind": "cell_split", "file": "a.md", "pages": [2],
               "evidence": {}} for i in range(12)]
    job = _job(tmp_path, anomalies=[_split(block=b, pages=range(1, 32)) for b in range(1, 13)])
    fid = _fid(job, shapes)
    assert fid["detail"]["structure_points"] == cs.FIDELITY_STRUCTURE_CAP
    assert fid["score"] == 100.0 - cs.FIDELITY_STRUCTURE_CAP


# ---------------------------------------------------------------- DISMISSALS

def test_a_dismissed_row_split_is_still_listed_but_costs_nothing(tmp_path):
    job = _job(tmp_path, anomalies=[_split()])
    _dismiss(job, cs._stitch_key(_split()))
    sc = cs.compute_scorecard(job, _validation())
    f = next(f for f in sc["findings"] if f["kind"] == "row_split")
    assert f["dismissed"] is True
    assert sc["dimensions"]["fidelity"]["score"] == 100.0


def test_only_the_active_row_splits_cost_points_when_some_are_dismissed(tmp_path):
    splits = [_split(block=b, pages=(1, 2, 3, 4)) for b in (1, 2, 3)]
    job = _job(tmp_path, anomalies=splits)
    _dismiss(job, cs._stitch_key(splits[0]))
    fid = _fid(job)
    assert fid["detail"]["rows_split_across_page_break"] == 2
    assert fid["score"] == 98.0


def test_a_dismissed_unconfirmed_split_costs_nothing(tmp_path):
    a = _split(action="unflagged_continuation", confidence=100)
    job = _job(tmp_path, anomalies=[a])
    _dismiss(job, cs._stitch_key(a))
    assert _fid(job)["score"] == 100.0


# ---------------------------------------------------------------- EMPTY ANSWER SEGMENTS

EMPTY_CELL = ("# 7 PRIVATE PLACEMENT\n\n*Source: `source.pdf`, page 2–3*\n\n"
              "<table><tr><td>7.1(i) sub-items (i)-(iii)</td><td>"
              "N/a. Please see 7.1(a) above.<br><br>N/a. Please see 7.1(a) above."
              "<br><br><br><br>N/a. Please see 7.1(a) above.</td></tr></table>\n")
FULL_CELL = EMPTY_CELL.replace("<br><br><br><br>", "<br><br>N/a. Please see 7.1(a) above.<br><br>")


def test_an_empty_answer_segment_is_detected_and_costs_completeness(tmp_path):
    """Cayman Islands 183333 p46: a blank segment between repeated boilerplate that
    every word-coverage check reads as complete."""
    job = _job(tmp_path, tree={"08-private.md": EMPTY_CELL})
    sc = cs.compute_scorecard(job, _validation())
    hits = [f for f in sc["findings"] if f["kind"] == "empty_answer_segment"]
    assert len(hits) == 1 and hits[0]["pages"], "must land on Page Review, not pages: []"
    assert sc["dimensions"]["completeness"]["score"] == 100.0 - cs.EMPTY_ANSWER_SEGMENT_PENALTY


def test_a_complete_multi_part_cell_is_not_an_empty_segment(tmp_path):
    job = _job(tmp_path, tree={"08-private.md": FULL_CELL})
    sc = cs.compute_scorecard(job, _validation())
    assert not [f for f in sc["findings"] if f["kind"] == "empty_answer_segment"]
    assert sc["dimensions"]["completeness"]["score"] == 100.0


def test_a_dismissed_empty_segment_is_listed_but_costs_nothing(tmp_path):
    job = _job(tmp_path, tree={"08-private.md": EMPTY_CELL})
    key = next(f["key"] for f in cs.compute_scorecard(job, _validation())["findings"]
               if f["kind"] == "empty_answer_segment")
    _dismiss(job, key)
    sc = cs.compute_scorecard(job, _validation())
    f = next(f for f in sc["findings"] if f["kind"] == "empty_answer_segment")
    assert f["dismissed"] is True
    assert sc["dimensions"]["completeness"]["score"] == 100.0


def test_only_active_empty_segments_cost_points_when_some_are_dismissed(tmp_path):
    other = EMPTY_CELL.replace("7.1(a)", "8.2(b)").replace("7 PRIVATE", "8 OTHER")
    job = _job(tmp_path, tree={"08-private.md": EMPTY_CELL, "09-other.md": other})
    keys = [f["key"] for f in cs.compute_scorecard(job, _validation())["findings"]
            if f["kind"] == "empty_answer_segment"]
    assert len(keys) == 2
    _dismiss(job, keys[0])
    sc = cs.compute_scorecard(job, _validation())
    assert sc["dimensions"]["completeness"]["score"] == 100.0 - cs.EMPTY_ANSWER_SEGMENT_PENALTY


# ---------------------------------------------------------------- extraction_annotation

def test_an_extractor_badge_is_detected_as_advisory_and_costs_nothing(tmp_path):
    pdf = tmp_path / "source.pdf"
    _pdf(pdf, lines=("1 SECTION",) + tuple(f"line {i} of body text" for i in range(8)))
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "01-s.md").write_text("# 1 SECTION\n\n*Source: `source.pdf`, page 2*\n\n"
                                  "**⚙ MinerU-extracted table** — table_001, pages 1–1\n\n"
                                  + "\n".join(f"line {i} of body text" for i in range(8)))
    report = audit_source(pdf, tree)
    badge = [f for f in report["findings"] if f["kind"] == "extraction_annotation"]
    assert badge and badge[0]["evidence"]["confidence"] == "advisory"
    job = _job(tmp_path / "job")
    sc = cs.compute_scorecard(job, _validation(badge))
    assert sc["gate"] == "pass" and sc["source_fidelity"]["review_count"] == 0
    assert all(d["score"] in (None, 100.0) for k, d in sc["dimensions"].items()
               if k in cs.CRITICAL_DIMENSIONS)


# ---------------------------------------------------------------- readable evidence

def test_a_gap_title_is_rewritten_as_a_readable_quote_from_the_pdf(tmp_path):
    """Token joins ("article 6 4") are right for the diff and unreadable as a quote —
    the title is reconstructed from the page's own text, punctuation intact."""
    job = tmp_path
    _pdf(job / "source.pdf", lines=("Pursuant to Article 6(4), the OFI must register.",))
    f = {"kind": "gap", "pages": [2], "title": "pursuant to article 6 4 the ofi must register"}
    cs._READABLE_DOC_CACHE.clear()
    cs._READABLE_PAGE_CACHE.clear()
    cs._attach_readable_titles([f], job)
    assert f["title"] == "Pursuant to Article 6(4), the OFI must register"


def test_an_unrelocatable_gap_title_is_left_as_it_was(tmp_path):
    _pdf(tmp_path / "source.pdf", lines=("Nothing that matches.",))
    f = {"kind": "gap", "pages": [2], "title": "words that are not on the page"}
    cs._READABLE_DOC_CACHE.clear()
    cs._READABLE_PAGE_CACHE.clear()
    cs._attach_readable_titles([f], tmp_path)
    assert f["title"] == "words that are not on the page"


# ---------------------------------------------------------------- gate exception

def test_a_source_fidelity_crash_downgrades_a_numeric_pass_to_review(tmp_path):
    job = _job(tmp_path)
    v = _validation()
    v["source_fidelity"] = {"error": "PDF could not be opened", "findings": []}
    sc = cs.compute_scorecard(job, v)
    assert sc["worst_score"] >= cs.GATE_PASS and sc["gate"] == "review"


def test_an_ordinary_finding_does_not_override_a_numeric_pass(tmp_path):
    job = _job(tmp_path)
    one = {"key": "x", "kind": "cell_split", "file": "a.md", "pages": [2], "evidence": {}}
    sc = cs.compute_scorecard(job, _validation([one]))
    assert sc["worst_score"] == 99.0 and sc["gate"] == "pass"
