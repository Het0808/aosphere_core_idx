# Fixes to do

**Status: 1-6 and 8 implemented and tested (375 passing). 7 and the items under
"known, still open" are not started.** Fix 8 has an outstanding guard — see its section.

Found while investigating `124_Marketing_Restrictions_-_Asset_Management`, mostly on
`Bermuda__166524`. **Nothing here is implemented.** Ordered by what it costs the
output, not by effort.

Every number below was measured, not estimated; the command that produced it is given
so it can be re-checked after a change. Where something is inferred rather than
measured it says so.

Deliberately not started yet: editing a module while `run_corpus` is mid-flight does
nothing to the running process (it imported already) and makes the diff impossible to
attribute. Do these after the run, one at a time, each with a before/after.

---

## 1. Stage 3 silently deletes cells when stitching a multi-page table  ⚠️ worst  ✅ IMPLEMENTED

**File:** `scripts/hybrid_extract.py:491`

```python
if bi > 0 and ri == 0 and rows and not cells[0]["text"]:
    prev = rows[-1]
    for j in range(min(len(cells), len(prev))):   # <-- min() truncates
        if cells[j]["text"]:
            prev[j]["text"] = normspace(prev[j]["text"] + " " + cells[j]["text"])
    continue                                       # <-- rest of the row discarded
```

MinerU splits a page-spanning table into one `<table>` per page-crop, and a row cut
across that boundary resumes with a blank first cell. This code correctly glues those
halves back together — but copies only `min(len(cells), len(prev))` cells. When the
continuation piece is WIDER than the row it is joining, the surplus cells are never
copied, and `continue` then drops the row that held them.

**What it cost on Bermuda__166524:** the 216-word answer to question (d)(i) — whether a
local intermediary may be used, covering the cold-calling, investment business and
overseas fund restrictions — plus a 54-word answer in `18-penalties-sanctions.md` about
agreements being voidable/unenforceable. Both were **extracted correctly by MinerU** and
destroyed on the way in. Absent from Stage 1 and Stage 3, all 22 files.

The surviving row is also corrupted, not merely short: a column header is concatenated
onto a question.

```
fragment A (2 cols): <td>(d)(i) Is it… interests in a</td><td>Carrying on Business Restriction</td>
fragment B (3 cols): <td></td><td>Fund to an investor…</td><td>There is no legal requirement…</td>
result     (2 cols): '(d)(i) Is it…' | 'Carrying on Business Restriction Fund to an investor…'
                                        ^ header + question fused      ^ 3rd cell gone
```

**Scale, on that one document:** fragment widths run
`[2,2,2,3,2,2,3,2,3,2,3,2,6,3,3,3,2,2,3,2,3,2,3,2,3,3,2,3,2,3]` — **11 boundaries where
the next piece is wider** (10 with one surplus column, one with four). Roughly
**1,755 of MinerU's 27,218 table words (6.4%) never reach the tree.**

Invisible to the scorecard: `fidelity 97.7`, `placement 100`. The table is present and
correctly located — just missing a column.

**Proposed fix** — grow the row instead of truncating it:

```python
if len(cells) > len(prev):
    prev.extend({"text": "", "rowspan": 1, "colspan": 1}
                for _ in range(len(cells) - len(prev)))
for j in range(len(cells)):
    ...
```

**Risk to watch:** if a wider piece is a genuinely NEW table rather than a continuation,
widening merges two tables and text can repeat. Same failure mode that took Argentina's
`uniqueness` from 91.3 to 56.8 during the `MIN_COL_GUTTER = 7` experiment. The guard
conditions (`bi > 0`, `ri == 0`, blank first cell) are unchanged, and same-width tables
take the identical path, so this can only add cells that are currently binned — but
measure anyway.

### Agreed strategy — do NOT ship `prev.extend()` alone

`prev.extend()` is the first repair, not the plan. Treat this as an extraction
correctness problem: **Detect -> Validate -> Repair -> Measure -> Accept/Reject.**

**Step 1 — preserve raw MinerU output. ALREADY DONE, no work needed.**
`02_stage2_mineru_tables/mineru_raw/` holds MinerU's untouched output plus a
`.cache_key`. That is exactly how this bug was proved: the 216 words are PRESENT in
`mineru_raw` and ABSENT from the final tree, which pins the loss on our
post-processing rather than on MinerU. Keep this guarantee — every future repair must
be answerable to "did MinerU get it wrong, or did we destroy it?"

**Step 2 — never silently discard cells. This is the biggest change, and it comes
first.** Invariant: *no extracted text disappears without a record.* When
`len(cells) > len(prev)`, emit an anomaly rather than falling through `continue`:

```
TABLE_CONTINUATION_WIDER_ROW  table=table_006 pages=[36..50] page=41
                              previous_cells=2 new_cells=3 surplus_text="There is no legal…"
```

There is a precedent for the plumbing: `check_content_localized.find_gaps` takes
`stats_out=None` and fills it (`check_content_localized.py:290`). Use the same shape —
`anomalies_out=None` — so `stitch_table_html`'s return signature stays
`(html, n_rows, n_cols)`. Then surface the log as a scorecard finding kind (alongside
`hollow` / `section_missing` / `build_rule`), because an anomaly file nobody reads is
not a control.

**Step 3 — score confidence before merging, but NOT on column similarity.**

⚠️ **Column similarity would BLOCK the repair we need.** In the Bermuda case the two
fragments are **2 cols vs 3 cols** — deliberately dissimilar, because the mis-split is
what we are repairing. A model scoring "same column structure +30 / same table
structure +20" gives this genuine continuation **0 of those 50 points** and refuses to
merge. Column agreement is evidence the split was CLEAN; the cases needing repair are
precisely the unclean ones.

What actually discriminates, measured on the real pair:

| signal | on Bermuda p41 | weight |
|---|---|---|
| previous row ends mid-sentence | last cell ends `"…interests in a"` — on the article "a" | **strongest** |
| continuation completes the sentence | next piece opens `"Fund to an investor…"` | **strong** |
| blank first cell in the continuation | yes | strong |
| pages are adjacent | yes | moderate |
| column counts agree | **NO — 2 vs 3** | weak/neutral, never a blocker |
| same bbox x-geometry | not yet available, see step 4 | moderate |

So invert the proposed weighting: *previous row ends mid-sentence* carries the decision,
not column geometry.

**Step 4 — three of the six signals need data plumbed in.** `stitch_table_html`
currently receives ONLY `(html_blocks, footnote_ids)` — no geometry, no page numbers, no
table id. The call site at `hybrid_extract.py:1124` already holds all of it in `t`
(`pages`, `bboxes`, `table_id`). Pass `t` in. One call site in the whole repo, so this is
cheap.

**Step 5 — when confidence is LOW, refuse to merge.** Keep both fragments as separate
tables and log it. That loses **no text** — it only risks one table appearing as two.
Today's behaviour loses text outright. So "refuse when unsure" is strictly safer than
both the current code and an aggressive merge, and it is the right failure direction for
legal content: a split table is recoverable by a reader, a deleted answer is not.

**Step 6 — accept/reject on measurement, not on reasoning.** Gate on `uniqueness`
(duplication from wrong merges), per-table row/column counts, and the anomaly count
trending to zero for the same input. `scripts/corpus_run_diff.py` reads existing
scorecards without re-scoring, and every job now carries `timing` in its scorecard.

**Tests must land with the fix — `stitch_table_html` has none today.** The nearest,
`tests/test_continuation_stub.py`, covers VERIFYING a stitched continuation, not the
stitching. Needed, all callable directly against the function with no PDF:
3-cell-onto-2-cell keeps the third cell; equal-width output byte-identical to today
(the regression guard); narrower continuation unchanged; blank first cell MID-block
still not merged (the rowspan protection the docstring warns about); a wider piece that
is a genuinely NEW table does not merge.

**Verify with:**
```
# per-table row/col counts before and after; columns should rise ONLY where a wider
# continuation exists, and uniqueness must not fall
python3 scripts/corpus_run_diff.py <before> <after>
```

---

## 2. Printed-TOC rows are dropped on purpose, then billed as content loss  ✅ IMPLEMENTED

Stage 1 suppresses printed contents rows (`_is_toc_row`) so section titles are not
duplicated. `check_content_localized` has no idea this was deliberate and counts them as
silent gaps.

Measured on Bermuda: `07-part-b-contents.md` holds **10 words** with all **571** TOC-row
tokens suppressed. **3 of its 9 silent gaps** are this.

**Fix:** Stage 1 already knows which spans it dropped and why — record them, and let the
validator treat a recorded deliberate drop as acknowledged rather than silent.

**Worth:** +13.6 completeness on Bermuda (58.8 → 72.4) and removes a whole class of
false alarms corpus-wide.

---

## 3. Boilerplate detector misses furniture that appears on few pages  ✅ IMPLEMENTED

`Legal - 24320065.2` sits on pages **1, 2, 15, 90 — 4 of 93**. The detector caught
`4147-1689-3533, v. 6` on **89 of 93** and `CONFIDENTIAL` on 92, so 4 is far under the
bar. Because front-matter sections hold only 1–6 tokens each, a six-word footer IS the
whole section's content and becomes a silent gap. **2 of Bermuda's 9 gaps.**

**Fix:** treat a document-reference shape (`Legal - <digits>.<digits>` and friends) as
furniture regardless of page count, or drop the page threshold when the pattern matches
a reference/footer shape rather than prose.

**Worth:** +9.1 on Bermuda (72.4 → 81.5, `fail` → `review`).

---

## 4. A wrapped cover-page title becomes five separate sections  ✅ IMPLEMENTED

```
01-restrictions-on-cross-border.md   # RESTRICTIONS ON CROSS-BORDER
02-marketing-and-selling-of.md       # MARKETING AND SELLING OF
03-funds-and-investment-…            # FUNDS AND INVESTMENT MANAGEMENT & ADVISORY SERVICES
04-into.md                           # INTO
05-bermuda.md                        # BERMUDA
```

One sentence — the document's title, wrapped over five lines on the cover. **None of the
five is in the 39-entry outline;** all were invented by the front-matter font heuristic
(`pre_outline_pages_recovered: 2`). So 5 of 22 sections (23%) are fragments of one
title, including a section named `INTO` holding 1 token.

Knock-on effects: junk nodes; sections so small that any stray footer word is a whole
"gap" (causes 2 of the 8 above); a smaller section denominator, which amplifies every
per-section penalty; and it is the same 1-token `into` title that previously broke
hollow-section detection.

**Fix:** consecutive same-size all-caps lines on a pre-outline page with no body text
between them are ONE heading, not several.

**Scope — measured but only a proxy:** 153 of 167 documents scanned have 3+ sections
with ≤6 own tokens. That is a correlation, NOT a confirmed count of this bug: some are
legitimately short (`[SECTION INTENTIONALLY LEFT BLANK]`). Needs a real count before
sizing the work.

---

## 5. A short-titled section fuses with a blank sibling and is never built  ✅ IMPLEMENTED

Affects **Bahamas__183503, Bahrain__169749, Belgium__163341** — section 10 `LICENCE`
missing entirely, its content misfiled under a heading reading *"[SECTION INTENTIONALLY
LEFT BLANK]"*.

Traced with an instrumented matcher:

```
[DBG] item='10 LICENCE' page=64 target='10licence' rest='licence' pi=255
[DBG]   j=255 pg=64 head=False skip=False n='9sectionintentionallyleftblank10licence'
[DBG] => found=None
```

Section 9 is `[SECTION INTENTIONALLY LEFT BLANK]` and has no body, so its heading and
section 10's heading end up in **one paragraph**. Then, in the matcher:

| test | result |
|---|---|
| `n.startswith(target)` | no — paragraph starts `9section…` |
| loose window `target in n[:len(target)+30]` | **would have matched** |
| …gated on `len(target) > 12` | `len('10licence')` is 9 → skipped |
| wrap accumulator / `n.startswith(rest)` | no |

**Fix (narrow, do first):** allow the loose window when the target carries a leading
enumeration, regardless of length — `10licence` is far more specific than bare
`licence`, so it does not reopen the cross-reference problem the 12-char guard exists
for (see the comment at that guard for the case that motivated it).

**Fix (root cause):** do not fuse two headings into one paragraph. Section 10's number
restarts at the left margin (x=56.9, same as section 9's) at heading size — that is a
paragraph break. Touches paragraph assembly, so it needs a corpus-wide before/after.

---

## 6. Tier 2 of the fallback chain runs even when there is nothing to rescue  ✅ IMPLEMENTED

Once the chain is entered, the printed-TOC rescue runs unconditionally — including when
`toc = 100`, i.e. the outline is already correct, often because the pre-flight repaired
it minutes earlier.

Measured on the pre-fix ADGM run: `toc_rescue 1171.2s` of a `2282.9s` total — **more
than half the document's time** to re-derive an outline it already had. Both the root
`source_repaired.pdf` and `fallback/source_repaired.pdf` came out with the same 41
bookmarks and identical scores (84.7 / 100 / 100).

The entry gate is already fixed (`needs_help`, and ADGM now runs in 9.75 min). This is
the other half.

**Fix:** skip tier 2 when `toc >= 70`, or when `toc_preflight.json` records
`applied: true`. Go straight to tier 3 if the document still needs help.

---

## 7. MinerU volume per pass — the cause of the Bermuda wedge

Bermuda sent **77 of its 93 pages** to MinerU in one pass. That pass ran 3h15m at ~31%
CPU and wrote **zero** output before being killed; the machine was at
`swap used 13,210 MB of 14,336 MB, 1,978,513 swapouts`. Killing it demoted the document
into the chain, which then re-ran the same 77 pages.

The flat product rule (`clause_tables=True`) relaxes `MAX_STITCHED_CELL`, so most of a
document becomes one table run and nearly every page is deferred.

Observed spread on this product: median 1.57 min, but `Belgium__163341` took **171 min**
and one document in `154_Marketing_Restrictions` took **84 min** — every outlier's
slowest stage is a MinerU or fallback tier.

**Fix candidates (not yet chosen):** cap pages per MinerU invocation and batch, or cap
the length of a stitched clause run under `clause_tables`. Needs a decision on the
trade-off against fix #1, which makes long runs more valuable to keep whole.

---

## 8. content_list.json drops rows of a page-spanning table  ✅ IMPLEMENTED

**File:** `scripts/hybrid_extract.py` — `middle_table_html_by_page()` + the `htmls`
assembly in `run_stage2`. Tests: `tests/test_middle_json_supplement.py` (7).

Stage 2 reads MinerU's `_content_list.json`, which is the right file to drive from —
normalised and documented. But it MERGES a table spanning pages into ONE entry
(mineru's `make_blocks_to_content_list`), so the table's other pages arrive as empty
`table_body` stubs. Those stubs still match geometrically at high IoU, so the region is
stamped `ok=true` and nothing reports that the rows never came.

Measured on `Saudi Arabia__174731` table_007, pages 17-26:

```
page   raw table_body chars   kept by the stitcher
  17          0                   0/0
  18          0                   0/0        <- 8 of 10 pages arrived EMPTY
  19          0                   0/0
  20          0                   0/0
  21     12,082                 34/34        <- the whole merged table, on one page
  26        742                   4/4
```

The stitcher kept everything it was given (34/34, 4/4) — it was never at fault here.
And content_list's merged form silently omits question (a) altogether, while
`_middle.json` keeps the same table split per page with question (a) intact on page 17.

**Fix:** when a matched block's `table_body` is blank, substitute that page's own table
HTML from `_middle.json`. It only ever fills a blank, so a block that already has a
body is untouched and a working table cannot change. Records
`body_from_middle_json: [pages]` in the region's status.json when it fires. A missing or
unreadable middle.json returns `{}` — exactly today's behaviour.

Re-stitching table_007's ten real blocks with this in place: **19 rows -> 28 rows,
12,802 -> 22,231 characters, question (a) recovered.**

### Investigation record — three hypotheses that were WRONG

Kept because each cost real time and would otherwise be re-tried:

1. **"Stage 3 drops region output."** No: every region's markdown reaches the tree in
   full (4/4, 8/8, 8/8, 8/8 of its own runs found).
2. **"Stage 2 keeps one block per page, best-IoU wins."** No: the code already
   concatenates every block above `CONSIDER_IOU` (`split_blocks`), and pages 18-19
   matched at IoU 0.99.
3. **"Regions are missing per-page bboxes."** No: every multipage region has a bbox for
   every page it claims.

And one measurement error that inflated the problem three times: **all probes must be
space-insensitive.** MinerU concatenates text across inline tags — `"Please outline
thelegal basisof permitted"` — so whitespace-sensitive matching under-reports badly.
Page 17 read as 33% present and is actually 72%. Compare with
`re.sub(r'[^a-z0-9]', '', html.unescape(s).lower())`, never on word runs.

### Still missing: the guard

The fix addresses this instance; it does NOT stop the class recurring. A region can
still report `ok=true` while its pages' text is absent, because nothing checks. The
machinery already exists — `verbatim_match` / `_best_verbatim` run on the FAILURE
paths only. Run the same check per page on the SUCCESS path, record recall in
status.json, and downgrade `ok` when a matched page's recall is low. Must be
space-insensitive per the note above, or it will fire constantly on the tag artifact.

## Known, previously recorded, still open

- **`completeness` per-file denominator on flat builds.** With `build_depth = 0` there
  are ~22 sections instead of ~100, so each flagged section costs `100/22 = 4.5` points
  instead of ~1. Not a bug in itself — 22 is the true node count — but it makes every
  artifact ~4x more expensive on this product than anywhere else. `special_mode`
  declares the flat build; nothing compensates for it.
- **`fidelity` is an unweighted per-table mean**, so a 29-page table counts the same as
  a 1-page one.
- **MinerU scrambles the appendix table's header row** (Australia appendix 5).
- **`rescue_outline`'s printed-TOC parser splits wrapped titles** into separate entries,
  and `_legal_levels` then promotes the fragments to level 1.
- **MinerU dropped 14 words on Bermuda p80** (`17-licence.md`, the question label
  "(i) any specific legal form requirements…"). MinerU's own loss, not ours — absent
  from its raw output. Nothing to fix on our side; noted so it is not re-investigated.

---

## Related

- `docs/COLUMN_GUTTER_THRESHOLD.md` — the table-detection constant that was tuned,
  measured, reverted, and documented. Read before touching table detection.
- `docs/REVERT_multipage_table_gate.md` — the other reverted table constant.
