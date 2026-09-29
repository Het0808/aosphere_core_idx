"""Chunker EVAL UI: upload the SAME jurisdiction's memo as a PDF (→ MinerU) and a
DOCX (→ legacy), then score both chunkings against that region's gold eval cases
with one shared local embedder. Launched via `aci eval-ui`.

/api/eval is a SYNC def on purpose — MinerU (PDF) can take minutes; FastAPI runs
sync endpoints in a threadpool so the event loop isn't blocked.
"""
from __future__ import annotations

import os
os.environ.setdefault("ACI_EMBED_BACKEND", "bge")   # local/offline; same embedder both arms
os.environ.setdefault("ACI_OFFLINE", "1")

import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from aosphere_core_index.extract import chunk_eval

app = FastAPI(title="Chunker Eval — MinerU vs legacy")
_TEMPLATE = (Path(__file__).parent / "eval_ui_template.html").read_text()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _TEMPLATE


@app.get("/api/eval/regions")
def regions() -> JSONResponse:
    """Regions with gold cases (product, jurisdiction, count) — newest-first by count."""
    return JSONResponse(chunk_eval.list_regions())


@app.get("/api/eval/hybrid-dirs")
def hybrid_dirs() -> JSONResponse:
    """03_stage3_final trees already on disk from the pdf2mdtree/hybrid_extract
    pipeline, so a hybrid run needs no upload/re-extraction — just pick one."""
    return JSONResponse(chunk_eval.list_hybrid_dirs())


def _save(upload: UploadFile | None) -> str | None:
    if upload is None or not upload.filename:
        return None
    suffix = Path(upload.filename).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(upload.file, tmp)
        return tmp.name


@app.post("/api/eval")
def evaluate(product: str = Form(...), jurisdiction: str = Form(...),
             synth: int = Form(0), hybrid_dir: str | None = Form(None),
             mineru_backend: str | None = Form(None), mineru_effort: str | None = Form(None),
             pdf: UploadFile | None = None, docx: UploadFile | None = None,
             hybrid_pdf: UploadFile | None = None) -> JSONResponse:
    pdf_path = _save(pdf)
    docx_path = _save(docx)
    hybrid_pdf_path = _save(hybrid_pdf)
    try:
        if not pdf_path and not docx_path and not hybrid_dir and not hybrid_pdf_path:
            return JSONResponse(
                {"error": "upload a PDF and/or a DOCX, or pick/upload a hybrid source"},
                status_code=400)
        report = chunk_eval.run_eval(product, jurisdiction, docx_path=docx_path, pdf_path=pdf_path,
                                     hybrid_dir=hybrid_dir or None, hybrid_pdf_path=hybrid_pdf_path,
                                     synth=synth, mineru_backend=mineru_backend,
                                     mineru_effort=mineru_effort)
        report["filenames"] = {"pdf": pdf and pdf.filename, "docx": docx and docx.filename,
                               "hybrid_dir": hybrid_dir, "hybrid_pdf": hybrid_pdf and hybrid_pdf.filename}
        return JSONResponse(report)
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        import traceback
        return JSONResponse({"error": f"{type(e).__name__}: {e}",
                             "trace": traceback.format_exc()[-1500:]}, status_code=500)
    finally:
        for p in (pdf_path, docx_path, hybrid_pdf_path):
            if p:
                Path(p).unlink(missing_ok=True)
