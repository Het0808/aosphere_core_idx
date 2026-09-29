# Geometry-based table matching & placement (hybrid_extract pipeline)

**Date:** 2026-07-28. **Status:** Rule B (geometric table matching) implemented and
tested. Rule A (section placement) has a page-range invariant check implemented as a
post-hoc validator; the full single-pass geometric scanner rewrite described below is
**not yet built**.

## Why this exists

The hybrid extraction pipeline (`scripts/hybrid_extract.py`, `scripts/pdf2mdtree.py`)
was misplacing and losing tables: a table that visually belongs under section 2.1 was
rendering under section 1, and a sibling table under 2.2 disappeared with no failure
marker. Root cause: two independent subsystems each decided "order" from a fragile
signal — TOC-title *text* matching for section boundaries (Rule A), and top-to-bottom
*rank* for pairing pdf2mdtree's detected tables to MinerU's extracted table blocks
(Rule B) — reconciled only by comparing list/array positions after the fact, with no
cross-check against a ground truth both sides already had: page number and geometric
position.

**Core principle:** every element (heading, paragraph, table placeholder, MinerU
block) is anchored to `(page_number, x0, y0, x1, y1)`. Geometry decides order and
placement. Text matching is only ever used to *label* something — never to decide
where it goes.

## Verified coordinate facts (don't re-derive these — they're measured, not assumed)

- **pdf2mdtree bboxes** (`scripts/pdf2mdtree.py`'s `detect_tables()`, stored in
  `tables_manifest.json`) are PyMuPDF/fitz bboxes: **top-left origin, y increasing
  downward, units = PDF points.**
- **MinerU's `content_list.json` bboxes are NOT pixel or point space** — they're
  normalized to **0–1000 independently per axis**: `x_norm = x * 1000 / page_width`,
  `y_norm = y * 1000 / page_height` (source:
  `.venv/lib/python3.12/site-packages/mineru/backend/vlm/vlm_middle_json_mkcontent.py`,
  `make_blocks_to_content_list`, ~line 524). Confirmed against `..._middle.json`'s
  `page_size` field and a real table: MinerU bbox `[92,91,905,722]` on a 595×842pt
  page ↔ `92/1000*595=54.7`, `905/1000*595=538.5` — matches pdf2mdtree's bbox for the
  same table (`[56,80,538,615]`) within a few points.
- **Same origin/axis direction on both sides — no Y-flip needed.** The commonly
  assumed "PDF is bottom-left, vision models are top-left" mismatch does not apply
  here because PyMuPDF already normalizes to top-left. The only conversion needed is
  the per-axis 0–1000 rescale, done in `_mineru_bbox_to_pt()`
  (`scripts/hybrid_extract.py`).
- Page dimensions for the rescale come straight from the **original source PDF**
  (`fitz.open(pdf_path)`), not the cropped `combined_tables.pdf` — `build_combined_pdf`
  inserts pages unmodified via `insert_pdf`, so dimensions are identical either way,
  but the original is the simpler/more obviously-correct source.

## Rule B — geometric table matching (IMPLEMENTED)

`scripts/hybrid_extract.py`, `run_stage2()`:

- `_iou(a, b)` — standard intersection-over-union of two `[x0,y0,x1,y1]` boxes in the
  same space.
- For every page with ≥1 pdf2mdtree table that has a real bbox, compute IoU against
  every MinerU table block on that page (`geo_match` precompute, built per-page so a
  block claimed by more than one placeholder can be detected as a likely merge).
- **Three-tier outcome**, stored on each table's `status.json` / `stage2_report.json`
  as `match_method`, `match_status`, `match_iou`, `match_note`, `mineru_bboxes`:
  - `match_status: "confident"` — best IoU > `CONFIDENT_IOU` (0.5). If more than one
    block clears `CONSIDER_IOU` (0.15) for the same placeholder, they're all kept and
    concatenated (MinerU split the table) — the existing `stitch_table_html()`
    dedup-header/merge-wrapped-row logic still runs on top; geometry decides *which*
    blocks belong together, not *how* to merge their rows.
  - `match_status: "uncertain"` — either a low-but-nonzero best IoU, or a block that's
    also the best match for a *different* placeholder on the same page (possible
    MinerU merge of two visually-distinct tables into one block). Best-effort content
    is still used, but the badge/marker surfaces the warning — never silently
    accepted.
  - `match_status: "missing"` — zero overlapping MinerU block at all. Falls through to
    the pipeline's existing failure-marker path (`[TABLE EXTRACTION FAILED]` —
    substring depended on by `check_content_localized.py`, `lib_content_compare.py`,
    and the UI's tagging, so it's deliberately unchanged).
- Tables with **no real bbox** (multi-page landscape comparison tables — pdf2mdtree's
  own `reconstruct_multipage_tables` — or the unanchored snapshot fallback) fall back
  to the **old rank-based pairing** (`blocks_for_page_rank`), tagged
  `match_method: "rank_fallback"`. Out of scope for this pass — see Rule A/multi-page
  notes below.

**Tested against a real document** (`out/hybrid_extractions/_ui/*/source.pdf`, 57
pages, 11 detected tables, 2 tables sharing one page): all single-page tables matched
at IoU 0.92–0.98; two tables sharing page 37 were correctly told apart by geometry
(previously the risk case for the old rank-based pairing). Screenshot-verified — the
drawn pdf2mdtree bbox (blue) and matched MinerU bbox (green, dashed) tightly overlap
each table independently.

## MinerU's cross-page table merge (fixed)

**Symptom reported:** a page-spanning table extracted perfectly as one table, then the
*following* pages showed `[TABLE EXTRACTION FAILED]`.

**Cause.** When a table spans pages, MinerU merges it into ONE logical block: the full HTML
is attached to a single page-region (its *anchor*) and **empty table stubs** are emitted for
that table's other page-regions. Matching was strictly per-page, producing two wrong
outcomes at once — the placeholder sitting over a stub reported "empty table_body" as a
FAILURE, while the block actually holding the rows was claimed by nobody and its content was
dropped from the output entirely. On the Argentina document the whole Exchanges/Crypto
restriction table was silently missing (all its distinctive phrases: 0 hits in the tree)
behind a failure marker pointing at the wrong page.

**Fix — three parts, all in `run_stage2`:**

1. **`adopt_orphan()`** — a placeholder whose matched blocks are ALL empty may adopt an
   unclaimed content-bearing block on an adjacent page (MinerU's merge anchor). Only fires
   where the pipeline would otherwise have failed outright, so it cannot change a table that
   already extracts correctly, and it refuses to guess when more than one candidate is in
   reach.
2. **`is_continuation()`** — matched only empty stubs, with a page-spanning table's rows on
   an adjacent page. Emits a calm `[TABLE CONTINUATION]` note (via `continuation_marker`)
   instead of a failure marker: the rows are already in the output, nothing is missing, and
   the page snapshot is retained so the merge can be confirmed by eye. Scored at
   `CREDIT_CONTINUATION = 0.75`, not as a failure.
3. **`orphan_blocks` reported** in `stage2_report.json` — any content-bearing MinerU block
   still unclaimed is a successfully-extracted table whose rows never reach the output.
   Previously invisible; now surfaced so this class of loss can never be silent again.

**Orphan accounting must cover BOTH matching paths.** A first version counted only
`geo_match` blocks as claimed, so blocks consumed by rank-fallback tables (multi-page tables
have no bbox and never appear in `geo_match`) looked orphaned — which over-reported loss AND
would have adopted blocks another placeholder was already going to emit, duplicating content.
`_rank_claimed` fixes this.

**Regression caught while fixing this:** the false-positive prose path was emitting footnote
*definitions* as a side effect. Once a table extracts successfully that path stops running,
so `[^53]`/`[^54]` disappeared. `recover_footnotes()` now claims `page_footnote` blocks on a
table's pages explicitly (they can never be table content, and the table's own `[^n]` refs
are orphaned without them). Net result is better than baseline: footnotes 52–57 all present,
where 55–57 were missing even before.

**Measured effect**

| | Argentina (57pg) | US (259pg) |
|---|---|---|
| tables filled | 6 → **7** | 32 → 32 |
| tables failed | 2 → **0** | 57 → **37** |
| continuations correctly identified | 2 | 20 |
| orphaned (silently dropped) blocks | **0** | **0** |
| footnote definitions present | 3 → **6** | — |

## Rule A — section placement

**Implemented now:** a cheap post-hoc invariant check, not the full rewrite.

- `scripts/pdf2mdtree.py` now also writes `headings_manifest.json` next to
  `tables_manifest.json` — the same `matched` heading list (title, level, page) the
  TOC-matching pass already computes, just persisted to disk instead of only printed.
- `scripts/check_table_placement.py` (new) reads both manifests, derives each
  section's `[start_page, end_page)` from consecutive headings in document order, and
  flags any table whose actual page falls outside its assigned section's range (±1
  page tolerance for ordinary TOC drift). Wired into `hybrid_extract_ui.py`'s
  `_run_validation()` alongside the existing word-coverage/content-localized/numeric
  checks, so it shows up in the Validation tab automatically.
- This catches the *symptom* (table ends up under the wrong heading) using data
  already on disk. It does **not** fix the *cause* — Rule A's ordering is still
  decided by TOC-title text search, not geometry.

**Not yet implemented — the full redesign, for when the invariant check above starts
surfacing real cases to fix:**

- Single-pass scanner: fold heading-anchoring into the same top-to-bottom per-page
  walk that already emits paragraphs and table placeholders, instead of a separate
  global text search run afterward. Eliminates the "two clocks" problem where heading
  position and content position can independently drift.
- Heading anchor priority: TOC destination Y-coordinate if the PDF provides one →
  else restrict text search to *only* the TOC's claimed page (never a global forward
  search).
- Sections as spatial intervals (`page`, `y0`), not just page ranges — resolves same-
  page multi-heading boundaries the coarser page-range invariant above cannot (a table
  on the same page as both 2.1 and 2.2 would pass today's check either way).
- Orphaned content: a heading that fails to anchor confidently should produce an
  `[UNRESOLVED SEGMENT]`, not silently fold into the previous section.
- Column-aware reading order — **only if this corpus turns out to have multi-column
  layout**; not yet confirmed either way. Don't build it speculatively.

## Open items / caveats carried forward

1. **IoU thresholds (0.5 / 0.15) are untuned** — reasonable starting points, not
   validated against a labeled set of real split/merge cases in this corpus.
2. **Merge detection has no row-level geometry to act on** — MinerU's `content_list.json`
   gives one bbox per whole table block, not per row, so a genuine 1-block-covers-2-
   placeholders merge can only be flagged `uncertain`, not automatically split at the
   right row. Would need MinerU's richer per-row output (if available) to do better.
3. **The no-TOC heuristic path** (`pdf2mdtree.py`'s font-size-tier fallback, used when
   a PDF has no bookmarks) has no independent page range to invariant-check against —
   out of scope for both the implemented check and the planned rewrite.
4. **Dashboard**: `scripts/hybrid_extract_ui.py`'s Inspector tab now shows a
   "Geometry" section per table (bbox-annotated page image via the new
   `/api/jobs/{id}/page_bbox_image/{page}` endpoint, plus a match-status/IoU badge),
   and the Validation tab surfaces `[PLACEMENT]` flags from `check_table_placement.py`.
   `/api/jobs/{id}/geometry.json` exposes the raw per-table geometry for scripting/
   debugging outside the UI.

---

# Validation scorecard (Phase 1 — implemented)

**Status:** shipped. `scripts/check_scorecard.py` + the Scorecard tab in the UI.

## The reframe: conservation, not correctness

"Is this extraction correct?" cannot be answered without a human-labelled answer key.
"**Was anything lost, moved, or duplicated?**" can be answered from the source PDF alone,
because content is *conserved* through the pipeline: every word, number and footnote on a
PDF page must appear somewhere in the tree, exactly once, under a section whose page range
contains that page. Every metric below is a conservation check, which is why none of them
need ground truth.

## The circularity problem — and the cheap fix (implemented)

**The flaw.** Tests 1–3 read the source PDF with fitz (PyMuPDF) — the engine `pdf2mdtree` is
itself built on. They therefore compare *fitz's reading of the PDF* against a tree derived
from *fitz's reading of the PDF*. Wherever fitz has a blind spot, both sides are missing the
same content, coverage looks perfect, and the dashboard reports green on a page that is
empty in the output. **Same-engine comparison structurally cannot see this.** Of the three
verdict-setting dimensions, Completeness is fully circular; Fidelity is not (it compares
pdf2mdtree geometry against MinerU, a vision model on rendered pixels) and Placement is
largely not (PDF bookmark metadata + page numbers).

There is a second-order version in the table path: **fitz decides which pages MinerU ever
sees** (`pages_union` only contains pdf2mdtree-flagged pages), so Fidelity only
cross-validates tables fitz already found. A table fitz misses entirely is invisible to
everything. Still open — see Phase 2/3.

**The fix — `scripts/check_engine_agreement.py`, runs on every job (~4s for 286 pages,
CPU-only, no GPU).** Two signals, neither derived from fitz:

1. **Engine disagreement** — per-page text volume vs **pypdf**, an independent pure-Python
   parser (not a MuPDF wrapper). If pypdf recovers ≥`DISAGREE_RATIO` (1.35×) more text on a
   page, fitz has a blind spot and every fitz-derived number for that page is unreliable.
   Deliberately one-directional: fitz seeing *more* than pypdf is normal and not reported.
2. **Unreadable pages** — fitz text ≈ empty while the page demonstrably carries content
   (images / vector drawings) or pypdf finds text there. Such a page previously scored
   **perfect**: nothing expected, nothing found.

Unreadable pages are a *definite* loss (not merely unjudgeable), so they colour their page
`silent` — or `flagged` if a snapshot at least shows the page — and cost Completeness
`100 × (unreadable ÷ pages)` at silent weight.

**Measured effect.** On a synthetic 8-page document with pages 3 and 6 replaced by
image-only renders (simulated scans): word coverage still reported **96.9%**, but
Completeness fell from **70.8 → 45.8** and the verdict from REVIEW to **FAIL**, with both
pages named as findings. On the real corpus nothing false-fires: fitz and pypdf agree within
0.1% on every page of both pilot documents (163,563 vs 163,360 chars), so these PDFs have a
clean text layer and the fitz reading is genuinely trustworthy — a property of the documents,
not a guarantee of the design, which is the point of checking.

**Still not wired in:** `check_three_way_crosscheck.py` (fitz + pypdf + MinerU) is the full
answer to circularity but remains opt-in and does **not** feed the scorecard. Wiring its
high-confidence findings into Completeness is the remaining step.

## Ground-truth ladder (ranked by independence from the thing being tested)

| Tier | Source | Note |
|---|---|---|
| 1 (weak) | fitz text extraction | What Tests 1–3 use — but pdf2mdtree **is built on fitz**, so a shared blind spot passes both sides silently |
| 2 | pypdf / poppler | Independent code, same approach (parse internal text objects) |
| 3 (strong) | MinerU vision model | Reads rendered pixels — catches what text-object parsing structurally cannot |
| 4 (orthogonal) | PDF's own TOC/outline, page count | The document declaring its own structure, not an extraction |
| 5 (self-referential) | Internal consistency: footnote ref ↔ definition, "see B.1.3" ↔ does B.1.3 exist | The document as its own answer key |
| 6 (gold) | Human spot-check on a sample | The only real truth — used to *calibrate* tiers 1–5, not run per job |

## Three non-negotiable scoring decisions

1. **The gate is the WORST dimension, never the average.** For compliance documents an
   average is a vanity metric: a 99%-complete extraction that dropped one risk-disclosure
   table is a failed extraction, and averaging lets four green dimensions hide it.
2. **Silent loss is penalised ~4× harder than flagged loss** (`SILENT_WEIGHT=1.0` vs
   `FLAGGED_WEIGHT=0.25`). The pipeline's whole philosophy is that an unextractable table is
   *marked* in the output, never dropped quietly. If the score punished a visible
   `[TABLE EXTRACTION FAILED]` marker as hard as a silent disappearance, the rational move
   would become hiding failures — so flagged losses keep partial credit for being honest.
3. **Only CRITICAL dimensions set the verdict** (`CRITICAL_DIMENSIONS` = completeness,
   placement, fidelity). The question this dashboard exists to answer is "is a whole
   paragraph or a whole table missing / in the wrong place?" — that is what makes an
   extraction unusable. Uniqueness and Integrity are `ADVISORY`: scored and displayed, tagged
   as such in the UI, but they never move the verdict. Rationale, learned empirically: on the
   pilot documents Integrity was driven almost entirely by expected noise — footnote markers,
   `%` values that recur, and page-1 document-control identifiers like `0010023-0028263 UKO2:
   2009055849.1` that pdf2mdtree deliberately never carries into the tree. Letting that set a
   REVIEW verdict trains everyone to ignore the verdict.

### Reporting numbers honestly (two fixes worth not regressing)

- **Never report the normalised key.** Matching strips thousands separators so `25,750` ==
  `25750`, but `25750` appears in *neither* document — a reader sees an apparently fabricated
  number they cannot grep for. `check_numeric_integrity` keeps the first **surface form** seen
  and reports that.
- **"Absent" ≠ "fewer occurrences".** `25,750` was reported missing while plainly present:
  the PDF has 7 occurrences, the tree 4. That is a count reduction (3 instances lost inside an
  already-flagged failed table), not a disappearance. The check now emits `absent`,
  `tree_count` and `pdf_count` per finding; only `absent` numbers carry real weight or paint a
  heat-map page red.

## The five dimensions

| Dimension | Question | Fed by |
|---|---|---|
| Completeness | Is all source content present? | word coverage (Test 1) + per-section dropped spans (Test 2) |
| Placement | Did each table land under the right heading? | `check_table_placement.py` page-range invariant |
| Fidelity | Were tables extracted with right content/structure? | Stage 2 per-table outcome + geometric IoU match status |
| Uniqueness | Is anything duplicated? | words in tree but not in PDF (Test 1 "extra") |
| Integrity | Are numbers/thresholds/footnote refs intact? | Test 3 transpositions + orphan footnote refs |

Every dimension carries its own `what` / `from` / `advice` / `caveat` helper text in
`check_scorecard.HELP`, rendered directly on each card — kept beside the scoring logic so
explanation and formula can never drift apart.

## Page heat-map: three states, not two

`unvalidatable` is deliberately a **separate state from failure**. An image-only or
diagram-heavy page scores ~0% on every text-conservation check while being perfectly fine;
painting those red would make the whole map cry wolf and destroy trust in it. States:
`ok` / `flagged` (loss the output declares) / `silent` (loss nothing announces) /
`unvalidatable` (visual-only, needs a human or a vision pass).

**Attribution rule:** a finding is attributed to ONE page (the first of its section's
range), not smeared across the range — smearing is what made an earlier iteration flag
nearly every page. And a missing number on a page that already has a *flagged* failure is
treated as explained by that failure (amber), not as a separate silent loss (red) —
otherwise one problem is double-reported and honestly-flagged pages look like data loss.

## Deliberately NOT built (and why)

- **Levenshtein / embedding-based fuzzy diffing** for ensemble comparison — O(n·m) blowup on
  a 259-page document, and embeddings add a model dependency plus non-determinism to a tool
  whose value is being fast and reproducible. Token-multiset comparison over the existing
  `clean_markdown()` normalisation is sufficient.
- **Terminal-punctuation + "does it follow grammatically" reading-order heuristic** — this
  corpus is full of legitimately non-terminal blocks (`(i) domiciled in Argentina;`, bare
  labels like `Media`), so it would misfire constantly, and the grammar half needs an LLM.
  Superseded by the geometric approach below.
- **Rebuilding header/footer filtering** — already exists and already applied:
  `detect_boilerplate_patterns()` in `lib_content_compare.py` does frequency (≥80% of pages)
  + spatial zone (top/bottom N lines) + digit-run normalisation. Reuse it; don't rewrite it.

---

# Meaning, hierarchy, and testing the tests

## Semantic integrity — the hole every other check was blind to

**Everything else verifies content is PRESENT. Nothing verified it still SAYS the same
thing.** Demonstrated on a real extraction: changing *"a managed fund that is **not**
structured as a legal entity"* to *"…that is structured as a legal entity"* — one word, the
obligation inverted — produced **no finding anywhere**, and word coverage still reported
98.5% PASSED.

Why: the per-section gap check only reports runs of ≥4 consecutive missing words, so a single
dropped `not` is a run of one and is discarded as noise. Correct for ordinary stopwords,
catastrophic for negations.

**`scripts/check_semantic_integrity.py`.** Two approaches were tried; the first failed and
the reason is worth keeping:

- ❌ **Counting tracked words per section.** Confounded twice over — sections sharing a page
  range always look short (siblings hold the text), and a clean extraction *already* loses
  ~2 negations (ones inside a failed table go with it), so no threshold separates signal from
  baseline.
- ✅ **Testing each word in context.** Take 5 words either side of a tracked word. If that
  context appears in the tree **without** the word between the halves, the sentence survived
  and its meaning flipped. If neither form appears, the whole passage is gone — content loss,
  already reported elsewhere, not a meaning change.

That distinction is the whole trick: it isolates *meaning changed* from *content missing*.
Verified both directions — silent on a clean extraction, and on the injected flip it reports
the page plus the PDF and tree wordings side by side.

## Heading hierarchy — is the shape of the tree right?

`check_table_placement.py` asks whether a table landed under the right heading. Nothing asked
whether the **heading tree itself** is right: 2.1 filed under section 1 instead of section 2
passes every content and placement check, because all the text is present and each table sits
under its own heading.

**`scripts/check_heading_hierarchy.py`** compares the folder tree against the PDF outline's
own levels: depth, parentage (is each heading's parent the nearest preceding heading one level
up?), and completeness. Getting it quiet took three fixes, each a real matching hazard:

| symptom | cause | fix |
|---|---|---|
| 49 false positives | `"4. Aggregation"` vs the zero-padded dir `04-aggregation` | strip leading zeros |
| 3 false positives | pdf2mdtree adds an ordering prefix absent from the title (`"Appendix 1 …"` → `06-appendix-1-…`) | strip leading digits from **both** sides |
| 9 false positives | `"Sources of Law"` repeats under every part, so several nodes share one key | prefer the candidate whose parent matches; consume nodes so repeats map 1:1 |

Result: **78/78 headings clean** on the pilot document, and a heading moved to the wrong
parent is caught. Folded into the **Placement** dimension — a wrongly-nested heading is the
same defect class as a misplaced table.

## Fault injection — who tests the tests?

`scripts/fault_injection.py` takes a known-good extraction, breaks it one specific way, and
asserts the right check notices. No human labelling needed, which makes it the cheapest way
to stop the scores being purely a matter of opinion. Every fault is a **historical
regression** — once a gap is closed the suite enforces it forever.

**It found four problems on its first run**, which is the point of it:

- two were bugs in the harness itself: `tables_manifest.json` stores an **absolute** path to
  the source PDF and every check resolves through it, so a fault that edits the PDF was being
  evaluated against the pristine original (`_repoint_manifest` fixes it); and `move_heading`
  was moving files into `_assets/`, then into a synthetic `00-overview` node that matches no
  outline heading — so the check was right to stay silent.
- two are **genuine gaps**, now recorded as `known_gap` rather than quietly dropped:

| fault | why it is missed |
|---|---|
| `delete_table` | sibling appendix sections hold near-identical tables, so deleted rows are found next door and classed *relocated*. Needs a direct check — every table marked `ok` in `stage2_report` must have its rows in the final tree, no text comparison involved. |
| `move_table` | content is still in the tree, just under the wrong heading; a presence check cannot see it, and `check_table_placement` compares only manifest page ranges, not where the rendered table landed. This is the content-level placement-purity work. |

A `known_gap` does not fail the run — a permanently red suite stops being read. Closing one
means deleting its reason, after which it is enforced.

Current state: **4/4 enforced faults caught, 2 known gaps open.**

## Corpus baseline — did a change help *overall*?

Every comparison before this was one document at a time, by hand, which cannot answer the
question that matters: a fix that improves one document and breaks fifty looks like a success.

`scripts/run_baseline.py` runs a folder of PDFs, stores every scorecard, and diffs the tracked
numbers (5 dimension scores, gate, silent pages, failed tables, negation flips, hierarchy
errors, unreadable pages) against the stored baseline, labelling each change better/worse by
direction.

```
python scripts/run_baseline.py "<folder>" --save          # record a baseline
python scripts/run_baseline.py "<folder>"                 # diff against it
python scripts/run_baseline.py "<folder>" --rescore-only  # checks only, no MinerU
```

`--rescore-only` is the one to use when iterating on a *check* rather than the pipeline:
extraction is unchanged, so re-running MinerU proves nothing and costs minutes per document.
Exits non-zero on any regression, so it can gate a change.

## Remaining phases

- **Phase 2** — content-level *placement purity* (cross-tabulate each word's PDF page against
  its file's declared page range — catches misplacement the page-range invariant can't see,
  including same-page heading boundaries); real repeated-block duplication detection;
  dangling cross-reference check ("see B.1.3" → does it exist).
- **Phase 3** — reading-order fidelity via **geometric** sequence comparison: count inversions
  between the emitted word order and fitz's own (page, x, y) reading order. Deterministic, no
  linguistics. Must run prose-only, excluding known table bboxes, since table cells
  legitimately reflow row-major. Then a **historical-regression fault library**: every fixed
  bug (starting with the 2.1/2.2 `yr`-clamping bug) codified as a synthetic fault the
  dashboard must keep catching forever.
- **Phase 4** — ensemble diff across two MinerU backends (the selector now exists in the UI)
  on normalised tokens, as an uncertainty map.

## Calibration status — read this before trusting a number

`GATE_PASS=90` / `GATE_REVIEW=70` and every per-dimension weight are **provisional defaults,
not calibrated against a human-labelled gold set** (`"calibrated": false` is returned in the
payload and stated on the dashboard). Treat a score as "where to look first", not as a
measurement. Two ways to earn the numbers, cheapest first: (1) **fault injection** — corrupt a
known-good extraction and confirm the dashboard catches it, which validates the *checks*
without any labelling; (2) **gold set** — one-time human verification of every table and
section on 2–3 documents, which is what converts a score into a claim.

## Where the code lives

| Piece | File |
|---|---|
| IoU matching, bbox conversion, geo_match | `scripts/hybrid_extract.py` (`_iou`, `_mineru_bbox_to_pt`, `run_stage2`) |
| Badge/marker surfacing of match status | `scripts/hybrid_extract.py` (`mineru_badge`, `failure_marker`) |
| Multi-page table `yr` clamping (2.1/2.2 placement fix) | `scripts/pdf2mdtree.py` (`reconstruct_multipage_tables`) |
| Headings manifest (ground truth for Rule A) | `scripts/pdf2mdtree.py` (written alongside `tables_manifest.json`) |
| Machine-readable Stage 1 report | `scripts/pdf2mdtree.py` (`stage1_report.json` — page count, snapshotted pages, heading match rate) |
| Placement invariant check | `scripts/check_table_placement.py` |
| Scorecard: dimensions, gate, page states, helper text | `scripts/check_scorecard.py` |
| Dashboard: bbox overlay + scorecard endpoints | `scripts/hybrid_extract_ui.py` (`page_bbox_image`, `job_geometry`, `job_scorecard`) |
| Dashboard: Scorecard/Inspector/Validation UI | `scripts/hybrid_extract_ui.py` (`JOB_HTML` — `loadScorecard`, `renderDimension`, `geoBadge`) |
| MinerU backend/effort selector | `scripts/hybrid_extract_ui.py` (`MINERU_BACKENDS`, `upload`, `INDEX_HTML`) |
