"""A standalone "No." is an ANSWER, and the flip test cannot judge one.

That test proves a sentence survived by matching five words either side of the tracked
word. An answer cell has no such context: its neighbours are OTHER CELLS, and the tree
reorders them by design — a PDF's reading order interleaves a Yes/No column with the
prose beside it, while the tree serialises the table row by row. So the words around an
answer are EXPECTED to differ, which fails the "did it survive" test on a perfectly good
extraction. And because these memoranda ask near-identical questions repeatedly (once
for Funds, once for Investment Management & Advisory Services), a parallel clause is
readily available to satisfy the "sentence is here without it" test.

Measured on the 95-document corpus: all four negation flips reported were standalone
answers, and all four were wrong —

  Switzerland p25  "…potential or an existing client [No] (b) Are there registration…"
                   the No. is in 08-private-placement-regime.md row 7; the context failed
                   only because the tree reads "potentialor anexistingclient" (glued).
  Luxembourg  p92  tree holds "crypto-asset strategy | No | Luxembourg UCITS may…";
                   "no" counts 250 in the PDF and 250 in the tree.
  Ecuador     p24  "No. Ecuadorian legislation related to…" — present, on another row.
  Mauritius   p78  "No, there are no exemptions to…" — present.

Over the same corpus 13 documents ARE missing a negation and not one was reported, so
exempting answers costs no detection. What must NOT be lost is a negation inside a
sentence, which is the case this check was built for and is pinned below.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_semantic_integrity import (  # noqa: E402
    _hay, count_answers, find_meaning_flips, tokenize_page,
)
from lib_content_compare import tokenize  # noqa: E402


def flips(pdf_text: str, tree_text: str):
    toks, answers = tokenize_page(pdf_text)
    return find_meaning_flips(toks, _hay(tokenize(tree_text)), skip_idx=answers)


# ---- what must still be caught ------------------------------------------

def test_a_negation_inside_a_sentence_is_still_reported():
    """The case this check exists for: one word, the obligation inverted."""
    got = flips("a managed fund that is not structured as a legal entity at all",
                "a managed fund that is structured as a legal entity at all")
    assert [f["word"] for f in got] == ["not"]


def test_a_leading_no_with_no_punctuation_is_clause_text_not_an_answer():
    """"No person shall market a fund" is a clause, and its "no" stays tracked."""
    _toks, answers = tokenize_page("No person shall market a fund without approval")
    assert answers == set()


# ---- what must no longer be reported ------------------------------------

def test_a_bare_answer_line_is_exempt():
    _toks, answers = tokenize_page("(v) any sensitivities depending on the client.\nNo.\n(b) Are there")
    assert answers, "a line that is just 'No.' is an answer"


def test_an_answer_opener_is_exempt():
    for line in ("No. Ecuadorian legislation related to the securities market does not apply",
                 "No, there are no exemptions to the Licensing Requirement however the",
                 "Yes: the regulator requires prior notification before any marketing"):
        _toks, answers = tokenize_page(line)
        assert answers == {0}, line


def test_the_switzerland_false_positive_is_gone():
    """Glued context at the true location, a parallel clause elsewhere — the exact
    shape that produced a -15 penalty on an intact extraction."""
    pdf = ("(v) any sensitivities depending on whether the investor is a\n"
           "potential or an existing client.\n"
           "No.\n"
           "(b) Are there registration/notification requirements which apply\n")
    tree = ("v any sensitivities depending on whether the investor is a potentialor "
            "anexistingclient no b are thereregistration notification requirements "
            "iii any sensitivities depending on whether the n a investor is a potential "
            "or an existing client b are there registration notification requirements")
    assert flips(pdf, tree) == []


# ---- the answer counters ------------------------------------------------

def test_answers_are_counted_the_same_way_on_both_sides():
    assert count_answers(["No.", "Yes", "No, there are no exemptions to the rule",
                          "the fund is not marketed"]) == {"no": 2, "yes": 1}
