# Productionising Core Index — phased plan

**Date:** 2026-07-20. **Scope:** the path from "works in dev / alpha" to "operable in
production at 235 regions × 2 products". This **extends** [`IMPROVEMENT_PLAN.md`](IMPROVEMENT_PLAN.md)
(2026-07-02): it records what has been done since, folds in the retrieval / model-cost /
infra work from the July alpha cycle, and re-sequences the remaining work by production risk.

---

## 1. Where we stand

Strong core, as of the 2026-07-02 review and the work since:

- **Retrieval**: ~93% citation hit@20 on 299 scorable Data-Privacy eval cases; grounded,
  citation-enforced agent (tools-only: `search_clauses` / `read_clauses`, every answer cites
  `[Jurisdiction · ClauseKey]`, scope locked at the tool layer — no prompt-injection surface).
- **Auth**: Keycloak JWT (RS256), stateless HMAC stream tokens, email allow-list.
- **Deploy**: Trivy-hardened Docker image, models baked in, deploy via the Skandiam
  `gh-action-library` (ECS/Fargate; ALB + WAF managed externally).
- **Index**: 235 regions across two products; three vector backends behind one seam
  (`ACI_VECTOR_BACKEND` = memory | opensearch | atlas), OpenSearch validated at 100% parity
  with the in-memory index.

The weaknesses below are about **hardening, quality-precision, and scale** — not the core
retrieval design.

## 2. Guiding principle — this is a legal product

The top production risk is **a fluent, confident, wrong answer**, not latency or uptime.
So the ordering is: **correctness → provenance → regression safety → cost/abuse → scale → ops**.
A retrieval regression or an ungrounded answer must be *impossible to ship silently*.

**Data-residency guardrail (cross-cutting).** All inference stays in-region (Bedrock EU,
in-account). Any generation/rerank model must be servable in `eu-west-1`. Verified this cycle:
gpt-oss-120b/20b and Mixtral 8x7B are in `eu-west-1`; **Mistral Small, Llama 3.1-8B and
Llama 3.3-70B are not in any EU region**; external US APIs (OpenAI direct / Cohere / Voyage)
are out of bounds for clause text.

## 3. Done since the 2026-07-02 review (do not redo)

- SSE **token-in-URL → stateless HMAC stream token** exchange (`service/auth.py`).
- `_SESSIONS` **bounded** (TTL + max entries) (`llm/agent.py`).
- **Input caps** on `q` / `jurisdictions` / `products` (`service/app.py`).
- Reranker moved **MiniLM → Bedrock Haiku** (`ACI_RERANK_BACKEND=claude-bedrock`) — the
  local cross-encoder was a no-op on this corpus. *(Still untuned for hit@1 — see Phase 2.)*
- **OpenSearch** vector backend built and validated at 100% parity vs in-memory.
- **Synthetic-question embed augmentation** validated: +~7 hit@10 across UK / Germany /
  South Korea (DP) + Australia (SD). Off by default (`ACI_SYNTH_Q`).
- **Serving-RAM fix (this change):** under an external vector backend the in-memory vector
  matrix is no longer loaded (metadata-only). Verified **649 MB → 245 MB RSS (~400 MB
  reclaimed)**; memory-backend behaviour byte-identical; admin reindex/divergence still get
  the full matrix via `registry.multi_with_matrix()`.
- **Model-cost screen (this cycle):** 5 EU-in-region candidates A/B'd against Haiku for
  synthetic-Q generation. **Only Haiku holds the +7 lift** (Mixtral +5, gpt-oss-120b +1,
  gpt-oss-20b +2, Mistral-7B +1, Nova-Lite +0). Decision: **keep Haiku; take the cost win
  from batch inference (~$57 → ~$28 full corpus), not a model swap.**

## 4. Phased plan

### Phase 1 — Ship-blockers: safety, cost, correctness net  (~1 week)
*Goal: nothing can silently regress, leak, or run away on cost.*

1. **CI quality gate.** Run `scripts/eval_cases.py` on PR; **fail if hit@20 < threshold**
   (e.g. 90%). Add ~20–30 targeted unit tests (golden-docx extraction, region qualify/split,
   alias normalization, auth, `/api/search` smoke). Required before the deploy workflow.
   *(Plan D1/D2 — today CI runs pytest but there are 0 tests; a broken `main` can be tagged
   and shipped.)*
2. **Rate limiting + LLM budget.** Proxy-level rate limit; per-run max input/output tokens
   and turn cap already exists (`_MAX_TURNS`) — add a **per-user/org Bedrock spend ceiling**.
   *(Plan C3 — search embeds+reranks and the agent runs up to 28 turns with no cost bound.)*
3. **Loud ingestion/extraction validation.** Per-jurisdiction build report (sections
   extracted, Parts A–K present, alerts parsed/dropped); **warn/quarantine on deviation**
   instead of silently indexing a degraded doc. *(Plan A/B2 — `cli.py`, `ingest/alerts.py`,
   `extract/styles.py`.)*
4. **Error hygiene.** Global exception handler (generic client message, full server-side
   log); return **404** (not 500) for unknown jurisdictions. *(Plan C4/C5 — `service/app.py`.)*
5. **Prod auth hardening.** Fail-fast at startup if Keycloak is unset in a prod environment;
   require a shared `ACI_STREAM_SECRET` on multi-pod. *(`service/auth.py`.)*

**Exit:** a red PR cannot ship; agent cost is bounded; a bad extraction is visible, not silent.

### Phase 2 — Retrieval & answer quality: highest user-visible ROI  (~1–2 weeks)

1. **Jurisdiction alias map** (Seoul → South Korea, Britain/England/GB → UK, regulator/city
   names, common typos). Targets ~half of the 22 known misses; evaluated by re-running
   `eval_cases.py`. *(Plan A2 — likely +3–4 pts hit@20.)*
2. **Tune hit@1 (36% → 55%+).** Rerank depth/boost experiments on the Bedrock reranker,
   scored on hit@1 / MRR (not just hit@20). *(Plan A1 — the reranker is wired but never tuned.)*
3. **Measure Shareholding Disclosure.** Run the SD eval suite and add SD rows to the report —
   we are currently flying blind on half the index. *(Plan A5.)*
4. **Roll out synthetic questions** (validated +~7 hit@10). **Generation model = Haiku**
   (screen-confirmed; no cheaper in-region model holds the lift), **on batch inference** for
   ~$28 full corpus. Regenerate all regions → re-embed → load OpenSearch. Gate: `ACI_SYNTH_Q`.
   Automate generation for new regions (Phase 3 idempotency).
5. **Faithfulness + behavioural eval.** LLM-judged grounding (is the answer supported by the
   cited clause?) plus the **138 behavioural cases** (refuse / out-of-scope / redirect /
   negative) as the agent's regression suite. *(Plan A4 — the single most important gate for a
   legal product; the agent architecture is already grounded, but there is no eval proving it.)*

**Exit:** hit@1 materially up, SD measured, synth-Q in prod, and the agent has a faithfulness
gate that runs in CI.

### Phase 3 — Pipeline & index hardening for scale  (~2 weeks)

1. **Real idempotency + automated re-ingest.** Per-jurisdiction source content-hash / S3 ETag
   in the manifest; rebuild only changed regions; trigger on new drops. *(Plan B1/B2 — today
   `_is_built()` checks file existence only, so a changed DOCX is silently skipped.)*
2. **Product config as data.** Drive products/prefixes from `config/products.yaml`; validate
   product names; kill the fragile `" — "` em-dash split. *(Plan B5 — adding a product
   currently needs code edits.)*
3. **Atomic index cutover + divergence guard.** Build a new OpenSearch index and **alias-swap**
   (not delete-and-recreate, which blanks the index mid-rebuild); record the index version in
   the manifest and refuse to serve if the in-memory metadata and external store disagree.
   *(Serving-RAM gating already landed — see §3.)* `embeddings/vector_load.py`,
   `scripts/load_vectors.py`.
4. **Per-product mapping thresholds.** Sweep alert/guidance `MIN_SIM` / `TOPK` / `MARGIN`
   against a small labelled sample per product before trusting them across 235 regions.
   *(Plan B4 — tuned on Data Privacy only.)*
5. **MinerU vs legacy decision.** Finish the extraction A/B and pick the default per doc type;
   stop carrying two unmeasured extraction paths.

**Exit:** unattended incremental rebuilds, zero-downtime index swaps, a new product added via
config alone.

### Phase 4 — Engineering & ops foundation  (parallel / ongoing)

1. **Test suite depth** + **unify dependencies** (pyproject as single source; generate
   requirements/lock from it). *(Plan D2/D3 — `pip install .` is currently broken.)*
2. **README + runbook**: what it is, how to build an index, deploy (tag format), rollback,
   troubleshooting, expected latency/SLA. *(Plan D3.)*
3. **Observability metrics**: search / rerank / LLM latency, cache hits, and **citation
   accuracy**; extend `/healthz` to check Keycloak JWKS + vector-backend + Bedrock reachability
   (today it can report healthy while a dependency is down).
4. **Security headers** (CSP, X-Frame-Options, HSTS, X-Content-Type-Options); CORS review.
5. **Multi-pod**: Redis-backed sessions or sticky routing (stream tokens are already stateless,
   but agent conversation history is still in-process).

**Exit:** operable by someone other than the author; observable; horizontally scalable.

## 5. Sequencing rationale

Phase 1 removes deploy-blockers and the cost/abuse exposure cheaply. Phase 2 is where users
feel the improvement — alias mapping + hit@1 tuning + synth-Q rollout — and it installs the
faithfulness gate a legal product needs. Phase 3 makes the 235-region, multi-product build
operable **unattended**. Phase 4 is the durable foundation, done in parallel throughout.

## 6. Evidence / references

- 2026-07-02 gap review: [`IMPROVEMENT_PLAN.md`](IMPROVEMENT_PLAN.md) (Plan A–D items cited above).
- Synthetic questions design + A/B: [`RETRIEVAL_SYNTHETIC_QUESTIONS.md`](RETRIEVAL_SYNTHETIC_QUESTIONS.md).
- Index/build pipeline: [`INDEX_PIPELINE.md`](INDEX_PIPELINE.md).
- Serving-RAM gating: `embeddings/multi_index.py` (`load_multi(with_matrix=…)`),
  `service/registry.py` (`get_multi` / `multi_with_matrix`), `service/app.py` (warmup + admin).
