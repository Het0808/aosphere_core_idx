"""lib_content_compare.py — shared utilities for comparing an extracted markdown
tree against its source PDF, used by:
    check_word_coverage.py      (Test 1: whole-doc word-multiset diff)
    check_content_localized.py (Test 2: per-section sequence diff, pinpoints WHERE)
    check_numeric_integrity.py (Test 3: numbers/percentages/thresholds diff)

None of these compare markdown syntax against the PDF — clean_markdown() strips
the pipeline's own synthetic markup (status badges, failure/pending markers,
snapshot image refs, source breadcrumbs, HTML tags) first, so what's left on
both sides is real document content.
"""
from __future__ import annotations

import html
import json
import re
import sys
from collections import Counter
from pathlib import Path

import fitz

# ---- tokenizing --------------------------------------------------------
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")

# The Latin ligature block (U+FB00–U+FB06), expanded to the letters it stands for.
# See tokenize(): these glyphs are invisible to TOKEN_RE and silently split a word.
_LIGATURES = {ord(k): v for k, v in {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}.items()}

# Typographic quotation marks the PDF's text layer sets in place of a plain
# apostrophe. TOKEN_RE only admits the ASCII apostrophe (U+0027) inside a token, so
# a possessive or contraction the PDF prints with the typeset RIGHT SINGLE QUOTATION
# MARK ("regulator’s", U+2019) is invisible to it exactly as a ligature is: the
# glyph matches nothing, so instead of joining the word it acts as a SEPARATOR, and
# the word SPLITS -- "regulator" + "s" -- against a tree that, writing its own
# markdown in plain ASCII, holds "regulator's" as one token. Every check that
# compares the two then reports the whole surrounding run as missing, because the
# two sides can never agree on this one word. Measured on
# 124_Marketing_Restrictions/Brunei__180131: a 21-token span ending "...regulator's
# website plus any possible exemptions..." was reported DROPPED even though the tree
# prints the identical words -- the PDF's apostrophe glyph was the only difference.
# LEFT SINGLE QUOTATION MARK (U+2018) is mapped for the same reason, on the rare word
# that opens with one ("'Til", "'90s").
_CURLY_APOSTROPHES = {ord(k): "'" for k in ("’", "‘")}
NUMBER_RE = re.compile(r"\d[\d,.]*%?")

# ---- markdown cleanup ---------------------------------------------------
# "00-section.md" is subchunk.py's own landing-page file: split_file() puts everything
# above a section's first numbered sub-heading there -- the title, the source breadcrumb,
# and whatever intro prose exists (often none; most of these sections jump straight into
# "6.1 ...", "6.2 ..." with nothing between the heading and the first sub-heading). It is
# the stage-5 equivalent of pdf2mdtree's README.md, playing the same "landing page, not a
# content leaf" role, for the same reason -- and diffing it as an ordinary section used to
# report it as HOLLOW (kept ~0% of a 5000+-token body) even though nothing was lost: that
# body was never its own, it was always its children's, by construction of the split.
SKIP_MD_NAMES = {"CONVERSION_REPORT.md", "STAGE3_REPORT.md", "README.md", "PIPELINE_SUMMARY.md",
                 "00-section.md"}

# README.md (and 00-section.md, subchunking's equivalent) is skipped as a DIFF TARGET
# above (it's a generated index of links, or a section's own landing page -- not prose
# lifted from the PDF and diffed against a page range like an ordinary leaf). But it is
# still real tree content: an ancestor section's heading ("# A. Substantial Shareholding",
# "# D. Issuer-initiated disclosure") exists ONLY in its README, while the PDF page
# carrying that heading also falls inside a leaf file's page range. Excluding these files
# from the relocation search therefore made every ancestor title look like a silent gap
# in the first leaf file of its section — see iter_index_files/clean_index_markdown.
INDEX_MD_NAMES = {"README.md", "00-section.md"}
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")

_BADGE_LINE_RE = re.compile(
    r"^(?:\*\*⚙ MinerU-extracted table\*\*.*"
    # Every marker the pipeline injects, so none is mistaken for source content.
    # CONTINUATION and REGION ABSORBED were missing here: their prose ("nothing is
    # missing", "absorbed into the adjacent extracted output", …) was being counted
    # as words the tree has and the PDF doesn't, i.e. as duplication, which pushed
    # Uniqueness down on exactly the documents that have the most merged tables.
    r"|>\s*\*\*\[TABLE (?:EXTRACTION FAILED|PENDING|CONTINUATION|REGION ABSORBED)[^\]]*\]\*\*.*"
    r"|>\s*Page \d+ of the source PDF contains a complex table/diagram.*"
    r"|!\[Page \d+\]\([^)]*\).*)$",
    re.MULTILINE,
)
_SOURCE_BREADCRUMB_RE = re.compile(r"^\*Source:.*\*\s*$", re.MULTILINE)
# The traffic-light squares are GRAPHICS in the source — the PDF's text layer holds
# nothing where they are printed — so summary_ai_extract asks the model to write them
# as [green]/[amber]/[red] (see its prompt). That makes them pipeline-injected tokens
# the PDF can never supply, and they land in the worst possible place: immediately
# after a heading, at the head of the section's first paragraph. Left in, each one
# CUTS A CONTIGUOUS RUN IN TWO, which is precisely what decomposed_coverage and
# relocation_ratio measure — so a section whose text is present verbatim scores as
# two short runs instead of one long one and is reported as a gap. Measured on
# 124_Marketing_Restrictions/India__34712: "Pre-Marketing of Funds" is reproduced word
# for word in its own file and was still reported absent, because `red` sat between
# "funds" and "there". 231 files across 31 of the 43 AI-processed documents carry one.
_COLOUR_MARKER_RE = re.compile(r"\[(?:green|amber|red)\]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HEADING_MARK_RE = re.compile(r"^#+\s*", re.MULTILINE)

SOURCE_LINE_RE = re.compile(r"^\*Source:\s*`[^`]*`,\s*page\s*(\d+)(?:[–-](\d+))?\*", re.MULTILINE)


def tokenize(text: str) -> list[str]:
    # NOTE: a digit run glued to a following word ("182Section", from a footnote marker
    # flush against its text) does NOT get split here. Splitting it corpus-wide was
    # tried and reverted: it made the two sides agree about footnotes, but it also
    # turned table-of-contents noise ("3Structure of Part B") into well-formed token
    # runs, inventing missing-content spans in documents whose TOC page is correctly
    # not extracted — 5 regressions in a 57-document sample, one of them review -> 0.0.
    # The footnote excusal needs a narrower fix; see out/_regression/.
    #
    # Edge hyphens ARE stripped. TOKEN_RE admits a hyphen inside a token, which is
    # right for "reverse-enquiry" and "cross-border", but it also swallows a trailing
    # dash that only ever came from layout: the PDF prints "in Jersey - if there is",
    # the extractor emits "in Jersey- if there is", and the two sides then disagree
    # about the word "jersey" itself. That cost a real document 15 points — the
    # negation check read "which is NOT intended for marketing in jersey" as a
    # dropped "not", because its 5-token context ended on `jersey-` and could not
    # match the tree, while the same sentence WITHOUT the "not" matched a genuine
    # parallel clause elsewhere in the document. A dash at a token EDGE carries no
    # meaning, and stripping it is applied to both sides, so it can only ever make
    # the PDF and the tree agree about a word they already spell the same way.
    # Typographic LIGATURES are expanded first, on both sides. TOKEN_RE is ASCII-only,
    # so a word the PDF sets with one ("deﬁnitions", U+FB01) does not merely spell
    # differently — the glyph matches nothing and the word SPLITS, giving "de" +
    # "nitions" against a tree holding "definitions". Every check that compares the two
    # then reports a gap in text that is present and identical. Measured on
    # 124_Marketing_Restrictions/Dubai International Financial Centre__34552, whose only
    # finding was "de nitions of client types professional clients fall into three
    # sub-categories" — the one document in 43 that Scorecard 2 held at `review`.
    #
    # Only the Latin ligature block is mapped, not full NFKC: that would also rewrite
    # superscripts and fractions ("m²" -> "m2", "½" -> "1⁄2"), which are exactly the
    # tokens check_numeric_integrity reasons about.
    text = text.translate(_LIGATURES).translate(_CURLY_APOSTROPHES)
    return [s for s in (t.lower().strip("-") for t in TOKEN_RE.findall(text)) if s]


def clean_markdown(md_text: str) -> str:
    text = _BADGE_LINE_RE.sub("", md_text)
    text = _SOURCE_BREADCRUMB_RE.sub("", text)
    text = _COLOUR_MARKER_RE.sub(" ", text)
    text = html.unescape(text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _HEADING_MARK_RE.sub("", text)
    return text


def parse_page_range(raw_md_text: str) -> tuple[int, int] | None:
    m = SOURCE_LINE_RE.search(raw_md_text)
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return (a, b)


def iter_content_files(tree_root: Path):
    for p in sorted(tree_root.rglob("*.md")):
        if p.name in SKIP_MD_NAMES:
            continue
        yield p


def iter_index_files(tree_root: Path):
    """Index/README files — never diffed, but their headings count as tree
    content when deciding whether a span was relocated (see INDEX_MD_NAMES)."""
    for p in sorted(tree_root.rglob("*.md")):
        if p.name in INDEX_MD_NAMES:
            yield p


def clean_index_markdown(md_text: str) -> str:
    """Index text for relocation lookup only: keeps link LABELS (real section
    titles that do exist in the tree) and drops link TARGETS (path slugs like
    '01-overview/README.md', which appear nowhere in the source PDF and would
    otherwise pollute the haystack with tokens that can never legitimately
    match)."""
    return clean_markdown(_MD_LINK_RE.sub(r"\1", md_text))


def has_flagged_gap_marker(raw_md_text: str) -> bool:
    return "TABLE EXTRACTION FAILED" in raw_md_text or "TABLE PENDING" in raw_md_text


# ---- footnotes -----------------------------------------------------------
# Shared with check_footnote_integrity.py (the canonical definition of what
# counts as a footnote reference vs. a body) so a numeric token that check_
# numeric_integrity.py wants to cross-check against "is this actually a
# footnote marker?" always means the same thing in both places.
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:[ \t]*(.*)$", re.MULTILINE)
FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]]+)\](?!:)")


def footnote_ids_in_tree(tree_root: Path) -> set[int]:
    """Every NUMERIC footnote id appearing anywhere in the tree, as either an
    inline [^n] reference or a [^n]: body definition. A non-numeric custom
    footnote key (rare) is skipped — it can never collide with a digit run
    lifted from the PDF's plain text, so it isn't useful to a numeric
    cross-check."""
    ids = set()
    for f in iter_content_files(tree_root):
        raw = f.read_text(encoding="utf-8")
        for m in FOOTNOTE_DEF_RE.finditer(raw):
            if m.group(1).isdigit():
                ids.add(int(m.group(1)))
        for m in FOOTNOTE_REF_RE.finditer(raw):
            if m.group(1).isdigit():
                ids.add(int(m.group(1)))
    return ids


# ---- PDF text -----------------------------------------------------------
def pdf_page_texts(pdf_path: Path) -> list[str]:
    """1-indexed: texts[0] is a blank sentinel, texts[1] is page 1's text.
    Extracted via PyMuPDF (fitz) — the same engine pdf2mdtree itself uses."""
    doc = fitz.open(str(pdf_path))
    texts = [""] + [doc[i].get_text() for i in range(doc.page_count)]
    doc.close()
    return texts


def pypdf_page_texts(pdf_path: Path) -> list[str]:
    """1-indexed, same shape as pdf_page_texts(). pypdf is a completely
    independent, pure-Python PDF parser (not a MuPDF wrapper) — an extraction
    bug/blind-spot specific to fitz's engine is very unlikely to be reproduced
    identically here, which is what makes cross-checking the two meaningful."""
    import pypdf
    reader = pypdf.PdfReader(str(pdf_path))
    texts = [""] + [(p.extract_text() or "") for p in reader.pages]
    return texts


# MinerU block types that carry real document content worth comparing.
# "footer"/"header"/"page_number" are MinerU's OWN classification of running
# boilerplate — matches what detect_boilerplate_patterns() finds heuristically
# on the fitz side, so both are excluded the same way.
_MINERU_CONTENT_TYPES = {"text", "table", "page_footnote"}


def mineru_page_texts(content_list_path: Path) -> list[str]:
    """1-indexed, same shape as pdf_page_texts(). Reads a whole-document MinerU
    content_list.json (NOT the table-only crop Stage 2 produces) and
    concatenates every content block's text per page, in top-to-bottom order.
    Vision-based — MinerU reads the rendered page, not the PDF's internal text
    objects, so it doesn't share fitz/pypdf's failure modes at all."""
    blocks = json.loads(Path(content_list_path).read_text())

    def _y0(b):
        bbox = b.get("bbox")
        return bbox[1] if bbox else 0.0

    by_page: dict[int, list[dict]] = {}
    for b in blocks:
        if b.get("type") not in _MINERU_CONTENT_TYPES:
            continue
        by_page.setdefault(b.get("page_idx", 0), []).append(b)

    n_pages = (max(by_page) + 1) if by_page else 0
    texts = [""] * (n_pages + 1)
    for page_idx, page_blocks in by_page.items():
        page_blocks.sort(key=_y0)
        parts = []
        for b in page_blocks:
            if b.get("type") == "table":
                raw_html = b.get("table_body", "") or ""
                parts.append(html.unescape(_HTML_TAG_RE.sub(" ", raw_html)))
            else:
                parts.append(b.get("text", "") or "")
        texts[page_idx + 1] = "\n".join(parts)
    return texts


# ---- running header/footer detection -------------------------------------
_DIGIT_RUN_RE = re.compile(r"\d+")
# "182Section" -> "182 Section": a footnote number flush against its text
_GLUED_NUM_RE = re.compile(r"(\d)([A-Za-z])")


def _boiler_signature(line: str) -> str:
    """Signature for a running header/footer line.

    Digit runs collapse to "#" so "page 3 of 57" and "page 4 of 57" are one pattern.
    Spacing and dash style collapse too, because a PDF text layer is not consistent
    about them: Arkansas emits its header as "Data Privacy – US States – December
    2025" on 142 pages and "Data Privacy – US States– December 2025" (no space before
    the dash) on 138, and the two buckets each fell under the 80% threshold — so a
    header printed on 280 of 281 pages was never recognised and its 280 occurrences
    were reported as missing content."""
    t = _DIGIT_RUN_RE.sub("#", line.lower())
    t = re.sub(r"[\u2010-\u2015\u2212]", "-", t)      # every dash to a hyphen
    t = re.sub(r"[^a-z0-9#]+", " ", t)                  # punctuation to space
    return " ".join(t.split())


# A printed contents page lists "<title> …… <page-number>" with the number in the
# right margin. Detected by SHAPE (see scripts/rescue_outline.py, which uses the same
# signal to rebuild a broken outline) rather than by a heading, because publishers
# spell the heading every possible way.
_TOC_MIN_ROWS = 6
# weaker bar, and ONLY for a page touching a confirmed listing page
_TOC_EDGE_ROWS = 3


def toc_page_numbers(pdf_path, max_scan: int = 10) -> set[int]:
    """1-based page numbers near the front of the document that ARE a contents
    listing.

    A contents page is not body content and no extracted tree carries it, so charging
    a section for "missing" the listing printed on its pages is a false positive —
    which is what happens once a recurring header is stripped and the listing's rows
    join into one coherent run."""
    import fitz
    out: set[int] = set()
    row_hits: dict[int, int] = {}
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:  # noqa: BLE001 — a detector must never sink a check
        return out
    for pno in range(min(max_scan, doc.page_count)):
        page = doc[pno]
        width = page.rect.width
        rows: list[tuple[float, list[tuple[float, str]]]] = []
        for x0, y0, _x1, _y1, w, *_ in page.get_text("words"):
            for y, items in rows:
                if abs(y - y0) <= 2.5:
                    items.append((x0, w))
                    break
            else:
                rows.append((y0, [(x0, w)]))
        hits = 0
        for _y, items in rows:
            if len(items) < 2:
                continue
            items.sort()
            last_x, last_w = items[-1]
            if last_w.strip(".").isdigit() and last_x >= 0.62 * width:
                hits += 1
        row_hits[pno + 1] = hits
        if hits >= _TOC_MIN_ROWS:
            out.add(pno + 1)
    # A listing that runs onto another page often thins out at its edges — Austria
    # 115454 puts 20 rows on page 2 and only 4 on page 1, and leaving page 1 in
    # charged a section for not containing the listing printed on it. Extend only
    # ADJACENT to a page already confirmed at full strength, so a weak threshold can
    # never promote a body page on its own.
    for pno in sorted(out):
        for nb in (pno - 1, pno + 1):
            if nb in row_hits and nb not in out and row_hits[nb] >= _TOC_EDGE_ROWS:
                out.add(nb)
    doc.close()
    return out


# A document-reference stamp is page furniture no matter how few pages carry it.
# Bermuda__166524 prints "Legal - 24320065.2" on pages 1, 2, 15 and 90 — 4 of 93, far
# under `threshold` — while its sibling stamp "4147-1689-3533, v. 6" sits on 89 and is
# caught. The front-matter sections that footer lands in hold only 1-6 words of their
# own, so those six tokens became a whole "silent gap" and cost 9.1 completeness.
#
# Shape, after _boiler_signature turns digit runs into "#":
#     'legal # #'   (the missed one)      '# # # v #'  (the caught one)
# At least half the tokens are digit runs, at least TWO of them, and whatever words
# remain are short. Two digit runs is what keeps 'section #' — plausibly a real heading
# — out of this, while a reference number always splits into several.
STAMP_MIN_DIGIT_RUNS = 2
STAMP_DIGIT_RATIO = 0.5
STAMP_MAX_WORDS = 2
STAMP_MAX_WORD_LEN = 8
STAMP_MIN_PAGES = 2


def _is_reference_stamp(norm: str) -> bool:
    """Is this normalized line-signature a document-reference stamp?"""
    toks = norm.split()
    if not toks:
        return False
    hashes = [t for t in toks if t == "#"]
    words = [t for t in toks if t != "#"]
    return (len(hashes) >= STAMP_MIN_DIGIT_RUNS
            and len(hashes) / len(toks) >= STAMP_DIGIT_RATIO
            and len(words) <= STAMP_MAX_WORDS
            and all(len(w) <= STAMP_MAX_WORD_LEN for w in words))


# A running header/footer is defined by WHERE IT IS PRINTED, not by where it lands in
# the extractor's line order. The zone_top/zone_bottom line counts below are a proxy for
# that, and on a page with a busy masthead the proxy misses: Guatemala 32221 prints
# "4 | Marketing Restrictions - Asset Management" at y=0.96 of the page on all four of
# its pages, but fitz emits page 1's copy at line index 6 -- one past the 6-line zone --
# so the footer was seen on 3 pages of 4, scored 0.75 against the 0.80 threshold, and was
# never recognised. Both sections whose range covered those pages were then charged with
# a silent content gap whose five "missing" tokens were the page number and the footer.
#
# So ask the PDF where the line actually sits, and admit the lines it prints in a page's
# top/bottom band to that page's zone whatever index they landed at.
#
# PER PAGE AND BY POSITION -- never as a set of signatures pooled across the document,
# and not by line text either. A page number signs as "#", and "#" is also the signature
# of every bare number in the body: a pooled set therefore un-gates "#" on every line of
# every page, and on Czech Republic 166819 that stripped a contents list's own "1."
# through "11." numbering off page 2 as though it were furniture. Stripping real content
# does not fix a false gap, it hides one -- that document's gap disappeared for exactly
# the wrong reason. Matching a page against its own band by TEXT is closer but still
# wrong: page 2 of that document prints "4." twice, once in the band and once in the
# body, and the first match wins, which is the body one.
#
# So identify the band lines by their INDEX in the page's own line sequence. That names
# the line the PDF actually printed down there and nothing else.
#
# The band only widens WHERE a line may be counted -- it never lowers `threshold`, so a
# line still has to recur on 80% of pages (or be a reference stamp) before anything is
# stripped. Measured over 12 corpus documents, the 8% band holds running titles,
# document-reference stamps, "CONFIDENTIAL" and page numbers; the only body text
# reaching it is a disclaimer tail on a single cover page, which 1-of-N recurrence
# excludes anyway.
BAND_FRAC = 0.08


def page_band_lines(pdf_path, band_frac: float = BAND_FRAC) -> list[set[int]]:
    """Per page, the indices of the non-blank lines printed in its top/bottom `band_frac`.

    Indices are into the page's non-blank line sequence as pdf_page_texts yields it, so
    this is only meaningful against page text from the SAME extractor (fitz). The index
    comes from fitz's structured traversal while pdf_page_texts uses its flat one; the
    two agree on 6042 of 6048 corpus pages, and a page where they DON'T is returned
    empty rather than guessed at -- a wrong index would strip a wrong line, so the page
    falls back to the line-index zone alone.

    Returns [] if the PDF cannot be read, which callers pass straight through to the
    same fallback. 1-indexed to match pdf_page_texts: [0] is an empty sentinel.
    """
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return []
    out: list[set[int]] = [set()]
    try:
        for page in doc:
            height = page.rect.height
            seq: list[str] = []
            band: set[int] = set()
            for block in (page.get_text("dict").get("blocks", []) if height > 0 else []):
                for line in block.get("lines", []):
                    text = "".join(sp.get("text", "") for sp in line.get("spans", [])).strip()
                    if not text:
                        continue
                    y0, y1 = line["bbox"][1], line["bbox"][3]
                    if not band_frac < (y0 + y1) / 2 / height < 1 - band_frac:
                        band.add(len(seq))
                    seq.append(text)
            flat = [l.strip() for l in page.get_text().split("\n") if l.strip()]
            out.append(band if seq == flat else set())
    finally:
        doc.close()
    return out


def _band_for(band_lines: list[set[int]] | None, pno: int) -> set[int]:
    """Page `pno`'s band line indices, or an empty set when there are none for it."""
    if not band_lines or pno >= len(band_lines):
        return set()
    return band_lines[pno]


def detect_boilerplate_patterns(pages: list[str], threshold: float = 0.8, zone_top: int = 6,
                                zone_bottom: int = 6, band_lines: list[set[int]] | None = None):
    """Find lines that recur near the top/bottom of most pages (doc-control
    stamps, confidentiality footers, running titles) — content that pdf2mdtree
    deliberately doesn't carry into the tree. Digit runs (page numbers, doc
    version numbers) are normalized away before matching so "Page 3 of 57"
    and "Page 4 of 57" count as the same recurring line.

    `band_lines` (from page_band_lines) names, per page, which of its lines the PDF
    prints in that page's top/bottom band; those are in-zone on that page however far
    down the extracted stream they landed. Pass the SAME list to strip_boilerplate.

    Returns (patterns, detected_lines): `patterns` is the set of normalized
    line-signatures classified as boilerplate (feed to strip_boilerplate());
    `detected_lines` is a summary list for reporting.
    """
    n_pages = len(pages) - 1  # pages[0] is the unused sentinel
    if n_pages <= 0:
        return set(), []

    line_pages: dict[str, set[int]] = {}
    line_example: dict[str, str] = {}
    for pno in range(1, len(pages)):
        lines = [l.strip() for l in pages[pno].split("\n") if l.strip()]
        band = _band_for(band_lines, pno)
        zone = lines[:zone_top] + lines[-zone_bottom:]
        zone += [lines[k] for k in band if k < len(lines)]
        seen_this_page = set()
        for raw in zone:
            norm = _boiler_signature(raw)
            if norm in seen_this_page:
                continue
            seen_this_page.add(norm)
            line_pages.setdefault(norm, set()).add(pno)
            line_example.setdefault(norm, raw)

    patterns, detected_lines = set(), []
    for norm, pset in line_pages.items():
        recurring = len(pset) / n_pages >= threshold
        # A reference stamp needs only to recur at all — it is furniture by shape, not
        # by frequency. Restricted to the header/footer zone like everything here.
        stamp = len(pset) >= STAMP_MIN_PAGES and _is_reference_stamp(norm)
        if recurring or stamp:
            patterns.add(norm)
            detected_lines.append({"pattern": norm, "pages": len(pset),
                                   "example": line_example[norm],
                                   "why": "recurring" if recurring else "reference stamp"})

    return patterns, detected_lines


# The cover's boilerplate Disclaimer paragraph, which the summary-AI prompt is TOLD to
# omit (see summary_ai_extract, which strips it from its own completeness score for
# exactly this reason: "counting them would score compliance as loss").
#
# Boilerplate detection cannot reach it. That works by finding lines that RECUR across
# pages -- running headers and footers -- and this notice is printed once, on the cover.
# So every conservation check read it as source text the tree had dropped, and 15 of the
# 22 summary-route documents in the 16-09-26 corpus carried a silent "N word(s) on page 1
# appear nowhere in the tree" finding whose words were the notice the model was
# instructed to leave out.
#
# The three guards are summary_ai_extract's, kept verbatim because they are what stop a
# stray "disclaimer" in body prose from eating a page: the COLON is required (a body
# heading has none), the run is bounded at 600 characters, and it stops at the first
# blank line. The caller adds the fourth -- page 1 only.
COVER_DISCLAIMER = re.compile(r"\bDisclaimer:\s*.{0,600}?(?:\n\s*\n|\Z)",
                              re.IGNORECASE | re.DOTALL)


def summary_ai_route(out_root) -> bool:
    """Was this job extracted by the summary-AI pipeline rather than stages 1-3?

    Read off the rule file the router writes into the job dir at extraction time, which
    is evidence of the route ACTUALLY taken -- a check reading a finished job dir should
    not have to re-derive the routing decision from the product and the content tile.
    """
    return (Path(out_root) / "summary_ai_rule.json").exists()


def strip_cover_disclaimer(pages: list[str], enabled: bool = True) -> list[str]:
    """`pages` with the cover Disclaimer removed from page 1. No-op when not enabled.

    Scoped to the summary route by the caller: a full stages-1-3 memo KEEPS the notice in
    its tree (Malta 181816's front matter carries it), so stripping it there would stop
    the checks noticing if it ever went missing for real.
    """
    if not enabled or len(pages) < 2:
        return pages
    out = list(pages)
    out[1] = COVER_DISCLAIMER.sub("", out[1] or "")
    return out


def strip_boilerplate(pages: list[str], patterns: set[str], zone_top: int = 6, zone_bottom: int = 6,
                      band_lines: list[set[int]] | None = None) -> list[str]:
    """Return a copy of `pages` with lines matching a detected boilerplate
    pattern removed from the header/footer zone of each page. The zone is
    measured in non-blank lines (matching detect_boilerplate_patterns exactly)
    so pages with a blank line or two near the top don't push the real header
    text out of range. `band_lines` widens the zone the same way it does in
    detect_boilerplate_patterns and MUST be the same list, or a pattern found there
    will not be stripped here. It names individual LINES, so one page number in the
    band cannot excuse every bare number on the page."""
    cleaned = [pages[0]]
    for pno in range(1, len(pages)):
        raw_lines = pages[pno].split("\n")
        nonblank_idx = [i for i, l in enumerate(raw_lines) if l.strip()]
        zone_idx = set(nonblank_idx[:zone_top]) | set(nonblank_idx[-zone_bottom:] if zone_bottom else [])
        # Band indices count non-blank lines; zone_idx counts raw ones. Map across.
        zone_idx |= {nonblank_idx[k] for k in _band_for(band_lines, pno)
                     if k < len(nonblank_idx)}
        keep = []
        for i, raw in enumerate(raw_lines):
            stripped = raw.strip()
            if not stripped:
                keep.append(raw)
                continue
            # MUST sign identically to detect_boilerplate_patterns, or a pattern it
            # found will never match here and nothing gets stripped
            norm = _boiler_signature(stripped)
            if i in zone_idx and norm in patterns:
                continue
            keep.append(raw)
        cleaned.append("\n".join(keep))
    return cleaned


# A multi-page table's header row is reprinted at the top of every continuation page.
# The pipeline reconstructs such a table as ONE table with ONE header row, so those
# reprints have no counterpart in the tree and every one of them reads as missing
# content — 3,410 word-occurrences on the 281-page US States privacy survey, 45% of
# everything the coverage check called missing, plus a phantom dropped span at each
# page break inside the table.
#
# The page ranges and per-page bounding boxes come from the extractor's OWN
# tables_manifest, and a line only counts if the SAME line sits in the header band of
# that table's first page. So nothing outside a table the extractor declared can be
# excused, and neither can a repeated cell value ("no explicit requirement" recurs on
# dozens of these pages) unless it is genuinely part of the header row.
HEADER_BAND_PT = 46.0        # how far below a table's top edge its header row reaches
# How far ABOVE a table's recorded top edge to look, on that table's FIRST page only.
#
# The reference set is built from the first page, and on these questionnaires the first
# page is exactly where the extractor's region does NOT contain the header row: the
# region is fitted to the rows it reconstructed, and where the table opens under a
# section heading the printed "Questions | Answers" row is left just above it. Measured
# on 124/Czech Republic 166819, the document this was diagnosed from: its pages 23-45
# table records a top edge of 404.3 on page 23 while the header row prints at 360.0,
# 44.3pt higher — so the reference set held the first QUESTION instead of the header,
# and all 22 reprints on pages 24-45 were discarded as not-a-header. Page 27's is the
# one a reader sees, as "questions answers marketing selling an aif via the aifmd
# marketing passport" reported as a silent content gap.
#
# CONTINUATION pages keep the tight -2.0: there the region starts at the top of the
# page and anything above it is page furniture, not table.
#
# One band's worth of reach, no more, and it cannot admit anything on its own — a line
# found up here still only counts once the SAME line is found in a continuation page's
# header band. Measured over the 124 corpus: reprinted-header pages 548 -> 930, of which
# recognised "Questions" rows 165 -> 370.
HEADER_LOOKUP_ABOVE_PT = HEADER_BAND_PT


def _header_band_lines(doc, bb, pno: int, band: float, above: float = 2.0) -> list[str]:
    """The text lines inside a table's header band on one page, in reading order.

    `above` is how far over the recorded top edge to reach; see HEADER_LOOKUP_ABOVE_PT."""
    if not bb or not (0 < pno <= doc.page_count):
        return []
    lines = []
    for blk in doc[pno - 1].get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            x0, y0, x1, _ = ln["bbox"]
            if y0 < bb[1] - above or y0 > bb[1] + band or x1 < bb[0] or x0 > bb[2]:
                continue
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if txt:
                lines.append(txt)
    return lines


def _reprinted_header_lines(pdf_path, tables: list[dict] | None,
                            band: float = HEADER_BAND_PT):
    """One geometry pass -> [(continuation page number, [reprinted header lines])].

    A line is reprinted only if the SAME line sits in the header band of that table's
    first page, so a repeated cell value ("no explicit requirement" recurs on dozens of
    these pages) is not mistaken for a header. The first page's band reaches a little
    ABOVE the recorded top edge, because that is the one page where the printed header
    row routinely sits outside the region — see HEADER_LOOKUP_ABOVE_PT."""
    if not tables:
        return []
    found = []
    with fitz.open(str(pdf_path)) as doc:
        for t in tables:
            pages = t.get("pages") or []
            if not t.get("multipage") or len(pages) < 2:
                continue
            boxes = t.get("bboxes") or {}
            first = {_boiler_signature(l)
                     for l in _header_band_lines(doc, boxes.get(str(pages[0])), pages[0], band,
                                                 above=HEADER_LOOKUP_ABOVE_PT)}
            if not first:
                continue
            for pno in pages[1:]:
                lines = [l for l in _header_band_lines(doc, boxes.get(str(pno)), pno, band)
                         if _boiler_signature(l) in first]
                if lines:
                    found.append((pno, lines))
    return found


def table_header_repeats(pdf_path, tables: list[dict] | None,
                         band: float = HEADER_BAND_PT) -> dict[int, set[str]]:
    """{page number: {line signatures}} for header rows reprinted on continuation pages.

    Signatures are _boiler_signature()s, so they can be fed to strip_page_lines() the
    same way detected boilerplate is fed to strip_boilerplate().

    Only safe where the comparison is a token COUNT (Test 1). Do not strip these from
    the pages Test 2 diffs per section: removing text re-aligns the sequence matcher and
    invents new spans elsewhere — measured on Private Wealth Armenia 175560, where
    coverage rose 90.4%->93.8% and the flagged-section count went 1->3, a net 13-point
    LOSS. Test 2 uses table_header_units() below instead, which classifies without
    touching the diff."""
    out: dict[int, set[str]] = {}
    for pno, lines in _reprinted_header_lines(pdf_path, tables, band):
        out.setdefault(pno, set()).update(_boiler_signature(l) for l in lines)
    return out


def table_header_units(pdf_path, tables: list[dict] | None,
                       band: float = HEADER_BAND_PT, min_tokens: int = 3) -> list[list[str]]:
    """The reprinted header lines as token lists, for use as restructured-content UNITS.

    The tree holds this text once, in the reconstructed table's single header row, so a
    dropped span made up of it is restructured content rather than loss — the same
    argument already applied to table cells and footnote definitions. Whole lines and
    each page's whole header block are both offered, because a span can straddle a
    wrapped header line ("...data collected, / used and stored")."""
    units, seen = [], set()
    for _pno, lines in _reprinted_header_lines(pdf_path, tables, band):
        for text in lines + [" ".join(lines)]:
            toks = tokenize(text)
            key = "\x01".join(toks)
            if len(toks) >= min_tokens and key not in seen:
                seen.add(key)
                units.append(toks)
    return units


def split_word_punctuation(tokens: list[str]) -> list[str]:
    """Spell hyphens and apostrophes the same way on both sides of a comparison.

    Nearly all the residual noise in these documents is the two sides disagreeing about
    punctuation INSIDE a word, not about content:

      hyphens     the tree holds "closed-ended" as ONE token while the PDF yields
                  "closed" + "ended" -- and the reverse happens too, when the PDF breaks
                  a compound across a line ("formal \u201cblack-/letter law\u201d" on Germany
                  179874 page 128) and the tree holds "black-letter" whole. Splitting
                  BOTH sides removes the disagreement without having to decide which
                  spelling is right, and without deleting anything.
      apostrophes the tree holds "regulator's" as one token; the PDF yields "regulator"
                  + "s". Dropping the apostrophe makes the tree's token "regulators".

    Lifted here from check_text_conservation, which measured it (71 -> 32 -> residue on
    the two documents it was built on, with the injection probe still at zero misses) and
    now imports it back, so the conservation checks and the per-section checks cannot
    drift into normalising a word two different ways.
    """
    out = []
    for t in tokens:
        for part in t.replace("\u2019", "'").replace("'", "").split("-"):
            if part:
                out.append(part)
    return out


def strip_page_lines(pages: list[str], per_page: dict[int, set[str]]) -> list[str]:
    """Remove specific lines from specific pages — the page-scoped counterpart of
    strip_boilerplate, which strips one pattern set from every page's header zone."""
    if not per_page:
        return pages
    cleaned = [pages[0]]
    for pno in range(1, len(pages)):
        sigs = per_page.get(pno)
        if not sigs:
            cleaned.append(pages[pno])
            continue
        cleaned.append("\n".join(l for l in pages[pno].split("\n")
                                 if not l.strip() or _boiler_signature(l.strip()) not in sigs))
    return cleaned


def manifest_tables(out_root) -> list[dict]:
    """The extractor's own record of the tables it found, or [] when there is none
    (a stage-1-only run, or a document rescored from a tree built before manifests)."""
    p = Path(out_root) / "01_stage1_extract" / "tables_manifest.json"
    try:
        return (json.loads(p.read_text()) or {}).get("tables") or []
    except (OSError, ValueError):
        return []


# ---- pipeline output layout ---------------------------------------------
class ContentCompareError(Exception):
    """Base for input-resolution failures.

    These used to be `sys.exit()` calls. That is fine for a CLI but wrong for a
    library: SystemExit derives from BaseException, so it sailed straight through
    the `except Exception` guards in hybrid_extract_ui._run_job and
    run_baseline._validate and killed the worker thread instead of being reported
    as a failed check. Each CLI main() catches these and exits non-zero, so the
    shell behaviour is unchanged."""


class StageDirectoryNotFoundError(ContentCompareError):
    pass


class MissingManifestError(ContentCompareError):
    pass


def run_cli(main_fn) -> None:
    """Entry-point wrapper for the check scripts: turn an input-resolution failure
    back into the clean `message + exit 1` that sys.exit() used to give, instead of
    a traceback. Library callers still get the exception."""
    try:
        main_fn()
    except ContentCompareError as exc:
        sys.exit(str(exc))


def resolve_stage_dir(out_root: Path, stage: int) -> Path:
    hits = sorted(out_root.glob(f"{stage:02d}_*"))
    if not hits:
        raise StageDirectoryNotFoundError(
            f"no {stage:02d}_* stage directory under {out_root}")
    return hits[0]


def resolve_pdf(out_root: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    # A job directory carries its own copy of the source at <out_root>/source.pdf.
    # Prefer it: tables_manifest.json records an ABSOLUTE path captured when the
    # job was first created, so a COPIED job dir (the fault-injection harness, a
    # moved output tree) would otherwise be validated against the original PDF
    # rather than its own — which is exactly why an injected scan_page fault first
    # looked undetectable.
    local = Path(out_root) / "source.pdf"
    if local.exists():
        return local.resolve()
    stage1 = resolve_stage_dir(out_root, 1)
    manifest = stage1 / "tables_manifest.json"
    if manifest.exists():
        src = json.loads(manifest.read_text()).get("source_pdf")
        if src:
            return Path(src)
    raise MissingManifestError(
        f"could not infer source PDF from {stage1} — pass it explicitly with --pdf")


_BREADCRUMB_PDF_RE = re.compile(r"^\*Source:\s*`([^`]+)`", re.MULTILINE)


def resolve_tree_pdf(out_root: Path, stage: int = 3) -> Path:
    """The PDF the tree was actually BUILT from — not always what resolve_pdf returns.

    A job dir holds `source.pdf` and, when the TOC pre-flight repaired its outline,
    `source_repaired.pdf`. resolve_pdf prefers the former, which is right for a
    CONTENT comparison (the page text is identical) and wrong for anything that
    reads the OUTLINE, because repair replaces it wholesale. On
    124/ADGM__170680 the original carries 92 Word auto-bookmarks made from body
    sentences — the first is titled "Where:" — against the repaired copy's 39 real
    section titles, and Stage 1 built the tree from the latter.

    Every node writes the filename it used into its own breadcrumb, so ask the
    tree. Falls back to resolve_pdf when there is no tree yet (the pre-flight runs
    before Stage 1) or no breadcrumb to read."""
    try:
        tree_root = resolve_stage_dir(out_root, stage)
    except StageDirectoryNotFoundError:
        return resolve_pdf(out_root, None)
    for p in sorted(tree_root.rglob("*.md")):
        try:
            m = _BREADCRUMB_PDF_RE.search(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        if m:
            named = Path(out_root) / m.group(1)
            return named if named.exists() else resolve_pdf(out_root, None)
    return resolve_pdf(out_root, None)


def load_table_status(out_root: Path) -> dict:
    """table_id -> status dict (ok, reason, pages, source, rows, cols)."""
    stage2_candidates = sorted(out_root.glob("0*_*mineru*"))
    out = {}
    for stage2 in stage2_candidates:
        for status_path in stage2.glob("tables/*/status.json"):
            try:
                st = json.loads(status_path.read_text())
                out[st["table_id"]] = st
            except (json.JSONDecodeError, KeyError):
                continue
    return out


# ---- terminal formatting --------------------------------------------------
_TTY = sys.stdout.isatty()


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if _TTY else s


BOLD = lambda s: _c("1", s)
DIM = lambda s: _c("2", s)
GREEN = lambda s: _c("32", s)
RED = lambda s: _c("31", s)
YELLOW = lambda s: _c("33", s)
CYAN = lambda s: _c("36", s)


def banner(msg: str):
    line = "─" * max(4, 72 - len(msg))
    print(f"\n{BOLD(CYAN('▶ ' + msg))} {DIM(line)}", flush=True)
