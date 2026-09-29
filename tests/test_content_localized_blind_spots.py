"""Content a file holds from OUTSIDE its own heading slice is never checked.

MEASURED, on 124_Marketing_Restrictions/Bahamas-sonnet-nocounts-08-09-0524: deleting a
948-character paragraph from 08-private-placement-regime.md leaves every number the
check produces for that file byte-identical — dropped 0, relocated 4/66 tokens,
present_elsewhere 0, restructured 0, kept_tokens 3762, absent_tokens 110, before and
after. The paragraph's closing sentence appears nowhere in the tree afterwards, so this
is real loss reported as nothing at all.

WHY. compute_content_localized cuts the PDF side to each section's own slice — its
heading up to the heading that genuinely follows it — rather than to its declared page
range (see the `gap_slice` assignment). That cut is correct and load-bearing: a page
range also covers a section's NEIGHBOURS, and charging every word of those to this file
produced exactly the false positives this corpus kept reporting ("management advisory
services investment", 4 tokens, billed to 02-background while it held all four many
times over).

But the cut is one-directional. find_gaps only ever turns `delete` and `replace`
opcodes into findings — source text this file was supposed to hold and does not. Text
the file DOES hold that falls outside its own slice is an `insert`, which no bucket
looks at, so removing it costs nothing and is reported nowhere. On a flat-built product
(product_rules.SECTION_DEPTH = 0 for this one) sections are large and MinerU routinely
merges a page-spanning table across a section boundary, so misfiled content is common —
which makes the unchecked surface large rather than theoretical.

These tests pin the boundary as it currently stands, so a fix flips them deliberately
rather than by accident.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_content_localized import find_gaps  # noqa: E402

MIN_GAP = 4


def toks(prefix: str, n: int) -> list[str]:
    """A distinctive run — nothing here may accidentally match anything else."""
    return [f"{prefix}{i}" for i in range(n)]


OWN_BODY = toks("own", 60)        # section 8's real text, inside its own slice
NEIGHBOUR = toks("nbr", 60)       # section 7's text, which file 08 also holds


def test_a_loss_inside_the_sections_own_slice_is_caught():
    """The control. Content the slice covers, missing from the file, is a finding."""
    dropped, _reloc, _elsewhere, _restr, _changed = find_gaps(
        pdf_tokens=OWN_BODY, md_tokens=OWN_BODY[:20], min_gap=MIN_GAP,
        whole_tokens=OWN_BODY[:20])
    assert dropped, "content inside the slice must still be reported when it goes missing"
    assert sum(len(d["tokens"]) for d in dropped) == 40


def test_content_held_from_outside_the_slice_is_invisible_while_present():
    """The file holds its neighbour's text. The PDF side is cut to this section's own
    slice, so that text has no counterpart and produces no opcode of any kind."""
    dropped, reloc, elsewhere, restr, changed = find_gaps(
        pdf_tokens=OWN_BODY, md_tokens=OWN_BODY + NEIGHBOUR, min_gap=MIN_GAP,
        whole_tokens=OWN_BODY + NEIGHBOUR)
    assert not (dropped or reloc or elsewhere or restr or changed)


def test_KNOWN_GAP_deleting_that_same_content_is_also_invisible():
    """...and so is losing it. Identical output before and after a 60-token deletion:
    nothing distinguishes a file that holds its neighbour's clause from one that
    dropped it on the floor.

    This asserts the CURRENT behaviour, which is wrong. When the check learns to
    reconcile a file's md-only content against the document as a whole, this test
    should fail and be rewritten to assert the finding it then produces."""
    before = find_gaps(pdf_tokens=OWN_BODY, md_tokens=OWN_BODY + NEIGHBOUR,
                       min_gap=MIN_GAP, whole_tokens=OWN_BODY + NEIGHBOUR)
    after = find_gaps(pdf_tokens=OWN_BODY, md_tokens=OWN_BODY,
                      min_gap=MIN_GAP, whole_tokens=OWN_BODY)
    assert before == after
    assert not any(before)          # and both are empty, so neither is reported


def test_retention_accounting_is_equally_blind_to_it():
    """kept/absent are counted over every opcode, not only the ones that became
    findings — but `insert` is not an opcode either of them counts, so the hollow
    test's retention figure cannot see the loss the finding buckets missed."""
    stats_held: dict = {}
    stats_lost: dict = {}
    find_gaps(OWN_BODY, OWN_BODY + NEIGHBOUR, MIN_GAP,
              whole_tokens=OWN_BODY + NEIGHBOUR, stats_out=stats_held)
    find_gaps(OWN_BODY, OWN_BODY, MIN_GAP,
              whole_tokens=OWN_BODY, stats_out=stats_lost)
    assert stats_held["kept_tokens"] == stats_lost["kept_tokens"] == len(OWN_BODY)
    assert stats_held["absent_tokens"] == stats_lost["absent_tokens"] == 0


# ------------------------------------------- the reprinted table header seam (Germany 179874)

_HEADER = [["questions", "answers"]]
_TAIL = ("marketed sold into your jurisdiction via the private placement regime "
         "in respect of").split()
_LEAD = "alpha beta gamma delta epsilon".split()


def _gaps(pdf, md):
    from check_content_localized import find_gaps
    return find_gaps(pdf, md, 5, whole_tokens=md, header_units=_HEADER)


def test_a_span_starting_with_a_reprinted_header_is_not_a_gap():
    """Germany 179874 page 29. The PDF reprints "Questions / Answers" above the
    continuation of a question, so its reading order glues the header onto the first cell
    under it; the tree holds the header once, at the top of the reconstructed table. The
    15-token span scored 0.50 against its own file — one window short of RELOC_THRESHOLD,
    because RELOC_STRIDE=4 gives a span that length only two windows and the seam poisons
    one. Its 13-token tail scores 1.0: every word is present.
    """
    pdf = _LEAD + ["questions", "answers"] + _TAIL
    md = _LEAD + ["questions", "answers"] + ["zzz"] * 3 + _TAIL
    dropped, *_ = _gaps(pdf, md)
    assert dropped == []


def test_the_same_header_does_not_excuse_a_real_loss():
    """The point of the rescue is the SEAM, not the header. With the tail genuinely
    absent, stripping the header leaves content that is still nowhere in the tree."""
    pdf = _LEAD + ["questions", "answers"] + _TAIL
    md = _LEAD + ["questions", "answers"]
    dropped, *_ = _gaps(pdf, md)
    assert len(dropped) == 1
    assert "private placement" in " ".join(dropped[0]["tokens"])


def test_a_partly_present_tail_stays_reported():
    """Half a sentence surviving is the shape of a genuine partial loss, not a seam."""
    pdf = _LEAD + ["questions", "answers"] + _TAIL
    md = _LEAD + ["questions", "answers"] + _TAIL[:4]
    dropped, *_ = _gaps(pdf, md)
    assert len(dropped) == 1


def test_a_loss_with_no_header_involved_is_untouched():
    dropped, *_ = _gaps(_LEAD + _TAIL, _LEAD)
    assert len(dropped) == 1


def test_the_strip_only_takes_whole_runs_from_the_ends():
    """It must never bite into the middle of a span, and never consume all of it."""
    from check_content_localized import _strip_reprinted_header
    assert _strip_reprinted_header(["questions", "answers"] + _TAIL, _HEADER) == _TAIL
    assert _strip_reprinted_header(_TAIL + ["questions", "answers"], _HEADER) == _TAIL
    # the header sitting mid-span is not an end, so nothing is removed
    mid = _TAIL[:3] + ["questions", "answers"] + _TAIL[3:]
    assert _strip_reprinted_header(mid, _HEADER) == mid
    # a span that IS the header keeps its tokens rather than becoming empty
    assert _strip_reprinted_header(["questions", "answers"], _HEADER) == ["questions", "answers"]


# ------------------------------------ a word spelled differently is not a missing word

def test_a_compound_broken_across_a_line_is_not_a_gap():
    """Germany 179874 page 128 wraps mid-compound — 'formal "black-/letter law"' — so the
    PDF yields 'black' + 'letter' while the tree holds 'black-letter' whole. Every word of
    the 14-token span was present and it read as content loss."""
    lead = "which must be satisfied in order to".split()
    pdf = lead + "utilise it please comment on both formal black letter law exemptions".split()
    md = lead + "utilise it please comment on both formal black-letter law exemptions".split()
    dropped, *_ = _gaps_plain(pdf, md)
    assert dropped == []


def test_it_works_in_the_other_direction_too():
    """The haystack can be the side carrying the other spelling."""
    lead = "which must be satisfied in order to".split()
    pdf = lead + "utilise it please comment on both formal black-letter law exemptions".split()
    md = lead + "utilise it please comment on both formal black letter law exemptions".split()
    dropped, *_ = _gaps_plain(pdf, md)
    assert dropped == []


def test_an_apostrophe_spelled_differently_is_not_a_gap():
    lead = "which must be satisfied in order to".split()
    dropped, *_ = _gaps_plain(lead + "the regulator s published view on this".split(),
                              lead + "the regulators published view on this".split())
    assert dropped == []


def test_punctuation_normalisation_does_not_excuse_absent_words():
    """The rescue is about SPELLING, not about presence. With the words genuinely gone,
    normalising changes nothing and the span stays reported."""
    lead = "which must be satisfied in order to".split()
    pdf = lead + "utilise it please comment on both formal black letter law exemptions".split()
    dropped, *_ = _gaps_plain(pdf, lead)
    assert len(dropped) == 1
    assert "black" in dropped[0]["tokens"]


def _gaps_plain(pdf, md):
    from check_content_localized import find_gaps
    return find_gaps(pdf, md, 5, whole_tokens=md)
