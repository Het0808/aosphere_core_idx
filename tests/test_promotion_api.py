"""The promotion endpoints: what they refuse, and how.

Refusals are weighted as heavily as successes here, because every one of them is a decision
this API is responsible for rather than the UI:

  * writes 404 when the environment has not opted in (the ACI_ADMIN_REINDEX pattern) — an
    endpoint that exists but errors is worse than one that is not there;
  * a product outside `region_map.PRODUCTS` is a 400 from the SERVER. It has no
    product-qualified region identity, so its documents can never be searched — publishing
    gallery rows for them would promise something Phase 2 cannot deliver. A UI default is not
    a policy;
  * `fail`/`error` gates need a second, explicit opt-in. "Publish the documents we already
    know are broken" should take a deliberate act;
  * "already running" is a 409 carrying a reason, never a 500 — the caller can act on it;
  * asking for a run AND a version is a 400: they are two different screens.
"""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("numpy")

from fastapi.testclient import TestClient  # noqa: E402

import aosphere_core_index.service.app as A  # noqa: E402
from aosphere_core_index.service import promotion_monitor as PM  # noqa: E402

RUN = "2026-08-21-02"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACI_AUTH_ENABLED", "0")
    monkeypatch.delenv("ACI_ADMIN_PROMOTE", raising=False)
    monkeypatch.delenv("ACI_ADMIN_PROMOTE_ANY_GATE", raising=False)
    monkeypatch.setenv("ACI_EXTRACTION_BUCKET", "bkt")
    monkeypatch.setenv("ACI_EXTRACTION_PREFIX", "corpus")


@pytest.fixture
def client():
    return TestClient(A.app)


def _stub(monkeypatch, **fns):
    for name, fn in fns.items():
        monkeypatch.setattr(PM, name, fn)


# ---------------- the env gate ----------------
def test_writes_are_absent_until_the_environment_opts_in(client):
    assert client.post("/api/promotion/request", json={"run": RUN}).status_code == 404
    assert client.post("/api/promotion/stop",
                       json={"run": RUN, "promotion": "x-p1"}).status_code == 404


def test_reads_need_no_opt_in_because_they_change_nothing(client, monkeypatch):
    _stub(monkeypatch, list_promotions=lambda *a, **k: [])
    assert client.get("/api/promotion/runs").status_code == 200


def test_the_ui_is_told_whether_the_controls_should_exist(client, monkeypatch):
    """A button that 404s is indistinguishable from a broken one."""
    assert client.get("/api/config").json()["promotion"] is False
    monkeypatch.setenv("ACI_ADMIN_PROMOTE", "1")
    assert client.get("/api/config").json()["promotion"] is True


def test_an_unconfigured_environment_says_so_rather_than_erroring(client, monkeypatch):
    monkeypatch.delenv("ACI_EXTRACTION_BUCKET", raising=False)
    monkeypatch.delenv("ACI_DOC_GALLERY_BUCKET", raising=False)
    assert client.get("/api/promotion/runs").json() == {"promotions": [], "configured": False}
    assert client.get(f"/api/promotion/plan?run={RUN}").status_code == 503
    assert client.get(
        f"/api/promotion/progress?run={RUN}&promotion=x-p1").status_code == 503


# ---------------- selection is server policy ----------------
def test_a_product_the_index_cannot_represent_is_rejected(client, monkeypatch):
    monkeypatch.setenv("ACI_ADMIN_PROMOTE", "1")
    r = client.post("/api/promotion/request",
                    json={"run": RUN, "products": ["Data Privacy (US States)"]})
    assert r.status_code == 400
    assert "cannot represent" in r.json()["detail"]


def test_the_three_indexable_products_are_accepted(client, monkeypatch):
    from aosphere_core_index.regions.region_map import PRODUCTS
    monkeypatch.setenv("ACI_ADMIN_PROMOTE", "1")
    _stub(monkeypatch, request_promotion=lambda *a, **k: {"queued": True, "promotion": "v"})
    r = client.post("/api/promotion/request", json={"run": RUN, "products": list(PRODUCTS)})
    assert r.status_code == 200


def test_a_broken_gate_needs_a_second_explicit_opt_in(client, monkeypatch):
    monkeypatch.setenv("ACI_ADMIN_PROMOTE", "1")
    r = client.post("/api/promotion/request", json={"run": RUN, "gates": ["fail"]})
    assert r.status_code == 400 and "not permitted" in r.json()["detail"]
    monkeypatch.setenv("ACI_ADMIN_PROMOTE_ANY_GATE", "1")
    _stub(monkeypatch, request_promotion=lambda *a, **k: {"queued": True, "promotion": "v"})
    assert client.post("/api/promotion/request",
                       json={"run": RUN, "gates": ["fail"]}).status_code == 200


def test_the_default_gates_match_the_monitors_and_cannot_drift():
    """app.py carries the tuple as a literal because PromoteRequest needs it at
    class-definition time, so this is what keeps the two honest."""
    assert tuple(A._PROMOTE_GATES_DEFAULT) == tuple(PM.GATES_DEFAULT) == ("pass", "review")


def test_the_default_selection_is_every_indexable_product_and_pass_plus_review(monkeypatch):
    got = {}
    monkeypatch.setenv("ACI_ADMIN_PROMOTE", "1")

    def capture(*a, **k):
        got.update(k)
        return {"queued": True, "promotion": "v"}

    _stub(monkeypatch, request_promotion=capture)
    TestClient(A.app).post("/api/promotion/request", json={"run": RUN})
    assert got["gates"] == ("pass", "review")
    assert got["products"] is None, "None means 'every indexable product', decided downstream"


# ---------------- ids ----------------
@pytest.mark.parametrize("bad", ["Has-Capitals", "has space", "a/b", "x" * 80])
def test_an_illegal_version_is_a_400_not_a_500(client, monkeypatch, bad):
    monkeypatch.setenv("ACI_ADMIN_PROMOTE", "1")
    assert client.post("/api/promotion/request",
                       json={"run": RUN, "version": bad}).status_code == 400


def test_an_illegal_run_id_is_a_400(client, monkeypatch):
    _stub(monkeypatch, promotions_for_run=lambda *a, **k: [])
    assert client.get("/api/promotion/runs?run=../etc").status_code == 400
    assert client.get("/api/promotion/plan?run=_progress").status_code == 400


def test_asking_for_a_run_and_a_version_at_once_is_a_400(client):
    """They are two different screens: a run is unpublished, a staged version is published
    but not pointed at."""
    for path in ("/api/doc-gallery", "/api/doc-gallery/tree"):
        assert client.get(f"{path}?run={RUN}&version=v-p1").status_code == 400


# ---------------- refusals carry reasons ----------------
def test_already_running_is_a_409_with_a_reason_not_a_500(client, monkeypatch):
    monkeypatch.setenv("ACI_ADMIN_PROMOTE", "1")
    _stub(monkeypatch, request_promotion=lambda *a, **k: {
        "queued": False, "reason": "a promotion of v is already running"})
    r = client.post("/api/promotion/request", json={"run": RUN})
    assert r.status_code == 409
    assert "already running" in r.json()["reason"]


def test_a_missing_promotion_is_a_404(client, monkeypatch):
    _stub(monkeypatch, progress=lambda *a, **k: None)
    assert client.get(
        f"/api/promotion/progress?run={RUN}&promotion=v-p1").status_code == 404


def test_stopping_something_that_does_not_exist_is_a_404(client, monkeypatch):
    monkeypatch.setenv("ACI_ADMIN_PROMOTE", "1")
    _stub(monkeypatch, request_stop=lambda *a, **k: {"stopping": False, "reason": "no"})
    assert client.post("/api/promotion/stop",
                       json={"run": RUN, "promotion": "v-p1"}).status_code == 404


def test_an_unreadable_run_is_a_502_not_an_empty_plan(client, monkeypatch):
    """An empty plan and an unreadable run must not look the same: the first says "nothing
    to promote", the second says "the listing failed"."""
    def boom(*a, **k):
        raise RuntimeError("denied")

    _stub(monkeypatch, compute_plan=boom)
    assert client.get(f"/api/promotion/plan?run={RUN}").status_code == 502


def test_a_listing_failure_still_returns_a_usable_payload(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("denied")

    _stub(monkeypatch, list_promotions=boom)
    body = client.get("/api/promotion/runs").json()
    assert body["configured"] is True and body["promotions"] == [] and "error" in body


# ---------------- the happy read path ----------------
def test_progress_is_handed_through_unchanged(client, monkeypatch):
    payload = PM.summarize({"promotion_id": "v-p1", "run": RUN, "version": "v-p1",
                            "started_at": 1.0, "updated_at": 2.0, "stages": {}}, 3.0)
    _stub(monkeypatch, progress=lambda *a, **k: payload)
    got = client.get(f"/api/promotion/progress?run={RUN}&promotion=v-p1").json()
    assert got["version"] == "v-p1"
    assert [s["key"] for s in got["stages"]] == [m["key"] for m in PM.STAGES]


def test_the_plan_endpoint_returns_counts_and_never_the_rows(client, monkeypatch):
    plan = {"run": RUN, "version": "v-p1", "target_prefix": "index/v-p1/doc-gallery",
            "products": ["Data Privacy"], "gates": ["pass"], "products_walked": 1,
            "docs": [{"job": "a/b__1", "decision": "promote", "product": "Data Privacy",
                      "gate": "pass", "objects": 3, "bytes": 10},
                     {"job": "a/c__2", "decision": "skip", "reason": "gate"}]}
    _stub(monkeypatch, compute_plan=lambda *a, **k: plan)
    body = client.get(f"/api/promotion/plan?run={RUN}").json()
    assert body["eligible"] == 1 and body["excluded"] == {"skip/gate": 1}
    assert "docs" not in body


def test_plan_detail_is_bounded(client, monkeypatch):
    _stub(monkeypatch, read_plan=lambda *a, **k: {"summary": {}, "total": 900,
                                                  "offset": 0, "limit": 500, "docs": []})
    assert client.get(
        f"/api/promotion/plan-detail?run={RUN}&promotion=v-p1&limit=99999").status_code == 422
