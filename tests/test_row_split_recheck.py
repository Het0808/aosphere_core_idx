"""A page-break row split is charged only while the tree being scored still shows it.

Stage 2's stitcher records every page-break row it did not rejoin, and that record is
frozen before anything downstream runs. Two things make it wrong on the tree actually
scored, and _recheck_row_splits disproves both against the tree and the PDF:

  * a later stage rejoined the row (Netherlands__156999 table_003 p36: after Stage 4 the
    page-36 tail reads on inside the 4.6 answer, whose question is on p35);
  * it was never a continuation: the row opening page N is a complete labelled item in
    the PDF (Mexico__183466 table_004 p19, "(viii) Are there restrictions ...") and the
    stitcher read the table's empty label column as a continuation's blank first cell.

Anything it cannot locate unambiguously stays a split.
"""
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_scorecard import (HEADER_ROW_ACTION, OWN_ROW_ACTION, REJOINED_ACTION,  # noqa: E402
                             _recheck_row_splits, _score_fidelity)

Q1 = "(a) Please outline the position as regards a Small AIFM wishing to market an AIF into your jurisdiction"
A1_HEAD = "It follows from article 2:66a subsection 8 Wft that it is possible for Dutch and non-Dutch small managers to operate"
A1_TAIL = "This health warnings can be found at the website of the regulator and can be downloaded in a number of sizes"
Q2 = "(viii) Are there restrictions regarding future communications with investors known to the OFI on that basis"
A2 = "As noted above all communications from the OFI pursuant to the conditions must be made from outside the country"


def _ruled_row(page, y, cells, x0=40, widths=(260, 260)):
    xs = [x0]
    for w in widths:
        xs.append(xs[-1] + w)
    for x in xs:
        page.draw_line((x, y), (x, y + 120))
    for yy in (y, y + 120):
        page.draw_line((xs[0], yy), (xs[-1], yy))
    for i, text in enumerate(cells):
        if text:
            page.insert_textbox((xs[i] + 3, y + 3, xs[i + 1] - 3, y + 117), text, fontsize=9)


def _pdf(path, page2_cells):
    doc = fitz.open()
    p1 = doc.new_page(width=600, height=800)
    _ruled_row(p1, 80, [Q1, A1_HEAD])
    p2 = doc.new_page(width=600, height=800)
    _ruled_row(p2, 80, page2_cells)
    doc.save(path)
    doc.close()


def _tree(root, stage_dir, rows):
    d = root / stage_dir
    d.mkdir(parents=True, exist_ok=True)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    (d / "05-section.md").write_text("# 4.6 Small AIFMs\n\n*Source: `source.pdf`, page 1–2*\n\n"
                                     f"<table>{body}</table>\n")


def _anomaly(text=A1_TAIL, action="kept_as_separate_row"):
    return {"kind": "TABLE_CONTINUATION_WIDER_ROW", "table_id": "table_003", "pages": [1, 2],
            "block": 1, "previous_cells": 2, "new_cells": 2, "confidence": 40,
            "surplus_text": [text] if text else [], "action": action}


@pytest.fixture
def continuation_job(tmp_path):
    """Page 2 opens with a genuine continuation: an empty question cell and the tail."""
    _pdf(tmp_path / "source.pdf", ["", A1_TAIL])
    # Stage 3: the split as the stitcher left it -- the tail is an orphan row.
    _tree(tmp_path, "03_stage3_final", [[Q1, A1_HEAD], ["", A1_TAIL]])
    return tmp_path


def test_a_split_still_in_the_tree_is_still_charged(continuation_job):
    out = _recheck_row_splits([_anomaly()], continuation_job, 3)
    assert out[0]["action"] == "kept_as_separate_row"


def test_a_split_a_later_stage_rejoined_is_not_charged(continuation_job):
    # Stage 5: the AI pass put the tail back inside the answer it continues.
    _tree(continuation_job, "05_subchunks", [[Q1, A1_HEAD + " " + A1_TAIL]])
    out = _recheck_row_splits([_anomaly()], continuation_job, 5)
    assert out[0]["action"] == REJOINED_ACTION
    assert out[0]["stage2_action"] == "kept_as_separate_row"


def test_a_later_stage_that_left_the_orphan_row_is_still_charged(continuation_job):
    _tree(continuation_job, "05_subchunks", [[Q1, A1_HEAD], ["", A1_TAIL]])
    assert _recheck_row_splits([_anomaly()], continuation_job, 5)[0]["action"] == \
        "kept_as_separate_row"


def test_a_row_that_is_complete_in_the_source_is_not_a_split(tmp_path):
    # Page 2 opens with its OWN labelled question -- nothing continues from page 1.
    _pdf(tmp_path / "source.pdf", [Q2, A2])
    _tree(tmp_path, "03_stage3_final", [[Q1, A1_HEAD], [Q2, A2]])
    out = _recheck_row_splits([_anomaly(text=A2)], tmp_path, 3)
    assert out[0]["action"] == OWN_ROW_ACTION


def test_a_complete_source_row_the_tree_broke_apart_is_still_charged(tmp_path):
    _pdf(tmp_path / "source.pdf", [Q2, A2])
    _tree(tmp_path, "03_stage3_final", [[Q1, A1_HEAD], [Q2, ""], ["", A2]])
    assert _recheck_row_splits([_anomaly(text=A2)], tmp_path, 3)[0]["action"] == \
        "kept_as_separate_row"


def test_page_n_text_ahead_of_the_tail_is_not_evidence_of_a_rejoin(tmp_path):
    # The orphan row holds page-2 text of its own ahead of the tail (the Netherlands
    # warning-sign cell). Only page-1 text -- where the question lives -- proves a rejoin,
    # and an unlabelled fragment is not a question of its own.
    lead = "Let op this sign must be shown on every document in both languages as set out"
    _pdf(tmp_path / "source.pdf", [lead, A1_TAIL])
    _tree(tmp_path, "03_stage3_final", [[Q1, A1_HEAD], [lead, A1_TAIL]])
    assert _recheck_row_splits([_anomaly()], tmp_path, 3)[0]["action"] == \
        "kept_as_separate_row"


def test_an_ambiguous_continuation_with_an_orphan_holder_stays_charged(continuation_job):
    # The tail text occurs in two rows and one of them is still the orphan the split
    # left (empty question): which row is its home cannot be told, and a defect exists.
    _tree(continuation_job, "05_subchunks",
          [[Q1, A1_HEAD + " " + A1_TAIL], ["", A1_TAIL]])
    assert _recheck_row_splits([_anomaly()], continuation_job, 5)[0]["action"] == \
        "kept_as_separate_row"


def test_a_record_with_no_row_text_is_read_from_the_pdf(continuation_job):
    # Stage 2 often records no text for the row; the row it means is the top ruled row
    # of page N, so that is read from the PDF instead of giving up.
    _tree(continuation_job, "05_subchunks", [[Q1, A1_HEAD + " " + A1_TAIL]])
    assert _recheck_row_splits([_anomaly(text=None)], continuation_job, 5)[0]["action"] == \
        REJOINED_ACTION


def test_no_row_text_and_no_ruled_row_on_the_page_stays_charged(tmp_path):
    doc = fitz.open()
    _ruled_row(doc.new_page(width=600, height=800), 80, [Q1, A1_HEAD])
    doc.new_page(width=600, height=800).insert_text((40, 90), A1_TAIL, fontsize=9)
    doc.save(tmp_path / "source.pdf")
    doc.close()
    _tree(tmp_path, "05_subchunks", [[Q1, A1_HEAD + " " + A1_TAIL]])
    assert _recheck_row_splits([_anomaly(text=None)], tmp_path, 5)[0]["action"] == \
        "kept_as_separate_row"


def test_answers_that_open_identically_are_told_apart_by_reading_further(continuation_job):
    # Mexico__183466 table_005 p27: the same opening in several rows. Only one row holds
    # the continuation as a whole.
    tail = A1_TAIL + " and this is the part that only the right row carries"
    _pdf(continuation_job / "source.pdf", ["", tail])
    _tree(continuation_job, "05_subchunks",
          [[Q1, A1_HEAD + " " + tail],
           ["(b) A different question in a later row of the table", A1_TAIL + " but then it differs"]])
    assert _recheck_row_splits([_anomaly(text=tail)], continuation_job, 5)[0]["action"] == \
        REJOINED_ACTION


def test_only_the_splits_still_present_cost_fidelity(continuation_job):
    _tree(continuation_job, "05_subchunks", [[Q1, A1_HEAD + " " + A1_TAIL]])
    table = {"table_id": "table_003", "ok": True, "match_status": "confident",
             "match_method": "iou", "pages": [1, 2]}
    before, d_before = _score_fidelity([table], None, None, [_anomaly()], 5)
    after, d_after = _score_fidelity([table], None, None,
                                     _recheck_row_splits([_anomaly()], continuation_job, 5), 5)
    assert d_before["rows_split_across_page_break"] == 1
    assert d_after["rows_split_across_page_break"] == 0
    assert after > before


def test_a_passage_the_document_repeats_elsewhere_is_disambiguated_by_page(continuation_job):
    # Spain__176285 p64: the continuation text is printed again in an earlier section.
    # Only the chunk whose page range covers the continuation's page can hold it.
    _tree(continuation_job, "05_subchunks", [[Q1, A1_HEAD + " " + A1_TAIL]])
    other = continuation_job / "05_subchunks" / "01-earlier.md"
    other.write_text("# 1 Earlier\n\n*Source: `source.pdf`, page 1*\n\n"
                     f"<table><tr><td>Earlier question text for the section</td><td>{A1_TAIL}</td></tr></table>\n")
    (continuation_job / "05_subchunks" / "05-section.md").write_text(
        "# 4.6 Small AIFMs\n\n*Source: `source.pdf`, page 2*\n\n"
        f"<table><tr><td>{Q1}</td><td>{A1_HEAD} {A1_TAIL}</td></tr></table>\n")
    assert _recheck_row_splits([_anomaly()], continuation_job, 5)[0]["action"] == REJOINED_ACTION


def test_a_repeated_column_header_is_not_a_split(tmp_path):
    # Liechtenstein__180334 table_008 p61/p62: the header is printed atop every page.
    header = ["Yes/No answer column heading", "If the answer is yes please include a brief description"]
    doc = fitz.open()
    for _ in range(3):
        _ruled_row(doc.new_page(width=600, height=800), 80, header)
    doc.save(tmp_path / "source.pdf")
    doc.close()
    _tree(tmp_path, "03_stage3_final", [header, header, header])
    a = {**_anomaly(text=header[1]), "pages": [1, 2, 3]}
    assert _recheck_row_splits([a], tmp_path, 3)[0]["action"] == HEADER_ROW_ACTION


def test_repeated_text_with_every_holder_carrying_its_own_question_is_no_split(continuation_job):
    # "Please see (d) above" answers several labelled questions; no orphan row exists.
    _pdf(continuation_job / "source.pdf", ["", "Please see section d above for the answer"])
    _tree(continuation_job, "05_subchunks",
          [["(a) a licensed third party intermediary", "Please see section d above for the answer"],
           ["(b) an unlicensed local third party intermediary", "Please see section d above for the answer"]])
    a = _anomaly(text="Please see section d above for the answer")
    assert _recheck_row_splits([a], continuation_job, 5)[0]["action"] == REJOINED_ACTION


def test_repeated_text_with_an_orphan_holder_stays_charged(continuation_job):
    ans = "Please see section d above for the answer"
    _pdf(continuation_job / "source.pdf", ["", ans])
    _tree(continuation_job, "05_subchunks",
          [["(a) a licensed third party intermediary", ans], ["", ans]])
    assert _recheck_row_splits([_anomaly(text=ans)], continuation_job, 5)[0]["action"] == \
        "kept_as_separate_row"


def test_a_stranded_question_tail_is_still_an_orphan(continuation_job):
    # A split can leave the question's TAIL ahead of the answer's tail. Unlabelled and
    # printed only on page N, it is not a question of the row's own.
    ans = "Please see section d above for the answer"
    tail = "telephone call email without itself being licensed locally"
    _pdf(continuation_job / "source.pdf", [tail, ans])
    _tree(continuation_job, "05_subchunks",
          [["(a) a licensed third party intermediary", ans], [tail, ans]])
    assert _recheck_row_splits([_anomaly(text=ans)], continuation_job, 5)[0]["action"] == \
        "kept_as_separate_row"


def test_a_question_tail_repeated_earlier_in_the_document_is_still_an_orphan(tmp_path):
    # British Virgin Islands__175133 p41: the stranded tail is a templated sentence the
    # document also prints pages earlier, in another section. Only page N-1 counts.
    ans = "Please see section d above for the answer"
    tail = "for example can the sub advisor participate in the marketing activities"
    doc = fitz.open()
    _ruled_row(doc.new_page(width=600, height=800), 80, [tail + " elsewhere", "Other answer text here"])
    _ruled_row(doc.new_page(width=600, height=800), 80, [Q1, A1_HEAD])
    _ruled_row(doc.new_page(width=600, height=800), 80, [tail, ans])
    doc.save(tmp_path / "source.pdf")
    doc.close()
    _tree(tmp_path, "05_subchunks",
          [["(a) a licensed third party intermediary", ans], [tail, ans]])
    a = {**_anomaly(text=ans), "pages": [2, 3]}
    assert _recheck_row_splits([a], tmp_path, 5)[0]["action"] == "kept_as_separate_row"


def test_a_too_short_recorded_row_is_looked_up_by_the_pdf_row(continuation_job):
    # Panama__183139 p46: Stage 2 recorded only "IMAS: Yes."; the PDF's top row also
    # carries the question tail, long enough to find the row by.
    q_tail = "Such presentations may include investment strategy and track record"
    _pdf(continuation_job / "source.pdf", [q_tail, "IMAS Yes"])
    _tree(continuation_job, "05_subchunks", [[Q1 + " " + q_tail, A1_HEAD + " IMAS Yes"]])
    assert _recheck_row_splits([_anomaly(text="IMAS: Yes.")], continuation_job, 5)[0]["action"] == \
        REJOINED_ACTION
