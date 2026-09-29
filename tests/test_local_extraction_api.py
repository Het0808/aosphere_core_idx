"""The /api/extraction/* endpoints' local/S3 mode switch.

Priority is: local filesystem when ACI_LOCAL_CORPUS_DIR names a directory, else S3 when
ACI_EXTRACTION_BUCKET is set, else "not configured" — decided fresh on every request (no
cached mode flag), so flipping the env var between tests is enough to prove each branch
without touching the other's code path. The property under test that matters most: local
mode must never construct a ReadOnlyS3 or otherwise touch AWS, and S3 mode must be
completely unaffected by ACI_LOCAL_CORPUS_DIR being unset.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("numpy")

from fastapi.testclient import TestClient  # noqa: E402

import aosphere_core_index.service.app as A  # noqa: E402
from aosphere_core_index.service import extraction_monitor as em  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACI_AUTH_ENABLED", "0")
    monkeypatch.delenv("ACI_LOCAL_CORPUS_DIR", raising=False)
    monkeypatch.delenv("ACI_EXTRACTION_BUCKET", raising=False)


@pytest.fixture
def client():
    return TestClient(A.app)


def _job(root, product, label, *, doc_id="1", scorecard=None):
    d = root / product / label
    d.mkdir(parents=True)
    (d / "corpus_meta.json").write_text(
        json.dumps({"product": product, "jurisdiction": "X", "doc_id": doc_id}))
    if scorecard is not None:
        (d / "scorecard.json").write_text(json.dumps(scorecard))
    return d


# ---- neither configured ----------------------------------------------------------------------

def test_neither_local_nor_s3_configured_reports_not_configured(client):
    r = client.get("/api/extraction/runs")
    assert r.status_code == 200
    assert r.json() == {"runs": [], "configured": False}


# ---- local mode wins when configured, and never touches AWS ----------------------------------

def test_local_mode_wins_and_lists_one_run(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    _job(tmp_path, "P", "A__1", scorecard={"gate": "pass", "worst_score": 99.0})
    r = client.get("/api/extraction/runs")
    assert r.status_code == 200
    d = r.json()
    assert d["configured"] is True and d["local"] is True
    assert len(d["runs"]) == 1 and d["runs"][0]["run"] == "local"


def test_local_mode_never_constructs_readonly_s3(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    monkeypatch.setenv("ACI_EXTRACTION_BUCKET", "should-be-ignored")   # local still wins
    _job(tmp_path, "P", "A__1", scorecard={"gate": "pass", "worst_score": 99.0})

    def _boom(*a, **k):
        raise AssertionError("local mode must never touch S3")
    monkeypatch.setattr("aosphere_core_index.aws.s3_readonly.ReadOnlyS3", _boom)

    for path in ("/api/extraction/runs",
                "/api/extraction/progress?run=local",
                "/api/extraction/jobs?run=local",
                "/api/extraction/job?run=local&product=P&label=A__1",
                "/api/extraction/failures?run=local",
                "/api/extraction/retries?run=local",
                "/api/extraction/review-status?run=local&product=P&label=A__1"):
        r = client.get(path)
        assert r.status_code in (200, 404), (path, r.status_code, r.text)


def test_local_jobs_and_progress_reflect_the_scorecard(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    _job(tmp_path, "P", "A__1", scorecard={"gate": "pass", "worst_score": 91.0})
    _job(tmp_path, "P", "A__2", doc_id="2")   # still mid-pipeline, no scorecard

    jobs = client.get("/api/extraction/jobs?run=local").json()
    assert jobs["total"] == 2 and jobs["local"] is True
    labels = {j["label"]: j for j in jobs["jobs"]}
    assert labels["A__1"]["gate"] == "pass"
    assert labels["A__2"]["gate"] is None and labels["A__2"]["live"] is True

    prog = client.get("/api/extraction/progress?run=local").json()
    assert prog["jobs_total"] == 2 and prog["done"] == 1


def test_local_job_detail_404_for_unknown_document(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    tmp_path.mkdir(exist_ok=True)
    r = client.get("/api/extraction/job?run=local&product=P&label=nope")
    assert r.status_code == 404


def test_local_retry_is_refused_not_crashed(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    _job(tmp_path, "P", "A__1")
    r = client.post("/api/extraction/retry",
                    json={"run": "local", "product": "P", "label": "A__1"})
    assert r.status_code == 409
    assert r.json()["queued"] is False


def test_local_artifact_endpoint_serves_raw_structured_table(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    d = _job(tmp_path, "P", "A__1")
    table_dir = d / "02_stage2_mineru_tables" / "tables" / "table_001"
    table_dir.mkdir(parents=True)
    (table_dir / "table.html").write_text('<table><tr><td colspan="2">x</td></tr></table>')

    r = client.get("/api/extraction/local-artifact",
                   params={"product": "P", "label": "A__1",
                           "path": "02_stage2_mineru_tables/tables/table_001/table.html"})
    assert r.status_code == 200
    assert "colspan" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_local_artifact_endpoint_off_when_local_mode_unconfigured(client):
    r = client.get("/api/extraction/local-artifact",
                   params={"product": "P", "label": "A__1", "path": "scorecard.json"})
    assert r.status_code == 404


# ---- S3 mode is completely unaffected ----------------------------------------------------------

def test_s3_mode_unchanged_when_local_var_is_unset(client, monkeypatch):
    monkeypatch.setenv("ACI_EXTRACTION_BUCKET", "bkt")

    def _fake_list_runs(bucket, root, region):
        assert bucket == "bkt"
        return [{"run": "2026-08-21-02", "workers_reported": 4}]
    monkeypatch.setattr(em, "list_runs", _fake_list_runs)

    r = client.get("/api/extraction/runs")
    assert r.status_code == 200
    d = r.json()
    assert d["configured"] is True and "local" not in d
    assert d["runs"] == [{"run": "2026-08-21-02", "workers_reported": 4}]


def test_s3_mode_still_503s_without_a_bucket_for_progress(client):
    r = client.get("/api/extraction/progress?run=2026-08-21-02")
    assert r.status_code == 503
