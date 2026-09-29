# The column-gutter threshold (`MIN_COL_GUTTER`)

`pdf2mdtree._colbounds` decides how many columns a block of text has by projecting
every span onto the x-axis and looking for vertical stripes with no coverage. A
stripe counts as a column boundary once it is at least `MIN_COL_GUTTER` wide.

**Value: 12. Lowering it to 7 was tried, measured, and reverted — see the numbers
below before trying it again.**

That one number decides whether a horizontally-ruled block is handed to MinerU as a
table or flowed out as prose: `detect_tables` requires `ncols >= 2` before it will
defer a region, and below that the block is treated as a definition list — "no 2D
layout to lose" — and its text is emitted linearly.

## The problem it was meant to fix

Australia__181814 appendix 5, pages 150–153: a genuine two-column table (overseas
regulatory regime → the relief available) scores **one** column and is never
deferred. 1,188 words reach the tree as consecutive paragraphs, so the pairing
between a regime and its relief survives only as reading order.

Per row the gutter is comfortable. It is one over-long left cell that closes it:

| row | left cell ends | right cell starts | gap |
|---|---|---|---|
| `Financial Supervisory Authority` | 194.0 | 231.9 | 38 |
| `Denmark—if regulated by the Danish ` | 223.6 | 231.9 | **8.3pt → 7 units** |

Coverage is projected over the **whole band**, so that single line closes the gutter
for every row beneath it. One over-long cell hides the columns from the entire block.

The raw float distance is 8.3pt but the measured width is 7: `_colbounds` marks
`int(x0)`..`int(x1)` inclusive, so 223.6 covers through 223 and 231.9 starts at 231,
leaving 224–230. **A threshold of 8 does not fix this page** — and it introduces a
spurious boundary at x=117 inside the header band's left cell, which is worse than
the original bug.

## Why 7 was reverted

Measured on the three jurisdictions of 124_Marketing_Restrictions_-_Asset_Management,
same code, same machine, same day:

| document | gutter | gate | completeness | fidelity | uniqueness | regions |
|---|---|---|---|---|---|---|
| ADGM__170680 | 12 | review | 80.2 | 98.1 | 85.1 | 13 |
| ADGM__170680 | **7** | review | 80.2 | 98.1 | 85.1 | 13 |
| Argentina__176509 | 12 | pass | 92.6 | 93.6 | **91.3** | 22 |
| Argentina__176509 | **7** | pass | 94.1 | 92.2 | **56.8** | 23 |
| Australia__181814 | 12 | review | 84.7 | 94.6 | 90.6 | 12 |
| Australia__181814 | **7** | **FAIL** | **0.0** | 25.0 | – | 16 |

Two findings, and the benefit was one table on one document:

**Argentina lost a third of its uniqueness** (91.3 → 56.8) for one extra region. A
wide gap *inside* a cell now reads as a column boundary, so content is emitted twice.
Argentina has no portrait appendix tables — it took the entire cost and none of the
benefit, which is what every other product in the corpus would also do.

**Australia's MinerU pass crashed outright**, twice, reproducibly — once inside a
three-document run and once in isolation (`ok=0 failed=16`, `mineru exited 1`, no
exception text captured). It dies immediately after the `Table orientation` stage
completes, and the four new regions are the document's only PORTRAIT full-page
tables. The combined PDF is only 3% larger, so size is not the cause. The same
document succeeded twice at 12 on the same machine.

## The fix that would actually work

Measure the gutter **per row and take the majority**, so one over-long cell cannot
erase a boundary the other rows agree on. That keeps the 12-unit bar intact for
genuine single-column blocks, which is what protects Argentina.

It is a change to how `_colbounds` works rather than to its threshold, so it needs a
corpus-wide before/after — 92 documents where table detection would shift — not a
three-document check. Watch `uniqueness` (duplication from spurious boundaries) and
`tables_failed` (regions newly handed to MinerU that it cannot parse).

A second change is required alongside it, and on its own is a no-op: the region
`detect_tables` defers comes from `find_tables`' bbox, which under horizontal-only
ruling is routinely just the header band — 45pt of a 600pt table on p150. Anchoring
instead on the run of consecutive multi-column row bands captures the whole table
(and correctly stops at full-width prose, which reads as one column). That was
written and tested during the 7 experiment; it is not in the tree, because at 12 it
changes region counts (92 → 96 on Australia) with no benefit.

## Related

- `docs/REVERT_multipage_table_gate.md` — the other table-detection constant that was
  tuned, reverted, and documented.
- `MAX_STITCHED_CELL` in `pdf2mdtree.py` — the run-validity gate, relaxed per-product
  for this same document family (`scripts/product_rules.py`).
