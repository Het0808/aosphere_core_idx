"""A section is charged for its OWN body, not for everything on its pages.

Sections share pages. A file declaring "page 16-27" sits on a page that also carries
the previous section's last answer and the next section's heading, and diffing the file
against that whole PAGE RANGE bills it for every word of its neighbours' text — a gap
it can never close no matter how well it was extracted.

On the Jersey MRAM memo that produced four of six flagged sections, each a 4-7 token
span stitched out of adjacent heading text:

    05-section-intentionally-left-blank.md   "passive marketing reverse-enquiry questions answers"
    06-passive-marketing-reverse-enquiry.md  "section intentionally left blank passive marketing reverse-enquiry"

05 is a file whose entire content is "# 4 [SECTION INTENTIONALLY LEFT BLANK]" — an
empty section cannot drop a body, and the words it was accused of losing are the NEXT
section's title. None of the existing excusals can rescue spans like these: they are
assembled from two neighbours at once, so no single contiguous run covers enough of
them, and dropping the run floor far enough to reach them would excuse real losses
instead (measured: a genuine 41-token dropped answer in the same document reaches 0.88
coverage at a 2-token floor).

_own_source_slice already made this argument for the hollow-section test. It applies
to the gap diff too, and the only reason it did not hold there was that headings could
not be located: the clause NUMBER is set in a narrow left column, so reading order can
put "4." nowhere near its title, and the number is stripped as boilerplate besides (a
bare-number line recurs on all 92 pages as the page number). Anchoring on the tree's
"# 4 [SECTION INTENTIONALLY LEFT BLANK]" failed on 10 of this document's 16 headings.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_content_localized import (  # noqa: E402
    _locate_title, _own_source_slice, _title_len, _title_needles,
)


def toks(s: str) -> list[str]:
    return s.split()


# ---- locating a heading whose number the layout detached ------------------

def test_the_title_words_are_tried_without_the_number():
    assert _title_needles(toks("4 section intentionally left blank")) == [
        toks("4 section intentionally left blank"),
        toks("section intentionally left blank"),
    ]


def test_a_numbered_title_is_found_when_the_number_is_absent():
    hay = toks("jerseyfsc org section intentionally left blank passive marketing")
    assert _locate_title(hay, toks("4 section intentionally left blank")) == 2


def test_the_number_is_preferred_when_it_IS_inline():
    hay = toks("of cobo 9 section intentionally left blank 10 licence questions")
    # anchors on the numbered form, so the returned position is the number's
    assert _locate_title(hay, toks("9 section intentionally left blank")) == 2


def test_a_single_remaining_word_is_NOT_anchored():
    """The floor. A 1-token needle matches inside ordinary prose and would cut a
    section at the wrong place — worse than not cutting it at all, because a
    boundary set too late HIDES loss rather than inventing it."""
    assert _title_needles(toks("1 background")) == [toks("1 background")]
    assert _locate_title(toks("the background to this memorandum"), toks("1 background")) is None


def test_a_dotted_number_counts_as_a_number():
    assert _title_needles(toks("8.3 marketing activities"))[-1] == toks("marketing activities")


# ---- cutting the section at its own boundaries ---------------------------

def _entry(title: str, abs_pos: int = 0) -> dict:
    return {"pages": (1, 1), "title_tokens": toks(title), "abs_pos": abs_pos}


def test_the_slice_starts_after_its_own_heading_and_stops_at_the_next():
    pdf = toks("tail of the previous answer "
               "5 passive marketing reverse-enquiry "
               "this section's own body text "
               "6 marketing selling to the public "
               "the next section's body")
    got = _own_source_slice(pdf, _entry("5 passive marketing reverse-enquiry"),
                            toks("6 marketing selling to the public"))
    assert got == toks("this section's own body text")


def test_a_neighbours_heading_is_not_this_sections_gap():
    """The 05/06 false positive, in miniature: an empty section is bounded to
    nothing, so the next title cannot be charged to it."""
    pdf = toks("4 section intentionally left blank "
               "5 passive marketing reverse-enquiry questions answers")
    got = _own_source_slice(pdf, _entry("4 section intentionally left blank"),
                            toks("5 passive marketing reverse-enquiry"))
    assert got == []


def test_the_boundary_holds_when_the_numbers_are_detached():
    """Same shape, but with the clause numbers stripped out of the page text —
    which is how these PDFs actually tokenise."""
    pdf = toks("section intentionally left blank "
               "passive marketing reverse-enquiry questions answers")
    got = _own_source_slice(pdf, _entry("4 section intentionally left blank"),
                            toks("5 passive marketing reverse-enquiry"))
    assert got == []


def test_content_inside_the_section_is_still_compared():
    """The floor that matters: cutting at headings must not soften a real gap.
    This is the Jersey page-76 loss — two answers that fall at the very END of
    clause 8, on the page where clause 9's heading begins."""
    pdf = toks("8 marketing activities "
               "c please flag any practical considerations "
               "in both public and private placement scenarios an ofi should ensure "
               "9 section intentionally left blank 10 licence")
    got = _own_source_slice(pdf, _entry("8 marketing activities"),
                            toks("9 section intentionally left blank"))
    assert got == toks("c please flag any practical considerations "
                       "in both public and private placement scenarios an ofi should ensure")


def test_no_slice_when_the_heading_cannot_be_found_at_all():
    """A synthetic container title ("front matter") is in the tree and nowhere in
    the PDF. No slice, no change in behaviour — the page-range chunk stays the
    fallback rather than a guess."""
    assert _own_source_slice(toks("some body text"),
                             {"pages": (1, 1), "title_tokens": toks("front matter"),
                              "abs_pos": None}, None) is None


# ---- locating a heading the text layer spelled with spaces inside a word ---
# 124_Marketing_Restrictions/India__34712 page 4 reads "Pre -Market ing of Funds":
# the headings are kerned per character and the extractor reports exactly that. The
# tree holds "# Pre-Marketing of Funds", so no token-for-token scan matches, the file
# never enters document order, and the section BEFORE it is cut at the wrong boundary
# and billed for every word of it. See _glyphs.

def test_a_heading_the_pdf_split_mid_word_is_still_located():
    hay = toks("applies pre market ing of funds there is no route available")
    assert _locate_title(hay, toks("pre-marketing of funds")) == 1


def test_the_loose_match_reports_the_extent_it_consumed_not_the_title_length():
    """The body starts after the heading, and on the page the heading is five
    tokens even though the title has three."""
    hay = toks("applies pre market ing of funds there is no route")
    assert _title_len(hay, toks("pre-marketing of funds"), 1) == 5


def test_a_section_split_mid_word_is_cut_at_its_own_boundary():
    pdf = toks("pre market ing of funds "
               "there is no route available enabling a firm to avoid "
               "active marketing selling of funds")
    got = _own_source_slice(pdf, _entry("pre-marketing of funds"),
                            toks("active marketing selling of funds"))
    assert got == toks("there is no route available enabling a firm to avoid")


def test_the_exact_match_still_wins_over_the_loose_one():
    """The loose scan runs only when nothing spelled the heading exactly, so a
    document that prints the words normally is untouched by it."""
    hay = toks("premarketing of funds intro pre-marketing of funds body")
    assert _locate_title(hay, toks("pre-marketing of funds")) == 4


def test_letters_must_match_exactly_not_merely_start_the_same():
    assert _locate_title(toks("pre market ingenious of funds"),
                         toks("pre-marketing of funds")) is None
    assert _locate_title(toks("pre market ing of fundraising"),
                         toks("pre-marketing of funds")) is None
