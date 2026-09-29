"""MinerU-backed extraction — PDF only.

Scope, deliberately narrow: `.docx` always goes through the legacy extractor
(it reads Word's own paragraph styles directly — exact, no guessing; MinerU
has no equivalent signal and its own output has been found to be internally
inconsistent between its markdown and JSON renderings of the SAME run). MinerU
is used only for formats legacy cannot read at all (PDF, scans, ...), where no
comparable ground truth exists and MinerU's own layout/OCR understanding is
the only option.

`blocks_to_doc` reads `content_list.json` (MinerU's structured blocks, not its
markdown rendering) and builds a heading tree with the standalone MinerU2.5
"Document Chunker" project's logic, ported verbatim into extract/mineru_chunking.py
— `build_chunks`: re-derive heading depth from each heading's own numbering
marker ("A." / "1." / "1.1" / "(a)" / "(i)") because real MinerU2.5 output
flattens every heading to `text_level=1`; expand grouped `list` blocks so each
lettered/roman item can be classified; and broadcast page footnotes and running
page furniture to every section sharing their page.

This function is the aosphere adapter on top of that pristine port. It adds
three aosphere-specific policies (kept out of mineru_chunking.py so the port
stays a faithful copy):

  1. Real clause keys, matching the legacy docx extractor's convention exactly
     (`A`, `A1`, `A1.1`, `A1.6.1`) — synthesized from each heading's marker
     path (Part letter + deepest decimal marker + paren markers), so a retrieved
     chunk carries its true, document-order clause reference (`1.` under Part A
     is `A1`; `1.` under Part B is `B1` — never colliding).
  2. Per-product depth cap + fold, sharing legacy's `max_heading_level(product)`
     (Shareholding Disclosure = 3 decimal levels, default 8). Decimal clauses up
     to the cap are kept as sections; paren items "(a)"/"(i)" and over-cap
     decimals fold into their nearest kept clause's BODY (so the citable unit is
     the mid-level numbered clause, its sub-items included as content).
  3. Table-of-contents exclusion: a "Contents" run (entries with trailing page
     numbers / sharing the Contents page) is dropped from content. Markers still
     drive structure — the TOC only validates and de-clutters.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from aosphere_core_index.config import settings
from aosphere_core_index.extract import platform_profile
from aosphere_core_index.extract.mineru_chunking import (
    FOOTNOTE_TYPES,
    Chunk,
    build_chunks,
    render_block,
)
from aosphere_core_index.extract.model import Element, ExtractedDoc, Section
from aosphere_core_index.extract.styles import max_heading_level

# Leading numbering markers, mirrored on the aosphere side purely to SYNTHESIZE
# the clause key (mineru_chunking.classify_heading_family already decided the
# tree shape; here we only read the literal marker off each title).
_MARK_ALPHA = re.compile(r"^([A-Z])\.\s")
_MARK_DECIMAL = re.compile(r"^(\d+(?:\.\d+)*)(?=[.\s])")
_MARK_PAREN = re.compile(r"^\(([A-Za-z]+)\)\s")

_TOC_TITLE = re.compile(r"^(?:table of\s+)?contents$|^index$", re.IGNORECASE)
_TRAILING_PAGENO = re.compile(r"\S\s+\d+\s*$")  # "... Shareholding 3" (a TOC line)


def _marker(title: str) -> tuple[str, str | None]:
    """(kind, literal) of a heading title's leading marker: ('alpha', 'A'),
    ('decimal', '1.1'), ('paren', 'a'), or ('none', None)."""
    t = title.strip()
    m = _MARK_ALPHA.match(t)
    if m:
        return "alpha", m.group(1)
    m = _MARK_DECIMAL.match(t)
    if m:
        return "decimal", m.group(1)
    m = _MARK_PAREN.match(t)
    if m:
        return "paren", m.group(1)
    return "none", None


def _slug(text: str, max_len: int = 40) -> str:
    """Fallback key for a kept heading that carries no numbering marker (a title
    page, "Appendix 1 ...", "Glossary"): a short slug, so its key is still
    stable and human-readable rather than a bare positional counter."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rsplit("-", 1)[0] or s[:max_len]
    return s or "section"


_KEY_PAREN = re.compile(r"\([A-Za-z]+\)$")
_KEY_ALNUM = re.compile(r"^([A-Za-z]*)(\d+(?:\.\d+)*)$")


def _parent_key(key: str) -> str | None:
    """The clause key one level up, or None for a top-level key (-> the root).
    A1.1(a)(i)->A1.1(a); A1.1(a)->A1.1; A1.6.2->A1.6; A1.1->A1; A1->A; A->None;
    1.1->1; 1->None; a marker-less slug->None."""
    if _KEY_PAREN.search(key):
        return _KEY_PAREN.sub("", key)
    m = _KEY_ALNUM.match(key)
    if m:
        letter, nums = m.group(1), m.group(2)
        if "." in nums:
            return letter + nums.rsplit(".", 1)[0]
        return letter or None  # "A1"->"A"; "1"->root
    return None  # pure Part letter, or a marker-less slug -> root


def _element_for_block(block: dict) -> Element | None:
    """Render one MinerU body block into an aosphere Element, or None if it
    renders empty. `kind` maps MinerU's block type onto aosphere's small element
    vocabulary: tables -> "table", surviving grouped lists -> "bullet",
    everything else (text, expanded list items, equations, images, code,
    unknown types) -> "body"."""
    text = render_block(block)
    if not text.strip():
        return None
    btype = block.get("type")
    if btype == "table":
        kind = "table"
    elif btype == "list":
        kind = "bullet"
    else:
        kind = "body"
    return Element(kind=kind, text=text)


def _demote_repeating_headers(blocks: list[dict], min_pages: int = 4, max_len: int = 120) -> list[dict]:
    """Retype a running page header that a backend mis-tagged as a heading.

    The `vlm` backend tags a running header ("aosphere" atop every page) as
    `type: header`, which the chunker drops. The `pipeline`/`auto` backends
    sometimes mis-tag the SAME header as `type: text` with a heading `text_level`
    — one per page — which would otherwise become dozens of spurious clause
    sections. An unmarked, short heading text that repeats as a heading on
    `min_pages`+ distinct pages is page furniture, not a clause: retype those
    occurrences to `header` so the normal page-note path discards them. Real
    clause titles are unique (or numbered), so this never touches them."""
    from collections import defaultdict

    pages: dict[str, set] = defaultdict(set)
    for b in blocks:
        if b.get("type") == "text" and isinstance(b.get("text_level"), int):
            t = (b.get("text") or "").strip()
            if 0 < len(t) <= max_len and _marker(t)[0] == "none":
                pages[t.lower()].add(b.get("page_idx"))
    furniture = {t for t, pgs in pages.items() if len(pgs) >= min_pages}
    if not furniture:
        return blocks
    return [
        {**b, "type": "header"}
        if b.get("type") == "text" and (b.get("text") or "").strip().lower() in furniture
        else b
        for b in blocks
    ]


def _toc_orders(chunks: list[Chunk], by_order: dict[int, Chunk]) -> set[int]:
    """Orders of chunks that are table-of-contents navigation, to drop from
    content. A TOC line is recognized by a trailing page number ("... 3"); and,
    when an explicit "Contents" heading is present, every heading sharing its
    page is also treated as TOC (catches entries whose page number wrapped off,
    e.g. a long "Appendix 1 ..." title). Body clause headings never end in a
    bare page number, so the body is untouched — this only de-clutters."""
    dropped: set[int] = set()
    for c in chunks:
        if c.level > 0 and _TRAILING_PAGENO.search(c.title):
            dropped.add(c.order)
    for cc in chunks:
        if cc.level > 0 and _TOC_TITLE.match(cc.title.strip()):
            dropped.add(cc.order)
            page = cc.page_start
            for c in chunks:
                if c.order > cc.order and c.level > 0 and page is not None and c.page_start == page:
                    dropped.add(c.order)
    return dropped


def blocks_to_doc(blocks: list[dict], *, doc_meta: dict, source_key: str) -> ExtractedDoc:
    """MinerU content_list.json blocks -> ExtractedDoc via the ported MinerU2.5
    chunking logic plus aosphere clause-key / depth-cap / TOC policy (see module
    docstring). Pure function (no I/O) — testable against a cached
    content_list.json or synthetic blocks."""
    out = ExtractedDoc(
        doc_id=str(doc_meta.get("DOCID", "")),
        title=str(doc_meta.get("DOCNAME") or doc_meta.get("OPINIONNAME") or "Document"),
        jurisdiction=str(doc_meta.get("JURISDICTIONNAME", "")),
        jurisdiction_id=doc_meta.get("JURISDICTIONID"),
        opinion_id=doc_meta.get("OPINIONID"),
        product=str(doc_meta.get("RENDERSTYLENAME", "")),
        source_key=source_key,
    )

    blocks = _demote_repeating_headers(blocks)
    chunks = build_chunks(blocks, doc_title=out.title)
    by_order = {c.order: c for c in chunks}
    marker = {c.order: (_marker(c.title) if c.level > 0 else ("none", None)) for c in chunks}
    cap = max_heading_level(out.product)  # decimal-depth cap; shared with legacy
    dropped = _toc_orders(chunks, by_order)

    def _synth_key(order: int) -> tuple[str, str | None]:
        """Compose the clause key for a chunk from the markers along its path:
        Part letter + deepest decimal marker (self-describing) + paren markers,
        e.g. A -> A1 -> A1.1 -> A1.1(a) -> A1.1(a)(i). Returns (key, part_letter)."""
        path: list[int] = []
        o: int | None = order
        while o is not None:
            path.append(o)
            o = by_order[o].parent_order
        path.reverse()
        part, decimal, parens = "", "", []
        for po in path:
            kind, lit = marker[po]
            if kind == "alpha":
                part = lit
            elif kind == "decimal":
                decimal = lit
                parens = []  # a new numbered clause starts fresh — drop any paren ancestors
            elif kind == "paren":
                parens.append(f"({lit})")
        return f"{part}{decimal}" + "".join(parens), (part or None)

    def _is_kept(chunk: Chunk) -> bool:
        if chunk.level == 0:
            return True  # synthetic document root
        if chunk.order in dropped:
            return False  # table-of-contents navigation
        kind, lit = marker[chunk.order]
        if kind == "decimal":
            return lit.count(".") + 1 <= cap  # numbered clause within the product depth cap
        if kind == "paren":
            return False  # "(a)"/"(i)" fold into the enclosing clause's body
        return True  # Part letter, or a genuine unnumbered heading (Appendix, front matter)

    kept = {c.order for c in chunks if _is_kept(c)}

    # Every chunk resolves to the kept section its content belongs to (itself if
    # kept, else its nearest kept ancestor). Chunks are in document order with
    # parents before children, so a parent's target is always known first.
    fold_target: dict[int, int | None] = {}
    for c in chunks:
        base = fold_target[c.parent_order] if c.parent_order is not None else None
        fold_target[c.order] = c.order if c.order in kept else base

    # Pass 1a: create a Section per kept chunk with its synthesized clause key.
    # A marked heading's key (A1.1) encodes its own hierarchy; an unmarked one
    # (a slug, e.g. an "Aviation" callout) does not, so we remember which is
    # which — their parents are resolved differently in 1b.
    sec_by_order: dict[int, Section] = {}
    key_to_sec: dict[str, Section] = {}
    seen_keys: dict[str, int] = {}
    is_slug: dict[str, bool] = {}
    for c in chunks:
        if c.order not in kept:
            continue
        if c.order == 0:
            key, part_letter, slug = "0", None, False
        elif marker[c.order][0] == "none":
            key, part_letter, slug = _slug(c.title), None, True
        else:
            (key, part_letter), slug = _synth_key(c.order), False
        # Guarantee uniqueness (a marker-less heading can synthesize a key that
        # collides with an ancestor); disambiguate with a positional suffix.
        if key in seen_keys:
            seen_keys[key] += 1
            key = f"{key}-{seen_keys[key]}"
        else:
            seen_keys[key] = 1
        sec = Section(
            id=f"sec-{c.order}", key=key, title=c.title, level=0,
            parent_id=None, part_letter=part_letter,
        )
        out.sections.append(sec)
        sec_by_order[c.order] = sec
        key_to_sec[key] = sec
        is_slug[sec.id] = slug

    # Pass 1b: resolve each section's parent.
    #  - marked key (A1.1): nearest existing ancestor by KEY PREFIX
    #    (A1.1 -> A1 -> A -> root) — immune to cover-page pseudo-headings and
    #    MinerU's level-flattening.
    #  - unmarked slug (an "Aviation" callout): keep its place in MinerU's tree,
    #    i.e. the nearest KEPT ancestor of its raw parent — a slug key has no
    #    prefix to climb and must NOT fall out to the root.
    root_sec = sec_by_order[0]
    for c in chunks:
        if c.order not in kept or c.order == 0:
            continue
        sec = sec_by_order[c.order]
        if is_slug[sec.id]:
            anchor = fold_target[c.parent_order] if c.parent_order is not None else None
            parent = sec_by_order.get(anchor) if anchor is not None else root_sec
        else:
            pk = _parent_key(sec.key)
            while pk is not None and pk not in key_to_sec:
                pk = _parent_key(pk)
            parent = key_to_sec[pk] if pk is not None else root_sec
        sec.parent_id = (parent or root_sec).id

    # Levels: walk parent links to the root (robust for slug-under-slug chains,
    # where key length is no guide to depth).
    by_id = {s.id: s for s in out.sections}
    for sec in out.sections:
        depth, cur = 0, sec
        while cur.parent_id is not None and cur.parent_id in by_id:
            cur = by_id[cur.parent_id]
            depth += 1
        sec.level = depth

    # Pass 2: fill each kept section's body, walking chunks in document order so
    # a kept clause's own content and its folded sub-items interleave correctly.
    # A folded chunk contributes its heading title (so the "(a) ..." line is not
    # lost) followed by its body blocks. While doing so, record every page each
    # section's content occupies (its heading + own body + any folded sub-items),
    # which is what footnotes are keyed against below.
    section_pages: dict[str, set] = {sec.id: set() for sec in out.sections}
    for c in chunks:
        target_order = fold_target[c.order]
        if target_order is None or c.order in dropped:
            continue  # content of a dropped TOC chunk is discarded
        sec = sec_by_order[target_order]
        pages = section_pages[sec.id]
        if c.heading_block is not None and c.heading_block.get("page_idx") is not None:
            pages.add(c.heading_block["page_idx"])
        if c.order not in kept:
            title = c.title.strip()
            if title:
                sec.elements.append(Element(kind="body", text=title))
        for block in c.blocks:
            if block.get("page_idx") is not None:
                pages.add(block["page_idx"])
            element = _element_for_block(block)
            if element is not None:
                sec.elements.append(element)

    # Footnotes are page-scoped. Collect each page-footnote once (document order),
    # then give every section every footnote sitting on a page its content
    # occupies — so a clause that runs across pages carries the footnotes from
    # ALL of them, and clauses sharing a page share that page's footnotes.
    fn_page: dict[str, int] = {}
    for b in blocks:
        if b.get("type") in FOOTNOTE_TYPES:
            text = render_block(b).strip()
            page = b.get("page_idx")
            if not text or page is None:
                continue
            fid = str(len(out.footnotes) + 1)
            out.footnotes[fid] = text
            fn_page[fid] = page
    for sec in out.sections:
        pages = section_pages.get(sec.id) or set()
        sec.footnote_ids = [fid for fid, page in fn_page.items() if page in pages]

    return out


def _mineru_cli() -> str:
    """Path to the `mineru` CLI that ships in this venv (next to the python bin).

    Resolution is per-OS (Scripts/mineru.exe vs bin/mineru) — see
    platform_profile.mineru_cli. Falls back to a bare PATH lookup.
    """
    return platform_profile.mineru_cli() or "mineru"


def _mineru_env() -> dict:
    """Environment pinning MinerU to the locally-downloaded models, fully offline,
    plus this host's performance profile (Apple batch ratio / Windows CUDA_PATH —
    see extract/platform_profile.py). Profile values are defaults only: anything
    already exported by the caller wins.
    """
    env = dict(os.environ)
    env["HF_HOME"] = str(settings.mineru_model_cache.resolve())
    env["HF_HUB_OFFLINE"] = "1"
    env["MINERU_MODEL_SOURCE"] = "local"
    return platform_profile.apply_mineru_env(env)


def run_mineru(path: str, backend: str | None = None, effort: str | None = None) -> list[dict]:
    """Invoke the MinerU CLI on `path` and return its content_list.json blocks.

    `backend` (default settings.mineru_backend) is the MinerU parse backend:
    "vlm-engine" | "hybrid-engine" | "pipeline". `effort` ("medium" | "high")
    only applies to hybrid-* backends (medium disables image/chart analysis for
    speed; high keeps it) and is ignored otherwise."""
    backend = backend or settings.mineru_backend
    effort = effort or settings.mineru_effort
    with tempfile.TemporaryDirectory(prefix="mineru_") as out:
        cmd = [_mineru_cli(), "-p", path, "-o", out, "-b", backend]
        if effort and backend.startswith("hybrid"):
            cmd += ["--effort", effort]
        subprocess.run(
            cmd, env=_mineru_env(), check=True, capture_output=True, timeout=settings.mineru_timeout_s,
        )
        cands = sorted(Path(out).rglob("*_content_list.json"))
        if not cands:
            raise RuntimeError(f"MinerU produced no content_list.json under {out}")
        return json.loads(cands[0].read_text())


def run_mineru_to_dir(
    path: str, *, out_dir: Path, backend: str | None = None, effort: str | None = None,
) -> Path:
    """Like `run_mineru`, but writes MinerU's output under a caller-chosen
    persistent `out_dir` (instead of a throwaway temp dir) and returns the
    resolved parse-run directory — so raw output (content_list.json, .md, page
    images) survives for inspection rather than being discarded. Used by the
    hybrid extraction pipeline (scripts/hybrid_extract.py) where the whole
    point is to inspect what MinerU produced at each stage."""
    backend = backend or settings.mineru_backend
    effort = effort or settings.mineru_effort
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [_mineru_cli(), "-p", path, "-o", str(out_dir), "-b", backend]
    if effort and backend.startswith("hybrid"):
        cmd += ["--effort", effort]
    # encoding/errors pinned rather than left to the locale: MinerU's logs carry
    # non-ASCII (progress glyphs, CJK log lines), and text=True otherwise decodes with
    # the platform default — cp1252 on a stock Windows console. That raised
    # UnicodeDecodeError *inside subprocess's reader thread*, which does not fail the
    # call: the process still returns 0, so the only casualty is the captured output.
    # Since that output is the sole diagnostic when MinerU fails, losing it turns a
    # legible stderr into a silent mystery. errors="replace" keeps the text readable.
    subprocess.run(cmd, env=_mineru_env(), check=True, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
    stem = Path(path).stem
    doc_dir = out_dir / stem
    # Discovered dynamically (backend/parse-method subdir naming varies — see
    # mineru_runner._run) rather than hardcoded, so a MinerU release renaming
    # these doesn't silently break this integration.
    for candidate in sorted(d for d in doc_dir.iterdir() if d.is_dir()):
        if (candidate / f"{stem}_content_list.json").exists():
            return candidate
    raise RuntimeError(f"mineru produced no recognizable output under {doc_dir}")


def extract_mineru(
    path: str, *, doc_meta: dict, source_key: str,
    mineru_backend: str | None = None, effort: str | None = None,
) -> ExtractedDoc:
    """Parse a document via MinerU into the common ExtractedDoc model. Same
    signature as `docx_extract.extract_docx` (see `extract.extract_document`),
    plus optional `mineru_backend`/`effort` to override the parse backend for
    one call (e.g. from the A/B UI). Scoped to PDF/non-docx — see module docstring."""
    blocks = run_mineru(path, backend=mineru_backend, effort=effort)
    return blocks_to_doc(blocks, doc_meta=doc_meta, source_key=source_key)
