"""A vector load that lands short must fail, not report progress and stop.

load_opensearch DELETES and recreates the index before loading, so a partial load leaves search
worse than it was. That happened on dev: the admin endpoint defaults to batch=2000, a
1024-dimension vector is ~15KB of JSON, and a ~30MB bulk body exceeds the request cap of an AWS
managed domain. With raise_on_error=False the rejected chunks were collected rather than raised,
so the job reported "inserting" while the index sat at exactly 2000 of 84,197 vectors — it looked
stuck instead of failed, and dev search served almost nothing until it was rerun at batch=500.

The local Docker cluster has no request cap, which is why the local run of the identical call
succeeded and this only appeared against the real domain.
"""

import sys
import types

import pytest

from aosphere_core_index.embeddings import vector_load


class _Indices:
    def __init__(self):
        self.created = None

    def exists(self, index): return True
    def delete(self, index): pass
    def create(self, index, body): self.created = body
    def refresh(self, index): pass


class _Client:
    def __init__(self, landed):
        self.indices = _Indices()
        self._landed = landed

    def count(self, index): return {"count": self._landed}


def _install(monkeypatch, landed, chunk_seen, errs=()):
    """Stand in for opensearchpy.helpers.bulk and the shared client."""
    def bulk(cli, actions, chunk_size=None, **kw):
        chunk_seen.append(chunk_size)
        n = sum(1 for _ in actions)
        return n, list(errs)

    monkeypatch.setitem(sys.modules, "opensearchpy",
                        types.SimpleNamespace(helpers=types.SimpleNamespace(bulk=bulk)))
    monkeypatch.setattr("aosphere_core_index.embeddings.vector_backend._os_client",
                        lambda: _Client(landed))


def _rows(n):
    return [(i, [0.1] * 4, "France", f"A{i}", "Title", 1, "clause") for i in range(n)]


def test_the_bulk_chunk_is_capped_however_large_a_batch_is_requested(monkeypatch):
    """The admin endpoint's default is 2000; the loader must not pass that to a managed domain."""
    seen = []
    _install(monkeypatch, landed=10, chunk_seen=seen)
    vector_load.load_opensearch(_rows(10), 10, 4, lambda n: None, batch=2000)
    assert seen == [vector_load._MAX_BULK] and vector_load._MAX_BULK <= 500


def test_a_smaller_batch_is_respected(monkeypatch):
    seen = []
    _install(monkeypatch, landed=10, chunk_seen=seen)
    vector_load.load_opensearch(_rows(10), 10, 4, lambda n: None, batch=100)
    assert seen == [100]


def test_a_short_load_raises_instead_of_reporting_success(monkeypatch):
    """The dev failure mode: 2000 of 84,197 landed and the job looked healthy."""
    _install(monkeypatch, landed=2000, chunk_seen=[])
    with pytest.raises(RuntimeError, match=r"incomplete: 2000/84197"):
        vector_load.load_opensearch(_rows(10), 84197, 4, lambda n: None, batch=500)


def test_bulk_errors_raise_and_name_the_first_one(monkeypatch):
    _install(monkeypatch, landed=10, chunk_seen=[],
             errs=[{"index": {"error": {"type": "request_entity_too_large_exception"}}}])
    with pytest.raises(RuntimeError, match="request_entity_too_large"):
        vector_load.load_opensearch(_rows(10), 10, 4, lambda n: None, batch=500)


def test_a_complete_load_returns_its_counts(monkeypatch):
    _install(monkeypatch, landed=10, chunk_seen=[])
    res = vector_load.load_opensearch(_rows(10), 10, 4, lambda n: None, batch=500)
    assert res == {"backend": "opensearch", "indexed": 10, "errors": 0, "count": 10}
