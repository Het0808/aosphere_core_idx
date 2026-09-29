"""Populate an external vector backend (OpenSearch / MongoDB Atlas) from the in-memory
MultiIndex, with pollable progress.

The admin reindex endpoint runs this INSIDE the deployment (e.g. dev1), where the app
reaches the vector store at full LAN speed rather than over a developer VPN. One job runs
at a time; progress is a module-level snapshot polled via GET /api/admin/reindex.

The load functions take a `rows` iterable of (i, vec, region, key, title, level, kind), so
the same code serves both the in-memory index (rows_from_mi, used by the API) and the
on-disk multi.npz (scripts/load_vectors.py) — one source of truth for the index mappings.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

_INDEX = os.getenv("ACI_VECTOR_INDEX", "aci-vectors")
_RUNNING = ("starting", "inserting", "building-index")


def _tolist(vec):
    """numpy row -> python float list at C speed (avoids per-element float() under the GIL)."""
    return vec.tolist() if hasattr(vec, "tolist") else list(vec)


# ---- row source -----------------------------------------------------------------------

def rows_from_mi(mi):
    """Yield (i, vec, region, key, title, level, kind) from the in-memory MultiIndex —
    the same per-row fields MultiIndex.search emits."""
    m = mi.matrix
    has_kind = getattr(mi, "row_kind", None) is not None
    for i in range(m.shape[0]):
        yield (i, m[i], str(mi.row_region[i]), str(mi.keys[i]), str(mi.titles[i]),
               int(mi.levels[i]), str(mi.row_kind[i]) if has_kind else "clause")


# ---- backend loaders (row-based, progress-reporting) ----------------------------------

# Hard cap on the bulk chunk, regardless of what a caller asks for. A 1024-dimension vector is
# ~15KB of JSON, so 2000 documents is a ~30MB+ HTTP body — over the request cap of an AWS managed
# domain. With raise_on_error=False the rejected chunks are collected instead of raised, so the
# load reported "inserting" while nothing after the first chunk ever landed: dev's index sat at
# exactly 2000 of 84,197 vectors and looked stuck rather than failed. The local Docker cluster has
# no such cap, which is why this only ever appears against a real domain.
_MAX_BULK = 500


def load_opensearch(rows, total, dim, on_progress, batch=1000):
    from opensearchpy import helpers

    # Use the SAME client the query path builds (vector_backend._os_client) so the
    # loader authenticates identically — SigV4 for an AWS managed domain/serverless
    # (ACI_OPENSEARCH_AWS_SERVICE) and plain HTTP for a local Docker cluster. Writing
    # our own bare client here sent unsigned/anonymous requests, which an AWS domain's
    # access policy rejects with a 403.
    from aosphere_core_index.embeddings.vector_backend import _os_client
    cli = _os_client()
    if cli.indices.exists(index=_INDEX):
        cli.indices.delete(index=_INDEX)
    cli.indices.create(index=_INDEX, body={
        "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0,
                               "knn.algo_param.ef_search": 256}},
        "mappings": {"properties": {
            "vector": {"type": "knn_vector", "dimension": dim,
                       "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene",
                                  "parameters": {"ef_construction": 256, "m": 16}}},
            "region": {"type": "keyword"}, "key": {"type": "keyword"},
            "title": {"type": "text", "index": False},
            "level": {"type": "integer"}, "kind": {"type": "keyword"}}}})

    def actions():
        n = 0
        for i, vec, region, key, title, level, kind in rows:
            yield {"_index": _INDEX, "_id": str(i), "vector": _tolist(vec),
                   "region": region, "key": key, "title": title, "level": level, "kind": kind}
            n += 1
            if n % batch == 0:
                on_progress(n)

    ok, errs = helpers.bulk(cli, actions(), chunk_size=min(batch, _MAX_BULK),
                            request_timeout=180, raise_on_error=False)
    cli.indices.refresh(index=_INDEX)
    count = cli.count(index=_INDEX)["count"]
    on_progress(count)
    n_err = len(errs) if isinstance(errs, list) else (errs or 0)
    # A partial load is a FAILURE, not a result to report cheerfully. The index was deleted and
    # recreated at the top of this function, so anything short of the full count means search is
    # serving less than it did before — the caller must be able to see that without diffing counts.
    if n_err or count < total:
        raise RuntimeError(
            f"opensearch load incomplete: {count}/{total} vectors, {n_err} bulk error(s)"
            + (f"; first: {str(errs[0])[:300]}" if isinstance(errs, list) and errs else ""))
    return {"backend": "opensearch", "indexed": ok, "errors": n_err, "count": count}


def load_atlas(rows, total, dim, on_progress, batch=2000):
    from bson.binary import Binary, BinaryVectorDtype
    from pymongo import WriteConcern

    from aosphere_core_index.embeddings.vector_backend import atlas_client

    cli = atlas_client()
    db = cli[os.getenv("ACI_ATLAS_DB", "aosphere_search")]
    name = os.getenv("ACI_ATLAS_VECTOR_COLLECTION", "aci_vectors")
    db[name].drop()
    # float32 binData (~4B/dim vs ~13B/dim for a BSON double array) + unordered + w=1 keeps
    # the wire payload and per-batch replication wait low; $vectorSearch indexes it natively.
    coll = db.get_collection(name, write_concern=WriteConcern(w=1))
    buf, n = [], 0
    for i, vec, region, key, title, level, kind in rows:
        buf.append({"_id": i,
                    "vector": Binary.from_vector(_tolist(vec), BinaryVectorDtype.FLOAT32),
                    "region": region, "key": key, "title": title, "level": level, "kind": kind})
        if len(buf) >= batch:
            coll.insert_many(buf, ordered=False); n += len(buf); buf = []
            on_progress(n)
    if buf:
        coll.insert_many(buf, ordered=False); n += len(buf); on_progress(n)

    on_progress(n, phase="building-index")
    _ensure_atlas_vector_index(coll, dim)
    return {"backend": "atlas", "inserted": n, "count": coll.estimated_document_count(),
            "index": os.getenv("ACI_ATLAS_VECTOR_INDEX", "aci_vec_idx")}


def _ensure_atlas_vector_index(coll, dim):
    """(Re)create the $vectorSearch index (cosine + region filter) and wait until queryable —
    Atlas builds search indexes asynchronously."""
    from pymongo.operations import SearchIndexModel

    idx = os.getenv("ACI_ATLAS_VECTOR_INDEX", "aci_vec_idx")
    if idx in {i["name"] for i in coll.list_search_indexes()}:
        coll.drop_search_index(idx)
        while idx in {i["name"] for i in coll.list_search_indexes()}:
            time.sleep(3)
    coll.create_search_index(SearchIndexModel(
        name=idx, type="vectorSearch",
        definition={"fields": [
            {"type": "vector", "path": "vector", "numDimensions": dim, "similarity": "cosine"},
            {"type": "filter", "path": "region"}]}))
    for _ in range(160):  # up to ~8 min
        st = next((i for i in coll.list_search_indexes() if i["name"] == idx), None)
        if st and st.get("queryable"):
            return
        time.sleep(3)
    log.warning("Atlas vector index %s not queryable after wait; check Atlas UI", idx)


# ---- single-job manager + progress snapshot -------------------------------------------

_LOCK = threading.Lock()
_JOB = {"phase": "idle", "backend": None, "done": 0, "total": 0,
        "started_at": None, "elapsed_s": 0.0, "rate": 0.0, "error": None, "result": None}


def status() -> dict:
    with _LOCK:
        j = dict(_JOB)
    if j["started_at"] and j["phase"] in _RUNNING:
        j["elapsed_s"] = round(time.time() - j["started_at"], 1)
        j["pct"] = round(100 * j["done"] / j["total"], 1) if j["total"] else None
    return j


def start(mi, backend: str, batch: int = 2000) -> dict:
    backend = (backend or "").strip().lower()
    if backend not in ("atlas", "opensearch"):
        raise ValueError(f"unknown backend {backend!r} (expected atlas|opensearch)")
    with _LOCK:
        if _JOB["phase"] in _RUNNING:
            raise RuntimeError(
                f"reindex already running ({_JOB['backend']}: {_JOB['done']}/{_JOB['total']}, "
                f"phase={_JOB['phase']})")
        _JOB.update({"phase": "starting", "backend": backend, "done": 0,
                     "total": int(mi.matrix.shape[0]), "started_at": time.time(),
                     "elapsed_s": 0.0, "rate": 0.0, "error": None, "result": None})
    threading.Thread(target=_run, args=(mi, backend, batch), daemon=True, name="reindex").start()
    return status()


def _run(mi, backend, batch):
    t0 = time.time()

    def prog(done, phase="inserting"):
        el = time.time() - t0
        with _LOCK:
            _JOB.update(phase=phase, done=int(done), elapsed_s=round(el, 1),
                        rate=round(done / el, 1) if el else 0.0)

    try:
        dim, total = int(mi.matrix.shape[1]), int(mi.matrix.shape[0])
        fn = load_atlas if backend == "atlas" else load_opensearch
        result = fn(rows_from_mi(mi), total, dim, prog, batch=batch)
        with _LOCK:
            _JOB.update(phase="done", result=result, elapsed_s=round(time.time() - t0, 1),
                        done=int(result.get("count", _JOB["done"])))
    except Exception as e:  # noqa: BLE001 — surface the failure via status(), don't crash the thread
        log.exception("reindex failed")
        with _LOCK:
            _JOB.update(phase="error", error=f"{type(e).__name__}: {e}",
                        elapsed_s=round(time.time() - t0, 1))
