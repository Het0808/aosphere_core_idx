"""A stray space from stripped markup must not read as content loss.

_norm() replaces every HTML tag with a space so that removing one never glues two
words together — correct for prose, but stage 4 legitimately adds inline markup a
cell never had (a bare URL turned into a link, a defined term turned bold). Measured
on Jersey/181919's table_003: every one of its 4 answer cells failed the presence
check purely because "(the FSL)" became "(the FSL )" once an injected </strong> was
stripped from beside the parenthesis — 0 characters of the answer actually changed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_table_presence import _norm  # noqa: E402


def test_a_tag_stripped_beside_a_closing_paren_does_not_add_a_space():
    assert _norm("(the FSL) which") == _norm("(the FSL<strong></strong>) which")


def test_a_tag_stripped_beside_an_opening_paren_does_not_add_a_space():
    assert _norm("(amendment order)") == _norm("(<a href=\"x\">amendment</a> order)")


def test_a_tag_stripped_between_two_ordinary_words_still_keeps_them_apart():
    """The fix targets punctuation specifically -- two real words must still be kept
    apart, or a removed tag really would glue them together."""
    assert _norm("wordone<b>wordtwo") == _norm("wordone wordtwo")
    assert "wordonewordtwo" not in _norm("wordone<b>wordtwo")


def test_a_genuine_wording_difference_still_fails_to_match():
    assert _norm("the expert fund guide") != _norm("the expert fund gude")
