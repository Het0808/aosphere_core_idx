"""A section's table filed under the PRECEDING section, with the real section left as snapshots.

Spain__176285, Norway__171186 and Netherlands__156999 share one extraction defect: the
table that opens "10 LICENCE" starts on the page where section 9 ends, MinerU returns it
with its "10. LICENCE" title row as the first row, and the whole table is written at the
end of "9 PROSPECTUS REGULATION". "10 LICENCE" is left holding only page snapshots.

Nothing noticed, for two separate reasons, each pinned here:

  * table_placement compared a table's PAGE with the section that page belongs to. Page 125
    belongs to section 10, so it passed, whichever FILE the table was written into.
  * the hollow-section test skips a section that declares its pages as snapshots (the
    pipeline saying "I could not extract this"), which is wrong exactly when the content
    WAS extracted and is sitting in another file.
"""
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_content_localized import compute_content_localized  # noqa: E402
from check_table_placement import _tree_chunks, _tree_flags  # noqa: E402

BODY = " ".join(f"licensing{i} requirement{i} applies{i} to{i} marketing{i}" for i in range(80))


def _badge(tid, p1, p2):
    return f"**⚙ MinerU-extracted table** — {tid}, pages {p1}–{p2}, 26 rows × 12 cols\n\n"


def _tree(root, stage="03_stage3_final", files=None):
    d = root / stage
    d.mkdir(parents=True, exist_ok=True)
    for name, text in (files or {}).items():
        path = d / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return d


def _section(title, pages, body=""):
    rng = f"{pages[0]}–{pages[1]}" if pages[0] != pages[1] else f"{pages[0]}"
    return f"# {title}\n\n*Source: `source.pdf`, page {rng}*\n\n{body}\n"


# ---------------------------------------------------------------- table placement
def test_a_table_running_past_its_chunk_is_flagged_and_its_home_named(tmp_path):
    _tree(tmp_path, files={
        "10-prospectus.md": _section("9 PROSPECTUS REGULATION (PR3)", (122, 125),
                                     _badge("table_010", 125, 131) + "<table><tr><td>x</td></tr></table>"),
        "11-licence.md": _section("10 LICENCE", (125, 132), "snapshot"),
    })
    (flag,) = _tree_flags(tmp_path, 3, set())
    assert flag["table_id"] == "table_010"
    assert flag["file"] == "10-prospectus.md"
    assert flag["expected_section"] == "11-licence.md"
    assert flag["pages"] == list(range(125, 132))


def test_a_table_inside_its_chunk_is_not_flagged(tmp_path):
    _tree(tmp_path, files={
        "10-licence.md": _section("10 LICENCE", (125, 132), _badge("table_010", 125, 131) + "<table/>"),
    })
    assert _tree_flags(tmp_path, 3, set()) == []


def test_one_page_of_spill_is_tolerated(tmp_path):
    _tree(tmp_path, files={
        "09-pr3.md": _section("9 PR3", (122, 125), _badge("table_009", 124, 126) + "<table/>"),
    })
    assert _tree_flags(tmp_path, 3, set()) == []


def test_a_subsection_file_carrying_a_section_wide_table_is_not_flagged(tmp_path):
    # Stage 5 splits a section into subsection files; each carries the section's table
    # badge, so each is held to the range of the whole folder.
    _tree(tmp_path, "05_subchunks", files={
        "06-public/00-section.md": _section("6 PUBLIC", (21, 49), ""),
        "06-public/01-6.1-a.md": _section("6.1 A", (21, 22), _badge("table_007", 21, 49) + "<table/>"),
        "06-public/02-6.2-b.md": _section("6.2 B", (22, 34), _badge("table_007", 21, 49) + "<table/>"),
    })
    assert _tree_flags(tmp_path, 5, set()) == []


def test_a_table_the_first_pass_already_flagged_is_not_flagged_twice(tmp_path):
    _tree(tmp_path, files={
        "10-prospectus.md": _section("9 PR3", (122, 125), _badge("table_010", 125, 131) + "<table/>"),
    })
    assert _tree_flags(tmp_path, 3, {"table_010"}) == []


def test_an_unscored_stage_is_skipped_not_an_error(tmp_path):
    assert _tree_flags(tmp_path, 5, set()) == []


def test_chunks_without_a_page_range_are_never_judged(tmp_path):
    _tree(tmp_path, files={"x.md": "# X\n\n" + _badge("table_001", 1, 40) + "<table/>"})
    assert _tree_flags(tmp_path, 3, set()) == []
    assert _tree_chunks(tmp_path / "03_stage3_final")[0]["range"] is None


# ---------------------------------------------------------------- hollow, snapshot exemption
def _pdf(path, with_body=True):
    doc = fitz.open()
    p1 = doc.new_page(width=600, height=800)
    p1.insert_textbox((40, 40, 560, 300), "9 PROSPECTUS REGULATION\n\nThe prospectus regulation applies.", fontsize=10)
    p2 = doc.new_page(width=600, height=800)
    p2.insert_textbox((40, 40, 560, 780), "10 LICENCE\n\n" + (BODY if with_body else "."), fontsize=7)
    doc.save(path)
    doc.close()


SNAPSHOT = ("> Page 2 of the source PDF contains a complex table/diagram; snapshot for reference:\n\n"
            "![Page 2](_assets/page-002.png)")


def _hollow(tmp_path, files):
    _tree(tmp_path, files=files)
    return compute_content_localized(tmp_path, stage=3)["hollow_sections"]


def test_snapshots_with_the_body_filed_in_another_file_are_hollow(tmp_path):
    _pdf(tmp_path / "source.pdf")
    got = _hollow(tmp_path, {
        "01-prospectus.md": _section("9 PROSPECTUS REGULATION", (1, 2),
                                     "The prospectus regulation applies.\n\n10 LICENCE\n\n" + BODY),
        "02-licence.md": _section("10 LICENCE", (2, 2), SNAPSHOT),
    })
    assert [h["file"] for h in got] == ["02-licence.md"]
    assert got[0]["absorbed_by"] == ["01-prospectus.md"]


def test_snapshots_whose_content_is_nowhere_in_the_tree_stay_exempt(tmp_path):
    # The pipeline said it could not extract the page, and the text really is gone from
    # the tree: that is loss, counted elsewhere, not a section filed under the wrong heading.
    _pdf(tmp_path / "source.pdf")
    got = _hollow(tmp_path, {
        "01-prospectus.md": _section("9 PROSPECTUS REGULATION", (1, 1), "The prospectus regulation applies."),
        "02-licence.md": _section("10 LICENCE", (2, 2), SNAPSHOT),
    })
    assert got == []


def test_a_section_that_kept_its_own_body_is_not_hollow(tmp_path):
    _pdf(tmp_path / "source.pdf")
    got = _hollow(tmp_path, {
        "01-prospectus.md": _section("9 PROSPECTUS REGULATION", (1, 1), "The prospectus regulation applies."),
        "02-licence.md": _section("10 LICENCE", (2, 2), BODY),
    })
    assert got == []
