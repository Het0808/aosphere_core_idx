"""Generic document chunker + schema.org knowledge-graph exporter.

Turns a MinerU content_list.json (a flat, reading-order block list) into one
.md file per heading section plus a knowledge_graph.json manifest. This has NO
aosphere-specific concepts (clause keys, Part letters) — it's a standalone,
general-purpose chunker over MinerU's raw output (contrast the aosphere
clause-hierarchy path in mineru_extract.py / mineru_chunking.py).

Design notes / interpretive calls (the source spec leaves a few things open
to implementation judgement):
- Every heading candidate — a `text`-type block tagged with MinerU's own
  `text_level`, AND a plain paragraph/list-item with a recognized numbering
  marker — goes through `detect_marker()` (heading_markers.py) FIRST. A
  detected marker wins over `text_level`: empirically, MinerU tags nearly
  everything heading-shaped as the same text_level regardless of true nesting
  (see docs/MINERU_MIGRATION.md), so it's only trusted as a last resort for
  genuinely un-numbered headings ("APPENDIX 1") that no marker matches.
- A marker's level comes from `marker_level()` (heading_markers.py): each
  family nests relative to the nearest ancestor
  of a strictly higher-order family (upper_alpha < decimal < paren), not
  whatever merely happens to be deepest. This is what makes "A." -> "1." ->
  "1.1" -> "(i)" nest one level at a time, while a lone "1. Methodology" with
  no enclosing "A." still starts at the top instead of being pushed down for
  no reason, and what stops a later "B." from nesting under whatever
  deeply-nested paren-item happened to be open last.
- Roman-vs-alpha ambiguity for single-letter (i)/(v)/(x) markers is scoped to
  ONE list block's own numbering run (reset per list), not the whole document.
- MinerU's `image`/`code` block field names aren't confirmed against a real
  sample (our test corpus only produced text/table/list/page-furniture
  blocks) — `_render_block` checks a couple of plausible field names and
  always degrades to raw text rather than dropping content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from aosphere_core_index.extract.heading_markers import (
    TOC_TAIL_RE,
    Marker,
    detect_marker,
    list_has_roman_evidence,
    marker_level,
    split_list_items,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FOOTNOTE_TYPES = {"page_footnote", "footnote"}
_PAGE_NOTE_TYPES = {"header", "footer", "page_number"}


# ---- chunk tree --------------------------------------------------------

@dataclass
class Chunk:
    order: int
    level: int
    title: str
    marker_family: str | None = None
    parent: Chunk | None = None
    children: list[Chunk] = field(default_factory=list)
    own_blocks: list[dict] = field(default_factory=list)
    pages: set[int] = field(default_factory=set)
    footnotes: list[str] = field(default_factory=list)
    page_notes: list[str] = field(default_factory=list)
    footnote_types: set[str] = field(default_factory=set)
    page_note_types: set[str] = field(default_factory=set)


def _build_chunks(blocks: list[dict], doc_id: str) -> list[Chunk]:
    """One pass in reading order: open a new chunk per heading, attach
    everything else to the deepest open chunk. Skipped levels are tolerated —
    popping while the open top's level >= the new level always leaves the
    nearest real ancestor, however deep the gap."""
    root = Chunk(order=0, level=0, title=doc_id)
    chunks = [root]
    stack = [root]
    order = 1

    def open_chunk(title: str, level: int, page: int, marker_family: str | None) -> None:
        nonlocal order
        while len(stack) > 1 and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1]
        c = Chunk(order=order, level=level, title=title, marker_family=marker_family, parent=parent)
        c.pages.add(page)
        parent.children.append(c)
        chunks.append(c)
        stack.append(c)
        order += 1

    def promote(text: str, marker: Marker, page: int) -> None:
        # root_level=0: the synthetic root frame is always in `stack`, at
        # level 0 — passed explicitly to match marker_level()'s contract
        # rather than relying on its default.
        level = marker_level(marker, [(f.level, f.marker_family) for f in stack], root_level=0)
        open_chunk(text, level, page, marker.family)

    for block in blocks:
        btype = block.get("type", "text")
        page = block.get("page_idx", 0)

        if btype in _FOOTNOTE_TYPES or btype in _PAGE_NOTE_TYPES:
            continue  # attached globally by page in _attach_page_scoped

        if btype == "list":
            items = split_list_items(block)
            roman_open = list_has_roman_evidence(items)  # scoped to this list's own numbering run
            for item in items:
                marker = detect_marker(
                    item, roman_open=roman_open, parent_is_alpha=stack[-1].marker_family == "paren_alpha",
                )
                if marker is None:
                    stack[-1].own_blocks.append({"type": "list", "text": item, "page_idx": page})
                    stack[-1].pages.add(page)
                    continue
                roman_open = roman_open or marker.family == "paren_roman"
                promote(item, marker, page)
            continue

        text_level = block.get("text_level")
        text = (block.get("text") or "").strip()
        # MinerU sometimes tags a TOC line with text_level directly (not just
        # inside a list block) — e.g. "A. Substantial Shareholding 3". The same
        # trailing-page-number guard used for list-item markers applies here too.
        if btype == "text" and text_level and text and not TOC_TAIL_RE.search(text):
            # A recognized marker ("A.", "1.1") is a more reliable depth signal
            # than MinerU's own text_level, which — empirically — tags nearly
            # everything heading-shaped as the same level regardless of true
            # nesting (see docs/MINERU_MIGRATION.md). Only fall back to the raw
            # text_level for genuinely un-numbered headings ("APPENDIX 1").
            marker = detect_marker(
                text, roman_open=False, parent_is_alpha=stack[-1].marker_family == "paren_alpha",
            )
            if marker is not None:
                promote(text, marker, page)
            else:
                open_chunk(text, int(text_level), page, None)
            continue

        stack[-1].own_blocks.append(block)
        stack[-1].pages.add(page)

    return chunks


def _attach_page_scoped(chunks: list[Chunk], blocks: list[dict]) -> None:
    """Footnotes/page-furniture attach to EVERY chunk sharing their page, not
    just whichever chunk was open in reading order — so a chunk retrieved
    standalone still carries everything relevant to interpreting it."""
    page_to_chunks: dict[int, list[Chunk]] = {}
    for c in chunks:
        for p in c.pages:
            page_to_chunks.setdefault(p, []).append(c)

    for block in blocks:
        btype = block.get("type", "text")
        text = (block.get("text") or "").strip()
        if not text or (btype not in _FOOTNOTE_TYPES and btype not in _PAGE_NOTE_TYPES):
            continue
        page = block.get("page_idx", 0)
        for c in page_to_chunks.get(page, []):
            if btype in _FOOTNOTE_TYPES:
                c.footnotes.append(text)
                c.footnote_types.add(btype)
            else:
                c.page_notes.append(text)
                c.page_note_types.add(btype)


# ---- rendering by block type --------------------------------------------

def _render_block(block: dict) -> str:
    btype = block.get("type", "text")
    text = (block.get("text") or "").strip()

    if btype == "list":
        return f"- {text}"
    if btype == "table":
        parts = [f"**{c}**" for c in (block.get("table_caption") or [])]
        parts.append(block.get("table_body", ""))
        parts += [f"*{f}*" for f in (block.get("table_footnote") or [])]
        return "\n\n".join(p for p in parts if p)
    if btype in ("image", "chart"):
        caption = block.get("image_caption") or block.get("img_caption") or ""
        caption = " ".join(caption) if isinstance(caption, list) else caption
        notes = block.get("image_footnote") or []
        notes = notes if isinstance(notes, list) else [notes]
        parts = [f"![{caption}]({block.get('img_path', '')})"]
        parts += [f"*{n}*" for n in notes if n]
        return "\n\n".join(p for p in parts if p)
    if btype == "equation":
        return f"$$\n{text}\n$$"
    if btype == "code":
        parts = [f"**{c}**" for c in (block.get("code_caption") or [])]
        lang = block.get("sub_type", "")
        lang = "" if lang == "code" else lang
        parts.append(f"```{lang}\n{text}\n```")
        return "\n\n".join(p for p in parts if p)
    return text  # text / footnote / page-note / unrecognized type -> raw text


def _own_text(chunk: Chunk) -> str:
    """Only this chunk's own blocks (no footnotes/page-notes) — used for the
    description, so it reflects what the chunk itself says. Falls back to the
    heading title when a chunk (e.g. a promoted list item) has no body of its
    own, so a chunk never exports with an empty description."""
    rendered = "\n\n".join(t for t in (_render_block(b) for b in chunk.own_blocks) if t)
    return rendered or chunk.title


def _body(chunk: Chunk) -> str:
    text = _own_text(chunk)
    if chunk.footnotes:
        notes = "\n".join(f"- {f}" for f in chunk.footnotes)
        text = f"{text}\n\n**Footnotes**\n\n{notes}"
    return text  # page notes are metadata-only, never appended to the body


def _description(chunk: Chunk, limit: int = 180) -> str:
    own = re.sub(r"\s+", " ", _own_text(chunk)).strip()
    return own if len(own) <= limit else own[:limit].rstrip() + "…"


# ---- knowledge-graph entity ----------------------------------------------

def _breadcrumb(chunk: Chunk) -> list[str]:
    parts, cur = [], chunk
    while cur is not None:
        parts.append(cur.title)
        cur = cur.parent
    return list(reversed(parts))


def _content_types(chunk: Chunk) -> list[str]:
    types = {b.get("type", "text") for b in chunk.own_blocks} | chunk.footnote_types | chunk.page_note_types
    return sorted(types)


def _entity(chunk: Chunk, *, doc_id: str, source_file: str) -> dict:
    pages = sorted(chunk.pages) or [0]
    return {
        "@context": "https://schema.org",
        "@type": ["Document"] if chunk.level == 0 else ["DocumentChunk", "Article"],
        "@id": f"urn:doc:{doc_id}:chunk:{chunk.order}",
        "name": chunk.title,
        "description": _description(chunk),
        "isPartOf": f"urn:doc:{doc_id}",
        "level": chunk.level,
        "order": chunk.order,
        "breadcrumb": _breadcrumb(chunk),
        "parent": f"urn:doc:{doc_id}:chunk:{chunk.parent.order}" if chunk.parent else None,
        "children": [f"urn:doc:{doc_id}:chunk:{c.order}" for c in chunk.children],
        "position": {"pageStart": pages[0], "pageEnd": pages[-1]},
        "sourceFile": source_file,
        "contentTypes": _content_types(chunk),
        "pageFootnotes": list(chunk.footnotes),
        "pageNotes": list(chunk.page_notes),
    }


# ---- file output -----------------------------------------------------

def _slug(title: str, limit: int = 60) -> str:
    s = _SLUG_RE.sub("-", title.lower()).strip("-")
    return (s or "chunk")[:limit].rstrip("-")


def _filename(chunk: Chunk) -> str:
    return f"{chunk.order:04d}_l{chunk.level}_{_slug(chunk.title)}.md"


def _write_md(path: Path, entity: dict, body: str) -> bool:
    """Write one chunk's .md (YAML frontmatter, key order preserved, + body).
    Returns False (no write) if the file is already byte-identical."""
    front = yaml.dump(entity, sort_keys=False, allow_unicode=True, default_flow_style=False)
    content = f"---\n{front}---\n\n{body}\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def export_chunks(blocks: list[dict], *, doc_id: str, source_file: str, out_dir: Path) -> tuple[list[str], int]:
    """Chunk a MinerU content_list.json into per-chunk .md files + a
    knowledge_graph.json manifest under `out_dir`. Idempotent (unchanged
    chunks aren't rewritten) and self-cleaning (files left over from a
    since-renamed/removed chunk are deleted)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = _build_chunks(blocks, doc_id)
    _attach_page_scoped(chunks, blocks)

    entities = [_entity(c, doc_id=doc_id, source_file=source_file) for c in chunks]
    filenames = [_filename(c) for c in chunks]
    for chunk, entity, filename in zip(chunks, entities, filenames):
        _write_md(out_dir / filename, entity, _body(chunk))

    expected = set(filenames)
    removed = 0
    for existing in out_dir.glob("*.md"):
        if existing.name not in expected:
            existing.unlink()
            removed += 1

    manifest = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "position": entity["order"], "item": entity, "file": filename}
            for entity, filename in zip(entities, filenames)
        ],
    }
    (out_dir / "knowledge_graph.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return filenames, removed
