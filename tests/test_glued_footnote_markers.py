"""A footnote marker printed flush against its own body must not read as lost content.

These documents print the footnote area as "312Part 3, Section 16(1) DPA" — no space
between the superscript marker and the first word — and fitz reads the pair as ONE token,
'312part'. The tree writes the same footnote as "[^312]: Part 3, Section 16(1) DPA",
whose unit is ['part', '3', ...]. The glued first token defeats the unit match, so a
footnote block that is fully present in the output is reported as a silent gap.

Measured on 155_Data_Privacy/Cayman Islands (Data Privacy)__174081: 26 of its 38 dropped
spans, each moving from ratio 0.00 to 1.00 — exactly covered, not merely closer. Across
114 documents the sweep improved 9 and regressed 0 (254 -> 219 silent gaps).

The safety property that makes this legal is the GATE. Splitting every digit-glued token
was tried corpus-wide and reverted: it turned contents-page noise ("3Structure of Part B")
into well-formed runs and invented gaps in documents whose TOC page is correctly not
extracted. Splitting only when the digits name a footnote the tree DEFINES confines the
change to the one construct it was written for — and makes the check refuse to excuse a
marker whose body never reached the output, which is the case that must still fail.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_content_localized import (  # noqa: E402
    UNIT_THRESHOLD, _units_ratio, find_gaps, footnote_marker_numbers,
    unglue_footnote_markers,
)


def toks(s: str) -> list[str]:
    return s.split()


NOTE_BODY = toks("part 3 section 16 1 dpa")


def test_marker_numbers_are_read_from_definitions_only():
    """A DEFINITION is a line that starts with `[^n]:` — which is how the tree writes
    them. A bare `[^99]` reference, or a `[^n]:` appearing mid-sentence, is not one, so
    neither contributes a number that could license a split."""
    raws = ["[^312]: Part 3, Section 16(1) DPA\n[^7]: Section 24 DPA\n",
            "a reference to [^99] with no definition here\n",
            "prose mentioning [^55]: inline is not a definition\n"]
    assert footnote_marker_numbers(raws) == {"312", "7"}


def test_glued_marker_is_split_when_the_tree_defines_it():
    assert unglue_footnote_markers(["312part"], {"312"}) == ["312", "part"]


def test_glued_marker_is_left_alone_when_the_tree_does_not_define_it():
    """The 284 case: a marker whose body never reached the tree stays glued, so the
    unit cannot match and the span stays reported. This is the check refusing to
    excuse a real loss."""
    assert unglue_footnote_markers(["284https"], {"312"}) == ["284https"]


def test_contents_page_noise_is_never_split():
    """'3Structure of Part B' is the construct that broke the corpus-wide version.
    3 is not a footnote definition here, so nothing is split."""
    assert unglue_footnote_markers(["3structure", "of", "part", "b"], {"312"}) == \
        ["3structure", "of", "part", "b"]


def test_glued_span_is_excused_only_after_ungluing():
    span = ["312part", "3", "section", "16", "1", "dpa"]
    assert _units_ratio(span, [NOTE_BODY]) == 0.0
    unglued = unglue_footnote_markers(span, {"312"})
    assert _units_ratio(unglued, [NOTE_BODY]) >= UNIT_THRESHOLD


def test_marker_digits_stay_uncovered_so_the_ratio_under_excuses():
    """The marker token is kept, not dropped — a one-footnote span scores 6/7, not 1.0.
    Erring toward under-excusing is deliberate; UNIT_THRESHOLD leaves ample room."""
    unglued = unglue_footnote_markers(["312part", "3", "section", "16", "1", "dpa"], {"312"})
    assert _units_ratio(unglued, [NOTE_BODY]) == 6 / 7


def test_find_gaps_reclassifies_a_glued_footnote_block_as_restructured():
    pdf = toks("intro text here") + ["312part"] + toks("3 section 16 1 dpa") + toks("tail text")
    md = toks("intro text here tail text")
    dropped, _reloc, _elsewhere, restructured, _changed = find_gaps(
        pdf, md, min_gap=4, whole_tokens=md,
        note_units=[NOTE_BODY], note_nums={"312"})
    assert not dropped
    assert len(restructured) == 1
    assert restructured[0]["unit_kind"] == "footnote definition"


def test_find_gaps_still_reports_the_block_when_the_footnote_is_undefined():
    """Same span, but the tree defines no such footnote — it must stay dropped."""
    pdf = toks("intro text here") + ["312part"] + toks("3 section 16 1 dpa") + toks("tail text")
    md = toks("intro text here tail text")
    dropped, _reloc, _elsewhere, restructured, _changed = find_gaps(
        pdf, md, min_gap=4, whole_tokens=md,
        note_units=[NOTE_BODY], note_nums=set())
    assert len(dropped) == 1
    assert not restructured


def test_real_prose_loss_is_still_reported():
    """The ungluing must not become a general excuse: ordinary text with no footnote
    unit behind it stays a gap."""
    lost = toks("the controller shall notify the ombudsman without undue delay")
    pdf = toks("opening words") + lost + toks("closing words")
    md = toks("opening words closing words")
    dropped, _reloc, _elsewhere, restructured, _changed = find_gaps(
        pdf, md, min_gap=4, whole_tokens=md,
        note_units=[NOTE_BODY], note_nums={"312"})
    assert len(dropped) == 1
    assert not restructured


def test_ungluing_is_a_no_op_without_marker_numbers():
    span = ["312part", "3", "section"]
    assert unglue_footnote_markers(span, set()) == span
