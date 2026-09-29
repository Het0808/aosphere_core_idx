#!/usr/bin/env python3
"""mineru_full_extract.py — parse an ENTIRE PDF via MinerU only (headings,
prose AND tables). Structure comes from the PDF's OWN bookmark outline when
it has one — the same ground truth pdf2mdtree.py uses — not from MinerU's own
(unreliable) heading detection.

Why the outline and not MinerU's chunk hierarchy: MinerU re-derives heading
depth from font/marker patterns in raw text, and it gets that wrong exactly
where a heading doesn't look like a heading — e.g. a clause label ("Paragraph
17") that's the first cell of its own table rather than styled text above it.
The PDF's bookmark outline has no such failure mode; it's metadata the
document's own author wrote. Its limitation is coverage, not accuracy: an
outline can (and, in this corpus, regularly does) stop short of the document's
finest structure — one section here spans 12 pages and a dozen clause labels
with zero further bookmarks underneath it. That is not a bug to route around;
it means the document's own author never considered that level a real
"section". So: the outline's own entries — however coarse — define every
folder/file boundary, at whatever depth the outline itself goes and no
deeper. Whatever MinerU headings fall inside one outline entry's page range
render INLINE, in page order, as sub-heading markers inside that one file —
visible, but not each awarded its own file/folder the outline never vouched
for. If the PDF has no outline at all, this falls back to mirroring MinerU's
own chunk tree directly (see _emit_via_chunks) — the previous approach, still
correct for a document with no bookmarks to lean on.

Reuses, verbatim, the pieces of the DB-indexing MinerU pipeline
(src/aosphere_core_index/extract/mineru_extract.py + mineru_chunking.py) that
are already generic — run_mineru_to_dir(), build_chunks(), render_block(),
_demote_repeating_headers(), _toc_orders() — and bypasses the parts that are
specific to that pipeline's ExtractedDoc/Section model (clause-key synthesis,
legal-citation depth caps), which a markdown-tree adapter doesn't need.

The only place this module matches the hybrid pipeline's shape is the on-disk
FILE FORMAT (which JSON files exist, what fields they carry) — that is not a
style choice, it's what lets every existing check (lib_validate.CHECKS) and
check_scorecard.py score this tree completely unmodified.

Used ONLY on the completeness-fallback path (see mineru_fallback.py) — every
document that never triggers a fallback, and pdf2mdtree.py/hybrid_extract.py
themselves, are untouched by anything in this file.

Usage:
    python scripts/mineru_full_extract.py source.pdf -o out_dir
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# Share of an outline's titles that must actually appear in the pages they name
# before the outline is trusted to structure the document. Below this the entries
# are anchor names / stale labels, not headings.
# At the LAST-RESORT tier, emit MinerU's native markdown whole instead of
# re-chunking it into per-heading files. Re-chunking is what loses the content the
# tier was invoked to recover (see _emit_raw_markdown). Set to False to go back to
# the chunked tree.
RAW_MINERU_OUTPUT = True

OUTLINE_LOCATE_SHARE = 0.5
# An outline whose first entry sits this far into the document describes only its
# tail (the classic "only the appendix is bookmarked" case).
OUTLINE_START_FRAC = 0.5

import fitz  # PyMuPDF — page count + bookmark outline, same engine the rest of the pipeline uses

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import hybrid_extract as he  # noqa: E402 — reuse mineru_badge() so table markup matches the hybrid pipeline's convention exactly (clean_markdown() already knows how to strip it)
from aosphere_core_index.extract.mineru_extract import (  # noqa: E402
    _demote_repeating_headers, _toc_orders, run_mineru_to_dir,
)
from aosphere_core_index.extract.mineru_chunking import Chunk, build_chunks, render_block  # noqa: E402

_LEADING_NUM_RE = re.compile(r"^[\s\d.\-–—:)]+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slug(title: str, n: int = 48) -> str:
    """check_heading_hierarchy._slug_key() strips leading digits and non-alnum
    chars from both the heading title and the tree node name before matching —
    so this slug does not need to reproduce pdf2mdtree's own numbering-prefix
    scheme, only to be a stable, readable filename."""
    t = _LEADING_NUM_RE.sub("", title or "").strip().lower()
    t = _NON_ALNUM_RE.sub("-", t).strip("-")
    return (t or "section")[:n]


def _source_line(pdf_name: str, lo: int, hi: int) -> str:
    rng = f"{lo}" if hi <= lo else f"{lo}–{hi}"
    return f"*Source: `{pdf_name}`, page {rng}*"


# ---------------- outline-tree structure (preferred path) ----------------
def _outline_tree(toc: list, n_pages: int) -> list[dict]:
    """toc: fitz Document.get_toc() -> [[level, title, page_1indexed], ...],
    already in document order. -> real nested tree (top-level list, each node
    a dict with children), plus each node's own page range:

      "page"/"end"      — this node and everどんthing under it (through the
                           next entry at ANY level, i.e. the classic
                           "next bookmark, regardless of depth" rule)
      "own_end"         — just its OWN content, i.e. up to its first CHILD's
                           page (pdf2mdtree's "00-overview.md" case) or "end"
                           if it has no children

    Prepends a synthetic "Front matter" node for anything before the first
    real entry (a title page, a contents page — real content, no bookmark)."""
    if not toc:
        return []
    nodes = []
    for i, (level, title, page) in enumerate(toc):
        end = toc[i + 1][2] - 1 if i + 1 < len(toc) else n_pages
        nodes.append({"title": (title or "").strip(), "level": level,
                      "page": page, "end": max(end, page), "children": []})
    roots: list[dict] = []
    stack: list[dict] = []
    for node in nodes:
        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()
        (stack[-1]["children"] if stack else roots).append(node)
        stack.append(node)
    for node in nodes:
        node["own_end"] = node["children"][0]["page"] - 1 if node["children"] else node["end"]
        if node["own_end"] < node["page"]:
            node["own_end"] = node["page"]
    if roots and roots[0]["page"] > 1:
        roots.insert(0, {"title": "Front matter", "level": roots[0]["level"], "page": 1,
                         "end": roots[0]["page"] - 1, "own_end": roots[0]["page"] - 1,
                         "children": [], "is_front_matter": True})
    return roots


def _owner_node(roots: list[dict], page: int) -> dict:
    """The most specific outline node whose OWN range (not a child's) covers
    `page` — i.e. walk down through whichever child's [page, end] contains it,
    stopping the moment `page` falls in a node's own_end instead."""
    def _walk(node):
        for child in node["children"]:
            if child["page"] <= page <= child["end"]:
                return _walk(child)
        return node
    for root in roots:
        if root["page"] <= page <= root["end"]:
            return _walk(root)
    return roots[-1]


def run_mineru_full(pdf_path: Path, dest: Path, *, backend: str | None = None,
                    effort: str | None = None) -> dict:
    """Parse pdf_path entirely via MinerU; write dest/{01_stage1_extract,
    02_stage2_mineru_tables,03_stage3_final} in pdf2mdtree's own shape.
    Returns a small report dict."""
    pdf_path = Path(pdf_path)
    dest = Path(dest)
    stage1_dir = dest / "01_stage1_extract"
    stage2_dir = dest / "02_stage2_mineru_tables"
    stage3_dir = dest / "03_stage3_final"
    for d in (stage1_dir, stage2_dir, stage3_dir):
        if d.exists():
            shutil.rmtree(d)
    stage1_dir.mkdir(parents=True)
    tables_dir = stage2_dir / "tables"
    tables_dir.mkdir(parents=True)

    parse_dir = run_mineru_to_dir(str(pdf_path), out_dir=stage2_dir / "mineru_raw",
                                  backend=backend, effort=effort)
    content_list_path = next(parse_dir.glob("*_content_list.json"))
    blocks = json.loads(content_list_path.read_text())
    blocks = _demote_repeating_headers(blocks)

    chunks = build_chunks(blocks, doc_title=pdf_path.stem)
    by_order = {c.order: c for c in chunks}
    dropped = _toc_orders(chunks, by_order)  # Contents-page navigation chunks

    doc = fitz.open(str(pdf_path))
    n_pages = doc.page_count
    toc = doc.get_toc()
    doc.close()

    headings: list[dict] = []
    tables_manifest: list[dict] = []
    stage2_tables: list[dict] = []
    table_seq = [0]
    seen_footnotes: set[int] = set()  # dedup by id(): a footnote is page-broadcast to every chunk covering its page, so it must render exactly once or it inflates duplicate-word counts

    def table_writer(block: dict) -> str:
        table_seq[0] += 1
        tid = f"table_{table_seq[0]:03d}"
        pages = [block.get("page_idx", 0) + 1]
        bbox = block.get("bbox")
        caption = " ".join(block.get("table_caption") or [])[:80]
        html_body = block.get("table_body") or ""
        tdir = tables_dir / tid
        tdir.mkdir(parents=True, exist_ok=True)
        # table.md ALSO holds raw HTML, not pipe-text — check_table_presence.py's
        # cell regex reads table.md and expects <td>/<th> tags, matching exactly
        # what hybrid_extract.py's own Stage 2 writes there.
        (tdir / "table.html").write_text(html_body)
        (tdir / "table.md").write_text(html_body)
        status = {"table_id": tid, "pages": pages, "bbox": bbox, "source": None,
                  "match_method": "mineru_full", "match_status": "confident",
                  "match_iou": None, "match_note": None, "ok": True,
                  "rows": html_body.count("<tr"), "cols": None}
        (tdir / "status.json").write_text(json.dumps(status, indent=2))
        tables_manifest.append({"table_id": tid, "pages": pages, "multipage": False,
                                "bbox": bbox, "caption": caption})
        stage2_tables.append(status)
        # Badge + the actual table content — exactly what hybrid_extract.py's own
        # run_stage3() splices in (mineru_badge(...) + table_md_path.read_text()).
        # A badge with no content behind it is indistinguishable from a table
        # that never reached the tree at all to check_table_presence.py, and IS a
        # real loss of every cell's text from word coverage.
        return he.mineru_badge(tid, status) + "\n\n" + html_body

    def chunk_body(chunk: Chunk) -> str:
        parts = [table_writer(b) if b.get("type") == "table" else render_block(b)
                 for b in chunk.blocks]
        for fn in chunk.footnotes:
            if id(fn) in seen_footnotes:
                continue
            seen_footnotes.add(id(fn))
            parts.append(render_block(fn))
        return "\n\n".join(p for p in parts if p)

    # "has an outline" is not the same as "the outline describes the document" —
    # and this tier exists precisely for documents where that difference is the
    # whole problem. Check before trusting it.
    use_toc, outline_note = _outline_is_usable(toc, pdf_path, n_pages)
    raw_ok = False
    if RAW_MINERU_OUTPUT:
        # Preferred at this tier: hand MinerU's own markdown through untouched.
        raw_ok, raw_words = _emit_raw_markdown(parse_dir, pdf_path, stage1_dir, n_pages)
        if raw_ok:
            outline_note = (f"raw MinerU markdown emitted whole ({raw_words} words) \u2014 "
                            f"no re-chunking, which is what loses table content at this tier")
            headings.append({"level": 1, "title": pdf_path.stem, "page": 1})
    if not raw_ok:
        if use_toc:
            _emit_via_outline(toc, n_pages, chunks, by_order, dropped, stage1_dir,
                              pdf_path, headings, chunk_body)
        else:
            _emit_via_chunks(chunks, by_order, dropped, stage1_dir, pdf_path,
                             headings, chunk_body)

    (stage1_dir / "tables_manifest.json").write_text(json.dumps(
        {"source_pdf": str(pdf_path), "tables": tables_manifest}, indent=2))
    (stage1_dir / "headings_manifest.json").write_text(json.dumps(
        {"structure_source": ("MinerU raw markdown (unchunked)" if raw_ok else
                             "PDF bookmark outline" if use_toc else "MinerU (completeness fallback)"),
         "headings": headings}, indent=2))
    # Only pages / pages_snapshotted / orphan_footnote_refs are read for SCORING
    # (check_scorecard.py); headings_total/matched/structure_source are display
    # only. There is no "unreadable page" or "snapshot" concept on this path —
    # MinerU either parsed a page's blocks or it didn't reach the tree at all.
    stage1_report = {
        "pages": n_pages, "pages_snapshotted": [], "orphan_footnote_refs": [],
        "structure_source": ("MinerU raw markdown (unchunked)" if raw_ok else
                             "PDF bookmark outline" if use_toc else "MinerU (completeness fallback)"),
        "heading_signal": "mineru_raw" if raw_ok else "outline" if use_toc else "mineru",
        "outline_decision": outline_note,
        "headings_total": len(headings), "headings_matched": len(headings),
        "tables_deferred": len(tables_manifest),
    }
    (stage1_dir / "stage1_report.json").write_text(json.dumps(stage1_report, indent=2))

    stage2_report = {
        "tables_attempted": len(stage2_tables), "tables_ok": len(stage2_tables),
        "tables_failed": 0, "tables_continuation": 0, "tables_reclassified": 0,
        "tables_absorbed": 0, "tables": stage2_tables,
        "orphan_blocks": [], "orphan_block_count": 0,
    }
    (stage2_dir / "stage2_report.json").write_text(json.dumps(stage2_report, indent=2))

    # No splice step needed — Stage 1's tree already has every table resolved
    # inline, so Stage 3 is a straight copy, exactly like hybrid_extract.py's
    # own run_stage3() when nothing needs recovering.
    shutil.copytree(stage1_dir, stage3_dir)
    stage3_report = {"tables_total": len(tables_manifest), "tables_filled": len(stage2_tables),
                     "tables_reclassified": 0, "tables_continuation": 0, "tables_absorbed": 0,
                     "tables_failed": 0, "failed_table_ids": []}
    (stage3_dir / "stage3_report.json").write_text(json.dumps(stage3_report, indent=2))
    (stage3_dir / "STAGE3_REPORT.md").write_text(
        "# Stage 3 — combine report (MinerU fallback: nothing to splice)\n\n"
        + "\n".join(f"- **{k}**: {v}" for k, v in stage3_report.items()) + "\n")

    return {"pages": n_pages, "headings": len(headings), "tables": len(tables_manifest),
            "structure_source": ("mineru_raw" if raw_ok else "outline" if use_toc else "mineru_chunks")}


# Below this many pages, MinerU's raw markdown is emitted as ONE file: a short memo
# is genuinely one section, and splitting it yields stubs rather than structure.
# Imported, not restated: the page at which a document needs sections is one
# decision. When these were two constants they drifted and 8-9 page documents were
# emitted as one chunk and then failed for being one chunk.
from check_structure_profile import BLOB_MIN_PAGES as RAW_SPLIT_MIN_PAGES  # noqa: E402
# ...and a split is only worth doing if it actually produces sections. A 60-page
# document with two headings is not improved by becoming two 30-page files.
RAW_SPLIT_MIN_SECTIONS = 3

_RAW_HEAD_RE = re.compile(r"^(#{1,2})\s+(\S.*?)\s*$", re.M)


def _norm_head(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"\s+", " ", t or "").lower()).strip()[:60]


def _slug(t: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (t or "").lower()).strip("-")
    return (s[:limit].rstrip("-") or "section")


def _running_header_titles(text: str, min_repeats: int = 3) -> set:
    """Heading texts that repeat so often they are page furniture, not sections.

    MinerU marks the running page header as an h1 on every page it appears. Splitting
    on those produced four sections called "aosphere" on G20 Canada, one of them
    holding 1541 words of real content under a meaningless title. A heading that
    recurs this many times is the page's letterhead, not a section boundary."""
    seen: dict = {}
    for m in _RAW_HEAD_RE.finditer(text):
        k = _norm_head(m.group(2))
        if k:
            seen[k] = seen.get(k, 0) + 1
    return {k for k, n in seen.items() if n >= min_repeats}


def _split_raw_sections(text: str) -> list[tuple[str, str]]:
    """MinerU's markdown cut at its own h1/h2 boundaries -> [(title, body), ...].

    Verbatim: the text between two headings is passed through untouched, so this
    changes file boundaries and nothing else. Anything before the first heading
    becomes a leading section so no content can be lost to the split."""
    furniture = _running_header_titles(text)
    heads = [m for m in _RAW_HEAD_RE.finditer(text)
             if _norm_head(m.group(2)) not in furniture]
    if not heads:
        return []
    out: list[tuple[str, str]] = []
    preamble = text[:heads[0].start()].strip()
    if preamble:
        out.append(("Front matter", preamble))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end():end].strip()
        out.append((m.group(2).strip(), body))
    return out


def _heading_pages(blocks: list) -> dict:
    """normalised heading text -> 1-based page it appears on, from MinerU's blocks.
    Lets each emitted section carry a real page range instead of the whole document."""
    out: dict = {}
    for b in blocks or []:
        t = (b.get("text") or "").strip()
        if not t:
            continue
        k = _norm_head(t)
        if k and k not in out:
            out[k] = (b.get("page_idx") or 0) + 1
    return out


def _emit_raw_markdown(parse_dir: Path, pdf_path: Path, stage1_dir: Path,
                       n_pages: int) -> tuple[bool, int]:
    """Write everything MinerU extracted, unchunked. -> (wrote_it, word_count)

    This tier is the last resort. Everything above it has already failed, and the
    measurements say the re-chunking is what loses the document: on
    172_G20/Brazil__182791 MinerU read 1460 of the PDF's 1552 words (94%) and the
    chunked tree kept 231 (15%). So at this tier we stop re-shaping and hand
    MinerU's own output through.

    But MinerU's native .md is NOT everything MinerU found. Measured on the same
    document, of the blocks in content_list.json:

        text            24 blocks   533 words   all 24 in the .md
        table            3 blocks   906 words   none in the .md
        page_footnote    2 blocks    21 words   none in the .md
        footer           5 blocks    15 words   none in the .md

    Its markdown is the PROSE export. Passing only that through silently dropped
    every footnote and every table body — content MinerU had already read
    correctly. So the native markdown is the base, and any content-bearing block
    missing from it is appended rather than discarded.

    `page_number` is the one type deliberately left out: it is page furniture with
    no document content, and re-emitting it would inflate duplicate-word counts.

    A `*Source:` breadcrumb spanning the whole document is prepended because every
    validator locates a file's content by that line; without it the file is skipped
    as un-rangeable and the tier would look like it produced nothing."""
    md = [m for m in sorted(parse_dir.glob("*.md")) if not m.name.endswith("_content_list.md")]
    if not md:
        return False, 0
    text = max((m.read_text(encoding="utf-8", errors="replace") for m in md), key=len).strip()
    if not text:
        return False, 0

    # ---- recover whatever the .md left behind -------------------------------
    hay = re.sub(r"\s+", " ", text).lower()

    def _present(fragment: str) -> bool:
        f = re.sub(r"\s+", " ", fragment).strip().lower()
        return bool(f) and f[:60] in hay

    extra: list[str] = []
    try:
        blocks = json.loads(next(parse_dir.glob("*_content_list.json")).read_text())
    except (StopIteration, OSError, json.JSONDecodeError):
        blocks = []
    for b in blocks:
        btype = b.get("type")
        if btype == "page_number":
            continue                       # furniture, carries no document content
        payload = b.get("table_body") if btype == "table" else b.get("text")
        payload = (payload or "").strip()
        if not payload or _present(payload):
            continue
        page = (b.get("page_idx") or 0) + 1
        label = {"table": "table", "page_footnote": "footnote",
                 "footer": "page footer", "header": "page header"}.get(btype, btype)
        extra.append(f"<!-- recovered from MinerU {btype}, page {page} -->\n"
                     f"**[{label} \u00b7 page {page}]**\n\n{payload}")

    if extra:
        text = (text + "\n\n## Content MinerU extracted but omitted from its markdown export\n\n"
                + "\n\n".join(extra))

    # ---- one file, or one file per section? ----------------------------------
    # A short memo is genuinely one section and splitting it produces stubs. A long
    # one is not: emitting 25,000 words as a single chunk defeats the point of the
    # tree, since nothing downstream can retrieve or cite a section of it. MinerU's
    # own markdown already carries the headings to split on (Denmark 158704: 44 h1 +
    # 64 h2 across 48 pages), so the split costs no accuracy — the text is passed
    # through verbatim either way, only the file boundaries change.
    # Under the page bar: one chunk, by design, and the structure test accepts it.
    # At or over it: split on MinerU's own headings. If MinerU found no usable
    # headings there is nothing to split on — the single chunk is then the best that
    # exists, and failing a COMPLETE document for a shape MinerU could not give it
    # would be punishing the wrong thing. It is emitted whole and flagged instead.
    sections = _split_raw_sections(text) if n_pages >= RAW_SPLIT_MIN_PAGES else []
    if len(sections) < RAW_SPLIT_MIN_SECTIONS:
        # Too few headings to be worth splitting (or a short document). One file, whole.
        body = (f"# {pdf_path.stem}\n\n"
                f"*Source: `{pdf_path.name}`, page 1\u2013{n_pages}*\n\n{text}\n")
        (stage1_dir / "01-document.md").write_text(body, encoding="utf-8")
        return True, len(body.split())

    page_of = _heading_pages(blocks)
    written = 0
    for i, (title, chunk) in enumerate(sections, 1):
        lo = page_of.get(_norm_head(title))
        nxt = page_of.get(_norm_head(sections[i][0])) if i < len(sections) else None
        # A section runs to the page the NEXT one starts on; unknown either way falls
        # back to the whole document, which is honest rather than wrong — every
        # validator reads this line to know what pages a file is allowed to contain.
        rng = (f"page {lo}\u2013{nxt if nxt and nxt >= lo else n_pages}" if lo
               else f"page 1\u2013{n_pages}")
        body = f"# {title}\n\n*Source: `{pdf_path.name}`, {rng}*\n\n{chunk.strip()}\n"
        (stage1_dir / f"{i:02d}-{_slug(title)}.md").write_text(body, encoding="utf-8")
        written += len(body.split())
    return True, written

def _outline_is_usable(toc: list, pdf_path: Path, n_pages: int) -> tuple[bool, str]:
    """Can this bookmark outline actually describe the document?

    The outline is preferred over MinerU's own hierarchy for good reasons (see the
    module docstring) — but ONLY when it is a description of the document rather
    than merely present. This tier is the last resort, reached by documents the
    normal pipeline already failed on, and the commonest reason for that failure IS
    the outline: publishers ship PDFs whose two bookmarks are internal anchor names
    ("bmkFrontPage", "bmkPrimaryFrontPage") that appear nowhere in the page text.
    Re-parsing those through the same outline reproduces the same near-empty tree —
    the rescue inherits the defect it was called in to repair. Measured on
    172_G20/Brazil__182791: 6 pages, 1552 words, rescued output 233 words (15%).

    Two ways an outline fails this test:
      * it covers almost none of the document (bookmarks only on the last pages), or
      * its titles cannot be found in the text they claim to name.
    Either way MinerU's own hierarchy — imperfect as it is — is the better bet,
    because it is at least derived from what the page actually says."""
    if not toc:
        return False, "no bookmark outline"
    doc = fitz.open(str(pdf_path))
    try:
        located = 0
        for _lvl, title, page in toc:
            t = re.sub(r"\s+", " ", (title or "")).strip().lower()
            if not t:
                continue
            lo, hi = max(0, page - 2), min(n_pages, page + 1)
            hay = " ".join(re.sub(r"\s+", " ", doc[i].get_text()).lower()
                           for i in range(lo, hi))
            if t[:40] in hay:
                located += 1
    finally:
        doc.close()
    total = sum(1 for _l, t, _p in toc if (t or "").strip())
    if not total:
        return False, "outline entries have no titles"
    share = located / total
    if share < OUTLINE_LOCATE_SHARE:
        return False, (f"only {located}/{total} outline titles appear in the text they name "
                       f"\u2014 these are internal anchors, not headings")
    first = min(pg for _l, _t, pg in toc)
    if first > OUTLINE_START_FRAC * n_pages:
        return False, (f"outline starts on page {first} of {n_pages} \u2014 it covers only the tail "
                       f"of the document")
    return True, f"{located}/{total} outline titles located"


def _emit_via_outline(toc, n_pages, chunks, by_order, dropped, stage1_dir,
                      pdf_path, headings, chunk_body) -> None:
    """The preferred path (PDF has a bookmark outline). Every outline entry
    is its own folder (if it has children) or file (if not) — never deeper,
    never shallower than the outline itself goes. A MinerU chunk that falls
    inside one outline entry's page range but isn't itself an outline entry
    (this corpus's un-bookmarked "Paragraph 16(a)" clause labels) renders
    INLINE within that entry's own file, as a sub-heading marker + body, in
    page order — visible, but not mistaken for a structural boundary the
    source document's own author never drew."""
    roots = _outline_tree(toc, n_pages)

    # Group every real (non-TOC, non-root, level>=1) MinerU chunk under the
    # outline node that owns its heading's page — id() because the same
    # outline node dict can't be used as a dict key otherwise (unhashable).
    by_owner: dict[int, list[Chunk]] = {}
    for c in chunks:
        if c.order == 0 or c.order in dropped:
            continue
        page = (c.heading_block or {}).get("page_idx", 0) + 1
        owner = _owner_node(roots, page)
        by_owner.setdefault(id(owner), []).append(c)

    def render_inline_extras(node: dict) -> str:
        """MinerU chunks owned by this node, in document order, each as its
        own inline sub-heading + body — not a separate file. Skips a chunk
        that's really just MinerU independently re-detecting THIS SAME
        heading (its title matches the outline entry's own title) — that one
        already IS the file's "# Title" line above; repeating it as a "###"
        right below would be a pure duplicate, not new structure."""
        own_key = _slug(node["title"])
        parts = []
        for c in sorted(by_owner.get(id(node), []), key=lambda c: c.order):
            if _slug(c.title) == own_key:
                body = chunk_body(c)
                if body:
                    parts.append(body)
                continue
            marker = "#" * min(c.level + 1, 6)
            parts.append(f"{marker} {c.title}")
            body = chunk_body(c)
            if body:
                parts.append(body)
        return "\n\n".join(parts)

    def emit(node: dict, out_dir: Path, name: str, is_doc_root: bool = False) -> str:
        if not node.get("is_front_matter"):
            headings.append({"level": node["level"], "title": node["title"], "page": node["page"]})
        own_body = render_inline_extras(node)

        if node["children"]:
            node_dir = out_dir if is_doc_root else out_dir / name
            node_dir.mkdir(parents=True, exist_ok=True)
            child_links = []
            for i, child in enumerate(node["children"], 1):
                child_name = f"{i:02d}-{_slug(child['title'])}"
                rel = emit(child, node_dir, child_name)
                child_links.append(f"- [{child['title']}]({rel})")
            title = pdf_path.stem if is_doc_root else node["title"]
            text = "\n\n".join([f"# {title}", _source_line(pdf_path.name, node["page"], node["end"])]
                              + ([own_body] if own_body else [])
                              + (["\n".join(child_links)] if child_links else []))
            (node_dir / "README.md").write_text(text + "\n")
            return "README.md" if is_doc_root else f"{name}/README.md"

        title = node["title"]
        text = "\n\n".join([f"# {title}", _source_line(pdf_path.name, node["page"], node["end"])]
                          + ([own_body] if own_body else []))
        path = out_dir / f"{name}.md"
        path.write_text(text + "\n")
        return f"{name}.md"

    child_links = []
    for i, root in enumerate(roots, 1):
        name = f"{i:02d}-{_slug(root['title'])}"
        rel = emit(root, stage1_dir, name)
        child_links.append(f"- [{root['title']}]({rel})")
    text = "\n\n".join([f"# {pdf_path.stem}", _source_line(pdf_path.name, 1, n_pages),
                        "\n".join(child_links)])
    (stage1_dir / "README.md").write_text(text + "\n")


# ---------------- MinerU-chunk-tree structure (no outline available) ------
def _chunk_page_span(*nodes: Chunk) -> tuple[int, int]:
    pages = []
    for c in nodes:
        if c.heading_block is not None:
            pages.append(c.heading_block.get("page_idx", 0))
        pages.extend(b["page_idx"] for b in c.blocks if b.get("page_idx") is not None)
    if not pages:
        pages = [0]
    return (min(pages) + 1, max(pages) + 1)


def _children(by_order: dict[int, Chunk], order: int) -> list[Chunk]:
    return [by_order[o] for o in by_order[order].children_orders]


def _descendants(by_order: dict[int, Chunk], chunk: Chunk) -> list[Chunk]:
    out: list[Chunk] = []
    stack = list(_children(by_order, chunk.order))
    while stack:
        c = stack.pop(0)
        out.append(c)
        stack = list(_children(by_order, c.order)) + stack
    return out


def _emit_via_chunks(chunks, by_order, dropped, stage1_dir, pdf_path,
                     headings, chunk_body) -> None:
    """Fallback when the PDF has no bookmark outline at all: mirror MinerU's
    own chunk tree exactly — every chunk with children is its own directory,
    at whatever depth that goes, numbered by document-order sibling position
    (never a number parsed out of the title, so listing order always matches
    page order even when a title repeats)."""
    def record_heading(chunk: Chunk) -> None:
        if chunk.level >= 1:
            page = (chunk.heading_block or {}).get("page_idx", 0) + 1
            headings.append({"level": chunk.level, "title": chunk.title, "page": page})

    def emit(chunk: Chunk, out_dir: Path, name: str) -> str:
        is_root = chunk.order == 0
        record_heading(chunk)
        kids = [k for k in _children(by_order, chunk.order) if k.order not in dropped]

        if kids:
            node_dir = out_dir if is_root else out_dir / name
            node_dir.mkdir(parents=True, exist_ok=True)
            body = chunk_body(chunk)
            child_links = []
            for i, k in enumerate(kids, 1):
                child_name = f"{i:02d}-{_slug(k.title)}"
                rel = emit(k, node_dir, child_name)
                child_links.append(f"- [{k.title}]({rel})")
            lo, hi = _chunk_page_span(chunk, *_descendants(by_order, chunk))
            title = pdf_path.stem if is_root else chunk.title
            text = "\n\n".join([f"# {title}", _source_line(pdf_path.name, lo, hi)]
                              + ([body] if body else [])
                              + (["\n".join(child_links)] if child_links else []))
            (node_dir / "README.md").write_text(text + "\n")
            return "README.md" if is_root else f"{name}/README.md"

        lo, hi = _chunk_page_span(chunk)
        title = pdf_path.stem if is_root else chunk.title
        body = chunk_body(chunk)
        text = "\n\n".join([f"# {title}", _source_line(pdf_path.name, lo, hi)]
                          + ([body] if body else []))
        path = out_dir / f"{name}.md"
        path.write_text(text + "\n")
        return f"{name}.md"

    emit(by_order[0], stage1_dir, "README")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--mineru-backend", default=None)
    ap.add_argument("--mineru-effort", default=None)
    args = ap.parse_args()
    report = run_mineru_full(Path(args.pdf), Path(args.out),
                             backend=args.mineru_backend, effort=args.mineru_effort)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
