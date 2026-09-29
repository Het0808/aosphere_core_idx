# Retrieval comparison: core-index vs graph-qa — and the chunk-level question

**Status:** decision doc + experiment plan. **Date:** 2026-06-30.

## Why this exists
On the 105-question approved benchmark, core-index scores **104/105 equivalent-or-better**
with **0 ungrounded answers** (after fixing judge/checker bugs). The single genuine
shortfall — *"summarise exemptions to privacy law Colorado"* (`SYSTEM_WORSE`) — is a
**scattered-content** problem: the CPA exemptions (HIPAA/COPPA/GLBA/publicly-available)
are spread across generic intro clauses (A1.1, A2.1) with no explicit cross-reference
edge from the scope clause (A2.2) the agent reads. core-index's section-level retrieval
dilutes those scattered sentences, so they never surface.

graph-qa (the system that produced the approved gold answers) handles this class of
question by design. This doc compares the two and decides whether core-index should
adopt graph-qa's mechanism.

## How graph-qa resolves scattered content
graph-qa is a Postgres-backed **knowledge graph** (25 node types, 6 edge types) with
**multi-strategy backtracing retrieval**:
1. **Chunk-level vector search across all content types** — embeds sub-section *chunks*,
   footnotes, AI section-summaries, and Q&A pairs; searches them all. A scattered
   exemption sentence is found wherever it lives.
2. **Graph backtracing** — hops edges from each match to gather related content:
   `is_subset_of` (chunk→section), `refers_to` (footnote / "see Section X" cross-refs),
   `is_summarised_by` (summary→section), `is_answered_by`+`cites` (question→approved
   answer→cited sections).
3. **Approved answers & summaries are first-class, retrievable nodes** with `cites` edges
   back to sources — and the benchmark gold *is* these `approved_ai_answer` nodes.
4. **Threshold relaxation** widens automatically when results are sparse; the LLM then
   composes from everything gathered, with citations.

## The three architectural gaps in core-index

| Capability | graph-qa | core-index |
|---|---|---|
| Retrieval granularity | sub-section **chunks** + footnotes + summaries + Q&A | **one vector per section** — scattered sentences diluted |
| Traversal during retrieval | native multi-strategy backtrace over real edges | read-time *candidate* edges only (cites_out + "Part X" prose) |
| Curated answers / summaries | retrievable **nodes** with `cites` edges | folded into the section vector (Stage 3); not separately retrievable |

The Colorado miss maps exactly to gap #1.

## Honest caveat
The benchmark gold **is graph-qa's own approved answers**, so "graph-qa resolves it" is
partly graph-qa grading graph-qa. The comparison is informative about *mechanism*, not a
like-for-like quality contest.

## Decision
- **core-index's strengths stand**: fast cross-jurisdiction semantic search, the document
  viewer, relevance highlighting, the lean offline-deployable index. These are not in
  question.
- **The open question is scattered-content recall.** The highest-leverage transferable
  idea is **chunk-level retrieval** (embed sub-section units, not whole sections). It
  targets the *entire class* of scattered-content misses, not just Colorado — but it is a
  genuine architectural shift toward what graph-qa already is.
- **Before committing to it, run a bounded experiment** (below). If chunk-level retrieval
  measurably lifts recall of the gold-cited clauses for Colorado (and a sample) without
  regressing the suite, it's worth a wider rollout. If not, **defer scattered-content Q&A
  to graph-qa** and keep core-index focused on what it does best.

## Experiment (Colorado, reversible)
Baseline committed at `c181a52` so the experiment can be discarded cleanly.
1. Build **chunk-level** embeddings for Colorado (sub-section chunks instead of one vector
   per section).
2. Use `scripts/eval_retrieval.py` (recall@k / hit@k / MRR against gold clause citations)
   to compare **section-level vs chunk-level** retrieval on Colorado's questions.
3. Decision rule: adopt chunk-level only if it clearly improves recall of the gold-cited
   exemption clauses without hurting the broader retrieval metrics.

## Experiment result (2026-06-30, `scripts/exp_chunk_colorado.py`)
Section-level (59 vectors) vs chunk-level (142 ~100-word chunks), Colorado, relevance =
exemption-bearing clauses {A1.1, A2.1, G}:

| Query | section finds | chunk finds |
|---|---|---|
| summarise exemptions colorado | A2.1 | **A2.1, G** |
| what data/entities are exempt | *none* | **G** |
| exemptions HIPAA/GLBA/publicly-available | A2.1 | **A2.1, G** |

Chunk-level **consistently retrieves more** exemption clauses (never worse). **But neither
surfaces A1.1** — the clause with the HIPAA/COPPA/GLBA text — because that content is
*generic federal-framework framing*, not Colorado-CPA-specific, so it doesn't rank under
any granularity. That's a data/authoring trait, not a retrieval-granularity one. And the
extra clause chunk-level surfaces (`G`, generic breach exemptions) carries the
over-attribution risk seen on Ohio.

**Conclusion:** chunk-level is a modest, real improvement but does **not** close the
Colorado gap, so it does not meet the bar to re-architect core-index. Note that graph-qa
resolves Colorado primarily by retrieving its own `approved_ai_answer` node — i.e. the
curated answer is itself retrievable — not by chunk granularity alone.

## Recommendation (decided)
1. **Do not adopt chunk-level retrieval** solely for this class — the win is marginal and
   doesn't close the gap.
2. **Defer scattered multi-clause Q&A to graph-qa**, the mature system purpose-built for
   it (chunk-level + backtracing + approved-answer nodes).
3. core-index's closest lever, if we ever want to improve this in-house, is to treat
   **curated guidance/summaries as separately-retrievable nodes** (graph-qa's
   approved-answer mechanism), not chunk granularity. Stage 3 folded guidance into section
   vectors; promoting it to its own retrievable unit is the higher-leverage follow-up —
   but only if scattered-content Q&A becomes a priority for core-index.
4. Keep core-index focused on its strengths: fast cross-jurisdiction semantic search, the
   document viewer, relevance highlighting, lean offline deployment. It is at 104/105
   equivalent-or-better with strong faithfulness; the one gap is this scattered-content
   class, best served by graph-qa.
