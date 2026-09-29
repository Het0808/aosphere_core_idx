"""Stage 1 carries the PDF's own bold/italic runs into the markdown.

Bold is not decoration in this corpus: it marks defined-term introductions and clause
cross-references. Bahamas 183503's glossary prints "Active Marketing Marketing
activities which extend beyond Passive Marketing" as bold term + plain definition, and
before this the two arrived as one run-on sentence with the boundary gone.

Every case below is a shape measured in the corpus, not an invented one. The two that
matter most are the ones that produce BROKEN markdown rather than merely plain text:

  - a styled run continuing across a line break (585 in UK 179582, 64 in Australia
    181814). Serialising per line closes the markers at the line end and emits
    "**wording in **" + "**Section D**" -- visible asterisks.
  - two styled groups ABUTTING with no space (Bahamas 183503 p6: italic "Dealing in ",
    bold-italic "Capital Markets Instruments", italic ":"), which runs the delimiters
    together into "****:*" -- four asterisks, rendered literally.
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "pdf2mdtree", Path(__file__).resolve().parents[1] / "scripts" / "pdf2mdtree.py")
p2m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p2m)

B = (True, False)      # bold
I = (False, True)      # italic
BI = (True, True)      # bold + italic
N = (False, False)     # unstyled


def run(text, style=N):
    return (text, style[0], style[1])


def test_trailing_space_inside_the_styled_run_stays_outside_the_markers():
    """Spans routinely carry their trailing space INSIDE the styled run. CommonMark
    needs the delimiter against a non-space character, so "**Collateral giver **"
    renders as literal asterisks."""
    assert p2m.emphasise([run("Collateral giver ", B)]) == "**Collateral giver** "


def test_run_continuing_across_a_line_break_emits_one_run():
    """The ' ' emit() inserts between two lines is unstyled and would otherwise split
    one bold run into two groups."""
    got = p2m.emphasise([run("wording in "), run("Section D", B), run(" "),
                         run("of each")])
    assert got == "wording in **Section D** of each"


def test_run_spanning_three_lines_still_merges():
    got = p2m.emphasise([run("one ", B), run(" "), run("two ", B), run(" "),
                         run("three", B)])
    assert got == "**one  two  three**"


def test_abutting_styled_groups_collapse_to_their_shared_style():
    """Bahamas 183503 p6. Nested emphasis ("*a **b** c*") would express it exactly but
    needs a render tree; the shared style is always representable and never claims a
    style the source does not have."""
    got = p2m.emphasise([run("Dealing in ", I), run("Capital Markets Instruments", BI),
                         run(":", I)])
    assert got == "*Dealing in Capital Markets Instruments:*"
    assert "****" not in got


def test_abutting_groups_sharing_no_style_fall_back_to_plain():
    assert p2m.emphasise([run("a", B), run("b", I)]) == "ab"


def test_groups_separated_by_a_space_each_keep_their_own_style():
    """The collapse above must not fire when the delimiters cannot collide."""
    assert p2m.emphasise([run("a", B), run(" "), run("b", I)]) == "**a** *b*"


def test_defined_term_glued_to_its_definition_is_still_valid():
    """Bahamas prints "Accredited Investor(s):" bold with no space before the
    definition. A closing delimiter against a word character is legal."""
    got = p2m.emphasise([run("Accredited Investor(s):", B), run("This definition")])
    assert got == "**Accredited Investor(s):**This definition"


def test_footnote_reference_is_never_emphasised():
    """The [^n] marker is ours, not the document's."""
    assert p2m.emphasise([run("business", B), run("[^17]")]) == "**business**[^17]"


def test_bare_bullet_glyph_is_left_unstyled():
    """emit()'s leading-glyph rewrite has to still recognise the bullet, which it
    cannot do through a pair of asterisks."""
    assert p2m.emphasise([run("•", B), run(" item")]) == "• item"


def test_text_already_containing_an_asterisk_is_left_unstyled():
    assert p2m.emphasise([run("note*", B)]) == "note*"


def test_whitespace_only_styled_run_emits_no_markers():
    assert p2m.emphasise([run(" ", B), run("x")]) == " x"


def test_bold_and_italic_together():
    assert p2m.emphasise([run("very", BI)]) == "***very***"


def test_unstyled_paragraph_is_returned_unchanged():
    assert p2m.emphasise([run("nothing styled here")]) == "nothing styled here"


def test_emphasis_never_changes_the_alphanumeric_token_sequence():
    """The invariant that makes this safe downstream: every text comparison in the QA
    stack is punctuation-insensitive (lib_content_compare.TOKEN_RE, check_scorecard's
    norm(), hybrid_extract's verbatim shingles, and the .split() word counts), so
    markers are invisible to all of them only while the token sequence is untouched.
    emit() asserts this per paragraph and falls back to the plain join if it fails."""
    runs = [run("the Data Protection Act 2018"), run("3"), run(" ("), run("DPA", B),
            run("). See "), run("D1 Privacy notice", B), run(" and "),
            run("Re Smith", I), run(".")]
    md = p2m.emphasise(runs)
    plain = "".join(r[0] for r in runs)
    assert p2m._ALNUM_RUN_RE.findall(md) == p2m._ALNUM_RUN_RE.findall(plain)
    assert "**DPA**" in md and "*Re Smith*" in md
