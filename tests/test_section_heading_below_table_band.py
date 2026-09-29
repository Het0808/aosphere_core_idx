"""A clause whose heading is printed UNDER the previous clause's table must keep its table.

These questionnaire tables do not align to page boundaries. A clause's table routinely
finishes two thirds of the way down a page, and the NEXT clause's heading is then printed
underneath it, on that same page:

    p67   y  76-466   rows — the TAIL of clause 6.4's table
          y 504       "7. PRIVATE PLACEMENT REGIME"
    p68   y  76-535   rows — clause 7.1's table starts here

`sec_pages` only knows which PAGE a section starts on, never where on it. So the run above
was cut at the page boundary, a NEW run was seeded at the top of p67 — a page that is
entirely clause 6's table — and that run's placeholder was emitted above the heading, i.e.
into clause 6. Stage 3 then spliced all 31 pages of clause 7's table into clause 6.

Measured on Marketing Restrictions - Asset Management, Czech Republic 166819: table_008,
100 rows x 7 cols and 98KB of perfectly good MinerU output, rendered inside
07-marketing-selling-to-the-public.md, while "7.1 Private Placement Regime" — the substance
of the whole clause — shipped as 31 page images and nothing else. Every check passed it:
the table's first page is 67 and clause 7 starts on page 67, so the page range agreed with
itself. 13 further sections across the 106-document corpus are printed this way.

The fix reads the page: a heading BELOW the last rule means the band above it still belongs
to the run that is already open, so the page joins THAT run and the new one starts on the
next page.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

fitz = pytest.importorskip("fitz")
import pdf2mdtree as P  # noqa: E402

W, H = 842, 595                      # A4 landscape, as these memoranda are set
BAND_TOP, BAND_BOT = 76, 466         # where the rules sit on the seam page
HEADING_Y = 504                      # ...and where clause 7's heading is printed, below them

# The title sets exactly as pdf2mdtree's main() builds them: norm() of each outline
# title, plus the same title minus its enumeration token (a printed heading sets "7."
# and "PRIVATE PLACEMENT REGIME" as separate lines, so only the second form can match).
_OUTLINE = ("6.4 Investment Management & Advisory Services", "7 Private Placement Regime")
_REST = ("Investment Management & Advisory Services", "Private Placement Regime")
TITLES = ({P.norm(t) for t in _OUTLINE},
          tuple(sorted({P.norm(t) for t in _OUTLINE + _REST})))
EXACTS = frozenset(TITLES[0] | {P.norm(t) for t in _REST})


def _ruled_page(doc, rows, heading=None, band_bot=BAND_BOT):
    """One landscape page of ruled table rows, optionally with a heading UNDER the rules."""
    page = doc.new_page(width=W, height=H)
    ys = [BAND_TOP + i * (band_bot - BAND_TOP) / max(len(rows), 1) for i in range(len(rows))]
    for y, row in zip(ys, rows):
        page.draw_line((51, y), (762, y), width=0.5)          # a row rule, >150pt wide
        page.insert_text((57, y + 12), row[0], fontsize=10)
        page.insert_text((374, y + 12), row[1], fontsize=10)
    page.draw_line((51, band_bot), (762, band_bot), width=0.5)
    if heading:
        page.insert_text((87, HEADING_Y), heading, fontsize=11)
    return page


def _seam_doc():
    """p0-p1 clause 6's table; p2 its TAIL plus clause 7's heading below the rules;
    p3-p4 clause 7's table. The outline puts clause 7 on p2 — the seam page."""
    doc = fitz.open()
    rows = [("Questions", "Answers")] + [(f"(a{i}) question text {i}", f"answer text {i}")
                                         for i in range(5)]
    _ruled_page(doc, rows)
    _ruled_page(doc, rows)
    _ruled_page(doc, rows, heading="PRIVATE PLACEMENT REGIME")
    _ruled_page(doc, rows)
    _ruled_page(doc, rows)
    return doc


SEAM = 2          # the page carrying both clause 6's table tail and clause 7's heading


def _comp(doc, **kw):
    return P.reconstruct_multipage_tables(
        doc, 0, doc.page_count, sec_pages={SEAM}, head_min=11.4,
        titles=TITLES, clause_tables=True, title_exacts=EXACTS, **kw)


def test_the_heading_under_the_rules_is_found():
    doc = _seam_doc()
    try:
        y = P._head_below_band(doc[SEAM], BAND_BOT, head_min=11.4,
                               titles=TITLES, title_exacts=EXACTS)
        assert y is not None, "the clause heading printed below the rules was not seen"
        assert y > BAND_BOT, f"y={y} is inside the band, not below it"
    finally:
        doc.close()


def test_a_page_with_no_heading_under_its_rules_reports_none():
    """The common case, and the one that must stay untouched: rules, then nothing."""
    doc = _seam_doc()
    try:
        assert P._head_below_band(doc[0], BAND_BOT, head_min=11.4,
                                  titles=TITLES, title_exacts=EXACTS) is None
    finally:
        doc.close()


def test_the_seam_page_joins_the_run_above_it_not_the_one_below():
    """The bug itself. The seam page's rows are clause 6's, so it must belong to clause 6's
    run — and clause 7's run must start on the NEXT page, where its own table does."""
    doc = _seam_doc()
    try:
        comp = _comp(doc)
        assert SEAM in comp, "the seam page was dropped from every run"
        assert comp[SEAM]["first"] == 0, (
            f"the seam page seeded a new run at {comp[SEAM]['first']} — its rows are the "
            "PREVIOUS clause's table, so its placeholder renders in the previous section")
        assert not comp[SEAM]["is_first"], (
            "the placeholder is emitted on the run's first page; emitting it here puts it "
            "ABOVE clause 7's heading, i.e. inside clause 6")
    finally:
        doc.close()


def test_the_next_clause_s_run_starts_on_its_own_first_page():
    doc = _seam_doc()
    try:
        comp = _comp(doc)
        firsts = [p for p, v in comp.items() if v["is_first"]]
        assert SEAM + 1 in firsts, (
            f"clause 7's run does not begin on p{SEAM + 1}, where its table actually starts "
            f"— placeholders are emitted on {firsts}")
    finally:
        doc.close()


def test_a_heading_printed_ABOVE_the_rules_still_cuts_the_run():
    """The ordinary boundary must keep working: where a clause starts at the TOP of a page,
    that page's rows ARE the new clause's, and the run above it ends at the page before."""
    doc = fitz.open()
    rows = [("Questions", "Answers")] + [(f"(a{i}) question", f"answer {i}") for i in range(5)]
    _ruled_page(doc, rows)
    _ruled_page(doc, rows)
    page = doc.new_page(width=W, height=H)                     # the clause opens this page
    page.insert_text((87, 60), "PRIVATE PLACEMENT REGIME", fontsize=11)
    for i, row in enumerate(rows):
        y = 120 + i * 60
        page.draw_line((51, y), (762, y), width=0.5)
        page.insert_text((57, y + 12), row[0], fontsize=10)
        page.insert_text((374, y + 12), row[1], fontsize=10)
    page.draw_line((51, 120 + len(rows) * 60), (762, 120 + len(rows) * 60), width=0.5)
    try:
        comp = _comp(doc)
        assert comp.get(2, {}).get("first") != 0, (
            "a clause opening at the top of its page was swallowed into the run above it")
    finally:
        doc.close()


def test_a_table_cell_quoting_a_section_title_cannot_fire_it():
    """What makes the rule safe: only lines BELOW the last rule count. A cell that happens
    to name a section — these documents cross-refer constantly — sits inside the band."""
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    for i, y in enumerate((100, 200, 300)):
        page.draw_line((51, y), (762, y), width=0.5)
        page.insert_text((57, y + 12), "Private Placement Regime", fontsize=10)
    page.draw_line((51, 400), (762, 400), width=0.5)
    try:
        assert P._head_below_band(page, 400, head_min=11.4,
                                  titles=TITLES, title_exacts=EXACTS) is None
    finally:
        doc.close()


def test_the_running_footer_is_not_mistaken_for_a_heading():
    """A page number and a document title are printed under everything on every page."""
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    page.draw_line((51, 100), (762, 100), width=0.5)
    page.draw_line((51, 400), (762, 400), width=0.5)
    page.insert_text((402, 563), "67", fontsize=8)
    page.insert_text((594, 563), "Private Placement Regime", fontsize=8)   # worst case
    try:
        assert P._head_below_band(page, 400, head_min=11.4,
                                  titles=TITLES, title_exacts=EXACTS) is None
    finally:
        doc.close()
