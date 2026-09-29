"""Swappable vector-retrieval backend behind the flat MultiIndex.

`retrieve(mi, qvec, k, jurisdictions, per_region)` is a drop-in for `MultiIndex.search`
(returns the SAME [{jurisdiction, key, title, level, score, kind}] rows), routed by
ACI_VECTOR_BACKEND so we can A/B the in-memory brute-force index against external
vector stores WITHOUT touching search_all / the reranker:

  * "memory"     (default): in-memory exact cosine (MultiIndex.search) — today's path.
  * "opensearch": OpenSearch k-NN (HNSW/cosine, lucene engine) with a region filter.
  * "atlas":      MongoDB Atlas $vectorSearch with a region filter.

The external backends are populated by scripts/load_vectors.py from the same multi.npz,
so all three serve identical vectors + metadata — only the ANN algorithm differs.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_INDEX = os.getenv("ACI_VECTOR_INDEX", "aci-vectors")
# OpenSearch's lucene knn engine only does EXACT filtered search when the knn `k` is >=
# the filtered-doc count; below that it does approximate graph traversal that, for a small
# region cluster far from the query vector, exhausts ef_search and can return ZERO filtered
# hits. Our scoped queries filter to one (or a few) regions of ~1.2k vectors each, so we set
# the knn `k` for filtered queries above that cardinality to force the exact path — which
# also makes OpenSearch match the in-memory brute-force index exactly. Covers ~8 regions
# (8*1.2k); a broader scope falls back to approximate (acceptable at that breadth).
_FILTER_EXACT_K = int(os.getenv("ACI_OPENSEARCH_FILTER_K", "10000"))
# Atlas $vectorSearch numCandidates = limit * this (capped 10000). Oversamples for recall +
# fixes the filtered under-fetch WITHOUT exact ENN (which cold-scans the vectors — pathological
# on a RAM-tight tier). 15 gave 100% recall in testing.
_ATLAS_NC_MULT = int(os.getenv("ACI_ATLAS_NUM_CANDIDATES_MULT", "15"))
_OS = None
_ATLAS = None


def backend() -> str:
    return os.getenv("ACI_VECTOR_BACKEND", "memory").strip().lower()


def _os_client():
    global _OS
    if _OS is None:
        from opensearchpy import OpenSearch

        url = os.getenv("ACI_OPENSEARCH_URL", "http://localhost:9200")
        kw = {"hosts": [url], "http_compress": True,
              "timeout": int(os.getenv("ACI_OPENSEARCH_TIMEOUT", "60"))}
        # SigV4 (IAM) auth for AWS: service "aoss" = OpenSearch Serverless, "es" = managed
        # domain. Unset -> plain HTTP (local Docker / basic-auth cluster), so local is unchanged.
        service = os.getenv("ACI_OPENSEARCH_AWS_SERVICE")
        if service:
            import boto3
            from opensearchpy import AWSV4SignerAuth, RequestsHttpConnection

            region = os.getenv("ACI_OPENSEARCH_AWS_REGION") or os.getenv("AWS_REGION", "eu-west-1")
            # Use an explicit named profile (e.g. the SSO profile with es:ESHttp* on the
            # domain) when set, so SigV4 signs with the domain's credentials rather than
            # whatever ambient env keys (.env) happen to be exported. Falls back to the
            # default credential chain when unset.
            _profile = os.getenv("ACI_OPENSEARCH_AWS_PROFILE")
            _session = boto3.Session(profile_name=_profile) if _profile else boto3.Session()
            kw.update(http_auth=AWSV4SignerAuth(_session.get_credentials(), region, service),
                      use_ssl=True, verify_certs=True, connection_class=RequestsHttpConnection,
                      pool_maxsize=int(os.getenv("ACI_OPENSEARCH_POOL", "20")))
        _OS = OpenSearch(**kw)
    return _OS


# A health probe runs every few seconds on every replica, so the store count is cached
# briefly: the number only changes during a load, and a probe must never become a load test
# on the search store.
_COUNT_TTL = float(os.getenv("ACI_STORE_COUNT_TTL", "15"))
_count_cache: tuple[float, int | None] = (0.0, None)


def store_rows() -> int | None:
    """How many vectors the SEARCH STORE actually holds, or None if it cannot be asked.

    The index's own `n_rows` is what the mounted data EXPECTS; this is what the store has.
    They differ exactly when a load truncated or never ran — dev sat at 11,000 of 77,939
    after a rolling deploy killed the loader mid-flight, and nothing in /healthz, the status
    endpoint or the logs said so, because status lives in one pod's memory and the bulk
    helper swallowed the outcome. Reporting both numbers makes that visible to anyone with
    curl, which is the cheapest possible version of "readiness depends on the index".
    """
    global _count_cache
    import time
    now = time.time()
    ts, val = _count_cache
    if now - ts < _COUNT_TTL:
        return val
    out = None
    try:
        b = backend()
        if b == "opensearch":
            out = int(_os_client().count(index=_INDEX)["count"])
        elif b == "atlas":
            out = int(_atlas_coll().estimated_document_count())
    except Exception as e:  # noqa: BLE001 — a health probe must not fail on the store
        log.debug("store_rows unavailable: %s: %s", type(e).__name__, e)
        out = None
    _count_cache = (now, out)
    return out


def atlas_uri() -> str | None:
    """MONGODB_URI with MONGODB_USERNAME/MONGODB_PASSWORD injected percent-encoded
    (mirrors service.entity_search so the vector store auths the same way the app does)."""
    from urllib.parse import quote_plus

    uri = os.environ.get("MONGODB_URI")
    user = os.environ.get("MONGODB_USERNAME")
    pwd = os.environ.get("MONGODB_PASSWORD")
    if uri and user and pwd:
        scheme, sep, rest = uri.partition("://")
        rest = rest.rsplit("@", 1)[-1]
        uri = f"{scheme}{sep}{quote_plus(user)}:{quote_plus(pwd)}@{rest}"
    return uri


def atlas_client():
    """A MongoClient wired like the app's (URI injection + certifi CA on macOS)."""
    from pymongo import MongoClient

    kwargs = {"serverSelectionTimeoutMS": 8000}
    try:  # python.org macOS installs lack system CA access; use certifi
        import certifi
        kwargs["tlsCAFile"] = certifi.where()
    except ImportError:
        pass
    return MongoClient(atlas_uri(), **kwargs)


def _atlas_coll():
    global _ATLAS
    if _ATLAS is None:
        cli = atlas_client()
        db = cli[os.getenv("ACI_ATLAS_DB", "aosphere_search")]
        _ATLAS = db[os.getenv("ACI_ATLAS_VECTOR_COLLECTION", "aci_vectors")]
    return _ATLAS


def _rows_from(hits) -> list[dict]:
    return hits  # already normalized by each backend


def _opensearch_search(qvec, k, jurisdictions, per_region) -> list[dict]:
    """Filtered k-NN. For a single scoped region (the eval case) this is one HNSW query
    with a region term-filter; for multi-region scoped queries with per_region>0 we also
    pull each region's own top-m so one region can't crowd the pool (mirrors MultiIndex)."""
    client = _os_client()
    vec = [float(x) for x in qvec]
    regs = list(dict.fromkeys(jurisdictions)) if jurisdictions else None

    def _knn(size, region_terms):
        # For filtered queries, oversize k past the region cardinality so lucene runs EXACT
        # filtered search (see _FILTER_EXACT_K); unfiltered stays approximate over the full index.
        knn_k = max(size, _FILTER_EXACT_K) if region_terms else max(size, 1)
        knn = {"vector": vec, "k": knn_k}
        if region_terms:
            knn["filter"] = {"terms": {"region": region_terms}}
        body = {"size": size, "query": {"knn": {"vector": knn}},
                "_source": ["region", "key", "title", "level", "kind"]}
        resp = client.search(index=_INDEX, body=body)
        out = []
        for h in resp["hits"]["hits"]:
            s = h["_source"]
            out.append({"jurisdiction": s["region"], "key": s["key"], "title": s["title"],
                        "level": s.get("level", 0), "score": float(h["_score"]),
                        "kind": s.get("kind", "clause")})
        return out

    rows = _knn(k, regs)
    if per_region > 0 and regs and len(regs) > 1:  # per-region fairness (multi-region only)
        seen = {(r["jurisdiction"], r["key"]) for r in rows}
        for reg in regs:
            for r in _knn(per_region, [reg]):
                if (r["jurisdiction"], r["key"]) not in seen:
                    seen.add((r["jurisdiction"], r["key"])); rows.append(r)
        rows.sort(key=lambda r: -r["score"])
    return rows


def _atlas_search(qvec, k, jurisdictions, per_region) -> list[dict]:
    """MongoDB Atlas $vectorSearch. numCandidates is oversized per Atlas guidance (~10-20x
    limit) for recall; region filter restricts to the scoped jurisdiction(s)."""
    from bson.binary import Binary, BinaryVectorDtype

    coll = _atlas_coll()
    # Stored vectors are float32 binData (see scripts/load_vectors.py); send the query as the
    # same type so $vectorSearch compares like-for-like.
    vec = Binary.from_vector([float(x) for x in qvec], BinaryVectorDtype.FLOAT32)
    regs = list(dict.fromkeys(jurisdictions)) if jurisdictions else None

    def _vs(limit, region_terms):
        # HNSW/ANN — NOT exact. exact ENN cold-loads + exhaustively scans the vector data
        # (seconds/query cold, ~3x slower warm); ANN traverses the graph. Oversampling
        # numCandidates well past the limit gives full recall AND fixes the filtered
        # under-fetch (the earlier zero-hit bug was numCandidates=600 being too low for a
        # selective region filter, not a need for exact). Capped at Atlas's 10000 ceiling.
        stage = {"index": os.getenv("ACI_ATLAS_VECTOR_INDEX", "aci_vec_idx"),
                 "path": "vector", "queryVector": vec, "limit": limit,
                 "numCandidates": min(max(limit * _ATLAS_NC_MULT, 1500), 10000)}
        if region_terms:
            stage["filter"] = {"region": {"$in": region_terms}}
        pipe = [{"$vectorSearch": stage},
                {"$project": {"_id": 0, "region": 1, "key": 1, "title": 1, "level": 1,
                              "kind": 1, "score": {"$meta": "vectorSearchScore"}}}]
        out = []
        for d in coll.aggregate(pipe):
            out.append({"jurisdiction": d["region"], "key": d["key"], "title": d.get("title", ""),
                        "level": d.get("level", 0), "score": float(d.get("score", 0.0)),
                        "kind": d.get("kind", "clause")})
        return out

    rows = _vs(k, regs)
    if per_region > 0 and regs and len(regs) > 1:
        # Per-region floor: fire the top-ups CONCURRENTLY — they are independent queries, so
        # R regions cost ~one round-trip instead of R sequential ones (pymongo's client is
        # pooled + thread-safe). This is free in-RAM (memory backend) but the sequential
        # network fan-out is what made multi-jurisdiction Atlas queries pile up.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(regs), 12)) as ex:
            batches = list(ex.map(lambda reg: _vs(per_region, [reg]), regs))
        seen = {(r["jurisdiction"], r["key"]) for r in rows}
        for batch in batches:
            for r in batch:
                if (r["jurisdiction"], r["key"]) not in seen:
                    seen.add((r["jurisdiction"], r["key"])); rows.append(r)
        rows.sort(key=lambda r: -r["score"])
    return rows


def retrieve(mi, qvec, k, jurisdictions=None, per_region=0) -> list[dict]:
    """Drop-in for MultiIndex.search, routed by ACI_VECTOR_BACKEND."""
    b = backend()
    if b == "opensearch":
        return _opensearch_search(qvec, k, jurisdictions, per_region)
    if b == "atlas":
        return _atlas_search(qvec, k, jurisdictions, per_region)
    return mi.search(qvec, k, jurisdictions=jurisdictions, per_region=per_region)
