# aosphere-core-index

## Setup

### Requirements

- Python 3.12 (`>=3.12,<3.13` — pinned in `pyproject.toml`)
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- Docker + Docker Compose, if running the full stack (OpenSearch, Dashboards) locally
- System libraries for the extraction path (Debian/Ubuntu): `libgomp1 liblzma5 libgl1
  libglib2.0-0` (see `Dockerfile.extract`) — only needed if you run the PDF
  extraction pipeline (`scripts/run_corpus.py`, `aci build`) locally, not for the
  deployed/query-only service.

### Install

```bash
uv sync
uv pip install -r requirements.txt --python .venv/Scripts/python.exe   # Linux/macOS: .venv/bin/python
```

`uv sync` installs `pyproject.toml`'s CLI/build-time dependencies (`aci`, the extraction
scripts). **It does not know about `requirements.txt`** — that file is the *deployed
service's* runtime deps (fastapi, uvicorn, anyio, litellm, etc., see its own header
comment) and is installed separately, normally only inside the Docker image. Running
the service locally without Docker needs both — skipping the second command means
`uvicorn` can't even import `app.py` (`ModuleNotFoundError: No module named 'fastapi'`).
Re-running a bare `uv sync` after this **will silently remove** everything
`requirements.txt` installed (it isn't tracked by `uv.lock`) — re-run the `uv pip
install -r requirements.txt` line again afterward if that happens.

Two optional extras exist on top of that for the extraction pipeline, deliberately
excluded from the default sync (they pull heavy or extraction-only deps the deployed
service never needs):

```bash
uv sync --extra extract   # PyMuPDF — reads PDFs for pdf2mdtree + verification checks
uv sync --extra mineru    # mineru[all] — layout+OCR model, multi-GB (torch etc.)
```

### Configure

```bash
cp .env.example .env
```

Fill in what you need — most settings have safe local defaults and are commented out.
The essentials for local dev:

- `ACI_EMBED_BACKEND=bge` — local embeddings, no Bedrock/AWS needed
- `ACI_OFFLINE=1` + `ACI_LOCAL_CORPUS_DIR=out/corpus` — serve the Doc Gallery/Scorecard
  straight off a local extraction output dir instead of S3
- AWS creds (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) are only needed for the
  build/extract path (source PDFs) or if using `ACI_EMBED_BACKEND=titan`/Bedrock AI Mode
- Auth (`KEYCLOAK_*`) stays off until those are set — local dev is open by default

### Run

**Full stack (Docker Compose)** — app + OpenSearch + Dashboards:

```bash
docker compose up
```

- App: http://localhost:8000
- OpenSearch: http://localhost:9200
- OpenSearch Dashboards: http://localhost:5601

**App only, no Docker** (e.g. for local Doc Gallery/Scorecard work against
`out/corpus`):

```bash
uv run uvicorn aosphere_core_index.service.app:app --host 127.0.0.1 --port 8000
```

(`--host 0.0.0.0` binds all interfaces and is what the Docker image uses — uvicorn then
prints `http://0.0.0.0:8000` literally, which is not a browsable address. Use
`127.0.0.1` for a plain local run so the printed link actually opens.)

### Tests

```bash
uv run pytest
```

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
