# Combined validator — detection & scoring rules

The single rule set for every extraction scorecard. It combines codebase-a's
point-costing model (source-cited defects cost points) with the detectors from
raj-aosphere-core-index (row splits, unconfirmed continuations, empty answer segments,
readable evidence, extractor annotations). Implementation: `scripts/check_scorecard.py`
(scoring), `scripts/check_source_fidelity.py`, `scripts/hybrid_extract.py` (stitcher),
`scripts/check_stage4.py` and the other `check_*.py` modules (detection). Every number
below is pinned by a test; a change to a rule must change this file and its test together.

Thresholds are provisional — reasonable defaults, not calibrated against a
human-labelled gold set. Read a score as "where to look first".

## 1. Detection is not scoring

The validator is built to **detect as much as it can** and to **charge only what it
should**. These are separate steps:

1. **Detection** — every detector runs on every document and every finding it produces
   is listed in `scorecard.findings` (and on Page Review), whatever happens next.
2. **Scoring** — a finding reduces a score only when **all** of these hold:
   - it is a scorable kind with a cost rule in section 4 (advisory kinds never cost);
   - it is **not dismissed** by a reviewer;
   - it is **not a duplicate** of a finding already charged (section 5);
   - its evidence is not marked `confidence: "advisory"`.

A false positive is handled by dismissal, deduplication or reclassification — never by
switching a useful detector off.

## 2. Gate

| Verdict | Rule |
|---|---|
| **PASS** | worst critical dimension `>= 90` |
| **REVIEW** | worst critical dimension `>= 70` and `< 90` (i.e. 70–89.9) |
| **FAIL** | worst critical dimension `< 70` |

- The gate is the **worst** critical dimension, never the average.
- **Critical:** completeness, placement, fidelity, sectioning (sectioning only for
  products whose expected node count is known — `product_rules.sectioning_gates`).
- **Non-gating** (scored and shown, never setting the verdict): uniqueness, integrity,
  ai_postprocess (`ADVISORY_DIMENSIONS`, listed in `advisory_low` when weak) and toc.
- **Short documents** (`< 10` pages, `SHORT_DOC_MAX_PAGES`) gate on completeness alone.
- **Ordinary source-fidelity findings never override the numeric gate.** They cost
  points through section 4 like any other finding, and are listed in `gate_reasons`.
- **Exception — source-fidelity could not run.** If the source-fidelity audit errors or
  crashes (`source_fidelity.error`), a PASS or unknown gate becomes **REVIEW**: the
  audit is incomplete, so a clean score means "not checked", not "checked and fine".

## 3. Dimensions

| Dimension | Question | Gating |
|---|---|---|
| completeness | Is all the source content present? | critical |
| placement | Did content land under the right heading? | critical |
| fidelity | Were tables extracted with the right structure? | critical |
| sectioning | Did published sections keep their own bodies? | critical (product-scoped) |
| uniqueness | Is anything duplicated / not in the source? | non-gating |
| integrity | Are numbers, cross-references and footnotes intact? | non-gating |
| ai_postprocess | Did AI post-processing keep the document safe? | non-gating |
| toc | Was the outline the tree was built from trustworthy? | non-gating |

All scores are clamped to 0–100.

## 4. Defect types and their cost

"Points" are subtracted directly from the 0–100 dimension score. "Cap" is the most one
defect kind can cost that dimension.

### Completeness

| Defect | Detector | Cost | Cap |
|---|---|---|---|
| Word coverage below 100% | word_coverage | `2 × (100 − coverage%)` | — |
| Page with text missing from the tree | page_coverage | `100 × missing/pages_with_text × 1.0` | — |
| Section with a silent gap | content_localized | `100 × files/files_scanned × 1.0` | — |
| Section with a flagged (visible) gap | content_localized | `100 × files/files_scanned × 0.25` | — |
| Unreadable page, silent / flagged | engine_agreement | `100 × pages/n_pages × 1.0` / `× 0.25` | — |
| Negation flip (meaning inverted) | semantic_integrity | 15 each | — |
| Empty answer segment | find_empty_answer_segments | 5 each | — |
| Missing cell answer (source-cited) | source_fidelity | 1 each | 10 |

Silent loss always costs 4× flagged loss (`SILENT_WEIGHT 1.0` vs `FLAGGED_WEIGHT 0.25`),
so hiding a failure never scores better than marking it. Section-gap terms are skipped
for short documents and unchunked (≤ 2 file) trees, where per-section ratios are
meaningless.

### Placement

| Defect | Cost | Cap |
|---|---|---|
| Table outside its section's page range | `100 × flagged/tables` | — |
| Heading nested under the wrong parent/depth | `100 × flagged/headings` | — |
| Section boundary leak (source-cited) | 1 each | 10 |

### Fidelity

| Defect | Cost | Cap |
|---|---|---|
| Table geometric match quality (extraction gate only) | per-table credit: confident / verified continuation / reclassified-as-prose 1.0; unverified continuation, absorbed, no-bbox 0.75; uncertain 0.6; failed 0.25 → `100 × Σcredit / tables` | — |
| Table extracted but lost before the output | forfeits that table's credit | — |
| Table-shape defect (source-cited): `inconsistent_table_columns`, `cell_split`, `row_merge`, `column_merge`, `block_merge`, `row_alignment`, `table_as_text` | 1 each | 10 |
| Row split across a page break (confirmed) | 1.0 × severity | 10 |
| Possible row split (unconfirmed continuation) | 1.0 × severity | (shared with above) |
| **All structural terms above combined** | shape + row-split points | **15** |

**Row-split formula** (`ROW_SPLIT_*` in check_scorecard.py):

```
severity  = 1.0                                        confirmed  (kept_as_separate_row)
          = 0.5 × clamp(confidence / 100, 0.2, 1.0)    unconfirmed (unflagged_continuation;
                                                          0.25 when confidence is absent)
row_split_points = min(10, 1.0 × Σ severity)            after dedup + dismissal
structure_points = min(15, shape_points + row_split_points)
fidelity = 100 × Σ table_credit / tables − structure_points
```

The cost is proportional to the number of broken rows and how certain each one is. It
does **not** depend on how many tables the document has (the earlier 0.15 table-credit
rule cost ~5 points in a 3-table document and ~0.4 in a 35-table one for the same row).
Resolved continuations (`realigned_and_merged`, `widened_and_merged`) are listed as
advisory `row_continuation_merged` findings and cost nothing.

Post-AI (stage 4/5) the geometric credit is stale, so every table starts at full credit;
lost tables, shape defects and row splits still count.

### Sectioning

`100 − 100 × sections_lost/sections − 100 × outline_entries_lost/outline_entries`,
capped at 40 for a structureless blob. The outline term is skipped for the raw-MinerU
fallback tier, whose single synthetic heading would otherwise zero the score.

### Uniqueness (advisory)

| Defect | Cost | Cap |
|---|---|---|
| Words in the tree not in the PDF | `5 × 100 × extra/tree_words` | — |
| Duplicated content (source-cited) | 1 each | 10 |

### Integrity (advisory)

Number transposition: 15 each. Orphan footnote reference: 3 each, at most 10 counted.

### ai_postprocess (advisory)

Scored by `check_stage4.compute_stage4` against the contract the stage-4 report declares
(`stage4_report.json` → `mode`):

- **`batched`** — character-exact: any critical flag (content added/removed, row drift,
  invented markup, missing file) scores **0**; otherwise
  `100 − 60 × rejection_rate − 10 × reprojection_rate`.
- **`section`** (the default) — may add text read from page images and restructure,
  recorded by the producer as "reported, not enforced". The exactness flags are still
  **detected and listed** as warnings (`contract: "batched"`); the score is
  `100 − 12 × cells_deleted − 5 × cells_changed_role` (`integrity_score`, the same number
  as the Stage 4 tab). Cells merely split across chunks cost nothing. Whether added text
  is really in the PDF is answered by the post-AI completeness/uniqueness/source-fidelity
  checks.
- No stage 4 ran → absent (None), never 0.

## 5. Deduplication

One physical defect is charged once:

- **Same row, many reports:** row splits are keyed by `(table_id, page, block)`; repeats
  and the two stitcher heuristics on one row collapse to the most severe reading.
- **Same finding key:** source-cited findings sharing a content-derived `key` are charged
  once.
- **Symptom explained by a cause:** a table-level `inconsistent_table_columns` finding in
  the same table (file + page range) as a charged row split is the split's own symptom.
  It is not charged, and is marked `charged: false, absorbed_by: "row_split"`. A ragged
  table with no row split inside it is charged normally. If a split's table cannot be
  mapped to a file, it absorbs only when exactly one ragged finding covers its page.

## 6. Dismissed findings

A reviewer can dismiss any finding. A dismissed finding **stays listed**
(`dismissed: true`, restorable) and **costs nothing** in every dimension — including row
splits and empty answer segments, whose dismissal key is the same one the UI stores
(`_stitch_key`). Mixed active and dismissed findings: only the active ones are charged.
Dismissals are stored per source-PDF hash, so they survive re-extraction.

## 7. Detectors that report but never cost

`table_without_source_grid`, `extraction_annotation` (extractor badge left in a chunk),
`row_continuation_merged`, `orphan_recovered`, `orphan_duplicate`, ai_postprocess
exactness flags in section mode, and anything with `evidence.confidence: "advisory"`.
They exist so a reviewer sees them; they are advisory by design.

Readable evidence: `gap` / `found_elsewhere` titles are rewritten as verbatim quotes
from the cited PDF page when the token run can be relocated; otherwise the token join is
kept — never a fabricated quote.

## 8. Running it

- Re-score a finished job without re-extracting: `scripts/reverify_corpus.py --job
  "<product>/<job>"` (`--dry-run` first). Summary-AI jobs (a `04_*` tree and no `03_*`)
  are skipped there and must be scored with `scripts/backfill_post_ai_scorecard.py`.
- Post-AI scorecards: `scripts/backfill_post_ai_scorecard.py out/corpus --force`.
- Stitcher detectors (row splits, unconfirmed continuations) run during **stage 2
  extraction** and are read from `stage2_report.json`. A document extracted before a
  stitcher change keeps its old anomalies until stage 2 is re-run; re-scoring alone
  cannot add them.
- Never compare scorecards produced by different code versions.

## 9. Adding a rule

1. The detector emits a finding with `key`, `kind`, `severity`, `pages`, and
   `evidence.confidence: "advisory"` if it is not confirmed.
2. Map it to exactly one dimension; reuse a cost class from section 4 before adding one.
3. Decide what it duplicates (section 5) and make that explicit.
4. Tests: detected; charged the stated amount; dismissed → free; duplicate → charged once.
5. Update this file.
