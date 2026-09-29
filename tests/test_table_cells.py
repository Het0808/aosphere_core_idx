"""A merged column loses no words, so only the geometry can see it.

check_table_cells asks the one question no other check here asks: did the GRID survive?
Every content check compares text, and a cell that swallowed the column beside it still
holds every word of both, so word coverage, per-section gaps and table presence are all
green on it.

THE FIRST IMPLEMENTATION OF THIS WAS WRONG AND SHIPPED FINDINGS FOR A WHOLE REVIEW
CYCLE. It took a rendered cell's text, found those words on the page, and asked whether
they crossed a column corridor. Finding the words meant anchoring on the cell's first
and last few and taking every page word between them — but fitz returns words in READING
order, which in a two-column table interleaves the columns line by line, and the anchor
matches the first row whose text starts the same way, which on a questionnaire is
routinely the wrong row. On Jersey p76 a 34-word answer cell resolved to a 121-word
segment spanning the full page width. Worse, the failure was SELECTED FOR: a segment can
only cross a corridor when the lookup went wrong, so 77 of that document's 81 cells
resolved correctly and were silently fine while the 3 that resolved to page-wide slabs
were the 3 reported. Every finding was the check reporting its own failure.

So this never looks a cell up by text. It rebuilds the PDF's OWN cells from geometry and
asks a containment question instead, and the tests below pin each step of that plus the
two things that make it honest: it must fire on a real merge (positive control), and it
must not fire on the four shapes that fooled the first version.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_table_cells import (  # noqa: E402
    MIN_PDF_CELL_TOKENS, _best_span, _cells_inside, _classify, _column_bounds,
    _norm_words, _pdf_cells, _row_bounds,
)

BBOX = (0.0, 0.0, 400.0, 200.0)


def word(x0, y0, text="w", w=30.0, h=10.0):
    return (x0, y0, x0 + w, y0 + h, text)


def toks(prefix, n):
    return [f"{prefix}{i}" for i in range(n)]


# ---- column boundaries -------------------------------------------------------
def two_columns():
    """Left column at x 0-30, right at x 200-230: a corridor no row crosses."""
    out = []
    for i, y in enumerate((10.0, 30.0, 50.0)):
        out += [word(0.0, y, f"l{i}"), word(200.0, y, f"r{i}")]
    return out


def test_a_corridor_no_row_crosses_is_a_column_boundary():
    xs = _column_bounds(two_columns(), BBOX)
    assert len(xs) == 3                      # two columns -> three edges
    assert 30.0 < xs[1] < 200.0


def test_a_gap_one_row_crosses_is_not_a_column_boundary():
    """The list-indent case: "(ii)" at x<111 with its paragraph from x>135 is clear on
    its own row and occupied on every other one. Testing per row instead of per region
    flagged 83 of 790 cells on one document, essentially all numbered sub-items."""
    words = two_columns() + [word(0.0, 70.0, "wide", w=260.0)]
    assert len(_column_bounds(words, BBOX)) == 2      # region edges only


# ---- row boundaries ----------------------------------------------------------
def test_a_full_width_rule_is_a_row_boundary():
    rules = [(100.0, 0.0, 400.0)]
    assert _row_bounds(rules, BBOX)[1:-1] == [100.0]


def test_a_hyperlink_underline_is_not_a_row_boundary():
    """Measured on Jersey p86, whose right-hand cell holds three underlined JFSC links.
    Each underline is a long horizontal line, and because row boundaries apply across
    every column it also split the LEFT cell's "(i) … (ii) …" list. Both cells are single
    cells in the source and both were reported as merges."""
    assert _row_bounds([(100.0, 200.0, 280.0)], BBOX)[1:-1] == []


def test_a_row_rule_drawn_as_separate_per_column_segments_still_counts():
    """These tables border each CELL, so one row line arrives as two or three pieces,
    none covering 70% of the table alone. Without unioning them no inner rule was ever
    found: every grid came out one row tall and only 31 of Jersey's 905 rendered cells
    could be compared to anything."""
    segs = [(100.0, 0.0, 190.0), (100.0, 200.0, 400.0)]
    assert _row_bounds(segs, BBOX)[1:-1] == [100.0]


def test_a_corridor_with_no_rule_through_it_is_not_a_row_boundary():
    """A horizontal gap alone cannot tell a row from a paragraph break: the verified row
    merge on Bahamas p64 sits in an 8.0pt gap where the median line gap is 13.1pt (ratio
    0.61) and Austria's paragraph breaks sit at ratios 0.59 and 0.64 — the same number."""
    assert _row_bounds([], BBOX)[1:-1] == []


# ---- the grid ----------------------------------------------------------------
def test_every_word_lands_in_the_slot_its_centre_falls_in():
    cells = _pdf_cells(two_columns(), [0.0, 100.0, 400.0], [0.0, 40.0, 200.0])
    assert set(cells) == {(0, 0), (0, 1), (1, 0), (1, 1)}
    assert cells[(0, 0)] == ["l0", "l1"]
    assert cells[(1, 1)] == ["r2"]


# ---- the merge claim ---------------------------------------------------------
def test_POSITIVE_CONTROL_two_source_cells_in_one_output_cell_is_a_merge():
    """The test that matters. A detector that only ever reports PASS is
    indistinguishable from one that does nothing, and the first run of the rebuilt
    check reported PASS on every document — it took this to show the logic was sound
    and the earlier blank result was a bad control (it had picked a table-page whose
    grid collapsed to one row)."""
    a, b = toks("alpha", 10), toks("bravo", 10)
    grid = {(0, 0): a, (0, 1): b}
    hits = _cells_inside(a + b, grid)
    assert sorted(hits) == [(0, 0), (0, 1)]
    assert _classify(hits) == "column_merge"


def test_two_source_cells_from_different_rows_is_a_row_merge():
    a, b = toks("alpha", 10), toks("bravo", 10)
    hits = _cells_inside(a + b, {(0, 0): a, (1, 0): b})
    assert _classify(hits) == "row_merge"


def test_an_output_cell_holding_exactly_one_source_cell_is_not_a_merge():
    a, b = toks("alpha", 10), toks("bravo", 10)
    assert _classify(_cells_inside(a, {(0, 0): a, (0, 1): b})) is None


def test_two_source_cells_with_the_SAME_text_are_not_a_merge():
    """Jersey p50 prints the whole table twice side by side, so one rendered cell
    legitimately matches both copies — two slots, same row, different columns, which
    reads as a column merge unless duplicate content is collapsed."""
    a = toks("alpha", 10)
    hits = _cells_inside(a, {(0, 0): list(a), (0, 3): list(a)})
    assert len(hits) == 1 and _classify(hits) is None


def test_a_source_cell_repeated_inside_a_larger_one_is_not_a_merge():
    """Jersey p65: question (g)'s cell contains the sentence "Does the analysis depend
    on the type of investor…" and so does the continuation cell at the top of the same
    page. Both matched the same span of the output cell. A real merge is cell A's words
    THEN cell B's, so the spans have to be disjoint."""
    shared = toks("shared", 12)
    body = toks("body", 8) + shared
    hits = _cells_inside(body, {(0, 0): body, (2, 0): shared})
    assert len(hits) == 1 and _classify(hits) is None


def test_a_tiny_source_cell_is_never_evidence():
    """"Yes", "No" and bare numbers recur all over these documents and would match
    inside any cell by accident."""
    a, tiny = toks("alpha", 10), ["yes"] * (MIN_PDF_CELL_TOKENS - 1)
    assert _classify(_cells_inside(a + tiny, {(0, 0): a, (0, 1): tiny})) is None


def test_a_merge_survives_the_rendering_differences_an_exact_match_would_not():
    """Requiring the source cell's whole run verbatim matched 17 of 905 cells on Jersey
    — one hyphenation or footnote marker breaks it — so the check passed having
    compared almost nothing."""
    a, b = toks("alpha", 20), toks("bravo", 20)
    rendered = a[:9] + ["footnote"] + a[10:] + b        # one token differs
    assert _classify(_cells_inside(rendered, {(0, 0): a, (0, 1): b})) == "column_merge"


# ---- helpers -----------------------------------------------------------------
def test_cell_text_is_normalised_to_bare_words():
    assert _norm_words("<b>Yes,</b> see &amp; answer at 8.1(a)") == [
        "yes", "see", "answer", "at", "8", "1", "a"]


def test_best_span_reports_where_the_source_cell_sits_in_the_output_cell():
    a, b = toks("alpha", 10), toks("bravo", 10)
    assert _best_span(a + b, b) == (10, 20)
    assert _best_span(a + b, a) == (0, 10)


def test_best_span_is_none_when_the_source_cell_is_not_there():
    assert _best_span(toks("alpha", 10), toks("zulu", 10)) is None
