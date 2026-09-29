"""Recovering table rows that content_list.json drops for a page-spanning table.

MinerU's `_content_list.json` is the normalised shape the pipeline reads, but it MERGES
a table that spans pages into ONE entry, leaving the table's other pages as empty
`table_body` stubs. Those stubs are matched geometrically at high IoU, so the region is
stamped ok=true and nobody notices the rows never arrived. On Saudi Arabia__174731
table_007 (pages 17-26) content_list produced one fat block on page 21 and EMPTY bodies
for 8 of the 10 pages, and its merged form omits question (a) altogether.

`_middle.json` keeps the same table split per page with those rows intact.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hybrid_extract as he  # noqa: E402


def _middle(pages):
    """pages: list of lists of table-HTML strings, one list per crop page."""
    return {"pdf_info": [
        {"preproc_blocks": [
            {"type": "table", "blocks": [
                {"lines": [{"spans": [{"type": "table", "html": h}]} for h in htmls]}]}
        ] if htmls else []}
        for htmls in pages]}


def _write(tmp_path, obj, stem="combined_tables"):
    d = tmp_path / "raw" / "sub"
    d.mkdir(parents=True)
    (d / f"{stem}_middle.json").write_text(json.dumps(obj))
    return tmp_path / "raw"


def test_reads_table_html_per_crop_page(tmp_path):
    raw = _write(tmp_path, _middle([["<table><tr><td>A</td></tr></table>"],
                                    [],
                                    ["<table><tr><td>C</td></tr></table>"]]))
    got = he.middle_table_html_by_page(raw, "combined_tables")
    assert set(got) == {0, 2}
    assert "A" in got[0][0] and "C" in got[2][0]


def test_several_blocks_on_one_page_are_all_kept(tmp_path):
    raw = _write(tmp_path, _middle([["<table><tr><td>one</td></tr></table>",
                                     "<table><tr><td>two</td></tr></table>"]]))
    got = he.middle_table_html_by_page(raw, "combined_tables")
    assert len(got[0]) == 2


def test_blank_html_is_not_recorded(tmp_path):
    raw = _write(tmp_path, _middle([["   "], [""]]))
    assert he.middle_table_html_by_page(raw, "combined_tables") == {}


def test_a_missing_middle_json_is_not_an_error(tmp_path):
    # No supplement available is exactly the old behaviour, not a failure.
    assert he.middle_table_html_by_page(tmp_path / "nope", "combined_tables") == {}
    empty = tmp_path / "raw"
    empty.mkdir()
    assert he.middle_table_html_by_page(empty, "combined_tables") == {}


def test_unreadable_middle_json_is_not_an_error(tmp_path):
    d = tmp_path / "raw" / "sub"
    d.mkdir(parents=True)
    (d / "combined_tables_middle.json").write_text("{not json")
    assert he.middle_table_html_by_page(tmp_path / "raw", "combined_tables") == {}


def test_non_table_spans_are_ignored(tmp_path):
    obj = {"pdf_info": [{"preproc_blocks": [
        {"type": "table", "blocks": [{"lines": [
            {"spans": [{"type": "text", "content": "not a table"}]}]}]}]}]}
    raw = _write(tmp_path, obj)
    assert he.middle_table_html_by_page(raw, "combined_tables") == {}


def test_stem_must_match(tmp_path):
    raw = _write(tmp_path, _middle([["<table><tr><td>A</td></tr></table>"]]))
    assert he.middle_table_html_by_page(raw, "some_other_stem") == {}


# --- the two representations must never be mixed -----------------------------
# content_list merges a page-spanning table into ONE entry. Filling only the blank
# pages from middle.json leaves that merged entry in place too, so every row it holds
# is emitted twice. Measured on Saudi Arabia__174731: uniqueness 86.9 -> 0.0, 78 cells
# with 47 distinct. The rule is all-or-nothing per region.

def _decide(bodies, mids):
    """Mirror of the swap decision in run_stage2 — kept as a pure function so the
    rule can be asserted without standing up a whole Stage 2."""
    any_blank = any(not (b or "").strip() for b in bodies)
    covers_all = bool(mids) and all(mids.get(i) for i in range(len(bodies)))
    if any_blank and covers_all:
        return "middle", ["\n".join(mids[i]) for i in range(len(bodies))]
    return "content_list", list(bodies)


def test_full_coverage_swaps_the_whole_region():
    bodies = ["", "<table><tr><td>merged whole table</td></tr></table>", ""]
    mids = {0: ["<table><tr><td>p0</td></tr></table>"],
            1: ["<table><tr><td>p1</td></tr></table>"],
            2: ["<table><tr><td>p2</td></tr></table>"]}
    which, out = _decide(bodies, mids)
    assert which == "middle"
    assert "merged whole table" not in "".join(out), "the merged entry must be dropped"
    assert all(f"p{i}" in out[i] for i in range(3))


def test_partial_coverage_changes_nothing():
    bodies = ["", "<table><tr><td>merged whole table</td></tr></table>", ""]
    mids = {0: ["<table><tr><td>p0</td></tr></table>"]}      # page 2 not covered
    which, out = _decide(bodies, mids)
    assert which == "content_list"
    assert out == bodies, "a partial swap would mix representations — leave it alone"


def test_no_blanks_means_no_swap():
    bodies = ["<table><tr><td>a</td></tr></table>", "<table><tr><td>b</td></tr></table>"]
    mids = {0: ["<table><tr><td>x</td></tr></table>"], 1: ["<table><tr><td>y</td></tr></table>"]}
    which, out = _decide(bodies, mids)
    assert which == "content_list", "nothing was missing, so nothing is substituted"
    assert out == bodies
