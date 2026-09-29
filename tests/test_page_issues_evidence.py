"""_build_page_issues (hybrid_extract_ui.py) cites the flagged text, not just its
category label.

check_source_fidelity.py's cell-level findings (section_boundary_leak,
duplicated_content, ...) carry the actual flagged text in evidence.text -- the one
thing a reader paging through the PDF needs to find the row a generic label like
"duplicated content" cannot point to. Before this, the Page Review card showed only
the category and a generic sentence, with the specific text existing only in the
raw finding JSON.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hybrid_extract_ui as UI


@pytest.fixture
def job_dir(tmp_path, monkeypatch):
    d = tmp_path / "job"
    (d / "03_stage3_final").mkdir(parents=True)
    (d / "03_stage3_final" / "one.md").write_text("# One\n\n*Source: `x.pdf`, page 1*\nbody")
    return d


def _scorecard_with(finding):
    return {"scored_stage": 3, "findings": [finding]}


def test_evidence_text_is_quoted_in_the_page_card(job_dir, monkeypatch):
    finding = {
        "kind": "duplicated_content", "dimension": "source_fidelity",
        "severity": "review", "file": "10-licence.md", "pages": [89],
        "title": "duplicated content",
        "detail": "A substantial table cell repeats in different chunks, but its "
                  "opening passage occurs once in the PDF.",
        "evidence": {"text": "in our view marketing activities relating to new "
                             "investments would need to be viewed conservatively"},
    }
    monkeypatch.setattr(UI, "_fresh_scorecard", lambda j, view="extraction": _scorecard_with(finding))
    result = UI._build_page_issues({"root": str(job_dir)}, view="extraction")
    issues = result["page_issues"]["89"]
    assert len(issues) == 1
    assert "in our view marketing activities" in issues[0]["detail"]
    assert "A substantial table cell repeats" in issues[0]["detail"]   # category kept, not replaced


def test_long_evidence_text_is_capped_not_dumped_whole(job_dir, monkeypatch):
    long_text = "word " * 200
    finding = {"kind": "section_boundary_leak", "dimension": "source_fidelity",
              "severity": "review", "file": "a.md", "pages": [5],
              "detail": "This cell precedes the chunk's heading in the source PDF.",
              "evidence": {"text": long_text}}
    monkeypatch.setattr(UI, "_fresh_scorecard", lambda j, view="extraction": _scorecard_with(finding))
    result = UI._build_page_issues({"root": str(job_dir)}, view="extraction")
    detail = result["page_issues"]["5"][0]["detail"]
    assert len(detail) < len(long_text)
    assert detail.rstrip('"').endswith("…")


def test_a_finding_with_no_evidence_text_is_unaffected(job_dir, monkeypatch):
    finding = {"kind": "inconsistent_table_columns", "dimension": "source_fidelity",
              "severity": "review", "file": "a.md", "pages": [5],
              "detail": "Rows have different logical column counts.", "evidence": {}}
    monkeypatch.setattr(UI, "_fresh_scorecard", lambda j, view="extraction": _scorecard_with(finding))
    result = UI._build_page_issues({"root": str(job_dir)}, view="extraction")
    detail = result["page_issues"]["5"][0]["detail"]
    assert detail == "a.md — Rows have different logical column counts."
    assert '"' not in detail
