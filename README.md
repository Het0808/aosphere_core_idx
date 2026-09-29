# aosphere-core-index

## Running the GPU corpus extraction

Extracting a corpus on the cluster is driven entirely by the
**Build & Deploy Extract Worker** GitHub Action — build a new image, change
`CORPUS_SHARDS`, switch between a dry run and the real thing, all from one
dispatch. Nothing needs hand-editing in `k8s-deployment`.

See **[`docs/GPU_EXTRACTION_RUNBOOK.md`](docs/GPU_EXTRACTION_RUNBOOK.md)** for the
inputs, the common recipes, how resume works (and the one input that can throw
away days of work), troubleshooting, and cost/capacity planning.

## Document extraction backend

Document parsing (DOCX/PDF → typed sections) runs behind a pluggable
`Profile` interface (`src/aosphere_core_index/extract/profiles/`), so the rest
of the pipeline — chunking, embedding, indexing, the service — never knows or
cares which parser produced a document.

- **`mineru`** (default) — [opendatalab/mineru](https://github.com/opendatalab/mineru),
  a layout+OCR model. Needs the `mineru` CLI: `uv sync --extra mineru`.
- **`legacy`** — the original Word-style-based `extract_docx`, still fully
  supported as a fallback.

Switch backends per-run with no code change:

```bash
ACI_EXTRACT_BACKEND=legacy aci build <region>
```

Only affects build-time tooling (`aci build`/`build-all`/`build-batch`) — the
deployed service (`ACI_OFFLINE=1`) reads pre-baked artifacts and never parses
documents itself. See `docs/MINERU_MIGRATION.md` for the full design, empirical
findings, and pilot status.
