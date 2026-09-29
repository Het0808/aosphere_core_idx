#!/usr/bin/env python3
"""rescue_outline — a FALLBACK stage for documents the normal pipeline failed on.

Nothing here runs during a normal extraction and nothing here changes the normal
extraction: Stage 1 is still pdf2mdtree reading the PDF's own bookmark outline. This
is a second pass, run afterwards, only against documents that already scored badly.

The failure it repairs
----------------------
pdf2mdtree treats "the PDF has bookmarks" as "the bookmarks describe the document"
and starts reading at the first bookmark. When a publisher bookmarks only the
appendix — Curaçao 164302 has exactly two bookmarks, both on page 37 of 40 — the
first 36 pages are never visited and completeness scores 0 on a document whose text
layer was perfectly readable all along.

Those documents almost always carry a printed Table of Contents that is complete and
correct. This stage reads THAT, verifies it against the document, and writes a copy
of the PDF whose bookmark outline has been replaced with it. The repaired copy then
goes through the ordinary Stage 1-3 + validate + score path, unmodified — the fix is
in the input, not in the extractor, so no existing document can change behaviour.

    scripts/rescue_outline.py --scan out/corpus              # who is rescuable, and why
    scripts/rescue_outline.py <job_dir>                      # parse + verify only
    scripts/rescue_outline.py <job_dir> --rerun              # + re-extract into fallback/
    scripts/rescue_outline.py <job_dir> --rerun --promote    # + make it the job's result

Outputs live in <job_dir>/fallback/ and the original artifacts are never touched
unless --promote is given (which moves them aside as *_original first).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fitz  # noqa: E402
import hybrid_extract as he  # noqa: E402
from check_scorecard import compute_scorecard  # noqa: E402
from lib_validate import validate as _validate  # noqa: E402

# A TOC line: "<title> ...... <page>". Four or more leaders, because a title can
# legitimately contain "..." and a two-dot ellipsis is not a leader run.
ENTRY = re.compile(r"^(?P<title>\S.*?)\s*[.…·]{4,}\s*(?P<page>\d{1,4})\s*$")
# The numbering usually sits on its own line ahead of the title ("A." / "1" / "3.2").
LABEL = re.compile(r"^\(?(?P<lab>[A-Z]|\d{1,2}(?:\.\d{1,2})*)[.)]?\s*$")
# Heading spellings seen in this corpus: "Table of Contents", "TABLE OF CONTENT"
# (singular), and letter-spaced "T A B L E  O F  C O N T E N T S". Matching on the
# line with every non-letter stripped covers all three, and any future spacing or
# punctuation variant, without the regex growing a special case per publisher.
def _is_toc_heading(line: str) -> bool:
    flat = re.sub(r"[^a-z]", "", line.lower())
    return flat.startswith("tableofcontent") or flat in ("contents", "content")


def has_toc_heading(text: str) -> bool:
    return any(_is_toc_heading(l) for l in text.splitlines())


# ---------------- finding and reading the printed TOC ----------------
def _toc_row_count(doc, pno: int) -> int:
    """How many TOC-shaped rows a page yields, by EITHER reading — leader lines or
    right-margin geometry. Counting only leaders made the page walk blind to a
    leaderless TOC, which is how France 168855's heading was found on its cover page
    and its actual TOC on the next page was never looked at."""
    leaders = sum(1 for line in doc[pno].get_text("text").splitlines()
                  if ENTRY.match(line.strip()))
    return max(leaders, len(parse_toc_layout(doc, [pno])))


def find_toc_pages(doc, max_scan: int = 10) -> list[int]:
    """0-based indices of the pages that look like a printed TOC.

    Found by SHAPE, not by heading. Chasing heading spellings was a losing game — the
    corpus has "Table of Contents", "TABLE OF CONTENT", letter-spaced
    "T A B L E  O F  C O N T E N T S", and "CONTENTS0F1" (a footnote marker glued to
    the word), and each new publisher brings another. A page that carries several
    "<title> … <page-number>" rows near the front of a document IS a table of
    contents, whatever it calls itself.

    Dropping the heading requirement is safe because it was never what made this
    trustworthy: verify() is, by checking each entry's title against the page it
    points at. A page misidentified as a TOC produces entries that verify at ~0% and
    is rejected. The heading survives only as a tie-breaker between candidate runs."""
    counts = {i: _toc_row_count(doc, i) for i in range(min(max_scan, doc.page_count))}
    runs, cur = [], []
    for i in sorted(counts):
        if counts[i] >= 3:
            cur.append(i)
        elif cur:
            runs.append(cur); cur = []
    if cur:
        runs.append(cur)
    if not runs:
        return []
    # most rows wins; a heading on or just before the run breaks a tie
    def score(run):
        head = any(has_toc_heading(doc[max(0, run[0] - 1 + k)].get_text("text"))
                   for k in (0, 1))
        return (sum(counts[i] for i in run), head)
    return max(runs, key=score)


def running_header_titles(doc, share: float = 0.5) -> set[str]:
    """Text that appears in the top or bottom margin of most pages: the running header
    and footer. Cached on the document object — every TOC row is tested against it."""
    cached = getattr(doc, "_aci_running", None)
    if cached is not None:
        return cached
    seen: dict[str, int] = {}
    n = max(doc.page_count, 1)
    for i in range(n):
        lines = [l.strip() for l in doc[i].get_text("text").splitlines() if l.strip()]
        for l in lines[:2] + lines[-2:]:
            key = _norm_title(l)
            if len(key) > 4:
                seen[key] = seen.get(key, 0) + 1
    out = {k for k, c in seen.items() if c >= share * n}
    try:
        doc._aci_running = out
    except Exception:  # noqa: BLE001 — caching is a nicety, not a requirement
        pass
    return out


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _is_running_header(title: str, doc) -> bool:
    """A TOC row that IS the page's running header, e.g. "Data Privacy - US States"
    printed above the entries with the page's own number in the right margin.

    Matching on the header TEXT, not on "does this row point at its own page": Crypto
    Assets ADGM prints its TOC on page 3 and starts the body on page 3, so its first
    three entries legitimately target the page they appear on. The earlier, blunter
    rule silently dropped them — including the document's opening section."""
    if not title:
        return False
    key = _norm_title(title)
    return any(key == h or (len(key) > 6 and key in h) or (len(h) > 6 and h in key)
               for h in running_header_titles(doc))


def _label_depth(lab: str) -> int | None:
    """Nesting depth implied by a TOC label. -> 1-based depth, or None if unlabelled.

        A.      -> 1        1       -> 1
        B.      -> 1        1.1     -> 2
                            1.4.2   -> 3

    The label already states the hierarchy; the old rule threw it away, giving level 1
    only to a bare letter and level 2 to everything else. A contents page of
    "1 / 1.1 / 1.2 / 2 / 2.1" therefore arrived as twenty-five level-2 entries, and
    since pdf2mdtree emits sections from level-1 headings only, the rescued document
    came out completely flat — content recovered, structure not."""
    lab = (lab or "").strip()
    if not lab:
        return None
    if len(lab) == 1 and lab.isalpha():
        return 1
    if lab[0].isdigit():
        return lab.count(".") + 1
    return None


def _rebase_depths(rows: list) -> list:
    """A document numbered BOTH ways ("A." parts containing "1.1" sections) needs its
    numeric depths pushed one level down so they nest under their letter part. A
    document numbered only one way is left exactly as it is."""
    has_alpha = any(kind == "alpha" for kind, _d, _t, _p in rows)
    out = []
    for kind, d, title, page in rows:
        out.append(((d + 1) if (has_alpha and kind == "num") else d, title, page))
    return out


def _has_title_text(s: str) -> bool:
    """Does this row carry a name, or only punctuation? A letter or a digit anywhere
    (any script) is a name; a run of leader dots is not."""
    return bool(re.search(r"[^\W_]", s or "", re.UNICODE))


# How many lines of a wrapped title _wrapped_head will walk back over. Three covers
# every wrap in this corpus (the longest is two) with room to spare; a bound is here so
# a TOC page whose every line fails ENTRY cannot glue the whole page into one title.
_WRAP_MAX_LINES = 3


def _wrapped_head(pending: list[str], title_txt: str, doc=None) -> tuple[str, str]:
    """The earlier lines of a title that WRAPPED before its leader run -> (head, label line).

    The mirror image of the leaders-only case handled below, and the one that was
    missing. A contents page sets a long entry over three lines -- the label alone, the
    first part of the title with NO leaders, then the tail WITH the leaders and the page:

        '7.4 '
        'Sensitivities based on Investment Strategy (Investment Management & Advisory '
        'Services) ......................................................... 48 '

    ENTRY matches only the last of those, whose title is the bare tail "Services)". It
    carries a name, so the leaders-only repair does not fire; it carries no label, so it
    was written out as a top-level entry called "Services)" -- a section the document
    does not have. Japan 172122 built one, took pages 45-50 off section 7 with it and
    adopted 7.4/7.5 as its children; Bahamas 183503 produced the identical entry and
    escaped only because it failed to locate in the body and was dropped.

    Returns ("", "") when there is nothing to glue, so the caller keeps its own `prev`.
    """
    if not pending:
        return "", ""
    # A title that already carries its own label is a whole entry, not a tail. Checked
    # before anything is joined: the label is what says "a new entry starts here".
    if LABEL.match((title_txt.split() or [""])[0]):
        return "", ""
    head: list[str] = []
    i = len(pending) - 1
    while i >= 0 and len(head) < _WRAP_MAX_LINES:
        cand = pending[i].strip()
        # A label alone above the tail means the normal two-line entry, NOT a wrap --
        # stop and let the caller read the label from it as it always has.
        if not cand or LABEL.match(cand) or not _has_title_text(cand):
            break
        # Page furniture is not part of anybody's title.
        if _is_toc_heading(cand) or (doc is not None and _is_running_header(cand, doc)):
            break
        head.insert(0, cand)
        i -= 1
    if not head:
        return "", ""
    # The label sits on the line above the wrap, when there is one.
    label_line = pending[i].strip() if i >= 0 else ""
    return " ".join(head), label_line


def _entries_from_lines(lines: list[str], own_page: int | None = None,
                        doc=None) -> list[tuple[int, str, int]]:
    """(level, title, page) for every leader line, taking the level from the label
    on the preceding line: a bare letter is a part, anything else a section."""
    rows, prev, prev2, prev_was_entry = [], "", "", False
    # Every line since the last entry, oldest first. prev/prev2 are kept exactly as they
    # were -- the leaders-only repair below reads them -- and this is the same history,
    # deep enough to walk back over a title that took more than one line.
    pending: list[str] = []
    for raw in lines:
        line = raw.strip()
        m = ENTRY.match(line)
        if m:
            title_txt = m.group("title").strip()
            label_line = prev
            # LEADERS ONLY, NO NAME: a title too long for its line, which WRAPPED
            # before its leader run. Finland 138417 prints section 3 as three lines --
            # "3.", then the title, then a line that is nothing but dots and "7" -- so
            # the row matching here carries no name and the real one is the line above.
            #
            # Taking "." as the title wrote a bookmark literally called "." into the
            # repaired PDF. That is a section the tree can never build, counted as
            # promised and then missing: 25 points on a flat 4-section build, which is
            # why Finland, France 138522, Hungary 139811, Netherlands 146753 and Norway
            # 179259 all sat at exactly 75.0, and why Greece 138436 fell from 86.2 to
            # 75.0 once its completeness improved enough to be adopted.
            #
            # The wrapped line is taken as the name and the label read from the line
            # before it, so the entry becomes the section it always was. A row with no
            # name and nothing above it to borrow is dropped: whatever it is, it is not
            # a section, and an entry that cannot be named cannot be located either.
            if not _has_title_text(title_txt):
                if (prev and not prev_was_entry and _has_title_text(prev)
                        and not (doc is not None and _is_running_header(prev, doc))):
                    title_txt, label_line = prev, prev2
                else:
                    prev2, prev, prev_was_entry = prev, line, True
                    pending = []
                    continue
            else:
                # A NAMED tail whose title wrapped above it. Only reached when the row
                # carries a name, so the leaders-only repair above has declined it.
                _head, _lab_line = _wrapped_head(pending, title_txt, doc)
                if _head:
                    title_txt, label_line = f"{_head} {title_txt}", _lab_line
            lab_m = LABEL.match(label_line)
            lab = lab_m.group("lab") if lab_m else ""
            # The label may sit on the preceding line ("A." / "1.2" alone in the left
            # column) or lead the title itself; try the line first, then the title.
            if not lab:
                inline = LABEL.match((title_txt.split() or [""])[0])
                lab = inline.group("lab") if inline else ""
            depth = _label_depth(lab)
            kind = "alpha" if (len(lab) == 1 and lab.isalpha()) else "num" if lab else "none"
            title = (f"{lab} " if lab and not title_txt.startswith(lab) else "") + title_txt
            page_no = int(m.group("page"))
            if not (own_page is not None and page_no == own_page
                    and doc is not None and _is_running_header(title, doc)):
                # AN UNLABELLED ENTRY SITS WHERE THE CONTENTS PAGE PUT IT, and that is
                # the entry above it -- not the top of the document.
                #
                # `depth or 1` made every unlabelled row a top-level SECTION, and
                # pdf2mdtree emits sections from level-1 headings only, so each one
                # became a file of its own. Belgium 163341 prints two sub-headings of
                # clause 4.4 without numbers --
                #
                #     4.4 Definition of "AIFMD Marketing" and/or "Pre-Marketing" ... 34
                #     Harmonised CBDF Pre-Marketing Regime ....................... 38
                #     Non-CBDF Pre-Marketing Regime .............................. 41
                #     4.5 Provision of Non-Core Services by an AIFM .............. 45
                #
                # -- so both were promoted out of clause 4 and inserted between it and
                # clause 5. That shifted every later file's ordinal, leaving two files
                # prefixed `10-` (clause 10 and clause 7) and two `11-` (clause 8 and
                # clause 11): the tree no longer ran in clause order.
                #
                # Inheriting the previous depth is what the printed page already says,
                # and it is why this is not the blunter "an unlabelled title is never a
                # section": DISCLAIMERS: OPEN-ENDED FUND and its three siblings are also
                # unlabelled, and they ARE top-level divisions -- they follow clause 11,
                # a depth-1 entry, so they inherit 1 and are unchanged. The `or 1` still
                # covers an unlabelled FIRST entry, which has nothing to inherit and
                # whose demotion is what the empty-tree note above guards against.
                rows.append((kind, depth or (rows[-1][1] if rows else 1), title, page_no))
            prev2, prev, prev_was_entry = prev, line, True
            pending = []
            continue
        prev2, prev, prev_was_entry = prev, line, False
        pending.append(line)
    return _rebase_depths(rows)


def parse_toc_text(doc, pages: list[int]) -> list[tuple[int, str, int]]:
    """The cheap path: the TOC's own text layer. Works whenever the TOC is real text
    (it was on every document in this corpus), and costs nothing."""
    out = []
    for p in pages:
        out += _entries_from_lines(doc[p].get_text("text").splitlines(), own_page=p + 1, doc=doc)
    return out


def parse_toc_layout(doc, pages: list[int]) -> list[tuple[int, str, int]]:
    """Same TOC, read by GEOMETRY instead of by leader dots.

    Plenty of TOCs align their page numbers in a right-hand column with no leaders at
    all (France 168855 leaders only its two appendix lines — which is exactly the two
    entries the line parser found). Grouping words into visual rows and taking a
    trailing integer that sits in the right margin reads those, and reads leadered
    TOCs too, since the leader run is just tokens to discard."""
    cand: list[tuple[float, str, int]] = []
    for pno in pages:
        page = doc[pno]
        width = page.rect.width
        rows: list[tuple[float, list[tuple[float, str]]]] = []
        # x1 is kept now, not discarded: a title that WRAPPED filled its column, and how
        # far right the row reaches is the only honest way to tell a wrapped first line
        # from a short standalone heading sitting above an entry. See the join below.
        right = {}
        for x0, y0, x1, _y1, w, *_ in page.get_text("words"):
            for y, items in rows:
                if abs(y - y0) <= 2.5:          # same visual line
                    items.append((x0, w))
                    right[y] = max(right[y], x1)
                    break
            else:
                rows.append((y0, [(x0, w)]))
                right[y0] = x1
        # TOP TO BOTTOM. The rows are built in word order, which is usually reading order
        # and is not promised to be; the join below asks "what is on the line ABOVE", so
        # it needs the real vertical order rather than whatever order the words arrived in.
        rows.sort(key=lambda r: r[0])
        # The contiguous run of non-entry rows immediately above the current row — the
        # earlier lines of a title whose leader run and page number are further down.
        # Cleared by every entry and by every row that cannot be part of a title, so it
        # only ever holds lines that touch the entry being built.
        head: list[tuple[float, str]] = []
        for _y, items in rows:
            items.sort()
            if len(items) < 2:
                # A LABEL ALONE on its own row ("7.4"), which is one of the two shapes a
                # printed TOC uses. Not an entry and not a title line, but it must not
                # clear the run either: the title continues on the next row.
                if not (items and LABEL.match(items[0][1].strip())):
                    head = []
                continue
            last_x, last_w = items[-1]
            page_no = last_w.strip(".")
            # A page number, in the right margin. The margin test is what separates a
            # TOC row from a sentence that happens to end in a number.
            if not page_no.isdigit() or last_x < 0.62 * width:
                # No page number here, so not an entry on its own — but possibly the
                # first line of one whose title wrapped. Held only if it looks like part
                # of a title AND reaches into the right half of the page, i.e. it ran out
                # of room, which is the whole reason a title wraps.
                whole = " ".join(w for _x, w in items).strip()
                if (_has_title_text(whole) and right.get(_y, 0) >= 0.55 * width
                        and not _is_toc_heading(whole)
                        and not _is_running_header(whole, doc)):
                    head.append((items[0][0], whole))
                    del head[:-_WRAP_MAX_LINES]
                else:
                    head = []
                continue
            title_txt = " ".join(w for _x, w in items[:-1])
            if int(page_no) == pno + 1 and _is_running_header(title_txt, doc):
                head = []
                continue
            toks = [w for _x, w in items[:-1] if set(w) - {".", "…", "·"}]
            if not toks:
                head = []
                continue
            title, x_indent = " ".join(toks).strip(), items[0][0]
            # Same rule as the text parser: a row that carries its own label is a whole
            # entry, never a tail. Without the join, the tail alone became the entry —
            # "Services)" as a section of its own, with the real 7.4 nowhere.
            if head and not LABEL.match((title.split() or [""])[0]):
                title = " ".join([t for _x, t in head] + [title])
                # The LEVEL comes from the indent, and the label sits on the first line
                # of the wrap, so that is the line whose indent means anything.
                x_indent = head[0][0]
            head = []
            cand.append((x_indent, title, int(page_no)))

    # Level from INDENT, not from the numbering label. A printed TOC shows its
    # hierarchy by how far each entry is inset — ADGM's parts sit at x=48 and their
    # subsections at x=71 — while the labels lie: "Background" carries no number and
    # "1. Digital assets framework" carries one, yet the second is the child of the
    # first. Reading the label made every entry level 2, and pdf2mdtree emits sections
    # from level-1 headings only, so the whole document came out empty.
    xs = sorted({round(x / 4) * 4 for x, _t, _p in cand})          # 4pt tolerance
    rank = {x: i for i, x in enumerate(xs)}
    out = [(min(rank[round(x / 4) * 4] + 1, 4), t, p) for x, t, p in cand]
    return out


def parse_toc_mineru(pdf: Path, pages: list[int], backend=None, effort=None):
    """The expensive path, for a TOC that is an image or a layout the text extractor
    scrambles: crop those pages to their own PDF and run MinerU over just them.

    One or two pages, so ~10-20s — the same per-page cost Stage 2 pays. MinerU may
    return the TOC as a markdown table instead of leader lines, so both shapes are
    parsed."""
    src = fitz.open(str(pdf))
    with tempfile.TemporaryDirectory(prefix="toc_") as tmp:
        crop = Path(tmp) / "toc.pdf"
        out = fitz.open()
        for p in pages:
            out.insert_pdf(src, from_page=p, to_page=p)
        out.save(str(crop))
        run_dir = he.run_mineru_to_dir(str(crop), out_dir=Path(tmp) / "mineru",
                                       backend=backend, effort=effort)
        md = "\n".join(f.read_text(encoding="utf-8", errors="ignore")
                       for f in sorted(Path(run_dir).glob("*.md")))
    lines = []
    for line in md.splitlines():
        # "| A. SUBSTANTIAL SHAREHOLDING | 3 |" -> "A. SUBSTANTIAL SHAREHOLDING ..... 3"
        if line.count("|") >= 2:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[-1].isdigit() and cells[0]:
                lines.append(f"{cells[0]} {'.' * 5} {cells[-1]}")
                continue
        lines.append(line)
    return _entries_from_lines(lines)


# ---------------- deciding whether to trust it ----------------
def _norm_page(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _title_needles(title: str) -> list[str]:
    """Progressively less distinctive forms of a heading to search for, best first.

    The clause LABEL is kept as the first candidate rather than thrown away, because it
    is what makes a short heading findable: "8.2 Telephone" reduces to the single word
    "telephone", which is body text on dozens of pages, while "8.2 Telephone" as a run
    appears only where the heading is. Requiring three words and dropping the label
    lost 16 of Portugal's 47 headings outright — Introduction, Definitions, Sub-funds,
    Telephone, Arranging, LICENCE and the rest of the short ones produced no candidate
    at all and could never be located.

    Shorter forms follow because a printed contents line is routinely truncated or
    wrapped, so its tail may be missing from the page's own rendering of the heading."""
    t = (title or "").strip()
    m = re.match(r"^([A-Za-z0-9.()]{1,7})\s+(.*)$", t)
    label, rest = (m.group(1), m.group(2)) if m else ("", t)
    lab, core = _norm_page(label), _norm_page(rest)
    words = core.split()
    out: list[str] = []

    def add(cand: str) -> None:
        cand = cand.strip()
        # Two words, or one long one. A single short word ("part", "other") is body
        # text everywhere and would place the heading almost at random.
        if cand and cand not in out and (len(cand.split()) >= 2 or len(cand) >= 8):
            out.append(cand)

    if lab and words:
        add(f"{lab} {' '.join(words[:6])}")
        add(f"{lab} {' '.join(words[:3])}")
    add(" ".join(words))
    for n in (6, 4, 3):
        if n < len(words):
            add(" ".join(words[:n]))
    return out


def locate_titles(doc, entries, page_norms=None, *, skip_pages) -> tuple[list, int]:
    """Find each heading in the document BY SEARCHING, in document order.

    Returns (entries with their real page, how many were found).

    This replaces trusting the page number a printed contents page prints beside each
    title. Those numbers are frequently stale: Portugal 176637's contents page was
    generated and then had content inserted after it, so its numbers run 0 pages out at
    clause 1, +2 by clause 4 and +7 by clause 11. No single offset can describe a gap
    that grows, which is why a perfectly good 47-entry outline verified only 27/47 and
    was thrown away.

    Searching forward from the previous heading keeps the guard that the offset search
    was really providing. A contents page lifted from a DIFFERENT document still fails,
    because its titles will not be found in this one, in this order — and that, not
    page arithmetic, is what "does this TOC describe this document" means."""
    if page_norms is None:
        page_norms = [_norm_page(doc[i].get_text("text")) for i in range(doc.page_count)]
    # The contents page itself lists every title, so searching from page 1 finds all of
    # them there and nowhere else — 47 of 47 "located" on page 2, a 2.5% span. The pages
    # the TOC was READ from are excluded, and the search starts after them.
    skip = set(skip_pages)
    located, found = [], 0
    cursor = (max(skip) + 1) if skip else 0
    for lvl, title, claimed in entries:
        hit = None
        for needle in _title_needles(title):
            for i in range(cursor, len(page_norms)):
                if i in skip:
                    continue
                if needle in page_norms[i]:
                    hit = i + 1
                    break
            if hit:
                break
        if hit:
            found += 1
            cursor = hit - 1          # the next heading may share this page
            located.append((lvl, title, hit))
        else:
            # Unfound: keep it in the outline at the page the sequence has reached, so
            # one unreadable line cannot drop a section, but do not count it as verified.
            located.append((lvl, title, max(1, cursor + 1)))
    return located, found


def verify(doc, entries: list[tuple[int, str, int]], max_offset: int = 8,
           *, skip_pages) -> dict:
    """Only a TOC that demonstrably describes THIS document may be written into it.

    The decisive check is per-entry: the section's own words must be FOUND IN THE
    DOCUMENT, in the order the contents page lists them. A TOC lifted from a different
    edition fails here rather than silently producing a tree whose sections point at
    the wrong text.

    It used to be checked differently — the title had to appear on the page the TOC
    named, allowing one constant offset for cover sheets. That rejected any document
    whose printed page numbers had gone stale, however good its titles were, and the
    page numbers are the one part of a contents page nothing downstream actually needs:
    the located page is better than the printed one. max_offset is accepted and ignored,
    kept so existing callers do not break.

    skip_pages is REQUIRED, and keyword-only, because forgetting it is not a weaker
    check — it is the wrong answer, silently. The contents page lists every title, so a
    search allowed to look at it locates all of them there and then rejects the TOC for
    spanning 2% of the document. It defaulted to () when the search replaced the offset
    match, and check_toc_quality's reader — written against the old signature, where the
    printed page numbers pointed into the body and there was nothing to exclude — kept
    compiling and quietly stopped agreeing with this function for every document in the
    corpus. Pass () deliberately if a caller genuinely has no pages to skip."""
    r = {"entries": len(entries), "verified": 0, "monotonic": False, "offset": 0,
         "span_pct": 0.0, "ok": False, "reasons": [], "located": []}
    if len(entries) < 4:
        r["reasons"].append(f"only {len(entries)} entries parsed")
        return r
    located, found = locate_titles(doc, entries, skip_pages=skip_pages)
    r["located"], r["verified"] = located, found
    pages = [p for _, _, p in located]
    # Monotonic by construction now; kept in the report because callers read it.
    r["monotonic"] = all(b >= a for a, b in zip(pages, pages[1:]))
    r["span_pct"] = round(100 * (max(pages) - min(pages) + 1) / doc.page_count, 1)
    ratio = found / len(entries)
    if ratio < 0.8:
        r["reasons"].append(f"only {found}/{len(entries)} titles found in the document")
    if r["span_pct"] < 40:
        r["reasons"].append(f"covers only {r['span_pct']}% of the document")
    r["ok"] = ratio >= 0.8 and r["span_pct"] >= 40
    return r


# A bookmark outline is supposed to describe the document's STRUCTURE. Some are instead a
# dump of body text: Marketing Restrictions - Asset Management Australia 181814 has 335
# entries of which 271 (81%) are whole sentences and sub-bullets —
#   "(a) for a Fund that is a Body Corporate: (i) Sufficient Equivalent Relief (to ..."
#   "An exemption is available where the Financial Service is the provision of Gene..."
# Stage 1 believed them, so its Executive Summary became ~40 pseudo-sections named after
# sentence fragments ("09-however-it-has-not-typically-covered-the-actual.md") instead of one
# section holding the summary's prose. Nothing failed: the WORDS were all present, so
# completeness/fidelity stayed green and only a human reading the tree could see it.
_PROSE_MIN_WORDS = 10          # a heading this long is a sentence
_PROSE_SHARE = 0.5             # more than half the outline is prose -> not a structure
_PROSE_MIN_KEPT = 8            # ... and pruning must leave a usable skeleton behind


def _heading_like(title: str) -> bool:
    """Does this outline entry read as a heading rather than a line of body text?"""
    t = (title or "").strip()
    if not t or len(t.split()) >= _PROSE_MIN_WORDS:
        return False
    # Sentence punctuation, unless it is just a numbered heading's trailing dot ("1.4.").
    if t.endswith((",", ";", ":")) or (t.endswith(".") and not re.match(r"^[\d.]+$", t)):
        return False
    if re.match(r"^[a-z]", t):                       # a lowercase start continues a sentence
        return False
    if re.match(r"^\(?[a-z0-9ivx]{1,4}\)\s+[a-z]", t):   # "(a) the advice has been prepared"
        return False
    return True


def prose_share(toc) -> float:
    """Fraction of outline entries that are body text, not headings."""
    return 0.0 if not toc else 1 - sum(_heading_like(t) for _l, t, _p in toc) / len(toc)


def prune_prose_entries(toc) -> list:
    """The outline with body-text entries removed, or [] if too little would remain."""
    kept = [[l, t, p] for l, t, p in toc if _heading_like(t)]
    return kept if len(kept) >= _PROSE_MIN_KEPT else []


def current_outline_is_suspect(doc) -> tuple[bool, str]:
    """Cheap triage: is the embedded outline plausibly a description of the document?"""
    toc = doc.get_toc()
    if not toc:
        return True, "no bookmark outline at all"
    first = min(pg for _, _, pg in toc)
    # "Starts deep into the document" needs a floor, not just a fraction. On a 3-page
    # PDF 0.2 x pages is 0.6, so an outline whose first entry is on PAGE 1 counted as
    # suspect; on an 8-page PDF with 59 bookmarks, starting on page 2 did too. Both are
    # healthy outlines. A cover page and a contents page in front of the first bookmark
    # is normal, so anything within the first two pages is never suspicious.
    if first > max(2, 0.2 * doc.page_count):
        return True, f"{len(toc)} bookmark(s), first on p{first}/{doc.page_count}"
    share = prose_share(toc)
    if share > _PROSE_SHARE and prune_prose_entries(toc):
        return True, (f"{len(toc)} bookmark(s) but {share:.0%} are body text, not headings "
                      f"— {len(prune_prose_entries(toc))} real heading(s) under them")
    return False, f"{len(toc)} bookmark(s) from p{first}"


# ---------------- the fallback stage ----------------
def _legal_levels(entries):
    """PyMuPDF requires an outline to start at level 1 and never jump more than one
    level at a time. Levels are SHIFTED so the shallowest heading present becomes 1,
    not merely forced on the first entry.

    pdf2mdtree emits sections from level-1 headings only (`top = [... if level == 1]`),
    so an outline whose real headings all sit at level 2 produces an EMPTY tree. Crypto
    Assets ADGM hit exactly that: promoting only the first entry made a stray page
    footer the sole level-1 heading, it matched no text, and all 14 correctly-matched
    headings below it were never written — 6,321 words in, 0 out."""
    if not entries:
        return []
    base = min(lvl for lvl, _t, _p in entries)
    out, prev = [], 0
    for lvl, title, page in entries:
        lvl = max(1, lvl - base + 1)
        lvl = 1 if not out else min(lvl, prev + 1)
        out.append([lvl, title, page])
        prev = lvl
    return out


def rescue(job_dir: Path, engine: str = "auto", rerun: bool = False,
           promote: bool = False, backend=None, effort=None, dry: bool = False,
           force_promote: bool = False, no_prune: bool = False) -> dict:
    job_dir = Path(job_dir)
    pdf = job_dir / "source.pdf"
    doc = fitz.open(str(pdf))
    suspect, why = current_outline_is_suspect(doc)
    res = {"doc": job_dir.name, "outline": why, "suspect": suspect}

    # A prose outline is repaired by DELETING the prose, not by rebuilding from a printed
    # TOC: the real headings are already there and correct ("1. Background", "1.4 Executive
    # Summary"), and a printed contents page usually lists only those same headings anyway.
    # Pruning keeps whatever page offsets and nesting the embedded outline got right.
    # --no-prune forces the printed-TOC rescue on a prose outline, for comparing the two
    # repairs on the same document rather than taking the cheaper one on faith.
    pruned = ([] if no_prune else
              prune_prose_entries(doc.get_toc()) if suspect and "body text" in why else [])
    if pruned:
        res["engine"] = "prune"
        res["status"] = f"pruned outline: {len(doc.get_toc())} -> {len(pruned)} heading(s)"
        if dry:
            return res
        out_dir = job_dir / "fallback"
        out_dir.mkdir(exist_ok=True)
        repaired = out_dir / "source_repaired.pdf"
        doc.set_toc(_legal_levels(pruned))
        doc.save(str(repaired))
        res["repaired_pdf"] = str(repaired)
        if not rerun:
            return res
        return _rerun_and_maybe_promote(job_dir, out_dir, repaired, res, promote,
                                        backend, effort, force_promote)

    toc_pages = find_toc_pages(doc)
    res["toc_pages"] = [p + 1 for p in toc_pages]
    if not toc_pages:
        res["status"] = "no printed TOC found"
        return res

    entries = [] if engine == "mineru" else parse_toc_text(doc, toc_pages)
    res["engine"] = "text"
    check = (verify(doc, entries, skip_pages=toc_pages) if entries
             else {"ok": False, "reasons": ["nothing parsed"]})
    if not check["ok"] and engine != "mineru":
        lay = parse_toc_layout(doc, toc_pages)
        lay_check = (verify(doc, lay, skip_pages=toc_pages) if lay
                     else {"ok": False, "reasons": ["nothing parsed"]})
        # take it if it is usable, or simply better than what the leaders gave
        if lay_check.get("ok") or lay_check.get("verified", 0) > check.get("verified", 0):
            entries, check, res["engine"] = lay, lay_check, "layout"
    if not check["ok"] and engine in ("auto", "mineru"):
        # the text layer did not give a usable TOC — read the page visually instead
        try:
            entries = parse_toc_mineru(pdf, toc_pages, backend, effort)
            res["engine"] = "mineru"
            check = verify(doc, entries, skip_pages=toc_pages)
        except Exception as e:  # noqa: BLE001
            res["mineru_error"] = f"{type(e).__name__}: {e}"
    res["check"] = check
    if not check["ok"]:
        res["status"] = "TOC rejected: " + "; ".join(check.get("reasons", []))
        return res

    # The pages come from LOCATING each title, not from the contents page's own numbers.
    entries = check.get("located") or entries
    res["status"] = (f"TOC usable: {len(entries)} entries, {check['verified']} "
                     f"located by title search")
    if dry:                       # --scan must not write into a job dir
        return res
    out_dir = job_dir / "fallback"
    out_dir.mkdir(exist_ok=True)
    repaired = out_dir / "source_repaired.pdf"
    doc.set_toc(_legal_levels(entries))
    doc.save(str(repaired))
    res["repaired_pdf"] = str(repaired)
    if not rerun:
        # --promote on its own adopts a rescue that was already re-extracted, rather
        # than silently doing nothing (or paying for Stage 2 a second time).
        if promote and (out_dir / "scorecard.json").exists():
            sc = json.loads((out_dir / "scorecard.json").read_text())
            before = json.loads((job_dir / "scorecard.json").read_text())
            res["before"] = {"gate": before.get("gate"), "worst": before.get("worst_score")}
            res["after"] = {"gate": sc.get("gate"), "worst": sc.get("worst_score")}
            if _is_better(res) or force_promote:
                promote(job_dir, out_dir, res)
            else:
                res["status"] += (f" — NOT promoted: {res['after']['worst']} is not better than "
                                  f"{res['before']['worst']} (use --promote-anyway)")
        elif promote:
            res["status"] += " — nothing to promote (no completed rescue); add --rerun"
        return res

    return _rerun_and_maybe_promote(job_dir, out_dir, repaired, res, promote,
                                    backend, effort, force_promote)


def _rerun_and_maybe_promote(job_dir, out_dir, repaired, res, promote,
                             backend, effort, force_promote):
    """Re-extract with the UNCHANGED pipeline; only the input PDF differs.

    Timed PER STAGE, because the caller records this whole function as a single `toc_rescue`
    step sitting beside `stage2_mineru` in the document's timings — which reads as though the
    rescue were a cheap re-shuffle of the hierarchy alongside the real work. It is not: this
    runs Stage 2 AGAIN, and on a table-heavy document that second MinerU pass is essentially
    the entire cost. Bermuda__166524 measured 796.7s here against a 797.3s first pass, i.e.
    the rescue duplicated the whole extraction, and nothing on the screen said so."""
    t0 = time.time()
    steps: dict[str, float] = {}

    def _timed(name, fn, *a, **kw):
        t = time.time()
        try:
            return fn(*a, **kw)
        finally:
            steps[name] = round(time.time() - t, 1)

    manifest = _timed("stage1", he.run_stage1, repaired, out_dir / "01_stage1_extract")
    s2 = _timed("stage2_mineru", he.run_stage2, repaired, manifest,
                out_dir / "02_stage2_mineru_tables", backend, effort,
                out_dir / "01_stage1_extract")
    _timed("stage3", he.run_stage3, out_dir / "01_stage1_extract",
           out_dir / "02_stage2_mineru_tables", out_dir / "03_stage3_final", s2)
    val = _timed("validation", _validate, out_dir)
    sc = _timed("scorecard", compute_scorecard, out_dir, val)
    res["steps"] = steps
    (out_dir / "validation.json").write_text(json.dumps(val, indent=2))
    (out_dir / "scorecard.json").write_text(json.dumps(sc, indent=2))
    before = json.loads((job_dir / "scorecard.json").read_text()) if (job_dir / "scorecard.json").exists() else {}
    res["before"] = {"gate": before.get("gate"), "worst": before.get("worst_score")}
    res["after"] = {"gate": sc.get("gate"), "worst": sc.get("worst_score")}
    res["seconds"] = round(time.time() - t0, 1)

    if promote:
        if _is_better(res) or force_promote:
            promote(job_dir, out_dir, res)
        else:
            res["status"] += (f" — NOT promoted: {res['after']['worst']} is not better than "
                              f"{res['before']['worst']} (use --promote-anyway)")
    return res


def _is_better(res: dict) -> bool:
    """A rescue is only worth adopting if it scores BETTER. A printed TOC can be
    sparser than the structure the font-size heuristic inferred — DRV Repo Germany
    155633's TOC yields 8 entries and re-extracting on it dropped the document from
    45.5 to 31.7. Verified-looking provenance is not the same as a better result."""
    b, a = (res.get("before") or {}).get("worst"), (res.get("after") or {}).get("worst")
    if not isinstance(a, (int, float)):
        return False
    return not isinstance(b, (int, float)) or a > b


def promote(job_dir: Path, out_dir: Path, res: dict) -> None:
    """Make the rescued tree the document's result. The original stays recoverable as
    *_original, and the job dir keeps its normal shape so the dashboard and the
    publisher need no special case for a rescued document — the provenance travels in
    rescued_by_toc.json, which the publisher turns into the gallery's "rescued" flag."""
    for name in ("01_stage1_extract", "02_stage2_mineru_tables", "03_stage3_final",
                 "validation.json", "scorecard.json", "source.pdf"):
        src, keep = job_dir / name, job_dir / f"{name}_original"
        if src.exists():
            if not keep.exists():
                src.rename(keep)        # first promotion: preserve the pristine original
            else:
                # A LATER promotion (the document was re-extracted and rescued again).
                # `_original` already holds the pristine copy, so this `src` is a
                # previous rescue's output — and it MUST be cleared, not kept: the move
                # below is shutil.move, which drops the source INSIDE an existing
                # directory instead of replacing it. That nested the rescued tree under
                # the old flat one, doubling the files and leaving the dashboard showing
                # the stale structure while the real result sat a level down.
                shutil.rmtree(src) if src.is_dir() else src.unlink()
        moved = out_dir / ("source_repaired.pdf" if name == "source.pdf" else name)
        if not moved.exists() and name == "source.pdf":
            moved = out_dir / "source.pdf"
        if moved.exists():
            shutil.move(str(moved), str(job_dir / name))
    (job_dir / "rescued_by_toc.json").write_text(json.dumps(res, indent=2, default=str))
    res["promoted"] = True


# ---------------- driver ----------------
def failed_jobs(root: Path, max_score: float = 50.0) -> list[Path]:
    """Documents worth a second pass: the scorecard exists and completeness (the
    dimension this failure mode destroys) is at or below the bar."""
    out = []
    for sc_path in sorted(root.glob("*/*/scorecard.json")):
        try:
            sc = json.loads(sc_path.read_text())
        except (OSError, ValueError):
            continue
        comp = (sc.get("dimensions", {}).get("completeness") or {}).get("score")
        if comp is not None and comp <= max_score:
            out.append(sc_path.parent)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_dir", nargs="?", help="one job dir (out/corpus/<product>/<jur>__<id>)")
    ap.add_argument("--scan", metavar="CORPUS_ROOT",
                    help="report which failed documents a TOC rescue could fix (reads only)")
    ap.add_argument("--all", metavar="CORPUS_ROOT", help="rescue every failed document under this root")
    ap.add_argument("--max-score", type=float, default=50.0, help="completeness at/below this counts as failed")
    ap.add_argument("--no-prune", action="store_true",
                    help="skip the prose-outline prune and rebuild from the PRINTED contents "
                         "page instead (the two repairs can then be compared)")
    ap.add_argument("--engine", choices=("auto", "text", "mineru"), default="auto",
                    help="how to read the TOC page: text (leaders then geometry), MinerU, or text then MinerU (default)")
    ap.add_argument("--rerun", action="store_true", help="re-extract with the repaired outline")
    ap.add_argument("--promote", action="store_true", help="make the rescued result the job's result")
    ap.add_argument("--force", action="store_true", help="redo documents that already have a completed rescue")
    ap.add_argument("--promote-anyway", action="store_true",
                    help="promote even when the rescue scores no better (default: refuse)")
    ap.add_argument("--mineru-backend", default=None)
    ap.add_argument("--mineru-effort", default=None)
    args = ap.parse_args()

    if args.scan:
        jobs = failed_jobs(Path(args.scan), args.max_score)
        print(f"{len(jobs)} document(s) with completeness <= {args.max_score}\n")
        fixable = 0
        for j in jobs:
            r = rescue(j, engine="text", dry=True)   # text only, read-only: a scan stays cheap
            ok = (r.get("check") or {}).get("ok")
            fixable += bool(ok)
            mark = "RESCUABLE" if ok else "         "
            print(f"  {mark}  {j.parent.name[:22]:22s}/{j.name[:26]:26s} "
                  f"{r['outline']:34s} TOC p{r.get('toc_pages') or '-'}  {r['status'][:44]}")
        print(f"\n{fixable} of {len(jobs)} rescuable from their printed TOC")
        return

    targets = failed_jobs(Path(args.all), args.max_score) if args.all else [Path(args.job_dir)]
    if args.all and args.rerun and not args.force:
        before = len(targets)
        targets = [j for j in targets if not (j / "fallback" / "scorecard.json").exists()]
        print(f"{before - len(targets)} document(s) already rescued — skipping "
              f"(use --force to redo); {len(targets)} to go\n")
    for j in targets:
        r = rescue(j, args.engine, args.rerun, args.promote,
                   args.mineru_backend, args.mineru_effort,
                   force_promote=args.promote_anyway, no_prune=args.no_prune)
        print(json.dumps(r, indent=2, default=str))


if __name__ == "__main__":
    main()
