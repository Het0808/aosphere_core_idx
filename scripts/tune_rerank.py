"""Rerank/hit@1 tuning harness — sweep retrieval configs against the eval cases.

Goal: lift hit@1 / MRR (currently 36% / 0.51) without regressing hit@20. Runs the
same scorable cases as eval_cases.py through a PARAMETERIZED copy of the
search_all pipeline, one config at a time, and reports hit@1/3/5/20 + MRR per
config so winners are directly comparable to the baseline eval report.

Swept knobs (see CONFIGS):
  pool_mult   bi-encoder candidate pool = k * pool_mult (prod: 2)
  blend       final = rerank_logit + blend * cosine       (prod: 0)
  canon       append canonical jurisdiction names for alias/city/regulator
              mentions to the query before embed+rerank ("...in Seoul?" ->
              "...in Seoul? (South Korea)")                (prod: off)
  guid_chars  curated-guidance text included in the rerank doc (prod: 600)
  snip_chars  clause snippet length in the rerank doc      (prod: 500)

Usage (same env as eval_cases.py — ACI_OFFLINE=1, matching embed backend/creds):
  .venv/bin/python scripts/tune_rerank.py [--k=20] [--limit=100] [--product=P] \
                                          [--configs=baseline,canon]
Writes test/tune_rerank_report.md. Read-only w.r.t. the index and eval workbooks.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, replace

sys.path.insert(0, "scripts")
from eval_cases import hit_ok, load_cases  # noqa: E402  (reuse the case loader/scorer)

from aosphere_core_index.regions.aliases import match_aliases  # noqa: E402
from aosphere_core_index.regions.region_map import qualified, split_region  # noqa: E402


@dataclass(frozen=True)
class Cfg:
    name: str
    pool_mult: int = 2
    blend: float = 0.0
    canon: bool = False
    guid_chars: int = 600
    snip_chars: int = 500


BASE = Cfg("baseline")  # mirrors production search_all
CONFIGS: list[Cfg] = [
    BASE,
    replace(BASE, name="canon", canon=True),
    replace(BASE, name="pool-x4", pool_mult=4),
    replace(BASE, name="blend-0.5", blend=0.5),
    replace(BASE, name="blend-1.0", blend=1.0),
    replace(BASE, name="guid-0", guid_chars=0),
    replace(BASE, name="guid-1200", guid_chars=1200),
    replace(BASE, name="snip-300", snip_chars=300),
    replace(BASE, name="canon+blend-0.5", canon=True, blend=0.5),
]


def canonicalize(query: str, region_id: str) -> str:
    """Append canonical jurisdiction names implied by aliases (cities, regulators,
    colloquial names) so the embedder/cross-encoder can match the jurisdiction-
    prefixed docs. Only names not already literally present are appended."""
    bare = split_region(region_id)[1]
    ql = query.lower()
    implied = {j for j in match_aliases(query) if j.lower() not in ql}
    # Scoped runs: only the scoped jurisdiction's name is useful context.
    implied = {j for j in implied if j == bare}
    if not implied:
        return query
    return f"{query} ({', '.join(sorted(implied))})"


def build_doc(bundle, h: dict, cfg: Cfg, snippet_fn) -> str:
    """Rerank doc — same composition as registry.search_all, parameterized."""
    if h.get("kind") == "alert":
        base = h["key"].split("#", 1)[0]
        aid = base.split(":", 1)[1] if ":" in base else base
        rec = bundle.content.get("alerts_by_id", {}).get(aid, {})
        att = rec.get("attachment_text", "") or ""
        return (f"{h['jurisdiction']}. Alert: {rec.get('title', h.get('title', ''))}. "
                f"{rec.get('summary', '')} {att[:400]}").strip()
    sec = bundle.sections_by_key.get(h["key"], {})
    guid = " ".join(
        f"{a.get('subject', '')} {a.get('question', '')} {a.get('answer', '')}".strip()
        for a in bundle.content["answers_by_clause"].get(h["key"], []))
    return (f"{h['jurisdiction']}. {sec.get('title', '')}. "
            f"{snippet_fn(sec, cfg.snip_chars, jurisdiction=h['jurisdiction'])} "
            f"{guid[:cfg.guid_chars]}").strip()


def run_config(cfg: Cfg, cases: list[dict], k: int) -> dict:
    from aosphere_core_index.embeddings.reranker import rerank
    from aosphere_core_index.service.registry import _snippet, embedder, get_bundle, get_multi

    mi = get_multi()

    def _ident(h):
        key = h["key"].split("#", 1)[0] if h.get("kind") == "alert" else h["key"]
        return (h["jurisdiction"], key)

    ranks: list[int] = []
    for i, c in enumerate(cases, 1):
        region = c["region_id"]
        q = canonicalize(c["question"], region) if cfg.canon else c["question"]
        qvec = embedder().embed([q])[0]
        seen, hits = set(), []
        for h in mi.search(qvec, k * cfg.pool_mult, jurisdictions=[region]):
            ident = _ident(h)
            if ident not in seen:
                seen.add(ident)
                hits.append(h)
        hits = hits[:k]
        bundle = get_bundle(region)
        docs = [build_doc(bundle, h, cfg, _snippet) for h in hits]
        for h, s in zip(hits, rerank(q, docs)):
            h["final"] = s + cfg.blend * h["score"]
        hits.sort(key=lambda h: -h["final"])
        keys = [_ident(h)[1] for h in hits]
        _, rank = hit_ok([s for (_j, s) in c["expected"]], keys)
        ranks.append(rank)
        if i % 50 == 0:
            print(f"    {cfg.name}: {i}/{len(cases)}", flush=True)

    n = len(ranks) or 1
    at = lambda kk: 100 * sum(1 for r in ranks if 0 <= r < kk) / n  # noqa: E731
    return {"config": cfg.name, "n": n, "hit@1": at(1), "hit@3": at(3), "hit@5": at(5),
            "hit@20": at(k), "mrr": sum(1 / (r + 1) for r in ranks if r >= 0) / n}


def main() -> None:
    k = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--k=")), 20)
    limit = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--limit=")), 0)
    prod = next((a.split("=")[1] for a in sys.argv if a.startswith("--product=")), None)
    names = next((a.split("=")[1].split(",") for a in sys.argv if a.startswith("--configs=")), None)
    configs = [c for c in CONFIGS if names is None or c.name in names]

    from aosphere_core_index.embeddings.multi_index import load_multi
    indexed = set(load_multi().regions)
    cases = load_cases()
    if prod:
        cases = [c for c in cases if c["product"] == prod]
    for c in cases:
        c["region_id"] = qualified(c["product"], c["expected"][0][0]) if c["expected"] else None
    cases = [c for c in cases if c["expected"] and c["region_id"] in indexed]
    if limit:
        cases = cases[:limit]
    print(f"{len(cases)} scorable cases | k={k} | configs: {[c.name for c in configs]}", flush=True)

    rows = []
    for cfg in configs:
        print(f"  running {cfg.name}…", flush=True)
        rows.append(run_config(cfg, cases, k))

    hdr = "| config | n | hit@1 | hit@3 | hit@5 | hit@20 | MRR |"
    sep = "|---|---|---|---|---|---|---|"
    body = [f"| {r['config']} | {r['n']} | {r['hit@1']:.0f}% | {r['hit@3']:.0f}% | "
            f"{r['hit@5']:.0f}% | {r['hit@20']:.0f}% | {r['mrr']:.2f} |" for r in rows]
    md = "\n".join(["# Rerank tuning sweep", "", hdr, sep, *body, "",
                    "_Adopt a config only if hit@1/MRR improve without hit@20 regressing._"])
    open("test/tune_rerank_report.md", "w").write(md)
    print("\n" + md + "\n\nsaved -> test/tune_rerank_report.md")


if __name__ == "__main__":
    main()
