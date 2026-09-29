"""Cross-encoder reranker.

Bi-encoder cosine (bge-small) compresses scores on homogeneous legal text, so it
ranks poorly and its scores don't separate relevant from irrelevant. A reranker
scores each (query, passage) pair directly — far better ranking. Three backends
(ACI_RERANK_BACKEND):

  * "minilm" (default): local ONNX cross-encoder (fastembed). Scores are logits,
    separable around 0 (relevant >0). No Bedrock. Cost is O(pool) CPU + memory, so
    concurrent inferences are semaphore-bound to cap peak memory.
  * "cohere-bedrock": Cohere Rerank 3.5 via Bedrock (bedrock-agent-runtime). One
    server-side batched call, scores in [0,1]. Offloads CPU/memory (no local model,
    no semaphore) and is ~flat in pool size — faster on large pools, better under
    concurrency, and it removes the ONNX model from the pod's memory budget.
  * "claude-bedrock": LLM *listwise* reranker (Bedrock Claude Haiku 4.5 via Converse).
    Reasons over the whole pool to reorder it — separates near-identical sibling
    clauses that both cross-encoders and cosine score alike. Rank position is mapped
    to a [0,1] score. Also offloaded (no local model / no semaphore).

Scale note: the two backends emit DIFFERENT score scales (minilm logits vs cohere
0..1). Only rank order and "higher = better" are portable — anything additive on the
score (e.g. the guidance bonus) or an absolute min-score threshold must be tuned per
backend. See `backend()` / `score_scale()`.
"""

from __future__ import annotations

import logging
import os
import re
import threading

from ..config import settings as _settings

log = logging.getLogger(__name__)

_BACKEND = os.getenv("ACI_RERANK_BACKEND", "minilm").strip().lower()
_MODEL = os.getenv("ACI_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
_CE = None
# Bound inference concurrency. FastAPI runs sync endpoints in a threadpool, so
# concurrent requests (browser page-load, AI-agent rerank bursts) would otherwise
# fire N simultaneous ONNX inferences, each allocating activations at once -> memory
# multiplies and the container OOMs (seen at ~10 concurrent). A semaphore caps how
# many run at once, so peak memory ≈ N × one-inference. Default 1 (safest); raise
# ACI_RERANK_CONCURRENCY toward the pod's memory budget for more throughput.
# (Not applied to the cohere-bedrock backend — it holds no local memory.)
_SEM = threading.BoundedSemaphore(int(os.getenv("ACI_RERANK_CONCURRENCY", "1")))
# Cap per-call batch so a large candidate/passage list can't spike on its own either
# (fastembed iterates internally over batches of this size).
_BATCH = int(os.getenv("ACI_RERANK_BATCH", "32"))

# Cohere-on-Bedrock config. Cohere Rerank 3.5 is available in us-west-2 (in-account,
# no data-residency issue — Bedrock, same as Titan). Region/ARN overridable in case
# it lands in an eu region later (which would drop the cross-region latency).
# NOT settings.bedrock_region, and not an oversight: cohere.rerank-v3-5 is not offered
# in either EU region (checked against ListFoundationModels in eu-west-1/eu-west-2 —
# us-west-2 is the nearest region that has it). Pointing this at the shared AI region
# would fail at Bedrock with a model-not-found, so it keeps its own region and env var.
_COHERE_REGION = os.getenv("ACI_RERANK_COHERE_REGION", "us-west-2")
_COHERE_ARN = os.getenv(
    "ACI_RERANK_COHERE_ARN",
    f"arn:aws:bedrock:{_COHERE_REGION}::foundation-model/cohere.rerank-v3-5:0",
)
_BC = None

# LLM listwise reranker (Bedrock Claude via Converse). A tool-capable model reorders
# the whole candidate pool by *reasoning* about which clause answers the query — the
# one mechanism that separates near-identical sibling clauses a cross-encoder can't.
# Haiku 4.5 (eu.* EU inference profile) is the pick: in-account + in-EU, and A/Bs showed
# Sonnet/Opus give no reranking gain over it. Offloaded -> no local model, no semaphore.
_LLM_REGION = os.getenv("ACI_RERANK_LLM_REGION") or _settings.bedrock_region
_LLM_MODEL = os.getenv("ACI_RERANK_LLM_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
_LLM_SYS = (
    "You rerank passages for a legal Q&A search engine. You are given a QUERY and N "
    "numbered PASSAGES. Return a COMPLETE ranking: every passage number from 1 to N, each "
    "exactly once, ordered most relevant first. Then insert a single '|' immediately after "
    "the last passage that DIRECTLY answers the specific question asked. Be strict: the "
    "passages are already pre-filtered to the same broad topic, so most are on-topic — mark "
    "as relevant (before '|') ONLY the few that actually answer THIS question; put everything "
    "that is merely same-topic, background, or procedurally adjacent AFTER '|'. If none "
    "directly answer, put '|' first; if all do, put '|' last. Output ONLY the comma-separated "
    "numbers with one '|' (e.g. '3,1,5|2,4,6'). Include ALL N numbers. No prose."
)
_BR = None
_LLM_BACKENDS = ("claude-bedrock", "llm-bedrock")

# Voyage rerank-2.5 via REST (api.voyageai.com). 32K-token context + instruction-following:
# an optional natural-language instruction (ACI_RERANK_VOYAGE_INSTRUCTION) is prepended to
# the query to STEER ranking (rerank-2.5 feature) — e.g. "prefer the clause that directly
# answers the question; downrank background/definitions". Scores are [0,1] relevance.
# NOTE: EXTERNAL US API — passage text leaves the account (data-residency), unlike the
# Bedrock backends. Fine for evals; production adoption needs a private/in-region deployment.
_VOYAGE_MODEL = os.getenv("ACI_RERANK_VOYAGE_MODEL", "rerank-2.5")
_VOYAGE_INSTRUCTION = os.getenv("ACI_RERANK_VOYAGE_INSTRUCTION", "").strip()
_VOYAGE_URL = os.getenv("ACI_RERANK_VOYAGE_URL", "https://api.voyageai.com/v1/rerank")
_VOYAGE_BACKENDS = ("voyage", "voyage-2.5")

# Cascade reranker: a CHEAP pre-filter (local minilm cross-encoder, or the incoming cosine
# order) trims the pool to the top-K finalists, then the high-quality claude LISTWISE
# reranker reorders ONLY those K. Goal: the LLM's @1 precision (its whole-pool reasoning is
# what wins the #1 slot) at a fraction of its latency — LLM generation scales with pool
# size, so 80->15 cuts the slow part ~3-5x. Pre-filter ('minilm'|'cosine') and K configurable.
_CASCADE_BACKENDS = ("cascade",)
_CASCADE_PREFILTER = os.getenv("ACI_RERANK_CASCADE_PREFILTER", "cosine").strip().lower()
_CASCADE_K = int(os.getenv("ACI_RERANK_CASCADE_K", "15"))


def backend() -> str:
    return _BACKEND


def score_scale() -> str:
    """'unit' if scores are in [0,1] (cohere relevance, or the LLM reranker's rank->score
    mapping), 'logit' if signed & unbounded (minilm). Callers that add to / threshold the
    raw score use this to pick sane defaults."""
    return "unit" if _BACKEND in ("cohere", "cohere-bedrock", *_LLM_BACKENDS,
                                  *_VOYAGE_BACKENDS, *_CASCADE_BACKENDS) else "logit"


def enabled() -> bool:
    return os.getenv("ACI_RERANK", "1") == "1"


def _threads() -> int | None:
    """ONNX intra-op threads. Default to the cores actually available to the
    process (cgroup quota), NOT os.cpu_count() — in k8s the latter reports the
    whole node and oversubscribes threads, thrashing under CFS throttling.
    Override with ACI_RERANK_THREADS."""
    env = os.getenv("ACI_RERANK_THREADS")
    if env:
        return int(env)
    from aosphere_core_index import _effective_cpus

    return _effective_cpus()


def _ce():
    global _CE
    if _CE is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        _CE = TextCrossEncoder(_MODEL, threads=_threads())
    return _CE


def _bedrock():
    global _BC
    if _BC is None:
        import boto3
        from botocore.config import Config

        # Adaptive retries absorb Bedrock throttling under bursts (we deliberately
        # DON'T semaphore this backend, so concurrent requests all reach Bedrock).
        _BC = boto3.client(
            "bedrock-agent-runtime",
            region_name=_COHERE_REGION,
            config=Config(retries={"max_attempts": 6, "mode": "adaptive"}),
        )
    return _BC


def _cohere_rerank(query: str, docs: list[str]) -> list[float]:
    """Cohere Rerank 3.5 via Bedrock. Returns a [0,1] relevance score per doc, in the
    SAME order as `docs`. Scores are absolute per (query,doc) pair — comparable across
    calls — so a large pool can be chunked and concatenated without distortion."""
    client = _bedrock()
    # Cohere/query truncation guards: keep well under the model's per-input token cap
    # (chars are a cheap proxy; ~4 chars/token). Doc order is preserved via res.index.
    q = {"type": "TEXT", "textQuery": {"text": (query or " ")[:1900]}}
    scores = [0.0] * len(docs)
    step = 100  # sources per request; pools are usually <=~80 so this is one call
    for base in range(0, len(docs), step):
        chunk = docs[base : base + step]
        resp = client.rerank(
            queries=[q],
            sources=[
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {
                        "type": "TEXT",
                        "textDocument": {"text": (d or " ")[:3900]},
                    },
                }
                for d in chunk
            ],
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "numberOfResults": len(chunk),
                    "modelConfiguration": {"modelArn": _COHERE_ARN},
                },
            },
        )
        for res in resp["results"]:
            scores[base + res["index"]] = float(res["relevanceScore"])
    return scores


def _bedrock_runtime():
    global _BR
    if _BR is None:
        import boto3
        from botocore.config import Config

        _BR = boto3.client(
            "bedrock-runtime",
            region_name=_LLM_REGION,
            config=Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=60),
        )
    return _BR


def _llm_rerank(query: str, docs: list[str]) -> list[float]:
    """LLM listwise reranker (Bedrock Claude via Converse). The model returns a complete
    ranking of the passages; we map rank position -> a [0,1] score (best≈1, worst≈1/N)
    aligned to `docs`, so the caller's score-descending sort reproduces the model's order.
    Any passage the model omits is ranked last (defensive; the prompt forces a full list)."""
    n = len(docs)
    numbered = "\n".join(f"[{i+1}] {(d or ' ')[:600]}" for i, d in enumerate(docs))
    user = (f"QUERY: {(query or ' ')[:1900]}\n\nThere are {n} passages; rank ALL {n}.\n\n"
            f"PASSAGES:\n{numbered}\n\nRANKING (all {n} numbers):")
    inf = {"maxTokens": min(1024, 64 + 6 * n)}
    if not re.search(r"opus-4-[78]", _LLM_MODEL):  # opus 4.7/4.8 reject `temperature`
        inf["temperature"] = 0
    try:
        resp = _bedrock_runtime().converse(
            modelId=_LLM_MODEL,
            system=[{"text": _LLM_SYS}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig=inf,
        )
        txt = resp["output"]["message"]["content"][0]["text"]
    except Exception as e:  # noqa: BLE001 — Bedrock outage/throttle/creds must not 500 search
        # Graceful degradation: the pool arrives cosine-descending, so preserving the
        # incoming order (descending scores) falls back to plain retrieval, not chaos.
        log.warning("LLM rerank failed (%s: %s); falling back to cosine order",
                    type(e).__name__, str(e)[:120])
        return [(n - i) / n for i in range(n)]
    # Parse the '|' relevance boundary: passages before it are the model's "relevant"
    # set. Rank order is preserved (hit@k unchanged); scores are BANDED so the relevant
    # set lands in (0.5, 1.0] and the rest in (0, 0.5) — a cutoff at 0.5 is then the
    # model's own per-query relevance boundary, not an arbitrary rank cut.
    before, sep, after = txt.partition("|")

    def _idxs(s: str) -> list[int]:
        out, seen = [], set()
        for x in (int(mm) - 1 for mm in re.findall(r"\d+", s)):
            if 0 <= x < n and x not in seen:
                seen.add(x); out.append(x)
        return out

    rel, rest = _idxs(before), _idxs(after)
    order, seen = [], set()
    for x in rel + rest:
        if x not in seen:
            seen.add(x); order.append(x)
    order += [i for i in range(n) if i not in seen]  # dropped -> end (treated non-relevant)
    m = len(rel) if sep else len(order)  # no boundary returned -> treat all as relevant
    scores = [0.0] * n
    for pos, doc_i in enumerate(order):
        if pos < m:  # relevant band: (0.5, 1.0], rank-ordered
            scores[doc_i] = 1.0 - 0.5 * pos / max(1, m)
        else:        # tangential/irrelevant band: (0, 0.5), rank-ordered
            r = pos - m
            scores[doc_i] = 0.5 * (1.0 - (r + 1) / (n - m + 1))
    return scores


def _voyage_rerank(query: str, docs: list[str]) -> list[float]:
    """Voyage rerank-2.5 via REST. Returns a [0,1] relevance score per doc, aligned to
    `docs`. An optional instruction (ACI_RERANK_VOYAGE_INSTRUCTION) is prepended to the
    query to steer ranking (rerank-2.5's instruction-following). Graceful cosine-order
    fallback so an API outage/throttle never 500s search."""
    import json as _json
    import urllib.request

    n = len(docs)
    q = query or " "
    if _VOYAGE_INSTRUCTION:
        q = f"{_VOYAGE_INSTRUCTION}\nQuery: {q}"
    payload = _json.dumps({
        "query": q[:7000],                                # <= 8000-token cap (chars ~4/token)
        "documents": [(d or " ")[:16000] for d in docs],  # 32K-token ctx -> generous char cap
        "model": _VOYAGE_MODEL,
        "top_k": n,
        "truncation": True,
    }).encode()
    req = urllib.request.Request(
        _VOYAGE_URL, data=payload,
        headers={"Authorization": f"Bearer {os.getenv('VOYAGE_API_KEY', '')}",
                 "Content-Type": "application/json"})
    import time as _time
    data = None
    for attempt in range(3):  # retry transient read-timeouts / ephemeral-port exhaustion
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = _json.loads(r.read())
            break
        except Exception as e:  # noqa: BLE001 — API outage/throttle/creds must not 500 search
            if attempt == 2:
                log.warning("Voyage rerank failed after retries (%s: %s); cosine fallback",
                            type(e).__name__, str(e)[:160])
                return [(n - i) / n for i in range(n)]
            _time.sleep(0.4 * (attempt + 1))  # backoff: let sockets free / transient recover
    scores = [0.0] * n
    for item in data.get("data", []):
        i = item.get("index")
        if isinstance(i, int) and 0 <= i < n:
            scores[i] = float(item.get("relevance_score", 0.0))
    return scores


def crossencoder_scores(query: str, docs: list[str]) -> list[float]:
    """Local cross-encoder logit scores (separable around 0), aligned to `docs`. This is
    the 'minilm' search backend, but also the right tool — regardless of ACI_RERANK_BACKEND
    — for callers that need per-passage RELEVANCE with a meaningful threshold (e.g.
    /api/relevance highlighting): the cohere/LLM backends emit rank scores, not relevance."""
    if not docs:
        return []
    batch = min(len(docs), _BATCH) or 1
    with _SEM:  # bound concurrent inferences -> bound peak memory (see _SEM above)
        return [float(s) for s in _ce().rerank(query, docs, batch_size=batch)]


def _cascade_rerank(query: str, docs: list[str]) -> list[float]:
    """Two-stage rerank: a cheap pre-filter picks the top-K finalists, then claude listwise
    reorders ONLY those K. Finalists map to [0.5, 1.0] (claude's order); non-finalists to
    (0, 0.5) in pre-filter order — so the result stays unit-scale and finalists always
    outrank the rest. Small pools (<= K) skip the pre-filter and go straight to the LLM."""
    n = len(docs)
    if n <= _CASCADE_K:
        return _llm_rerank(query, docs)
    if _CASCADE_PREFILTER == "cosine":
        order = list(range(n))  # pool arrives cosine-descending from retrieval
    else:
        pre = crossencoder_scores(query, docs)  # local minilm cross-encoder
        order = sorted(range(n), key=lambda i: pre[i], reverse=True)
    top = order[:_CASCADE_K]
    llm = _llm_rerank(query, [docs[i] for i in top])  # claude reorders the K finalists
    scores = [0.0] * n
    for pos, i in enumerate(top):
        scores[i] = 0.5 + 0.5 * llm[pos]                        # finalists -> [0.5, 1.0]
    tail = order[_CASCADE_K:]
    for rank, i in enumerate(tail):
        scores[i] = 0.5 * (1.0 - (rank + 1) / (len(tail) + 1))  # non-finalists -> (0, 0.5) desc
    return scores


def rerank(query: str, docs: list[str]) -> list[float]:
    """Return a relevance score per doc (higher = more relevant), aligned to `docs`.
    Scale depends on the backend — see module docstring / score_scale()."""
    if not docs:
        return []
    if _BACKEND in ("cohere", "cohere-bedrock"):
        return _cohere_rerank(query, docs)  # no semaphore: offloaded, holds no memory
    if _BACKEND in _LLM_BACKENDS:
        return _llm_rerank(query, docs)  # no semaphore: offloaded, holds no memory
    if _BACKEND in _VOYAGE_BACKENDS:
        return _voyage_rerank(query, docs)  # no semaphore: offloaded REST call
    if _BACKEND in _CASCADE_BACKENDS:
        return _cascade_rerank(query, docs)  # minilm/cosine pre-filter -> claude on top-K
    return crossencoder_scores(query, docs)
