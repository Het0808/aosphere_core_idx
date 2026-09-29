"""Local filesystem extraction monitoring — the Extraction tab without S3.

Mirrors what test_extraction_monitor.py / test_extraction_jobs.py verify about the S3 side,
against a plain directory tree instead of ReadOnlyS3: which stage each document reached is
read purely from which files exist, never inferred from a directory merely existing, and a
document can be read while still mid-pipeline, not only once it has a scorecard.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from aosphere_core_index.service import local_extraction as lx


def _write(root: Path, product: str, label: str, *, jurisdiction="Argentina", doc_id="172099",
          stage1=False, stage2=False, stage3=False, validation=False, scorecard=None,
          started_ago=0.0):
    d = root / product / label
    d.mkdir(parents=True, exist_ok=True)
    meta = {"product": product, "jurisdiction": jurisdiction, "doc_id": doc_id}
    (d / "corpus_meta.json").write_text(json.dumps(meta))
    if started_ago:
        import os
        t = time.time() - started_ago
        os.utime(d / "corpus_meta.json", (t, t))
    if stage1:
        (d / "01_stage1_extract").mkdir(exist_ok=True)
        (d / "01_stage1_extract" / "tables_manifest.json").write_text("{}")
    if stage2:
        s2 = d / "02_stage2_mineru_tables"
        s2.mkdir(exist_ok=True)
        (s2 / "stage2_report.json").write_text(json.dumps({"tables_attempted": 3}))
    if stage3:
        s3 = d / "03_stage3_final"
        s3.mkdir(exist_ok=True)
        (s3 / "stage3_report.json").write_text(json.dumps({"tables_total": 3}))
    if validation:
        (d / "validation.json").write_text("{}")
    if scorecard is not None:
        (d / "scorecard.json").write_text(json.dumps(scorecard))
    return d


def _sc(gate="pass", worst=95.0, **extra):
    return {"gate": gate, "worst_score": worst,
            "timing": {"seconds": 12.3, "steps": {"stage1": 1.0}, "pages": 10}, **extra}


# ---- configured_root ---------------------------------------------------------------------

def test_not_configured_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("ACI_LOCAL_CORPUS_DIR", raising=False)
    assert lx.configured_root() is None


def test_configured_root_resolves_the_path(monkeypatch, tmp_path):
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    assert lx.configured_root() == tmp_path.resolve()


def test_invalid_corpus_path_is_not_a_crash(monkeypatch, tmp_path):
    """A configured path that does not exist yet (a run about to start) must read as an
    empty, valid corpus -- not raise."""
    missing = tmp_path / "does-not-exist-yet"
    assert lx.discover_jobs(missing) == []
    assert lx.list_runs(missing) == []
    p = lx.progress(missing)
    assert p["jobs_total"] == 0 and p["state"] == "idle"


def test_empty_corpus_reports_zero_documents(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    assert lx.discover_jobs(tmp_path) == []
    runs = lx.list_runs(tmp_path)
    assert runs and runs[0]["documents"] == 0


# ---- stage discovery ----------------------------------------------------------------------

def test_stage1_only_extraction_reads_as_stage2(tmp_path):
    """No manifest yet at all -> still in stage1; manifest present -> currently entering
    stage2 (the next thing this document has not finished)."""
    _write(tmp_path, "104_Shareholding_Disclosure", "Argentina__172099")
    jobs = lx.discover_jobs(tmp_path)
    assert jobs[0]["stage_idx"] == 1 and jobs[0]["status"] == "running"

    _write(tmp_path, "104_Shareholding_Disclosure", "Argentina__172099", stage1=True)
    jobs = lx.discover_jobs(tmp_path)
    assert jobs[0]["stage_idx"] == 2


def test_full_stage1_mineru_stage3_extraction(tmp_path):
    d = _write(tmp_path, "124_Marketing_Restrictions_-_Asset_Management", "Japan__172122",
              stage1=True, stage2=True, stage3=True, validation=True,
              scorecard=_sc(gate="pass", worst=93.3))
    jobs = lx.discover_jobs(tmp_path)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["stage_idx"] == lx.GATE_INDEX
    assert j["status"] == "pass" and j["gate"] == "pass" and j["worst_score"] == 93.3
    assert j["gate_extraction"] == "pass" and j["worst_extraction"] == 93.3
    assert j["scored_stage"] == 3               # never a post-AI verdict locally
    assert j["tables"] == 3 and j["mineru_pages"] == 3
    assert d.exists()


def test_missing_scorecard_reads_as_in_progress(tmp_path):
    _write(tmp_path, "104_Shareholding_Disclosure", "Austria__180854",
          stage1=True, stage2=True, stage3=True, validation=True)
    j = lx.discover_jobs(tmp_path)[0]
    assert j["gate"] is None and j["status"] == "running" and j["live"] is True


def test_missing_stage_directory_does_not_crash_and_reads_as_earlier_stage(tmp_path):
    # Stage 2 present, but 03_stage3_final was never created (e.g. a crashed run).
    _write(tmp_path, "104_Shareholding_Disclosure", "India__169716", stage1=True, stage2=True)
    j = lx.discover_jobs(tmp_path)[0]
    assert j["stage_idx"] == 3
    assert (tmp_path / "104_Shareholding_Disclosure" / "India__169716"
           / "03_stage3_final").exists() is False


def test_directory_without_corpus_meta_is_not_a_job(tmp_path):
    (tmp_path / "104_Shareholding_Disclosure" / "not_a_job_dir").mkdir(parents=True)
    assert lx.discover_jobs(tmp_path) == []


# ---- multiple products / jurisdictions / doc ids -------------------------------------------

def test_multiple_products_and_jurisdictions_and_doc_ids(tmp_path):
    _write(tmp_path, "104_Shareholding_Disclosure", "Argentina__172099",
          scorecard=_sc(gate="pass"))
    _write(tmp_path, "104_Shareholding_Disclosure", "India__172436", doc_id="172436",
          scorecard=_sc(gate="review", worst=84.0))
    _write(tmp_path, "104_Shareholding_Disclosure", "India__169716", doc_id="169716",
          stage1=True)
    _write(tmp_path, "124_Marketing_Restrictions_-_Asset_Management", "Turkey__166449",
          jurisdiction="Turkey", doc_id="166449", scorecard=_sc(gate="fail", worst=40.0))

    jobs = lx.discover_jobs(tmp_path)
    assert len(jobs) == 4
    products = {j["product"] for j in jobs}
    assert products == {"104_Shareholding_Disclosure",
                        "124_Marketing_Restrictions_-_Asset_Management"}
    labels = {j["label"] for j in jobs}
    assert labels == {"Argentina__172099", "India__172436", "India__169716", "Turkey__166449"}

    jv = lx.jobs_view(tmp_path)
    assert jv["total"] == 4 and jv["ledger"] is True
    assert jv["funnel"][lx.GATE_INDEX]["count"] == 3      # 3 scored, 1 still mid-pipeline


# ---- progress() aggregation -----------------------------------------------------------------

def test_progress_state_complete_when_every_job_is_scored(tmp_path):
    _write(tmp_path, "104_Shareholding_Disclosure", "Argentina__172099",
          scorecard=_sc(gate="pass"))
    p = lx.progress(tmp_path)
    assert p["state"] == "complete" and p["done"] == 1 and p["jobs_total"] == 1
    assert p["local"] is True and p["shards"] == []


def test_progress_gates_tally_by_verdict(tmp_path):
    _write(tmp_path, "P", "A__1", scorecard=_sc(gate="pass"))
    _write(tmp_path, "P", "A__2", doc_id="2", scorecard=_sc(gate="review"))
    _write(tmp_path, "P", "A__3", doc_id="3", scorecard=_sc(gate="pass"))
    p = lx.progress(tmp_path)
    assert p["gates"] == {"pass": 2, "review": 1}


# ---- list_runs -------------------------------------------------------------------------------

def test_list_runs_returns_one_synthetic_run(tmp_path):
    _write(tmp_path, "P", "A__1", scorecard=_sc())
    runs = lx.list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0]["run"] == lx.LOCAL_RUN_ID
    assert runs[0]["local"] is True
    assert runs[0]["documents"] == 1


# ---- job_detail + traversal guard -------------------------------------------------------------

def test_job_detail_works_before_and_after_scoring(tmp_path):
    _write(tmp_path, "P", "A__1", stage1=True)
    detail = lx.job_detail(tmp_path, "P", "A__1")
    assert detail is not None and detail["gate"] is None

    _write(tmp_path, "P", "A__1", stage1=True, scorecard=_sc(gate="pass", worst=97.0))
    detail = lx.job_detail(tmp_path, "P", "A__1")
    assert detail["gate"] == "pass" and detail["worst_score"] == 97.0


def test_job_detail_missing_document_returns_none(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    assert lx.job_detail(tmp_path, "P", "nope") is None


@pytest.mark.parametrize("product,label", [
    ("../escape", "A__1"), ("P", "../../escape"), ("P", "a/b"), ("..", ".."), ("P", "."),
])
def test_job_detail_rejects_path_traversal(tmp_path, product, label):
    _write(tmp_path, "P", "A__1", scorecard=_sc())
    assert lx.job_detail(tmp_path, product, label) is None


def test_read_artifact_returns_table_html_with_structure_intact(tmp_path):
    d = _write(tmp_path, "P", "A__1", stage2=True)
    table_dir = d / "02_stage2_mineru_tables" / "tables" / "table_001"
    table_dir.mkdir(parents=True)
    html = "<table><tr><td rowspan=\"2\">x</td><td colspan=\"3\">y</td></tr></table>"
    (table_dir / "table.html").write_text(html)

    got = lx.read_artifact(tmp_path, "P", "A__1", "02_stage2_mineru_tables/tables/table_001/table.html")
    assert got is not None
    content, content_type = got
    assert content.decode() == html                # not flattened to plain text
    assert "rowspan" in content.decode()
    assert content_type.startswith("text/html")


@pytest.mark.parametrize("bad_path", [
    "../../../etc/passwd", "01_stage1_extract/../../../escape", "mineru_raw/secret.pdf",
    "scorecard.json/../escape",
])
def test_read_artifact_rejects_traversal_and_disallowed_files(tmp_path, bad_path):
    _write(tmp_path, "P", "A__1", stage1=True)
    assert lx.read_artifact(tmp_path, "P", "A__1", bad_path) is None


def test_read_artifact_allows_top_level_scorecard(tmp_path):
    _write(tmp_path, "P", "A__1", scorecard=_sc(gate="pass"))
    got = lx.read_artifact(tmp_path, "P", "A__1", "scorecard.json")
    assert got is not None
    content, content_type = got
    assert json.loads(content)["gate"] == "pass"
    assert content_type == "application/json"


# ---- Stage 4/5 (post-AI) — a genuinely post-AI scorecard wins over the extraction gate ------

def test_post_ai_scorecard_wins_over_extraction_gate(tmp_path):
    d = _write(tmp_path, "P", "A__1", stage1=True, stage2=True, stage3=True,
              scorecard=_sc(gate="review", worst=84.0))
    (d / "scorecard_post_ai.json").write_text(json.dumps(_sc(gate="pass", worst=94.6)))
    (d / "04_stage4_ai").mkdir()
    (d / "04_stage4_ai" / "stage4_report.json").write_text(
        json.dumps({"usage": {"cost_usd": 1.23}}))
    j = lx.discover_jobs(tmp_path)[0]
    assert j["gate"] == "pass" and j["worst_score"] == 94.6         # SC2 is the verdict shown
    assert j["scored_stage"] == 5
    assert j["gate_extraction"] == "review" and j["worst_extraction"] == 84.0  # SC1 preserved
    assert j["cost_usd"] == 1.23
    assert j["stage4_status"] == "ok"


def test_post_ai_scorecard_with_subchunks_reaches_the_last_stage(tmp_path):
    d = _write(tmp_path, "P", "A__1", stage1=True, stage2=True, stage3=True,
              scorecard=_sc(gate="pass"))
    (d / "scorecard_post_ai.json").write_text(json.dumps(_sc(gate="pass", worst=97.0)))
    (d / "05_subchunks").mkdir()
    j = lx.discover_jobs(tmp_path)[0]
    assert j["stage_idx"] == 7                     # stage5_subchunk, the last real stage


def test_post_ai_scorecard_with_no_gate_yet_does_not_override(tmp_path):
    """scorecard_post_ai.json can exist mid-write (gate not yet decided) -- must not be
    preferred over a genuine, already-scored extraction gate."""
    d = _write(tmp_path, "P", "A__1", stage1=True, stage2=True, stage3=True,
              scorecard=_sc(gate="pass", worst=91.0))
    (d / "scorecard_post_ai.json").write_text(json.dumps({"gate": None}))
    j = lx.discover_jobs(tmp_path)[0]
    assert j["gate"] == "pass" and j["worst_score"] == 91.0
    assert j["scored_stage"] == 3


def test_job_detail_exposes_extraction_gate_alongside_post_ai_verdict(tmp_path):
    d = _write(tmp_path, "P", "A__1", stage1=True, stage2=True, stage3=True,
              scorecard=_sc(gate="review", worst=84.0))
    (d / "scorecard_post_ai.json").write_text(json.dumps(_sc(gate="pass", worst=94.6)))
    detail = lx.job_detail(tmp_path, "P", "A__1")
    assert detail["gate"] == "pass" and detail["scored_stage"] == 5
    assert detail["extraction_gate"] == {"gate": "review", "worst_score": 84.0}


def test_read_artifact_allows_stage4_and_stage5_dirs(tmp_path):
    d = _write(tmp_path, "P", "A__1")
    (d / "04_stage4_ai").mkdir()
    (d / "04_stage4_ai" / "01-intro.md").write_text("# hi")
    (d / "05_subchunks").mkdir()
    (d / "05_subchunks" / "01-a.md").write_text("chunk")
    assert lx.read_artifact(tmp_path, "P", "A__1", "04_stage4_ai/01-intro.md") is not None
    assert lx.read_artifact(tmp_path, "P", "A__1", "05_subchunks/01-a.md") is not None
