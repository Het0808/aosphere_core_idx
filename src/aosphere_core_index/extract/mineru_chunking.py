"""Hierarchy-aware chunking of a MinerU `content_list.json` block list.

This is a faithful port of the standalone "Document Chunker" research project
(MinerU2.5) — its `backend/hierarchy.py` (heading-tree construction) plus the
block-rendering half of its `backend/kg_entity.py` (`render_block`). It is kept
here as its own module, close to the original structure, so it can be diffed
against that project directly; `mineru_extract.blocks_to_doc` is a thin adapter
that maps the `Chunk` tree this produces onto aosphere's ExtractedDoc model.

MinerU emits a flat, reading-order list of blocks. Each block has a `type`
(text, table, image, equation, code, list, header, footer, page_footnote, ...)
and, for headings, a `text_level` of 1/2/3/... (absent or 0 means body text).
Real MinerU2.5 output frequently reports EVERY heading as `text_level=1`
regardless of its true depth, so `build_chunks` re-derives depth from the
heading's own numbering marker ("A." / "1." / "1.1" / "(a)" / "(i)") rather
than trusting `text_level` alone — see `classify_heading_family`.

NOTE (docx footnotes): the original project also had `office_footnotes.py`,
which recovers real footnotes MinerU's *office* backend drops from `.docx`
files. That is intentionally NOT ported here: in aosphere, `.docx` is always
parsed by the legacy Word-style extractor, never by MinerU (see
extract/mineru_extract.py's scope), so no `.docx` ever reaches this code. The
`page_idx is None` footnote branch that recovery relied on is kept anyway, so
the logic stays identical to the original.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree


# --- heading numbering-scheme detection -------------------------------------

_BOLD_MD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_HTML_EMPHASIS_RE = re.compile(r"</?(?:strong|em)[^>]*>", re.IGNORECASE)


def _strip_heading_markdown(text: str) -> str:
    """MinerU's office-document path (.docx/.pptx/.xlsx) preserves bold/italic
    Word-run formatting as inline emphasis even inside heading text itself —
    either markdown ("1 **Overview**") or, for overlapping bold+italic runs,
    literal "<strong>"/"<em>" HTML tags. The PDF/VLM path never does this. A
    heading's title feeds the tree label / section title, a plain-text label
    rather than rendered markdown/HTML, so both are stripped here."""
    text = _HTML_EMPHASIS_RE.sub("", text)
    return _BOLD_MD_RE.sub(lambda m: m.group(1) or m.group(2), text)


_DECIMAL_RE = re.compile(r"^(\d+(?:\.\d+)*)\b")
_UPPER_ALPHA_RE = re.compile(r"^([A-Z])\.\s")
_PAREN_ALPHA_RE = re.compile(r"^\(([a-zA-Z])\)\s")
_PAREN_ROMAN_RE = re.compile(r"^\(([ivxlcdm]+)\)\s", re.IGNORECASE)
_FULL_ROMAN_RE = re.compile(r"^M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$", re.IGNORECASE)


def classify_heading_family(
    text: str, open_families: list[str], open_alpha_letter: str | None = None
) -> str | None:
    """Identify which numbering scheme a heading uses ("decimal-2", "upper-alpha",
    "paren-alpha", "paren-roman", ...), or None if it carries no recognizable marker.

    Legal/regulatory documents commonly nest multiple schemes: "A." sections
    containing "1." sub-sections containing "1.1" clauses containing "(a)"
    points containing "(i)" sub-points. Decimal depth is self-describing via dot
    count; other schemes are flat markers whose *relative* depth only makes
    sense against `open_families` (the schemes already open on the heading
    stack, outermost first) — see `build_chunks` for how a family's absolute
    level is derived from this classification.

    A single letter in parens ("(i)", "(v)", "(x)"...) is genuinely ambiguous
    between a lettered list and a roman-numeral one. We bias toward roman when
    a roman context is already open, or when we're directly nested under a
    "(a)"-style item — nested roman-under-letter is by far the more common
    real-world pattern (vs. a lettered list actually reaching a 9th item, "i") —
    *unless* `open_alpha_letter` shows this marker is simply the next letter in
    an alpha sequence still open (e.g. "(b)" -> "(c)"): the caller's stack still
    has the previous sibling open at the point this runs (it isn't popped until
    after the new heading's own level is known), so "an alpha item is open"
    alone can't tell a same-level sibling from a genuine parent — checking it's
    literally the next letter can.
    """
    text = text.strip()

    m = _DECIMAL_RE.match(text)
    if m:
        return f"decimal-{m.group(1).count('.') + 1}"

    if _UPPER_ALPHA_RE.match(text):
        return "upper-alpha"

    roman_m = _PAREN_ROMAN_RE.match(text)
    alpha_m = _PAREN_ALPHA_RE.match(text)
    if roman_m and _FULL_ROMAN_RE.match(roman_m.group(1)):
        marker = roman_m.group(1)
        if len(marker) > 1:
            return "paren-roman"  # unambiguous: "ii", "iii", "iv", ... aren't valid single-letter list items
        if open_alpha_letter and ord(marker.lower()) == ord(open_alpha_letter.lower()) + 1:
            return "paren-alpha"  # continues the open alpha sequence — not a nested roman numeral
        if "paren-roman" in open_families or (open_families and open_families[-1] == "paren-alpha"):
            return "paren-roman"
        return "paren-alpha"
    if alpha_m:
        return "paren-alpha"

    return None


# --- grouped-list expansion -------------------------------------------------

def _estimate_item_bbox(bbox: list | None, i: int, n: int) -> list | None:
    """MinerU gives one bbox for an entire list block, not per item. Slice it into
    `n` equal vertical bands as a rough per-item estimate — imperfect (items can
    wrap to different numbers of lines) but far better than every item sharing
    the identical full-list box.
    """
    if not bbox or n <= 0:
        return bbox
    x0, y0, x1, y1 = bbox
    band = (y1 - y0) / n
    return [x0, round(y0 + i * band), x1, round(y0 + (i + 1) * band)]


def expand_list_blocks(blocks: list[dict]) -> list[dict]:
    """Split a MinerU `list` block's flat `list_items` into individual pseudo-blocks.

    MinerU often groups an entire lettered/roman enumeration — "(a) ...",
    "(i) ...", "(ii) ...", "(b) ..." — into ONE `list` block with a flat
    `list_items` array and no per-item position/depth info at all, rather than
    flagging each item as its own heading. Left as one block, that whole
    sub-hierarchy would collapse into undifferentiated body text. Splitting it
    into one pseudo-block per item lets the normal heading-marker detection in
    `build_chunks` run over each item and recover the nesting.

    A corrected list block (`_override_markdown` already set) is left intact —
    the correction replaces its rendering wholesale, so there's nothing to expand.
    """
    expanded: list[dict] = []
    for block in blocks:
        if block.get("type") != "list" or block.get("_override_markdown") is not None:
            expanded.append(block)
            continue
        items = block.get("list_items") or []
        for i, item_text in enumerate(items):
            expanded.append(
                {
                    "type": "text",
                    "text": item_text,
                    "page_idx": block.get("page_idx"),
                    "bbox": _estimate_item_bbox(block.get("bbox"), i, len(items)),
                    "_index": f"{block.get('_index')}:{i}",
                    "_from_list": True,
                }
            )
    return expanded


# --- the heading tree -------------------------------------------------------

# MinerU tags a page-bottom footnote (distinguished from body text by a divider
# rule + smaller font) as its own block type, separate from ordinary "text".
FOOTNOTE_TYPES = {"page_footnote", "footnote"}

# Running page furniture MinerU also tags as its own types: a repeated
# top-of-page banner ("header"), bottom-of-page footer text, and page numbers.
# Like footnotes, these apply to the whole page rather than any one section's
# narrative — unlike footnotes, they're purely repetitive boilerplate, so they
# are tracked separately and kept out of the exported body.
PAGE_NOTE_TYPES = {"header", "footer", "page_number"}

# MinerU's office path tags a Word-style hyperlinked table of contents as its
# own "index" type — jump-links, not prose. It carries no retrieval value
# (every title it links to already exists as a real heading), so it's dropped.
NAV_TYPES = {"index"}


@dataclass
class Chunk:
    order: int
    level: int  # 0 = synthetic document root, 1 = H1, 2 = H2, ...
    title: str
    parent_order: int | None
    children_orders: list[int] = field(default_factory=list)
    blocks: list[dict] = field(default_factory=list)
    footnotes: list[dict] = field(default_factory=list)
    page_notes: list[dict] = field(default_factory=list)  # running headers/footers/page numbers
    heading_block: dict | None = None  # the raw block that produced this chunk (None for the root)
    page_start: int | None = None
    page_end: int | None = None


def _touch_pages(chunk: Chunk, page_idx) -> None:
    if page_idx is None:
        return
    chunk.page_start = page_idx if chunk.page_start is None else min(chunk.page_start, page_idx)
    chunk.page_end = page_idx if chunk.page_end is None else max(chunk.page_end, page_idx)


def _attach_by_page(chunks: list[Chunk], by_page: dict[int, list[dict]], attr: str) -> None:
    """Attach each page's collected blocks to every chunk whose page range covers
    that page — page-level annotations belong to the whole page, not just
    whichever chunk happened to be open when the block appeared in reading order.
    """
    for page_idx, page_blocks in by_page.items():
        if page_idx is None:
            continue
        for chunk in chunks:
            if chunk.page_start is not None and chunk.page_start <= page_idx <= chunk.page_end:
                getattr(chunk, attr).extend(page_blocks)


def _innermost_open_alpha_letter(stack: list[tuple[int, Chunk, str | None]]) -> str | None:
    """The single letter of the innermost currently-open paren-alpha heading —
    the deepest open *marked* ancestor `open_families[-1]` finds (skipping
    unmarked ancestors), but the actual letter, so classify_heading_family can
    tell a same-level sibling ("(b)" -> "(c)") from a nested child ("(a)" -> "(i)").
    """
    for _, chunk, fam in reversed(stack):
        if fam is None:
            continue
        if fam != "paren-alpha":
            return None
        m = _PAREN_ALPHA_RE.match(chunk.title.strip())
        return m.group(1) if m else None
    return None


def build_chunks(blocks: list[dict], doc_title: str = "Document") -> list[Chunk]:
    """Group a flat content_list.json block list into one chunk per heading section.

    A synthetic level-0 root chunk represents the whole document (and holds any
    content that appears before the first heading). Each heading block opens a
    new chunk nested under the deepest currently-open heading whose level is
    less than the new heading's level — this tolerates skipped levels (e.g. an
    H3 with no preceding H2) by attaching directly to the nearest real ancestor.

    Page footnotes and page notes (running headers/footers/page numbers) are
    page-level annotations, not part of any one section's narrative flow —
    rather than attaching each to whichever chunk happens to be open when it's
    encountered in reading order, both go to *every* chunk on that page (see
    _attach_by_page): a footnote printed at the bottom of a page can apply to
    any of the sections sharing that page.

    A footnote block with no `page_idx` at all skips page-broadcast entirely and
    attaches only to whichever single chunk is open at that point in the stream
    (this is the docx-recovery path in the original project; unused here since
    aosphere never routes docx through MinerU, but kept for logic parity).
    """
    root = Chunk(order=0, level=0, title=doc_title, parent_order=None)
    chunks: list[Chunk] = [root]
    stack: list[tuple[int, Chunk, str | None]] = [(0, root, None)]
    footnotes_by_page: dict[int, list[dict]] = {}
    page_notes_by_page: dict[int, list[dict]] = {}

    for block in expand_list_blocks(blocks):
        mineru_level = block.get("text_level") or 0
        is_plain_text = block.get("type") == "text"

        title = (
            _strip_heading_markdown((block.get("_override_markdown") or block.get("text") or "").strip())
            if is_plain_text
            else ""
        )
        open_families = [f for _, _, f in stack if f is not None]
        open_alpha_letter = _innermost_open_alpha_letter(stack)
        family = classify_heading_family(title, open_families, open_alpha_letter) if title else None

        # A saved correction can force a block's classification outright — demote
        # an over-detected heading to body, or promote a missed one. None means
        # no override: fall back to the usual auto-detection below.
        force_heading = block.get("_force_heading")
        if force_heading is not None:
            is_heading = is_plain_text and force_heading
        else:
            # A list item never carries MinerU's own text_level (see
            # expand_list_blocks), so it only becomes a heading when its text
            # unambiguously matches a marker we recognize — otherwise it's just
            # body text under whatever heading is open.
            is_heading = is_plain_text and (mineru_level >= 1 or (block.get("_from_list") and family is not None))

        if is_heading:
            forced_level = block.get("_force_level")
            if forced_level is not None:
                level = forced_level
            elif family is not None and family.startswith("decimal-"):
                # Decimal depth is self-describing (dot count), but must nest under
                # any enclosing *marked* ancestor (e.g. "A." before "1.") while
                # staying independent of unmarked ones (e.g. a plain, unnumbered
                # document title before "1." — those are siblings). Skipping over
                # other decimal entries when searching for that anchor means a
                # jump like "1.1" -> "1.1.1.1" still lands at the depth its own
                # dots say, even with no "1.1.1" in between.
                dot_count = int(family.split("-", 1)[1])
                anchor_level = 0
                for lvl, _, fam in stack:
                    if fam is not None and fam.startswith("decimal"):
                        break  # reached decimal's own lineage — deeper is a sibling branch, not an anchor
                    if fam is not None:
                        anchor_level = lvl
                level = anchor_level + dot_count
            elif family is not None:
                match_level = next((lvl for lvl, _, fam in reversed(stack) if fam == family), None)
                level = match_level if match_level is not None else stack[-1][0] + 1
            elif mineru_level >= 1:
                # No recognizable marker. MinerU2.5 flattens EVERY heading to
                # text_level=1, so the level integer carries no real depth here —
                # trusting it (level = 1) would pop an unmarked sub-heading like a
                # "Aviation" callout clean out of the numbered clause it belongs
                # to AND strip the Part prefix from every clause after it. Nest it
                # directly under the deepest open numbered/marked ancestor instead,
                # so it stays inside its section and consecutive unmarked headings
                # come out as siblings rather than a descending staircase.
                marked_levels = [lvl for lvl, _, fam in stack if fam is not None]
                level = (marked_levels[-1] if marked_levels else 0) + 1
            else:
                # a body block force-promoted to a heading with no marker/explicit
                # level to go on — nest it one level under whatever's open now
                level = stack[-1][0] + 1

            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack else root

            new_chunk = Chunk(
                order=len(chunks),
                level=level,
                title=title or f"Section {len(chunks)}",
                parent_order=parent.order,
                heading_block=block,
            )
            parent.children_orders.append(new_chunk.order)
            chunks.append(new_chunk)
            _touch_pages(new_chunk, block.get("page_idx"))
            stack.append((level, new_chunk, family))
        elif block.get("type") in FOOTNOTE_TYPES:
            if block.get("page_idx") is None:
                stack[-1][1].footnotes.append(block)
            else:
                footnotes_by_page.setdefault(block.get("page_idx"), []).append(block)
        elif block.get("type") in PAGE_NOTE_TYPES:
            page_notes_by_page.setdefault(block.get("page_idx"), []).append(block)
        elif block.get("type") in NAV_TYPES:
            continue
        else:
            current = stack[-1][1]
            current.blocks.append(block)
            _touch_pages(current, block.get("page_idx"))

    _attach_by_page(chunks, footnotes_by_page, "footnotes")
    _attach_by_page(chunks, page_notes_by_page, "page_notes")

    return chunks


# --- block rendering (from the original project's kg_entity.render_block) ----

def _html_table_to_text(html: str) -> str:
    """MinerU `table_body` HTML -> pipe-delimited rows. aosphere renders tables
    as plain pipe text rather than keeping raw HTML (the original project kept
    HTML); this is the one rendering deviation from that project — a content
    typing choice, not a hierarchy one."""
    if not html:
        return ""
    root = etree.HTML(html)
    if root is None:
        return ""
    rows = [
        " | ".join("".join(td.itertext()).strip().replace("\n", " ") for td in tr.iter("td"))
        for tr in root.iter("tr")
    ]
    return "\n".join(r for r in rows if r)


def render_block(block: dict) -> str:
    """One MinerU block -> its markdown/text body. Faithful port of the original
    project's kg_entity.render_block, except a `table`'s body is rendered as
    aosphere pipe-text (see _html_table_to_text) instead of raw HTML."""
    # A saved correction wins outright, regardless of block type.
    override = block.get("_override_markdown")
    if override is not None:
        return override

    btype = block.get("type")

    if btype == "text" or btype in FOOTNOTE_TYPES or btype in PAGE_NOTE_TYPES:
        return (block.get("text") or "").strip()

    if btype == "list":
        items = block.get("list_items") or []
        return "\n".join(f"- {item}" for item in items)

    if btype == "table":
        parts = [f"**{cap}**" for cap in (block.get("table_caption") or [])]
        body = _html_table_to_text(block.get("table_body", "")).strip()
        if body:
            parts.append(body)
        parts += [f"*{fn}*" for fn in (block.get("table_footnote") or [])]
        return "\n\n".join(parts)

    if btype in ("image", "chart"):
        caption = " ".join(block.get("image_caption") or [])
        parts = [f"![{caption}]({block.get('img_path', '')})"]
        parts += [f"*{fn}*" for fn in (block.get("image_footnote") or [])]
        return "\n\n".join(parts)

    if btype == "equation":
        text = (block.get("text") or "").strip()
        return f"$$\n{text}\n$$" if text else ""

    if btype == "code":
        parts = [f"**{cap}**" for cap in (block.get("code_caption") or [])]
        lang = block.get("sub_type") or ""
        lang = "" if lang == "code" else lang
        parts.append(f"```{lang}\n{block.get('code_body', '').strip()}\n```")
        return "\n\n".join(parts)

    # Fallback for any block type we don't explicitly model (MinerU's type set
    # isn't fully documented/stable) — surface its text/list_items rather than
    # silently dropping real content just because the type is unfamiliar.
    if block.get("text"):
        return block["text"].strip()
    if block.get("list_items"):
        return "\n".join(f"- {item}" for item in block["list_items"])
    return ""
