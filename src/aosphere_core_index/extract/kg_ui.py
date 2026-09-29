"""Local-only web UI: upload a document, run MinerU + kg_export, browse the
resulting chunks. Standalone from the main search service (no auth, no
embedding models) — one workflow, run entirely on this machine.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

from aosphere_core_index.config import settings
from aosphere_core_index.extract.kg_export import export_chunks
from aosphere_core_index.extract.mineru_runner import MineruError, run_mineru

app = FastAPI(title="Chunk & Knowledge-Graph Viewer")
_TEMPLATE = (Path(__file__).parent / "kg_ui_template.html").read_text()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _TEMPLATE


@app.get("/api/backend")
def backend() -> dict:
    return {"backend": settings.mineru_backend}


@app.post("/api/chunk")
async def chunk(file: UploadFile) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / (file.filename or "upload")
        src.write_bytes(await file.read())

        try:
            blocks = run_mineru(
                str(src), out_dir=tmp_dir / "mineru_out", backend=settings.mineru_backend,
                timeout=settings.mineru_timeout_s, config_path=settings.mineru_config_path,
                model_cache=settings.mineru_model_cache,
            )
        except MineruError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

        out_dir = tmp_dir / "chunks"
        export_chunks(blocks, doc_id=src.stem, source_file=file.filename or src.name, out_dir=out_dir)
        manifest = json.loads((out_dir / "knowledge_graph.json").read_text())

        combined = []
        for item in manifest["itemListElement"]:
            md = (out_dir / item["file"]).read_text()
            body = md.split("---\n", 2)[2].strip() if md.count("---") >= 2 else md
            combined.append({**item["item"], "body": body})
        return combined
