#!/usr/bin/env python3
"""check_table_cells.py — does each rendered cell hold what the PDF puts in it?

Every other table check asks whether the CONTENT survived. This one asks whether the
GRID did. A merged column loses nothing — every word is still on the page, in the right
order, inside a table — so word coverage, per-section gaps and table presence all stay
green while the question/answer pairing that makes a questionnaire readable is gone.

MEASURED AGAINST THE PDF, never against stage 2. MinerU's extraction is what a merge
would otherwise have to be diagnosed from, and it is frequently the thing doing the
merging, so it cannot be its own answer key. The source page's word geometry can.

HOW IT WORKS, and why it is built this way rather than the obvious way.

The obvious way is to take a rendered cell's text, find those words on the page, and
ask whether they cross a column boundary. That was the first implementation and it was
wrong in a way worth recording, because it looked convincing for a whole review cycle.
Finding a cell's words meant anchoring on its first and last few words and taking every
page word between them — but fitz returns words in READING order, which in a two-column
table interleaves the columns line by line, and the anchor matches the first row whose
text happens to start the same way, which on a questionnaire is routinely the wrong row.
Measured on Jersey__181919 p76: a 34-word answer cell resolved to a 121-word segment
spanning x=55–769, the full page width, beginning a row too early. Worse, the failure
was SELECTED FOR — a segment can only cross a corridor when the lookup went wrong, so
every single finding was the check reporting its own failure. 77 of that document's 81
cells resolved correctly and were silently fine; the 3 that resolved to page-wide slabs
were the 3 reported.

So this never looks a cell up by text. It reconstructs the PDF's OWN cells from
geometry, then asks a containment question that needs no positions at all:

  1. COLUMN BOUNDARIES are vertical corridors no row crosses, over the whole table
     region on that page. A list indent — "(ii)" at x<111 with its paragraph resuming at
     x>135 — is clear on its own row and occupied on every other one, so a region-wide
     test excludes it. Measured: per-row testing flagged 83 of 790 cells on one
     document, essentially all numbered sub-items; region-wide brings that to 10.

  2. ROW BOUNDARIES are the table's own PRINTED RULES. A horizontal corridor alone
     cannot tell a row boundary from a paragraph break and no width threshold can fix
     it: the verified row merge on Bahamas p64 sits in an 8.0pt corridor where the
     median line gap is 13.1pt (ratio 0.61), and Austria's paragraph breaks sit in
     7.8–7.9pt corridors at ratios 0.59 and 0.64 — the same number. A drawn rule
     separates them cleanly, and it is the cue a reader uses.

  3. Every word in the region is assigned to the (row, column) slot its centre falls in,
     giving the PDF's own cells.

  4. A rendered cell is then MERGED if two or more distinct PDF cells appear inside it
     as contiguous token runs. Adjacent in the same row -> column merge; adjacent in the
     same column -> row merge. This is the claim itself — "one output cell holds two of
     the source's cells" — so it cannot be satisfied by a lookup going wrong.

A cell that DECLARES it spans (colspan/rowspan) is exempt: a header row written as
`<td colspan="2">Questions</td>` is supposed to cross the boundary, and stage 4 is
allowed to promote a label row that way.

REPEATED TEXT IS THE HARD PART, and three separate shapes of it produced false merges
before the containment rules below were right. A source cell only counts as a second
cell inside a rendered one if it sits in a DIFFERENT PART of it (Jersey p65, Australia
p120: a question cell ends with the same sentence that also stands alone as the
continuation cell above it) and its text is NOT CONTAINED in another matched cell's
(Australia p152: five jurisdiction rows repeating "The relief applies to providing
financial advice…" and the same closing item verbatim) and is not a duplicate of it
(Jersey p50 prints the whole table twice, side by side).

STATUS. Verified in both directions before being trusted: 6 of 6 merges injected into
real tables are detected and classified, and the four MRAM documents with stage-5 trees
report none, with pages 50, 65, 76, 86, 120 and 152 checked against the rendered PDF by
eye. It can only judge the 62% of table-pages whose grid reconstructs (>=3 rows and >=2
columns); the rest report nothing rather than guessing.

NOT YET IN lib_validate.CHECKS. The detector is sound now, but it has been wrong once
in a way that reached the scorecard, so it earns the gate by running clean over the
corpus first rather than by argument.

Usage:
    python scripts/check_table_cells.py out/corpus/<product>/<job>
    python scripts/check_table_cells.py <job> --stage 5
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (BOLD, DIM, GREEN, RED, banner, iter_content_files,  # noqa: E402
                                 resolve_pdf, resolve_stage_dir, run_cli)

_CELL_RE = re.compile(r"<t[dh]([^>]*)>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPAN_RE = re.compile(r"\b(?:col|row)span\s*=\s*[\"']?\s*([2-9]\d*)", re.I)

# A corridor narrower than this is letter-spacing or a wide word gap, not a column.
MIN_COL_GAP = 8.0
# How far a drawn rule may sit from a row's glyphs and still bound it. Rules are drawn
# on the gap, not measured from the text either side of it.
RULE_TOL = 4.0
# A row rule must span essentially the whole table, because a row boundary is applied
# across every column. A HYPERLINK UNDERLINE is also a long horizontal line and is local
# to the few words it underlines — measured on Jersey p86, whose right-hand cell holds
# three underlined JFSC links: each underline became a row boundary, and because
# boundaries apply region-wide it split the LEFT cell's "(i) … (ii) …" numbered list
# into separate rows too. Both cells are single cells in the source and both were
# reported as merges. Requiring the rule to cover most of the region's width is what
# separates a table rule from an underline.
MIN_RULE_SPAN = 0.7
# A PDF cell below this many tokens is not evidence of anything: "Yes", "N/A" and bare
# numbers recur all over these documents and would match inside any cell by accident.
MIN_PDF_CELL_TOKENS = 4
# A rendered cell below this is likewise too small to be carrying two source cells.
MIN_TREE_CELL_TOKENS = 8
# How much of a source cell must be accounted for inside a rendered cell before it
# counts as present there. High, because the claim being built on it is that BOTH of
# two source cells are in one output cell — a loose bar would let a partial overlap
# with a neighbour manufacture a merge.
CELL_MATCH_THRESHOLD = 0.9


def _norm_words(s: str) -> list[str]:
    s = _html.unescape(_TAG_RE.sub(" ", s or ""))
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", s.lower()).split() if w]


def _page_words(doc, pno: int) -> list[tuple]:
    """(x0, y0, x1, y1, normalised_text) for every word on the page."""
    out = []
    for w in doc[pno - 1].get_text("words"):
        t = re.sub(r"[^a-z0-9]", "", w[4].lower())
        if t:
            out.append((w[0], w[1], w[2], w[3], t))
    return out


def _page_rules(doc, pno: int) -> list[tuple[float, float, float]]:
    """Every drawn horizontal line as (y, x0, x1). Filtered to table ROW rules by
    _row_bounds, which knows the region they have to span."""
    out = []
    for d in doc[pno - 1].get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 1.5:
                    out.append((round(p1.y, 1), min(p1.x, p2.x), max(p1.x, p2.x)))
            elif it[0] == "re":
                r = it[1]
                if r.height < 2.5:
                    out.append((round(r.y0, 1), r.x0, r.x1))
    return sorted(set(out))


def _column_bounds(words: list[tuple], bbox) -> list[float]:
    """x boundaries of the region's columns: region edges plus every corridor that no
    row crosses. Returns n+1 edges for n columns."""
    x0, y0, x1, y1 = bbox
    band = [w for w in words
            if w[1] < y1 and w[3] > y0 and w[0] >= x0 - 1 and w[2] <= x1 + 1]
    if len(band) < 2:
        return []
    spans = sorted((w[0], w[2]) for w in band)
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    cuts = [(b1 + a2) / 2 for (_a, b1), (a2, _b) in zip(merged, merged[1:])
            if a2 - b1 >= MIN_COL_GAP]
    return [x0 - 1] + cuts + [x1 + 1]


def _row_bounds(rules: list[tuple[float, float, float]], bbox) -> list[float]:
    """y boundaries of the region's rows: region edges plus every printed rule that
    spans the table. Rules only — a corridor cannot tell a row from a paragraph — and
    only FULL-WIDTH rules, or a hyperlink underline splits every column (see
    MIN_RULE_SPAN)."""
    x0, y0, x1, y1 = bbox
    need = (x1 - x0) * MIN_RULE_SPAN
    # Segments at the same y are UNIONED before the span is measured, because these
    # tables border each cell separately: a single row line arrives as two or three
    # pieces, one per column, none of which covers 70% of the table on its own.
    # Without this no inner rule was ever found — every reconstructed grid came out one
    # row tall, the source cells were page-sized blobs, and only 31 of Jersey's 905
    # rendered cells could be compared to anything.
    by_y: dict[float, list[tuple[float, float]]] = {}
    for y, rx0, rx1 in rules:
        if not (y0 + RULE_TOL < y < y1 - RULE_TOL):
            continue
        a, b = max(rx0, x0), min(rx1, x1)
        if b > a:
            by_y.setdefault(y, []).append((a, b))

    inner = []
    for y, segs in by_y.items():
        segs.sort()
        covered, reach = 0.0, None
        for a, b in segs:
            if reach is None or a > reach:
                covered += b - a
                reach = b
            elif b > reach:
                covered += b - reach
                reach = b
        if covered >= need:
            inner.append(y)
    return [y0 - 1] + sorted(inner) + [y1 + 1]


def _pdf_cells(words: list[tuple], xs: list[float], ys: list[float]) -> dict:
    """-> {(row, col): [tokens]}. Every word goes to the slot its CENTRE falls in, so a
    glyph brushing a boundary cannot land in two cells."""
    cells: dict[tuple[int, int], list[tuple]] = {}
    for w in words:
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        c = next((i for i in range(len(xs) - 1) if xs[i] <= cx < xs[i + 1]), None)
        r = next((i for i in range(len(ys) - 1) if ys[i] <= cy < ys[i + 1]), None)
        if c is None or r is None:
            continue
        cells.setdefault((r, c), []).append(w)
    # Reading order WITHIN a cell is safe and necessary: the interleaving problem is
    # between columns, and a single cell has none.
    return {k: [w[4] for w in sorted(v, key=lambda w: (round(w[1], 1), w[0]))]
            for k, v in cells.items()}


def _cells_inside(tree_tokens: list[str], pdf_cells: dict) -> list[tuple[int, int]]:
    """Which PDF cells are present inside this one rendered cell.

    Coverage, not exact containment. Requiring the source cell's whole token run to
    appear verbatim matched 17 of 905 cells on Jersey — a single hyphenation, footnote
    marker or rendering difference breaks the run — so the check passed having compared
    almost nothing. Contiguous-run coverage is the same measure check_content_localized
    uses to tell restructured text from lost text, and it tolerates those without
    tolerating a cell that is merely similar.

    Cells with IDENTICAL text count once. Jersey p50 prints the same table twice side by
    side, so a tree cell legitimately matches both copies — two slots, same row,
    different columns, which the classifier would otherwise read as a column merge.
    Duplicate content is ambiguity about WHICH cell this is, never evidence that two
    were joined.
    """
    matches = []
    for k, toks in sorted(pdf_cells.items()):
        if len(toks) < MIN_PDF_CELL_TOKENS:
            continue
        span = _best_span(tree_tokens, toks)
        if span is not None:
            matches.append((span, k))

    # Two source cells count as joined only if they sit in DIFFERENT PARTS of the
    # output cell. Overlapping spans mean the same text matched twice, which happens
    # constantly in this corpus: Jersey p65's question (g) contains the sentence "Does
    # the analysis depend on the type of investor..." and so does the continuation cell
    # at the top of the same page, and p50 prints the whole table twice side by side.
    # Both were reported as merges. A real merge is cell A's words THEN cell B's, so
    # requiring disjoint spans is the claim itself rather than a heuristic against it.
    # Longest span first: a cell that covers the whole rendered cell must be the one
    # kept, and the fragments inside it dropped.
    matches.sort(key=lambda m: (m[0][0] - m[0][1], m[0][0]))
    hits, taken = [], []
    for (start, end), k in matches:
        # Disjoint in POSITION...
        if any(start < e and s < end for s, e in taken):
            continue
        # ...and distinct in CONTENT. Australia p152 lists five jurisdictions whose
        # answers repeat the same boilerplate ("The relief applies to providing
        # financial advice, dealing, making a market…") and the same closing item
        # verbatim, so one row's cell matches a neighbouring row's as well. A source
        # cell whose text is contained in another matched cell's is the same text seen
        # twice, not a second cell that was joined to the first.
        toks = pdf_cells[k]
        if any(_is_subsequence(toks, pdf_cells[o]) for o in hits):
            continue
        hits.append(k)
        taken.append((start, end))
    return hits


def _longest_run(hay: list[str], needle: list[str]) -> int:
    """Length of the longest contiguous slice of `needle` that appears in `hay`."""
    at: dict[str, list[int]] = {}
    for p, tok in enumerate(hay):
        at.setdefault(tok, []).append(p)
    best, n = 0, len(needle)
    for i in range(n):
        if n - i <= best:
            break                              # nothing longer can start here
        for p in at.get(needle[i], ()):
            k, q = i, p
            while k < n and q < len(hay) and hay[q] == needle[k]:
                k += 1
                q += 1
            best = max(best, k - i)
    return best


def _is_subsequence(small: list[str], big: list[str]) -> bool:
    n = len(small)
    if n > len(big):
        return False
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


def _best_span(tree_tokens: list[str], pdf_tokens: list[str]):
    """Where this source cell sits inside the rendered cell -> (start, end), or None.

    Coverage, not exact containment. Requiring the source cell's whole token run to
    appear verbatim matched 17 of 905 cells on Jersey — a single hyphenation, footnote
    marker or rendering difference breaks the run — so the check passed having compared
    almost nothing.
    """
    from check_content_localized import _hay, decomposed_coverage
    if decomposed_coverage(pdf_tokens, _hay(tree_tokens)) < CELL_MATCH_THRESHOLD:
        return None
    # The span is the cell's FULL extent in the rendered cell — first matched token to
    # last — not its first run. That distinction is what makes the disjointness test
    # below work. Measured on Australia p120 and Jersey p65, which share a shape: a
    # question cell ENDS with the same sentence that also stands alone as the
    # continuation cell at the top of the page. The long cell's text is interrupted
    # mid-way by a rendering difference, so a first-run span stopped early, left the
    # trailing sentence outside it, and the standalone cell then matched a "disjoint"
    # region — a merge reported on one cell that legitimately contains both sentences.
    # Taking the whole extent lets the long cell swallow the short one, which is the
    # truth about where that text sits.
    at: dict[str, list[int]] = {}
    for p, tok in enumerate(tree_tokens):
        at.setdefault(tok, []).append(p)
    n, w = len(pdf_tokens), MIN_PDF_CELL_TOKENS
    lo = hi = None
    i = 0
    while i <= n - w:
        hit = None
        for p in at.get(pdf_tokens[i], ()):
            if tree_tokens[p:p + w] == pdf_tokens[i:i + w]:
                hit = p
                break
        if hit is None:
            i += 1
            continue
        end, k = hit + w, i + w
        while k < n and end < len(tree_tokens) and tree_tokens[end] == pdf_tokens[k]:
            end += 1
            k += 1
        lo = hit if lo is None else min(lo, hit)
        hi = end if hi is None else max(hi, end)
        i = k
    return None if lo is None else (lo, hi)


def _classify(found: list[tuple[int, int]]) -> str | None:
    """Two source cells in one output cell — which way were they joined?"""
    if len(found) < 2:
        return None
    rows = {r for r, _c in found}
    cols = {c for _r, c in found}
    if len(rows) == 1 and len(cols) > 1:
        return "column_merge"
    if len(cols) == 1 and len(rows) > 1:
        return "row_merge"
    return "block_merge"        # spans both axes — a whole sub-grid collapsed


_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)


_BADGE_RE = re.compile(r"table_\d{3,}")


def _tree_grid_cells(tree_root: Path) -> list[dict]:
    """Every rendered cell with the position its own markup gives it, keyed by the
    TABLE ID it belongs to.

    The tree states its grid directly — <table>, <tr>, <td> — so for a cell that came
    from ANOTHER TREE there is nothing to infer: no corridors, no printed rules, no
    geometry at all. That is why the stage-to-stage comparison is reliable where the
    PDF one is not.

    The table id is what scopes the comparison, and without it the check is unusable.
    Matching a stage-5 cell against every stage-3 cell in the document cross-matches
    the boilerplate these questionnaires repeat verbatim in every section: "Please
    confirm whether your response to section 5(a) would apply…" appears in both
    07-marketing-selling-to-the-public and 08-private-placement-regime, so an untouched
    Jersey reported 8 merges, at least one of which was a question still sitting in its
    own <td> at stage 5. Badges survive subchunking (10 of stage 3's 14 ids are still
    present at stage 5), so they are the join key.
    """
    out = []
    for f in sorted(tree_root.rglob("*.md")):
        raw = f.read_text(encoding="utf-8", errors="ignore")
        badges = [(m.start(), m.group(0)) for m in _BADGE_RE.finditer(raw)]
        for ti, m in enumerate(_TABLE_RE.finditer(raw)):
            # The badge this table is rendered under: the nearest one before it, or
            # the first one after if the badge follows its table.
            before = [b for pos, b in badges if pos <= m.start()]
            after = [b for pos, b in badges if pos > m.start()]
            tid = before[-1] if before else (after[0] if after else None)
            for ri, row in enumerate(_TR_RE.findall(m.group(1))):
                for ci, (attrs, body) in enumerate(_CELL_RE.findall(row)):
                    toks = _norm_words(body)
                    if toks:
                        out.append({"tokens": toks, "row": ri, "col": ci,
                                    "table": tid, "group": (str(f.relative_to(tree_root)), ti),
                                    "spans": bool(_SPAN_RE.search(attrs or ""))})
    return out


def compute_cell_merges(out_root: Path, stage: int = 5, baseline_stage: int = 3) -> dict:
    """Did a later stage join cells that the earlier one kept apart?

    THIS IS THE CHECK THAT ANSWERS "did AI post-processing merge two rows or columns",
    and it is deliberately a stage-to-stage comparison rather than a PDF one.

    The PDF version of this question needs the source grid reconstructed from word
    geometry, and that is where it keeps failing: corridors and printed rules only
    reconstruct a usable grid on 62% of table-pages, and even then a rendered cell often
    does not correspond to any single slot. Injecting real merges into a stage-5 tree,
    the geometry check caught 1 of 6.

    Stage 3 needs none of that. It is the AI pass's own INPUT, its markup states the
    grid outright, and "the model joined two of the cells it was given" is exactly the
    defect being looked for. A merge is then one later-stage cell that contains two
    distinct earlier-stage cells — same row, different columns is a column merge; same
    column, different rows is a row merge — read off the earlier tree's own <tr>/<td>.

    The containment rules are the ones the PDF version already needed, and they matter
    here for the same reason: the spans must be disjoint and neither cell's text may be
    contained in the other's, or repeated boilerplate reads as a merge.
    """
    out_root = Path(out_root)
    try:
        base_root = resolve_stage_dir(out_root, baseline_stage)
        tree_root = resolve_stage_dir(out_root, stage)
    except Exception as e:  # noqa: BLE001
        return {"passed": True, "skipped": True, "reason": str(e), "flags": [],
                "cells_checked": 0}
    if base_root == tree_root:
        return {"passed": True, "skipped": True,
                "reason": f"stage {stage} is the baseline — nothing to compare against",
                "flags": [], "cells_checked": 0}

    by_table: dict[str, list[dict]] = {}
    for c in _tree_grid_cells(base_root):
        if len(c["tokens"]) >= MIN_PDF_CELL_TOKENS:
            by_table.setdefault(c["table"], []).append(c)
    if not by_table:
        return {"passed": True, "skipped": True,
                "reason": f"no attributable tables in stage {baseline_stage}",
                "flags": [], "cells_checked": 0}

    # Which stage-3 table each stage-5 table corresponds to. The badge is the join key
    # where it survives; where it does not, content decides. Subchunking splits one
    # section's table across several files and the badge stays with only one of them —
    # measured on Jersey, 01-7.1-private-placement-regime-funds.md holds a whole table
    # and no badge at all, so badge-only attribution silently dropped every one of its
    # cells and the check went blind on exactly the file the faults were injected into.
    later = _tree_grid_cells(tree_root)
    groups: dict[tuple, list[dict]] = {}
    for c in later:
        groups.setdefault(c["group"], []).append(c)
    attribution: dict[tuple, str | None] = {}
    for g, cells in groups.items():
        tid = next((c["table"] for c in cells if c["table"]), None)
        if tid is None:
            tid = _best_matching_table(cells, by_table)
        attribution[g] = tid

    flags, checked, skipped_no_table = [], 0, 0
    for cell in later:
        toks = cell["tokens"]
        if len(toks) < MIN_TREE_CELL_TOKENS or cell["spans"]:
            continue
        # ONLY this table's own stage-3 cells. Comparing against the whole document
        # cross-matches repeated boilerplate — see _tree_grid_cells.
        base = by_table.get(attribution.get(cell["group"]))
        if not base:
            skipped_no_table += 1
            continue
        checked += 1
        found = _baseline_cells_inside(toks, base)
        if len(found) < 2:
            continue
        kind = _classify([(base[i]["row"], base[i]["col"]) for i in found])
        if kind is None:
            continue
        flags.append({
            "kind": kind, "table_id": cell["table"],
            "page": None, "source_cells": len(found),
            "slots": [[base[i]["row"], base[i]["col"]] for i in found],
            "preview": " ".join(toks[:16]) + (" …" if len(toks) > 16 else ""),
            "detail": (
                f"one cell in stage {stage} holds {len(found)} cells that stage "
                f"{baseline_stage} kept apart in {cell['table']} "
                f"({'side by side' if kind == 'column_merge' else 'one above the other' if kind == 'row_merge' else 'a block'})"
                f". Nothing is missing — the pairing is. Joined: "
                + " | ".join(" ".join(base[i]['tokens'][:8]) for i in found[:3])),
        })
    return {
        "cells_checked": checked,
        "cells_comparable": checked,
        "cells_unattributable": skipped_no_table,
        "baseline_cells": sum(len(v) for v in by_table.values()),
        "baseline_tables": len(by_table),
        "flags": flags,
        "column_merges": sum(1 for f in flags if f["kind"] == "column_merge"),
        "row_merges": sum(1 for f in flags if f["kind"] == "row_merge"),
        "block_merges": sum(1 for f in flags if f["kind"] == "block_merge"),
        "merge_count": len(flags),
        "scored_stage": stage,
        "baseline_stage": baseline_stage,
        "passed": not flags,
    }


def _best_matching_table(cells: list[dict], by_table: dict) -> str | None:
    """Which baseline table these unbadged cells came from, decided by content.

    Scored on whole-cell hits rather than loose overlap, so a table is only claimed by
    the one whose actual cells it reproduces — the repeated boilerplate that defeats a
    document-wide comparison is present in every candidate equally and cannot break the
    tie on its own."""
    sigs = {}
    for tid, base in by_table.items():
        sigs[tid] = {" ".join(c["tokens"]) for c in base}
    best, score = None, 0
    for tid, sig in sigs.items():
        n = sum(1 for c in cells if " ".join(c["tokens"]) in sig)
        if n > score:
            best, score = tid, n
    return best if score >= 2 else None


def _baseline_cells_inside(tree_tokens: list[str], base: list[dict]) -> list[int]:
    """Indices of the baseline cells present in this one later-stage cell."""
    matches = []
    for i, c in enumerate(base):
        span = _best_span(tree_tokens, c["tokens"])
        if span is not None:
            matches.append((span, i))
    matches.sort(key=lambda m: (m[0][0] - m[0][1], m[0][0]))
    hits, taken = [], []
    for (start, end), i in matches:
        if any(start < e and s < end for s, e in taken):
            continue
        if any(_is_subsequence(base[i]["tokens"], base[o]["tokens"]) for o in hits):
            continue
        hits.append(i)
        taken.append((start, end))
    return hits


def compute_table_cells(out_root: Path, pdf: str | None = None, stage: int = 3) -> dict:
    import fitz

    out_root = Path(out_root)
    s2 = next((p for p in sorted(out_root.glob("0*_*mineru*/stage2_report.json"))), None)
    if s2 is None:
        return {"passed": True, "skipped": True, "reason": "no stage2_report.json found",
                "flags": [], "cells_checked": 0}
    report = json.loads(s2.read_text())
    try:
        tree_root = resolve_stage_dir(out_root, stage)
    except Exception as e:  # noqa: BLE001
        return {"passed": True, "skipped": True, "reason": str(e),
                "flags": [], "cells_checked": 0}

    doc = fitz.open(str(resolve_pdf(out_root, pdf)))
    try:
        flags, checked, comparable = [], 0, 0
        per_table: dict[str, dict] = {}
        for t in report.get("tables", []):
            if not t.get("ok"):
                continue
            pages = t.get("mineru_bbox_pages") or t.get("pages") or []
            boxes = t.get("mineru_bboxes") or ([t["bbox"]] if t.get("bbox") else [])
            if not pages or len(boxes) != len(pages):
                continue                      # no per-page geometry to reason about
            tid = t["table_id"]

            cells: list[tuple[str, str]] = []
            for f in iter_content_files(tree_root):
                raw = f.read_text(encoding="utf-8")
                if tid not in raw:
                    continue
                cells += list(_CELL_RE.findall(raw))
            if not cells:
                continue

            # The PDF's own cells, for every page this table covers.
            grid: dict[int, dict] = {}
            for p, bb in zip(pages, boxes):
                pw = [w for w in _page_words(doc, p)
                      if bb[0] - 1 <= (w[0] + w[2]) / 2 <= bb[2] + 1
                      and bb[1] - 1 <= (w[1] + w[3]) / 2 <= bb[3] + 1]
                xs = _column_bounds(pw, bb)
                ys = _row_bounds(_page_rules(doc, p), bb)
                if len(xs) < 3 and len(ys) < 3:
                    continue                  # one cell wide and one tall — no grid
                grid[p] = _pdf_cells(pw, xs, ys)

            for attrs, body in cells:
                cw = _norm_words(body)
                if len(cw) < MIN_TREE_CELL_TOKENS:
                    continue
                checked += 1
                if _SPAN_RE.search(attrs or ""):
                    continue                  # declares that it spans — allowed to
                for p, pdf_cells in grid.items():
                    found = _cells_inside(cw, pdf_cells)
                    if not found:
                        continue              # this cell is not on this page
                    comparable += 1
                    slot = per_table.setdefault(tid, {"comparable": 0, "merged": 0})
                    slot["comparable"] += 1
                    kind = _classify(found)
                    if kind:
                        slot["merged"] += 1
                        joined = " | ".join(
                            " ".join(pdf_cells[k][:9]) for k in sorted(found)[:3])
                        flags.append({
                            "kind": kind, "table_id": tid, "page": p,
                            "source_cells": len(found),
                            "slots": [list(k) for k in sorted(found)],
                            "preview": " ".join(cw[:16]) + (" …" if len(cw) > 16 else ""),
                            "detail": (
                                f"{tid} on page {p}: one rendered cell contains "
                                f"{len(found)} of the source's own cells "
                                f"({'side by side' if kind == 'column_merge' else 'one above the other' if kind == 'row_merge' else 'a block'})"
                                f". Nothing is missing — the pairing is. Source cells: "
                                f"{joined}"),
                        })
                    break
        for tid, slot in per_table.items():
            slot["merge_rate"] = round(slot["merged"] / max(slot["comparable"], 1), 4)
        return {
            "cells_checked": checked,
            "cells_comparable": comparable,
            "per_table": per_table,
            "flags": flags,
            "column_merges": sum(1 for f in flags if f["kind"] == "column_merge"),
            "row_merges": sum(1 for f in flags if f["kind"] == "row_merge"),
            "block_merges": sum(1 for f in flags if f["kind"] == "block_merge"),
            "merge_count": len(flags),
            "scored_stage": stage,
            "passed": not flags,
        }
    finally:
        doc.close()


def print_report(report: dict, top: int = 15):
    if report.get("skipped"):
        print(DIM(f"skipped — {report['reason']}"))
        return
    print(f"{DIM('rendered cells:')} {report['cells_checked']} "
          f"({report['cells_comparable']} matched to the PDF's own grid)")
    if not report["flags"]:
        print(f"\n{GREEN(BOLD('✓ PASS — no rendered cell holds two of the source cells'))}")
        return
    for f in report["flags"][:top]:
        print(f"\n    {RED(f['kind'].replace('_', ' ').upper())}  {BOLD(f['table_id'])} "
              f"{DIM('page ' + str(f['page']))}")
        print(f"      {DIM(f['preview'])}")
        print(f"      {DIM(f['detail'])}")
    print()
    print(RED(BOLD(f"✗ FAIL — {report['column_merges']} merged column(s), "
                   f"{report['row_merges']} merged row(s), "
                   f"{report['block_merges']} merged block(s)")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--stage", type=int, default=3)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    banner("Table cells · does each cell hold what the PDF puts in it?")
    report = compute_table_cells(Path(args.out_root).resolve(), stage=args.stage)
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
