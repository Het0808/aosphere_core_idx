"""Doc Gallery's "Browse run" over a LOCAL corpus (ACI_LOCAL_CORPUS_DIR) instead of S3.

Regression this covers: store(run, version) only ever knew one way to resolve a `run` —
RunDocStore, which requires ACI_EXTRACTION_BUCKET. Once the Extraction tab started
offering a "local" run (the synthetic run_corpus.py id, see local_extraction.LOCAL_RUN_ID),
clicking "Browse Run" — and, via the `_galRun` global the Doc Gallery JS carries across
tab switches, plain Doc Gallery too — called /api/doc-gallery/tree?run=local, which fell
into that S3-only branch and 503'd with "extraction runs are not configured (set
ACI_EXTRACTION_BUCKET)" on every local setup, no matter how the corpus was configured.

The two properties under test are the ones a partial fix would get wrong:
  * local wins over S3 for this one synthetic run id, even when a bucket IS set (priority) —
    but a real S3 run id is completely unaffected, still requires the bucket it always did.
  * a document with no scorecard yet is a 404 for one slug, not a 503 for the whole run.
"""
import json

import pytest

from aosphere_core_index.service import doc_gallery as G
from aosphere_core_index.service import local_extraction as lx


def _job(root, product, label, *, jurisdiction, doc_id, gate="pass", worst=95.0):
    d = root / product / label
    d.mkdir(parents=True)
    (d / "corpus_meta.json").write_text(
        json.dumps({"product": product, "jurisdiction": jurisdiction, "doc_id": doc_id}))
    (d / "scorecard.json").write_text(json.dumps({
        "gate": gate, "worst_score": worst, "weakest_dimension": "completeness",
        "dimensions": {"completeness": {"label": "Completeness", "score": worst,
                                        "critical": False}},
        "pages": {"total": 10, "counts": {}}, "headings": {"matched": 5, "total": 5},
        "tables": [], "active_finding_count": 0,
    }))
    return d


@pytest.fixture(autouse=True)
def _clear_store_caches():
    # store() memoises by run/version id at module scope; a leftover S3 fixture (or a
    # previous local root) from another test must not leak into this one.
    G._run_stores.clear()
    G._version_stores.clear()
    yield
    G._run_stores.clear()
    G._version_stores.clear()


def test_local_run_wins_over_s3_and_returns_real_data(tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    monkeypatch.setenv("ACI_EXTRACTION_BUCKET", "should-be-ignored-for-the-local-run-id")
    _job(tmp_path, "104_Shareholding_Disclosure", "Argentina__172099",
        jurisdiction="Argentina", doc_id="172099", gate="pass", worst=98.0)
    _job(tmp_path, "104_Shareholding_Disclosure", "India__172436",
        jurisdiction="India", doc_id="172436", gate="review", worst=84.0)

    st = G.store(run=lx.LOCAL_RUN_ID)
    assert isinstance(st, G.LocalRunDocStore)
    man = st.manifest()
    assert {m["product"] for m in man} == {"104_Shareholding_Disclosure"}
    assert {m["gate"] for m in man} == {"pass", "review"}

    tree = G.gallery_tree(run=lx.LOCAL_RUN_ID)
    assert tree["totals"]["documents"] == 2
    assert tree["totals"]["pass"] == 1 and tree["totals"]["review"] == 1


def test_local_scorecard_endpoint_returns_real_json_not_503(tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    monkeypatch.delenv("ACI_EXTRACTION_BUCKET", raising=False)
    _job(tmp_path, "P", "Argentina__1", jurisdiction="Argentina", doc_id="1", gate="pass", worst=99.0)

    slug = G._run_slug("P", "Argentina__1")
    sc = G.scorecard_json(slug, run=lx.LOCAL_RUN_ID)
    assert sc is not None and sc["gate"] == "pass" and sc["worst_score"] == 99.0


def test_local_document_with_no_scorecard_yet_is_absent_not_503(tmp_path, monkeypatch):
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    d = tmp_path / "P" / "A__1"
    d.mkdir(parents=True)
    (d / "corpus_meta.json").write_text(json.dumps({"product": "P", "jurisdiction": "A",
                                                     "doc_id": "1"}))
    # No scorecard.json yet -- still mid-pipeline, so it must not appear as a gallery row
    # (a genuinely missing scorecard for a KNOWN slug is a 404, covered via scorecard_json
    # returning None below; a job with none at all is simply not in the manifest).
    assert G.store(run=lx.LOCAL_RUN_ID).manifest() == []
    assert G.gallery_list(run=lx.LOCAL_RUN_ID) == []
    assert G.scorecard_json("anything", run=lx.LOCAL_RUN_ID) is None


def test_s3_run_is_completely_unaffected_when_local_mode_is_unconfigured(monkeypatch):
    monkeypatch.delenv("ACI_LOCAL_CORPUS_DIR", raising=False)
    monkeypatch.delenv("ACI_EXTRACTION_BUCKET", raising=False)
    with pytest.raises(LookupError, match="ACI_EXTRACTION_BUCKET"):
        G.store(run="2026-08-21-02")


def test_a_real_s3_run_id_is_unaffected_even_when_local_mode_is_also_configured(
        tmp_path, monkeypatch):
    """Local mode only ever answers for its OWN synthetic run id -- never inferred from
    "a run id was asked for", so a genuine S3 run alongside a configured local corpus
    still needs its own bucket, exactly as before."""
    monkeypatch.setenv("ACI_LOCAL_CORPUS_DIR", str(tmp_path))
    monkeypatch.delenv("ACI_EXTRACTION_BUCKET", raising=False)
    with pytest.raises(LookupError, match="ACI_EXTRACTION_BUCKET"):
        G.store(run="2026-08-21-02")
