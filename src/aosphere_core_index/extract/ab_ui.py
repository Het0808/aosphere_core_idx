"""A/B extraction comparison UI: upload one PDF or DOCX, see legacy vs MinerU
side by side. Launched via `aci ab-compare-ui`.

The /api/compare endpoint is a SYNC def on purpose — MinerU shells out to the
CLI and can take minutes; FastAPI runs sync endpoints in a threadpool, so this
doesn't block the event loop.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from aosphere_core_index.extract.ab_compare import (
    BackendUnavailable,
    list_cached,
    run_backend,
    run_cached,
)

app = FastAPI(title="Extractor A/B — legacy vs MinerU")
_TEMPLATE = (Path(__file__).parent / "ab_ui_template.html").read_text()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _TEMPLATE


@app.get("/api/cached")
def cached_list() -> JSONResponse:
    """Cached MinerU content_list.json outputs available to render instantly."""
    return JSONResponse(list_cached())


@app.post("/api/cached")
def cached_render(path: str = Form(...), product: str = Form("Shareholding Disclosure")) -> JSONResponse:
    """Render the ported MinerU chunk tree from a cached content_list.json — no
    model, no CLI, instant. Legacy is N/A (this is already-parsed PDF output)."""
    result: dict = {"filename": Path(path).name, "cached": True,
                    "legacy": {"unavailable": "cached MinerU output — legacy extractor is docx-only"}}
    try:
        result["mineru"] = run_cached(path, product)
    except BackendUnavailable as e:
        result["mineru"] = {"unavailable": str(e)}
    except Exception as e:  # noqa: BLE001 — surface any extraction error to the UI
        result["mineru"] = {"error": f"{type(e).__name__}: {e}"}
    return JSONResponse(result)


@app.post("/api/compare")
def compare(
    file: UploadFile,
    product: str = Form("Shareholding Disclosure"),
    mineru_backend: str = Form("vlm-engine"),
    effort: str = Form(""),
) -> JSONResponse:
    """Run a live upload through both extractors. `mineru_backend`
    ("vlm-engine" | "hybrid-engine" | "pipeline") and `effort` ("medium" |
    "high", hybrid only) pick the MinerU parse mode for this run."""
    suffix = Path(file.filename or "upload").suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        path = tmp.name
    try:
        result: dict = {"filename": file.filename}
        for backend in ("legacy", "mineru"):
            try:
                if backend == "mineru":
                    result[backend] = run_backend(path, backend, product,
                                                  mineru_backend=mineru_backend, effort=effort or None)
                else:
                    result[backend] = run_backend(path, backend, product)
            except BackendUnavailable as e:
                result[backend] = {"unavailable": str(e)}
            except Exception as e:  # noqa: BLE001 — surface any extraction error to the UI
                result[backend] = {"error": f"{type(e).__name__}: {e}"}
        return JSONResponse(result)
    finally:
        Path(path).unlink(missing_ok=True)
