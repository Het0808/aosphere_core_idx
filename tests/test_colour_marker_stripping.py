"""The traffic-light squares are graphics, so the marker standing in for them is
a token the PDF can never supply.

These memos answer each question with a coloured square — green permitted, amber
workable, red not permitted. It is drawn, not written: the PDF's text layer holds
nothing where it is printed. summary_ai_extract therefore asks the model to emit
`[green]`/`[amber]`/`[red]` in its place, which is the right output and the wrong
thing to compare against the source.

Left in the token stream it does more than add a word. It lands immediately after
the heading, at the head of the section's first paragraph, and the two measures that
decide whether a span is really missing — decomposed_coverage and relocation_ratio —
both work in CONTIGUOUS RUNS. One phantom token there cuts the section's body in two,
so text reproduced word for word scores as two short runs instead of one long one and
is reported as a gap.

Measured on 124_Marketing_Restrictions/India__34712: "Pre-Marketing of Funds" is
reproduced verbatim in its own file and was still reported absent, because `red` sat
between "funds" and "there". 231 files across 31 of the 43 AI-processed documents
carry at least one marker.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from lib_content_compare import clean_markdown, tokenize  # noqa: E402


def test_the_colour_markers_are_not_source_text():
    md = "# Pre-Marketing of Funds\n\n[red] There is no route available.\n"
    assert "red" not in tokenize(clean_markdown(md))


def test_every_colour_is_stripped():
    md = "[green] permitted\n\n[amber] workable\n\n[red] not permitted\n"
    assert tokenize(clean_markdown(md)) == [
        "permitted", "workable", "not", "permitted",
    ]


def test_stripping_a_marker_leaves_the_body_contiguous_with_its_heading():
    """The point of the fix: the run either side of the marker becomes one run."""
    md = "# Pre-Marketing of Funds\n\n[red] There is no route available.\n"
    assert tokenize(clean_markdown(md)) == [
        "pre-marketing", "of", "funds", "there", "is", "no", "route", "available",
    ]


def test_the_colour_words_survive_as_ordinary_prose():
    """Only the bracketed marker goes. A document that discusses a red flag, or
    prints a bracketed word that is not one of the three, keeps its words."""
    assert "red" in tokenize(clean_markdown("the red tape is [blue] and amber"))
    assert "blue" in tokenize(clean_markdown("the red tape is [blue] and amber"))
    assert "amber" in tokenize(clean_markdown("the red tape is [blue] and amber"))


def test_a_marker_does_not_glue_the_words_either_side_of_it():
    """Substituted with a SPACE, not with nothing — "funds[red]There" must not
    become one token."""
    assert tokenize(clean_markdown("funds[red]There")) == ["funds", "there"]
