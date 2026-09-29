"""Is any of the source's text absent from the tree — asked with no blind spot.

Every other content check diffs a FILE against a SLICE of the source, and all of them
have holes where those two do not line up. Measured this session, dropping a paragraph
from each eligible file of one document: 2 of 8 caught, 4 correctly silent (a copy
really survived), 2 genuinely missed. Three causes, one shape — some text was never part
of any comparison: content held outside its own heading slice, 00-section.md being in
SKIP_MD_NAMES so stage 5's landing files are read by nothing, and a duplicated file
supplying a copy.

This check removes the first two by having no slices and no exclusions, and is right
about the third: if a duplicate supplies the words, the words ARE in the document.
Re-run against the same probe it missed none of 15 injections across both stages.

WHAT IT DOES NOT DO, pinned below so nobody expects it: it compares MULTISETS, so it
answers "does the document still contain this word, this many times" and not "is this
word in the right place". Deleting one occurrence of a word that appears elsewhere is
invisible to it — verified by deleting a single "memorandum" from a stage-5 tree, which
it does not report. That limitation is the same property that makes it free of the false
positives every placement-aware attempt produced.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_text_conservation import MIN_SCORED_SPAN, _normalise, _span  # noqa: E402


def toks(prefix, n):
    return [f"{prefix}{i}" for i in range(n)]


# ---- normalisation: the two sides must spell a word the same way ----------------
def test_a_hyphenated_compound_is_split_the_same_way_on_both_sides():
    """The tree holds "closed-ended" as ONE token 17 times on Bahamas stage 5 while the
    PDF yields "closed" + "ended", so every occurrence read as two missing words."""
    assert _normalise(["closed-ended"]) == ["closed", "ended"]
    assert _normalise(["closed", "ended"]) == ["closed", "ended"]


def test_a_possessive_loses_its_apostrophe_on_both_sides():
    """The tree holds "regulator's" as one token; the PDF yields "regulator" + "s"."""
    assert _normalise(["regulator's"]) == ["regulators"]
    assert _normalise(["regulator’s"]) == ["regulators"]


def test_normalisation_drops_nothing():
    assert _normalise(["plain", "words"]) == ["plain", "words"]
    assert _normalise(["a-b-c"]) == ["a", "b", "c"]


# ---- spans -----------------------------------------------------------------
def test_a_span_carries_its_page_and_the_text_either_side_of_it():
    pdf = toks("w", 30)
    page_of = [7] * 30
    s = _span([10, 11, 12], pdf, page_of)
    assert s["page"] == 7
    assert s["tokens_missing"] == 3
    assert s["text"] == "w10 w11 w12"
    assert s["before"].endswith("w9")
    assert s["after"].startswith("w13")


def test_the_scored_floor_is_stated_and_above_one_token():
    """Single tokens are reported but never scored: after hyphen and apostrophe
    normalisation the residue is entirely one-token tokeniser disagreement — 22 of them
    in Jersey's 38,000 tokens, 99.94% conserved. Scoring those would put every document
    permanently below perfect for no defect."""
    assert MIN_SCORED_SPAN > 1
