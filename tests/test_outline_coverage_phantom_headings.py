"""Two things the outline names that are not headings the document lost.

check_outline_coverage compares every entry of the stage-1 headings manifest against
every heading in the shipped tree. That question is only as good as the outline, and the
outline carries two entries that can never be answered honestly:

  * "Front Matter" — pdf2mdtree INVENTS it, to keep paragraphs printed before the first
    recognized heading. No page prints it, so asking whether it survived is asking
    whether a later stage kept a label that was never in the source.
  * a bare ordinal the BOOKMARK adds — the ELTIF supplementals' outline names their last
    division "1 Disclaimers" while the page, stage 3 and the shipped tree all head it
    "DISCLAIMERS". Same family as the `Appendix N` fold already in _norm_heading: the two
    sides disagree about a label neither took from the body text.

Both matter now that sectioning is scored on this rate. On the 16-09-26 corpus the three
6-heading supplementals (Denmark 148204, Italy 181491, Sweden 138020) carried one or both,
at 16.7 points each: Sweden fell to 57.1 and failed the gate on two phantoms and one real
loss. With them gone every remaining finding in the corpus is a heading genuinely absent
from the tree.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compare_stage_completeness import _fold_bare_ordinals, _norm_heading  # noqa: E402


def keys(*titles):
    return _fold_bare_ordinals([{"title": t} for t in titles])


def test_a_bookmark_only_ordinal_folds_away():
    k = keys("1 Disclaimers", "1 BACKGROUND", "10. LICENCE")
    assert k[_norm_heading("1 Disclaimers")] == "disclaimers"
    assert k[_norm_heading("1 BACKGROUND")] == "background"
    assert k[_norm_heading("10. LICENCE")] == "licence"


def test_a_subsection_number_is_never_folded():
    """"7.1 Private Placement Regime" must not fold: a section and its sub-section differ
    by exactly that number, and the fold is decided on the RAW title because tokenize()
    flattens "7.1" to "7 1", where the two are indistinguishable."""
    n = _norm_heading("7.1 Private Placement Regime (AIFs and Non-Passported UCITS Funds)")
    assert keys("7.1 Private Placement Regime (AIFs and Non-Passported UCITS Funds)")[n] == n
    n2 = _norm_heading("4.1 Gold-plating/Super-equivalence")
    assert keys("4.1 Gold-plating/Super-equivalence")[n2] == n2


def test_an_ambiguous_fold_is_refused():
    """Two divisions whose text is identical apart from the number: one "Disclaimers"
    heading in the tree must not answer for both."""
    k = keys("1 Disclaimers", "2 Disclaimers")
    assert k[_norm_heading("1 Disclaimers")] == _norm_heading("1 Disclaimers")
    assert k[_norm_heading("2 Disclaimers")] == _norm_heading("2 Disclaimers")


def test_a_fold_that_collides_with_another_entry_is_refused():
    k = keys("1 Disclaimers", "Disclaimers")
    assert k[_norm_heading("1 Disclaimers")] == _norm_heading("1 Disclaimers")


def test_a_bare_number_never_folds_to_nothing():
    assert keys("APPENDIX 5")[_norm_heading("APPENDIX 5")] == "5"
