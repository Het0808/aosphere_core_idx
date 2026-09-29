"""Restoring the spaces MinerU drops inside a table cell.

MinerU's `hybrid` and `vlm` backends never read a table's text. They hand the table
IMAGE to a VLM and take back whatever HTML it returns -- hybrid_magic_model does
`span["html"] = block_content` with no whitespace handling at all -- so a cell's lines
arrive end to end with NO separator between them:

    Such tear sheets may include:        ->  ...may include:A brief description of
    *  A brief description of strategy       strategySummary statisticsTermsService
    *  Summary statistics                    providers
    *  Terms

The same happens at every bold or italic run: "wording in " + "Section D" + " of each"
comes back as "wording inSection Dof each". Measured across this corpus, 3,666 words
glued this way in 115 of 144 documents -- and that counts only the collisions a capital
letter makes visible; a lower-to-lower join leaves no trace to search for.

This is MinerU working as built, not a misconfiguration: its own Gradio app produces the
identical glue on the identical document, and only the `pipeline` backend -- a different
table model entirely -- joins cell fragments with a space.

So the spacing is recovered from the one source that still holds it: the crop PDF's text
layer. MinerU already prefers that layer over the model for TEXT blocks when OCR is off
(`not_extract_list`); tables are simply not on that list. The cell's own text is never
replaced, only spaced, so a bad alignment can never import the wrong words.

Four guards, each of which corrected a real regression found on the corpus:

  hyphen        "Closed-" / "Ended" over a line break is ONE word (ADGM 170680 p13)
  space-only    a narrow column wraps mid-word with a bare newline and no space, and
                reading that as a separator gives "environment al" (Bahamas 183503 p31)
  adjacency     characters paired across a jump in a repetitive page gave "h owe ver"
                (Australia 181814 p94)
  entities      an insertion inside `&quot;` reaches the reader as literal "&quo t;"
                (ADGM 170680 p4)
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hybrid_extract as he  # noqa: E402


def _cell(inner):
    return f"<table><tr><td>{inner}</td></tr></table>"


def _text(html):
    return re.sub(r"<[^>]+>", "", html)


def _flat(html):
    """Cell text with a recovered line break read as the space it stands in for."""
    return _text(html).replace(he._BREAK, " ")


# ---------------------------------------------------------------- the defect itself

def test_lines_run_together_in_a_cell_are_separated():
    """The bulleted-cell case, verbatim from ADGM 170680 page 50."""
    page = ("Such tear sheets may include: \n• \nA brief description of strategy \n"
            "• \nSummary statistics \n• \nTerms \n• \nService providers \n")
    glued = ("Such tear sheets may include:A brief description of strategySummary "
             "statisticsTermsService providers")
    fixed = he.realign_table_spacing(_cell(glued), page)
    assert _flat(fixed) == ("Such tear sheets may include: A brief description of strategy "
                            "Summary statistics Terms Service providers")
    # and each of those four joins was a LINE in the PDF, not a space
    assert _text(fixed).count(he._BREAK) == 4


def test_a_bold_run_does_not_swallow_the_spaces_around_it():
    """ADGM 170680 page 26: the PDF has three spans and the spaces are plainly in it,
    but the VLM returns them welded together."""
    page = "If so, please provide recommended wording in Section D of each of \n"
    fixed = he.realign_table_spacing(
        _cell("recommended wording in<b>Section D</b>of each of"), page)
    assert _text(fixed) == "recommended wording in Section D of each of"
    assert he._BREAK not in fixed          # same line in the PDF -> a space, not a break


def test_the_tags_themselves_survive():
    """clean_mineru_text still has to see <sup> to turn it into a footnote marker, so
    the cell is spaced in place rather than flattened to text."""
    fixed = he.realign_table_spacing(
        _cell("the Fund<sup>12</sup>isregistered"), "the Fund12 is registered \n")
    assert "<sup>12</sup>" in fixed
    assert "is registered" in _text(fixed)


@pytest.mark.parametrize("glued,page,expect", [
    ("on across-border basis", "on a cross-border basis \n", "on a cross-border basis"),
    ("dealt within section 6", "dealt with in section 6 \n", "dealt with in section 6"),
    ("20% stake (director through", "20% stake (direct or through \n",
     "20% stake (direct or through"),
])
def test_lower_to_lower_collisions_are_repaired_too(glued, page, expect):
    """The camelCase cases are merely the visible ones. These three are real corpus
    cells (Cayman 183333, ADGM 170680, Australia 172274) where the glue leaves a word
    that reads as ordinary English -- no scan could find them, and only the PDF says."""
    assert _text(he.realign_table_spacing(_cell(glued), page)) == expect


# ---------------------------------------------------------------- the four guards

def test_a_word_hyphenated_over_a_line_break_stays_one_word():
    """ADGM 170680 p13. MinerU rejoined "Closed-" / "Ended" correctly and the line
    break must not be read as evidence to break it apart again."""
    page = "apply to both Open-Ended and Closed- \nEnded Funds? \n"
    out = _text(he.realign_table_spacing(
        _cell("apply to bothOpen-EndedandClosed-Ended Funds?"), page))
    assert "Closed-Ended" in out
    assert "Closed- Ended" not in out
    assert out.startswith("apply to both Open-Ended and Closed-Ended")


def test_a_bare_newline_inside_a_word_is_not_a_separator():
    """Bahamas 183503 p31 holds a literal "environment\\nal, social": a narrow column
    wrapping mid-word, with NO space. Only a space is evidence of a word boundary."""
    page = "As above. \nenvironment\nal, social \nand \n"
    out = _text(he.realign_table_spacing(_cell("As above.environmental, social and"), page))
    assert "environmental" in out
    assert "environment al" not in out


def test_characters_paired_across_a_jump_insert_nothing():
    """Australia 181814 p94 read "N o, h owe ver" once: on a page whose wording repeats,
    the matcher can pair a character with a distant twin, and the whitespace between
    those belongs to neither. Adjacency in the PDF is required, not just a match."""
    page = ("No, however discussions which refer to particular characteristics \n"
            "No, however discussions which refer to particular characteristics \n")
    out = _text(he.realign_table_spacing(
        _cell("No, however discussions which refer"), page))
    assert out == "No, however discussions which refer"


def test_an_html_entity_is_never_split():
    """ADGM 170680 p4 produced `&quo t;`, which reaches the reader as literal text."""
    fixed = he.realign_table_spacing(
        _cell("a &quot;reverse-enquiry&quot;or&quot;reverse-solicitation&quot;"),
        'a "reverse-enquiry" or "reverse-solicitation" \n')
    assert "&quo t;" not in fixed
    assert fixed.count("&quot;") == 4
    assert "&quot;or&quot;" not in fixed        # the real join still gets its spaces


# ---------------------------------------------------------------- refusing to guess

def test_a_cell_that_does_not_align_is_left_exactly_as_it_was():
    """The pass only ever adds a space the PDF vouches for. Text that is not on this
    page has nothing vouching for it, so it is returned untouched rather than guessed
    at -- which is what makes a wrong alignment harmless."""
    glued = "somethingEntirelyDifferentFromThisPage"
    assert he.realign_table_spacing(_cell(glued), "wholly unrelated wording \n") == _cell(glued)


@pytest.mark.parametrize("html,page", [
    ("", "some page text"),
    ("<table><tr><td>abc</td></tr></table>", ""),
    ("<table><tr><td>abc</td></tr></table>", "   \n  \n"),
])
def test_nothing_to_work_with_is_not_an_error(html, page):
    """A scanned page has no text layer; stage 2 must not fail because of it."""
    assert he.realign_table_spacing(html, page) == html


def test_a_cell_already_spaced_is_not_touched():
    page = "Such tear sheets may include: \nA brief description of strategy \n"
    html = _cell("Such tear sheets may include: A brief description of strategy")
    assert he.realign_table_spacing(html, page) == html


def test_a_cell_split_across_a_page_break_takes_the_candidate_that_covers_it():
    """content_list MERGES a page-spanning table into one block, so a cell can straddle
    the boundary: Argentina 172099 table_005 ends page 6 with "may apply" and opens page
    7 with "The Manager". The anchor page alone clears the coverage bar while missing the
    join itself, so the widest candidate that accounts for the cell has to win, not the
    first one that passes."""
    page6 = "for details of when this exception may apply \n"
    page7 = "The Manager has voting discretion in this scenario \n"
    glued = ("for details of when this exception may applyThe Manager has voting "
             "discretion in this scenario")
    # anchor page first, then the anchor joined with its neighbour — as run_stage2 builds it
    out = _flat(he.realign_table_spacing(_cell(glued), [page6, page6 + "\n" + page7]))
    assert "may apply The Manager" in out


def test_a_candidate_that_covers_nothing_cannot_win_over_one_that_does():
    """Widening must not let an unrelated page take the cell: the best coverage wins."""
    unrelated = "wholly unrelated wording that shares nothing with the cell \n"
    real = "Exchanges/market operators \nCrypto firms \n"
    out = _flat(he.realign_table_spacing(
        _cell("Exchanges/market operatorsCrypto firms"), [unrelated, real]))
    assert out == "Exchanges/market operators Crypto firms"


def test_every_cell_in_a_table_is_repaired_not_just_the_first():
    page = "Which sub-industries? \nExchanges/market operators \nCrypto firms \n"
    html = ("<table><tr><td>Which sub-industries?</td>"
            "<td>Exchanges/market operatorsCrypto firms</td></tr></table>")
    out = _flat(he.realign_table_spacing(html, page))
    assert "operators Crypto firms" in out


# ------------------------------------------------- the break, from cell to rendered row

def _row(inner):
    return f"<table><tr><td>{inner}</td></tr></table>"


def test_a_recovered_break_renders_as_a_line_break():
    """End to end: PDF lines -> marker -> <br> in the row the viewer and stage 3 read."""
    page = "Exchanges/market operators \nCrypto firms \n"
    fixed = he.realign_table_spacing(_row("Exchanges/market operatorsCrypto firms"), page)
    html, _, _ = he.stitch_table_html([fixed])
    assert "operators<br>Crypto firms" in html
    assert he._BREAK not in html          # the marker itself never reaches a file


def test_a_space_recovered_on_one_line_stays_a_space():
    page = "recommended wording in Section D of each \n"
    fixed = he.realign_table_spacing(_row("recommended wording in<b>Section D</b>of each"), page)
    html, _, _ = he.stitch_table_html([fixed])
    assert "wording in Section D of each" in html
    assert "<br>" not in html


def test_the_break_never_reaches_the_text_the_stitcher_reasons_about():
    """The whole safety argument for restoring structure: `text` is byte-identical to
    what it was before, so _row_key, _looks_truncated and _alignment_score -- which
    decide how a multi-page table joins -- cannot see any difference."""
    from lxml import etree
    page = "Exchanges/market operators \nCrypto firms \n"
    fixed = he.realign_table_spacing(_row("Exchanges/market operatorsCrypto firms"), page)
    tr = etree.fromstring(fixed).iter("tr").__next__()
    cell = he._tr_to_cells(tr)[0]
    assert cell["text"] == "Exchanges/market operators Crypto firms"
    assert he._BREAK not in cell["text"]
    assert he._BREAK in cell["rich"]


def test_a_table_with_no_breaks_renders_exactly_as_before():
    """A cell the pass never touched must serialise identically -- no stray markup."""
    plain = "<table><tr><td>already fine</td><td>so is this</td></tr></table>"
    html, _, _ = he.stitch_table_html([plain])
    assert "<br>" not in html
    assert "already fine" in html and "so is this" in html


def test_a_page_break_merge_does_not_repeat_the_cell_it_absorbs():
    """The join that carries a value wrapped across a page-crop boundary rewrites `text`
    and `rich` together, and they must stay in step.

    Honest note on scope: `rich` is read before the text merge because a cell without a
    `rich` of its own falls back to its `text`, and taking that fallback afterwards would
    read the already-merged string and repeat the donor. I could NOT construct an input
    that reaches it -- every cell _tr_to_cells builds carries a `rich`, and the padding
    cells that do not are empty -- so this pins the invariant rather than reproducing a
    known failure. Ordering it the safe way costs nothing."""
    first = '<table><tr><td>Registered office:</td><td>Tervurenlaan 268A</td></tr></table>'
    # second block opens with a blank first cell: the continuation shape stitch_table_html
    # merges into the row above
    second = '<table><tr><td></td><td>1150 Brussels</td></tr></table>'
    html, _, _ = he.stitch_table_html([first, second])
    assert html.count("1150 Brussels") == 1
    assert html.count("Tervurenlaan 268A") == 1
