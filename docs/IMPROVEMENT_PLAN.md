# Project review: weaknesses & improvement plan

**Date:** 2026-07-02. Scope: full review of src/, scripts/, docs/, evals, Docker/CI.

## Where the project stands

Strong core: 93% citation hit@20 on 299 scorable eval cases, 104/105 on the approved
benchmark with 0 ungrounded answers, clean read-only S3 guard, honest decision docs
(RETRIEVAL_COMPARISON.md), Trivy-scanned deploy, 237 jurisdiction indexes built across
two products. The weaknesses below are mostly about hardening and scaling, not the
core retrieval design.

---

## Weaknesses

### A. Retrieval & eval quality

1. **hit@1 is 36%, MRR 0.51.** Users see the right clause somewhere in the top 20, but
   rarely first. The reranker exists but top-rank quality hasn't been tuned/evaluated.
2. **Colloquial jurisdiction phrasing fails.** Most of the 22 misses share one pattern:
   "Seoul", "Britain", "England", "PICP" (typo) don't map to South Korea / UK. This is a
   query-normalization gap, not an embedding gap — and it's cheap to fix.
3. **Two-hop questions: 60% hit@20** (n=6). Known class; small sample.
4. **138 behavioural cases (negatives, out-of-scope, redirect, self-referencing) have no
   automated eval.** They need an agent-level harness; today they're parsed and skipped.
5. **Shareholding Disclosure is indexed but unmeasured** — SD eval cases exist
   (`test/aosphere adv search - SD - eval cases v1.xlsx`) but eval_report.md contains
   only Data Privacy rows.
6. **Benchmark circularity** (acknowledged in RETRIEVAL_COMPARISON.md): gold answers are
   graph-qa's own output. Fine for regression, weak for absolute quality claims.

### B. Pipeline reliability & scaling (to ~130 jurisdictions × N products)

1. **Idempotency is promised but not delivered.** INDEX_PIPELINE.md promises rebuild on
   "content hash / source ETag"; `cli.py` only checks `_is_built()` (files exist). A
   changed source DOCX is silently skipped; the only remedy is `--force` (full re-embed).
   The manifest (`cli.py:360`) lacks per-jurisdiction hashes, timestamps, and counts.
2. **Silent failures throughout ingestion.** Malformed alert JSON returns `None → []`
   (`ingest/alerts.py:30-34`); alerts missing id/date are dropped without logging; docx
   extraction has no validation that expected Parts (A–K) or section counts came out.
   A degraded extraction is indistinguishable from a good one.
3. **Heading detection is heuristic and tuned on one product.** Median-length +
   style-name heuristics (`extract/styles.py:45-99`) were validated on Data Privacy;
   new products/authors will hit edge cases with no warning (ties into B2).
4. **Global thresholds.** Alert-mapping constants (`MIN_SIM=0.55`, `TOPK=3`,
   `semantic_mapper.py:18-20`) are untuned per product/jurisdiction and unvalidated
   against ground truth.
5. **Products are hardcoded.** Adding a product means editing `config.py` prefixes and
   `regions/region_map.py` lists. The `" — "` product-qualified region string is fragile
   (em-dash collisions, typo → silent index fragmentation). `config/products.yaml`
   exists but isn't the single source of truth.

### C. Service & security

1. **JWT passed as a query parameter** for SSE (`/api/agent/stream?access_token=...`,
   app.py:172) — lands in proxy/access logs and browser history. Exchange it for a
   short-lived one-time stream token instead.
2. **In-memory session store** (`llm/agent.py:81 _SESSIONS`): unbounded growth (no TTL),
   and silently loses conversation context under multiple uvicorn workers. Bound + TTL
   now; Redis if/when multi-worker.
3. **No request limits or rate limiting.** `q` and `jurisdictions` are unbounded; each
   query is expensive (embed + rerank). Add `max_length`/count caps and proxy-level
   rate limits.
4. **Broad `except Exception` blocks** (app.py:55,155,186; registry.py:315,326) swallow
   failures; one returns exception type/message to the client. Log fully, return generic.
5. Smaller: no CORS policy, invalid jurisdiction → 500 not 404, `/healthz` doesn't check
   JWKS/S3/LLM reachability, ~761-line `web.py` embeds the whole frontend with a
   hand-rolled regex markdown renderer.

### D. Engineering hygiene

1. **Zero automated tests** (~5k LOC in src, 0 test functions) and **no CI quality gate**
   — workflows only tag, deploy, and Trivy-scan. Nothing stops a regression from shipping.
2. **Split dependency truth.** `pyproject.toml` is missing fastapi, fastembed, numpy,
   litellm, mlflow, openai-agents… (they live only in requirements.txt). `pip install .`
   yields a broken package; requirements.txt and uv.lock can drift.
3. **README is empty** (one line). INDEX_PIPELINE.md is good but describes a partly
   unbuilt future; no doc says "what exists today and how to run it".
4. Repo hygiene: `scratch_dockerbuild*.log` and `.DS_Store` committed/present, `test/`
   directory untracked, `scripts/eval_cases.py` has uncommitted changes, eval report
   leaks `np.str_(...)` into output.
5. Bench/eval scripts contain machine-specific hardcoded paths — not reproducible by
   anyone else.

---

## Improvement plan

### Phase 1 — safety & correctness (do before wider rollout, ~1 week)

1. Fix token-in-URL for the SSE endpoint (one-time stream token exchange).
2. Bound `_SESSIONS` (TTL + max entries); document single-worker constraint.
3. Add input caps (`q` max_length, jurisdictions count) and a rate limit at the proxy.
4. Replace silent excepts: log full error server-side, generic message client-side;
   return 404 for unknown jurisdictions.
5. Make the build loud: per-jurisdiction build report (sections extracted, parts seen,
   alerts parsed/dropped) with warnings when counts deviate; fail-fast option.

### Phase 2 — retrieval quality wins (highest user-visible ROI, ~1–2 weeks)

1. **Jurisdiction alias map** (Seoul→South Korea, Britain/England/GB→UK, city/regulator
   names, common typos like PICP). Directly targets ~half of the 22 misses; trivially
   evaluated by re-running `eval_cases.py`.
2. **Tune for hit@1**: rerank-depth/boost experiments scored on hit@1/MRR, not just
   hit@20. Target: hit@1 36% → 55%+.
3. **Run the SD eval suite** and add SD rows to the report — you're flying blind on
   half the index.
4. **Behavioural eval harness** for the 138 agent cases (LLM-judged: refused when it
   should, redirected when it should). This becomes the regression suite for the agent.
5. Revisit later (per RETRIEVAL_COMPARISON.md): curated guidance/summaries as
   separately-retrievable nodes — only if scattered-content Q&A becomes a priority.

### Phase 3 — pipeline hardening for 130-jurisdiction scale (~2 weeks)

1. **Real idempotency**: store source content hash / S3 ETag per jurisdiction in the
   manifest; rebuild only on change (delivers what INDEX_PIPELINE.md promises).
2. Complete the manifest: per-jurisdiction hash, counts, timestamps, index version.
3. Extraction validation gates: expected-Parts check, min section count, style-coverage
   stats per document; quarantine + report failures instead of crashing or passing
   silently.
4. Product config as data: drive prefixes/products from `config/products.yaml`; validate
   product names; kill the em-dash split fragility.
5. Threshold validation: sweep alert-mapping thresholds against a small labelled sample
   per product before trusting them across 130 jurisdictions.

### Phase 4 — engineering foundation (parallel, ongoing)

1. **Tests where they pay most**: extraction (golden docx → expected sections), region
   qualify/split, alias normalization, auth, search API smoke test. Even ~30 targeted
   tests changes the risk profile.
2. **CI gate**: ruff + pytest on PR, required before the deploy workflow.
3. Unify deps: pyproject as single source; generate requirements.txt/lock from it.
4. Write the README (what it is, how to build an index, how to run the service, where
   evals live). Clean scratch logs, commit or gitignore `test/`, fix `np.str_` leak.
5. De-hardcode bench/eval script paths (CLI args or env).

### Sequencing rationale

Phase 1 removes deploy-blockers cheaply. Phase 2 is where users feel improvement —
alias mapping alone likely moves hit@20 from 93% toward 96–97%, and hit@1 work changes
the perceived quality most. Phases 3–4 are what make the promised 130-jurisdiction
build operable by someone other than the author.
