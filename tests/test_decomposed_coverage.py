"""Covering a reported gap with maximal runs instead of fixed windows.

The per-section diff reports a contiguous run of PDF tokens with no counterpart in the
section's own markdown. Deciding whether that run is genuinely LOST means asking whether
it exists anywhere in the tree — and the sliding-window measure cannot answer that when
the run straddles a seam. These documents glue two kinds of text together in the PDF's
reading order (a reprinted table header, then the first cell under it), while the tree
holds the pieces far apart: no 8-token window lies wholly inside either piece, so the
window ratio collapses to 0.0 even though every word is present.

Measured on the 281-page US States privacy survey — the ten spans still reported after
the header-unit fix scored 0.00-0.62 by window and 0.86-1.00 by decomposition, and each
was text plainly in the tree.

What must NOT happen is excusing real loss, so the floor on run length is pinned too:
without it, any span could be assembled from two-token coincidences.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_content_localized import (  # noqa: E402
    DECOMPOSE_MIN_RUN, PRESENT_ELSEWHERE_THRESHOLD, REORDER_MAX_SPAN, _hay,
    decomposed_coverage, find_gaps, relocation_ratio,
)


def toks(s: str) -> list[str]:
    return s.split()


def test_a_span_present_in_full_is_fully_covered():
    hay = _hay(toks("alpha beta gamma delta epsilon zeta"))
    assert decomposed_coverage(toks("beta gamma delta epsilon"), hay) == 1.0


def test_absent_text_is_not_covered():
    hay = _hay(toks("alpha beta gamma delta"))
    assert decomposed_coverage(toks("nothing here matches at all"), hay) == 0.0


def test_the_seam_case_the_window_measure_misses():
    """The real shape: header row + the cell text under it, held apart in the tree."""
    header = "law and scope requirements"
    cell = "credit card debit card and or financial account number in combination"
    span = toks(f"{header} {cell}")
    # the tree has both pieces, far apart, never adjacent
    tree = toks(f"{header} state alabama alaska arizona many other words in between {cell}")
    hay = _hay(tree)
    assert decomposed_coverage(span, hay) == 1.0
    # ...while the window measure sees far less, which is the bug this fixes
    assert relocation_ratio(span, tree) < 1.0


def test_partial_loss_stays_partial():
    """Half the span present, half genuinely gone -> about half covered, so a span like
    this stays REPORTED at the 0.9 threshold. This is the sensitivity that matters."""
    present = "alpha beta gamma delta epsilon zeta eta theta"
    span = toks(present + " unseen unrelated missing content words here now")
    cov = decomposed_coverage(span, _hay(toks(present)))
    assert 0.4 < cov < 0.7
    assert cov < PRESENT_ELSEWHERE_THRESHOLD


def test_short_coincidental_runs_do_not_count():
    """"of the" and "and or" occur everywhere; a span must not be excused by them."""
    hay = _hay(toks("of the and or in a to be for it with"))
    span = toks("of the completely absent clause and or")
    assert decomposed_coverage(span, hay) == 0.0
    assert DECOMPOSE_MIN_RUN >= 4


def test_runs_exactly_at_the_floor_count():
    hay = _hay(toks("one two three four"))
    assert decomposed_coverage(toks("one two three four"), hay) == 1.0
    assert decomposed_coverage(toks("two three four"), hay) == 0.0     # 3 tokens < floor


def test_bisection_finds_the_longest_run_not_just_the_first_match():
    """A greedy scan that stopped at the floor length would under-count: it would take
    4 tokens, restart mid-run, and miss that the whole thing is present."""
    long_run = toks("a b c d e f g h i j k l")
    assert decomposed_coverage(long_run, _hay(long_run)) == 1.0


def test_empty_inputs_are_not_excused():
    assert decomposed_coverage([], _hay(toks("a b c d"))) == 0.0
    assert decomposed_coverage(toks("a b c d"), "") == 0.0
    assert decomposed_coverage(toks("a b c d"), _hay([])) == 0.0


# ---- the classification chain -------------------------------------------------
def test_find_gaps_excuses_a_seam_span_only_when_the_tree_has_it():
    """End to end through find_gaps: same span, two trees. Present -> excused as
    present_elsewhere; absent -> stays dropped."""
    header, cell = "law and scope requirements", "credit card debit card and or financial"
    # the section's own markdown has neither piece, so the diff reports the span
    pdf = toks(f"intro words {header} {cell} closing words")
    md = toks("intro words closing words")
    elsewhere = toks(f"{header} filler filler filler filler {cell}")

    dropped, _rel, present, _res, _chg = find_gaps(
        pdf, md, min_gap=4, whole_tokens=md + elsewhere)
    assert not dropped and len(present) == 1
    assert present[0]["decomposed_ratio"] >= PRESENT_ELSEWHERE_THRESHOLD

    dropped, _rel, present, _res, _chg = find_gaps(
        pdf, md, min_gap=4, whole_tokens=md)
    assert len(dropped) == 1 and not present


# ---- a span whose words are all present, just never adjacent -------------------------
# decomposed_coverage still needs a run of DECOMPOSE_MIN_RUN-or-more matching tokens IN A
# ROW. A heading immediately followed by a different node's table header, or a glossary
# label and its own definition printed out of order (Jersey/181919, both measured), fail
# that the same way real loss does: no run long enough exists anywhere, because the two
# halves of the span are never neighbours in the tree. What distinguishes them from real
# loss is that EVERY token, independently, is findable — just not next to each other.

def test_find_gaps_excuses_a_short_span_whose_words_are_all_present_but_scattered():
    """A section heading immediately followed by a DIFFERENT table's header -- the exact
    shape measured on Jersey/181919: 'marketing selling to the public' (a heading) glued
    in PDF reading order to 'questions answers' (a table header that belongs to a sibling
    node), never adjacent anywhere in the tree."""
    span = toks("marketing selling to the public questions answers")
    pdf = toks("intro") + span + toks("mutual recognition closing")
    md = toks("intro mutual recognition closing")               # this file's own content
    # the heading and the table header are each real, just filed under different nodes,
    # and never adjacent to one another anywhere in the tree
    tree = md + toks("marketing selling to the public") + toks("other node questions answers here")

    dropped, _rel, present, _res, _chg = find_gaps(pdf, md, min_gap=4, whole_tokens=tree)
    assert not dropped and len(present) == 1
    assert present[0]["reorder_note"]


def test_a_genuinely_missing_word_still_blocks_the_excuse():
    """Five of six words present is not six of six -- the bar is EVERY token, not most,
    so a span with one real gap in it must stay reported."""
    span = toks("marketing selling to the public vanished")    # "vanished" is nowhere
    pdf = toks(f"intro {' '.join(span)} closing")
    md = toks("closing")
    tree = md + toks("marketing selling to the public")

    dropped, _rel, present, _res, _chg = find_gaps(pdf, md, min_gap=4, whole_tokens=tree)
    assert len(dropped) == 1 and not present


def test_a_long_span_is_not_excused_just_because_every_word_exists_somewhere():
    """Past REORDER_MAX_SPAN, "every word appears somewhere in the document" stops being
    meaningful evidence -- a document has thousands of words, and a long span assembled
    entirely from scattered singletons is exactly what real, paraphrased loss looks like,
    not a reordering artifact."""
    words = [f"word{i}" for i in range(REORDER_MAX_SPAN + 1)]    # longer than the cap
    pdf = toks("intro") + words + toks("closing")
    md = toks("intro closing")
    # every word present, but scattered — never two of them adjacent — so this is testing
    # the length cap specifically, not decomposed_coverage's own run requirement
    tree = md + [t for w in words for t in (w, "filler", "filler", "filler")]

    dropped, _rel, present, _res, _chg = find_gaps(pdf, md, min_gap=4, whole_tokens=tree)
    assert len(dropped) == 1 and not present


def test_find_gaps_without_a_whole_tree_pool_still_reports():
    """whole_tokens is optional (single-file callers); decomposition must not fire on
    None and quietly excuse everything."""
    pdf = toks("intro words some genuinely missing clause here closing words")
    md = toks("intro words closing words")
    dropped, _rel, present, _res, _chg = find_gaps(pdf, md, min_gap=4)
    assert len(dropped) == 1 and not present


# ---- the page fingerprint must not depend on hash order -----------------------
def test_page_fingerprint_is_deterministic():
    """The sample is chosen from a SET with a tie-prone key, so without the word itself
    as the final key the order came from PYTHONHASHSEED and a borderline page flipped
    between present and missing by luck (Denmark 167272: page coverage 100% on five
    seeds, 98.7% on the sixth, moving completeness 97.8 <-> 96.5)."""
    from check_page_coverage import _page_fingerprint

    # same length, same frequency -> the key ties on BOTH components, which is the
    # normal case on a page of prose where most words occur once
    tokens = ["alpha", "bravo", "charl", "delta", "echoo", "foxtr", "golfy", "hotel",
              "india", "kilos", "limas", "mikes"]
    freq = {t: 1 for t in tokens}
    first = _page_fingerprint(tokens, freq)
    # a differently-ordered input set must not change the sample
    for rotation in range(1, len(tokens)):
        rotated = tokens[rotation:] + tokens[:rotation]
        assert _page_fingerprint(rotated, freq) == first
    assert first == sorted(tokens)[:len(first)]     # fully tied -> alphabetical

    # length still outranks the alphabet: a longer word is more distinctive
    assert _page_fingerprint(["zzzzzz"] + tokens, {**freq, "zzzzzz": 1})[0] == "zzzzzz"
