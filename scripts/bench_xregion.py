"""Cross-region candidate-pool RECALL: Titan v2 vs bge-small.

The live product searches ALL regions at once: one query, top-K pooled across
~30k sections from 67 jurisdictions, then reranked. This measures whether each
bi-encoder gets the relevant clauses INTO that cross-region pool.

bge matrix is loaded from the PRODUCTION multi-index (data/regions/_multi/multi.npz)
— the exact vectors the live service uses, no recompute. Titan embeds the same rows
(answer-focused text) via Bedrock and is cached to scratchpad.

Relevance (pooled judgments, TREC-style): per query, take the union of both models'
top-POOL_N cross-region hits, rerank just that union with the cross-encoder, call
score >= ORACLE_T relevant. recall@K = fraction of relevant in each model's top-K.

  eval "$(aws configure export-credentials --profile dev1 --format env)"
  AWS_REGION_NAME=eu-west-2 .venv/bin/python scripts/bench_xregion.py
"""
import glob
import json
import os
import re

import numpy as np
from aosphere_core_index.config import settings as _settings

ORACLE_T = 4.0
POOL_N = 200
KS = (40, 80, 200)
FN = re.compile(r"\[\^\d+\]")
CACHE = "/private/tmp/claude-501/-Users-gaurangpatel-Documents-Projects-aosphere-core-index/3031ae4b-8e2c-4286-a971-f0793b734c40/scratchpad"

QUERIES = [
    "how long do we have to report a data breach to the regulator",
    "when must an organisation appoint a data protection officer",
    "is consent required to use cookies",
    "rules for transferring personal data internationally",
    "what are the penalties or fines for non-compliance",
    "do we need to register or notify the data protection authority",
    "what rights do individuals have to access their personal data",
    "is a data protection impact assessment required",
]


def norm(M):
    return M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)


def load_section_text():
    """(region, key) -> (breadcrumb, answer_body) from local content.json."""
    out = {}
    for f in sorted(glob.glob("data/regions/*/artifacts/*.content.json")):
        region = f.split("/")[2]
        secs = json.load(open(f))["sections"]
        by_key = {s["key"]: s for s in secs}

        def crumb(s):
            parts, cur = [], s
            while cur is not None:
                parts.append(f"{cur['key']} {cur['title']}")
                cur = by_key.get(cur.get("parent_key")) if cur.get("parent_key") else None
            return " > ".join(reversed(parts))

        for s in secs:
            body = " ".join(e["text"] for e in s["elements"] if e["kind"] == "body").strip()
            if not body:
                body = " ".join(e["text"] for e in s["elements"] if e["kind"] == "question").strip()
            out[(region, s["key"])] = (crumb(s), FN.sub("", body))
    return out


# ---- bge: load production matrix + embed queries with the same model ----
from aosphere_core_index.embeddings.multi_index import load_multi
from aosphere_core_index.embeddings.embedder import FastEmbedEmbedder

mi = load_multi()
print(f"bge matrix {mi.matrix.shape} / {len(mi.regions)} regions, model={mi.model}", flush=True)
bM = norm(mi.matrix.astype("float32"))
emb = FastEmbedEmbedder()
bQ = norm(np.asarray(emb.embed(QUERIES), dtype="float32"))

# row metadata aligned to bM
rows = [(str(mi.row_region[i]), mi.keys[i]) for i in range(len(mi.keys))]
text = load_section_text()
docs, embed_texts = [], []
for i, (region, key) in enumerate(rows):
    crumb, body = text.get((region, key), (mi.titles[i], ""))
    docs.append(f"{region} — {crumb}. {body[:500]}")
    embed_texts.append(f"{crumb}\n{body[:1500]}")

# ---- titan: embed same rows (answer text), cached ----
def titan_embed(texts, queries):
    import boto3
    from concurrent.futures import ThreadPoolExecutor

    def emb1(t):
        br = boto3.client("bedrock-runtime", region_name=_settings.bedrock_region)
        r = br.invoke_model(modelId="amazon.titan-embed-text-v2:0",
                            body=json.dumps({"inputText": t[:8000] or " ", "dimensions": 1024, "normalize": True}))
        return json.loads(r["body"].read())["embedding"]

    cache = f"{CACHE}/titan_docs.npy"
    if os.path.exists(cache) and np.load(cache).shape[0] == len(texts):
        M = np.load(cache)
        print(f"  titan docs from cache {M.shape}", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=16) as ex:
            M = np.asarray(list(ex.map(emb1, texts)), dtype="float32")
        np.save(cache, M)
        print(f"  titan docs embedded {M.shape}", flush=True)
    with ThreadPoolExecutor(max_workers=8) as ex:
        Q = np.asarray(list(ex.map(emb1, queries)), dtype="float32")
    return norm(M), norm(Q)


print("embedding titan (30k, cached after first run)...", flush=True)
tM, tQ = titan_embed(embed_texts, QUERIES)

# ---- pooled relevance + recall ----
from fastembed.rerank.cross_encoder import TextCrossEncoder
ce = TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2")


def topk(M, qvec, k):
    return np.argsort(-(M @ qvec))[:k].tolist()


agg = {"bge": {K: [] for K in KS}, "titan": {K: [] for K in KS}}
nrel = []
for qi, q in enumerate(QUERIES):
    b_rank, t_rank = topk(bM, bQ[qi], POOL_N), topk(tM, tQ[qi], POOL_N)
    pool = sorted(set(b_rank) | set(t_rank))
    scores = list(ce.rerank(q, [docs[i] for i in pool]))
    relevant = {pool[j] for j, sc in enumerate(scores) if sc >= ORACLE_T}
    nrel.append(len(relevant))
    for name, rank in (("bge", b_rank), ("titan", t_rank)):
        for K in KS:
            agg[name][K].append(len(set(rank[:K]) & relevant) / max(1, len(relevant)))
    print(f"  q{qi+1}: {len(relevant):3d} relevant in pool({len(pool)})", flush=True)

print(f"\navg relevant/query: {np.mean(nrel):.1f}  (pooled from both models' top-{POOL_N})")
for name in ("bge", "titan"):
    line = "  ".join(f"recall@{K}={np.mean(agg[name][K]):.3f}" for K in KS)
    print(f"  {name:6} {line}", flush=True)
