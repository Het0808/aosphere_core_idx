"""Data model for an extracted document (pre-chunking, pre-embedding)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Element:
    """A unit of section content (prose, bullet, question, reader note, table)."""

    kind: str  # "body" | "bullet" | "question" | "readernote" | "table"
    text: str
    clause_refs: list[str] = field(default_factory=list)


@dataclass
class Section:
    """A node in the document's clause hierarchy.

    key is the synthesized clause key (e.g. "C1.2(a)"); it is what inline
    references like "C1.2(a)" resolve to.
    """

    id: str
    key: str
    title: str
    level: int
    parent_id: str | None
    part_letter: str | None
    elements: list[Element] = field(default_factory=list)
    footnote_ids: list[str] = field(default_factory=list)


@dataclass
class ExtractedDoc:
    """The full extraction result for one source document."""

    doc_id: str
    title: str
    jurisdiction: str
    jurisdiction_id: int | None
    opinion_id: int | None
    product: str
    source_key: str
    sections: list[Section] = field(default_factory=list)  # flat, document order
    footnotes: dict[str, str] = field(default_factory=dict)  # footnote id -> text

    def section_by_key(self) -> dict[str, Section]:
        return {s.key: s for s in self.sections}
