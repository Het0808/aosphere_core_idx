"""Heading coverage must not depend on punctuation the pipeline never treated as content.

Measured on Jersey/181919: the outline's own heading reads "10 LICENCE", but subchunking's
renumbering pass writes the tree's copy as "# 10. LICENCE" -- a period after the number that
carries no information. A plain lowercased-whitespace compare called that heading MISSING at
stage 5, when it was there the whole time under a cosmetically different number.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compare_stage_completeness import _norm_heading  # noqa: E402


def test_a_trailing_period_after_the_number_does_not_create_a_mismatch():
    assert _norm_heading("10 LICENCE") == _norm_heading("10. LICENCE")


def test_case_and_whitespace_still_do_not_matter():
    assert _norm_heading("6   Marketing/Selling to the Public") == _norm_heading(
        "6 MARKETING/SELLING TO THE PUBLIC")


def test_a_genuinely_different_heading_still_does_not_match():
    assert _norm_heading("10 LICENCE") != _norm_heading("11 PENALTIES/SANCTIONS")


# ---------------------------------------------------- the group label the AI pass folds in

def test_the_folded_appendix_label_does_not_read_as_a_lost_heading():
    """The page prints "APPENDIX 1" above "DISCLAIMERS: OPEN-ENDED FUND", the bookmark
    names the appendix "1 Disclaimers: Open-Ended Fund", and stage 4 is told to fold the
    orphaned label into the H1 — so the post-AI tree says "Appendix 1 Disclaimers:
    Open-Ended Fund". Neither is wrong, but an exact compare called every appendix in the
    product a heading the tree had lost: 187 of 197 headings reported MISSING at stage 5
    over the 16-09-26 corpus, in all 54 documents.
    """
    from compare_stage_completeness import _norm_heading
    assert (_norm_heading("Appendix 1 Disclaimers: Open-Ended Fund")
            == _norm_heading("1 Disclaimers: Open-Ended Fund"))
    assert (_norm_heading("SCHEDULE 2 Designated Jurisdictions")
            == _norm_heading("2 Designated Jurisdictions"))


def test_the_appendix_number_still_discriminates():
    """Only the family word is dropped. Appendix 2 must not satisfy appendix 3."""
    from compare_stage_completeness import _norm_heading
    assert (_norm_heading("Appendix 2 Disclaimers")
            != _norm_heading("Appendix 3 Disclaimers"))


def test_a_title_that_merely_starts_with_the_word_is_untouched():
    """"Appendix to the Agreement" is a title, not a label — no number follows it."""
    from compare_stage_completeness import _norm_heading
    assert _norm_heading("Appendix to the Agreement").startswith("appendix")
