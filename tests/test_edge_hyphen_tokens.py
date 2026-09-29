"""A dash at a token's EDGE must not make the two sides disagree about the word.

TOKEN_RE admits a hyphen INSIDE a token, which is what "reverse-enquiry" and
"cross-border" need. It also swallowed a trailing dash, and that is never part of a
word — it is layout. The Jersey MRAM memo prints "…marketing in Jersey - if there is
no change…" and the extractor emitted "…marketing in Jersey- if there is…", so the
PDF held `jersey` and the tree held `jersey-`.

That one character cost the document 15 points. The negation check takes 5 words of
context either side of every "not" and asks two questions: is the context WITH the
word in the tree (intact), or is it there WITHOUT the word (the clause flipped)? The
context ended on `jersey`, could not match `jersey-`, and so failed the first test —
while the second test matched a DIFFERENT and entirely legitimate sentence, because
this document carries both bullets:

    "Adding a new sub fund to the Prospectus which is intended for marketing in Jersey"
    "Adding a new sub fund to the Prospectus which is NOT intended for marketing in Jersey"

Both were correctly extracted. The check still reported the "not" as dropped.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_semantic_integrity import _hay, find_meaning_flips  # noqa: E402
from lib_content_compare import tokenize  # noqa: E402


def test_a_trailing_dash_is_not_part_of_the_word():
    assert tokenize("marketing in Jersey- if") == ["marketing", "in", "jersey", "if"]


def test_an_interior_hyphen_still_is():
    assert tokenize("reverse-enquiry and cross-border") == [
        "reverse-enquiry", "and", "cross-border"]


def test_a_bare_dash_yields_no_token():
    assert tokenize("Jersey - if") == ["jersey", "if"]
    assert tokenize("- -- ---") == []


def test_the_negation_survives_a_trailing_dash_in_the_tree():
    """The regression this fix exists for: a present "not" read as dropped."""
    pdf = tokenize(
        "Adding a new sub fund to the Prospectus which is NOT intended for marketing "
        "in Jersey - if there is no change to the offer")
    tree = tokenize(
        # the parallel bullet, which is what supplied the false "without not" match
        "Adding a new sub fund to the Prospectus which is intended for marketing in "
        "Jersey - Depending on the entity. "
        # and the clause itself, correctly extracted, dash glued to the word
        "Adding a new sub fund to the Prospectus which is NOT intended for marketing "
        "in Jersey- if there is no change to the offer")
    assert find_meaning_flips(pdf, _hay(tree)) == []


def test_a_genuinely_dropped_negation_is_still_reported():
    """The floor: stripping edge dashes must not cost the check its teeth."""
    pdf = tokenize("a managed fund that is not structured as a legal entity at all")
    tree = tokenize("a managed fund that is structured as a legal entity at all")
    flips = find_meaning_flips(pdf, _hay(tree))
    assert [f["word"] for f in flips] == ["not"]
