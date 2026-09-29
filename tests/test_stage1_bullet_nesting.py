"""A list item's DEPTH is carried only by the x-position of its bullet glyph.

emit() rewrites the glyph to a flat "- " and normspace() throws the position away, so a
document nesting several levels deep arrived as one flat list. Bahamas 183503 p5 prints
an item at x=74.9 and a sub-item at x=92.9, and both came out as "- " -- reading as
siblings when one contains the other.

Every threshold in bullet_tiers exists to REFUSE rather than guess: a wrong nesting
asserts a containment the document does not have, which is worse than no nesting. So the
cases below are mostly about what must NOT be nested. The measured ladders they are drawn
from:

    Australia 181814   54.0 / 90.0 / 103.7 / 126.0 / 164.6      accepted
    Bahamas 183503     56.9 / 74.9 / 92.9 / 110.9  (18pt steps) accepted
    UK 179582          552 of 628 items at x=48.2, tail over 8  refused
                       further positions incl. a 2-column glossary
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "pdf2mdtree", Path(__file__).resolve().parents[1] / "scripts" / "pdf2mdtree.py")
p2m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p2m)


# ---- reading the ladder --------------------------------------------------------

def test_bahamas_18pt_ladder_is_accepted():
    xs = [56.9] * 7 + [74.9] * 2 + [92.9] * 38 + [110.9] * 4
    tiers, why = p2m.bullet_tiers(xs)
    assert why is None
    assert [round(t, 1) for t in tiers] == [56.9, 74.9, 92.9, 110.9]


def test_a_rare_level_still_counts():
    """x=74.9 holds 2 of Bahamas' 51 items, because most of that document's pages are
    deferred tables. A share-of-items floor was tried here and reverted: it flattened
    exactly the sub-items this feature exists for. Rarity is not noise in a document
    whose prose is mostly elsewhere."""
    xs = [56.9] * 7 + [74.9] * 2 + [92.9] * 38 + [110.9] * 4
    tiers, _ = p2m.bullet_tiers(xs)
    assert 74.9 in [round(t, 1) for t in tiers]
    assert p2m.bullet_level(74.9, tiers) == 1


def test_glyphs_within_the_tolerance_are_one_level():
    """Real glyph positions wobble a point or two; that is not a new indent level."""
    tiers, why = p2m.bullet_tiers([54.0, 54.4, 55.2, 90.0, 90.3])
    assert why is None and len(tiers) == 2


# ---- refusing ------------------------------------------------------------------

def test_one_level_is_not_nesting():
    tiers, why = p2m.bullet_tiers([48.2] * 200)
    assert tiers is None and why == 'one indent level only'


def test_no_items_at_all():
    tiers, why = p2m.bullet_tiers([])
    assert tiers is None and why == 'no list items'


def test_a_page_column_is_not_an_indent_step():
    """The UK glossary prints term/definition as two columns at x=51 and x=300. A
    bullet in the right column is not eight levels deep."""
    tiers, why = p2m.bullet_tiers([51.0] * 40 + [300.2] * 13)
    assert tiers is None and 'page-column gap' in why


def test_levels_too_close_together_are_refused():
    """UK 179582's tail includes positions 6.8pt apart — inside one indent step, so
    they cannot both be levels."""
    tiers, why = p2m.bullet_tiers([48.2] * 552 + [65.3] * 5 + [72.1] * 4 + [82.2] * 2)
    assert tiers is None and 'not distinct indents' in why


def test_more_levels_than_a_real_list_uses_is_refused():
    xs = []
    for i in range(p2m.BULLET_MAX_LEVELS + 1):
        xs += [50.0 + 20.0 * i] * 4
    tiers, why = p2m.bullet_tiers(xs)
    assert tiers is None and 'noise, not nesting' in why


def test_the_refusal_always_says_why():
    """A silent fallback to flat bullets is indistinguishable from a document that has
    no nesting, and the two want opposite fixes."""
    for xs in ([], [48.2] * 10, [51.0] * 5 + [300.0] * 5):
        tiers, why = p2m.bullet_tiers(xs)
        assert tiers is None and why, xs


# ---- assigning levels ----------------------------------------------------------

def test_level_is_the_last_tier_at_or_left_of_the_glyph():
    tiers = [56.9, 74.9, 92.9, 110.9]
    assert [p2m.bullet_level(x, tiers) for x in (56.9, 74.9, 92.9, 110.9)] == [0, 1, 2, 3]


def test_a_glyph_between_tiers_takes_the_shallower_one():
    """Never the deeper: over-nesting invents a containment, under-nesting only fails to
    report one."""
    assert p2m.bullet_level(85.0, [56.9, 74.9, 92.9]) == 1


# ---- rewriting the paragraphs --------------------------------------------------

def _paras():
    return [
        {'text': '- top level', 'bullet_x': 56.9},
        {'text': '- one deep', 'md': '- one **deep**', 'bullet_x': 74.9},
        {'text': '- two deep', 'bullet_x': 92.9},
        {'text': 'ordinary prose, not a list item'},
    ]


def test_indent_is_two_spaces_per_level_on_both_text_and_md():
    """Two spaces because "- " is two characters wide, so a wrapped continuation lines
    up under its parent's text. 'md' must move in step with 'text' or the two stop
    differing by emphasis alone."""
    ps = _paras()
    report = {}
    p2m.apply_bullet_nesting(ps, report)
    assert ps[0]['text'] == '- top level'
    assert ps[1]['text'] == '  - one deep' and ps[1]['md'] == '  - one **deep**'
    assert ps[2]['text'] == '    - two deep'
    assert ps[3]['text'] == 'ordinary prose, not a list item'
    assert report['bullet_items'] == 3 and report['bullet_items_nested'] == 2


def test_nesting_adds_only_whitespace():
    """The guarantee that keeps this safe downstream: word counts, the token guard in
    emit() and every text comparison in the QA stack are untouched."""
    ps = _paras()
    before = [p.get('text', '') for p in ps]
    p2m.apply_bullet_nesting(ps, {})
    for b, p in zip(before, ps):
        assert b.split() == p['text'].split()


def test_a_refused_ladder_leaves_every_item_flat():
    ps = [{'text': '- a', 'bullet_x': 48.2}, {'text': '- b', 'bullet_x': 48.2}]
    report = {}
    p2m.apply_bullet_nesting(ps, report)
    assert [p['text'] for p in ps] == ['- a', '- b']
    assert report['bullet_tiers'] is None and report['bullet_flat_reason']


def test_report_carries_the_raw_positions_for_retuning():
    ps = _paras()
    report = {}
    p2m.apply_bullet_nesting(ps, report)
    assert report['bullet_x_counts'] == {56.9: 1, 74.9: 1, 92.9: 1}
