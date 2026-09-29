#!/usr/bin/env python3
"""
pdf2mdtree — Stage 1 of the hybrid pipeline. Convert a structured, born-digital
PDF into a folder tree of markdown files, preserving heading hierarchy and
footnotes, and leaving every detected table as a placeholder for Stage 2.

Requires: pymupdf  (pip install pymupdf --break-system-packages)

Usage:
  python3 pdf2mdtree.py input.pdf -o output_dir [--depth 3]
                        [--no-tables] [--no-snapshots]

Tables are detected here (geometry/ruling) but never rendered: this script's own
cell-splitting does a poor job on ruled-but-columnless tables. Each detected
table becomes a visible `<!-- TABLE:table_NNN -->` placeholder in the tree, every
table page gets a snapshot for visual reference, and tables_manifest.json records
each table's id/pages/bbox for scripts/hybrid_extract.py (Stage 2, MinerU) to
fill in.

How it works:
  1. Detects typography automatically: body font size (most common), footnote
     size (notably smaller), heading size (notably larger), and repeated
     header/footer lines (stripped).
  2. Uses the PDF's bookmark outline as the structure source. If absent, falls
     back to font-size-based heading detection (lower confidence, flagged in
     the report).
  3. Splits content into folders (down to --depth levels) and files, converts
     superscript references into markdown footnotes, writes README indexes,
     per-file source-page references, and a CONVERSION_REPORT.md with
     verification stats.
  4. Pages whose tables are too complex to reconstruct (multi-page tables with
     partial ruling, diagram-heavy pages) additionally get a rendered PNG
     snapshot embedded next to the extracted text.
"""
import argparse, collections, json, os, re, sys, unicodedata

try:
    import fitz
except ImportError:
    sys.exit("pymupdf missing. Run: pip install pymupdf --break-system-packages")


# ---------------- helpers ----------------
def split_after_title(raw, title_norm, min_tail_words=20):
    """-> (title_part, tail) for a paragraph whose text OPENS with a heading title.

    A heading and its body are frequently one PDF text block, so the matched
    paragraph holds both. content_between() skips the matched paragraph, which threw
    the body away with the title: Mexico__183466 p5 lost the whole of "1.4 Executive
    Summary" -- 562 words, present in `paras` at 3,718 characters, absent from the
    output. The pipeline's own comment on loose_fallback names this ("the paragraph
    ... is consumed as a heading and disappears from the output entirely").

    Matching happens on norm()'d text (no separators), so the cut point is found by
    walking the RAW text until the title's normalised characters are consumed.

    Returns (raw, "") unless the tail is substantial: a short trailing fragment is far
    more likely a mis-split heading than lost body, and re-emitting it would add noise
    to every heading in the corpus. Nothing is discarded either way -- today the whole
    paragraph is dropped, so this can only ever put text back.
    """
    if not raw or not title_norm:
        return raw, ""
    want = len(title_norm)
    got = 0
    cut = None
    for i, ch in enumerate(raw):
        if norm(ch):
            got += 1
            if got >= want:
                cut = i + 1
                break
    if cut is None or cut >= len(raw):
        return raw, ""
    head, tail = raw[:cut], raw[cut:].strip()
    if len(tail.split()) < min_tail_words:
        return raw, ""
    return head.strip(), tail


def norm(s):
    s = unicodedata.normalize('NFKC', s)
    s = s.replace('–', '-').replace('—', '-').replace('’', "'")
    s = re.sub(r'\[\^\d+\]', '', s)
    return re.sub(r'[^a-z0-9]+', '', s.lower())


def normspace(s):
    return re.sub(r'\s+', ' ', s).strip()


# ---- inline emphasis ---------------------------------------------------------
# Bold and italic are already read off every span (is_style_heading uses bit 4 for
# the bold-caps heading test) and were then thrown away: emit() joins a line's raw
# span text and normspace()s it, so a defined term printed in bold arrives
# indistinguishable from the prose around it. In this corpus that is not
# decoration — bold marks defined-term introductions ("(**DPA**)",
# "**Bahamas-Based Funds**") and clause cross-references ("**D1 Privacy notice**",
# 35 occurrences in UK 179582), which is exactly what a downstream term/reference
# pass has to find.
#
# Serialised PER PARAGRAPH, not per line. A styled run routinely continues across
# a line break — 585 of them in UK 179582, 64 in Australia 181814 — and closing
# the markers at each line end would emit "**wording in **" + "**Section D**":
# visible asterisks, not emphasis. Grouping consecutive same-style runs over the
# whole paragraph merges those back into one run.
#
# Markers never wrap whitespace. CommonMark needs the delimiter against a
# non-space character, and spans here routinely carry their trailing space INSIDE
# the styled run ('Collateral giver '), which would otherwise render literally.
_EMPH = {(True, False): '**', (False, True): '*', (True, True): '***'}
_BULLET_GLYPHS = '•▪◦−–'
# The invariant that makes this safe downstream: every text comparison in the QA
# stack is punctuation-insensitive (lib_content_compare.TOKEN_RE, check_scorecard's
# norm(), hybrid_extract's verbatim shingles, and the .split() word counts), so
# markers are invisible to all of them — PROVIDED the alphanumeric token sequence
# is untouched. emit() asserts exactly that and falls back to the plain join if it
# ever fails, so a serialisation bug can only cost emphasis, never content.
_ALNUM_RUN_RE = re.compile(r'[A-Za-z0-9]+')


def _fuse_whitespace_runs(runs):
    """Whitespace carries no visible style, so let a whitespace-only run adopt its
    neighbours' style when they agree.

    Without this, the run-continues-across-a-line-break case this whole function
    exists for still breaks: the ' ' emit() inserts between two lines is unstyled,
    so it splits one bold run into two groups and emits '**wording in** **Section
    D**' — two runs where the PDF has one. The same shape occurs mid-line whenever
    a styled span is followed by a bare space span."""
    out = list(runs)
    for i, (t, _b, _it) in enumerate(out):
        if t.strip():
            continue
        prev = next((out[j] for j in range(i - 1, -1, -1) if out[j][0].strip()), None)
        nxt = next((out[j] for j in range(i + 1, len(out)) if out[j][0].strip()), None)
        if prev and nxt and (prev[1], prev[2]) == (nxt[1], nxt[2]):
            out[i] = (t, prev[1], prev[2])
    return out


def _group_runs(runs):
    """runs -> [[chunk, bold, italic]] with consecutive same-style runs merged."""
    out = []
    for text, bold, italic in runs:
        if out and (out[-1][1], out[-1][2]) == (bold, italic):
            out[-1][0] += text
        else:
            out.append([text, bold, italic])
    return out


def _collapse_touching(groups):
    """Two styled groups that ABUT with no whitespace between them emit a
    delimiter run markdown cannot parse.

    Bahamas 183503 p6 prints "Dealing in Capital Markets Instruments:" as italic,
    then bold-italic, then italic — three groups touching, which serialised as
    '*Dealing in* ***Capital Markets Instruments****:*'. Those four asterisks
    render literally; the reader sees the markup.

    Nested emphasis ('*a **b** c*') would express it exactly, but that needs a
    render tree rather than a flat scan. So a chain of touching styled groups
    collapses to the style they all SHARE — italic here — which is always
    representable and never claims a style the source does not have. A chain
    sharing nothing collapses to plain."""
    i = 0
    while i < len(groups):
        if not _EMPH.get((groups[i][1], groups[i][2])):
            i += 1
            continue
        j = i
        while (j + 1 < len(groups)
                and _EMPH.get((groups[j + 1][1], groups[j + 1][2]))
                and groups[j][0] and not groups[j][0][-1].isspace()
                and groups[j + 1][0] and not groups[j + 1][0][0].isspace()):
            j += 1
        if j > i:
            shared = (all(g[1] for g in groups[i:j + 1]),
                      all(g[2] for g in groups[i:j + 1]))
            for g in groups[i:j + 1]:
                g[1], g[2] = shared
        i = j + 1
    return groups


def emphasise(runs):
    """[(text, bold, italic)] -> markdown, consecutive same-style runs merged.

    A run is left unstyled when its text is only whitespace, is a bare bullet
    glyph (the leading-glyph rewrite in emit() has to still see it), or already
    contains an asterisk (escaping it is not worth the ambiguity it invites)."""
    groups = _collapse_touching(_group_runs(_fuse_whitespace_runs(runs)))
    out = []
    for chunk, bold, italic in _group_runs([tuple(g) for g in groups]):
        mark = _EMPH.get((bold, italic))
        core = chunk.strip()
        if mark and core and '*' not in core and core not in _BULLET_GLYPHS:
            lead = chunk[:len(chunk) - len(chunk.lstrip())]
            trail = chunk[len(chunk.rstrip()):]
            out.append(f'{lead}{mark}{core}{mark}{trail}')
        else:
            out.append(chunk)
    return ''.join(out)


# ---- nested bullet levels -----------------------------------------------------
# A bullet's DEPTH is carried only by the x-position of its glyph. emit() rewrites the
# glyph to a flat "- " and normspace() throws the position away, so a document nesting
# several levels deep arrived as one flat list: Bahamas 183503 p5 prints an item at
# x=74.9 and a sub-item at x=92.9, and both came out as "- ".
#
# The ladder is read from the whole document rather than one page: a single page rarely
# shows every level in use, and per-page tiers would make the same visual indent mean
# level 1 on one page and level 2 on the next.
#
# Every threshold below exists to REFUSE rather than to guess. Measured glyph positions:
# Australia 181814 steps 54/90/126/162/198 -- a clean 36pt ladder; Bahamas 183503 gives
# 57/75/81/93/99/111/117/133/135 in its left column plus 382..418 in its right, because
# its questionnaire pages are two-column. A wrong nesting is worse than no nesting (it
# asserts a containment the document does not have), so anything that does not look like
# a ladder falls back to today's flat output.
BULLET_TIER_TOL = 6.0      # pt: glyphs within this of a tier's start are that tier
BULLET_MIN_STEP = 9.0      # pt: tiers closer than this are not distinct indent levels
BULLET_MAX_LEVELS = 5      # deeper than this is noise, not nesting
BULLET_COLUMN_GAP = 150.0  # pt: a jump this big is a page COLUMN, not an indent step
# Dropping RARE positions before the tests below was tried and reverted. It was meant
# to rescue UK 179582, whose 628 items sit 552-at-x=48.2 with a scattered tail over
# eight further positions -- but a 2%-of-items floor did not rescue it (its tail still
# spans a two-column glossary) and it cost Bahamas 183503 the nesting this exists for:
# the x=74.9 level holds only 2 of that document's 51 items, because most of its pages
# are deferred tables, and trimming it flattened exactly the sub-items in question.
# Rarity is not noise in a document whose prose is mostly elsewhere.
#
# UK 179582 refusing is the RIGHT answer, not a gap: 88% of its items share one
# position, so it has no ladder to read.


def bullet_tiers(xs):
    """Glyph x-positions -> (tiers, reason).

    `tiers` is ascending tier starts, or None when the positions are not a ladder —
    in which case `reason` names the test that refused. Stated rather than silent: a
    fallback to flat bullets is otherwise indistinguishable from a document that has
    no nesting, and the two want opposite fixes."""
    if not xs:
        return None, 'no list items'
    keep = []
    for x in sorted(xs):
        if not keep or x - keep[-1] > BULLET_TIER_TOL:
            keep.append(x)
    if len(keep) < 2:
        return None, 'one indent level only'
    steps = [b - a for a, b in zip(keep, keep[1:])]
    if any(s >= BULLET_COLUMN_GAP for s in steps):
        return None, f'page-column gap ({max(steps):.0f}pt) is not an indent step'
    if any(s < BULLET_MIN_STEP for s in steps):
        return None, f'levels {min(steps):.1f}pt apart are not distinct indents'
    if len(keep) > BULLET_MAX_LEVELS:
        return None, f'{len(keep)} levels is noise, not nesting'
    return keep, None


def bullet_level(x, tiers):
    """Which tier `x` belongs to — the last tier at or left of it."""
    lvl = 0
    for i, t in enumerate(tiers):
        if x >= t - BULLET_TIER_TOL:
            lvl = i
    return lvl


def apply_bullet_nesting(paras, report):
    """Indent each list item to the level its glyph's x-position implies.

    Rewrites the "- " prefix in place, on both 'text' and the emphasised 'md', so the
    two stay identical apart from emphasis markers. Indents with TWO spaces per level:
    markdown needs the continuation to line up under the parent's text, and "- " is two
    characters wide.

    Nothing is added or removed — only leading whitespace — so word counts, the token
    guard in emit() and every downstream comparison are untouched."""
    xs = [round(p['bullet_x'], 1) for p in paras if 'bullet_x' in p]
    tiers, why = bullet_tiers(xs)
    report['bullet_items'] = len(xs)
    report['bullet_tiers'] = [round(t, 1) for t in tiers] if tiers else None
    report['bullet_flat_reason'] = why
    # The raw positions, so a refusal above can be understood (and the thresholds
    # re-tuned) from the report alone rather than by re-instrumenting the scan.
    report['bullet_x_counts'] = dict(sorted(collections.Counter(xs).items()))
    if not tiers:
        return
    nested = 0
    for p in paras:
        if 'bullet_x' not in p:
            continue
        lvl = bullet_level(p['bullet_x'], tiers)
        if not lvl:
            continue
        pad = '  ' * lvl
        for key in ('text', 'md'):
            if key in p and p[key].startswith('- '):
                p[key] = pad + p[key]
        nested += 1
    report['bullet_items_nested'] = nested


def slug(s, maxlen=48):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    s = re.sub(r'^[A-Z]\.\s*|^\d+(\.\d+)*\.?\s*|^\(\w+\)\s*', '', s)
    s = re.sub(r'[^a-zA-Z0-9]+', '-', s).strip('-').lower()
    if len(s) > maxlen:
        s = s[:maxlen].rsplit('-', 1)[0]
    return s or 'section'


def band_key(text):
    return re.sub(r'\d+', '#', normspace(text)).lower()


def table_placeholder(table_id, pages):
    """Human-readable, machine-matchable stand-in for a table left unrendered
    in --defer-tables mode. The HTML-comment sentinels let a downstream stage
    find-and-replace the whole block once the real table is extracted
    elsewhere, without depending on the placeholder's visible wording."""
    pages_str = f"page {pages[0]}" if len(pages) == 1 else f"pages {pages[0]}–{pages[-1]}"
    return (f"<!-- TABLE:{table_id} -->\n"
            f"> **[TABLE PENDING: {table_id} — {pages_str} — "
            f"awaiting Stage 2 MinerU extraction]**\n"
            f"<!-- /TABLE:{table_id} -->")


def clean_rows(rows):
    """Remove merged-cell artifacts: duplicated adjacent cells, empty
    columns/rows that looser detection strategies produce."""
    rows = [list(r) for r in rows]
    for r in rows:
        for i in range(len(r) - 1, 0, -1):
            if r[i] and r[i] == r[i - 1]:
                r[i] = ''
    ncols = max((len(r) for r in rows), default=0)
    rows = [r + [''] * (ncols - len(r)) for r in rows]
    keep_cols = [i for i in range(ncols) if any(r[i] for r in rows)]
    rows = [[r[i] for i in keep_cols] for r in rows]
    return [r for r in rows if any(r)]


def detect_fractions(page, dd, table_bboxes=()):
    """Stacked fractions (numerator over a bar over denominator, optional
    trailing factor like '× 100') read wrongly in linear text order. Detect
    them via their fraction-bar rule and rewrite as an inline formula.

    Returns (consumed_line_ids, formulas, unparsed):
      consumed_line_ids  set of id(line) whose text is now part of a formula and
                         must not be emitted as prose.
      formulas           DUAL-KEYED: int key -> the formula text, and
                         ('line', line_id) -> that int key. The caller emits a
                         formula when it reaches the first of its lines in
                         reading order.
      unparsed           True if a fraction-like region was found but rejected as
                         a wrapped table cell — the caller forces a page snapshot.
    """
    consumed, formulas = set(), {}
    unparsed = False
    try:
        rules = [dr['rect'] for dr in page.get_cdrawings()
                 if dr.get('rect') and 30 < dr['rect'][2] - dr['rect'][0] < 360
                 and dr['rect'][3] - dr['rect'][1] < 3]
    except Exception:
        return consumed, formulas, unparsed
    if not rules:
        return consumed, formulas, unparsed
    tls = []
    for b in dd['blocks']:
        if b['type'] != 0:
            continue
        for l in b['lines']:
            t = normspace(''.join(s['text'] for s in l['spans']))
            if t:
                tls.append((l['bbox'], t, id(l)))
    def join_frags(frags):
        """x-sort line fragments and join, gluing directly-adjacent ones
        (PDFs often split words like 'Denominato'+'r')."""
        frags = sorted(frags, key=lambda f: f[0][0])
        out, prev_x2 = '', None
        for bb, t, lid in frags:
            if prev_x2 is not None and bb[0] - prev_x2 >= 2:
                out += ' '
            out += t
            prev_x2 = bb[2]
        # Symbol-font operators used by Word equations
        out = out.replace('', '×').replace('', '÷').replace('', '/')
        out = re.sub(r'([×÷=+])(?=\S)', r'\1 ', out)
        out = re.sub(r'(?<=\S)([×÷=+])', r' \1', out)
        return normspace(out)

    for r in rules:
        # rules that are part of a detected table are row separators
        if any(tb[0] - 4 <= r[0] and r[2] <= tb[2] + 4 and
               tb[1] - 4 <= r[1] <= tb[3] + 4 for tb in table_bboxes):
            continue
        rw = r[2] - r[0]
        rc = (r[0] + r[2]) / 2
        num, den, lhs, rhs = [], [], [], []
        for bb, t, lid in tls:
            xo = min(bb[2], r[2]) - max(bb[0], r[0])
            cy = (bb[1] + bb[3]) / 2
            if xo > 0 and -2 <= r[1] - bb[3] < 9:
                num.append((bb, t, lid))
            elif xo > 0 and -2 <= bb[1] - r[1] < 9:
                den.append((bb, t, lid))
            elif abs(cy - r[1]) < 12 and bb[2] <= r[0] + 3 and r[0] - bb[2] < 90:
                lhs.append((bb, t, lid))       # e.g. "Threshold % ="
            elif abs(cy - r[1]) < 12 and bb[0] >= r[2] - 3 and bb[0] - r[2] < 60:
                rhs.append((bb, t, lid))       # e.g. "× 100"
        if not num or not den:
            continue
        # sanity: numerator/denominator must sit on the bar, roughly centred
        # and no wider than it (otherwise this is a table row separator)
        ok = True
        for frags in (num, den):
            x0 = min(f[0][0] for f in frags)
            x1 = max(f[0][2] for f in frags)
            if x0 < r[0] - 20 or x1 > r[2] + 20 or abs((x0 + x1) / 2 - rc) > 25:
                ok = False
        if not ok:
            continue
        # isolation: a wrapped table cell continues directly below the
        # denominator at the same left edge; ordinary body text further to
        # the left is fine
        den_bot = max(f[0][3] for f in den)
        ids = {f[2] for f in num + den + lhs + rhs}
        # A genuine threshold formula is an EQUATION — its fragments contain "=" and/or
        # "× 100". Table row-rules (adjacent cells masquerading as num/den/lhs/rhs) have
        # neither, so skip them BEFORE the crowded/unparsed test — otherwise a table row
        # falsely trips frac_unparsed and forces a spurious page snapshot.
        _eqtext = ' '.join(t for _, t, _ in num + den + lhs + rhs)
        if not any(op in _eqtext for op in ("=", "×")):
            continue
        crowded = any(lid not in ids and -1 < bb[1] - den_bot < 12 and
                      abs(bb[0] - r[0]) < 15 and bb[2] - bb[0] < rw * 2
                      for bb, t, lid in tls)
        if crowded:
            unparsed = True
            continue
        f = f"({join_frags(num)} ÷ {join_frags(den)})"
        if lhs:
            f = f"{join_frags(lhs)} {f}"
        if rhs:
            f = f"{f} {join_frags(rhs)}"
        # NOTE: no second equation gate here. The check above already requires "=" or
        # "×" in the raw fragment text, and join_frags only ever ADDS characters
        # (private-use operators -> ×/÷, space padding), so anything reaching this
        # point necessarily still satisfies it.
        key = len(formulas)
        formulas[key] = f"**Formula:** {f}"
        for _, _, lid in num + den + lhs + rhs:
            consumed.add(lid)
            # map each consumed line to its formula; the first one reached
            # in reading order emits it
            formulas.setdefault(('line', lid), key)
    return consumed, formulas, unparsed


def detect_tables(page, heading_ys=()):
    """Ruled tables worth converting to markdown, plus a flag saying whether
    the page is visually complex enough to deserve a rendered snapshot
    (coarse/unreconstructable tables, diagram-heavy pages).

    Tries strict line detection first, then a looser pass (tables ruled only
    horizontally). Drops tables nested inside a bigger one and huge-celled
    'layout boxes' that read better as prose.

    `heading_ys` are the y positions the OUTLINE points to on this page (see
    outline_ys in the caller). They only ever narrow the STRONG_RULE escape
    hatch below; every other decision here is unchanged when they are absent."""
    snapshot = False
    diagram_heavy = False
    try:
        drawings = page.get_cdrawings()
        if len(drawings) >= 25:
            diagram_heavy = True
        # cheap pre-filter: a ruled table needs several vector lines; prose
        # pages (0-2 drawings, e.g. just the footnote separator) can skip
        # the expensive find_tables pass entirely
        if len(drawings) < 3:
            return [], diagram_heavy, []
        found = page.find_tables(strategy='lines_strict').tables
        if not found:
            # tables ruled only horizontally: look for >=3 wide horizontal
            # rules sharing a left edge (row separators) before paying for
            # the looser detection pass — this excludes link underlines etc.
            xs = {}
            for drr in drawings:
                r = drr.get('rect')
                if r and r[2] - r[0] > 150 and r[3] - r[1] < 3:
                    xs[round(r[0] / 5)] = xs.get(round(r[0] / 5), 0) + 1
            if not any(c >= 3 for c in xs.values()):
                return [], diagram_heavy, []
            found = page.find_tables(strategy='lines').tables
            # Horizontal-only ruling gives NO column boundaries, so cell
            # extraction garbles multi-column text (merged/interleaved cells —
            # e.g. the US-state comparison tables). Preserve the page as a
            # snapshot and let its text flow as linear prose instead of emitting
            # a broken markdown grid. (lines_strict grids above are unaffected.)
            if found:
                # Snapshot ONLY the regions with real column structure (>=2
                # whitespace-separated columns). A single-column ruled block
                # (definition list / ruled prose, e.g. "category → description")
                # has no 2D layout to lose, so let its text flow as plain prose
                # instead of an unnecessary snapshot.
                #
                # Judged PER TABLE, never over the union span of all of them.
                # Two stacked tables separated by a heading (very common: one
                # table's tail at the top of a page, a new section heading, then
                # the next table) together span nearly the whole page — and the
                # FULL-WIDTH prose sitting between them fills the very gutter
                # _colbounds looks for, collapsing the union to one column. That
                # scored the whole page as unstructured and returned nothing at
                # all: no table, no snapshot, no bbox, so neither table ever
                # reached MinerU and both flowed out as undifferentiated prose.
                # One weak region must not veto its neighbour.
                multi = []
                for t in found:
                    rsp = _tspans(page, t.bbox[1], t.bbox[3])
                    ncols = (len(_colbounds(rsp, int(page.rect.width))) - 1) if rsp else 0
                    if ncols >= 2:
                        multi.append(tuple(t.bbox))
                # STRONGLY RULED but no column count: defer it anyway.
                #
                # _colbounds and _hdr_cols both measure columns from whitespace, and
                # neither can see a tight gutter. Australia's appendix 5 (p150-153) is a
                # real two-column table whose gutter is 7 units while a gap INSIDE its
                # right column — between "(a)" and "derivatives;" — is 23. So width
                # cannot separate the boundary from the artefact in either direction:
                # at 12 the page reads as one column, at 7 every list marker becomes a
                # column (measured: Argentina's uniqueness 91.3 -> 56.8).
                #
                # When a block is ruled this heavily the ruling has already answered
                # the question that matters — this is a table — so hand it to MinerU and
                # let it work the columns out from the rendered page, which is the one
                # thing it is better at than geometry. Cost is a snapshot and a MinerU
                # region on ruled prose that would otherwise have flowed as text;
                # nothing is lost either way, since MinerU renders that fine too.
                if not multi and len(_hrules(page)) >= STRONG_RULE_COUNT:
                    _rs = _hrules(page)
                    _xs = _hrule_xspan(page)
                    _x0, _x1 = _xs if _xs else (0.0, page.rect.width)
                    # AN OUTLINE HEADING IS A HARD BOUNDARY. The band above is
                    # first-rule to last-rule, which assumes every rule between
                    # them is a row separator. When a document rules its HEADINGS
                    # instead, that assumption swallows the page: Germany (Data
                    # Privacy)__180656 p18 has 8 rules spread over 85% of the
                    # height — section dividers, not row separators — so the band
                    # became y71-784 and the whole body was deferred. Measured
                    # over the document: 158 of 164 regions came from this hatch,
                    # covering 162 of 328 pages, and 292 of the 293 outline
                    # headings that could not be located in the text were sitting
                    # inside one of them. A heading printed inside a deferred
                    # region is never seen as text, so it can never be matched —
                    # the census delivered 193 of 514 sections.
                    #
                    # The presence of a heading destination strictly inside the
                    # band is itself the evidence that these rules are not row
                    # separators, so the band is cut there and each piece must
                    # earn deferral on its own count and span. A real table
                    # between two headings still passes; ruled prose no longer
                    # does. Australia__181814's appendix 5 — the case this hatch
                    # was written for — has no outline heading inside its band
                    # and is unaffected.
                    _cuts = [y for y in heading_ys if _rs[0] < y < _rs[-1]]
                    _edges = [_rs[0]] + sorted(_cuts) + [_rs[-1]]
                    for _a, _b in zip(_edges, _edges[1:]):
                        _seg = [r for r in _rs if _a <= r <= _b]
                        if (len(_seg) >= STRONG_RULE_COUNT and
                                _seg[-1] - _seg[0] >= STRONG_RULE_SPAN_FRAC * page.rect.height):
                            multi.append((float(_x0), float(_seg[0]),
                                          float(_x1), float(_seg[-1])))
                if multi:
                    # multi-column horizontal-only -> snapshot; return bboxes so
                    # detect_fractions doesn't mistake row-rules for fraction bars
                    return [], True, multi
                return [], False, []   # single-column -> prose, no snapshot
    except Exception:
        return [], diagram_heavy, []
    cand = []
    for t in found:
        raw_rows = t.extract()
        raw_ncols = max((len(r) for r in raw_rows), default=0)
        rows = clean_rows([[('' if c is None else str(c)).strip() for c in r]
                           for r in raw_rows])
        # hoist leading single-cell caption rows out of the table
        caption = ''
        while len(rows) > 2 and sum(1 for c in rows[0] if c) == 1:
            caption = (caption + ' ' + next(c for c in rows[0] if c)).strip()
            rows = clean_rows(rows[1:])
        ncols = max((len(r) for r in rows), default=0)
        nonempty = sum(1 for r in rows for c in r if c)
        raw_maxcell = max((len(str(c)) for r in raw_rows for c in r if c),
                          default=0)
        if len(rows) >= 2 and ncols >= 2 and nonempty >= 4:
            cand.append((tuple(t.bbox), rows, caption, raw_maxcell))
        elif raw_ncols >= 2 and nonempty >= 1 and t.bbox[3] - t.bbox[1] > 25:
            # multi-column region we could not reconstruct (e.g. continuation
            # page of a multi-page table) — single-cell callout boxes are just
            # bordered prose and don't count
            snapshot = True
    cand.sort(key=lambda c: -(c[0][2] - c[0][0]) * (c[0][3] - c[0][1]))
    keep = []
    for bb, rows, caption, raw_maxcell in cand:
        if any(k['bbox'][0] <= bb[0] and k['bbox'][1] <= bb[1] and
               k['bbox'][2] >= bb[2] and k['bbox'][3] >= bb[3] for k in keep):
            continue  # nested inside a kept table
        if raw_maxcell > 3000:
            snapshot = True
            continue  # pathological layout box, not a data table
        if raw_maxcell > 1500:
            snapshot = True  # kept, but very coarse — snapshot aids fidelity
        keep.append({'bbox': bb, 'rows': rows, 'caption': caption,
                     'done': False})
    return keep, snapshot, [tuple(t.bbox) for t in found]


# ---------------- multi-page comparison-table reconstruction ----------------
# Wide state-by-state comparison tables are landscape, ruled only horizontally,
# and span many pages. find_tables can't map their whitespace columns, so instead:
# horizontal rules -> row bands; vertical whitespace gutters -> columns; detect the
# column set ONCE (richest page) and apply across the whole run; stitch pages
# (dedup repeated headers, merge rows split at a page break) into ONE markdown table.
def _hrules(page):
    ys = set()
    for d in page.get_cdrawings():
        r = d.get('rect')
        if r and (r[2] - r[0]) > 150 and (r[3] - r[1]) < 3:
            ys.add(round(r[1], 1))
    return sorted(ys)


def _hrule_xspan(page):
    """Horizontal extent of the row rules — i.e. how wide the table actually is.

    _hrules keeps only y. The x span matters for the per-page bbox recorded in
    tables_manifest.json: using the full page width instead makes the IoU against
    MinerU's (correctly narrow) table block far too low, so a perfectly good match
    reads as 'uncertain'. Returns None when there are no rules to measure."""
    xs = [(d['rect'][0], d['rect'][2]) for d in page.get_cdrawings()
          if d.get('rect') and (d['rect'][2] - d['rect'][0]) > 150
          and (d['rect'][3] - d['rect'][1]) < 3]
    return (min(x0 for x0, _ in xs), max(x1 for _, x1 in xs)) if xs else None


def _tspans(page, y0, y1):
    out = []
    for b in page.get_text('dict')['blocks']:
        if b['type'] != 0:
            continue
        for l in b['lines']:
            for s in l['spans']:
                if s['text'].strip() and y0 - 2 <= s['bbox'][1] <= y1 + 2:
                    out.append((s['bbox'][0], s['bbox'][1], s['bbox'][2], s['text'], s['size']))
    return out


# Narrowest column gutter, in integer x-coverage units, that still counts as a
# column boundary. See docs/COLUMN_GUTTER_THRESHOLD.md — this was 12 and is the
# single number that decides whether a horizontally-ruled block is treated as a
# table or as prose.
#
# Measured, not guessed: Australia__181814's appendix 5 (pages 150-153) is a real
# two-column table whose gutter reads as 7 here. One over-long left cell
# ("Denmark—if regulated by the Danish ", ending at x=223.6) reaches within 8.3pt
# of the right column at x=231.9 — and because coverage is projected over the WHOLE
# band, that single line closes the gutter for every row beneath it. At 12 the page
# scored one column, was never deferred, and 1,188 words of regime-to-relief
# pairings flowed out as linear prose.
MIN_COL_GUTTER = 12

# A horizontally-ruled block this heavily ruled, spanning this much of the page,
# is a table whatever the whitespace projection says about its columns.
STRONG_RULE_COUNT = 4
STRONG_RULE_SPAN_FRAC = 0.4


def _colbounds(sp, W):
    """Column boundaries for a band of spans: [left, ...gutter midpoints..., right].

    Projected over the whole band, so one over-long cell can erase a gutter for every
    row beneath it. That is a known limitation and NOT fixable by lowering
    MIN_COL_GUTTER or by counting per row — see docs/COLUMN_GUTTER_THRESHOLD.md for
    the measurement that rules both out."""
    cov = bytearray(W + 2)
    for x0, y0, x1, t, sz in sp:
        for xx in range(max(0, int(x0)), min(W, int(x1)) + 1):
            cov[xx] = 1
    first, last = int(min(s[0] for s in sp)), int(max(s[2] for s in sp))
    guts, run = [], None
    for xx in range(first, last + 1):
        if not cov[xx]:
            run = xx if run is None else run
        elif run is not None:
            if xx - run >= MIN_COL_GUTTER:
                guts.append((run, xx))
            run = None
    return [first] + [(a + b) // 2 for a, b in guts] + [last + 2]


def _hdr_cols(page, rules):
    """Column bounds from the header (first) row band. Data rows wrap and fill the
    inter-column gaps, so a whole-region gutter projection collapses adjacent columns
    (e.g. "Main data privacy law" + "Breach notification law" -> one column). The
    header row's cells stay short and cleanly separated, so they recover the true
    column count. Bounds are the midpoints of the gaps between header cells."""
    if len(rules) < 2:
        return None
    sp = sorted(_tspans(page, rules[0], rules[1]), key=lambda s: s[0])
    if not sp:
        return None
    cells = []  # [min_x, max_x] per header cell
    for x0, y0, x1, t, sz in sp:
        if cells and x0 - cells[-1][1] < 25:   # < 25px gap -> same cell (intra-cell word spacing)
            cells[-1][1] = max(cells[-1][1], x1)
        else:
            cells.append([x0, x1])
    if len(cells) < 2:
        return None
    first = int(cells[0][0])
    last = int(max(c[1] for c in cells))
    mids = [int((cells[i][1] + cells[i + 1][0]) / 2) for i in range(len(cells) - 1)]
    return [first] + mids + [last + 2]


def _row_is_titled(row, titles, title_exacts=None):
    """True if any cell in `row` is (or closely matches) one of the document's
    own outline titles — e.g. a sub-heading like "(b) Opt-out methods" that a
    column-gutter split happened to place in what looks like a table's key
    column. Real table-cell content (a state name, a short data value) never
    coincides with a full outline title, so this is safe even when the
    matching text is body-sized rather than heading-sized (see _page_bands).

    An EXACT whole-cell match to a title needs no length bar, and the >= 8 bar
    below is why it used to. The bar exists so a short FRAGMENT cannot claim to
    be a heading by prefix — but a cell that IS the title is not a fragment.
    "LICENCE" is seven characters: clause 10 of Norway 171186, Spain 176285,
    Sweden 174961, Belgium 163341, Germany 179874, Ireland 175625 and
    Netherlands 156999 is printed as "10." and "LICENCE" in two columns of one
    band, mid-page, between clause 9's table and clause 10's own. The band was
    not rejected, so nothing clamped the run's start past it, the placeholder
    was emitted at the top of the page and clause 10's table rendered inside
    clause 9 — leaving "10 Licence" as page images and nothing else.

    The per-line pass already made exactly this exception for exactly this
    document (see title_exacts and the _is_outline_title note beside it); this
    is the same judgement, on the band scanner that feeds the run builder.
    title_exacts is empty for an outline that is really a dump of body text, so
    the protection that guards the per-line pass guards this too."""
    if not titles:
        return False
    title_norms, title_prefixes = titles
    exacts = title_exacts or frozenset()
    for cell in row:
        c = cell.strip()
        nc = norm(c)
        if len(nc) >= 4 and nc in exacts:
            return True
        if len(c) < 8:
            continue
        if nc in title_norms:
            return True
        if any(nc.startswith(tp) or tp.startswith(nc) for tp in title_prefixes):
            return True
    return False


def is_style_heading(line_text, spans):
    """Is this line a heading marked by STYLE rather than size?

    Needed because the no-outline fallback used to look at font size alone, and
    hard-exited with "no distinct heading fonts. Is this a scanned document?" on
    documents that are neither scanned nor structureless — they simply set their
    headings in BOLD CAPITALS at body size. 18 of the 129 documents in the pilot
    corpus (22%) failed that way, every one of them with 24k-46k characters of
    perfectly good text.

    The rule is bold AND fully upper-case, deliberately narrow. Measured over the
    failing documents, bold ALONE fires on 217/403/111 lines — that is inline
    emphasis and letterhead, not structure. Adding the upper-case requirement cuts
    those to 10/47/2, which read as real section headings ("I. INTRODUCTION",
    "MEMORANDUM OF LAW", "ASSUMPTIONS").

    Requires EVERY non-blank span to be bold: a sentence with one bold phrase in it
    is emphasis, not a heading."""
    vis = [s for s in spans if s.get('text', '').strip()]
    if not vis:
        return False
    t = normspace(line_text)
    if not (3 <= len(t) <= STYLE_HEADING_MAX_LEN):
        return False
    if not t.isupper():
        return False
    return all(s.get('flags', 0) & 16 for s in vis)      # bit 4 = bold


def _is_heading_size(size, head_min):
    """Is this span heading-sized, for TABLE-vs-CONTENT decisions only?

    STRICTLY greater than head_min, deliberately. head_min is the FLOOR of
    "notably larger than body text" (body_size + 1.4), so a line sitting exactly
    on that floor is the most marginal case there is — and if a table's row-label
    style happens to land on it, every one of that table's rows gets thrown away.
    usa-dp does exactly this: body 10.6 -> head_min 12.0, and section G's state
    names are set at exactly 12.0. That single coincidence cost 68 pages their
    multi-page grouping, fragmenting one table into 62 placeholders and leaking
    78% of its cell text back out as loose prose.

    Real section headings are still protected: they appear in the PDF outline, so
    the `titles` test (_row_is_titled / title_norms) recognises them regardless of
    size. This size test is only the fallback for headings ABSENT from the
    outline.

    Document-structure classification does NOT use this helper — it keeps
    `>= head_min` — so the tree's heading hierarchy, headings_manifest.json and
    the folder layout are unchanged by construction."""
    return head_min is not None and size > head_min


def _recon(page, bounds=None, head_min=None, titles=None, title_exacts=None):
    rules = _hrules(page)
    if len(rules) < 2:   # >=2 rules -> >=1 row band (sparse continuation pages have few rows)
        return None
    top, bot = rules[0], rules[-1]
    sp = _tspans(page, top, bot)
    if not sp:
        return None
    if bounds is None:
        gb = _colbounds(sp, int(page.rect.width))
        hb = _hdr_cols(page, rules)
        # prefer header-derived columns when they resolve MORE columns than the gutter
        # projection (mis-assigned tables are caught later by the confidence gate)
        bounds = hb if (hb and len(hb) > len(gb)) else gb
    nc = len(bounds) - 1
    if nc < 2:
        return None

    def col_of(x):
        for i in range(nc):
            if bounds[i] <= x < bounds[i + 1]:
                return i
        return nc - 1
    rows = []
    for y_lo, y_hi in zip(rules[:-1], rules[1:]):
        if y_hi - y_lo < 6:
            continue
        band_spans = [s for s in sp if y_lo - 1 <= s[1] < y_hi - 1]
        if any(_is_heading_size(s[4], head_min) for s in band_spans):
            # skip just this band, not the rest of the page (see _page_bands)
            continue
        cells = [[] for _ in range(nc)]
        for x0, y0, x1, t, sz in sorted(band_spans, key=lambda s: (round(s[1]), s[0])):
            cells[col_of(x0)].append(t)
        row = [normspace(' '.join(c)) for c in cells]
        if not any(row):
            continue
        if _row_is_titled(row, titles, title_exacts):
            continue
        rows.append(row)
    return bounds, rows, (top, bot)


def _nrow(cells):
    return tuple(re.sub(r'\W', '', c.lower()) for c in cells)


def _hcat(cells):
    # column-agnostic header signature: concatenate all cells (a continuation page may
    # split the header into a different number of columns, but the text is the same)
    return re.sub(r'\W', '', ''.join(cells).lower())


def _col_of(x, bounds):
    for i in range(len(bounds) - 1):
        if bounds[i] <= x < bounds[i + 1]:
            return i
    return len(bounds) - 2


def _page_bands(page, bounds, head_min=None, titles=None, title_exacts=None):
    """(row_or_None, y_lo, y_hi) per band, for one page, using canonical `bounds`.
    row is None for a rejected band — kept as an explicit placeholder (with its
    Y-range) rather than omitted, so the caller can (a) tell the rest of a
    rejected heading/sub-heading's own wrapped text — which lands in a
    separate band with a blank first cell, same as a genuine multi-line table
    cell would — apart from a real continuation of the last kept table row,
    and (b) know exactly which Y-ranges on this page were rejected, so that
    region can be exempted from whatever else claims this page's content (see
    reconstruct_multipage_tables's 'rejected_ranges'). A page with <2 rules (a
    single row spilling across the whole page) returns ONE band spanning the
    full page — i.e. a continuation of the previous row. A band is rejected
    (not the rest of the page — an earlier table's tail can end and a
    DIFFERENT table start again right after, on the same page) if EITHER: it
    contains a heading-sized span (head_min — the real table ended here), OR
    its content matches one of the document's own outline titles (titles — a
    sub-heading the column split happened to place in what looks like a key
    column, even though it's body-sized text)."""
    rules = _hrules(page)
    nc = len(bounds) - 1
    if len(rules) >= 2:
        top, bot = rules[0], rules[-1]
        sp = _tspans(page, top, bot)
        bands = list(zip(rules[:-1], rules[1:]))
    else:
        sp = _tspans(page, 0, page.rect.height)
        bands = [(0, page.rect.height)] if sp else []
    out = []
    for y_lo, y_hi in bands:
        if y_hi - y_lo < 6:
            continue
        band_spans = [s for s in sp if y_lo - 1 <= s[1] < y_hi - 1]
        if any(_is_heading_size(s[4], head_min) for s in band_spans):
            out.append((None, y_lo, y_hi))
            continue
        cells = [[] for _ in range(nc)]
        for x0, y0, x1, t, sz in sorted(band_spans, key=lambda s: (round(s[1]), s[0])):
            cells[_col_of(x0, bounds)].append(t)
        row = [normspace(' '.join(c)) for c in cells]
        if not any(row):
            continue  # a truly blank band carries nothing either way — not a rejection
        if _row_is_titled(row, titles, title_exacts):
            out.append((None, y_lo, y_hi))
            continue
        out.append((row, y_lo, y_hi))
    return out


# Confidence gate for a multi-page run.
#
# WHAT THIS GATE NOW DECIDES HAS CHANGED. It was written when Stage 1 RENDERED
# tables to markdown: MAX_STITCHED_CELL caught a run whose columns were mis-mapped
# (they collapse into one enormous cell) and rejected it rather than emit a garbled
# table. Stage 1 no longer renders anything — md_table() and the whole rendering
# path are gone, MinerU does it in Stage 2 — so a large cell can no longer produce
# garbled output. The gate's only remaining effect is whether the pages get
# GROUPED as one table, and for grouping a big cell just means "this page is
# sparsely ruled", which is not a reason to fragment a table.
#
# So the cap is now a sanity bound on pathological stitching, not a rendering
# guard, and it is set well above real content. Reference: usa-dp section G
# (pages 213-280) is one correctly-mapped table whose largest LEGITIMATE cell is
# 3397 chars — it has only 4-5 horizontal rules per page, so one row band holds an
# entire state's breach rules. At 2000 the whole run was discarded, fragmenting it
# into 62 placeholders and leaking 78% of its cell text back out as prose.
#
# The earlier attempt to fix that failed for a DIFFERENT reason, now addressed:
# head_min also gated the placeholder emitter, so relaxing the gate alone claimed
# the run (suppressing prose) while emitting no placeholder, and those pages never
# reached MinerU. See _is_heading_size() and docs/REVERT_multipage_table_gate.md.
# If grouping does go wrong now, it is recoverable: MinerU emits its own blocks and
# the per-page bbox matching in hybrid_extract pairs them up.
MAX_STITCHED_CELL = 8000
# Bounds how many consecutive landscape pages may form one run. The real
# boundaries are the checks in the loop below (a new outline section, a portrait
# page, a DIFFERENT repeated header); this is only a blunt backstop. At 60 it
# truncated section G's 68-page table at page 272, so its tail could never join.
MAX_RUN_PAGES = 120
# Minimum height for a page's row band to be worth claiming. Same 6pt floor the
# band scanners already use — below it there is nothing to swallow and nothing to
# place, so claiming the page only disables its fallbacks.
MIN_YR_HEIGHT = 6

# Share of a per-page table region that must already sit inside a multi-page run's
# own row band before the region is treated as part of that run rather than a table
# of its own. Set below the measured duplicate (80% on Australia__181814 p106) and
# well above a genuinely separate table stacked on the same page, which overlaps the
# run's band hardly at all — a second table starts where the first one's rules end.
RUN_CLAIMED_OVERLAP = 0.6
# Longest line still eligible to be a style-marked heading. A heading is short; a
# fully-capitalised paragraph is not a heading.
STYLE_HEADING_MAX_LEN = 120

# Bottom slice of a page that is running footer, not content: a page number, a
# document-id line, the product name. A heading is never printed down there, and
# _head_below_band must not mistake one of those lines for the start of a section.
FOOTER_BAND_FRAC = 0.93


# A contents-page row: "<title> ......... <page>". Four or more leaders, because a
# title can legitimately contain "..." and a two-dot ellipsis is not a leader run.
# Same shape rescue_outline.ENTRY matches, restated here rather than imported to keep
# the extractor free of a dependency on the rescue stage.
_TOC_ROW_RE = re.compile(r"^.+?[.\u2026\u00b7]{4,}\s*\d{1,4}\s*$")
# ... or the same row with the leaders already collapsed by text extraction, leaving
# "<title>   <page>" with a wide gap.
_TOC_ROW_BARE_RE = re.compile(r"^.{6,}?\s{2,}\d{1,4}\s*$")

# A group label heading its own section, printed ALONE on the line above that section's
# descriptive title:
#
#     APPENDIX 2
#     DISCLAIMERS: CLOSED-ENDED FUND
#
# "Alone on the line" is the whole test, and it is what separates a label from a
# reference to one: "Appendix 1(A) applies." opens a page in Chile 169565 and is a
# cross-reference, not a heading. Measured across this corpus, 138 of the 139 labels
# printed this way are immediately followed by their title, one blank line (19-27pt)
# below. `parts?` is deliberately absent -- "PART B" heads whole memoranda here and
# folding it into a section title would rename the document's own divisions.
_GROUP_LABEL_RE = re.compile(
    r"^(?P<word>appendices|appendix|annexures?|annexes?|annexe|annex"
    r"|schedules?|exhibits?)\s+(?P<num>\d{1,3})\s*[:.\-–—]?$", re.I)


def _is_toc_row(text: str) -> bool:
    """Is this line a table-of-contents entry rather than a heading?

    A contents page is NAVIGATION, not content: its rows name sections that appear
    again, properly, later in the document. Promoting them to headings duplicates
    every clause — ADGM 170680 emitted "SOURCES OF LAW ... 16" and "MARKETING/SELLING
    TO THE PUBLIC ... 25" as sections of their own, each holding the contents listing
    as its body, alongside the real sections of the same name. 39 outline entries
    became 52 headings that way."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_TOC_ROW_RE.match(t) or _TOC_ROW_BARE_RE.match(t))


def _head_below_band(page, band_hi, head_min=None, titles=None, title_exacts=None):
    """y of the topmost SECTION HEADING printed BELOW a page's table band, or None.

    These questionnaire tables do not align to page boundaries: a clause's table
    routinely finishes two thirds of the way down a page, and the NEXT clause's
    heading is then printed underneath it, on that same page. `sec_pages` only
    knows which PAGE a section starts on, never where on it — so the run above is
    cut at the page boundary and a new run is seeded at the TOP of that page.
    The new run's first page is then entirely the PREVIOUS clause's table, its
    placeholder is emitted above the heading, and Stage 3 splices the whole
    stitched table into the section BEFORE the one it belongs to.

    Measured: Czech Republic 166819 clause 7 starts at the foot of p67 (heading
    at y504, the page's rules ending at y466). Its 31-page table (pages 67-97,
    100 rows) was rendered inside clause 6, and clause 7.1 -- the substance of
    the private placement regime -- was left as 31 page images and nothing else.
    13 further sections across the 106-document corpus are printed this way.

    Only lines BELOW the last rule are considered, which is what makes this safe:
    a table cell can never sit under its own table's last rule, so a cell quoting
    a section title cannot fire this. The footer band is excluded for the same
    reason -- a page number or a running document title is not a heading.
    """
    title_norms, title_prefixes = titles if titles else (frozenset(), ())
    exacts = title_exacts or frozenset()
    foot = page.rect.height * FOOTER_BAND_FRAC
    best = None
    for b in page.get_text('dict')['blocks']:
        if b['type'] != 0:
            continue
        for l in b['lines']:
            y = l['bbox'][1]
            if y <= band_hi + 2 or y >= foot:
                continue
            t = norm(''.join(s.get('text', '') for s in l['spans']))
            if len(t) < 4:
                continue
            sz = max((s['size'] for s in l['spans']), default=0.0)
            # Same three signals the per-line pass uses to know a heading when it
            # sees one (see _is_outline_title there): an exact outline title, a
            # long-enough prefix of one -- a printed heading wraps, and the
            # enumeration token is often a line of its own -- or heading size.
            if (t in exacts or t in title_norms
                    or (len(t) >= 8
                        and (t.startswith(title_prefixes)
                             or any(x.startswith(t) for x in title_prefixes)))
                    or _is_heading_size(sz, head_min)):
                best = y if best is None else min(best, y)
    return best


def reconstruct_multipage_tables(doc, body_start, npages, sec_pages=frozenset(), head_min=None,
                                 titles=None, clause_tables=False, title_exacts=None):
    """-> {page_no: {'first': int, 'md': str|None (only on first page), 'yr': (top,bot)}}.
    A run is consecutive LANDSCAPE pages that repeat the SAME header (column-agnostic),
    so adjacent-but-different comparison tables don't merge. A run is emitted ONLY if it
    reconstructs cleanly (>=2 cols, >=3 rows, no absurdly large merged cell, >=60% of
    rows keyed); otherwise its pages fall through to the normal prose+snapshot path, so
    reconstruction can only ever improve, never garble. head_min/titles: a row band is
    excluded from the stitched table (see _recon/_page_bands) if it's heading-sized OR
    matches one of the document's own outline titles — either signal means a heading or
    sub-heading sits here, not real table data; a heading can sit between two back-to-
    back tables sharing one page, or trail a table entirely. The main per-line pass
    separately exempts heading-SIZED lines from this run's claimed 'yr' region outright
    (see pdf2mdtree's comp_here check), so a heading itself is never silently swallowed
    regardless of where it falls; body-sized sub-headings rely on the title match here."""
    comp = {}
    p = body_start
    while p < npages:
        page = doc[p]
        landscape = page.rect.width > page.rect.height
        r0 = _recon(page, head_min=head_min, titles=titles,
                    title_exacts=title_exacts) if landscape else None
        # clause_tables: the clause IS the table. Seed a run at the clause boundary
        # itself, not at the first page that happens to reconstruct — clause 7 of
        # Australia__181814 opens with five pages (p83-87) that reconstruct into
        # nothing, which pushed the run's start to p88 and left those five as
        # single-page regions of their own.
        if not r0 or len(r0[1]) < 2:
            if not (clause_tables and landscape and p in sec_pages):
                p += 1
                continue
        # `r0` truthy is not `r0[1]` non-empty. The guard above only `continue`s when this is
        # NOT a clause-table seed page, so a clause_tables product falls through here with a
        # header and no rows and indexed row 0 — the same mistake as the `rq` path below, and it
        # killed Marketing Restrictions Luxembourg 181768 after that one was fixed.
        hcat = _hcat(r0[1][0]) if (r0 and r0[1]) else None
        pages, q = [p], p + 1
        while q < npages and doc[q].rect.width > doc[q].rect.height \
                and q - p < MAX_RUN_PAGES:
            if q in sec_pages:
                # ...but only if the section's heading is printed ABOVE this page's
                # table band. Where it is printed BELOW the last rule, everything
                # above it is still THIS run's table and only what follows the
                # heading belongs to the next section — so the page joins the run
                # and the run ends after it. Without this the page seeds a run of
                # its own whose placeholder lands above the heading, i.e. in the
                # previous section (see _head_below_band).
                _rq = _hrules(doc[q])
                if (len(_rq) >= 2
                        and _head_below_band(doc[q], _rq[-1], head_min, titles,
                                             title_exacts) is not None):
                    pages.append(q)
                    q += 1
                break                            # a new outline section begins -> table ended
            # clause_tables: a header change does NOT end the run. Deciding that two
            # differently-headed page groups are two tables is exactly the judgement
            # being handed to MinerU, which sees the rendered pages; here it split
            # clause 7 at p98 ('open-ended fund / closed-ended fund', 6 columns
            # against the preceding 3). The clause boundary above still ends the run.
            if not clause_tables:
                rq = _recon(doc[q], head_min=head_min, titles=titles,
                            title_exacts=title_exacts)
                # `rq` truthy does NOT mean it has rows: a landscape page can reconstruct to a
                # header with an EMPTY body (ruled but rowless — a continuation page whose rules
                # carry over, a spacer, a page of footnotes under a table). Indexing row 0 there
                # raised IndexError and killed stage 1 outright, taking the whole document with
                # it: Marketing Restrictions Luxembourg 181768 (158pp) failed exactly here, and
                # it is data-dependent, so it looks intermittent across a corpus.
                #
                # With no rows there is no evidence of a NEW header, and this check exists only
                # to end the run when a different header starts — so the page is treated as a
                # continuation, which is what the surrounding code already assumes by default.
                if rq and rq[1]:
                    row0 = rq[1][0]
                    # short label cells = a header; long cells / rule-less = data continuation
                    is_header = any(c.strip() for c in row0) and all(len(c) < 40 for c in row0)
                    if is_header and not any(_hcat(row) == hcat for row in rq[1][:2]):
                        break                    # a DIFFERENT header -> next table starts
            pages.append(q)                      # header repeat / data / rule-less continuation
            q += 1
        canon = max((r[0] for r in (_recon(doc[pp], head_min=head_min, titles=titles,
                                           title_exacts=title_exacts) for pp in pages) if r),
                    key=len, default=None)
        stitched, yr, rejected, hsig = [], {}, {}, None
        if canon and len(canon) - 1 >= 2:
            for pp in pages:
                rules = _hrules(doc[pp])
                yr_lo = rules[0] if rules else 0.0
                yr_hi = rules[-1] if len(rules) >= 2 else doc[pp].rect.height
                page_rejected = []
                prev_rejected = False
                for row, y_lo, y_hi in _page_bands(doc[pp], canon, head_min=head_min, titles=titles,
                                                   title_exacts=title_exacts):
                    if row is None:
                        prev_rejected = True         # a heading/sub-heading label was here
                        page_rejected.append((y_lo, y_hi))
                        continue
                    if stitched and not row[0].strip() and prev_rejected:
                        # this blank-first-cell band directly follows a rejected
                        # label — it's the REST of that same rejected heading's
                        # own wrapped text, not a real continuation of the last
                        # kept table row. Drop it too, rather than glue
                        # unrelated prose onto an unrelated real row — and
                        # extend the rejected range to cover it, so the main
                        # per-line pass doesn't swallow it either.
                        page_rejected.append((y_lo, y_hi))
                        prev_rejected = False
                        continue
                    prev_rejected = False
                    if hsig is None:
                        hsig = _nrow(row)
                    if _nrow(row) == hsig:
                        if not stitched:
                            stitched.append(row)
                        continue
                    if stitched and not row[0].strip():        # split / continuation row
                        for j in range(len(row)):
                            if row[j].strip():
                                stitched[-1][j] = normspace(stitched[-1][j] + ' ' + row[j])
                        continue
                    stitched.append(row)
                if pp == pages[0]:
                    # The same boundary as the break above, seen from the other side:
                    # this run's first page carries the PREVIOUS section's table above
                    # a heading printed below the rules. Reached only when no run was
                    # open to absorb the page (the preceding pages were
                    # portrait, or their stitch was rejected), so the break above never
                    # ran. Clamping past the heading collapses the band below
                    # MIN_YR_HEIGHT and the page drops out of the run, which is correct:
                    # its rows are the previous section's, not this one's.
                    _hb = _head_below_band(doc[pp], yr_hi, head_min, titles, title_exacts)
                    if _hb is not None:
                        yr_lo = max(yr_lo, _hb)
                if pp == pages[0] and page_rejected:
                    # This run's FIRST page can be shared with the TAIL of a
                    # different, preceding comparison table that just happens to
                    # finish partway down the same page (very common — these
                    # tables rarely align to page boundaries). _hrules() can't
                    # tell the two tables apart; it returns every horizontal rule
                    # on the page regardless of which table it belongs to, so
                    # yr's start would otherwise sit ABOVE that leftover content
                    # instead of above THIS run's own heading. Clamp yr's start
                    # to just after the LAST heading/title-matched band on this
                    # page (a real section-boundary signal, already computed
                    # above) so the main per-line pass's placeholder-insertion
                    # trigger (comp_here['yr']) can't fire on the previous
                    # table's rows before the scan even reaches this run's own
                    # heading — which is exactly what silently misattributed a
                    # whole table to the wrong (earlier) section.
                    yr_lo = max(yr_lo, page_rejected[-1][1])
                # A page whose bands were ALL rejected has its yr collapsed to zero
                # height by that clamp, and a zero-height band is strictly worse than
                # no band at all: registering the page in COMP sets
                # page_has_deferred_table, which DISABLES both snapshot fallbacks,
                # while the empty band means the multi-page placeholder never fires
                # either — so the page gets no placeholder at all and MinerU is never
                # asked about it, yet its prose is still suppressed because the raw
                # table bbox stays in table_regions. That is silent content loss: it
                # deleted the table at the top of usa-dp page 207. Leave such a page
                # out of the run so it falls through to the fallback path that
                # handles it correctly.
                if yr_hi - yr_lo < MIN_YR_HEIGHT:
                    continue
                yr[pp] = (yr_lo, yr_hi)
                rejected[pp] = page_rejected
        data = stitched[1:] if stitched else []
        maxcell = max((len(c) for r in stitched for c in r), default=0)
        keyed = sum(1 for r in data if r and r[0].strip())
        # clause_tables: keep the run even when this dry-run stitch degenerated.
        # The stitched rows are discarded a few lines below — only the page
        # GROUPING survives — so on a product where every clause is known to be one
        # questionnaire table, a giant merged cell says the stitch failed, not that
        # the pages are different tables. Product-scoped; see
        # product_rules.CLAUSE_TABLE_RUNS for the measurements and why it is not
        # general. The row-count and keyed-ratio conditions still apply.
        cell_ok = clause_tables or maxcell <= MAX_STITCHED_CELL
        if len(data) >= 3 and cell_ok and keyed >= 0.6 * len(data):
            # The stitched rows themselves are discarded — MinerU renders the real
            # table in Stage 2. What survives is the PAGE GROUPING: which pages
            # belong to one run, and 'is_first' so the placeholder is emitted once
            # (on the run's first page) rather than on every page of it.
            #
            # 'is_first' is deliberately keyed off the first page that SURVIVES
            # (is in `yr`), not off `pages[0]` itself. `pages[0]` can fail to survive
            # on its own — its row band collapses to zero height when every one of
            # its bands is heading-sized (see the MIN_YR_HEIGHT guard above) — and if
            # is_first stayed pinned to that excluded page, NO page in the run would
            # ever satisfy `pp == pages[0]`, so the run's placeholder would never be
            # emitted for ANY page while every surviving page's content still gets
            # silently swallowed below. Confirmed: exactly this happened to a real
            # 7-row table on page 59 of a real corpus document — deleted with no
            # placeholder, no failure marker, and no trace in any report.
            effective_first = next((pp for pp in pages if pp in yr), None)
            for pp in pages:
                if pp in yr:
                    comp[pp] = {'first': pages[0], 'is_first': pp == effective_first,
                                'yr': yr[pp], 'rejected_ranges': rejected.get(pp, [])}
        p = max(q, p + 1)
    return comp


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pdf')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--depth', type=int, default=3,
                    help='max folder nesting depth (default 3)')
    ap.add_argument('--clause-tables', action='store_true',
                    help='keep a multi-page table run even when this stage\'s own '
                         'dry-run stitch degenerates (product-scoped; see '
                         'product_rules.CLAUSE_TABLE_RUNS)')
    ap.add_argument('--group-labels', action='store_true',
                    help='join a "APPENDIX n" / "SCHEDULE n" line printed alone above a '
                         'heading INTO that heading, instead of leaving it as the last '
                         'line of the section before it (product-scoped; see '
                         'product_rules.GROUP_LABEL_HEADINGS)')
    ap.add_argument('--no-tables', action='store_true',
                    help='disable table detection (tables become running text)')
    ap.add_argument('--no-snapshots', action='store_true',
                    help='disable page snapshots for complex pages')
    ap.add_argument('--no-emphasis', action='store_true',
                    help="do not carry the PDF's bold/italic runs into the markdown "
                         "(restores the pre-emphasis output byte-for-byte)")
    ap.add_argument('--no-bullet-nesting', action='store_true',
                    help='emit every list item flat, ignoring the indent level its '
                         "bullet glyph's x-position implies")
    args = ap.parse_args()

    doc = fitz.open(args.pdf)
    npages = doc.page_count
    report = {'warnings': [], 'paragraphs_emphasised': 0}

    # ---- pass 1: typography + header/footer detection ----
    size_weight = collections.Counter()
    band_counts = collections.Counter()
    text_pages = collections.defaultdict(set)
    for pno in range(npages):
        H = doc[pno].rect.height
        dd = doc[pno].get_text('dict')
        for b in dd['blocks']:
            if b['type'] != 0:
                continue
            for l in b['lines']:
                t = ''.join(s['text'] for s in l['spans'])
                for s in l['spans']:
                    if s['text'].strip():
                        size_weight[round(s['size'], 1)] += len(s['text'])
                y = l['bbox'][1]
                if (y < 95 or y > H - 95) and normspace(t):
                    band_counts[(round(y / 8), band_key(t))] += 1
                    text_pages[band_key(t)].add(pno)

    if not size_weight:
        # Every page was scanned for text spans, and none had any: this is an image-only PDF.
        # Indexing most_common() here raised IndexError, which read as a bug in this script and
        # was recorded as one — 24 documents in one run, all of them actually scans. They cannot
        # be extracted without OCR, and saying so is the useful outcome.
        imgs = sum(1 for pno in range(npages)
                   for b in doc[pno].get_text("dict")["blocks"] if b["type"] != 0)
        sys.exit(f"No text layer: {npages} page(s) contain no text spans at all "
                 f"({imgs} image block(s) found). This is a SCANNED document — it needs OCR "
                 f"before this pipeline can read it, and no heading detection can help.")
    body_size = size_weight.most_common(1)[0][0]
    foot_max = body_size - 1.5
    head_min = body_size + 1.4
    repeated = {k for k, c in band_counts.items() if c >= max(4, 0.2 * npages)}
    # `repeated` assumes a running header/footer sits at a FIXED y on every
    # page — true for a page number or title, false for a doc-control/footer
    # block anchored to the END of that page's own content, which lands at a
    # different y (and so a different round(y/8) band) whenever body length
    # varies. That line then never accumulates enough hits in any ONE band to
    # cross the threshold, and is never recognized as boilerplate — which is
    # how a page-bottom version stamp ends up looking like unclaimed fine
    # print (or worse, its adjacent page-number digit ends up looking like a
    # footnote marker; see process_fn_lines). Keyed on TEXT ALONE — which
    # PAGE, not which pixel — this catches it regardless of where it lands,
    # same digit-collapsing signature (band_key) and threshold as `repeated`.
    repeated_text = {k for k, pages in text_pages.items() if len(pages) >= max(4, 0.2 * npages)}
    report['body_font_size'] = body_size
    report['pages'] = npages

    # ---- outline ----
    toc = doc.get_toc()
    heuristic = not toc
    # Where each outline entry actually POINTS on its page. get_toc(simple=False)
    # carries the destination as a page-space Point (top-left origin, the same
    # coordinate space as a bbox or a rule), so the outline can be used as
    # geometry and not only as a list of titles. Used by detect_tables to stop a
    # deferred region from swallowing a heading — see the outline split in the
    # STRONG_RULE branch. Empty dict when the producer wrote no explicit
    # destinations, which degrades to the previous behaviour.
    outline_ys = {}
    if toc:
        try:
            for _lvl, _t, _pg, _d in doc.get_toc(simple=False):
                _to = _d.get('to') if isinstance(_d, dict) else None
                if _d.get('kind') == 1 and _to is not None:
                    outline_ys.setdefault(_pg - 1, []).append(float(_to.y))
        except Exception:
            outline_ys = {}
        for _k in outline_ys:
            outline_ys[_k].sort()
    body_start = min((pg - 1 for _, _, pg in toc), default=0) if toc else 0
    report['outline_entries'] = len(toc)
    report['structure_source'] = ('font-size heuristic (no bookmarks — verify results!)'
                                  if heuristic else 'PDF bookmark outline')
    # Style-marked headings are a LAST RESORT, enabled only when there is no
    # outline AND no text is heading-sized anywhere in the document. Gating it on
    # "size found nothing" means it can only ever affect a document that currently
    # produces NOTHING AT ALL, so it cannot change any document that works today —
    # in particular the bookmarked ones, which never reach this branch.
    style_headings = heuristic and not any(sz >= head_min for sz in size_weight)
    if heuristic:
        report['warnings'].append(
            'PDF has no bookmark outline; headings were inferred from '
            + ('BOLD CAPITALISED lines at body size (no text in this document is '
               'heading-sized) — structure is FLAT and may be imperfect.'
               if style_headings else 'font sizes and may be imperfect.'))
    report['heading_signal'] = ('outline' if not heuristic
                                else 'bold-caps' if style_headings else 'font-size')

    # ---- recover a body_start that skips real, un-bookmarked content ----
    # body_start assumes anything before the outline's earliest page is cover/
    # TOC front matter. That assumption breaks when the outline itself is
    # INCOMPLETE — e.g. only an appendix got bookmarked while the whole main
    # body (which has its own printed heading numbering, just never turned into
    # PDF bookmarks) was silently skipped. Detected by checking whether the
    # skipped pages contain an actual heading pattern — heading-sized text or a
    # bold ALL-CAPS line — outside the running header/footer bands: a lone
    # cover page never has one, a document body does. Gated on body_start >= 2
    # so a single skipped cover page (today's common, working case) is
    # untouched by construction.
    extended_start = body_start
    if toc and body_start >= 2:
        for pno in range(body_start):
            H = doc[pno].rect.height
            dd = doc[pno].get_text('dict', sort=True)
            found = False
            for b in dd['blocks']:
                if b['type'] != 0:
                    continue
                for l in b['lines']:
                    spans = [s for s in l['spans'] if s['text'].strip()]
                    if not spans:
                        continue
                    y = l['bbox'][1]
                    t = ''.join(s['text'] for s in spans)
                    if (y < 95 or y > H - 95) and (
                            (round(y / 8), band_key(t)) in repeated or band_key(t) in repeated_text):
                        continue  # running header/footer, not body structure
                    sz = max(s['size'] for s in spans)
                    if _is_heading_size(sz, head_min) or is_style_heading(t, spans):
                        found = True
                        break
                if found:
                    break
            if found:
                extended_start = 0
                break
    report['pre_outline_pages_recovered'] = body_start if extended_start < body_start else 0
    if extended_start < body_start:
        report['warnings'].append(
            f"PDF bookmark outline starts at page {body_start + 1}, but pages "
            f"1-{body_start} contain heading-shaped text with no bookmark of "
            "their own — reprocessed with the bold-caps/font-size heuristic "
            "instead of being dropped.")

    # ---- pass 2: extract paragraphs + footnotes ----
    paras, footnotes = [], {}
    # Verification baseline: source words that actually REACH the tree. Counted at
    # the four emission sites (prose line, heading, formula, footnote body), never
    # up front — text swallowed by a table region or a deferred-table footprint is
    # deliberately absent from Stage 1's output, so counting it here would make
    # word_delta_pct structurally negative and fire the "content may be missing"
    # warning on every single run regardless of evidence.
    kept_words = 0
    snapshot_pages = set()
    deferred_tables = []
    # bookmark titles must never be swallowed as table cell content
    # (also match titles minus their enumeration token: "(i) Recipient..."
    # can appear in the text without the "(i)")
    def _rest(t):
        return norm(re.sub(r'^([A-Z]\.|\d+(\.\d+)*\.?|\([a-z0-9]+\))\s*', '', t))
    title_norms = {norm(t) for _, t, _ in toc} if toc else set()
    title_prefixes = tuple(sorted(
        {x for _, t, _ in toc for x in (norm(t), _rest(t)) if len(x) >= 8}))
    # Every title form, WITHOUT the length filter above — for exact whole-line
    # matching, where a short title like "LICENCE" is still unambiguous.
    #
    # ONLY for an outline that is actually a structure. Some outlines are a dump of
    # body text (Belgium 163341: 396 entries, 84% whole sentences), and there the set
    # fills with short common fragments — "PART B", a date line, a cover-page title.
    # Every line matching one then escapes the table suppression and can be taken for
    # a heading: Belgium built 1 real section and 94 junk ones, sectioning 81.0 -> 6.2,
    # and took 1316s doing it against 46-61s for its neighbours. The >= 8 prefix bar
    # was holding that back, so dropping it for exact matches has to be paid for by
    # trusting the outline first.
    #
    # A heading is not a sentence: _PROSE_WORDS mirrors rescue_outline._heading_like,
    # which is the same judgement made for the same reason.
    _PROSE_WORDS = 10
    _prose = sum(1 for _, t, _ in toc if len((t or "").split()) >= _PROSE_WORDS)
    _outline_is_prose = bool(toc) and _prose > len(toc) * 0.5
    title_exacts = (set() if _outline_is_prose else
                    {x for _, t, _ in toc
                     for x in (norm(t), _rest(t))
                     if len(x) >= 4 and len((t or "").split()) < _PROSE_WORDS})

    def flush_fn(cur):
        nonlocal kept_words
        if cur[0] is not None and cur[0] not in footnotes:
            footnotes[cur[0]] = normspace(cur[1])
            # Footnote bodies are diverted out of the prose stream but DO reach the
            # tree, re-emitted as `[^n]: body` by finalize(). Counted once each
            # here; finalize() duplicates a body into every file that cites it,
            # which is part of the acknowledged positive drift in md_words.
            kept_words += len(footnotes[cur[0]].split())

    def process_fn_lines(fn_lines):
        """Parse a page's footnote-area lines. A digit-only line starts a
        footnote only when body text sits beside it at (nearly) the same y —
        otherwise it's a stray page number and is discarded.

        Returns any lines in this zone that were never claimed by a footnote
        number — e.g. a one-off disclaimer/confidentiality notice printed in
        fine print with no marker of its own. Silently dropping those (the
        previous behaviour) is an untraceable content loss; the caller emits
        them as an ordinary paragraph instead."""
        # A running footer/doc-control line (see repeated_text above) can sit
        # in this same zone, and MOST dangerously, right beside a bare page
        # number — which then reads exactly like "digit marker + adjacent
        # footnote body" to the pairing logic below. Drop it before pairing
        # ever sees it, not after: two different footnote numbers ending up
        # with the SAME body text (this line, verbatim, from two different
        # pages) is exactly that failure, and it silently steals both
        # numbers from their real bodies (flush_fn keeps only the FIRST body
        # seen for a given number).
        #
        # Gated on TEXTUAL (has letters): band_key collapses every digit run
        # to '#', so a bare page number and a bare footnote marker are
        # INDISTINGUISHABLE by content alone, and either one recurs on most
        # pages of a document with footnotes throughout. Without this gate,
        # repeated_text would match '#' itself and this filter would erase
        # every real footnote marker in the document, not just the footer.
        def _is_repeated_boilerplate(spans):
            bk = band_key(''.join(s['text'] for s in spans))
            return bool(re.search(r'[a-z]', bk)) and bk in repeated_text
        fn_lines = [(y, spans) for y, spans in fn_lines if not _is_repeated_boilerplate(spans)]
        ys = [y for y, _ in fn_lines]
        cur = [None, '']
        unclaimed = []
        for idx, (y, allspans) in enumerate(fn_lines):
            raw = ''.join(s['text'] for s in allspans)
            stripped = raw.strip()
            if re.fullmatch(r'\d{1,4}', stripped):
                # body text on the same row or the row below → footnote number;
                # an isolated number (nothing nearby) is a page number
                if any(k != idx and -3.5 < ys[k] - y < 13 for k in range(len(ys))):
                    flush_fn(cur)
                    cur = [int(stripped), '']
                continue
            first = allspans[0]
            if re.fullmatch(r'\d{1,4}', first['text'].strip()) and len(allspans) > 1 \
                    and first['size'] <= foot_max + 0.6:
                flush_fn(cur)
                cur = [int(first['text'].strip()),
                       ''.join(s['text'] for s in allspans[1:])]
                continue
            m2 = re.match(r'^\s*(\d{1,4})\s{2,}(\S.*)$', raw)
            if m2 and cur[0] != int(m2.group(1)):
                flush_fn(cur)
                cur = [int(m2.group(1)), m2.group(2)]
            elif cur[0] is not None:
                cur[1] += ' ' + raw
            else:
                # No footnote has started yet on this page — not a footnote
                # body, just unmarked fine print (see docstring above).
                unclaimed.append(raw)
        flush_fn(cur)
        return unclaimed

    # Outline section starts = table boundaries — but only for the levels that
    # will actually BECOME a section node. Levels 1..depth+1 get their own file
    # (see emit_item: a level-1 heading is a node at --depth 0, and at --depth 3 a
    # level-4 heading still gets its own file), so anything deeper is content
    # INSIDE a section and must not cut that section's table.
    #
    # Cutting on every level regardless was the bug behind "one clause, many
    # tables". On Australia__181814 section 8 spans pages 106-129 and its twelve
    # subsections start on ten distinct pages, so a 24-page questionnaire table was
    # broken into 21 regions — one per subsection start plus the runs between them.
    # With the product built flat (product_rules.SECTION_DEPTH = 0) the only
    # boundary left inside section 8 is its own start at 106, so the clause can
    # reconstruct as ONE table. This only ENABLES that: the run still has to pass
    # reconstruct_multipage_tables' own tests, and a genuinely different header
    # mid-clause still ends it, which is correct.
    #
    # Measured blast radius at the default --depth 3 (boundaries at levels 1-4):
    # 2 of the 72 outlined documents in this corpus have any outline entry deeper
    # than level 4, so 70 of them are bit-identical.
    _sec_pages = frozenset(pg - 1 for lvl, _, pg in toc if lvl <= args.depth + 1)
    COMP = {} if args.no_tables else reconstruct_multipage_tables(
        doc, extended_start, npages, _sec_pages, head_min=head_min,
        titles=(title_norms, title_prefixes), clause_tables=args.clause_tables,
        title_exacts=title_exacts)

    for pno in range(extended_start, npages):
        page = doc[pno]
        H = page.rect.height
        dd = page.get_text('dict', sort=True)
        fn_lines = []
        if args.no_tables:
            page_tables, snap, tbl_bboxes = [], False, []
        else:
            page_tables, snap, tbl_bboxes = detect_tables(
                page, heading_ys=outline_ys.get(pno, ()))
        comp_here = COMP.get(pno)   # page belongs to a reconstructed multi-page comparison table
        page_has_deferred_table = False
        if comp_here or page_tables:
            snap = True             # deferred tables get a visual snapshot for inspection
            page_has_deferred_table = True
        if args.no_snapshots:
            snap = False
        # Geometric footprint of every deferred table on this page, used below
        # to decide which body-text lines are the table's own leaking cell
        # content (skip — MinerU/Stage 2 extracts them) vs. unrelated text that
        # merely shares the page (keep). tbl_bboxes is find_tables()'s raw,
        # unfiltered candidate set — a superset of page_tables' kept bboxes —
        # so it also covers regions dropped from page_tables (nested/pathological
        # tables) that can still leak a stray cell line. margin_y is generous
        # (a wrapped cell line can extend a full line-height past the box) but
        # nowhere near page-sized, so a heading/paragraph starting well below
        # the table is never swept in.
        table_regions = list(tbl_bboxes)
        if comp_here and comp_here.get('yr'):
            y0, y1 = comp_here['yr']
            table_regions.append((0, y0, page.rect.width, y1))
        comp_rejected_ranges = (comp_here.get('rejected_ranges') or []) if comp_here else []

        def _in_table_region(lx, ly, margin_x=2, margin_y=10):
            if any(y0 - 2 <= ly <= y1 + 2 for y0, y1 in comp_rejected_ranges):
                # a body-sized sub-heading (and its own wrapped text) that the
                # comparison-table reconstruction itself excluded — not table
                # territory even though it falls inside comp_here's yr span.
                return False
            return any(bb[0] - margin_x <= lx <= bb[2] + margin_x and
                       bb[1] - margin_y <= ly <= bb[3] + margin_y for bb in table_regions)
        # a page snapshotted only because detect_tables() found a table-like
        # region it didn't trust enough to render (page_tables empty, but a
        # bbox exists) still gets tried through MinerU — anchored to that
        # bbox's actual position on the page (below), not blindly appended at
        # page-end, so it can't get misattributed to a new section that
        # happens to start lower on the same page
        fallback_bboxes = tbl_bboxes if (not page_has_deferred_table and snap) else []
        # Per-region, not a single flag: a page can legitimately carry two
        # distinct table regions (see the per-table column test in
        # detect_tables). One shared boolean gave the whole page ONE
        # placeholder, and recorded bbox[0] for it regardless of which region
        # actually matched — so the second table was swallowed as prose with no
        # placeholder of its own, and the first could be anchored to the wrong
        # box entirely.
        fallback_done = set()
        frac_lines, frac_formulas, frac_unparsed = detect_fractions(
            page, dd, tbl_bboxes)
        emitted_formulas = set()
        if frac_unparsed and not args.no_snapshots and not comp_here:
            snap = True  # unassemblable formula: keep visual evidence (but never over a
            #              reconstructed comparison-table page — its table is already shown)

        # pre-scan: lowest body-size line on the page; small-font lines below
        # it form the footnote area (small text above it is fine print → body)
        max_body_y = -1.0
        for b in dd['blocks']:
            if b['type'] != 0:
                continue
            for l in b['lines']:
                y = l['bbox'][1]
                raw = ''.join(s['text'] for s in l['spans'])
                spans = [s for s in l['spans'] if s['text'].strip()]
                if not spans:
                    continue
                maxsz = max(s['size'] for s in spans)
                if (y < 95 or y > H - 95):
                    is_rep = (round(y / 8), band_key(raw)) in repeated or band_key(raw) in repeated_text
                    textual = bool(re.search(r'[a-z]', band_key(raw)))
                    digit_only = bool(re.fullmatch(r'\s*\d+\s*', raw))
                    if (is_rep and (textual or maxsz > foot_max)) or \
                            (digit_only and maxsz > foot_max):
                        continue
                if maxsz > foot_max:
                    max_body_y = max(max_body_y, y)

        # Parallel to block_lines: the same lines as (text, bold, italic) runs, so
        # emit() can serialise emphasis over the WHOLE paragraph (see emphasise).
        # Kept as a separate list rather than folded into block_lines so that every
        # existing consumer of a paragraph's 'text' — outline matching, band_key,
        # split_after_title, kept_words — sees byte-identical input to before.
        block_runs = []
        # One-element list, not a bare name: emit() is a closure that has to WRITE it
        # (clearing the item once emitted), same reason block_runs is mutated in place.
        # Holds the x of the bullet glyph that opened the paragraph now being built, or
        # None when this paragraph is not a list item.
        bullet_x = [None]

        def emit(lines_list):
            if lines_list:
                text = normspace(' '.join(lines_list))
                text = re.sub(r'^[•▪◦−]\s*', '- ', text)
                if text:
                    para = {'page': pno, 'text': text}
                    if not args.no_emphasis and len(block_runs) == len(lines_list):
                        joined = []
                        for k, ln in enumerate(block_runs):
                            joined.extend(ln)
                            if k < len(block_runs) - 1:
                                joined.append((' ', False, False))   # ' '.join above
                        md = normspace(emphasise(joined))
                        md = re.sub(r'^[•▪◦−]\s*', '- ', md)
                        # Two guards, both fail-safe to today's plain output:
                        #   - the token sequence must be untouched, so nothing a
                        #     downstream comparison can see has changed;
                        #   - no delimiter run of 4+, which markdown renders
                        #     literally (_collapse_touching should have removed
                        #     every one; this catches any shape it did not).
                        if (md != text
                                and not re.search(r'\*{4,}', md)
                                and _ALNUM_RUN_RE.findall(md) == _ALNUM_RUN_RE.findall(text)):
                            para['md'] = md
                            report['paragraphs_emphasised'] += 1
                    # The x-position of the GLYPH that opened this item, which is the
                    # only thing in the PDF that says how deep the item sits. Recorded
                    # raw here and turned into a level after the whole document is
                    # scanned (see apply_bullet_nesting) — a single page rarely shows
                    # every level the document uses, so the ladder cannot be read off
                    # one page in isolation.
                    if bullet_x[0] is not None and text.startswith('- '):
                        para['bullet_x'] = bullet_x[0]
                    paras.append(para)
                lines_list.clear()
                block_runs.clear()
                bullet_x[0] = None

        for b in dd['blocks']:
            if b['type'] != 0:
                continue
            block_lines = []
            block_runs.clear()   # block_lines is rebound per block; keep the two in step
            bullet_x[0] = None
            for l in b['lines']:
                y = l['bbox'][1]
                raw = ''.join(s['text'] for s in l['spans'])
                spans = [s for s in l['spans'] if s['text'].strip() or s['text'] == ' ']
                if not spans:
                    continue
                # classify by visible text size only (whitespace spans can
                # carry a different font size and would mislabel the line)
                vis = [s['size'] for s in spans if s['text'].strip()]
                if not vis:
                    continue
                maxsz = max(vis)
                # drop running headers/footers and bare page numbers, but never
                # small-font digit-only lines (those are footnote numbers)
                if (y < 95 or y > H - 95):
                    is_rep = (round(y / 8), band_key(raw)) in repeated or band_key(raw) in repeated_text
                    textual = bool(re.search(r'[a-z]', band_key(raw)))
                    digit_only = bool(re.fullmatch(r'\s*\d+\s*', raw))
                    if (is_rep and (textual or maxsz > foot_max)) or \
                            (digit_only and maxsz > foot_max):
                        continue
                # stacked-fraction lines: replaced by one inline formula
                if id(l) in frac_lines:
                    key = frac_formulas.get(('line', id(l)))
                    if key is not None and key not in emitted_formulas:
                        emit(block_lines)
                        paras.append({'page': pno, 'text': frac_formulas[key]})
                        emitted_formulas.add(key)
                        # the formula REPLACES its source lines, so count what is
                        # actually emitted, not the raw numerator/denominator text
                        kept_words += len(frac_formulas[key].split())
                    continue
                # lines inside a detected table: emit the whole table as
                # markdown at the position of its first line, skip the rest
                lx = (l['bbox'][0] + l['bbox'][2]) / 2
                ly = (l['bbox'][1] + l['bbox'][3]) / 2
                # Is this line one of the document's own OUTLINE TITLES? The outline
                # states what the document contains, so a line matching it is a
                # heading — not table cell content — whatever its size or position,
                # and neither swallow below may claim it. A wrapped or partial line
                # counts: printed headings break across lines ("10." on one, "LICENCE"
                # on the next), and the fragment is enough to know a heading is here.
                # An EXACT whole-line match to a title needs no length bar. The bar
                # exists so a short fragment cannot claim to be a heading by prefix,
                # but a line that IS the title is not a fragment. Sweden 174961 prints
                # its clause as "10." then "LICENCE" on the next line: "licence" is
                # seven characters, fell under an >= 8 bar, was swallowed with the
                # table, and clause 10 was never built — node 9 ran p96-102 over it.
                # title_prefixes drops it for the same reason, so the exact set is
                # built here without that filter.
                _lt = norm(''.join(_s.get('text', '') for _s in l.get('spans', ())))
                _is_outline_title = bool(_lt) and len(_lt) >= 4 and (
                    _lt in title_exacts
                    or (len(_lt) >= 8
                        and (_lt.startswith(title_prefixes)
                             or any(_x.startswith(_lt) for _x in title_prefixes))))
                # spans inside a reconstructed multi-page comparison table: emit the
                # stitched table once (at its position on the first page), skip the rest.
                # Two escapes from that swallow: (1) any heading-sized line, regardless
                # of position — a heading can legitimately sit BETWEEN two back-to-back
                # tables sharing one page (an earlier table's tail ending, then a
                # heading, then a new table starting right after) or trail a table
                # entirely; a data-table cell is never heading-sized, so letting it
                # through costs nothing. (2) a line inside one of this page's own
                # rejected_ranges — a body-sized sub-heading (e.g. "(b) Opt-out
                # methods") that got excluded from the table's OWN stitching (see
                # reconstruct_multipage_tables) because it matched a known outline
                # title, even though it isn't heading-SIZED; without this, its label
                # AND its own wrapped text would still be silently swallowed here even
                # though the table itself no longer contains it.
                in_rejected = any(y0 - 2 <= ly <= y1 + 2
                                  for y0, y1 in (comp_here.get('rejected_ranges') or [])) if comp_here else False
                # `not _is_heading_size(...)` rather than `maxsz < head_min`: this
                # gate decides BOTH that the line is swallowed into the multi-page
                # table AND — via comp_here['is_first'] below — that the table's
                # placeholder gets emitted at all. When a table's rows sit exactly
                # ON head_min, `maxsz < head_min` is false for every one of them, so
                # the run is claimed (prose suppressed) but no placeholder is ever
                # written and those pages never reach MinerU. That is exactly the
                # regression recorded in docs/REVERT_multipage_table_gate.md.
                if (comp_here and comp_here['yr'] and not in_rejected
                        and not _is_outline_title
                        and not _is_heading_size(maxsz, head_min)
                        and comp_here['yr'][0] - 2 <= ly <= comp_here['yr'][1] + 2):
                    if comp_here['is_first'] and not comp_here.get('done'):
                        emit(block_lines)
                        run_pages = sorted(pp + 1 for pp, v in COMP.items()
                                           if v['first'] == comp_here['first'])
                        table_id = f"table_{len(deferred_tables) + 1:03d}"
                        paras.append({'page': pno, 'text': table_placeholder(table_id, run_pages),
                                     'table': True})
                        # A multi-page table can't be described by ONE box, but each
                        # of its pages can: reconstruct_multipage_tables already
                        # computed the row band (yr) per page, so record it as a
                        # full-width bbox per page. Without this these tables reach
                        # Stage 2 with no geometry at all and are paired to MinerU
                        # blocks by page rank, i.e. accepted with no verification.
                        bboxes = {}
                        for _pp, _v in COMP.items():
                            if _v['first'] != comp_here['first'] or not _v.get('yr'):
                                continue
                            _lo, _hi = _v['yr']
                            _xs = _hrule_xspan(doc[_pp])
                            _x0, _x1 = _xs if _xs else (0.0, doc[_pp].rect.width)
                            bboxes[str(_pp + 1)] = [float(_x0), float(_lo),
                                                    float(_x1), float(_hi)]
                        deferred_tables.append({'table_id': table_id, 'pages': run_pages,
                                                'multipage': True, 'bboxes': bboxes})
                    comp_here['done'] = True
                    continue
                tb = next((t for t in page_tables
                           if t['bbox'][0] - 2 <= lx <= t['bbox'][2] + 2 and
                              t['bbox'][1] - 2 <= ly <= t['bbox'][3] + 2), None)
                if tb is not None:
                    nr = norm(raw)
                    # heading alone, or heading sharing its physical line with a
                    # neighbouring cell: keep it as text so matching works. The
                    # exact-title match alone misses a heading that WRAPS across
                    # multiple physical lines (common for a long outline title
                    # squeezed into a narrow column beside/above a wide table) —
                    # no single line's text then equals or is long enough to
                    # satisfy startswith() against the full title, so every one
                    # of its lines gets silently swallowed into the table region
                    # and never reaches paras at all (surfacing later as an
                    # "outline heading could not be located" warning, with its
                    # content simply gone, not merged anywhere). A data-table
                    # cell is essentially never heading-sized, so any line at or
                    # above head_min is exempted regardless of exact title text.
                    if (nr in title_norms or nr.startswith(title_prefixes)
                            or _is_heading_size(maxsz, head_min)):
                        tb = None
                # A band the MULTI-PAGE run already claims must never be deferred a
                # second time as a single-page region.
                #
                # This is reachable only because a sub-heading row inside the run
                # lands in rejected_ranges (see _page_bands): the run declines that
                # band so the heading survives as prose, the heading LINE itself is
                # rescued just above — and the band's remaining lines, which are the
                # heading's own wrapped text, then fall through to here and re-defer
                # the whole page. Harmless while runs were cut at every subsection
                # start; with a clause-long run it duplicates the entire clause.
                # Measured on Australia__181814 section 8 (p106-129): 9 spurious
                # single-page regions, each covering a band table_075 already holds
                # (p109 y76.1-496.8 against the run's y76.0-496.4).
                if tb is not None and args.clause_tables and comp_here and comp_here.get('yr'):
                    _lo, _hi = comp_here['yr']
                    # PRODUCT-SCOPED, though the duplication it removes is general —
                    # the default path emits the same overlapping pair (this
                    # document's original build has table_070 [106] beside
                    # table_071 [106,107,108]). Gated on clause_tables because
                    # suppressing it changes 4 of this document's 92 regions and the
                    # corpus-wide effect has not been measured; the clause-long runs
                    # are what make the duplicate span a whole clause instead of a
                    # page, so it has to be fixed here and can wait elsewhere.
                    #
                    # OVERLAP, not containment. The per-page bbox routinely starts
                    # ABOVE the run's band because find_tables() includes the header
                    # rows sitting before the page's first horizontal rule, while the
                    # run's yr begins AT that rule. Requiring containment therefore
                    # missed exactly the case this guard exists for: on
                    # Australia__181814 page 106 the per-page region is y102-522 and
                    # the run's band y186-522 — 80% of the region already claimed —
                    # so it survived, MinerU merged both into one block, and the
                    # clause's opening rows were rendered twice (a 10-row table whose
                    # every row is also in the 100-row clause table, immediately
                    # before it).
                    _h = tb['bbox'][3] - tb['bbox'][1]
                    _ov = min(tb['bbox'][3], _hi) - max(tb['bbox'][1], _lo)
                    if _h > 0 and _ov / _h >= RUN_CLAIMED_OVERLAP:
                        tb = None
                if tb is not None:
                    if not tb['done']:
                        emit(block_lines)
                        # The caption is deliberately NOT emitted: it is often
                        # mis-captured page prose (leading single-cell rows the
                        # detector hoisted), and it sits inside the region MinerU
                        # extracts anyway. The placeholder + MinerU table (or the
                        # snapshot) represents the whole region. It is still carried
                        # in the manifest so Stage 3 can render it above the table.
                        table_id = f"table_{len(deferred_tables) + 1:03d}"
                        paras.append({'page': pno, 'text': table_placeholder(table_id, [pno + 1]),
                                     'table': True})
                        deferred_tables.append({'table_id': table_id, 'pages': [pno + 1],
                                                'multipage': False, 'bbox': list(tb['bbox']),
                                                'caption': tb['caption']})
                        tb['done'] = True
                    continue
                _hit = next((bi for bi, bb in enumerate(fallback_bboxes)
                             if bb[0] - 2 <= lx <= bb[2] + 2 and bb[1] - 2 <= ly <= bb[3] + 2),
                            None) if fallback_bboxes else None
                if _hit is not None:
                    if _hit not in fallback_done:
                        emit(block_lines)
                        table_id = f"table_{len(deferred_tables) + 1:03d}"
                        paras.append({'page': pno, 'text': table_placeholder(table_id, [pno + 1]),
                                     'table': True})
                        # the region this line actually fell inside, not bbox[0]
                        deferred_tables.append({'table_id': table_id, 'pages': [pno + 1],
                                                'multipage': False,
                                                'bbox': list(fallback_bboxes[_hit]),
                                                'caption': '', 'source': 'snapshot_fallback_anchored'})
                        fallback_done.add(_hit)
                        page_has_deferred_table = True
                    continue
                if maxsz <= foot_max and y > max_body_y + 1:
                    # small font below the last body line → footnote area
                    # (small text higher up is fine print → treated as body)
                    fn_lines.append((y, l['spans']))
                    continue
                parts = []
                line_runs = []
                for s in spans:
                    t = s['text']
                    if (s['flags'] & 1) and s['size'] < body_size - 0.5 and t.strip().isdigit():
                        parts.append(f"[^{t.strip()}]")
                        # A footnote REFERENCE is never emphasised: the marker is
                        # ours, not the document's, and "**[^17]**" is not a thing.
                        line_runs.append((f"[^{t.strip()}]", False, False))
                    else:
                        parts.append(t)
                        line_runs.append((t, bool(s['flags'] & 16), bool(s['flags'] & 2)))
                line_text = ''.join(parts)
                # bold-caps detection also applies within a recovered pre-outline
                # range (pno < body_start) regardless of the document-wide
                # style_headings flag — see extended_start above. A no-op for
                # every document where extended_start == body_start, since this
                # loop never visits pno < body_start there.
                _style_head = (style_headings or pno < body_start) and is_style_heading(line_text, spans)
                # Within the recovered range specifically: a line that's NOTHING
                # but an enumeration token ("A.", "1.", "(c)") or a bare page
                # number is never a real heading — this only matters on a printed
                # Table of Contents page, where a dot-leader row often splits its
                # enum prefix, its title, and its trailing page number into
                # separate (sometimes larger/bolder) lines, otherwise producing
                # one content-less heading stub per row/token. _rest() (defined
                # above) already strips exactly this leading-token pattern for
                # title matching; reused here since "nothing left after
                # stripping" is precisely "this line IS just the token".
                # Deliberately scoped to pno < body_start, same as _style_head
                # above: outline-matched pages (pno >= body_start) rely on a
                # heading-flagged enum-only line to unlock the LENIENT wrapped-
                # title match (see "title wrapped across several paragraphs"
                # below) — many real numbered clauses split their "1.1" token
                # onto its own line, and un-flagging it there forces the STRICT
                # equality path instead, which several documents' clauses then
                # fail, merging real, separate headings into their parent.
                _enum_or_num_only = pno < body_start and (
                    bool(re.fullmatch(r'\d{1,4}', normspace(line_text))) or not _rest(line_text))
                if (maxsz >= head_min or _style_head) and not _enum_or_num_only:
                    # large-font heading line: isolate as its own paragraph
                    emit(block_lines)
                    # A style-marked heading gets a SYNTHETIC hsize so the tier
                    # ranking below works unchanged. head_min + 1 is guaranteed not
                    # to collide with a real size, because style_headings is only on
                    # when nothing in the document reaches head_min at all.
                    paras.append({'page': pno, 'text': normspace(line_text),
                                  'heading': True,
                                  'hsize': round(head_min + 1, 1) if _style_head
                                           else round(maxsz, 1)})
                    kept_words += len(line_text.split())
                else:
                    # Japan 172122 lost clauses 4, 5 (p16) and 9, 10, 11 (p66, p66,
                    # p72) here — their headings are set at exactly the body size, so
                    # they fail the large-font test above, and the clause-table run
                    # claimed their whole page. Sweden 174961 lost "10 LICENCE" (p99)
                    # the same way, its node running 96-102 straight over the clause.
                    if (page_has_deferred_table and not _is_outline_title
                            and _in_table_region(lx, ly)):
                        # This body-text line is the table's own cell content
                        # leaking past the per-line bbox checks above (a wrapped
                        # cell line, or a nested/pathological region find_tables()
                        # located but page_tables dropped) — MinerU (Stage 2)
                        # extracts the table itself, so emitting this here too
                        # would duplicate it as loose prose. Scoped to the
                        # table's own footprint (see table_regions above), NOT
                        # the whole page: a heading or paragraph that starts
                        # further down the same page, unrelated to the table,
                        # is never swept in by this and is kept normally.
                        continue
                    if line_text.lstrip().startswith(('•', '▪', '◦', '−', '– ')):
                        emit(block_lines)  # new bullet item = new paragraph
                        # Set AFTER the emit above, which flushes (and clears the x of)
                        # the PREVIOUS item. The glyph frequently sits on a line of its
                        # own with the item's text hanging beside it on the next line,
                        # so the glyph line's own x0 is the item's depth — not the x0 of
                        # the text, which is one indent step further right.
                        bullet_x[0] = l['bbox'][0]
                    block_lines.append(line_text)
                    block_runs.append(line_runs)
                    kept_words += len(line_text.split())
            emit(block_lines)
        unclaimed_fine_print = process_fn_lines(fn_lines)
        if unclaimed_fine_print:
            text = normspace(' '.join(unclaimed_fine_print))
            if text:
                paras.append({'page': pno, 'text': text})
                kept_words += len(text.split())
        if snap:
            snapshot_pages.add(pno + 1)
            text = f'@@PAGEIMG:{pno + 1}@@'
            # a snapshotted page with no already-deferred table region (diagram_heavy
            # or an unparsed formula, neither of which locates a bbox to anchor to)
            # still gets a MinerU attempt — it may find a table our own geometry
            # heuristics couldn't. Unlike the bbox-anchored fallback above, this one
            # has no position to insert at, so it's appended at page-end same as
            # before — tagged distinctly ("_unanchored") since that means it can
            # still land in the wrong section if a heading starts lower on this
            # same page; if it finds nothing, Stage 3 just flags it, same as any
            # other failed table (see run_stage2 in scripts/hybrid_extract.py)
            if not page_has_deferred_table:
                table_id = f"table_{len(deferred_tables) + 1:03d}"
                text += '\n\n' + table_placeholder(table_id, [pno + 1])
                deferred_tables.append({'table_id': table_id, 'pages': [pno + 1],
                                        'multipage': False, 'bbox': None, 'caption': '',
                                        'source': 'snapshot_fallback_unanchored'})
            paras.append({'page': pno, 'text': text})

    # Every page has been scanned, so the document's full set of glyph positions is
    # known and the indent ladder can be read from it. Before the tree is built, so the
    # nesting is in the paragraph text every later pass and the renderer see.
    if not args.no_bullet_nesting:
        apply_bullet_nesting(paras, report)

    report['paragraphs'] = len(paras)
    report['footnotes_found'] = len(footnotes)
    report['pages_snapshotted'] = sorted(snapshot_pages)
    report['tables_deferred'] = len(deferred_tables)

    # ---- build item list ----
    if not heuristic:
        items = [{'level': lvl, 'title': normspace(t), 'page': pg - 1}
                 for lvl, t, pg in toc]
        # sequential matching of outline titles to paragraphs
        pi = 0
        unmatched = []
        for it in items:
            target = norm(it['title'])
            # title minus a leading enumeration token ("C.", "1.2", "(a)"),
            # for cases where the token sits in a separate text block
            rest = _rest(it['title'])
            found = None
            # The loose window below (title appearing ANYWHERE near the start of a
            # paragraph) matches a cross-reference as readily as the heading itself:
            # "Do not address here (or in B1.2 Local representative) filing of a
            # register…" claims heading "1.2 Local representative" two paragraphs
            # before the real one, so that section's boundary moves back, the
            # preceding section loses its answer, and the paragraph in between is
            # consumed as a heading and disappears from the output entirely.
            # A loose hit on a NON-heading paragraph is therefore remembered rather
            # than accepted, and a real heading line in the same window wins. With no
            # heading found, behaviour is exactly as before.
            loose_fallback = None
            for j in range(pi, len(paras)):
                p = paras[j]
                if p['page'] < it['page'] - 1:
                    continue
                if p['page'] > it['page'] + 2:
                    break
                n = norm(p['text'])
                if n.startswith(target):
                    found = j
                    break
                # The length bar exists because a SHORT title matches a
                # cross-reference as readily as its own heading (see the comment on
                # loose_fallback). A title carrying a leading enumeration is not short
                # in that sense: "10licence" is specific where bare "licence" is not,
                # and rest != target is exactly the test for "had an enumeration".
                # Without this, Bahamas/Bahrain/Belgium lost section 10 LICENCE
                # outright — its heading fuses into the paragraph of the empty section
                # above it ("9 [SECTION INTENTIONALLY LEFT BLANK]"), so the paragraph
                # reads "9sectionintentionallyleftblank10licence", and at 9 normalised
                # characters "10licence" fell under the bar that would have found it.
                if (len(target) > 12 or rest != target) and target in n[:len(target) + 30]:
                    if p.get('heading'):
                        found = j
                        break
                    if loose_fallback is None:
                        loose_fallback = j
                    continue
                # A wrapped title, accumulated across the paragraphs it broke over.
                # Seeded from the full title AND from `rest` (the title minus its
                # leading enumeration), because the printed heading and the contents
                # page routinely disagree about that prefix: Australia__181814's
                # appendix 5 prints "APPENDIX 5" on one line and
                # "RECOGNISED ... FOR A" / "FOREIGN AFSL" on the next two, while the
                # contents page calls it "5 Recognised ... Foreign AFSL". No line is
                # a prefix of the full title — "appendix5" is not — so the
                # target-only seed never started, and a whole appendix was dropped.
                # Appendices 1-4 matched only because their titles happen to fit on
                # one line, which is why this went unnoticed.
                #
                # `rest` is tried second and needs a seed of real length, so it can
                # only ever find a title the target seed already failed on.
                for _goal in ((target,) if rest == target else (target, rest)):
                    if not n or not _goal.startswith(n):
                        continue
                    if _goal is rest and len(n) < 8:
                        continue          # too short a fragment to trust as a seed
                    acc, k, consumed = n, j + 1, []
                    while (k < len(paras) and len(acc) < len(_goal)
                           and k - j < 6):
                        if not paras[k].get('skip'):
                            acc += norm(paras[k]['text'])
                            consumed.append(k)
                        k += 1
                    ok = (acc == _goal if not p.get('heading')
                          else acc.startswith(_goal))
                    if ok:
                        for kk in consumed:
                            paras[kk]['skip'] = True
                        found = j
                        break
                if found is not None:
                    break
                if rest != target and len(rest) > 5 and n.startswith(rest) \
                        and (p.get('heading') or len(n) <= len(rest) + 10):
                    found = j
                    break
            if found is None:
                found = loose_fallback
            it['pidx'] = found
            if found is not None:
                # The matched paragraph often carries the section's opening body text
                # fused to its title. content_between() skips this paragraph, so
                # without splitting, that body is lost. Stash the remainder; the title
                # itself still renders as the heading.
                _p = paras[found]
                if not _p.get('heading') and not _p.get('tail'):
                    _h, _t = split_after_title(_p['text'], norm(it['title']))
                    if _t:
                        _p['text'], _p['tail'] = _h, _t
                        # The split walks RAW characters to find the title's end, so
                        # it cannot be applied to the emphasised copy without risking
                        # a cut BETWEEN a pair of markers ("**Title" / "** body").
                        # Drop it: this paragraph loses its emphasis, never its words.
                        _p.pop('md', None)
                pi = found
            else:
                unmatched.append(it['title'])
        matched = [it for it in items if it['pidx'] is not None]
        # Recovered pre-outline pages (see extended_start above) have no toc
        # entries to match against — build their own mini hierarchy the same
        # way the no-outline heuristic branch below does, ranked by hsize, and
        # place it BEFORE the outline's own items: their pidx is always lower,
        # since they come from earlier pages in the same paras list.
        # Front matter is recovered so its CONTENT is not lost (see extended_start),
        # but a contents-page row must never become a heading — it names a section that
        # appears again for real further down, so promoting it duplicates the clause.
        # STRICTLY THE OUTLINE. When a document HAS a bookmark outline, that outline is
        # the only source of sections — no heading is invented from font size on the
        # pages before it starts.
        #
        # Those pages are the cover and the PRINTED CONTENTS PAGE, and promoting their
        # lines to headings duplicates the whole document as empty stubs. _is_toc_row
        # was supposed to stop it, but it matches leader dots or a trailing page number
        # on ONE line; Germany (Data Privacy)__180656 prints its contents
        # geometrically — "A." / "Introduction" / "10" as three separate positioned
        # runs — so every line tests False and pages 1-9 became a mirror of the
        # document under 02-germany/01-contents/. The stubs then claimed the real
        # titles first: headings_matched fell to 285 of 594, with 309 outline headings
        # reported as unlocatable.
        #
        # Front-matter TEXT is unaffected: extended_start still begins the scan at
        # page 0, so the cover and contents prose are still emitted — they just land
        # under the first real section instead of inventing sections of their own.
        pre_paras = []
        pre_sizes = sorted({p['hsize'] for _, p in pre_paras}, reverse=True)
        pre_tiers = {sz: min(i + 1, 6) for i, sz in enumerate(pre_sizes)}
        # A cover title WRAPS, and every line of it is bold, caps and heading-sized —
        # so one line per section turned Bermuda__166524's title into six sections:
        # "RESTRICTIONS ON CROSS-BORDER" / "MARKETING AND SELLING OF" / "FUNDS AND
        # INVESTMENT MANAGEMENT & ADVISORY SERVICES" / "INTO" / "BERMUDA" / "PART B".
        # Read together that is one sentence, and none of it is in the outline. Six of
        # that document's 22 sections were fragments of it, one of them ("INTO")
        # holding a single token — which also made any stray footer word a whole
        # "silent gap", and shrank the denominator every per-section penalty divides by.
        #
        # Consecutive paragraphs on the SAME page at the SAME heading size with nothing
        # between them are one wrapped heading. All three conditions are needed: same
        # size alone would fuse a title with a later same-sized label, and adjacency in
        # `paras` is what proves no body text separates them.
        runs: list[list[tuple[int, dict]]] = []
        for j, p in pre_paras:
            last = runs[-1][-1] if runs else None
            if (last is not None and j == last[0] + 1
                    and p['page'] == last[1]['page']
                    and p['hsize'] == last[1]['hsize']):
                runs[-1].append((j, p))
            else:
                runs.append([(j, p)])
        pre_matched = [{'level': pre_tiers[run[0][1]['hsize']],
                        'title': normspace(' '.join(q['text'] for _, q in run)),
                        'page': run[0][1]['page'], 'pidx': run[0][0]}
                       for run in runs]
        matched = pre_matched + matched
        report['headings_total'] = len(items) + len(pre_matched)
        report['headings_matched'] = len(matched)
        if unmatched:
            report['warnings'].append(
                f"{len(unmatched)} outline headings could not be located in the text "
                f"(their content merged into the parent section): {unmatched[:10]}")
    else:
        # heuristic: heading paragraphs, levels by font size rank
        hsizes = sorted({p['hsize'] for p in paras if p.get('heading')}, reverse=True)
        tiers = {sz: min(i + 1, 6) for i, sz in enumerate(hsizes)}
        matched = [{'level': tiers[p['hsize']], 'title': p['text'],
                    'page': p['page'], 'pidx': j}
                   for j, p in enumerate(paras) if p.get('heading')]
        report['headings_total'] = report['headings_matched'] = len(matched)
        if not matched:
            # Say what was actually tried and give the one measurement that
            # distinguishes the two real causes. The old message asserted "Is this a
            # scanned document?" — on the pilot corpus every document that hit this
            # had 24k-46k characters of text, so that question sent anyone debugging
            # it looking for an OCR problem that did not exist.
            chars = sum(len(doc[i].get_text('text')) for i in range(min(npages, 12)))
            sys.exit(
                "No structure found: no bookmark outline, no heading-sized text, and "
                "no BOLD CAPITALISED lines to fall back on.\n"
                f"  text in first {min(npages, 12)} page(s): {chars} characters"
                + ("  -> effectively no text layer; this looks genuinely scanned and "
                   "needs OCR before extraction." if chars < 500 else
                   "  -> there IS a text layer, so this is a heading-DETECTION gap, "
                   "not a scanned document. Its headings must be marked some other "
                   "way (colour, indentation, italics, numbering alone)."))

    # ---- a group label printed above a heading belongs IN that heading's section ----
    # "APPENDIX 2" over "DISCLAIMERS: CLOSED-ENDED FUND": only the second line is ever
    # recognised as a heading, because the bookmark outline and the contents page both
    # name the appendix "2 Disclaimers: Closed-Ended Fund". The label therefore fell one
    # paragraph short of the section it introduces and was emitted as the LAST LINE of the
    # PREVIOUS appendix's content -- 383 such lines across this corpus, each one telling a
    # reader they had reached appendix 2 while sitting at the bottom of appendix 1.
    #
    # The label is moved, and NOTHING ELSE. It is not folded into the title, which was the
    # first attempt and cost 61 documents their gate: the title is the chunk's name and its
    # entry in the section map, so rewriting it moves the node the scorecard is checking.
    # Measured over 91 documents -- appendix labels went 15 -> 312 and orphans 310 -> 13,
    # exactly as intended, while `sectioning` collapsed (Bahrain 100 -> 73.3, Australia
    # 100 -> 9.3) and pass fell 75 -> 14 with 6 outright failures. The orphan was a content
    # problem all along, so the repair belongs in the content.
    #
    # Carried on the heading paragraph's `tail`, the same channel already used for body
    # text found fused to a title (see split_after_title): content_between emits a
    # section's tail as its opening line. So the label leaves the section above and opens
    # the section it names, and the heading, the title, the chunk name, the level and the
    # section map are all byte-for-byte what they were.
    #
    # Done on `matched` because every heading source converges there -- the PDF outline,
    # the printed-TOC rebuild, a pruned outline and the font/bold heuristic -- so one pass
    # covers four paths. Not done by rewriting contents-page titles either: those feed the
    # choice of outline source itself, and changing them flipped Portugal 176637 off its
    # verified 47-entry TOC onto a pruned 396-bookmark outline.
    #
    # Conditions, each load-bearing: the label alone on its line (see _GROUP_LABEL_RE), on
    # the SAME page, in the IMMEDIATELY preceding paragraph (which is what proves no body
    # text separates them), and NOT itself a heading -- where a document with no outline
    # marks both lines bold, the label already has a section of its own (Italy 181491,
    # Russian Federation 89570) and there is no orphan to move. Product-scoped: the shape
    # has only been counted on 124_Marketing_Restrictions_-_Asset_Management.
    _heading_pidx = {it.get('pidx') for it in matched} if args.group_labels else set()
    for _it in (matched if args.group_labels else ()):
        _j = _it.get('pidx')
        if not _j or _j <= 0 or (_j - 1) in _heading_pidx:
            continue
        _prev = paras[_j - 1]
        if _prev.get('skip') or _prev.get('page') != paras[_j].get('page'):
            continue
        if not _GROUP_LABEL_RE.match(normspace(_prev['text'])):
            continue
        _label = normspace(_prev['text'])
        _tail = paras[_j].get('tail')
        paras[_j]['tail'] = f"{_label}\n\n{_tail}" if _tail else _label
        _prev['skip'] = True               # out of the section above; content_between skips it
        report['group_labels_moved'] = report.get('group_labels_moved', 0) + 1

    # ---- a group label NOTHING claimed opens a section of its own ----
    # The block above MOVES a label into a heading that some other source already found.
    # Where no source found the title beneath it, there is nothing to move it into and the
    # label stays body text — so the appendix it names never becomes a section at all.
    #
    # That is not a cosmetic loss. An appendix left as loose text inside the appendix above
    # it is then an unheaded block in stage 4's input, which stage 4 reads as stray
    # duplication and DELETES: measured on the 16-09-26 corpus, Malta 181816 lost
    # appendices 3 and 4 (22,912 -> 13,115 bytes), Iceland 181135 lost 3 and 4, Singapore
    # 175285 lost appendix 4 (10,256 -> 2,138) and Kazakhstan 174813 lost appendix 4. Not
    # relocated — a distinctive sentence from each is absent from the whole stage-4 tree.
    #
    # Neither navigation source can supply the missing entry, which is why this has to come
    # off the page. The omission originates there: Malta's bookmark outline AND its printed
    # contents page both list Disclaimers 1 and 2 only, and Singapore's outline lists
    # 1, 2, 3, 5 — numbering its last appendix 5 while never naming the 4 that must exist.
    #
    # Deliberately narrower than the "STRICTLY THE OUTLINE" rule it sits beside: this
    # invents nothing from font size, it promotes a line the page itself prints in the
    # shape _GROUP_LABEL_RE was measured on (138 of 139 such labels are immediately
    # followed by their title). Four conditions, each load-bearing:
    #   * the label is alone on its line, so a cross-reference cannot match;
    #   * nothing already claimed it — neither the heading it belongs to (which the block
    #     above would have handled) nor a heading of its own;
    #   * it sits AFTER the first real heading, so a contents-page row cannot become a
    #     section — the failure the "STRICTLY THE OUTLINE" note above records; and
    #   * its title paragraph is short enough to BE a title, else the label stands alone.
    if args.group_labels and matched:
        _claimed = {it.get('pidx') for it in matched}
        _first = min(p for p in _claimed if p is not None)
        _promoted = []
        for _j, _para in enumerate(paras):
            if _j <= _first or _j in _claimed or _para.get('skip'):
                continue
            if (_j + 1) in _claimed:
                continue                  # the relocation pass above owns this one
            _txt = normspace(_para.get('text') or '')
            _lm = _GROUP_LABEL_RE.match(_txt)
            if not _lm or _is_toc_row(_txt):
                continue
            # ALL-CAPS only, which the relocation pass above does not require and must not:
            # it is moving a label INTO a heading another source already found, so the
            # heading itself is the evidence. Here there is no such evidence, and case is
            # what separates this document's own divisions from a quoted instrument's.
            # Malaysia 176985's appendix 5 reproduces the Malaysian SC's "Guidelines for
            # the Offering, Marketing and Distribution of Foreign Funds" verbatim, that
            # instrument's own title-case "Appendix 1".."Appendix 4" included: promoting
            # those made four SIBLINGS of the memo's appendix 5 out of its own contents.
            # Every genuine case measured here prints the label in caps.
            if not _lm.group('word').isupper():
                continue
            # The descriptive title on the next usable paragraph of the same page.
            _k = None
            for _t in range(_j + 1, min(_j + 4, len(paras))):
                _cand = paras[_t]
                if _cand.get('skip') or not (_cand.get('text') or '').strip():
                    continue
                _ct = normspace(_cand['text'])
                if (_cand.get('page') == _para.get('page') and len(_ct) <= 160
                        and not _GROUP_LABEL_RE.match(_ct)):
                    _k = _t
                break
            if _k is None:
                # No title under it: the label IS the heading. Better a section named
                # "APPENDIX 3" than an appendix that is not a section.
                _promoted.append({'level': 1, 'title': _txt, 'page': _para['page'], 'pidx': _j})
                continue
            # Same shape the relocation pass produces, and the same shape an
            # outline-matched appendix already has: the TITLE is the heading and the label
            # opens the body, so stage 5 can still read "APPENDIX 3" off the section.
            _tail = paras[_k].get('tail')
            paras[_k]['tail'] = f"{_txt}\n\n{_tail}" if _tail else _txt
            _para['skip'] = True
            _promoted.append({'level': 1, 'title': normspace(paras[_k]['text']),
                              'page': paras[_k]['page'], 'pidx': _k})
        if _promoted:
            matched.extend(_promoted)
            matched.sort(key=lambda it: it.get('pidx') if it.get('pidx') is not None else -1)
            report['group_labels_promoted'] = len(_promoted)
            report['headings_total'] += len(_promoted)
            report['headings_matched'] += len(_promoted)
            report['warnings'].append(
                f"{len(_promoted)} group label(s) (" +
                ", ".join(sorted({normspace(paras[x['pidx']].get('text') or x['title'])[:24]
                                  for x in _promoted})) +
                ") named a section no outline or contents entry did — promoted to headings "
                "so the division they open becomes a chunk of its own.")

    # Content before the FIRST matched heading (cover page, contact block,
    # disclaimer text, even a whole undetected table sitting on it) has
    # nowhere to go below: render_subtree/content_between only ever walk
    # BETWEEN heading pairs, so anything before matched[0]'s own pidx never
    # reaches any file — silently. Modelled as an implicit top-level section,
    # the same way a section's OWN pre-child-heading text already gets a
    # synthetic overview file (see the `intro` handling in emit_item below) —
    # just one level up, for the document as a whole. pidx=-1, not 0:
    # content_between()'s default skip_first=True assumes p_start IS a
    # heading paragraph and skips past it — true for every real heading, but
    # this synthetic entry has no heading paragraph of its own, so -1 makes
    # range(-1+1, p_end) start at paras[0] instead of skipping it.
    # A no-op by construction whenever the first heading already starts at
    # paragraph 0 (the common case).
    if matched and matched[0]['pidx'] > 0:
        front_pages = [paras[j]['page'] for j in range(matched[0]['pidx'])
                       if not paras[j].get('skip') and paras[j]['text'].strip()]
        if front_pages:
            matched.insert(0, {'level': 1, 'title': 'Front Matter',
                               'page': min(front_pages), 'pidx': -1})
            report['headings_total'] += 1
            report['headings_matched'] += 1
            report['warnings'].append(
                f"{matched[1]['pidx']} paragraph(s) before the first recognized heading "
                "(page(s) " + ", ".join(str(p + 1) for p in sorted(set(front_pages))) +
                ") were previously dropped — now kept as a synthetic 'Front Matter' section.")

    # ---- rendering helpers ----
    def children_of(idx):
        lvl = matched[idx]['level']
        out = []
        for j in range(idx + 1, len(matched)):
            if matched[j]['level'] <= lvl:
                break
            if matched[j]['level'] == lvl + 1:
                out.append(j)
        return out

    def subtree_end(idx):
        lvl = matched[idx]['level']
        j = idx + 1
        while j < len(matched) and matched[j]['level'] > lvl:
            j += 1
        return j

    def content_between(p_start, p_end, skip_first=True):
        out = []
        # The skipped paragraph is the heading itself -- but if its title had body
        # text fused to it, that body was split off at match time and belongs to
        # this section, first.
        if skip_first and 0 <= p_start < len(paras):
            _tail = paras[p_start].get('tail')
            if _tail and not paras[p_start].get('skip'):
                out.append(_tail)
        for j in range(p_start + (1 if skip_first else 0), p_end):
            if not paras[j].get('skip'):
                # 'md' is the same paragraph with the PDF's own bold/italic runs
                # carried over; 'text' is the plain form every matching pass above
                # ran on. Only the rendered tree sees the emphasised one.
                out.append(paras[j].get('md') or paras[j]['text'])
        return out

    def render_subtree(idx, base_level):
        lines = []
        lvl0 = matched[idx]['level']
        end_i = subtree_end(idx)
        seq = list(range(idx, end_i))
        for k, mi in enumerate(seq):
            it = matched[mi]
            rel = min(base_level + (it['level'] - lvl0), 6)
            lines += ['#' * rel + ' ' + it['title'], '']
            p_end = matched[seq[k + 1]]['pidx'] if k + 1 < len(seq) else (
                matched[end_i]['pidx'] if end_i < len(matched) else len(paras))
            for t in content_between(it['pidx'], p_end):
                lines += [t, '']
        return lines

    def used_footnotes(text):
        return sorted({int(m) for m in re.findall(r'\[\^(\d+)\]', text)})

    def split_merged_ref(n):
        """e.g. 1516 -> (15, 16); 354355 -> (354, 355) when both exist and consecutive"""
        s = str(n)
        for cut in range(1, len(s)):
            a, b = s[:cut], s[cut:]
            if b.startswith('0'):
                continue
            n1, n2 = int(a), int(b)
            if n2 == n1 + 1 and n1 in footnotes and n2 in footnotes:
                return n1, n2
        return None

    orphan_fns = set()

    def finalize(lines):
        body = '\n'.join(lines).rstrip() + '\n'
        # fix adjacent footnote refs that merged into one number
        for n in used_footnotes(body):
            if n not in footnotes:
                sp = split_merged_ref(n)
                if sp:
                    body = body.replace(f'[^{n}]', f'[^{sp[0]}][^{sp[1]}]')
        fns = used_footnotes(body)
        if fns:
            body += '\n---\n\n'
            for n in fns:
                if n in footnotes:
                    body += f'[^{n}]: {footnotes[n]}\n'
                else:
                    orphan_fns.add(n)
                    body += f'[^{n}]: *(footnote body not present in source PDF)*\n'
        return body

    # ---- write tree ----
    OUT = args.out.rstrip('/')
    os.makedirs(OUT, exist_ok=True)
    manifest = []

    def write_file(path, content, page=None):
        if path in manifest:  # duplicate heading text under same parent
            stem, ext = os.path.splitext(path)
            k = 2
            while f"{stem}-{k}{ext}" in manifest:
                k += 1
            path = f"{stem}-{k}{ext}"
        # resolve page-snapshot markers with the right relative prefix
        depth = path.count('/')
        def _img(m):
            n = int(m.group(1))
            rel = '../' * depth + f'_assets/page-{n:03d}.png'
            return (f"> Page {n} of the source PDF contains a complex "
                    f"table/diagram; snapshot for reference:\n\n"
                    f"![Page {n}]({rel})")
        content = re.sub(r'@@PAGEIMG:(\d+)@@', _img, content)
        full = os.path.join(OUT, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(content)
        manifest.append(path)
        return os.path.basename(path)

    def item_prefix(title, seq):
        m = re.match(r'^([A-Z])\.\s', title)
        if m:
            return m.group(1)
        m = re.match(r'^(\d+(?:\.\d+)+)', title)
        if m:
            return m.group(1)
        m = re.match(r'^\((\w+)\)', title)
        if m:
            return m.group(1).lower()
        m = re.match(r'^(\d+)\.\s', title)
        if m:
            return f"{int(m.group(1)):02d}"
        return f"{seq:02d}"

    pdf_name = os.path.basename(args.pdf)

    def src_line(idx):
        it = matched[idx]
        pg = it['page'] + 1
        end_i = subtree_end(idx)
        end_pg = (matched[end_i]['page'] if end_i < len(matched) else npages - 1) + 1
        rng = f"{pg}" if end_pg <= pg else f"{pg}–{end_pg}"
        return f"*Source: `{pdf_name}`, page {rng}*"

    def emit_item(idx, rel_dir, depth_left, seq):
        it = matched[idx]
        pref = item_prefix(it['title'], seq)
        base = f"{pref}-{slug(it['title'])}"
        kids = children_of(idx)
        pg = it['page'] + 1
        if kids and depth_left > 0:
            d = os.path.join(rel_dir, base)
            index = [f"# {it['title']}", '', src_line(idx), '']
            intro = content_between(it['pidx'], matched[kids[0]]['pidx'])
            if any(x.strip() for x in intro):
                write_file(os.path.join(d, '00-overview.md'),
                           finalize([f"# {it['title']} — Overview", '',
                                     f"*Source: `{pdf_name}`, page {pg}*", ''] +
                                    [x for t in intro for x in (t, '')]), pg)
                index.append('- [Overview](00-overview.md)')
            for s2, kidx in enumerate(kids, 1):
                name = emit_item(kidx, d, depth_left - 1, s2)
                index.append(f"- [{matched[kidx]['title']}]({name})")
            write_file(os.path.join(d, 'README.md'), '\n'.join(index) + '\n', pg)
            return f"{base}/README.md"
        else:
            lines = render_subtree(idx, 1)
            lines[2:2] = [src_line(idx), '']  # after "# title" + blank
            return write_file(os.path.join(rel_dir, base + '.md'),
                              finalize(lines), pg)

    title = (doc.metadata.get('title') or '').strip() or \
        os.path.splitext(os.path.basename(args.pdf))[0]
    top = [i for i, it in enumerate(matched) if it['level'] == 1]
    root_index = [f"# {title}", '']
    root_index += [f"Converted from `{pdf_name}` ({npages} pages). "
                   "Footnotes preserved as markdown footnotes; ruled tables as "
                   "markdown tables. "
                   "See [CONVERSION_REPORT.md](CONVERSION_REPORT.md) for verification stats.",
                   '', '## Contents', '']
    for s1, tidx in enumerate(top, 1):
        name = emit_item(tidx, '', args.depth, s1)
        root_index.append(f"- [{matched[tidx]['title']}]({name})")
    write_file('README.md', '\n'.join(root_index) + '\n', 1)

    # ---- render page snapshots ----
    if snapshot_pages:
        os.makedirs(os.path.join(OUT, '_assets'), exist_ok=True)
        for n in sorted(snapshot_pages):
            pix = doc[n - 1].get_pixmap(dpi=110)
            pix.save(os.path.join(OUT, '_assets', f'page-{n:03d}.png'))

    # ---- verification report ----
    md_words = 0  # content only — README indexes / CONVERSION_REPORT are generated scaffolding, not source
    for r, _, fs in os.walk(OUT):
        for f in fs:
            if f.endswith('.md') and f not in ('README.md', 'CONVERSION_REPORT.md'):
                md_words += len(open(os.path.join(r, f)).read().split())
    pdf_words = kept_words  # source words excluding stripped headers/footers
    delta = (md_words - pdf_words) / max(pdf_words, 1) * 100
    report['pdf_words'] = pdf_words
    report['md_words'] = md_words
    # signed: positive = md larger (page refs, per-file footnotes, table
    # separators all add words); negative = content may be missing
    report['word_delta_pct'] = round(delta, 2)
    # Includes the four artifacts still to be written below — CONVERSION_REPORT.md
    # (via write_file, so it would otherwise be counted too late) plus the three
    # JSON manifests (which bypass write_file entirely and were never counted).
    # Stated arithmetically because this number has to appear INSIDE the report
    # that is one of the files it counts.
    report['files_written'] = len(manifest) + 4
    report['orphan_footnote_refs'] = sorted(orphan_fns)
    if delta < -5:
        report['warnings'].append(
            f'Markdown has {-delta:.1f}% fewer words than the source — '
            'content may be missing. Spot-check against the PDF.')

    rep = ['# Conversion report', '']
    for k, v in report.items():
        if k != 'warnings':
            rep.append(f'- **{k}**: {v}')
    if report['warnings']:
        rep += ['', '## Warnings', ''] + [f'- {w}' for w in report['warnings']]
    else:
        rep += ['', 'No warnings — conversion verified clean.']
    write_file('CONVERSION_REPORT.md', '\n'.join(rep) + '\n')
    # Same content as CONVERSION_REPORT.md, machine-readable — downstream
    # scoring (check_scorecard.py) needs page count, snapshotted pages, heading
    # match rate and orphan footnote refs without re-parsing markdown.
    with open(os.path.join(OUT, 'stage1_report.json'), 'w') as f:
        json.dump(report, f, indent=2)

    with open(os.path.join(OUT, 'tables_manifest.json'), 'w') as f:
        json.dump({'source_pdf': os.path.abspath(args.pdf),
                  'tables': deferred_tables,
                  # Every footnote number this (independent of MinerU) pass
                  # resolved a BODY for — ground truth Stage 2/3 can cross-check
                  # a MinerU-mangled superscript against (see
                  # hybrid_extract.clean_mineru_text) instead of guessing from
                  # typography MinerU may already have discarded.
                  'footnote_ids': sorted(footnotes.keys())}, f, indent=2)
    # Ground truth for a downstream placement-invariant check (does a table's
    # actual page fall inside the section it got assigned to?) — 'page' here
    # is 1-indexed to match tables_manifest.json's convention (paras/matched
    # use 0-indexed 'page' internally, converted at the boundary here only).
    with open(os.path.join(OUT, 'headings_manifest.json'), 'w') as f:
        json.dump({'structure_source': report.get('structure_source'),
                  # The nesting depth this tree was BUILT at. Recorded because a
                  # validator cannot otherwise tell a subsection that was lost
                  # from one that was deliberately collapsed: a --depth 0 tree
                  # holds only level-1 sections on purpose, and checked against
                  # the default it reports 25 of Australia__181814's 40 outline
                  # entries as missing. See product_rules.census_levels.
                  'build_depth': args.depth,
                  'clause_tables': bool(args.clause_tables),
                  'headings': [{'level': it['level'], 'title': it['title'],
                                'page': it['page'] + 1} for it in matched]},
                 f, indent=2)

    # ONE LINE, and still valid JSON. Pretty-printing this to stdout put a 60-line blob in the
    # log for every document, which is unreadable in a log stream where one line is one record —
    # a 1,088-document run emitted ~65,000 lines of it. The indented copy is written to
    # report.json next to the tree for anyone reading it by hand.
    print(json.dumps(report, separators=(",", ":")))


if __name__ == '__main__':
    main()
