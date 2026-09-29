# Vendored verbatim from "MinerU2.5" (Extraction Research), backend/office_footnotes.py,
# commit dc47d6148d351b6627d1dca8f6927c885a9f4446 (2026-07-10). Do not hand-edit —
# fix upstream and re-vendor. aosphere-side integration: extract/mineru_adapter.py.

"""Recovers footnotes from a .docx source file that MinerU's office-document
backend drops entirely.

MinerU's docx path extracts body content only — confirmed empirically: a real
57-section legal memo with 43 footnotes in its own `word/footnotes.xml` part
produces zero `footnote`/`page_footnote`-typed blocks in `content_list.json`,
and the footnote reference marks vanish from the extracted paragraph text too
(no superscript, no marker of any kind). There is nothing in MinerU's own
output to recover from — this module goes back to the original .docx file.

Page-scoped broadcast (how PDF footnotes work, see hierarchy.FOOTNOTE_TYPES /
_attach_by_page) doesn't translate here either: MinerU's docx path doesn't
paginate at all — the same real document has 967 of its 970 text blocks all
sharing `page_idx: 1` (Word documents are reflowable; there's no fixed page
until one is actually printed).

The natural docx analog of "page" is the section a footnote is actually
referenced within. Two independent style/marker heuristics for
re-detecting docx headings from scratch were tried and both proved
unreliable on a real document:

  - Word's own paragraph *styles* aren't a clean signal: this document's
    Table-of-Contents field only bookmarks its shallow heading levels
    (roughly Level1-3), so deeper heading styles (Level4+) never surface as
    "confirmed" heading styles, and some of those same deeper styles are
    also reused for non-heading lead-in sentences — either way, a footnote's
    real (deep) enclosing heading gets missed and it falls back to whatever
    shallower ancestor heading was last seen.
  - Word's auto-numbering ("1.2.1.1") is usually rendered by the paragraph's
    list-numbering definition, not literal text — the raw text for that
    heading is just "Regulated markets". MinerU computes and prepends the
    equivalent number itself, so heading text from the two sources doesn't
    even match verbatim.

Instead of re-deriving "is this a heading" independently, this trusts
MinerU's own (already correct, already-numbered) heading list — the same
`text_level`-tagged blocks hierarchy.py's real chunk tree is built from — and
just *aligns* it against the docx's own paragraph stream in order: walk both
sequences together, and for each MinerU heading (in its own order) scan
forward through not-yet-consumed paragraphs for the best fuzzy match. Because
both sequences share the same underlying document order, this alignment is
monotonic — each heading is only searched for *after* the previous one was
found — which rules out matching the wrong occurrence of similar wording
elsewhere in the document, the failure mode a global (unordered) fuzzy search
would risk.

Once every MinerU heading block has a corresponding docx paragraph position,
finding "which heading is a given footnote reference under" is just walking
paragraphs in order and tracking the most recent aligned heading — then a
synthetic footnote block is spliced in right after that heading block, with
no `page_idx`, so hierarchy.build_chunks attaches it to that exact chunk
instead of page-broadcasting (see the `stack[-1][1].footnotes.append(...)`
branch there; real PDF footnote blocks always carry a real page_idx, so that
path is untouched).
"""

from __future__ import annotations

import difflib
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Word reserves footnote ids -1 and 0 for its built-in separator/continuation
# marks — never real footnote content.
_RESERVED_IDS = {None, "-1", "0"}

# Heading-to-paragraph alignment should be a strong match even accounting for
# MinerU's computed numbering prefix not appearing in the raw docx text; the
# body-paragraph fallback is inherently noisier, so it keeps a looser floor.
_MIN_HEADING_MATCH_RATIO = 0.4
_MIN_FALLBACK_MATCH_RATIO = 0.3
_EARLY_STOP_RATIO = 0.92

_BOLD_MD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_HTML_EMPHASIS_RE = re.compile(r"</?(?:strong|em)[^>]*>", re.IGNORECASE)


def _clean_heading_text(text: str) -> str:
    """Mirrors hierarchy._strip_heading_markdown so docx heading text and
    MinerU's already-cleaned heading text compare on equal footing."""
    text = _HTML_EMPHASIS_RE.sub("", text)
    return _BOLD_MD_RE.sub(lambda m: m.group(1) or m.group(2), text).strip()


def _footnote_texts(docx_path: Path) -> dict[str, str]:
    """footnote id -> body text, straight from word/footnotes.xml."""
    with zipfile.ZipFile(docx_path) as z:
        if "word/footnotes.xml" not in z.namelist():
            return {}
        xml_bytes = z.read("word/footnotes.xml")

    root = ET.fromstring(xml_bytes)
    texts: dict[str, str] = {}
    for fn in root.findall(f"{_W}footnote"):
        fn_id = fn.get(f"{_W}id")
        if fn_id in _RESERVED_IDS:
            continue
        text = "".join(node.text or "" for node in fn.iter(f"{_W}t")).strip()
        if text:
            texts[fn_id] = text
    return texts


def _walk_paragraphs(docx_path: Path) -> list[dict]:
    """Every paragraph in document order: its text and which footnotes (if
    any) it references."""
    with zipfile.ZipFile(docx_path) as z:
        xml_bytes = z.read("word/document.xml")
    root = ET.fromstring(xml_bytes)

    paragraphs = []
    for p in root.iter(f"{_W}p"):
        text = "".join(node.text or "" for node in p.iter(f"{_W}t")).strip()
        ref_ids = [
            ref.get(f"{_W}id")
            for ref in p.iter(f"{_W}footnoteReference")
            if ref.get(f"{_W}id") not in _RESERVED_IDS
        ]
        paragraphs.append({"text": text, "footnote_ids": ref_ids})
    return paragraphs


def _align_headings_to_paragraphs(
    heading_blocks: list[tuple[int, str]], paragraphs: list[dict]
) -> dict[int, int]:
    """{content_block_index: paragraph_index}, found by scanning the docx's
    paragraph stream forward (never backward) for each MinerU heading in
    turn — see module docstring for why this has to be order-preserving
    rather than an unordered fuzzy search."""
    alignment: dict[int, int] = {}
    para_pointer = 0
    for block_idx, heading_text in heading_blocks:
        if not heading_text:
            continue
        best_para_idx, best_ratio = None, 0.0
        for pi in range(para_pointer, len(paragraphs)):
            para_text = _clean_heading_text(paragraphs[pi]["text"])
            if not para_text:
                continue
            ratio = difflib.SequenceMatcher(None, heading_text, para_text).quick_ratio()
            if ratio > best_ratio:
                best_ratio, best_para_idx = ratio, pi
            if ratio >= _EARLY_STOP_RATIO:
                break
        if best_para_idx is not None and best_ratio >= _MIN_HEADING_MATCH_RATIO:
            alignment[block_idx] = best_para_idx
            para_pointer = best_para_idx + 1
    return alignment


def _heading_block_per_footnote(paragraphs: list[dict], alignment: dict[int, int]) -> dict[str, int]:
    """footnote_id -> the MinerU content_block index of the nearest
    preceding aligned heading."""
    para_to_block = {para_idx: block_idx for block_idx, para_idx in alignment.items()}
    result: dict[str, int] = {}
    current_block_idx: int | None = None
    for pi, p in enumerate(paragraphs):
        if pi in para_to_block:
            current_block_idx = para_to_block[pi]
        if current_block_idx is None:
            continue
        for fn_id in p["footnote_ids"]:
            result.setdefault(fn_id, current_block_idx)
    return result


def build_blocks_with_footnotes(docx_path: Path, content_blocks: list[dict]) -> list[dict] | None:
    """Returns a copy of `content_blocks` with a synthetic, page_idx-less
    `footnote` block spliced in immediately after the MinerU heading block
    each real footnote falls under (aligning MinerU's heading list to the
    docx's own paragraph order — see module docstring), falling back to
    fuzzy-matching the footnote's own paragraph against any text block if no
    heading covers it. Returns None if the source has no footnotes, or
    nothing usable to match against — never raises, since this is a
    best-effort layer on an already-successful extraction.
    """
    try:
        footnote_texts = _footnote_texts(docx_path)
        if not footnote_texts:
            return None
        paragraphs = _walk_paragraphs(docx_path)
    except Exception:
        return None

    heading_blocks = [
        (i, _clean_heading_text(b.get("text") or ""))
        for i, b in enumerate(content_blocks)
        if b.get("type") == "text" and b.get("text_level") and (b.get("text") or "").strip()
    ]
    alignment = _align_headings_to_paragraphs(heading_blocks, paragraphs)
    heading_block_for_footnote = _heading_block_per_footnote(paragraphs, alignment)

    body_candidates = [
        (i, b.get("text") or "")
        for i, b in enumerate(content_blocks)
        if b.get("type") == "text" and (b.get("text") or "").strip()
    ]
    para_text_for_footnote: dict[str, str] = {}
    for p in paragraphs:
        for fn_id in p["footnote_ids"]:
            if fn_id not in para_text_for_footnote and p["text"]:
                para_text_for_footnote[fn_id] = p["text"]

    insertions: dict[int, list[dict]] = {}
    for fn_id, text in footnote_texts.items():
        idx = heading_block_for_footnote.get(fn_id)
        if idx is None:
            para_text = para_text_for_footnote.get(fn_id, "")
            best_idx, best_ratio = None, 0.0
            for i, block_text in body_candidates:
                ratio = difflib.SequenceMatcher(None, para_text, block_text).quick_ratio()
                if ratio > best_ratio:
                    best_ratio, best_idx = ratio, i
            idx = best_idx if best_ratio >= _MIN_FALLBACK_MATCH_RATIO else None
        if idx is None:
            continue
        insertions.setdefault(idx, []).append({"type": "footnote", "text": text})

    if not insertions:
        return None

    merged: list[dict] = []
    for i, block in enumerate(content_blocks):
        merged.append(block)
        merged.extend(insertions.get(i, ()))
    return merged
