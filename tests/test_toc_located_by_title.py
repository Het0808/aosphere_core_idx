"""A printed contents page is verified by FINDING its headings, not by trusting its page numbers.

The old check sent you to the page the contents page named and looked for the title
there, allowing one constant offset for cover sheets. That model fits a PDF whose
printed numbering is shifted by a fixed amount, and nothing else.

Portugal 176637 is the document it cannot describe. Its contents page was generated and
then had content inserted after it, so the gap between what it says and where things are
GROWS: 0 pages at clause 1, +2 by clause 4, +4 by 8.3, +7 by clause 11. No single offset
fits a moving target. The best available, +2, repaired the middle of the document and
broke both ends — it even broke entries 1-4, which were correct at 0 — verifying 27 of
47 and rejecting a 47-entry outline whose titles, nesting and order were all correct.

The page numbers are the one part of a contents page nothing downstream needs. Locating
each heading by searching forward from the previous one yields a BETTER page than the
printed one, and keeps the guard the offset search was really providing: a contents page
lifted from a different document still fails, because its titles are not in this one in
this order. Measured over 95 documents: acceptance 94/95 -> 95/95, no document lost.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from rescue_outline import _title_needles, locate_titles, verify  # noqa: E402


class FakeDoc:
    """Just enough of a fitz document for locate_titles/verify."""

    def __init__(self, pages: list[str]):
        self._pages = pages
        self.page_count = len(pages)

    def __getitem__(self, i):
        text = self._pages[i]
        return type("P", (), {"get_text": lambda self, *a, **k: text})()


# ---- needles -------------------------------------------------------------

def test_the_clause_label_is_kept_so_short_headings_stay_findable():
    """"Telephone" alone is body text on dozens of pages; "8.2 Telephone" is not."""
    needles = _title_needles("8.2 Telephone")
    assert needles[0] == "8 2 telephone"
    assert "telephone" in needles


def test_a_single_short_word_is_never_a_needle():
    """It would place the heading almost at random."""
    assert _title_needles("6 Other") == ["6 other"]
    assert all(len(n.split()) >= 2 or len(n) >= 8 for n in _title_needles("4.7 Sub-funds"))


def test_the_labelled_form_is_tried_before_the_bare_title():
    """Most distinctive first: the label pins the heading, the bare words may occur
    in prose. ("Non-Core" normalises to two words, hence the run below.)"""
    n = _title_needles("4.5 Provision of Non-Core Services by an AIFM")
    assert n[0] == "4 5 provision of non core services by"
    assert n.index("4 5 provision of non core services by") < n.index(
        "provision of non core services by an aifm")


# ---- locating ------------------------------------------------------------

def _doc():
    # contents page, then a body whose real pages drift further and further from it
    return FakeDoc([
        "cover",
        "PART B: CONTENTS  1. BACKGROUND 4  4. AIFMD 17  11. PENALTIES 104",
        "", "1. BACKGROUND intro",          # real page 4 (index 3)
        "", "", "", "", "", "", "", "",
        "", "", "", "4. AIFMD rules",       # real page 16
        "", "", "", "", "11. PENALTIES here",
    ])


def test_a_heading_is_found_where_it_IS_not_where_the_toc_says():
    doc = _doc()
    entries = [(1, "1. BACKGROUND", 4), (1, "4. AIFMD", 17), (1, "11. PENALTIES", 104)]
    located, found = locate_titles(doc, entries, skip_pages=[1])
    assert found == 3
    assert [p for _, _, p in located] == [4, 16, 21]


def test_the_contents_page_itself_is_excluded_from_the_search():
    """It lists every title, so searching it finds them all in one place — 47 of 47
    'located' on page 2, a 2.5% span, which is not a document outline."""
    doc = _doc()
    entries = [(1, "1. BACKGROUND", 4), (1, "4. AIFMD", 17), (1, "11. PENALTIES", 104)]
    _located, found = locate_titles(doc, entries, skip_pages=[])
    assert found == 3
    located_skipped, _ = locate_titles(doc, entries, skip_pages=[1])
    assert all(p != 2 for _, _, p in located_skipped)


def test_stale_page_numbers_no_longer_reject_a_good_outline():
    doc = _doc()
    entries = [(1, "1. BACKGROUND", 4), (1, "4. AIFMD", 17), (1, "11. PENALTIES", 104),
               (1, "1. BACKGROUND", 4)]
    v = verify(doc, entries[:3] + [entries[3]], skip_pages=[1])
    assert v["verified"] >= 3


# ---- the guard that must survive -----------------------------------------

def test_a_contents_page_from_a_different_document_is_still_rejected():
    """The whole point of verifying at all. Measured on the real corpus: Portugal's
    contents verifies 47/47 against Portugal and 25-31/47 against four siblings."""
    other = FakeDoc(["cover", "contents", "unrelated text about shipping law",
                     "more unrelated text", "nothing matching here", "still nothing",
                     "no headings of that kind", "different subject entirely",
                     "another page", "and another"])
    entries = [(1, "1. BACKGROUND", 4), (1, "4. AIFMD", 17),
               (1, "11. PENALTIES", 104), (1, "8.2 Telephone", 79)]
    v = verify(other, entries, skip_pages=[1])
    assert not v["ok"]
    assert any("found in the document" in r for r in v["reasons"])
