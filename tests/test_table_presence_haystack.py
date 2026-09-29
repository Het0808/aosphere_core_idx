"""A table deleted from the tree must be reported — even when its words survive as prose.

MEASURED, on 124_Marketing_Restrictions/Bahamas-sonnet-nocounts-08-09-0524: deleting
table_001's entire <table> block from 01-front-matter.md leaves the check reporting
nothing. 6 of its 9 substantive cells are still "found" because the cover page repeats
"Reporting Counsel for Part B:", "Date of Report:" and the responsibility paragraph as
flowed prose, so the ratio holds at 0.67 — above PRESENT_THRESHOLD — while the address
and contact rows are gone from the document entirely.

The cause was the haystack, not the threshold. A character-weighted ratio was tried
first and does NOT fix it (0.701, still passing): the surviving cells include the long
paragraph, so length does not separate them. The check's claim is that the extracted
ROWS reached the output, so the rows the tree renders are what it must be measured
against.

Stage matters, and that is the second half of this file. Up to stage 3 nothing may
restructure a table. From stage 4 the AI pass may lift a label row into a heading and
stage 5 splits a questionnaire table across sub-chunk files as prose — both sanctioned.
Requiring <td> there re-created the Jersey table_003 false positive this check has
already been corrected for twice, so `restructuring_allowed` widens the haystack again
once the tree is permitted to change shape.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_table_presence import PRESENT_THRESHOLD, compute_table_presence  # noqa: E402

CELLS = [
    "reporting counsel for part b:",
    "king and co, second floor, olde towne marina, sandyport, west bay street",
    "sarah packington, telephone 1-242-327-3127, email spackington@example.com",
    "date of report:",
    "the law is stated as at 7 august 2026",
    "responsibility:",
    "this memorandum has been prepared by king and co for aosphere limited",
]
# The generic labels and the long paragraph — the ones a cover page also prints as
# flowed prose. Exactly the set that kept the real ratio above threshold.
ALSO_AS_PROSE = [0, 3, 4, 5, 6]


def _table_html(cells):
    return "<table><tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr></table>"


def _job(root: Path, tree_name: str, tree_body: str) -> Path:
    s2 = root / "02_stage2_mineru_tables"
    (s2 / "tables" / "table_001").mkdir(parents=True)
    (s2 / "tables" / "table_001" / "table.md").write_text(_table_html(CELLS))
    (s2 / "stage2_report.json").write_text(json.dumps({
        "tables": [{"table_id": "table_001", "ok": True, "pages": [1],
                    "rows": 1, "cols": len(CELLS)}]}))
    tree = root / tree_name
    tree.mkdir(parents=True)
    (tree / "01-front-matter.md").write_text(tree_body)
    return root


def _prose_only_body() -> str:
    """The table is gone; the words a cover page repeats are still there."""
    return "# Front Matter\n\n*Source: `source.pdf`, page 1–1*\n\n" + "\n\n".join(
        CELLS[i] for i in ALSO_AS_PROSE)


def test_a_table_still_rendered_as_a_table_passes(tmp_path):
    body = "# Front Matter\n\n*Source: `source.pdf`, page 1–1*\n\n" + _table_html(CELLS)
    r = compute_table_presence(_job(tmp_path, "03_stage3_final", body), stage=3)
    assert r["tables_checked"] == 1
    assert r["missing_count"] == 0


def test_a_deleted_table_is_reported_even_though_its_words_survive_as_prose(tmp_path):
    """The regression this file exists for. Under a whole-document word search this
    scores 5/7 = 0.71 and passes; against rendered cells it scores 0."""
    r = compute_table_presence(_job(tmp_path, "03_stage3_final", _prose_only_body()),
                               stage=3)
    assert r["missing_count"] == 1
    flag = r["flags"][0]
    assert flag["table_id"] == "table_001"
    assert flag["found_ratio"] == 0.0
    # ...and the prose survivors are reported, because "the rows are gone but the text
    # survives" and "this content left the document" need different repairs.
    assert flag["cells_found_as_prose"] == len(ALSO_AS_PROSE)
    assert len(ALSO_AS_PROSE) / len(CELLS) > PRESENT_THRESHOLD   # would have passed


def test_after_stage_4_the_same_prose_is_accepted_because_restructuring_is_allowed(tmp_path):
    """Stage 5 splits a questionnaire table into sub-chunks whose rows become prose.
    Measured on Jersey__181919 table_003: 3 of 8 cells stay cells, 4 are present
    verbatim as prose under their own sub-section. Flagging that is the false positive,
    not the finding."""
    root = _job(tmp_path, "05_subchunks", _prose_only_body())
    assert compute_table_presence(root, stage=5)["missing_count"] == 0


def test_a_cell_the_tree_split_into_several_is_still_present(tmp_path):
    """MinerU routinely extracts several cells AS ONE, and stage 4/5 then split them
    correctly. Measured on Bahamas-sonnet-nocounts table_011: one extracted "cell" is
    761 characters holding questions (a) (b) (c) and (d); the tree has them as four
    separate cells with the answers interleaved. Whole-cell matching reported 75 of 156
    cells missing on a table that lost nothing — the tree was MORE correct than the
    extraction it was being compared against."""
    merged = ("(a) may the fund market to retail investors in your jurisdiction?"
              "(b) may it market to professional investors only?")
    s2 = tmp_path / "02_stage2_mineru_tables"
    (s2 / "tables" / "table_001").mkdir(parents=True)
    (s2 / "tables" / "table_001" / "table.md").write_text(_table_html([merged]))
    (s2 / "stage2_report.json").write_text(json.dumps({
        "tables": [{"table_id": "table_001", "ok": True, "pages": [1], "rows": 1, "cols": 1}]}))
    tree = tmp_path / "05_subchunks"
    tree.mkdir()
    # Split into two cells, with an answer between them — the shape stage 4 produces.
    (tree / "01-marketing.md").write_text(
        "# Marketing\n\n*Source: `source.pdf`, page 1–1*\n\n"
        "<table><tr><td>(a) may the fund market to retail investors in your jurisdiction?</td>"
        "<td>No, not without a licence.</td></tr>"
        "<tr><td>(b) may it market to professional investors only?</td>"
        "<td>Yes, subject to notification.</td></tr></table>")
    assert compute_table_presence(tmp_path, stage=5)["missing_count"] == 0


def test_a_split_cell_that_lost_half_its_text_is_still_reported(tmp_path):
    """The relaxation moves WHERE the boundaries fall, never HOW MUCH survives."""
    merged = ("(a) may the fund market to retail investors in your jurisdiction?"
              "(b) may it market to professional investors only?")
    s2 = tmp_path / "02_stage2_mineru_tables"
    (s2 / "tables" / "table_001").mkdir(parents=True)
    (s2 / "tables" / "table_001" / "table.md").write_text(_table_html([merged]))
    (s2 / "stage2_report.json").write_text(json.dumps({
        "tables": [{"table_id": "table_001", "ok": True, "pages": [1], "rows": 1, "cols": 1}]}))
    tree = tmp_path / "05_subchunks"
    tree.mkdir()
    (tree / "01-marketing.md").write_text(
        "# Marketing\n\n*Source: `source.pdf`, page 1–1*\n\n"
        "<table><tr><td>(a) may the fund market to retail investors in your jurisdiction?</td>"
        "<td>No, not without a licence.</td></tr></table>")
    assert compute_table_presence(tmp_path, stage=5)["missing_count"] == 1


def test_the_stage_5_allowance_does_not_excuse_a_table_that_actually_vanished(tmp_path):
    """Widening the haystack after stage 4 must not become a blanket pass: content
    absent from cells AND from prose is still reported."""
    body = "# Front Matter\n\n*Source: `source.pdf`, page 1–1*\n\nnothing of the table remains here"
    assert compute_table_presence(_job(tmp_path, "05_subchunks", body),
                                  stage=5)["missing_count"] == 1
