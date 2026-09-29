"""An unlabelled contents-page entry belongs where the page put it, not at the top.

`_entries_from_lines` reads an entry's depth from its label ("4.4" -> 2, "A." -> 1). A
row with no label at all returned None, and `depth or 1` made it a level-1 entry --
which pdf2mdtree turns into a SECTION of its own, because it emits sections from
level-1 headings only.

Belgium 163341 is the measured case. Its PDF ships 16 bookmarks and no usable outline,
so the printed contents page is rescued instead, and that page sets two sub-headings of
clause 4.4 without numbers:

    4.4 Definition of "AIFMD Marketing" and/or "Pre-Marketing" ... 34
    Harmonised CBDF Pre-Marketing Regime ....................... 38
    Non-CBDF Pre-Marketing Regime .............................. 41
    4.5 Provision of Non-Core Services by an AIFM .............. 45

Both were promoted out of clause 4 and written between it and clause 5, so the tree
gained two unnumbered sections and every later file's ordinal shifted: two files ended
up prefixed `10-` (clause 10 and clause 7) and two `11-` (clause 8 and clause 11), and
the tree no longer ran in clause order.

The second test is the guard rail, and it is the one that matters: the rule is NOT "an
unlabelled title is never a section". DISCLAIMERS: OPEN-ENDED FUND and its three
siblings are unlabelled too and they ARE top-level divisions -- they follow clause 11,
so inheriting the previous depth keeps them at 1, where a "must be numbered" rule would
have demoted all four.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from rescue_outline import _entries_from_lines  # noqa: E402


def _rows(*lines):
    """-> {title: level} for a contents page written as `lines`."""
    return {t: lvl for lvl, t, _p in _entries_from_lines(list(lines))}


def _dots(n=60):
    return "." * n


def test_an_unlabelled_entry_between_subsections_stays_inside_its_section():
    """Belgium 163341, verbatim. Both unlabelled rows sit between 4.4 and 4.5, so they
    are sub-headings of clause 4 -- not sections between clause 4 and clause 5."""
    r = _rows(
        f"4 SPECIFIC ISSUES RELATING TO THE IMPLEMENTATION OF AIFMD {_dots()} 26",
        f"4.3 Co-operation Agreements {_dots()} 33",
        f"4.4 Definition of “AIFMD Marketing” and/or “Pre-Marketing” {_dots()} 34",
        f"Harmonised CBDF Pre-Marketing Regime {_dots()} 38",
        f"Non-CBDF Pre-Marketing Regime {_dots()} 41",
        f"4.5 Provision of Non-Core Services by an AIFM {_dots()} 45",
        f"5 PASSIVE MARKETING (REVERSE-ENQUIRY) {_dots()} 47",
    )
    assert r["4 SPECIFIC ISSUES RELATING TO THE IMPLEMENTATION OF AIFMD"] == 1
    assert r["5 PASSIVE MARKETING (REVERSE-ENQUIRY)"] == 1
    # The two that used to be promoted. Level 2 = a sub-heading of clause 4, which is
    # what the contents page prints and what stops them becoming files of their own.
    assert r["Harmonised CBDF Pre-Marketing Regime"] == 2, "promoted out of its section"
    assert r["Non-CBDF Pre-Marketing Regime"] == 2, "promoted out of its section"


def test_an_unlabelled_entry_after_a_top_level_one_is_still_top_level():
    """The guard rail. These four are unlabelled AND genuinely top-level: they follow
    clause 11, so they inherit depth 1. A 'must be numbered to be a section' rule would
    demote every one of them."""
    r = _rows(
        f"10. LICENCE {_dots()} 128",
        f"11. PENALTIES/SANCTIONS {_dots()} 132",
        f"DISCLAIMERS: OPEN-ENDED FUND {_dots()} 135",
        f"DISCLAIMERS: CLOSED-ENDED FUND {_dots()} 141",
        f"DISCLAIMERS: INVESTMENT MANAGEMENT & ADVISORY SERVICES {_dots()} 145",
        f"DISCLAIMERS: OTHER {_dots()} 146",
    )
    for t in ("DISCLAIMERS: OPEN-ENDED FUND", "DISCLAIMERS: CLOSED-ENDED FUND",
              "DISCLAIMERS: INVESTMENT MANAGEMENT & ADVISORY SERVICES",
              "DISCLAIMERS: OTHER"):
        assert r[t] == 1, f"{t} was demoted out of the top level"


def test_an_unlabelled_first_entry_still_falls_back_to_one():
    """Nothing to inherit. The `or 1` fallback stays, because an outline whose entries
    all land below level 1 produces an EMPTY tree -- the hazard the original default
    was there to prevent."""
    r = _rows(
        f"Introduction {_dots()} 1",
        f"1 BACKGROUND {_dots()} 3",
    )
    assert r["Introduction"] == 1
