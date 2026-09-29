# Changes report — scripts/

Comparison: `scripts-prev.zip` (baseline snapshot) vs. the current `scripts/` folder.
Only 4 of the ~150 files in `scripts/` differ. Everything else is unchanged.

## Files changed

- `scripts/check_scorecard.py`
- `scripts/hybrid_extract.py`
- `scripts/build_inspect.py`
- `scripts/hybrid_extract_ui.py`

Attribution: most of this work was done in an earlier/different chat session, not this
one. Two specific edits were made in *this* session and are called out explicitly below;
everything else predates this session and is being documented here, not authored here.

---

## scripts/check_scorecard.py

**1. New finding: blank/dropped answer segments in multi-part table cells**
(`find_empty_answer_segments`, `EMPTY_ANSWER_SEGMENT_PENALTY = 5.0`) — prior session.

Some answer cells hold several `<br><br>`-separated segments, one per sub-question the
paired question cell enumerates. 4+ consecutive `<br>` tags means a segment that should
hold real content is blank. This was invisible to word-coverage checks because the
corpus pattern is a repeated short boilerplate line ("N/a. Please see 7.1(a) above.")
that still appears elsewhere in the *same* cell — so "is this text anywhere in the
tree" reads satisfied even though one copy is genuinely missing. A helper
`_locate_page` anchors the text immediately before the gap against a per-page token
index of the source PDF to find which page the gap is on. Adds a new
`empty_answer_segment` finding (dimension `completeness`, severity `silent`) and a fixed
5.0-point penalty per occurrence to the completeness score.

*This session's edit* (called out in the diff's own comments): a `"file_pages"`
fallback — when the page-anchor match fails (routine for table cells, since a cell's
PDF reading order routinely scrambles a contiguous token match), the finding now falls
back to the *file's own page range* instead of shipping an empty `pages: []`. Before
this fix the finding was invisible in the Page Review tab (any finding with no pages is
silently dropped there) despite still counting against the score — this is the "N/a.
Please see 7.1(a) above" / page 46 bug reported during this session.

**2. New finding/penalty: table rows split across a page break**
(`ROW_SPLIT_PENALTY = 0.15`, `ROW_SPLIT_PENALTY_UNCONFIRMED = 0.08`) — prior session.

Reads `stage2.stitch_anomalies` (from `hybrid_extract.stitch_table_html`, see below) and
classifies each anomaly by its `action`:
- `kept_as_separate_row` → confirmed split (stitcher couldn't safely rejoin a row after
  a page break). New finding kind `row_split`, penalty 0.15/occurrence.
- `unflagged_continuation` → weaker evidence (the new page's leading cell already has
  real text, so the usual blank-cell continuation check never caught it). New finding
  kind `row_split_unflagged`, half penalty (0.08), flagged as carrying real
  false-positive risk.
- anything else (`realigned_and_merged` / `widened_and_merged`) → already repaired by
  Stage 2; reported as advisory-only `row_continuation_merged`, no score penalty.

**3. Fidelity dimension no longer blanked out post-AI** — bug fix.

Previously `fidelity` was fully blanked (`score = None`) on the post-AI re-score because
its geometric-bbox table credit is a frozen Stage 2 snapshot, "stale and misleading"
once Stage 4 has rewritten the tree. Now: every table starts at full credit (1.0) on the
post-AI pass instead of re-deriving the stale bucket; a table lost before the final tree
forfeits that same 1.0; and the row-split penalties from #2 (a live Stage 2/3 defect,
unrelated to Stage 4) apply on top and now actually count. `fidelity` is no longer
excluded from the post-AI critical/gating set.

**4. Sectioning score no longer zeroed for the raw-MinerU fallback tier**
(`_score_sectioning` gains `unchunked` param) — bug fix.

The `RAW_MINERU_OUTPUT` fallback tier writes one synthetic placeholder "heading"
(the PDF filename) into the headings manifest. Treated as a real outline entry, it can
never "reach a heading in the tree," so `outline_lost/outline_total` always came out
1/1, zeroing sectioning for every document on this tier — concretely, Spain__138135
scored `worst_score 0.0` despite a cleanly divided 19-section tree. Fixed by skipping the
outline term when `unchunked` is true.

**5. Gate no longer auto-downgraded by source-fidelity findings** — behavioral change.

Previously, any non-advisory `source_fidelity` finding forced the gate down to
`review` even if the worst scored dimension was ≥90. That override is removed: the
gate is now purely the worst dimension score's own verdict. Source-fidelity findings
still populate `gate_reasons`/`source_review` for a reviewer's eye, but are informational
only — "a document scoring in the 90s reads PASS, full stop."

**6. Readable quotes for `gap`/`found_elsewhere` finding titles**
(`_readable_span`, `_attach_readable_titles`, `_cleaned_pages`, `_page_token_offsets`)
— new feature, cosmetic/display-only.

These findings used to title themselves with the raw normalized tokenizer output
(lowercased, punctuation stripped — e.g. "article 6 4"). A new pass re-tokenizes the
cited PDF page(s), locates the finding's token run, and substitutes the real, punctuated
substring as the title (joining across a page boundary where the span crosses pages).
Falls back silently to the old token-join title if the page can't be opened or the run
can't be relocated.

---

## scripts/hybrid_extract.py

Table row-continuation/split detection improvements (feeds directly into
check_scorecard.py's `row_split`/`row_split_unflagged` findings above) — prior session.

1. **Fixed a header-row false negative.** A repeated "Questions"/"Answers" header
   sitting at `ri == 0` on a continuation page masked the real continuation row
   (at `ri == 1`) from ever reaching the split-detection checks below it, which tested
   `ri == 0` to mean "the page's first row." Replaced with a `block_started` flag that
   tracks the block's actual first real row, whatever index it lands at. Confirmed
   against Liechtenstein__180334 table_011 (p88→89, p89→90).
2. **Added `row_text`** to the anomaly record (the row's own text, independent of which
   column the surplus sits in) — `surplus_text` is empty whenever the real content is at
   the *leading* edge, which previously left a `kept_as_separate_row` outcome with no
   text anywhere in the anomaly to identify the orphaned row by.
3. **New detection path: unflagged continuations.** When the new page's first row
   already has real text in its first cell (e.g. the next sub-question's own label)
   rather than a blank marker column, the existing blank-cell check never ran at all and
   the row was silently appended with no record. Now inferred from the previous row's
   last cell ending mid-sentence, guarded by `MIN_UNFLAGGED_TAIL_WORDS = 10`,
   `MIN_UNFLAGGED_NEW_ROW_WORDS = 4`, and a numbered-heading exclusion
   (`_HEADING_START_RE`) to avoid flagging short canned answers or a genuine new
   subsection. Logged as `TABLE_ROW_SPLIT_UNFLAGGED_CONTINUATION` — detection only,
   never auto-merged, since guessing which words belong to which row risks the exact
   corruption `_alignment_offset` exists to avoid.

---

## scripts/build_inspect.py

Explicit `encoding="utf-8"` added to every `read_text()` call (page-count lookups, table
`.md`/`.html` reads, scorecard/validation JSON reads) — prior session, robustness fix.
Without an explicit encoding, Python falls back to the platform's locale-preferred
encoding (cp1252 on Windows), which could raise `UnicodeDecodeError` or mojibake on
non-ASCII content even though everything is written as UTF-8. No behavioral change on a
system where the locale already happened to be UTF-8.

---

## scripts/hybrid_extract_ui.py

Note: a naive diff against the snapshot showed ~8,500 changed lines, but that was a
line-ending artifact (LF in the snapshot vs. CRLF in the current file), not real content
changes. Re-diffed after normalizing line endings — the genuine change set is ~140 lines
across 14 hunks.

**1. Explicit UTF-8 encoding**, same fix and same rationale as build_inspect.py above,
applied at roughly a dozen call sites (`_read_json`, corpus job/report/manifest reads,
`_progress.json`, table file reads, cached-scorecard reload, etc.) — prior session.

**2. `PR_KIND_LABEL` / `PR_KIND_CLASS` entries for ten previously-unlabeled finding
kinds** — **this session.** `empty_answer_segment` ("MISSING CONTENT", red), `row_split`
("ROW SPLIT", red), `row_split_unflagged` ("ROW SPLIT (UNCONFIRMED)", amber),
`row_continuation_merged` ("ROW MERGED", amber), `row_merge` ("ROW MERGE", red),
`table_without_source_grid` ("TABLE NO SOURCE GRID", red), `section_boundary_leak`
("SECTION BOUNDARY LEAK", red), `extraction_annotation` ("EXTRACTION ANNOTATION", amber),
`duplicated_content` ("DUPLICATED CONTENT", amber), `inconsistent_table_columns`
("INCONSISTENT COLUMNS", red). Without these, a finding of one of these kinds rendered
in Page Review with a blank/undefined label and no severity color. `empty_answer_segment`
is the display counterpart to the check_scorecard.py fix in this same session, above.

No other logic in this file changed between the snapshot and the current version.

---

---

# Changes report — src/

Comparison: `src-prev.zip` (baseline snapshot) vs. the current `src/` folder.
Only 1 file differs.

## Files changed

- `src/aosphere_core_index/service/doc_gallery.py`

## src/aosphere_core_index/service/doc_gallery.py

**Jurisdiction/product rollup row no longer stuck on Review by one low-severity
per-document finding** (`_verdict`) — prior session, bug fix.

A jurisdiction or product row in the Doc Gallery tree inherits `review`/`fail` from
*any one* of its documents, however far above the pass bar the row's own worst and mean
scores sit — so a single low-severity per-document finding (e.g. a `source_fidelity`
finding) could hold a whole jurisdiction at "review" even when both aggregate numbers
read well into the 90s. Fixed: once the row's own worst *and* mean scores both clear 90,
the row now shows `pass` regardless of that per-document review/fail count, "since that
is what the numbers next to the verdict already say." This is the rollup-level
counterpart to the per-document gate fix in check_scorecard.py's item 5 above (gate no
longer auto-downgraded by source-fidelity findings) — same underlying Pass/Review bug,
fixed at both the document level and the tree-rollup level.

---

## Data/config changes made this session (outside scripts/, not in the zip)

- Archived (moved, not deleted) `out/corpus/104_Shareholding_Disclosure/` and
  `out/corpus/124_Marketing_Restrictions_-_Asset_Management/{Japan__172122,
  Jersey__181919, Malaysia__176985, Turkey__166449}` to
  `out/corpus/_archived_removed/`, per request to remove those jurisdictions and the
  Share-Holding Disclosure product from the Doc Gallery/Scorecard.
- Regenerated `scorecard.json` / `scorecard_post_ai.json` and rebuilt
  `viewer.html` / `inspect.html` for all 19 remaining documents under
  `124_Marketing_Restrictions_-_Asset_Management`, so the Pass/Review gate on screen
  matches the current scoring rules instead of a stale pre-fix computation. Originals
  kept alongside as `*.bak`. Two documents genuinely still gate Review because their
  worst dimension is under 90 on the extraction pass (Mauritius__183013 at 86.5,
  Spain__176285 at 84.8) — both clear to Pass on the post-AI view.