"""A clause heading printed BETWEEN two tables on one page must end the table above it.

_page_bands rejects a row band when it holds a heading — either heading-SIZED, or matching
one of the document's own outline titles. A rejected band clamps the next run's start past
it, so the run's placeholder is emitted BELOW the heading and the table lands in the clause
the heading opens. When neither signal fires, nothing clamps, the placeholder is emitted at
the top of the page, and the table renders inside the PREVIOUS clause.

Both signals miss the same thing, for the same reason:

    size   these headings are set at 11pt against a 10pt body, and head_min is
           body + 1.4 = 11.4 — so _is_heading_size is False by 0.4pt.
    title  _row_is_titled required a cell of >= 8 characters. "LICENCE" is SEVEN.

Page 125 of Norway 171186 prints clause 9's table tail, then "10." and "LICENCE" as two
columns of one band at y186-224, then clause 10's own table from y224 to y520. The band was
kept as table DATA, so clause 10's 7-page table (pages 125-131) rendered under
"9 PROSPECTUS REGULATION (PR3)" and 12-licence.md shipped as seven page images with no
table at all. Same in Spain 176285, Sweden 174961, Belgium 163341, Germany 179874,
Ireland 175625 and Netherlands 156999 — every one of them on clause 10.

The >= 8 bar is there so a short FRAGMENT cannot claim to be a heading by prefix. A cell
that IS the title is not a fragment, so an EXACT match to an outline title skips the bar —
the same exception pdf2mdtree's per-line pass already makes, via the same title_exacts set,
for the same document (Sweden's "10." / "LICENCE" split is recorded there too).
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

fitz = pytest.importorskip("fitz")
import pdf2mdtree as P  # noqa: E402

HEAD_MIN = 11.4                      # a 10pt body: exactly Norway 171186's
TITLES = ({P.norm("10 LICENCE"), P.norm("9 PROSPECTUS REGULATION (PR3)")},
          tuple(sorted({P.norm("9 PROSPECTUS REGULATION (PR3)"),
                        P.norm("PROSPECTUS REGULATION (PR3)")})))
EXACTS = frozenset(TITLES[0] | {P.norm("LICENCE"), P.norm("PROSPECTUS REGULATION (PR3)")})

ROW = ["LICENCE", "10."]             # as the column split hands it over, key column first


def test_a_seven_character_title_is_recognised():
    assert P._row_is_titled(ROW, TITLES, EXACTS), (
        '"LICENCE" is an outline title in full, not a fragment of one — the >= 8 bar must '
        "not apply to an exact match")


def test_it_is_still_missed_without_the_exact_set():
    """Precisely what the bar used to cost, so the test above cannot pass for free."""
    assert not P._row_is_titled(ROW, TITLES), "the regression this guards is not reproduced"


def test_a_long_title_still_matches_as_before():
    assert P._row_is_titled(["PROSPECTUS REGULATION (PR3)", ""], TITLES, EXACTS)
    assert P._row_is_titled(["PROSPECTUS REGULATION (PR3)", ""], TITLES)


def test_ordinary_cell_content_is_not_a_title():
    """The bar's real job. Short data values recur in every one of these tables and must
    never be read as headings — an exact match to a TITLE is the only new escape."""
    for row in (["Yes", "No"], ["No", ""], ["hedge fund strategy", "No"],
                ["(a)", "The type of licence that would be required;"]):
        assert not P._row_is_titled(row, TITLES, EXACTS), row


def test_a_cell_merely_quoting_a_title_is_not_one():
    """These documents cross-refer constantly. Only a cell that IS the title matches."""
    assert not P._row_is_titled(
        ["See the licence requirements described in section 10 above.", ""], TITLES, EXACTS)


def _norway_page():
    """p125's geometry: clause 9's tail, the clause 10 heading, then clause 10's table."""
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    for y in (75.8, 95.4, 185.9, 224.4, 243.8, 520.4):
        page.draw_line((51, y), (762, y), width=0.5)
    page.insert_text((54.6, 91), "Questions", fontsize=10)        # clause 9's header
    page.insert_text((374, 91), "Answers", fontsize=10)
    page.insert_text((126.6, 111), "(c) For AIFs which fall into the scope of PR3", fontsize=10)
    page.insert_text((374, 111), "It is necessary to separately consider", fontsize=10)
    page.insert_text((51.1, 219), "10.", fontsize=11)             # the heading, at 11pt
    page.insert_text((87.1, 219), "LICENCE", fontsize=11)
    page.insert_text((54.6, 240), "Questions", fontsize=10)       # clause 10's header
    page.insert_text((374, 240), "Answers", fontsize=10)
    page.insert_text((54.6, 259), "If a licence is required in order to engage", fontsize=10)
    page.insert_text((374, 259), "Yes. The marketing and licensing requirements", fontsize=10)
    return doc, page


# The canonical column bounds for that page: the key column starts left of the enumeration
# token, so "10." and "LICENCE" land in DIFFERENT cells — which is what _page_bands actually
# hands _row_is_titled on p125, and why neither cell clears the >= 8 bar on its own.
BOUNDS = [0, 70, 300, 842]


def test_the_heading_band_on_the_real_page_shape_is_rejected():
    doc, page = _norway_page()
    try:
        bands = P._page_bands(page, BOUNDS, head_min=HEAD_MIN,
                              titles=TITLES, title_exacts=EXACTS)
        heading_band = [(row, lo, hi) for row, lo, hi in bands if 185 <= lo <= 187]
        assert heading_band, f"no band at the heading's y-range; got {[b[1] for b in bands]}"
        row, lo, hi = heading_band[0]
        assert row is None, (
            f"the clause heading band was kept as table data ({row}) — nothing then clamps "
            "the run's start past it, so the next clause's table renders in this one")
    finally:
        doc.close()


def test_the_same_band_is_kept_without_the_exact_set():
    """The bug as it stood, on the same page — so the test above is not vacuous."""
    doc, page = _norway_page()
    try:
        bands = P._page_bands(page, BOUNDS, head_min=HEAD_MIN, titles=TITLES)
        band = [(row, lo, hi) for row, lo, hi in bands if 185 <= lo <= 187]
        assert band, "no band at the heading's y-range"
        assert band[0][0] is not None, "the regression this guards is not reproduced"
    finally:
        doc.close()


def test_the_clause_9_data_band_above_it_is_still_data():
    """Only the heading is rejected — the real table rows either side of it survive."""
    doc, page = _norway_page()
    try:
        bands = P._page_bands(page, BOUNDS, head_min=HEAD_MIN,
                              titles=TITLES, title_exacts=EXACTS)
        kept = [(row, lo) for row, lo, hi in bands if row is not None]
        assert any(95 <= lo <= 97 for _, lo in kept), (
            f"clause 9's data band was rejected too; kept bands at {[lo for _, lo in kept]}")
        assert any(243 <= lo <= 245 for _, lo in kept), (
            f"clause 10's first data band was rejected; kept bands at {[lo for _, lo in kept]}")
    finally:
        doc.close()
