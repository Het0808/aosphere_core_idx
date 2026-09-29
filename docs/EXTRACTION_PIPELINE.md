# Claude-from-PDF extraction → ingestion pipeline (staged, S3-decoupled)

**Date:** 2026-07-21. **Status:** design; S2 (ingest) validated, S1 (extract-at-scale) to build.

A production pipeline to turn source PDFs into a searchable index using **Claude-driven
extraction**, decoupled into two stages that hand off via **S3 artifacts** so each stage is
tested independently. Extends [`PRODUCTIONIZATION_PLAN.md`](PRODUCTIONIZATION_PLAN.md) and the
extraction-backend seam (`extract/extract_document(path, *, backend=…)`).

## Why (validated evidence)

A Claude-from-PDF extraction of the UK + Germany Data-Privacy opinions, once ingested
correctly, **matched-to-beat the legacy docx index** on retrieval (retrieval-only, hit@k
family-matched):

| region | legacy FULL @10 | claude FULL @10 | @5 | notes |
|---|---|---|---|---|
| UK | 86 | **91** | 83 vs 71 | claude beats @3/@5/@10/@20/MRR, ties @1 |
| DE | 89 | **93** | 80 vs 74 | same shape |

So the **ingestion half (S2) is de-risked**; the new risk is **automating the extraction (S1)
reliably, in-region, at cost.** Focus build effort on S1.

## Architecture

```
 source PDF (S3)
      │   ┌──────────────────── STAGE 1: EXTRACT ────────────────────┐
      └──▶│ SKILL.md (master prompt) → Claude via Bedrock (EU region) │
          │  runtime: Converse fan-out (default) | AgentCore (only if │
          │  the extraction is genuinely agentic/multi-step)          │
          │  → hierarchical markdown tree → zip                        │
          └───────────────────────────┬───────────────────────────────┘
                                       ▼  GATE: coverage + faithfulness (Parts A–K present,
                                       │        text grounded in source, keys match template)
                            md-tree.zip (S3)  ◀── human-inspectable QA checkpoint
          ┌──────────────────── STAGE 2: INGEST ─────────────────────┐
          │ parse md tree → keyed content.json (N.M + letter/roman    │
          │   sub-clauses + section/overview nodes; window oversized) │
          │ + attach guidance (RAG_rated_answers) + alerts            │
          │ → Titan v2 embed → OpenSearch (aci-vectors)               │
          └───────────────────────────┬───────────────────────────────┘
                                       ▼  GATE: eval_cases hit@k (must not regress)
                                 vector DB (live)
```

## Stage 1 — Extract (the new work)

- **In:** source PDF from S3. **Out:** per-doc markdown tree, zipped, to S3.
- **Driver:** the extraction skill's `SKILL.md` as the **master prompt**, calling **Claude on
  Bedrock in an EU region** (residency — see below). A "skill" is a curated prompt + optional
  scripts; that pattern ports to Bedrock even though the native Skills *feature* is an
  Anthropic-runtime thing.
- **Runtime choice:**
  - **Default — Converse fan-out:** a Fargate/Lambda job per document calling the Bedrock
    `Converse` API in `eu-west-1`. Leaner/cheaper for a stateless transform, embarrassingly
    parallel, pay-per-call. **Recommended starting point.** (Bedrock **batch inference** is
    −50% but supports **no tool use / function calling** — use it only if S1 is a pure
    single prompt→completion with no tools; otherwise real-time Converse.)
  - **AgentCore — only if agentic:** if extraction genuinely needs multi-step tool use
    (page-by-page traversal, code-interpreter splitting, self-check), host a Claude-Agent-SDK
    agent (which understands `SKILL.md`) on **Bedrock AgentCore Runtime** — **GA in
    `eu-west-1` + `eu-central-1`, deployed as a container**. Heavier (pay per concurrent
    session-minute, not per call); use only if the transform proves it needs it.
- **Why md-tree (not JSON):** human-inspectable → you can QA S1 by eye before S2 runs. Keep it,
  but the S2 parser must be robust (below).
- **GATE:** extraction coverage (expected Parts A–K, section counts), **faithfulness** (no
  invented/dropped legal content — grounded in the PDF), and **key-template validation** against
  the product's canonical clause scheme. Quarantine+report on failure; never ship silently.

## Stage 2 — Ingest (validated)

- **In:** md-tree.zip (S3) + guidance (`RAG_rated_answers`) + alerts (S3). **Out:** vectors in
  OpenSearch.
- **Parser:** md tree → keyed `content.json`. Promote the prototype
  (`scripts`-side converter) into `extract/extract_claude.py` producing an `ExtractedDoc`, WITH
  the key-derivation fixes proven this session:
  - key = `<Part>` + `N.M` (from parent dir/filename) + optional `(letter|roman)` sub-clause;
    also capture section-level (`NN-name`) and `00-overview` parent nodes.
  - **window** oversized bodies into `#c` chunks (the build path already windows; the earlier
    truncation-to-2500-chars was the bug that faked a "finer hurt recall" result).
- **Attach:** guidance rows (`answers_by_clause`, family-remapped to the nearest existing key)
  + alert rows (`alerts_by_id`).
- **Embed + load:** Titan v2 → OpenSearch (`aci-vectors`), via the normal `aci build`/load path.
- **GATE:** `scripts/eval_cases.py` hit@k as the acceptance test — no regression vs the current
  index. (This is the gate we used to prove the UK/DE result.)

## Cross-cutting (non-negotiable)

- **Data residency (EU-only) — SETTLED:** every runtime that *sees clause text* runs in an EU
  region. Claude + Titan models are in `eu-west-1` ✅; **AgentCore Runtime is GA in `eu-west-1`
  + `eu-central-1`** (agent execution + session state stay in-region) ✅; Converse-fan-out on
  Fargate/Lambda in `eu-west-1` ✅. Enforce EU containment with **EU-geo-scoped cross-region
  inference profiles (never the *global* profile)** + IAM, and optionally **zero-data-retention**
  (`data_retention_mode: none`) so prompts/completions are never persisted. Neither runtime
  option breaches residency.
- **Idempotency + cost:** content-hash each source PDF in a manifest; **re-extract only changed
  docs**. Use **batch inference** and parallel fan-out over the ~235×2 corpus. Estimate S1 LLM
  cost before a full run (full-PDF prompts are large).
- **Testability:** the S3 artifacts (PDF → md.zip → content.json → vectors) let each stage be
  asserted in isolation — exactly the goal.

## Open items

1. ~~AgentCore EU availability + residency~~ **RESOLVED**: AgentCore GA in `eu-west-1` +
   `eu-central-1` (Oct 2025) with in-region execution + EU residency enforceable. **S1 runtime
   decision: default to Converse-fan-out (Fargate/Lambda) — cheapest/simplest for a stateless
   transform; adopt AgentCore only if extraction becomes genuinely agentic** (it can host the
   native Claude-Agent-SDK + SKILL.md agent as a container if so).
2. **Prototype `extract_claude.py`** — promote the scratchpad converter to a real
   `ExtractedDoc`-producing backend + wire `backend="claude"` into `extract_document`.
3. **Pin the S1 prompt/output contract** (Bedrock-EU) + the template/faithfulness validator.
4. **Scale cost estimate** for S1 across the full corpus.
5. **@1 / rerank:** the eval was rerank-off; confirm rank-1 with the reranker on.

## References
- Eval detail + gotchas: memory `claude-pdf-extraction-eval` (windowing + roman/section keys).
- Prototype converter: `scratchpad/build_claude_uk_content.py` (to be promoted).
- Backend seam: `src/aosphere_core_index/extract/__init__.py` (`extract_document`).
- Retrieval gate: `scripts/eval_cases.py`. Embed/window: `embeddings/section_index.py`.

## Hard gate: a bookmark outline of 1–2 entries

`mineru_fallback.USELESS_OUTLINE_MAX_ENTRIES = 2`

A document whose PDF bookmark outline has **one or two entries** is escalated to the
fallback chain regardless of how it scores. This is a **hard gate — chosen, not
measured** — and it is recorded here because it is the one rule in the pipeline that
is an assertion about publishing rather than a threshold read off a distribution.

**Why.** An outline that small is never a description of a document. Every instance in
this corpus is a publisher bookmarking a single anchor:

| document | outline | result before the gate |
|---|---|---|
| `104_Shareholding/curcao` | `Appendix`, `Discretionary Managed Holdings` — both p37 of 40 | first 36 pages never read |
| `172_G20/Brazil__182791` | `bmkFrontPage`, `bmkPrimaryFrontPage` | **0 content files** from a 1552-word PDF |
| `172_G20/China__181467` | same anchor names | near-empty tree |
| `172_G20/Canada (Ontario)__176861` | same | 773 words of 3335 |

Stage 1 trusts the outline and starts reading at its first entry, so everything before
that entry is never visited.

**Why zero entries is excluded.** No outline at all means Stage 1 never used one — it
fell back to the font-size heuristic, which works. Measured over 101 documents: 40 have
no outline, median completeness **90.2**, against **94.3** for documents with a proper
one. An earlier version of this gate scored TOC *presence* (`proper 100 / lacking 55 /
improper 35`) and failed 39 correctly-extracted documents on it, sending each through a
MinerU re-parse that could not help. Those numbers were reverse-engineered from the
gate threshold rather than measured, which is precisely the mistake this note exists to
prevent repeating.

**Known cost.** If a real document ever ships a legitimate 2-entry outline, this gate
escalates it needlessly. Accepted, because the failure it prevents is total content
loss.
