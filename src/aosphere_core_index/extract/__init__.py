"""Document extraction: turn a source file into the common ExtractedDoc model.

Two parsing backends, same signature and output shape, split by format —
NOT alternatives for the same input:
  - legacy : Word-style docx extractor (extract_docx) — reads Word's own
             paragraph styles directly. .docx only.
  - mineru : layout/OCR parser via the local mineru CLI (extract_mineru).
             Scoped to formats legacy can't read at all (PDF, scans, ...) —
             see extract/mineru_extract.py's module docstring for why .docx
             was deliberately dropped from its scope (legacy's direct style
             read is strictly more reliable than reconstructing structure
             from MinerU's own, sometimes internally-inconsistent, output).

`extract_document` picks one, so callers stay backend-agnostic. This function
itself doesn't enforce the format scoping (a low-level dispatcher, callable
with either backend on either format for testing) — extract/ab_compare.py's
run_backend() is where that policy is actually enforced for the A/B UI.
"""

from __future__ import annotations

from aosphere_core_index.config import settings
from aosphere_core_index.extract.model import ExtractedDoc


def extract_document(
    path: str, *, doc_meta: dict, source_key: str, backend: str | None = None,
) -> ExtractedDoc:
    """Parse `path` into an ExtractedDoc using the selected backend.

    `backend` (explicit arg > settings.extract_backend) is "legacy" or "mineru".
    Both take the same (path, doc_meta, source_key) and return the same model,
    so downstream code never needs to know which parser ran.
    """
    chosen = (backend or settings.extract_backend or "legacy").lower()
    if chosen == "mineru":
        from aosphere_core_index.extract.mineru_extract import extract_mineru
        return extract_mineru(path, doc_meta=doc_meta, source_key=source_key)
    if chosen == "legacy":
        from aosphere_core_index.extract.docx_extract import extract_docx
        return extract_docx(path, doc_meta=doc_meta, source_key=source_key)
    raise ValueError(f"unknown extract backend {chosen!r} (expected 'legacy' or 'mineru')")
