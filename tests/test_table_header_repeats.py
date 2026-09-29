"""A multi-page table's reprinted header row, and the two ways it is excused.

These surveys are one long comparison table: the 281-page US States privacy survey
reprints its column headers on 216 pages, the pipeline reconstructs the table with ONE
header row, and the other 215 copies read as lost content — 3,914 word-occurrences, 45%
of everything the coverage check called missing.

What is pinned here is the SHAPE of the excuse, because a looser rule silently eats real
content: only pages the extractor's own manifest declared part of a multi-page table,
and only lines that also sit in the header band of that table's first page. A cell value
that recurs on every page ("No omnibus privacy law.", true of most US states) must NOT
be excused.

Also pinned: the strip is safe for a token COUNT but not for the per-section diff, so
the two call sites use different mechanisms. See test_localized_gets_units_not_a_strip.
"""

import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from lib_content_compare import (  # noqa: E402
    pdf_page_texts, strip_page_lines, table_header_repeats, table_header_units,
)

HEADER = ["State", "Data minimisation", "Restrictions on how much data"]
TOP = 100.0                 # the table's top edge on every page
BAND_TEXT_Y = 112.0         # header text, inside the 46pt band
ROW_TEXT_Y = 300.0          # data rows, far below it


@pytest.fixture
def survey(tmp_path):
    """A 4-page table: header reprinted on every page, one cell value repeated too."""
    doc = fitz.open()
    for pno in range(4):
        page = doc.new_page(width=612, height=792)
        y = BAND_TEXT_Y
        for line in HEADER:
            page.insert_text((80, y), line, fontsize=10)
            y += 12
        page.insert_text((80, ROW_TEXT_Y), f"Alabama {pno}", fontsize=10)
        # the same cell text on every page — recurring, but NOT a header
        page.insert_text((80, ROW_TEXT_Y + 20), "No omnibus privacy law.", fontsize=10)
    pdf = tmp_path / "survey.pdf"
    doc.save(str(pdf))
    doc.close()
    return pdf


def _tables(pages, multipage=True):
    return [{"table_id": "table_001", "pages": pages, "multipage": multipage,
             "bboxes": {str(p): [72.0, TOP, 540.0, 700.0] for p in pages}}]


def test_reprints_are_found_on_continuation_pages_only(survey):
    """The first page's header is the copy the tree keeps — it is never excused."""
    rep = table_header_repeats(survey, _tables([1, 2, 3, 4]))
    assert sorted(rep) == [2, 3, 4]
    assert all(len(sigs) == len(HEADER) for sigs in rep.values())


def test_a_repeated_cell_value_is_not_excused(survey):
    """"No omnibus privacy law." is on all four pages and is real content."""
    rep = table_header_repeats(survey, _tables([1, 2, 3, 4]))
    joined = " ".join(s for sigs in rep.values() for s in sigs)
    assert "omnibus" not in joined
    assert "alabama" not in joined


def test_nothing_is_excused_without_a_multipage_table(survey):
    assert table_header_repeats(survey, _tables([1, 2, 3, 4], multipage=False)) == {}
    assert table_header_repeats(survey, _tables([1])) == {}
    assert table_header_repeats(survey, None) == {}
    assert table_header_repeats(survey, []) == {}


def test_pages_outside_the_declared_range_are_untouched(survey):
    """Only pages the manifest named — a table on 2-3 cannot excuse anything on 4."""
    rep = table_header_repeats(survey, _tables([2, 3]))
    assert sorted(rep) == [3]


def test_strip_removes_the_reprints_and_keeps_the_first_copy(survey):
    pages = pdf_page_texts(survey)
    stripped = strip_page_lines(pages, table_header_repeats(survey, _tables([1, 2, 3, 4])))
    assert "Data minimisation" in stripped[1]          # the tree's copy still expected
    for pno in (2, 3, 4):
        assert "Data minimisation" not in stripped[pno]
        assert "No omnibus privacy law." in stripped[pno]   # content survives


def test_localized_gets_units_not_a_strip(survey):
    """Test 2 must classify, never strip: removing text re-aligns the sequence matcher
    and invents spans elsewhere (Private Wealth Armenia 175560 went 70.7 -> 57.5 that
    way, coverage UP and two new flagged sections). Units carry the same knowledge
    without touching the diff."""
    units = table_header_units(survey, _tables([1, 2, 3, 4]))
    assert units, "the header lines must be offered as units"
    flat = [" ".join(u) for u in units]
    assert any("restrictions on how much data" in u for u in flat)
    # a span made of header text is then covered by whole units...
    assert any(u in "state data minimisation restrictions on how much data" for u in flat)
    # ...and no unit is built from the repeated cell value
    assert not any("omnibus" in u for u in flat)


def test_units_include_the_whole_header_block(survey):
    """A span can straddle a wrapped header line ("...data collected, / used and
    stored"), so the page's whole header block is offered as one unit too."""
    units = table_header_units(survey, _tables([1, 2, 3, 4]))
    flat = [" ".join(u) for u in units]
    assert any(u.startswith("state data minimisation restrictions") for u in flat)


def test_short_lines_are_not_units(survey):
    """Two-token lines match by coincidence; the unit floor keeps them out."""
    units = table_header_units(survey, _tables([1, 2, 3, 4]), min_tokens=3)
    assert all(len(u) >= 3 for u in units)
    assert not any(u == ["state"] for u in units)


# ---- the header row printed ABOVE the extractor's own top edge ------------
#
# The shape that made this rule wrong on every 124 questionnaire. A table that opens
# under a section heading has its region fitted to the rows the extractor
# reconstructed, and the printed "Questions | Answers" row is left just OUTSIDE it,
# above the recorded top edge — on the FIRST page only, which is the page the
# reference set is built from. So the reference set held the first question instead of
# the header, every reprint below failed to match it, and each one read as lost
# content. Measured on 124/Czech Republic 166819: the pages 23-45 table records a top
# edge of 404.3 on page 23 with the header printing at 360.0, and all 22 reprints were
# discarded.
FIRST_PAGE_TOP = 160.0      # where the extractor put this table's top edge on page 1
HEADING_Y = 60.0            # the section heading above it — NOT part of the table

@pytest.fixture
def opens_under_a_heading(tmp_path):
    """A 3-page table whose header row sits above the first page's recorded top edge."""
    doc = fitz.open()
    for pno in range(3):
        page = doc.new_page(width=612, height=792)
        if pno == 0:
            page.insert_text((80, HEADING_Y), "4. SPECIFIC ISSUES", fontsize=14)
            page.insert_text((80, FIRST_PAGE_TOP - 30), "Questions", fontsize=10)
            page.insert_text((300, FIRST_PAGE_TOP - 30), "Answers", fontsize=10)
        else:
            page.insert_text((80, TOP + 12), "Questions", fontsize=10)
            page.insert_text((300, TOP + 12), "Answers", fontsize=10)
        page.insert_text((80, ROW_TEXT_Y), f"(a) Please confirm {pno}", fontsize=10)
    pdf = tmp_path / "questionnaire.pdf"
    doc.save(str(pdf))
    doc.close()
    return pdf


def _heading_tables():
    boxes = {"1": [72.0, FIRST_PAGE_TOP, 540.0, 700.0]}
    boxes.update({str(p): [72.0, TOP, 540.0, 700.0] for p in (2, 3)})
    return [{"table_id": "table_001", "pages": [1, 2, 3], "multipage": True, "bboxes": boxes}]


def test_header_above_the_first_pages_top_edge_is_still_a_header(opens_under_a_heading):
    rep = table_header_repeats(opens_under_a_heading, _heading_tables())
    assert sorted(rep) == [2, 3], "the reprints on pages 2-3 must be recognised"
    joined = " ".join(s for sigs in rep.values() for s in sigs)
    assert "questions" in joined and "answers" in joined


def test_the_section_heading_above_the_table_is_not_a_header(opens_under_a_heading):
    """Reach above the top edge is one band's worth, and a line up there still has to
    be reprinted below before it counts — a heading is neither."""
    rep = table_header_repeats(opens_under_a_heading, _heading_tables())
    joined = " ".join(s for sigs in rep.values() for s in sigs)
    assert "specific" not in joined
    assert "please confirm" not in joined
