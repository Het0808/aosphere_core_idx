"""Render an ExtractedDoc as structured markdown.

Headings reflect the clause hierarchy; each section shows its clause key so the
structure (and how it would be navigated) is visible at a glance.
"""

from __future__ import annotations

from aosphere_core_index.extract.model import ExtractedDoc, Section

_KIND_PREFIX = {"bullet": "- ", "question": "> **Q:** ", "readernote": "> _", "table": ""}


def _render_element_text(kind: str, text: str) -> str:
    if kind == "bullet":
        return f"- {text}"
    if kind == "question":
        return f"**Q.** {text}"
    if kind == "readernote":
        return f"> _{text}_"
    if kind == "table":
        return f"```\n{text}\n```"
    return text


def section_to_markdown(s: Section) -> str:
    hashes = "#" * min(s.level + 1, 6)
    head = f"{hashes} [{s.key}] {s.title}" if s.part_letter or s.level else f"{hashes} {s.title}"
    lines = [head]
    for el in s.elements:
        lines.append(_render_element_text(el.kind, el.text))
    return "\n\n".join(lines)


def doc_to_markdown(doc: ExtractedDoc) -> str:
    lines = [
        f"# {doc.title}",
        f"_{doc.jurisdiction} · {doc.product} · DOCID {doc.doc_id} · "
        f"Opinion {doc.opinion_id}_",
    ]
    for s in doc.sections:
        lines.append(section_to_markdown(s))
    if doc.footnotes:
        lines.append("## Footnotes")
        for fid, text in sorted(doc.footnotes.items(), key=lambda kv: int(kv[0])):
            lines.append(f"[^{fid}]: {text}")
    return "\n\n".join(lines)
