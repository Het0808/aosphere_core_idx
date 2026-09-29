"""The section census must grade the tree it was ASKED about, under that tree's own names.

Two defects, both found by pointing the census at stage 5 for the first time and both
producing the same symptom — a section reported as one the tree never built, while it
sits there on disk. Measured on 124_Marketing_Restrictions/Bahamas-sonnet-nocounts at
stage 5: 8 of 15 promised sections reported MISSING, every one of them present.

  1. compute_heading_hierarchy resolved its tree with a hardcoded stage 3, so the
     post-AI scorecard's only "is this section here at all?" signal answered for a
     document that stage 4 and stage 5 had already replaced.

  2. subchunk.py renames as it restructures. A section that gained sub-chunks becomes a
     DIRECTORY holding 00-section.md (not README.md, which was the only index name the
     census knew), and the appendices take a letter prefix — 13-disclaimers-open-ended-
     fund becomes A1-disclaimers-open-ended-fund — so that letters sort after digits and
     document order survives. _slug_key stripped a leading NUMBER only, and did it after
     punctuation was already gone, by which point "A1-disclaimers" is "a1disclaimers"
     and the prefix can no longer be separated from the title.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_heading_hierarchy import _slug_key, _tree_nodes  # noqa: E402


# ---- 1. the tree a section's node is looked for in ----------------------------
def test_a_subchunk_directory_counts_as_its_sections_node(tmp_path):
    """05_subchunks/07-marketing.../00-section.md holds the section heading and intro,
    with the sub-section files beside it. Before this, nothing in that directory
    represented the section and the whole clause read as never built."""
    d = tmp_path / "07-marketing-selling-to-the-public"
    d.mkdir()
    (d / "00-section.md").write_text("# 7 Marketing/Selling to the Public\n")
    (d / "01-6.1-mutual-recognition-of-funds.md").write_text("### 6.1 Mutual Recognition\n")
    names = {(n["kind"], n["name"]) for n in _tree_nodes(tmp_path)}
    assert ("dir", "07-marketing-selling-to-the-public") in names
    assert ("file", "01-6.1-mutual-recognition-of-funds") in names


def test_a_readme_still_represents_its_directory(tmp_path):
    """The pre-existing convention must keep working — this widened the rule, it did
    not replace it."""
    d = tmp_path / "04-aggregation"
    d.mkdir()
    (d / "README.md").write_text("# 4 Aggregation\n")
    assert ("dir", "04-aggregation") in {(n["kind"], n["name"]) for n in _tree_nodes(tmp_path)}


def test_a_top_level_index_file_is_not_a_section_of_its_own(tmp_path):
    """At the root there is no directory for it to stand for."""
    (tmp_path / "README.md").write_text("# Contents\n")
    (tmp_path / "01-background.md").write_text("# 1 Background\n")
    assert [n["name"] for n in _tree_nodes(tmp_path)] == ["01-background"]


def test_generated_reports_are_never_nodes(tmp_path):
    d = tmp_path / "02-scope"
    d.mkdir()
    (d / "STAGE4_REPORT.md").write_text("run log\n")
    (d / "00-section.md").write_text("# 2 Scope\n")
    kinds = {(n["kind"], n["name"]) for n in _tree_nodes(tmp_path)}
    assert kinds == {("dir", "02-scope")}


# ---- 2. matching an outline title to a renamed node ---------------------------
def test_the_appendix_letter_prefix_does_not_break_the_match():
    assert _slug_key("A1-disclaimers-open-ended-fund") == _slug_key("Disclaimers: Open-Ended Fund")
    assert _slug_key("A1-disclaimers-open-ended-fund") == _slug_key("13-disclaimers-open-ended-fund")


def test_the_ordinary_numeric_prefix_still_does_not_break_the_match():
    assert _slug_key("06-marketing-selling-to-the-public") == _slug_key(
        "6 Marketing/Selling to the Public")
    assert _slug_key("04-aggregation") == _slug_key("4. Aggregation")


def test_a_number_that_is_part_of_the_title_is_not_stripped():
    """The prefix rule needs a separator to bound it, so a title whose own words begin
    with a number keeps them — 'Appendix 1 Forms' must not collapse to 'appendixforms'."""
    assert _slug_key("Appendix 1 Forms") == "appendix1forms"


def test_two_genuinely_different_sections_still_do_not_match():
    assert _slug_key("10-licence") != _slug_key("11-penalties-sanctions")
    assert _slug_key("A1-disclaimers-open-ended-fund") != _slug_key(
        "A2-disclaimers-closed-ended-fund")
