"""A printed-TOC row that carries no name is a wrapped title, not a section called ".".

A contents page prints "Title ......... 12". When the title is too long for its line it
wraps, and the leader run with the page number is left on a line of its own. That second
line is the one that matches: its "title" is a single dot.

Finland 138417 prints section 3 as three lines --

    3.
    SPECIFIC ISSUES RELATING TO CROSS-BORDER MARKETING/SELLING OF ELTIFS
     ..................................................................... 7

-- so the pre-flight wrote a bookmark literally called "." into the repaired PDF, and
threw the real title away. That bookmark is a section the tree can never build: counted
as promised, then as missing. On this product's flat 4-section build that is 25 points,
which is why Finland, France 138522, Hungary 139811, Netherlands 146753 and Norway
179259 all scored exactly 75.0, and why Greece 138436 fell from 86.2 to 75.0 once its
completeness improved enough for its Stage 1 tree to be adopted.

The wrapped line is the name, and the label is on the line before it. All six documents
now locate 6 of 6 titles instead of 5 of 6.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from rescue_outline import _entries_from_lines, _has_title_text, parse_toc_text  # noqa: E402

CORPUS = Path(__file__).resolve().parent.parent / "out" / "corpus"
MRAM = CORPUS / "124_Marketing_Restrictions_-_Asset_Management"

# Finland 138417's contents page, verbatim.
FINLAND = [
    "Clause", "Page",
    "1.", "BACKGROUND " + "." * 60 + " 3",
    "1.1", "Introduction " + "." * 60 + " 3",
    "1.2", "Definitions" + "." * 60 + " 3",
    "2.", "GUIDELINES FOR COMPLETING THIS SUPPLEMENTAL MEMORANDUM " + "." * 20 + " 6",
    "3.", "SPECIFIC ISSUES RELATING TO CROSS-BORDER MARKETING/SELLING OF ELTIFS",
    " " + "." * 100 + " 7",
    "Appendix", "1.", "Disclaimers " + "." * 60 + " 15",
]


def test_a_wrapped_title_is_recovered_from_the_line_above():
    entries = _entries_from_lines(FINLAND)
    titles = [t for _lvl, t, _pg in entries]
    assert "3 SPECIFIC ISSUES RELATING TO CROSS-BORDER MARKETING/SELLING OF ELTIFS" in titles
    assert ("3 SPECIFIC ISSUES RELATING TO CROSS-BORDER MARKETING/SELLING OF ELTIFS", 7) \
        in [(t, p) for _l, t, p in entries], "and it keeps the page the leader line named"


def test_no_entry_is_ever_named_by_punctuation_alone():
    """The defect itself: a bookmark called "." that no tree can build."""
    for _lvl, title, _pg in _entries_from_lines(FINLAND):
        assert _has_title_text(title), f"entry named {title!r} carries no name"


def test_the_ordinary_rows_are_untouched():
    entries = _entries_from_lines(FINLAND)
    assert [(l, t, p) for l, t, p in entries][:4] == [
        (1, "1 BACKGROUND", 3),
        (2, "1.1 Introduction", 3),
        (2, "1.2 Definitions", 3),
        (1, "2 GUIDELINES FOR COMPLETING THIS SUPPLEMENTAL MEMORANDUM", 6),
    ]
    assert len(entries) == 6


def test_a_nameless_row_with_nothing_above_it_is_dropped():
    """No name, and no wrapped line to borrow one from: whatever it is, it is not a
    section — and an entry that cannot be named cannot be located either."""
    lines = ["1.", "BACKGROUND " + "." * 60 + " 3",
             "." * 80 + " 4",                      # nothing above but an entry row
             "2.", "SCOPE " + "." * 60 + " 5"]
    titles = [t for _l, t, _p in _entries_from_lines(lines)]
    assert titles == ["1 BACKGROUND", "2 SCOPE"]


def test_a_running_header_is_not_borrowed_as_a_title():
    """The line above a nameless row is only a name if it is not the page furniture.
    "CONFIDENTIAL" sits atop every page of these memoranda and is the line directly
    above the contents rows, so without this the dot row would be renamed after it."""
    import fitz
    doc = fitz.open()
    for i in range(6):                     # a header on every page is what makes it one
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 40), "CONFIDENTIAL", fontsize=9)
        page.insert_text((72, 200), f"Body text for page {i + 1}.", fontsize=10)
    lines = ["CONFIDENTIAL", "." * 80 + " 7"]
    assert _entries_from_lines(lines, doc=doc) == []


@pytest.mark.skipif(not MRAM.exists(), reason="corpus not present")
@pytest.mark.parametrize("label", ["Finland__138417", "France__138522", "Hungary__139811",
                                   "Netherlands__146753", "Norway__179259", "Greece__138436"])
def test_every_pinned_document_now_names_all_six_of_its_sections(label):
    import fitz
    from rescue_outline import find_toc_pages, verify
    job = MRAM / label
    if not (job / "source.pdf").exists():
        pytest.skip(f"{label} not extracted here")
    doc = fitz.open(str(job / "source.pdf"))
    pages = find_toc_pages(doc)
    entries = parse_toc_text(doc, pages)
    assert all(_has_title_text(t) for _l, t, _p in entries)
    chk = verify(doc, entries, skip_pages=pages)
    assert chk["entries"] == 6
    assert chk["verified"] == 6, "was 5 of 6 — the wrapped title was the one lost"
    assert chk["ok"] is True
