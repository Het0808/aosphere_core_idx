"""Extract an aosphere DOCX into a typed section hierarchy.

The document encodes structure in custom DP* styles (see styles.py). We walk the
body in document order, treat DPSectionHead + DPNum1..4 as the clause hierarchy,
and attach body/bullet/question/reader-note/table content to the current section.

Each section gets a synthesized clause key (e.g. "C1.2(a)") aligned to the Part
letters (A..K), so inline references resolve to specific sections.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass

import docx
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml import etree

from aosphere_core_index.extract.model import Element, ExtractedDoc, Section
import statistics
from collections import defaultdict

from aosphere_core_index.extract.styles import (
    CONTENT_KINDS, GENERIC_HEADING_MEDIAN_MAX, GENERIC_HEADING_PARA_MAX,
    GENERIC_HEADING_RE, SKIP_STYLES, generic_heading_level, heading_levels_for,
    is_plausible_title, max_heading_level, numbering_heading_level,
    numbering_promotion_enabled,
)

# Canonical clause references, e.g. C1.2(a), D3, A2.1(b)(i). No trailing \b:
# it would fail after ")" and truncate the (a) suffix.
_CLAUSE_RE = re.compile(r"\b[A-K]\d+(?:\.\d+)*(?:\([a-z]+\))*")
# Leading item marker on DPNum3/4 paragraphs, e.g. "(a)\tEstablishment limb".
_ITEM_MARKER_RE = re.compile(r"^\(([a-z]+)\)")


def iter_block_items(parent: DocxDocument):
    """Yield paragraphs and tables in true document order."""
    for child in parent.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield Table(child, parent)


def _table_to_text(table: Table) -> str:
    """Render a table as a simple pipe-delimited block for readable chunks."""
    rows = []
    for row in table.rows:
        cells = [c.text.strip().replace("\n", " ") for c in row.cells]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def _textbox_text(p: Paragraph) -> str:
    """Text of any text boxes anchored on this paragraph (w:txbxContent).

    The newer survey template puts curated "aosphere summary" callouts in text
    boxes; the body-only walker never visits them, so they were dropped. Skip the
    VML mc:Fallback copy so each box is captured once, not duplicated.
    """
    blocks: list[str] = []
    for tb in p._p.iter(qn("w:txbxContent")):
        if any(etree.QName(a).localname == "Fallback" for a in tb.iterancestors()):
            continue  # AlternateContent fallback duplicate
        lines = []
        for tp in tb.iter(qn("w:p")):
            t = "".join(n.text or "" for n in tp.iter(qn("w:t"))).strip()
            if t:
                lines.append(t)
        if lines:
            blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _para_text_with_footnotes(p: Paragraph) -> tuple[str, list[str]]:
    """Return paragraph text with inline footnote markers and the ids referenced.

    python-docx's .text drops footnote reference markers, so we walk the XML in
    document order and insert a [^id] marker where each reference occurs, keeping
    footnotes anchored to where they apply in the prose.
    """
    parts: list[str] = []
    ids: list[str] = []
    for node in p._p.iter():
        tag = node.tag
        if tag == qn("w:t"):
            parts.append(node.text or "")
        elif tag == qn("w:tab"):
            parts.append("\t")
        elif tag == qn("w:footnoteReference"):
            fid = node.get(qn("w:id"))
            if fid:
                parts.append(f" [^{fid}]")
                ids.append(fid)
    return "".join(parts).strip(), ids


def _extract_footnotes(path: str) -> dict[str, str]:
    """Read word/footnotes.xml and return {footnote_id: text}."""
    with zipfile.ZipFile(path) as z:
        if "word/footnotes.xml" not in z.namelist():
            return {}
        root = etree.fromstring(z.read("word/footnotes.xml"))
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    out: dict[str, str] = {}
    for fn in root.iter(f"{w}footnote"):
        fid = fn.get(f"{w}id")
        if fid is None or int(fid) < 1:  # ids 0/-1 are separators
            continue
        text = "".join(t.text or "" for t in fn.iter(f"{w}t")).strip()
        if text:
            out[fid] = text
    return out


def _part_letter(index: int) -> str:
    return chr(ord("A") + index)


@dataclass
class _Frame:
    section: Section
    child_count: int = 0  # number of direct child sections created so far


def extract_docx(path: str, *, doc_meta: dict, source_key: str) -> ExtractedDoc:
    """Extract a DOCX file into an ExtractedDoc using its metadata record."""
    docx_doc = docx.Document(path)
    footnotes = _extract_footnotes(path)

    product = str(doc_meta.get("RENDERSTYLENAME", ""))
    # Heading detection is style-based and differs per product (RENDERSTYLENAME);
    # unknown products fall back to the Data Privacy map.
    heading_levels = heading_levels_for(product)
    _num_promo = numbering_promotion_enabled(product)
    # Author-specific templates use numbered styles (Level N / Heading N) we don't
    # curate. Classify each such style as a heading by its MEDIAN paragraph length
    # across THIS document (robust to a heading style's occasional long title and to
    # body styles that merely reuse a numbered name). Curated styles always win.
    # Same pass: note whether the doc anchors its Parts via a curated Part style
    # (a level-0 heading style, e.g. DPSectionHead) or via lettered "A." body lines
    # promoted to Parts — used just below to place the generic hierarchy correctly.
    _gen_lens: dict[str, list[int]] = defaultdict(list)
    _has_part_style = False
    _num_letter_parts = 0
    for block in iter_block_items(docx_doc):
        if isinstance(block, Table):
            continue
        st = (block.style.name if block.style else "") or ""
        txt = block.text.strip()
        if not txt:
            continue
        if heading_levels.get(st) == 0:
            _has_part_style = True
        if st not in heading_levels and GENERIC_HEADING_RE.match(st):
            _gen_lens[st].append(len(txt))
        elif (_num_promo and st not in heading_levels
              and len(txt) <= GENERIC_HEADING_PARA_MAX
              and numbering_heading_level(txt) == 0):
            _num_letter_parts += 1
    heading_style_levels = dict(heading_levels)
    for st, lens in _gen_lens.items():
        if statistics.median(lens) <= GENERIC_HEADING_MEDIAN_MAX:
            heading_style_levels[st] = generic_heading_level(st)
    # A generic "Level 1"/"DPLevel 1" style maps to level 0. But when the document
    # already anchors its Parts elsewhere — a curated Part style (DPSectionHead) or
    # lettered "A." lines promoted to Parts — that generic style is really the first
    # SUB-part level, authored 1-based. Left at level 0 it COLLIDES with the Parts:
    # each topic closes the open Part and becomes a spurious sibling ("35 parts",
    # childless Part A — 11 Shareholding jurisdictions). Shift the whole generic
    # hierarchy down one so it nests under the Parts. Templates that start their
    # generic headings at Level 2 (e.g. Spain's DPLevel 2) already nest (min generic
    # level != 0) and are untouched; DP uses curated DPNum* with no generic styles.
    _generic_levels = [lv for st, lv in heading_style_levels.items() if st not in heading_levels]
    _generic_offset = 0
    if (_has_part_style or _num_letter_parts >= 2) and _generic_levels and min(_generic_levels) == 0:
        _generic_offset = 1
        for st in list(heading_style_levels):
            if st not in heading_levels:
                heading_style_levels[st] += 1
    out = ExtractedDoc(
        doc_id=str(doc_meta.get("DOCID", "")),
        title=str(doc_meta.get("DOCNAME") or doc_meta.get("OPINIONNAME") or "Document"),
        jurisdiction=str(doc_meta.get("JURISDICTIONNAME", "")),
        jurisdiction_id=doc_meta.get("JURISDICTIONID"),
        opinion_id=doc_meta.get("OPINIONID"),
        product=product,
        source_key=source_key,
    )
    out.footnotes = footnotes

    stack: list[_Frame] = []  # open sections by level
    part_index = -1
    seq = 0  # unique id counter
    _max_lvl = max_heading_level(product)

    def current() -> Section | None:
        return stack[-1].section if stack else None

    for block in iter_block_items(docx_doc):
        if isinstance(block, Table):
            sec = current()
            if sec is not None:
                text = _table_to_text(block)
                if text.strip():
                    sec.elements.append(Element(kind="table", text=text))
            continue

        # Text boxes (curated "aosphere summary" callouts) are anchored on a
        # paragraph that may itself be empty/skip-styled — capture them first,
        # before any skip, and attach to the current section.
        tb_text = _textbox_text(block)
        if tb_text:
            sec = current()
            if sec is not None:
                sec.elements.append(Element(kind="summary", text=tb_text))

        style = (block.style.name if block.style else "") or ""
        text = block.text.strip()
        # Skip curated front-matter styles plus ANY toc-named style — templates use
        # custom TOC style names ("TOC2_1") beyond the standard "toc 1/2/3".
        if not text or style in SKIP_STYLES or style.lower().startswith("toc"):
            continue

        level = heading_style_levels.get(style)
        # A GENERIC heading style is a per-style median call — an individual long,
        # sentence-shaped paragraph in it is body the author mis-styled, not a
        # title. Demote it (curated DPSectionHead/DPNum* styles stay trusted).
        if level is not None and style not in heading_levels and not is_plausible_title(text):
            level = None
        # Mixed numbered style (not a heading style overall): still promote its
        # individually short paragraphs to headings.
        if level is None and len(text) <= GENERIC_HEADING_PARA_MAX:
            level = generic_heading_level(style)
            if level is not None:
                level += _generic_offset  # nest generic headings under anchored Parts
                if not is_plausible_title(text):
                    level = None
        # Body-styled heading kept only by a strong structural numbering prefix
        # ("A. …", "3.4.17 …"). Disabled for products whose curated styles already
        # encode the full hierarchy (their numbered body lines are list items).
        if level is None and _num_promo and len(text) <= GENERIC_HEADING_PARA_MAX:
            level = numbering_heading_level(text)
        # Cap nesting depth: a heading whose effective level would exceed the product
        # cap, and isn't a paren sub-item ("(a)"), is a deep list-item styled as a
        # heading — demote to body so its content stays on the citable mid-level clause
        # (Shareholding Disclosure over-nests to L5-L12; gold cites <= L3). Peek the
        # parent depth without mutating the stack.
        if level is not None and level > 0 and not _ITEM_MARKER_RE.match(text):
            p_lvl = next((f.section.level for f in reversed(stack) if f.section.level < level), None)
            eff = 0 if p_lvl is None else min(level, p_lvl + 1)
            if eff > _max_lvl:
                level = None
        if level is not None:
            # Close any open sections at this level or deeper.
            while stack and stack[-1].section.level >= level:
                stack.pop()
            parent = stack[-1] if stack else None
            # A generic-heuristic heading can be an orphan (deeper level with no
            # ancestor) or skip levels. Clamp so it always has a valid parent: no
            # ancestor -> promote to a part (level 0); otherwise attach directly
            # under the nearest open section.
            if level > 0 and parent is None:
                level = 0
            elif parent is not None and level > parent.section.level + 1:
                level = parent.section.level + 1
            seq += 1

            if level == 0:
                title = text
                if title.lower().startswith("annex") or title.lower() == "glossary":
                    letter = None
                    key = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                else:
                    part_index += 1
                    letter = _part_letter(part_index)
                    key = letter
            else:
                parent.child_count += 1
                n = parent.child_count
                p_letter = parent.section.part_letter
                marker = _ITEM_MARKER_RE.match(text)
                if level >= 3 and marker:
                    key = f"{parent.section.key}({marker.group(1)})"
                    title = _ITEM_MARKER_RE.sub("", text).strip()
                elif level == 1:
                    key = f"{p_letter}{n}" if p_letter else f"{parent.section.key}.{n}"
                    title = text
                else:
                    key = f"{parent.section.key}.{n}"
                    title = text
                letter = p_letter

            section = Section(
                id=f"sec-{seq}",
                key=key,
                title=title,
                level=level,
                parent_id=parent.section.id if parent else None,
                part_letter=letter,
            )
            out.sections.append(section)
            stack.append(_Frame(section))
            continue

        kind = CONTENT_KINDS.get(style)
        if kind is None:
            kind = "body"  # unknown styled text still counts as body content
        sec = current()
        if sec is None:
            continue  # content before the first heading (front matter) -> skip
        ctext, fids = _para_text_with_footnotes(block)
        refs = sorted(set(_CLAUSE_RE.findall(ctext)))
        sec.elements.append(Element(kind=kind, text=ctext or text, clause_refs=refs))
        for fid in fids:
            if fid not in sec.footnote_ids:
                sec.footnote_ids.append(fid)

    return out
