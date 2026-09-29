"""/healthz must say whether the search store actually holds the index the pod mounted.

Dev served 11,000 of 77,939 vectors after a rolling deploy killed the loader mid-flight, and
nothing said so: `/healthz` reported `rows: 77939` (what the MOUNTED DATA expects, read from
multi.npz — not what the store has), `GET /api/admin/reindex` answered `idle` because job
state lives in one pod's memory and the poll hit other replicas, and the logs were silent
because the bulk helper is called with raise_on_error=False. Establishing the truth took a
SigV4 query against the VPC OpenSearch endpoint.

So healthz now reports both numbers and whether they agree. `store_rows` is the store's own
count; `index_loaded` is the comparison a human would make. Neither may ever be able to take
the liveness probe down, and a probe that runs every few seconds on every replica must not
become a load test on the search store.
"""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from aosphere_core_index.embeddings import vector_backend as VB  # noqa: E402
from aosphere_core_index.service import app as A  # noqa: E402


@pytest.fixture(autouse=True)
def _no_count_cache(monkeypatch):
    """Each test starts with a cold cache, or the previous test's number leaks in."""
    monkeypatch.setattr(VB, "_count_cache", (0.0, None))


class _MI:
    regions = ["Data Privacy — France"]
    n_rows = 77939
    model = "amazon.titan-embed-text-v2:0"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(A, "get_multi", lambda: _MI())
    monkeypatch.setattr("aosphere_core_index.service.entity_backend.backend", lambda: "none")
    return TestClient(A.app)


def _health(client) -> dict:
    r = client.get("/healthz")
    assert r.status_code == 200, r.text
    return r.json()


def test_a_fully_loaded_store_reports_loaded(client, monkeypatch):
    monkeypatch.setattr(VB, "backend", lambda: "opensearch")
    monkeypatch.setattr(VB, "_os_client", lambda: type("C", (), {
        "count": staticmethod(lambda index=None: {"count": 77939})})())
    d = _health(client)
    assert d["rows"] == 77939 and d["store_rows"] == 77939
    assert d["index_loaded"] is True


def test_a_truncated_load_is_visible(client, monkeypatch):
    """The dev case, exactly: 11,000 of 77,939 and every other signal saying nothing."""
    monkeypatch.setattr(VB, "backend", lambda: "opensearch")
    monkeypatch.setattr(VB, "_os_client", lambda: type("C", (), {
        "count": staticmethod(lambda index=None: {"count": 11000})})())
    d = _health(client)
    assert d["rows"] == 77939, "what the mounted data expects"
    assert d["store_rows"] == 11000, "what the store actually holds"
    assert d["index_loaded"] is False
    assert d["status"] == "ok", "liveness is a separate question from index readiness"


def test_an_unreachable_store_does_not_break_the_probe(client, monkeypatch):
    """A health endpoint that fails when a dependency is unreachable takes the pod out of
    service for a reporting problem. Absence of an answer is not a failed index, so
    store_rows is None and index_loaded is None — not False."""
    def boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(VB, "backend", lambda: "opensearch")
    monkeypatch.setattr(VB, "_os_client", boom)
    d = _health(client)
    assert d["store_rows"] is None
    assert d["index_loaded"] is None, "unknown must not read as 'not loaded'"
    assert d["status"] == "ok"


def test_the_memory_backend_has_no_store_to_count(client, monkeypatch):
    monkeypatch.setattr(VB, "backend", lambda: "memory")
    d = _health(client)
    assert d["store_rows"] is None and d["index_loaded"] is None


def test_the_count_is_cached_so_probes_do_not_hammer_the_store(monkeypatch):
    """Kubernetes probes every replica every few seconds; the number only moves during a
    load."""
    calls = []

    def client_stub():
        calls.append(1)
        return type("C", (), {"count": staticmethod(lambda index=None: {"count": 5})})()

    monkeypatch.setattr(VB, "backend", lambda: "opensearch")
    monkeypatch.setattr(VB, "_os_client", client_stub)
    monkeypatch.setattr(VB, "_COUNT_TTL", 300)
    assert VB.store_rows() == 5
    for _ in range(20):
        assert VB.store_rows() == 5
    assert len(calls) == 1, f"asked the store {len(calls)} times for 21 probes"
