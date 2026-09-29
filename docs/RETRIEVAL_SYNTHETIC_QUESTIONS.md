# Retrieval augmentation: synthetic questions (embed-only)

## Problem

The expected clause was often ranked **10–40**, not in the top-10, so a top-10/20 UI
(or an agent reading the top few) missed it. The bottleneck is **first-stage dense
retrieval**, not reranking — the MiniLM cross-encoder was measured to be a **no-op**
(identical hit@10/@20, slightly worse @1/@3), so improving top-k means improving what
the bi-encoder retrieves.

Root cause of the misses: a **vocabulary gap**. A user asks *"do we need a DPO?"*; the
clause says *"A controller shall designate a data protection officer where…"*. The
Titan embedding of the answer prose doesn't sit close to the question phrasing.

## Approach

For every clause, an LLM generates the **natural-language questions a real user would
ask that the clause answers**, and we **prepend those questions to the clause's
embedding text only**. The vector now lives near how people actually search, without
changing what is displayed, snippeted, reranked, or fed to the agent (all of those keep
using `answer_text()`).

- **Generation** — `scripts/gen_synthetic_questions.py` (Bedrock Haiku, EU, in-account).
  Product-aware prompt (Data Privacy vs Shareholding Disclosure). 6–8 questions per
  clause across a deliberate mix of registers (keyword / natural / scenario / statutory),
  constrained to be answerable *from that clause* (no invented thresholds or dates).
  Cache: `<ACI_SYNTH_Q_DIR>/<jurisdiction>.json = {clause_key: {questions[], keywords[]}}`.
- **Consumption** — `embeddings/section_index._load_synth_questions`, gated by
  `ACI_SYNTH_Q=1`. Off by default → embed text is byte-for-byte the current behaviour.
  The questions are prepended to each embed window; keys/titles/levels/display unchanged.

## Validated results (retrieval-only, hit@k by clause key)

Isolated A/B per region: **same legacy extraction** for both arms, the only difference
is the embed text. Dense = Titan v2.

| Region (n) | floor hit@10 | **+synthetic Qs** hit@10 | Δ@10 | Δ@20 |
|---|---|---|---|---|
| United Kingdom — DP (115) | 83 | **89** | +6 | +3 |
| Germany — DP (91) | 86 | **99** | +13 | +4 |
| South Korea — DP (84) | 76 | **81** | +5 | −2* |
| Shareholding Disclosure — Australia (83) | 92 | **95** | +3 | +4 |

Lift in **every region, both products** (avg ≈ **+7 @10**); @20 is at/near ceiling.
\*Korea @20 wobbles ±2 (small n); @40 unchanged — the recall ceiling.

**Cost:** the augmentation trades a little rank-1/3 precision for top-10 recall (e.g.
Germany −7 @1 but +13 @10). The right fix for that is a **competent reranker over the
enriched top-10/20**, not a lexical signal (see below).

## What we tried and dropped

- **BM25 / lexical hybrid (dense + BM25, RRF).** Helped UK/Korea but **regressed
  Germany (99→89) and Shareholding-Australia (95→87)** — adding a lexical signal
  reorders good dense hits *out* of the top-10 in well-structured regions. Asymmetric
  downside; **not shipped.** The generated `keywords` (kept in the cache for the record)
  showed no consistent benefit either.
- **MiniLM cross-encoder rerank.** A no-op on this corpus (web-passage training doesn't
  transfer to legal clauses). The precision lever is a real reranker — the **Bedrock EU
  LLM-reranker** (in-region; prefer over US services like Cohere/Voyage for data
  residency) — applied on top of the synthetic-question recall. Tracked separately.

## Production rollout

1. **Generate** for all built regions (one-time batch; re-run on content changes):
   ```
   ACI_SYNTH_Q_DIR=data/products/_synthq \
   ACI_BEDROCK_PROFILE=dev1 AWS_PROFILE=dev1 AWS_REGION=eu-west-1 \
     python scripts/gen_synthetic_questions.py --all
   ```
2. **Re-embed** with the augmentation on (clear stale artifacts first — a bundle re-uses
   a cached `sections.npz` when the model matches, so force a fresh embed):
   ```
   ACI_SYNTH_Q=1 ACI_SYNTH_Q_DIR=data/products/_synthq \
   ACI_EMBED_BACKEND=titan ... aci bundle "<region>" --force   # then: aci reindex
   ```
3. **Reload** the external backend (OpenSearch) from the new `multi.npz`
   (`scripts/load_vectors.py`).

Ship the `_synthq` cache dir with the data volume so `ACI_SYNTH_Q=1` finds it at
build time. The flag has **no runtime cost** — it only affects embedding at build.

## Config

| var | default | effect |
|---|---|---|
| `ACI_SYNTH_Q` | off | `=1` prepends synthetic questions to clause embed text |
| `ACI_SYNTH_Q_DIR` | `data/products/_synthq` | where per-jurisdiction question caches live |
| `ACI_SYNTH_MODEL` | `eu.anthropic.claude-haiku-4-5-…` | generation model (Bedrock EU) |
| `ACI_SYNTH_WORKERS` | 12 | generation concurrency |
