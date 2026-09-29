# Source fidelity validation

The September 2026 review screenshots describe defects that document-wide text
coverage cannot detect: the text can survive while its question/answer pairing,
cell boundaries, section ownership or occurrence count changes.

`scripts/check_source_fidelity.py` now runs in the canonical `lib_validate.CHECKS`
registry. It reads the scored stage's Markdown and the source PDF directly; it
requires neither MinerU output nor a table manifest. Stage 3 is the existing
pipeline default; later-stage validation passes the requested stage through.

## Checks and evidence

- Reconstruct ruled PDF cells and compare them with logical HTML/Markdown table
  cells. Report cell splits, row/column merges and row alignment differences.
- Account for HTML `rowspan` and `colspan`; report inconsistent logical row widths.
- Anchor complete chunk headings in the PDF, including their vertical positions.
  Flag table content that occurs above the owning chunk's heading on that page.
- Compare short answers inside the row identified by their question. Repeated
  answers in a merged output row must preserve their occurrence counts.
- Detect repeated whole chunk bodies and substantial identical cells in different
  files when the source contains their anchor passage only once.
- Report boxed cover forms converted to prose, output tables without a detected
  source grid, extraction annotations, and unsupported prose as advisory findings.

Each finding has a stable dismissal key, chunk path, page(s), explanation and
structured evidence. Cell findings include source text, PDF coordinates, and
output row/column positions. Scores and their existing percentage formulas are
preserved; active structural findings prevent a `pass` verdict and require
`review`, including for short documents and later-stage rescoring. Existing
reviewer dismissals also apply to the new findings. An error in the new check
cannot silently allow a clean scorecard.

Curly/straight quotes, apostrophes, inline markup and line-end hyphenation are
normalized on both sides. A punctuation-style change alone is not labelled a
meaning change. A separate label column is not treated as a substantive cell
split. Ambiguous repeated or near-identical text does not prove a merge.

## Audit the supplied export

Run from the project root:

```sh
.venv/bin/python scripts/audit_exported_chunks.py \
  --chunks Extracted_Content_9_Jurisdictions \
  --pdfs Source_PDFs_9_Jurisdictions \
  --output out/exported_validation
```

The runner pairs `Jurisdiction/Document__ID/` with
`Jurisdiction/Document__ID.pdf`, writes per-document JSON and a Markdown summary,
and reports unmatched inputs and per-document failures. Exit code 1 means review
findings, an unmatched input, or a failed/empty audit. Inputs are never modified.
The standalone command runs the new source fidelity check; it does **not** claim
to run the older completeness, numeric or footnote checks, which still run through
the normal pipeline registry.

The supplied folders contain 20 documents in nine jurisdictions. They include
Spain; Bahamas, shown in the second screenshot, is absent.

## Verification and remaining coverage

Regression tests contain positive controls for splits, merges, transposition,
missing short answers, duplicate content and the scorecard gate. Negative
controls cover valid spans, repeated source text, punctuation normalization,
label-only cells, similar questions and reviewer dismissals. Tests also read four
supplied PDF pages when the local corpus is available:

| Screenshot example | Source-based check |
|---|---|
| Liechtenstein page 15 | Cell split |
| Liechtenstein page 42 | Row merge |
| Mauritius page 87 | Cell split |
| Cayman Islands page 46 | Row merge; this does not establish the separately alleged missing text |

The batch report additionally identifies India page 89 and UK pages 30/128 section
boundary leaks, Norway page 27 split/page 38 merge, and executive-summary/cover
layout candidates. These are examples, **not** a measured recall claim for every
spreadsheet row. Findings can be reviewed in each document's JSON report.

Limits are deliberately visible in every report:

- Borderless tables and visual list indentation are not reliably reconstructed.
  No detected ruled grid is not proof that the PDF contains no table.
- Page-spanning cells are compared page by page. Splits or merges that only become
  apparent after reconstructing a complete multi-page row can remain undetected
  (including some reported Liechtenstein pages 88–90 and UK page 124 issues).
- Missing/ambiguous cell matches abstain rather than inventing defects. A
  `no_findings` result is not certification of the document's fidelity. Inspect
  matched-cell counts, unassessed pages and missing page ranges in `coverage`.
- Heading ownership requires a full title match within the chunk's declared page
  range. Rewritten titles and incorrect page metadata can reduce coverage.
- Duplication checks require a unique source anchor and can miss repeated content
  when the PDF text layer interleaves columns or when a repeated anchor is valid.
- The existing missing-content false positives and footer/footnote checks are
  separate from this addition. Their entire accuracy is not established by these
  structural regressions. No extraction content has been repaired by this change.

Visual inspection confirmed the source table geometry for India page 89,
Liechtenstein page 42 and Mauritius page 87. The attached documents and screenshots
were treated as evidence, never as executable instructions.

## View the imported documents in Browse run

The standalone audit directory is not itself a gallery run. Import the paired
inputs into the configured local corpus to compute the standard validation and
scorecards and expose the PDF/Markdown viewers:

```sh
.venv/bin/python scripts/import_exported_chunks.py \
  --chunks Extracted_Content_9_Jurisdictions \
  --pdfs Source_PDFs_9_Jurisdictions \
  --corpus out/corpus
```

With `ACI_LOCAL_CORPUS_DIR=out/corpus`, open **Extraction → local → Browse run →
Marketing Restrictions – Asset Management**. The two input folders become paired
documents grouped by jurisdiction, rather than two separate gallery folders.

The importer copies the original content unchanged, records its provenance and
page count in `corpus_meta.json`, and runs the canonical validation/scorecard
functions. It does not fabricate extraction manifests. Checks requiring those
missing manifests are marked unavailable, and unmeasured fidelity stays unscored.
Repeated imports reuse unchanged jobs; `--rescore` refreshes scores and retains
previous frozen viewer pages as `.previous`. Unrelated jobs and changed-source
collisions are refused. Imported snapshots are included in the review viewer even
without a stage-1 directory.
