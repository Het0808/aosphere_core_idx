"""Cross-region recall, 4-way: bge-small (prod) vs bge-large vs mxbai-large vs Titan v2.

Same pooled-judgment harness as bench_xregion, on a representative SUBSET of regions
(bge-large is ~10x bge-small; full corpus would be hours on CPU). Model *ranking*
transfers from the subset. Local 1024-dim models get the bge query-instruction prefix
+ answer-focused text — their best config. We test whether a LOCAL model matches Titan
(which would keep search offline / zero per-query cost).

  eval "$(aws configure export-credentials --profile dev1 --format env)"
  AWS_REGION_NAME=eu-west-2 .venv/bin/python scripts/bench_models.py [n_regions]
"""
import glob
import json
import os
import re
import sys

import numpy as np
from aosphere_core_index.config import settings as _settings

N_REGIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 12
ORACLE_T = 4.0
POOL_N = 200
KS = (40, 80, 200)
FN = re.compile(r"\[\^\d+\]")
CACHE = "/private/tmp/claude-501/-Users-gaurangpatel-Documents-Projects-aosphere-core-index/3031ae4b-8e2c-4286-a971-f0793b734c40/scratchpad"
BGE_PREFIX = "Represent this sentence for searching relevant passages: "

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


# ---- corpus: multi-index rows, subset to N_REGIONS spread across the 67 ----
from aosphere_core_index.embeddings.multi_index import load_multi

mi = load_multi()
all_regions = sorted(set(map(str, mi.row_region)))
step = max(1, len(all_regions) // N_REGIONS)
subset = set(all_regions[::step][:N_REGIONS])
sub_idx = [i for i in range(len(mi.keys)) if str(mi.row_region[i]) in subset]
rows = [(str(mi.row_region[i]), mi.keys[i]) for i in sub_idx]
print(f"subset: {len(subset)} regions, {len(rows)} sections", flush=True)
print("  " + ", ".join(sorted(subset)), flush=True)

text = load_section_text()
crumbs = [text.get(rk, (mi.titles[sub_idx[j]], ""))[0] for j, rk in enumerate(rows)]
bodies = [text.get(rk, ("", ""))[1] for rk in rows]
answer_texts = [f"{c}\n{b[:1500]}" for c, b in zip(crumbs, bodies)]
docs = [f"{r} — {c}. {b[:500]}" for (r, _), c, b in zip(rows, crumbs, bodies)]


def cache_embed(tag, fn):
    p = f"{CACHE}/bm_{tag}_{N_REGIONS}.npy"
    pq = f"{CACHE}/bm_{tag}_{N_REGIONS}_q.npy"
    if os.path.exists(p) and os.path.exists(pq):
        print(f"  {tag}: from cache", flush=True)
        return norm(np.load(p)), norm(np.load(pq))
    M, Q = fn()
    np.save(p, M); np.save(pq, Q)
    print(f"  {tag}: embedded {M.shape}", flush=True)
    return norm(M), norm(Q)


def local_bge(model, prefix):
    def fn():
        from fastembed import TextEmbedding
        m = TextEmbedding(model)
        M = np.asarray(list(m.embed(answer_texts)), dtype="float32")
        Q = np.asarray(list(m.embed([prefix + q for q in QUERIES])), dtype="float32")
        return M, Q
    return fn


def titan_sub():
    # docs: subset the full cached titan matrix by original row index
    full = np.load(f"{CACHE}/titan_docs.npy")
    M = full[sub_idx]
    # queries: embed fresh (8 calls)
    import boto3
    br = boto3.client("bedrock-runtime", region_name=_settings.bedrock_region)

    def emb(t):
        r = br.invoke_model(modelId="amazon.titan-embed-text-v2:0",
                            body=json.dumps({"inputText": t, "dimensions": 1024, "normalize": True}))
        return json.loads(r["body"].read())["embedding"]
    Q = np.asarray([emb(q) for q in QUERIES], dtype="float32")
    return M, Q


# bge-small (prod vectors, prod text incl question, NO prefix — represents current prod)
bs_M = norm(mi.matrix[sub_idx].astype("float32"))
from aosphere_core_index.embeddings.embedder import FastEmbedEmbedder
bs_Q = norm(np.asarray(FastEmbedEmbedder().embed(QUERIES), dtype="float32"))

tM, tQ = titan_sub()
MODELS = {
    "bge-small(prod)": (bs_M, bs_Q),
    "bge-large+pfx": cache_embed("bgelarge", local_bge("BAAI/bge-large-en-v1.5", BGE_PREFIX)),
    "mxbai-large+pfx": cache_embed("mxbai", local_bge("mixedbread-ai/mxbai-embed-large-v1", BGE_PREFIX)),
    "titan-v2": (norm(tM.astype("float32")), norm(tQ.astype("float32"))),
}

# ---- pooled relevance (union of ALL models' top-POOL_N) + recall ----
from fastembed.rerank.cross_encoder import TextCrossEncoder

ce = TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2")


def topk(M, qvec, k):
    return np.argsort(-(M @ qvec))[:k].tolist()


agg = {name: {K: [] for K in KS} for name in MODELS}
nrel = []
for qi, q in enumerate(QUERIES):
    ranks = {name: topk(M, Q[qi], POOL_N) for name, (M, Q) in MODELS.items()}
    pool = sorted({i for r in ranks.values() for i in r})
    scores = list(ce.rerank(q, [docs[i] for i in pool]))
    relevant = {pool[j] for j, sc in enumerate(scores) if sc >= ORACLE_T}
    nrel.append(len(relevant))
    for name, rank in ranks.items():
        for K in KS:
            agg[name][K].append(len(set(rank[:K]) & relevant) / max(1, len(relevant)))
    print(f"  q{qi+1}: {len(relevant):3d} relevant in pool({len(pool)})", flush=True)

print(f"\nsubset {len(subset)} regions / {len(rows)} sections · avg relevant/query: {np.mean(nrel):.1f}")
for name in MODELS:
    line = "  ".join(f"recall@{K}={np.mean(agg[name][K]):.3f}" for K in KS)
    print(f"  {name:18} {line}", flush=True)
