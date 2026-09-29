"""Structure-aware chunking.

Chunks follow the section hierarchy: each section's own content becomes one or
more chunks, never crossing section boundaries. Every chunk carries a
section-path breadcrumb (e.g. "A. Introduction > A1 ... > A1.1 Laws") and full
provenance metadata. The breadcrumb is what we later prepend at embed time
(contextual embeddings) — a quality lever over the competitor's raw-text chunks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aosphere_core_index.extract.model import ExtractedDoc, Section

# Target chunk budget in (estimated) tokens. Sections smaller than this stay whole.
MAX_TOKENS = 512
# ~4 chars per token is a serviceable estimate without a tokenizer dependency.
_CHARS_PER_TOKEN = 4


def est_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass
class Chunk:
    """An embeddable unit of one section's content."""

    id: str
    doc_id: str
    section_id: str
    section_key: str
    breadcrumb: str
    text: str
    token_estimate: int
    clause_refs: list[str] = field(default_factory=list)
    footnote_ids: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _breadcrumbs(doc: ExtractedDoc) -> dict[str, str]:
    """Map each section id to its full title path from the root part."""
    by_id = {s.id: s for s in doc.sections}
    crumbs: dict[str, str] = {}

    def path(s: Section) -> str:
        parts = []
        cur: Section | None = s
        while cur is not None:
            parts.append(f"{cur.key} {cur.title}".strip())
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        return " > ".join(reversed(parts))

    for s in doc.sections:
        crumbs[s.id] = path(s)
    return crumbs


def chunk_document(doc: ExtractedDoc) -> list[Chunk]:
    """Produce structure-aligned chunks for every section with content."""
    crumbs = _breadcrumbs(doc)
    base_meta = {
        "jurisdiction": doc.jurisdiction,
        "jurisdiction_id": doc.jurisdiction_id,
        "opinion_id": doc.opinion_id,
        "product": doc.product,
        "source_key": doc.source_key,
    }
    chunks: list[Chunk] = []

    for s in doc.sections:
        if not s.elements:
            continue
        breadcrumb = crumbs[s.id]
        # Greedily pack elements into <= MAX_TOKENS chunks, never splitting an element.
        buckets: list[list] = [[]]
        running = 0
        for el in s.elements:
            t = est_tokens(el.text)
            if running + t > MAX_TOKENS and buckets[-1]:
                buckets.append([])
                running = 0
            buckets[-1].append(el)
            running += t

        for i, bucket in enumerate(buckets):
            if not bucket:
                continue
            text = "\n\n".join(el.text for el in bucket)
            refs = sorted({r for el in bucket for r in el.clause_refs})
            chunks.append(
                Chunk(
                    id=f"{doc.doc_id}:{s.key}:{i}",
                    doc_id=doc.doc_id,
                    section_id=s.id,
                    section_key=s.key,
                    breadcrumb=breadcrumb,
                    text=text,
                    token_estimate=est_tokens(text),
                    clause_refs=refs,
                    footnote_ids=list(s.footnote_ids) if i == 0 else [],
                    metadata=dict(base_meta, section_key=s.key, level=s.level),
                )
            )
    return chunks
