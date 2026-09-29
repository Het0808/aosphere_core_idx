"""Retrieval-quality harness — measures the *retriever* in isolation, not the answer.

Why this exists: ``eval_benchmark.py`` only judges the final LLM answer, so a bad
retrieval that the model papers over looks fine, and a good retrieval with clumsy
phrasing looks bad. This harness uses a signal we get for free: the approved gold
answers cite clause keys ("See sections B2.1, B2.2 ..."). We treat those cited
clauses as the relevance labels and measure whether the bi-encoder pulls them into
the top-k. It is deterministic (no LLM judge), cheap, and isolates the part you
actually tune (embedder / reranker) from generation.

What it reports, overall and per region group:
  - recall@k       fraction of a question's gold clauses found in top-k
  - hit@k          fraction of questions with >=1 gold clause in top-k
  - MRR            mean reciprocal rank of the first gold clause
  - coverage stats how many gold rows were usable (had clause keys + a resolved
                   jurisdiction that exists in the index)

Matching (gold key "A2" vs index key "A2.2(a)"):
  --match exact       only the identical key counts
  --match descendant  the cited key OR any sub-clause of it counts   (default)
  --match family      ancestors count too (retrieving the parent section is ok)

Usage (bge / local, no AWS needed):
  ACI_OFFLINE=1 python scripts/eval_retrieval.py [--k 1,3,5,10,20] [--pool 200]
                                                 [--match descendant] [--rerank]
                                                 [--limit N] [--out /tmp/retrieval.json]

Titan index: set ACI_EMBED_BACKEND=titan (+ Bedrock creds); the query encoder is
matched to the index's recorded model automatically, so it can't mismatch.

Output: a JSON results file + a printed summary table.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

# Reuse the EXACT gold loader + jurisdiction resolution the answer eval uses, so the
# two harnesses always agree on what "the benchmark" is.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_benchmark import load_gold  # noqa: E402

from aosphere_core_index.embeddings.embedder import make_embedder  # noqa: E402
from aosphere_core_index.embeddings.multi_index import (  # noqa: E402
    index_model,
    load_multi,
)

try:
    from aosphere_core_index.regions.region_map import region_for  # noqa: E402
except Exception:  # region grouping is optional — fall back to "(all)"
    region_for = None  # type: ignore

# Canonical clause reference, same shape used by the extractor (extract/docx_extract.py).
_CLAUSE_RE = re.compile(r"\b[A-K]\d+(?:\.\d+)*(?:\([a-z]+\))*")


def gold_keys(answer: str) -> list[str]:
    """Clause keys cited in a gold answer, de-duped, order preserved."""
    seen, out = set(), []
    for k in _CLAUSE_RE.findall(answer or ""):
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _is_descendant(child: str, parent: str) -> bool:
    """True if `child` is `parent` or a sub-clause of it. Boundary-aware so 'A2'
    does NOT match 'A20' but does match 'A2.1' and 'A2(a)'."""
    if child == parent:
        return True
    if not child.startswith(parent):
        return False
    nxt = child[len(parent)]
    return nxt in ".("


def relevant_index_keys(gold: list[str], index_keys: set[str], mode: str) -> set[str]:
    """Expand cited gold keys into the set of index keys that count as relevant,
    given the matching mode and the keys that actually exist for this jurisdiction."""
    rel: set[str] = set()
    for g in gold:
        if mode == "exact":
            if g in index_keys:
                rel.add(g)
            continue
        for k in index_keys:
            if mode == "descendant" and _is_descendant(k, g):
                rel.add(k)
            elif mode == "family" and (_is_descendant(k, g) or _is_descendant(g, k)):
                rel.add(k)
    return rel


def keys_by_jurisdiction(mi) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for jur, key in zip(mi.row_region, mi.keys):
        out[str(jur)].add(key)
    return out


def _region(jur: str) -> str:
    if region_for is None:
        return "(all)"
    try:
        return region_for(jur) or "(unmapped)"
    except Exception:
        return "(unmapped)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", default="1,3,5,10,20", help="comma list of cutoffs")
    ap.add_argument("--pool", type=int, default=200, help="candidates retrieved per query")
    ap.add_argument("--match", choices=("exact", "descendant", "family"), default="descendant")
    ap.add_argument("--rerank", action="store_true",
                    help="rerank the pool with the cross-encoder before scoring")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="/tmp/retrieval_results.json")
    args = ap.parse_args()
    ks = sorted(int(x) for x in args.k.split(","))
    maxk = max(ks)

    mi = load_multi()
    if mi is None or mi.matrix.shape[0] == 0:
        print("ERROR: no multi-index found (data/regions/_multi/multi.npz). Build it first.",
              file=sys.stderr)
        return 2
    jur_keys = keys_by_jurisdiction(mi)
    embedder = make_embedder(index_model())  # encoder matched to the index's model
    print(f"index: {mi.matrix.shape[0]} rows across {len(mi.regions)} jurisdictions, "
          f"model={mi.model} | encoder={embedder.name} | match={args.match} "
          f"| pool={args.pool} | rerank={args.rerank}", flush=True)

    gold = load_gold()
    if args.limit:
        gold = gold[: args.limit]

    # Coverage accounting — every dropped row is reported, not silently ignored.
    n_total = len(gold)
    skipped_no_keys = skipped_no_jur = skipped_jur_not_in_index = skipped_no_relevant = 0

    rerank_fn = None
    if args.rerank:
        from aosphere_core_index.embeddings import reranker

        rerank_fn = reranker.rerank

    per_q = []
    for g in gold:
        keys = gold_keys(g["gold"])
        if not keys:
            skipped_no_keys += 1
            continue
        jur = g["jurisdiction"]
        if not jur:
            skipped_no_jur += 1
            continue
        if jur not in jur_keys:
            skipped_jur_not_in_index += 1
            continue
        rel = relevant_index_keys(keys, jur_keys[jur], args.match)
        if not rel:
            # cited clauses don't exist in this index build — useful signal itself
            skipped_no_relevant += 1
            continue

        qv = embedder.embed([g["q"]])[0]
        hits = mi.search(qv, k=args.pool, jurisdictions=[jur])

        if rerank_fn is not None and hits:
            scores = rerank_fn(g["q"], [f"{h['key']} {h['title']}" for h in hits])
            hits = [h for _, h in sorted(zip(scores, hits), key=lambda t: -t[0])]

        ranked = [h["key"] for h in hits]
        rel_ranks = [i + 1 for i, k in enumerate(ranked) if k in rel]
        first = rel_ranks[0] if rel_ranks else None
        rec = {f"recall@{k}": sum(1 for r in rel_ranks if r <= k) / len(rel) for k in ks}
        hit = {f"hit@{k}": (1.0 if first is not None and first <= k else 0.0) for k in ks}
        per_q.append({
            "q": g["q"], "code": g["code"], "jurisdiction": jur, "region": _region(jur),
            "gold_keys": keys, "n_relevant": len(rel),
            "first_rank": first, "rr": (1.0 / first) if first else 0.0,
            **rec, **hit,
        })

    n = len(per_q)
    if not n:
        print("No scorable questions (after coverage filtering).", file=sys.stderr)
        return 1

    def agg(rows):
        m = {"n": len(rows), "MRR": np.mean([r["rr"] for r in rows])}
        for k in ks:
            m[f"recall@{k}"] = np.mean([r[f"recall@{k}"] for r in rows])
            m[f"hit@{k}"] = np.mean([r[f"hit@{k}"] for r in rows])
        return {kk: (round(float(v), 4) if isinstance(v, float) else v) for kk, v in m.items()}

    overall = agg(per_q)
    by_region = {}
    buckets = defaultdict(list)
    for r in per_q:
        buckets[r["region"]].append(r)
    for reg, rows in sorted(buckets.items()):
        by_region[reg] = agg(rows)

    coverage = {
        "gold_total": n_total, "scored": n,
        "skipped_no_clause_keys": skipped_no_keys,
        "skipped_no_jurisdiction": skipped_no_jur,
        "skipped_jurisdiction_not_in_index": skipped_jur_not_in_index,
        "skipped_cited_clauses_absent_from_index": skipped_no_relevant,
    }

    json.dump({"config": {"k": ks, "pool": args.pool, "match": args.match,
                          "rerank": args.rerank, "index_model": mi.model},
               "coverage": coverage, "overall": overall, "by_region": by_region,
               "results": per_q}, open(args.out, "w"), indent=1)

    # ---- printed summary ----
    print("\n==== COVERAGE ====")
    for kk, v in coverage.items():
        print(f"  {kk:42} {v}")

    cols = ["recall@%d" % k for k in ks] + ["hit@%d" % k for k in ks] + ["MRR"]
    print("\n==== RETRIEVAL (overall) ====")
    print("  n=%d" % overall["n"])
    print("  " + "  ".join(f"{c}={overall[c]:.3f}" for c in cols))

    if len(by_region) > 1:
        print("\n==== BY REGION ====")
        head = "  {:<14}{:>5}".format("region", "n") + "".join(f"{c:>11}" for c in cols)
        print(head)
        for reg, m in by_region.items():
            row = "  {:<14}{:>5}".format(reg[:14], m["n"]) + "".join(f"{m[c]:>11.3f}" for c in cols)
            print(row)

    print(f"\nsaved -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
