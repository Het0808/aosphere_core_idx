"""In-network divergence check: does an external vector backend diverge from the in-memory
exact index after integration?

Runs INSIDE the deployment (fast, in-network — not over a developer VPN). Samples real index
vectors as queries (no Titan/query-embed needed), then for each compares the top-k **row ids**
(multi.npz order — unique per row, so immune to duplicate keys + score ties, unlike comparing
by clause key) returned by the in-memory exact search vs the external backend, scoped to the
query's region. Reports recall overlap + per-backend latency, so `results are not diverging`
becomes a number you can assert on.

Scoped on purpose: scoped retrieval is where we force the external stores to be EXACT, so we
expect parity. Unscoped external search is ANN by design (approximate) and would "diverge"
from exact expectedly — that is not a regression, so it is out of scope here.
"""
from __future__ import annotations

import os
import statistics
import time

import numpy as np

from aosphere_core_index.embeddings import vector_backend as vb


def _memory_topk_ids(mi, qv, k, reg):
    rows = np.where(mi.row_region == reg)[0]
    sims = mi.matrix[rows] @ qv
    return rows[np.argsort(-sims)[:k]].tolist()


def _atlas_topk_ids(qv, k, reg):
    from bson.binary import Binary, BinaryVectorDtype

    coll = vb._atlas_coll()
    pipe = [{"$vectorSearch": {
                "index": os.getenv("ACI_ATLAS_VECTOR_INDEX", "aci_vec_idx"), "path": "vector",
                "queryVector": Binary.from_vector([float(x) for x in qv], BinaryVectorDtype.FLOAT32),
                "limit": k, "numCandidates": min(max(k * vb._ATLAS_NC_MULT, 1500), 10000),
                "filter": {"region": {"$in": [reg]}}}},  # ANN — matches _atlas_search (no exact)
            {"$project": {"_id": 1}}]
    return [int(d["_id"]) for d in coll.aggregate(pipe)]


def _opensearch_topk_ids(qv, k, reg):
    cli = vb._os_client()
    body = {"size": k, "_source": False,
            "query": {"knn": {"vector": {"vector": [float(x) for x in qv],
                      "k": max(k, vb._FILTER_EXACT_K), "filter": {"terms": {"region": [reg]}}}}}}
    return [int(h["_id"]) for h in cli.search(index=vb._INDEX, body=body)["hits"]["hits"]]


_EXT = {"atlas": _atlas_topk_ids, "opensearch": _opensearch_topk_ids}


def divergence(mi, backend: str, n: int = 40, k: int = 40, seed: int = 13) -> dict:
    """Compare `backend` vs the in-memory exact index over n sampled scoped queries."""
    backend = (backend or "").strip().lower()
    ext_fn = _EXT.get(backend)
    if ext_fn is None:
        raise ValueError(f"backend must be one of {sorted(_EXT)}, got {backend!r}")

    rng = np.random.default_rng(seed)
    idx = rng.choice(mi.matrix.shape[0], size=min(n, mi.matrix.shape[0]), replace=False)
    ov_k, ov_10, lat_mem, lat_ext, worst = [], [], [], [], []
    for i in idx:
        i = int(i); qv = mi.matrix[i]; reg = str(mi.row_region[i])
        t = time.perf_counter(); mem = _memory_topk_ids(mi, qv, k, reg)
        lat_mem.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter(); ext = ext_fn(qv, k, reg)
        lat_ext.append((time.perf_counter() - t) * 1000)
        ok = len(set(mem[:k]) & set(ext[:k])); o10 = len(set(mem[:10]) & set(ext[:10]))
        ov_k.append(ok); ov_10.append(o10)
        if ok < k:
            worst.append({"region": reg, f"overlap_top{k}": ok, "overlap_top10": o10,
                          "ext_returned": len(ext)})

    tk_mean = statistics.mean(ov_k)
    return {
        "backend": backend, "n_samples": len(idx), "k": k, "scope": "per-region (scoped)",
        "recall": {"top10_mean": round(statistics.mean(ov_10), 2), "top10_min": min(ov_10),
                   "topk_mean": round(tk_mean, 2), "topk_min": min(ov_k),
                   "perfect_topk": sum(1 for x in ov_k if x == k)},
        "latency_ms": {"memory_median": round(statistics.median(lat_mem), 2),
                       "external_median": round(statistics.median(lat_ext), 2),
                       "external_p95": round(sorted(lat_ext)[int(len(lat_ext) * 0.95)], 2)},
        # exact-parity backends sit at ~k; a real regression (filtered under-fetch) collapses
        # this. 0.9*k tolerates only float/tie tail-jitter, not lost recall.
        "diverging": bool(tk_mean < 0.9 * k),
        "worst": sorted(worst, key=lambda w: w[f"overlap_top{k}"])[:8],
    }
