"""EXPERIMENT (reversible): does chunk-level retrieval beat section-level on the
Colorado scattered-exemptions case?

Section-level = the current index (one vector per section, from Colorado.sections.npz).
Chunk-level   = split each section into ~100-word sub-section chunks, embed each,
                map a retrieved chunk back to its section key.

For Colorado exemption queries we check whether the clauses that actually contain the
CPA exemptions (A1.1, A2.1 — HIPAA/COPPA/GLBA/publicly-available) surface in top-k.

  eval "$(aws configure export-credentials --profile dev1 --format env)"
  ACI_EMBED_BACKEND=titan ACI_BEDROCK_REGION=eu-west-2 python scripts/exp_chunk_colorado.py
"""
import json
import re

import numpy as np

from aosphere_core_index.embeddings.embedder import make_embedder
from aosphere_core_index.embeddings.section_index import answer_text_dict, load_index

JUR = "United States - Colorado"
ART = f"data/regions/{JUR}/artifacts"
FN = re.compile(r"\[\^\d+\]")


def norm(M):
    return M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)


def breadcrumbs(secs):
    by = {s["key"]: s for s in secs}
    out = {}
    for s in secs:
        crumb, cur = [], s
        while cur is not None:
            crumb.append(cur["title"])
            cur = by.get(cur.get("parent_key")) if cur.get("parent_key") else None
        out[s["key"]] = " > ".join(reversed(crumb))
    return out


def chunk_units(secs, crumbs, words=100):
    """Sub-section chunks: split each section's answer text into ~`words`-word windows,
    each tagged with the section key + breadcrumb (contextual chunk)."""
    units = []
    for s in secs:
        txt = FN.sub("", answer_text_dict(s["elements"], jurisdiction=JUR)).strip()
        if not txt:
            continue
        toks = txt.split()
        for i in range(0, len(toks), words):
            chunk = " ".join(toks[i:i + words])
            units.append((s["key"], f"{crumbs[s['key']]}\n{chunk}"))
    return units


def topk_keys(qv, M, keys, k):
    order = np.argsort(-(M @ qv))
    seen, out = set(), []
    for o in order:
        if keys[o] not in seen:
            seen.add(keys[o]); out.append(keys[o])
        if len(out) >= k:
            break
    return out


def main():
    secs = json.load(open(f"{ART}/{JUR}.content.json"))["sections"]
    crumbs = breadcrumbs(secs)
    emb = make_embedder()

    # section-level: reuse the live index vectors
    si = load_index(f"{ART}/{JUR}.sections.npz")
    sM, sKeys = norm(si.matrix.astype("float32")), list(si.keys)

    # chunk-level: build now
    cu = chunk_units(secs, crumbs)
    cKeys = [k for k, _ in cu]
    cM = norm(np.asarray(emb.embed([t for _, t in cu]), dtype="float32"))
    print(f"section vectors: {len(sKeys)} | chunk vectors: {len(cKeys)} (model={emb.name})\n")

    # relevant = clauses that actually carry the CPA exemptions
    terms = ["publicly available", "coppa", "gramm", "glba", "hipaa", "protected health"]
    relevant = sorted({s["key"] for s in secs
                       if sum(t in " ".join(e["text"] for e in s["elements"]).lower() for t in terms) >= 3})
    print(f"exemption-bearing clauses (relevance labels): {relevant}\n")

    queries = ["summarise exemptions to privacy law colorado",
               "what data and entities are exempt from the colorado privacy act",
               "colorado privacy act exemptions HIPAA GLBA publicly available information"]
    K = 10
    for q in queries:
        qv = norm(np.asarray(emb.embed([q]), dtype="float32"))[0]
        s_top = topk_keys(qv, sM, sKeys, K)
        c_top = topk_keys(qv, cM, cKeys, K)
        s_hit = [r for r in relevant if r in s_top]
        c_hit = [r for r in relevant if r in c_top]
        print(f"Q: {q}")
        print(f"  section top{K}: {s_top}")
        print(f"     -> exemption clauses found: {s_hit or 'NONE'}")
        print(f"  chunk   top{K}: {c_top}")
        print(f"     -> exemption clauses found: {c_hit or 'NONE'}\n")


if __name__ == "__main__":
    main()
