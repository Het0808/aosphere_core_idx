"""pdf2mdtree's synthetic "Front Matter" is not a heading the document lost.

It is inserted by pdf2mdtree (`matched.insert(0, {'level': 1, 'title': 'Front Matter'...})`)
so paragraphs printed before the first recognized heading are kept rather than dropped. No
page prints it. Counting it as an outline heading made it a permanent, unanswerable entry
in the coverage rate — 14 points of a 7-entry outline on the ELTIF supplementals, now that
sectioning is scored on that rate.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from compare_stage_completeness import heading_coverage  # noqa: E402


def _job(tmp_path: Path, outline: list[dict], tree_headings: list[str]) -> Path:
    root = tmp_path / "job"
    (root / "01_stage1_extract").mkdir(parents=True)
    (root / "01_stage1_extract" / "headings_manifest.json").write_text(
        json.dumps({"headings": outline}), encoding="utf-8")
    for stage in ("03_stage3_final", "05_subchunks"):
        (root / stage).mkdir()
        (root / stage / "01-doc.md").write_text(
            "\n\n".join(f"# {h}" for h in tree_headings), encoding="utf-8")
    return root


OUTLINE = [{"level": 1, "title": "Front Matter", "page": 1},
           {"level": 1, "title": "1 BACKGROUND", "page": 3},
           {"level": 2, "title": "1.1 Introduction", "page": 3}]


def test_front_matter_is_not_counted_as_an_outline_heading(tmp_path):
    root = _job(tmp_path, OUTLINE, ["1 BACKGROUND", "1.1 Introduction"])
    d = heading_coverage(root, 3, 5)
    assert d["outline_count"] == 2
    assert d["missing_at_to"] == []


def test_a_real_heading_beside_it_is_still_reported(tmp_path):
    """Sweden 138020: stage 3 carries "1.1 Introduction", the shipped tree does not."""
    root = _job(tmp_path, OUTLINE, ["1 BACKGROUND"])
    d = heading_coverage(root, 3, 5)
    assert [m["title"] for m in d["missing_at_to"]] == ["1.1 Introduction"]
    assert d["outline_count"] == 2
