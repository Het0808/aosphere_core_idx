"""Run ONE uploaded document through both extraction backends and return their
structured results for side-by-side comparison in the A/B UI.

Extraction only — no embedding/search: the point is to SEE what each parser
produces (sections, clause keys, levels, tables, footnotes). Scope is split by
format, not overlapping: legacy reads Word styles directly, so it's .docx
only; MinerU is scoped to what legacy CANNOT read (PDF, scans, ...) — see
extract/mineru_extract.py's module docstring for why .docx was deliberately
dropped from MinerU's scope. A .docx upload shows legacy against MinerU "not
applicable"; a PDF upload shows MinerU against legacy "not applicable".
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from aosphere_core_index.config import settings
from aosphere_core_index.extract import extract_document
from aosphere_core_index.extract.model import ExtractedDoc


class BackendUnavailable(RuntimeError):
    """This backend cannot run on this input at all (not a failure to report)."""


def _doc_meta(path: str, product: str) -> dict:
    stem = Path(path).stem
    return {
        "DOCID": stem, "DOCNAME": stem, "JURISDICTIONNAME": "ab-test",
        "JURISDICTIONID": None, "OPINIONID": None, "RENDERSTYLENAME": product,
        "EXTENSION": Path(path).suffix.lstrip("."),
    }


def _tree(doc: ExtractedDoc) -> list[dict]:
    by_id = {s.id: s for s in doc.sections}
    return [
        {
            "key": s.key, "title": s.title, "level": s.level,
            "parent_key": by_id[s.parent_id].key if s.parent_id in by_id else None,
            "footnote_ids": s.footnote_ids,
            "elements": [{"kind": e.kind, "text": e.text[:600]} for e in s.elements],
        }
        for s in doc.sections
    ]


def _summary(doc: ExtractedDoc, elapsed: float) -> dict:
    levels: dict[int, int] = {}
    for s in doc.sections:
        levels[s.level] = levels.get(s.level, 0) + 1
    return {
        "elapsed_s": round(elapsed, 1),
        "sections": len(doc.sections),
        "parts": sum(1 for s in doc.sections if s.level == 0),
        "tables": sum(1 for s in doc.sections for e in s.elements if e.kind == "table"),
        "footnotes": len(doc.footnotes),
        "levels": dict(sorted(levels.items())),
    }


def run_backend(
    path: str, backend: str, product: str,
    mineru_backend: str | None = None, effort: str | None = None,
) -> dict:
    """Extract with ONE backend; return a JSON-safe result dict.

    For the MinerU extractor, `mineru_backend` ("vlm-engine" | "hybrid-engine" |
    "pipeline") and `effort` ("medium" | "high", hybrid only) select the parse
    mode for THIS run, overriding settings.mineru_backend."""
    if backend == "legacy" and Path(path).suffix.lower() != ".docx":
        raise BackendUnavailable(
            "legacy backend reads Word styles — it needs a .docx source; a PDF has none"
        )
    if backend == "mineru" and Path(path).suffix.lower() == ".docx":
        raise BackendUnavailable(
            "MinerU is scoped to formats legacy can't read (PDF, scans, ...) — "
            "a .docx should use the legacy backend instead"
        )
    doc_meta = _doc_meta(path, product)
    t0 = time.monotonic()
    if backend == "mineru":
        from aosphere_core_index.extract.mineru_extract import extract_mineru
        doc = extract_mineru(path, doc_meta=doc_meta, source_key="ab-test",
                             mineru_backend=mineru_backend, effort=effort)
    else:
        doc = extract_document(path, doc_meta=doc_meta, source_key="ab-test", backend=backend)
    elapsed = time.monotonic() - t0
    summary = _summary(doc, elapsed)
    if backend == "mineru":
        mode = mineru_backend or settings.mineru_backend
        summary["mode"] = f"{mode} · {effort}" if (effort and mode.startswith("hybrid")) else mode
    return {
        "backend": backend, "summary": summary,
        "sections": _tree(doc), "footnotes": doc.footnotes,
    }


# --- cached MinerU output: render the chunk tree without re-running the CLI ---
#
# A PDF run of MinerU takes minutes; but every run leaves its content_list.json
# in data/.mineru_cache. These let the A/B UI render the ported chunking of an
# already-parsed document INSTANTLY (no model, no CLI) — the fast way to SEE the
# hierarchy/clause keys the extractor produces.

_CONTENT_SUFFIX = "_content_list.json"


def _cache_root() -> Path:
    return settings.data_dir / ".mineru_cache"


def list_cached() -> list[dict]:
    """Available cached `content_list.json` outputs, newest per (name, backend)."""
    root = _cache_root()
    if not root.exists():
        return []
    best: dict[tuple[str, str], tuple[float, Path]] = {}
    for f in root.rglob(f"*{_CONTENT_SUFFIX}"):
        if "_v2" in f.name:
            continue
        name, backend = f.name[: -len(_CONTENT_SUFFIX)], f.parent.name
        mtime = f.stat().st_mtime
        key = (name, backend)
        if key not in best or mtime > best[key][0]:
            best[key] = (mtime, f)
    items = [{"name": n, "backend": b, "path": str(p)} for (n, b), (_, p) in best.items()]
    items.sort(key=lambda d: (d["name"].lower(), d["backend"]))
    return items


def run_cached(path: str, product: str) -> dict:
    """Build the MinerU chunk tree from a cached content_list.json (no CLI run).
    Same result shape as `run_backend(..., 'mineru', ...)`."""
    from aosphere_core_index.extract.mineru_extract import blocks_to_doc

    p = Path(path).resolve()
    root = _cache_root().resolve()
    if root not in p.parents or not p.name.endswith(_CONTENT_SUFFIX):
        raise BackendUnavailable("not a cached MinerU content_list.json under data/.mineru_cache")

    blocks = json.loads(p.read_text(encoding="utf-8"))
    meta = _doc_meta(str(p.with_name(p.name[: -len(_CONTENT_SUFFIX)] + ".pdf")), product)
    t0 = time.monotonic()
    doc = blocks_to_doc(blocks, doc_meta=meta, source_key="cached")
    elapsed = time.monotonic() - t0
    return {
        "backend": "mineru", "summary": _summary(doc, elapsed),
        "sections": _tree(doc), "footnotes": doc.footnotes,
    }
