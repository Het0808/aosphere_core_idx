"""Benchmark embedder/text variants for candidate-pool RECALL.

For each query we treat the cross-encoder as the relevance oracle (it's validated):
relevant = clauses scoring >= ORACLE_T over ALL sections. Then for each bi-encoder
variant we measure recall@K = fraction of relevant clauses captured in its top-K
cosine pool. Higher recall = the reranker has more relevant clauses to surface.

Reads section text from a region's local content.json (no S3). Titan needs dev1
Bedrock creds in env; bge + reranker run locally.

  eval "$(aws configure export-credentials --profile dev1 --format env)"
  AWS_REGION_NAME=eu-west-2 .venv/bin/python scripts/bench_embed.py Germany
"""
import json
import re
import sys

import numpy as np
from aosphere_core_index.config import settings as _settings

REGION = sys.argv[1] if len(sys.argv) > 1 else "Germany"
ORACLE_T = 4.0          # cross-encoder score => "relevant"
KS = (40, 80)
FN = re.compile(r"\[\^\d+\]")

QUERIES = [
    "how long do we have to report a data breach to the regulator",
    "when must an organisation appoint a data protection officer",
    "is consent required to use cookies",
    "rules for transferring personal data internationally",
    "what are the penalties or fines for non-compliance",
]

content = json.load(open(f"data/regions/{REGION}/artifacts/{REGION}.content.json"))
secs = content["sections"]
by_key = {s["key"]: s for s in secs}
idx = {s["key"]: i for i, s in enumerate(secs)}


def crumb(s):
    parts, cur = [], s
    while cur is not None:
        parts.append(f"{cur['key']} {cur['title']}")
        cur = by_key.get(cur.get("parent_key")) if cur.get("parent_key") else None
    return " > ".join(reversed(parts))


def body(s, n):
    t = " ".join(e["text"] for e in s["elements"] if e["kind"] == "body").strip()
    if not t:
        t = " ".join(e["text"] for e in s["elements"] if e["kind"] == "question").strip()
    return FN.sub("", t)[:n]


def all_text(s, n):
    return FN.sub("", " ".join(e["text"] for e in s["elements"]))[:n]


# embed-text variants
TEXTS = {
    "bge_current": [f"{crumb(s)}\n{all_text(s,1500)}" for s in secs],   # breadcrumb + ALL (incl question)
    "bge_answer":  [f"{crumb(s)}\n{body(s,1500)}" for s in secs],        # breadcrumb + ANSWER only
}
# rerank oracle docs (answer-focused, like the live path)
ODOCS = [f"{REGION} — {crumb(s)}. {body(s,500)}" for s in secs]


def cosine_recall(matrix, qvec, relevant):
    sims = matrix @ qvec
    order = np.argsort(-sims)
    out = {}
    for K in KS:
        topk = set(order[:K].tolist())
        out[K] = len(topk & relevant) / max(1, len(relevant))
    return out


def run_bge(texts):
    from fastembed import TextEmbedding
    m = TextEmbedding("BAAI/bge-small-en-v1.5")
    M = np.asarray(list(m.embed(texts)), dtype="float32")
    M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    Q = np.asarray(list(m.embed(QUERIES)), dtype="float32")
    Q /= (np.linalg.norm(Q, axis=1, keepdims=True) + 1e-9)
    return M, Q


def run_titan(texts):
    import boto3
    from concurrent.futures import ThreadPoolExecutor
    br = boto3.client("bedrock-runtime", region_name=_settings.bedrock_region)

    def emb(t):
        r = br.invoke_model(modelId="amazon.titan-embed-text-v2:0",
                            body=json.dumps({"inputText": t[:8000], "dimensions": 1024, "normalize": True}))
        return json.loads(r["body"].read())["embedding"]
    with ThreadPoolExecutor(max_workers=10) as ex:
        M = np.asarray(list(ex.map(emb, texts)), dtype="float32")
        Q = np.asarray(list(ex.map(emb, QUERIES)), dtype="float32")
    return M, Q


print(f"Region {REGION}: {len(secs)} sections, {len(QUERIES)} queries", flush=True)
# oracle: cross-encoder relevant set per query
from fastembed.rerank.cross_encoder import TextCrossEncoder
ce = TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2")
relevant_sets = []
for qi, q in enumerate(QUERIES):
    scores = list(ce.rerank(q, ODOCS))
    relevant_sets.append({i for i, sc in enumerate(scores) if sc >= ORACLE_T})
    print(f"  oracle {qi+1}/{len(QUERIES)} done", flush=True)
import gc
del ce
gc.collect()
print("avg relevant/query (oracle):", round(np.mean([len(r) for r in relevant_sets]), 1))

variants = {"bge_current": ("bge", TEXTS["bge_current"]),
            "bge_answer": ("bge", TEXTS["bge_answer"]),
            "titan_answer": ("titan", TEXTS["bge_answer"])}
for name, (eng, texts) in variants.items():
    M, Q = (run_bge(texts) if eng == "bge" else run_titan(texts))
    agg = {K: [] for K in KS}
    for qi, rel in enumerate(relevant_sets):
        if not rel:
            continue
        r = cosine_recall(M, Q[qi], rel)
        for K in KS:
            agg[K].append(r[K])
    line = "  ".join(f"recall@{K}={np.mean(agg[K]):.2f}" for K in KS)
    print(f"  {name:14} {line}", flush=True)
