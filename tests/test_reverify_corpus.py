"""reverify_corpus must not re-score summary-AI jobs against a stage-3 tree they lack."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import reverify_corpus as rc  # noqa: E402


def test_summary_ai_job_detected_by_rule_marker(tmp_path):
    (tmp_path / "summary_ai_rule.json").write_text("{}")
    assert rc.is_summary_ai(tmp_path)


def test_summary_ai_job_detected_by_missing_stage3_with_stage4(tmp_path):
    (tmp_path / "04_stage4_ai").mkdir()
    assert rc.is_summary_ai(tmp_path)


def test_normal_job_with_stage3_is_not_summary_ai(tmp_path):
    (tmp_path / "03_stage3_final").mkdir()
    (tmp_path / "04_stage4_ai").mkdir()
    assert not rc.is_summary_ai(tmp_path)
