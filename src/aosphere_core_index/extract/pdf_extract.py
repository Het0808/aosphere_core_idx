"""PDF text extraction + paragraph-aware chunking for alert attachments.

Runs at BUILD time only: the extracted text is baked into content.json / the
index, so the deployed (offline) container never needs a PDF library.
"""
from __future__ import annotations

import re

_WS = re.compile(r"[ \t]+")
_MULTI_NL = re.compile(r"\n{2,}")
_SENT = re.compile(r"(?<=[.!?])\s+")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
# MinerU renders complex tables as a single-line raw HTML <table> in its
# markdown (verified against a real attachment — one such table ran ~4000
# chars with no internal newlines), not pipe-delimited rows.
_HTML_TABLE_RE = re.compile(r"^\s*<table", re.I)


def extract_pdf_text(path: str) -> str:
    """Full text of a PDF via PyMuPDF, whitespace-normalised, paragraphs kept as
    blank-line-separated blocks. Returns "" if the file can't be read."""
    try:
        import fitz  # PyMuPDF — build-time dependency only
    except ImportError:  # pragma: no cover
        return ""
    try:
        doc = fitz.open(path)
    except Exception:
        return ""
    try:
        pages = [p.get_text() for p in doc]
    finally:
        doc.close()
    text = "\n\n".join(pages)
    # Normalise horizontal whitespace; collapse 3+ newlines to a paragraph break.
    text = _WS.sub(" ", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


def extract_pdf_text_via(path: str, backend: str) -> str:
    """extract_pdf_text() (flat text) for "legacy", or MinerU's own rendered
    markdown (headings/tables preserved) for "mineru" — same string shape
    either way, so every attachment_text consumer is unaffected by the backend."""
    if backend != "mineru":
        return extract_pdf_text(path)
    from aosphere_core_index.config import settings
    from aosphere_core_index.extract.mineru_runner import run_mineru_markdown

    return run_mineru_markdown(
        path, out_dir=settings.data_dir / ".mineru_cache", backend=settings.mineru_backend,
        timeout=settings.mineru_timeout_s, config_path=settings.mineru_config_path,
        model_cache=settings.mineru_model_cache,
    )


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in text.split("\n\n")]
    return [p for p in parts if p]


def _is_markdown_table(text: str) -> bool:
    """A table's rows have no blank line between them, so the paragraph
    splitter above treats the whole table as ONE paragraph — this flags that
    case so chunk_text can exempt it from the hard-split below instead of
    slicing a row (or an HTML tag) mid-way (only MinerU-rendered attachments
    produce these; legacy PyMuPDF text never contains `|`-rows or `<table>`).
    Covers both shapes MinerU renders: pipe-delimited markdown rows, and a
    single-line raw HTML <table> for anything it judges too complex for
    markdown (verified against a real attachment)."""
    if _HTML_TABLE_RE.match(text):
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return len(lines) >= 2 and sum(1 for ln in lines if _TABLE_ROW_RE.match(ln)) / len(lines) > 0.5


def _tail(text: str, n: int) -> str:
    """Up to the last ~n chars of `text`, snapped to a sentence start when possible
    so the overlap reads as whole sentences rather than a mid-word fragment."""
    if len(text) <= n:
        return text
    frag = text[-n:]
    sents = _SENT.split(frag)
    return " ".join(sents[1:]).strip() if len(sents) > 1 else frag.strip()


def chunk_text(text: str, target: int = 1000, overlap: int = 150, max_chunks: int = 10) -> list[str]:
    """Paragraph-aware packing with a small overlap.

    Pack whole paragraphs up to ~`target` chars (never splitting a paragraph unless
    it alone exceeds `target`), and carry a ~`overlap`-char sentence tail from the
    previous chunk into the next so context straddling a boundary is captured in
    both. Bounded to `max_chunks`. This mirrors how the DP memo docs are chunked by
    their own structure — here we respect paragraph boundaries since attachment PDFs
    have no clause hierarchy.
    """
    text = (text or "").strip()
    if not text:
        return []
    paras = _split_paragraphs(text) or [text]
    chunks: list[str] = []
    cur = ""
    for para in paras:
        # A single oversized paragraph: hard-split into target-sized windows w/ overlap.
        if len(para) > target:
            if cur:
                chunks.append(cur.strip())
                cur = ""
            if _is_markdown_table(para):
                # One whole oversized chunk beats a table sliced mid-row.
                chunks.append(para.strip())
                if len(chunks) >= max_chunks:
                    return chunks
                continue
            start = 0
            while start < len(para):
                piece = para[start:start + target]
                pref = _tail(chunks[-1], overlap) if chunks else ""
                chunks.append(f"{pref} {piece}".strip() if pref else piece.strip())
                if len(chunks) >= max_chunks:
                    return chunks
                start += max(1, target - overlap)
            continue
        if cur and len(cur) + 1 + len(para) > target:
            chunks.append(cur.strip())
            if len(chunks) >= max_chunks:
                return chunks
            cur = _tail(chunks[-1], overlap)  # seed next chunk with the overlap tail
        cur = f"{cur} {para}".strip() if cur else para
    if cur and len(chunks) < max_chunks:
        chunks.append(cur.strip())
    return chunks
