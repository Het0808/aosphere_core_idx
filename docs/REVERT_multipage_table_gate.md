# REVERT NOTE — multi-page table confidence gate (usa-dp section G)

**Date:** 2026-07-31. **Status:** EXPERIMENTAL — revert if the baseline regresses.

## Why this change exists

`usa-dp.pdf` section G is one table spanning pages 213–280. Stage 1's
`reconstruct_multipage_tables()` correctly builds the run, then **discards it on a
single failed check**:

```
run from page 213: extended to page 272 (60 pages, no early break)

CONFIDENCE GATE (needs rows>=3, maxcell<=2000, keyed>=60%)
  data rows = 49       -> ok
  maxcell   = 3397     -> FAIL   (cap is 2000)
  keyed     = 49/49    -> ok
```

The cell is legitimately that big: this table has only **4–5 horizontal rules per
page** (vs 16 on pages the reconstruction accepts), so one row band holds an
entire state's breach rules. The 2000 cap was tuned for tightly-ruled tables.

Discarding the run made all 60 pages fall to the per-page snapshot fallback,
which caused **three** symptoms at once:

1. **61 single-page placeholders** (`table_028`…`table_088`, all `multipage:false`)
   instead of one table — the "randomly broken every few pages" effect.
2. **Duplicated content** — 43 of 109 substantial prose paragraphs in
   `G-breach-response.md` also appear inside a table. On a fallback page
   `page_has_deferred_table` is only set to `True` *during* the emit loop, so
   lines before the bbox are already emitted as prose; MinerU then renders the
   same region as a table.
3. **36 failure markers + 15 continuation markers + 51 snapshots** in section G
   alone — most of usa-dp's 37 "failed tables", and why Fidelity sat at 62.9.

Note (2) is expected to fix itself: for a `comp_here` page,
`page_has_deferred_table = True` is set BEFORE the emit loop, so the prose-skip
works correctly. No ordering change was needed.

## The change

`scripts/pdf2mdtree.py` — two constants only:

| what | before | after |
|---|---|---|
| max chars in one stitched cell | `maxcell <= 2000` | `MAX_STITCHED_CELL = 4000` |
| max pages in one run | `q - p < 60` | `MAX_RUN_PAGES = 120` |

The page cap also mattered: the run stopped at page 272 purely because of the
60-page limit, so 273–280 could never join even with the gate passing.

## THE RISK, stated plainly

The `maxcell` cap is what guarantees *"reconstruction can only ever improve,
never garble"*. It exists to catch a table whose columns were mis-mapped, which
shows up as one enormous merged cell. **Raising it lets a genuinely garbled table
through.** That is the specific failure this change could introduce, and it would
appear as a table with wrong column boundaries rather than as an error.

## HOW TO REVERT

```bash
cd /Users/preay/Skandiam/aosphere-core-index
# restore the two constants
sed -i '' 's/MAX_STITCHED_CELL = 4000/MAX_STITCHED_CELL = 2000/' scripts/pdf2mdtree.py
sed -i '' 's/MAX_RUN_PAGES = 120/MAX_RUN_PAGES = 60/' scripts/pdf2mdtree.py
# re-extract and confirm the numbers return to the reference below
python scripts/run_baseline.py "/Users/preay/Skandiam/Examples of different files/test folder"
```

## REFERENCE NUMBERS — the state to return to

Saved in `out/baseline/baseline.json` immediately before this change.

| document | gate | compl | place | fidel | silent | tbl-fail |
|---|---|---|---|---|---|---|
| Argentina.pdf | pass | 94.0 | 100.0 | 96.4 | 3 | 0 |
| australia-share.pdf | pass | 97.9 | 100.0 | 95.7 | 3 | 1 |
| united-kingdom-dp.pdf | pass | 97.2 | 100.0 | 95.6 | 31 | 0 |
| usa-dp.pdf | **fail** | 81.1 | 100.0 | **62.9** | 7 | **37** |

**Keep the change if:** usa-dp's `tbl-fail` drops sharply and Fidelity rises,
while the other three documents move by nothing.

**Revert if:** any of Argentina / australia-share / united-kingdom-dp regresses on
any tracked number — those three currently reconstruct cleanly, and this change
must not cost them anything.
