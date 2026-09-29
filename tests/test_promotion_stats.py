"""The gallery card's `stats`, recovered from the scorecard instead of the PDF.

`push_hybrid_s3.doc_display_stats` opens the source PDF with fitz for the page count and
rglobs the whole Stage-3 tree for surviving snapshot references. A promotion copies three
small objects per document server-side, so downloading a 7MB PDF and a tree per document to
recover four numbers the scorecard already carries would cost more than the promotion does.

Three of the five are EXACT (the scorecard embeds stage3_report.json verbatim —
check_scorecard.py:1555 — and `pages.total` IS stage 1's fitz page count). The fourth,
`pages_snapshotted`, is a property of the final tree and can only be named from the other
side; what that approximation is, and why it is defensible, is asserted below.

The two that are absent — files_written, word_delta — are absent on every published MinerU
row today as well, because they come from parse_report reading CONVERSION_REPORT.md, which
this path has never run. Absent, not invented: a fabricated number in a legal document's
quality report is worse than a blank.
"""
import pytest

pytest.importorskip("numpy")

from aosphere_core_index.service.doc_gallery import _snap_count  # noqa: E402
from aosphere_core_index.service.promotion_monitor import promotion_stats  # noqa: E402


def scorecard(**over):
    sc = {
        "gate": "pass", "worst_score": 91.2,
        "pages": {"total": 148,
                  "states": {"3": "unvalidatable", "4": "ok", "9": "unvalidatable",
                             "17": "flagged"},
                  "counts": {"silent": 0, "flagged": 1}},
        "tables": [{"table_id": "t1", "pages": [3], "bucket": "ok"},
                   {"table_id": "t2", "pages": [41, 42], "bucket": "failed"},
                   {"table_id": "t3", "pages": [88], "bucket": "failed"}],
        "stage3": {"tables_total": 12, "tables_filled": 9, "tables_failed": 3},
    }
    sc.update(over)
    return sc


# ---------------- the exact three ----------------
def test_the_page_count_is_stage_ones_own_fitz_count():
    assert promotion_stats(scorecard())["pages"] == 148


def test_the_table_counts_come_from_the_embedded_stage3_report():
    s = promotion_stats(scorecard())
    assert (s["tables_total"], s["tables_converted"], s["tables_failed"]) == (12, 9, 3)


def test_no_pdf_and_no_tree_are_touched():
    """The whole point: `promotion_stats` is a pure function of a parsed scorecard, so
    there is no path by which it could open a PDF or walk a tree."""
    import ast
    import inspect
    import textwrap
    fn = ast.parse(textwrap.dedent(inspect.getsource(promotion_stats))).body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]                       # the docstring NAMES what it avoids
    code = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
    for forbidden in ("fitz", "rglob", "open", "read_text", "get_object", "Path"):
        assert forbidden not in code, f"promotion_stats must not reach for {forbidden}"


# ---------------- the approximate one ----------------
def test_snapshotted_pages_are_the_union_of_unvalidatable_pages_and_failed_tables():
    """`unvalidatable` is defined by check_scorecard._page_states as a page that WAS
    snapshotted in stage 1 and is not covered by an OK table; a failed table's pages kept
    their snapshot because the table failed. That union names the same set
    doc_display_stats finds by re-scanning the final markdown."""
    assert promotion_stats(scorecard())["pages_snapshotted"] == [3, 9, 41, 42, 88]


def test_a_page_whose_table_converted_is_not_counted_as_snapshotted():
    sc = scorecard(tables=[{"table_id": "t1", "pages": [41, 42], "bucket": "ok"}],
                   pages={"total": 10, "states": {}, "counts": {}})
    assert promotion_stats(sc)["pages_snapshotted"] == []


def test_the_approximation_says_where_it_came_from():
    """It can differ from doc_display_stats in edge cases (a snapshot inside a wholly
    dropped section), so a reader is never left guessing which number this is."""
    assert promotion_stats(scorecard())["pages_snapshotted_source"] == "scorecard"


def test_it_is_a_real_list_which_is_the_cleanest_of_the_three_shapes_the_ui_accepts():
    """_snap_count normalises list / int / stringified-list; only the COUNT is rendered."""
    s = promotion_stats(scorecard())
    assert isinstance(s["pages_snapshotted"], list)
    assert all(isinstance(p, int) for p in s["pages_snapshotted"])
    assert _snap_count(s["pages_snapshotted"]) == 5
    assert isinstance(_snap_count(s["pages_snapshotted"]), int)


# ---------------- degradation ----------------
def test_a_scorecard_with_no_stage3_block_yields_blanks_not_a_crash():
    """Older documents, and any scorecard written before the stage3 report was embedded."""
    s = promotion_stats({"pages": {"total": 5}})
    assert s["pages"] == 5
    assert s["tables_total"] is None and s["tables_converted"] is None
    assert s["pages_snapshotted"] == []


@pytest.mark.parametrize("sc", [None, {}, {"pages": None, "tables": None, "stage3": None}])
def test_an_empty_or_broken_scorecard_is_survivable(sc):
    s = promotion_stats(sc)
    assert s["pages"] is None and s["pages_snapshotted"] == []


def test_non_numeric_page_keys_are_ignored_rather_than_raising():
    sc = scorecard(pages={"total": 3, "states": {"cover": "unvalidatable", "2": "unvalidatable"}})
    assert promotion_stats(sc)["pages_snapshotted"] == [2, 41, 42, 88]


# ---------------- the deliberate absences ----------------
def test_publish_time_extras_are_absent_exactly_as_they_are_today():
    """doc_gallery._doc_row already renders these blank for every -mineru row, because the
    MinerU publish path has never run parse_report. So this is not a regression — and
    inventing them would be worse than the blank."""
    s = promotion_stats(scorecard())
    assert "files_written" not in s
    assert "word_delta" not in s
    assert "word_delta_pct" not in s
