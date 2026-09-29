"""Stitching a multi-page table's pieces back together without losing cells.

MinerU emits one <table> per page-crop, so a row cut across a page break resumes in
the next block with a blank first cell. stitch_table_html rejoins those. The case that
matters is a continuation WIDER than the row it joins: that used to copy
min(len(cells), len(prev)) cells and drop the row carrying the surplus, which deleted a
216-word answer on Bermuda__166524 p41 that MinerU had extracted correctly.

The invariant asserted throughout: no text present in the input is absent from the
output. Whether the row is widened or kept separate is a quality question; losing it is
a correctness one.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hybrid_extract as he  # noqa: E402


def _text(html):
    return " ".join(re.findall(r">([^<>]+)<", html or ""))


def _cells(html):
    return re.findall(r"<td[^>]*>(.*?)</td>", html or "", re.S)


HEADER = "<table><tr><td>Questions</td><td>Answers</td></tr>"

# The real shape from Bermuda__166524 p41: a 2-cell row whose last cell ends
# mid-sentence, continued by a 3-cell row whose third cell holds the answer.
WIDE_A = HEADER + "<tr><td>(d)(i) Is it permitted to use a local intermediary in a</td><td>Carrying on Business Restriction</td></tr></table>"
WIDE_B = "<table><tr><td></td><td>Fund to an investor and on what basis?</td><td>There is no legal requirement that an OFI use one.</td></tr></table>"


def test_the_regression_case_keeps_the_surplus_cell():
    html, _rows, cols = he.stitch_table_html([WIDE_A, WIDE_B])
    assert "There is no legal requirement that an OFI use one." in _text(html)
    # Losslessness is not enough: the surplus column here is at the LEADING edge
    # (MinerU fused the row-ref into the question on the narrow fragment), so the two
    # fragments are OFFSET. Merging by raw index puts the QUESTION continuation into
    # the ANSWER cell and parks the answer in a phantom third column. Each piece must
    # land in its own column instead, and the row must not grow.
    cells = _cells(html)
    question = next(c for c in cells if c.startswith("(d)(i)"))
    answer = next(c for c in cells if "Carrying on Business Restriction" in c)
    assert "Fund to an investor and on what basis?" in question, \
        "the question continuation belongs to the question column"
    assert "Fund to an investor" not in answer, \
        "the question continuation must not be fused onto the answer"
    assert "There is no legal requirement that an OFI use one." in answer
    assert cols == 2, "no phantom column: prev already had a column for every piece"


def test_the_wider_continuation_is_recorded():
    anoms = []
    he.stitch_table_html([WIDE_A, WIDE_B], table_meta={"table_id": "table_006",
                                                       "pages": [40, 41]},
                         anomalies_out=anoms)
    assert len(anoms) == 1
    a = anoms[0]
    assert a["kind"] == "TABLE_CONTINUATION_WIDER_ROW"
    assert (a["previous_cells"], a["new_cells"]) == (2, 3)
    assert a["table_id"] == "table_006" and a["pages"] == [40, 41]
    assert a["action"] == "realigned_and_merged"
    assert a["alignment_offset"] == 1, "the surplus column is the blank leading ref band"
    assert a["confidence"] >= he.CONTINUATION_MERGE_MIN


def test_a_genuinely_trailing_surplus_column_is_widened_not_realigned():
    """The other direction: when the extra column really is on the RIGHT, the previous
    row must be widened to take it — offset 0, exactly the pre-existing behaviour."""
    a = HEADER + "<tr><td>Label</td><td>a clause that continues on the</td></tr></table>"
    b = "<table><tr><td></td><td>next page here</td><td>brand new third column</td></tr></table>"
    anoms = []
    html, _rows, cols = he.stitch_table_html([a, b], anomalies_out=anoms)
    assert anoms and anoms[0]["action"] == "widened_and_merged"
    assert anoms[0]["alignment_offset"] == 0
    assert "a clause that continues on the next page here" in _cells(html)
    assert "brand new third column" in _text(html)
    assert cols == 3, "a real trailing column does widen the row"


def test_an_unresolvable_alignment_is_kept_whole_rather_than_guessed():
    """The rows belong together, but no alignment reads as one continued clause.
    Guessing is what fuses a question onto an answer, and that corruption is invisible
    downstream — so the row is kept separate, which a reader can still recover."""
    a = HEADER + "<tr><td>Ref</td><td>Short Label</td></tr></table>"
    b = "<table><tr><td></td><td>New Heading Here</td><td>Another Column Value</td></tr></table>"
    anoms = []
    html, _rows, _cols = he.stitch_table_html([a, b], anomalies_out=anoms)
    assert anoms and anoms[0]["confidence"] >= he.CONTINUATION_MERGE_MIN, \
        "this is NOT the low-confidence path — the rows do look joined"
    assert anoms[0]["action"] == "kept_as_separate_row"
    assert anoms[0]["reason"] == "no column alignment reads as a continuation"
    assert "Another Column Value" in _text(html), "nothing may be discarded"
    assert "Short Label New Heading Here" not in _cells(html), "no invented fusion"


def test_a_label_cell_is_not_mistaken_for_a_truncated_clause():
    """_ends_mid_sentence alone drove the old decision, and a label carries no terminal
    punctuation either — which is how a header got pulled onto a question."""
    assert not he._looks_truncated("Carrying on Business Restriction")
    assert not he._looks_truncated("Answer one complete.")
    assert he._looks_truncated("permitted to use a local intermediary in a"), \
        "a stop on a function word cannot end a clause"
    assert he._looks_truncated(
        "there is no specific mandatory requirement however in connection with the"), \
        "enough words to be prose, and no terminal punctuation"


def test_mid_sentence_ending_is_what_carries_the_decision():
    # Same widths, same blank first cell — only the previous row's ending differs.
    prev = [{"text": "…interests in a", "rowspan": 1, "colspan": 1}]
    cells = [{"text": "", "rowspan": 1, "colspan": 1},
             {"text": "Fund to an investor", "rowspan": 1, "colspan": 1}]
    open_score, _ = he._continuation_confidence(prev, cells)
    closed_score, _ = he._continuation_confidence(
        [{"text": "…interests in a Fund.", "rowspan": 1, "colspan": 1}], cells)
    assert open_score > closed_score
    assert open_score >= he.CONTINUATION_MERGE_MIN


def test_a_low_confidence_wider_row_is_kept_not_dropped():
    # Previous row ends in a full stop and the continuation opens upper-case: this
    # looks like a NEW table, so it must not be merged — but it must still survive.
    a = HEADER + "<tr><td>Question one.</td><td>Answer one.</td></tr></table>"
    b = "<table><tr><td></td><td>Totally New Heading</td><td>Third Column Value</td></tr></table>"
    anoms = []
    html, _rows, _cols = he.stitch_table_html([a, b], anomalies_out=anoms)
    assert "Third Column Value" in _text(html), "nothing may be discarded"
    assert anoms and anoms[0]["action"] == "kept_as_separate_row"
    assert anoms[0]["confidence"] < he.CONTINUATION_MERGE_MIN


def test_equal_width_continuation_is_unchanged_and_silent():
    """The regression guard: same-width joins must behave exactly as before."""
    a = HEADER + "<tr><td>(a)</td><td>a value split across the</td></tr></table>"
    b = "<table><tr><td></td><td>page boundary</td></tr></table>"
    anoms = []
    html, rows, cols = he.stitch_table_html([a, b], anomalies_out=anoms)
    assert anoms == [], "a same-width join is not an anomaly"
    assert (rows, cols) == (2, 2)
    assert "a value split across the page boundary" in _text(html)


def test_a_narrower_continuation_still_merges():
    a = HEADER + "<tr><td>(b)</td><td>text that runs</td><td>and a third cell</td></tr></table>"
    b = "<table><tr><td></td><td>on past the break</td></tr></table>"
    html, _rows, cols = he.stitch_table_html([a, b])
    assert "text that runs on past the break" in _text(html)
    assert cols == 3 and "and a third cell" in _text(html)


def test_blank_first_cell_mid_block_is_still_not_merged():
    """MinerU's rowspan structure, not a page break. Merging it corrupted correct
    single-page tables, which is why the trigger is first-row-of-a-later-block only."""
    one = (HEADER + "<tr><td>Label</td><td>first</td></tr>"
           "<tr><td></td><td>second</td></tr></table>")
    html, rows, _cols = he.stitch_table_html([one])
    assert rows == 3, "the mid-block blank-first-cell row stays its own row"
    # assert on CELLS, not on joined text: joining every cell with spaces would make
    # "first second" appear even when the two live in separate rows.
    assert "first second" not in _cells(html)


def test_repeated_header_on_a_later_block_is_still_dropped():
    a = HEADER + "<tr><td>Q1</td><td>A1</td></tr></table>"
    b = HEADER + "<tr><td>Q2</td><td>A2</td></tr></table>"
    html, rows, _cols = he.stitch_table_html([a, b])
    assert rows == 3, "header appears once"
    assert _text(html).count("Questions") == 1


def test_no_input_text_is_ever_absent_from_the_output():
    for blocks in ([WIDE_A, WIDE_B],
                   [HEADER + "<tr><td>x.</td><td>y.</td></tr></table>",
                    "<table><tr><td></td><td>P</td><td>Q</td><td>R</td></tr></table>"]):
        html, _r, _c = he.stitch_table_html(blocks)
        out = _text(html)
        for block in blocks:
            for cell in _cells(block):
                cell = cell.strip()
                if cell:
                    assert cell in out, f"{cell!r} vanished"
