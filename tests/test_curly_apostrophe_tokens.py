"""A possessive or contraction set with a typeset apostrophe must tokenize the
same as one written with a plain ASCII one.

TOKEN_RE only admits the ASCII apostrophe (U+0027) inside a token. A PDF's text
layer commonly sets a possessive with the typeset RIGHT SINGLE QUOTATION MARK
("regulator’s", U+2019) instead -- a glyph invisible to TOKEN_RE exactly as a
ligature is: it matches nothing, so instead of joining the word it acts as a
SEPARATOR, and the word SPLITS -- "regulator" + "s" -- against a tree that,
writing its own markdown in plain ASCII, holds "regulator's" as one token. Every
check that diffs the two token streams then reports the whole surrounding run as
missing, because the two sides can never agree on this one word.

Measured on 124_Marketing_Restrictions/Brunei__180131: a 21-token span ending
"...regulator's website plus any possible exemptions..." was reported DROPPED
even though the tree prints the identical words -- the PDF's apostrophe glyph
was the only difference.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from lib_content_compare import tokenize  # noqa: E402


def test_a_curly_right_apostrophe_possessive_tokenizes_like_the_ascii_one():
    assert tokenize("regulator’s website") == tokenize("regulator's website")
    assert tokenize("regulator’s website") == ["regulator's", "website"]


def test_a_curly_apostrophe_plural_possessive_also_joins():
    assert tokenize("firms’ clients") == ["firms'", "clients"]


def test_a_leading_curly_left_quote_apostrophe_does_not_split_the_word():
    assert tokenize("the ’90s market") == ["the", "90s", "market"]


def test_the_brunei_span_is_now_one_contiguous_run():
    """The exact span that was reported dropped: present verbatim in the tree
    (straight apostrophe) but printed by the PDF with a curly one."""
    pdf_text = ("relevant forms information on the relevant regulator’s website "
               "plus any possible exemptions from such requirements")
    tree_text = ("relevant forms/information on the relevant regulator's website) "
                "plus any possible exemptions from such requirements.")
    assert tokenize(pdf_text) == tokenize(tree_text)


def test_ordinary_ascii_apostrophes_are_unaffected():
    assert tokenize("the firm's clients aren't notified") == [
        "the", "firm's", "clients", "aren't", "notified",
    ]
