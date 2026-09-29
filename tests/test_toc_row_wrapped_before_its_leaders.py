"""A printed-TOC title that wrapped BEFORE its leader run is one entry, not two.

The mirror image of tests/test_toc_row_with_no_title.py, and the half that was missing.
There the title is on the first line and the leader run wraps onto its own line; here the
TITLE is what wraps, so the leaders and the page number sit on the second line together
with the title's tail:

    7.4
    Sensitivities based on Investment Strategy (Investment Management & Advisory
    Services) ................................................................ 48

ENTRY matches only the last of those three lines, and its "title" is the bare tail
"Services)". That carries a name, so the leaders-only repair declines it; it carries no
label, so `_label_depth` defaulted it to 1 and the pre-flight wrote a TOP-LEVEL bookmark
called "Services)" into the repaired PDF — a section the document does not have, sitting
between 7.3 and 8, while the real 7.4 and 7.5 were never written at all.

What that cost, measured on the MRAM corpus (139 documents parsed, 88 affected): 173
unnumbered orphan entries, and 168 real numbered subsections missing.

Japan 172122 is where it became visible. Stage 1 LOCATED "Services)" in the body — page
45 carries the wrapped tail "…ADVISORY SERVICES)" of the real 7.3 heading — so it built
a top-level section from it, took pages 45-50 off section 7 and adopted 7.4/7.5 as its
children. Stage 5 then named the folder "00-services", because `section_prefix` finds no
number in its H1 and falls back to "00", colliding with front matter.

Bahamas 183503 produced the IDENTICAL two orphan entries and escaped only by luck: both
failed to locate in its body and were dropped, which is why its tree looked clean and
Japan's did not. Nothing about Bahamas was more correct — so this is pinned on both.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest  # noqa: E402

from rescue_outline import (_entries_from_lines, _wrapped_head,  # noqa: E402
                            parse_toc_layout)

DOTS = "." * 60

# Japan 172122's contents page, verbatim (fitz text layer, trailing spaces stripped).
JAPAN = [
    "7.2",
    "Sensitivities based on Structure and/or Investment Strategy (Funds) " + DOTS + " 39",
    "7.3",
    "Private Placement Regime (Investment Management & Advisory Services) " + DOTS + " 45",
    "7.4",
    "Sensitivities based on Investment Strategy (Investment Management & Advisory",
    "Services) " + DOTS + " 48",
    "7.5",
    "Investment Management & Advisory Services in respect of Single Investor Vehicles,",
    "Segregated Managed Accounts and Family Offices " + DOTS + " 49",
    "8.",
    "Marketing Activities " + DOTS + " 50",
]


def _titles(lines):
    return [t for _lvl, t, _pg in _entries_from_lines(lines)]


def test_the_wrapped_title_becomes_one_entry_with_its_label():
    titles = _titles(JAPAN)
    assert ("7.4 Sensitivities based on Investment Strategy "
            "(Investment Management & Advisory Services)") in titles
    assert ("7.5 Investment Management & Advisory Services in respect of Single Investor "
            "Vehicles, Segregated Managed Accounts and Family Offices") in titles


def test_the_orphan_tails_are_gone():
    """The defect itself. "Services)" as an entry is what became a whole section."""
    titles = _titles(JAPAN)
    assert "Services)" not in titles
    assert "Segregated Managed Accounts and Family Offices" not in titles


def test_the_joined_entry_keeps_the_page_the_leader_line_named():
    entries = {t: p for _l, t, p in _entries_from_lines(JAPAN)}
    assert entries[("7.4 Sensitivities based on Investment Strategy "
                    "(Investment Management & Advisory Services)")] == 48
    assert entries[("7.5 Investment Management & Advisory Services in respect of Single "
                    "Investor Vehicles, Segregated Managed Accounts and Family Offices")] == 49


def test_the_joined_entry_is_a_subsection_not_a_section():
    """A wrong LEVEL is a wrong chunk: at level 1 these become sections of their own,
    which is exactly how Japan's section 7 lost pages 45-50."""
    levels = {t: l for l, t, _p in _entries_from_lines(JAPAN)}
    assert levels[("7.4 Sensitivities based on Investment Strategy "
                   "(Investment Management & Advisory Services)")] == 2
    assert levels[("7.5 Investment Management & Advisory Services in respect of Single "
                   "Investor Vehicles, Segregated Managed Accounts and Family Offices")] == 2


def test_the_ordinary_two_line_rows_are_untouched():
    """label alone, then title + leaders + page — the shape most entries have. The join
    must not fire here, or every entry would swallow the one above it."""
    entries = [(l, t, p) for l, t, p in _entries_from_lines(JAPAN)]
    assert (2, "7.2 Sensitivities based on Structure and/or Investment Strategy (Funds)", 39) \
        in entries
    assert (2, "7.3 Private Placement Regime (Investment Management & Advisory Services)", 45) \
        in entries
    assert (1, "8 Marketing Activities", 50) in entries


def test_a_title_that_carries_its_own_label_is_never_joined():
    """A label IS the statement that a new entry starts here. Without this test the
    single-line form would glue itself onto whatever prose sat above it."""
    head, _label = _wrapped_head(["some stray line above"], "7.4 Sensitivities based on X")
    assert head == ""


def test_nothing_is_joined_when_a_label_sits_directly_above():
    head, _label = _wrapped_head(["7.4"], "Sensitivities based on X")
    assert head == "", "a label above means the ordinary two-line entry, not a wrap"


def test_page_furniture_is_not_joined_into_a_title():
    head, _label = _wrapped_head(["TABLE OF CONTENTS"], "Services)")
    assert head == ""


def test_the_walk_back_is_bounded():
    """A contents page whose every line fails ENTRY must not glue into one title."""
    head, _label = _wrapped_head([f"line {i}" for i in range(40)], "tail)")
    assert len(head.split(" line ")) <= 4, head


# ---------------------------------------------------------------- the geometry parser
# parse_toc_layout reads the same contents page by POSITION instead of by leader dots,
# and had the identical defect: a wrapped first line carries no right-margin page number,
# so it was skipped outright and the tail row became the entry. Four documents in the
# corpus are parsed by this engine (Guernsey 174507, Denmark 169459, EU Member States
# 111131, Namibia 172977) and every one of them produced the orphan entries.
#
# Geometry affords one guard the text parser cannot have: a title wraps BECAUSE it filled
# its column, so a held line must reach into the right half of the page. That is what
# keeps a short standalone heading above an entry from being glued onto it.

def _toc_pdf(rows):
    """A PDF whose page 0 has words where `rows` puts them: [(y, [(x, word)])].

    Deliberately MULTI-PAGE. `_is_running_header` asks whether a line appears on at
    least half the document's pages, so on a one-page fixture every line it is shown is
    a running header by definition and the parser correctly throws it away — a property
    of the fixture, not of the parser, and it silently made the join look broken.
    """
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    for y, words in rows:
        for x, word in words:
            page.insert_text((x, y), word, fontsize=9)
    for i in range(4):
        doc.new_page(width=595, height=842).insert_text((60, 100), f"body page {i} text")
    return doc


def test_the_geometry_parser_joins_a_wrapped_title():
    doc = _toc_pdf([
        (100, [(60, "7.4"), (100, "Sensitivities"), (170, "based"), (210, "on"),
               (230, "Investment"), (300, "Strategy"), (360, "(Investment"),
               (430, "Management"), (500, "&"), (515, "Advisory")]),
        (120, [(100, "Services)"), (170, "." * 40), (520, "48")]),
    ])
    ents = parse_toc_layout(doc, [0])
    titles = [t for _l, t, _p in ents]
    doc.close()
    assert "Services)" not in titles, titles
    assert any(t.startswith("7.4 Sensitivities") and t.endswith("Services)") for t in titles), \
        titles
    assert [p for _l, _t, p in ents] == [48]


def test_the_geometry_parser_leaves_a_short_heading_above_an_entry_alone():
    """The width guard. "ANNEXURES" is a heading, not the first line of the entry under
    it — Namibia 172977 prints exactly this shape above Annexure A."""
    doc = _toc_pdf([
        (100, [(60, "ANNEXURES")]),
        (120, [(60, "Annexure"), (110, "A"), (170, "." * 40), (520, "70")]),
    ])
    ents = parse_toc_layout(doc, [0])
    titles = [t for _l, t, _p in ents]
    doc.close()
    assert titles == ["Annexure A"], titles


def test_the_geometry_parser_still_reads_an_ordinary_row():
    doc = _toc_pdf([
        (100, [(60, "7.3"), (100, "Private"), (150, "Placement"), (220, "Regime"),
               (280, "." * 40), (520, "45")]),
    ])
    ents = parse_toc_layout(doc, [0])
    doc.close()
    assert [(t, p) for _l, t, p in ents] == [("7.3 Private Placement Regime", 45)]
