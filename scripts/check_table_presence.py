#!/usr/bin/env python3
"""check_table_presence.py — did every extracted table actually reach the output?

Stage 2 records, per table, whether MinerU produced usable rows. Stage 3 splices
those rows into the tree. Nothing verified the second half: a table successfully
extracted in Stage 2 could vanish before the final tree and no check noticed.
Proven, not theoretical — the fault-injection suite deleted a 4,623-character
rendered table and every existing check stayed silent, because sibling appendix
sections contain near-identical tables so the deleted rows were found next door
and written off as "relocated".

This closes that hole with a STRUCTURAL comparison rather than a textual one:

    for every table Stage 2 marked ok, are its cells present in the tree?

Why structural matters. Tables are serialized row-major into HTML, which is a
different order from the one fitz reads the PDF page in, so any contiguous
run/sequence comparison over table content is invalid by construction — that
single fact is behind ~82% of the false content-gap warnings on the table-heavy
pilot document. Cell text is compared as an unordered set, so reordering is
irrelevant and only genuine absence registers.

Usage:
    python scripts/check_table_presence.py out/hybrid_extractions/_ui/<job_id>
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (
    BOLD, DIM, GREEN, RED, YELLOW, banner, clean_markdown, iter_content_files,
    resolve_pdf, resolve_stage_dir, tokenize, run_cli,
)

_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

# A cell must have some substance before its absence means anything: "No", "n/a"
# and bare digits recur throughout these documents and would match by accident.
MIN_CELL_CHARS = 12
# Below this fraction of cells found, the table did not make it into the output.
# Not 1.0 — Stage 3 legitimately rewrites some cell text (footnote markers become
# [^n] links), so a handful of cells can differ in a table that is fully present.
PRESENT_THRESHOLD = 0.6


# Punctuation this check strands a stray space next to, and only because tags were
# stripped there. _TAG_RE replaces every tag with a space so that removing one never
# glues two words together ("wordA<b>wordB" must not become "wordAwordB") -- correct
# for prose, but stage 4 legitimately adds inline markup a cell never had (a bare URL
# turned into a link, a defined term turned bold: measured on Jersey/181919's table_003,
# where every one of its 4 answer cells failed this check purely because "(the FSL)"
# became "(the FSL )" once an injected </strong> was stripped from beside the
# parenthesis -- 0 characters of the answer actually changed). Collapsing that spacing
# AFTER tag-stripping keeps the anti-glue reason intact while not billing markup
# addition as content loss.
_SPACE_BEFORE_CLOSE_RE = re.compile(r"\s+([),.;:!?])")
_SPACE_AFTER_OPEN_RE = re.compile(r"([(])\s+")


def _norm(s: str) -> str:
    s = re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", s or ""))).strip().lower()
    s = _SPACE_BEFORE_CLOSE_RE.sub(r"\1", s)
    s = _SPACE_AFTER_OPEN_RE.sub(r"\1", s)
    return s


# How much of a cell's own text must be accounted for by contiguous runs in the tree
# before the cell counts as present, once the tree is allowed to re-split it. High on
# purpose: this relaxes WHERE the boundaries fall, never HOW MUCH survives, so a cell
# that genuinely lost half its text still fails.
CELL_DECOMPOSE_THRESHOLD = 0.85


def _cell_present(cell: str, haystack: str, decomposed_hay: str) -> bool:
    """Whole-cell match first — it is exact and cheap. The run-coverage fallback only
    runs past stage 3, where `decomposed_hay` is non-empty (see compute_table_presence)."""
    if cell in haystack:
        return True
    if not decomposed_hay:
        return False
    from check_content_localized import decomposed_coverage
    return decomposed_coverage(tokenize(cell), decomposed_hay) >= CELL_DECOMPOSE_THRESHOLD


def _cell_haystack(tree_root: Path) -> str:
    """Every <td>/<th> the tree renders, normalised exactly as _cells normalises the
    extracted side, so the two are comparable. Sentinel-joined so a match cannot be
    stitched across two adjacent cells."""
    parts = []
    for f in iter_content_files(tree_root):
        for raw in _CELL_RE.findall(f.read_text(encoding="utf-8")):
            parts.append(_norm(raw))
    return " \x00 ".join(parts)


def _cells(table_html: str) -> list[str]:
    out = []
    for raw in _CELL_RE.findall(table_html or ""):
        t = _norm(raw)
        if len(t) >= MIN_CELL_CHARS:
            out.append(t)
    return out


# A continuation region's own text, compared against the table cells the tree
# actually holds. Well below 1.0 on purpose: the region text includes rules,
# wrapped fragments and the odd stray glyph that never become their own cell.
CONT_COVERAGE_THRESHOLD = 0.5

# Below this, a "region" holds too little text for any coverage measure to mean
# anything: a window test needs a window's worth of words, and a whole-cell test needs
# a cell's worth. MinerU's continuation stubs are routinely this small — a strip holding
# just the table's reprinted header row — so measuring them as-is manufactured failures.
MIN_REGION_TOKENS = 8

# MATCH DIRECTION IS THE WHOLE GAME HERE, and getting it wrong produced a large
# class of false failures.
#
# A table cell routinely SPANS a page break: a row's text begins on page N and
# runs onto N+1. So a continuation region on page N+1 holds only a FRAGMENT of
# that cell, and asking "does the whole cell fit inside this region?" can never
# be true — it reported 0.08-0.15 coverage on tables MinerU had merged perfectly,
# because the cells were longer than the page slice being checked.
#
# The correct question is the reverse: are the REGION's word windows found inside
# the tree's cells? A fragment matches its parent cell, so a merged table
# verifies, while genuinely dropped rows still match nothing.
#
# But neither direction alone is right, which the data settled decisively: on one
# group of tables whole-cell-into-region scored 1.00 and window-into-cell 0.43; on
# another the same two scored 0.10 and 0.80. Two genuinely different shapes:
#
#   cells FIT inside the page  -> the region contains complete cells
#                                 -> match whole cells into the region
#   cells SPAN the page break  -> the region contains fragments of long cells
#                                 -> match region windows into the cells
#
# Presence can legitimately manifest either way, so both count as evidence and
# the coverage is the better of the two. Requiring one direction would fail every
# table of the other shape — which is precisely the false-failure class here.
CONT_WINDOW = 6
CONT_STRIDE = 3
_CELL_SEP = "\x02"


def _cells_hay(cells: list[list[str]]) -> str:
    """Sentinel-joined cells: a window can only match if it lies wholly inside
    ONE cell, so unrelated cells cannot be stitched together into a false match."""
    return _CELL_SEP.join("\x01" + "\x01".join(c) + "\x01" for c in cells if c)


def _region_covered_by_cells(region_tokens: list[str], hay: str) -> float:
    """Fraction of the region's word windows found inside a single tree cell."""
    if not hay or not region_tokens:
        return 0.0
    w = min(CONT_WINDOW, len(region_tokens))
    stride = max(1, min(CONT_STRIDE, w))
    wins = [region_tokens[i:i + w]
            for i in range(0, len(region_tokens) - w + 1, stride)] or [region_tokens]
    return sum(1 for x in wins if ("\x01" + "\x01".join(x) + "\x01") in hay) / len(wins)


def _verify_continuations(report: dict, out_root: Path, cells: list[list[str]]) -> list[dict]:
    """Was a merged page-spanning table's content actually kept?

    Stage 2 marks a region `continuation` when MinerU merged the table across
    pages and left an empty stub here, meaning the rows are rendered with the
    neighbouring page's table. That was previously BELIEVED rather than checked —
    the table scored a flat 0.75 whether the merge kept the rows or dropped them.

    This checks it: clip the PDF to the region's own bbox, then measure how much
    of that text is accounted for by table cells present in the tree. Order-free
    by construction, because the tree serializes cells row-major while the PDF
    reads across columns — the same reason a sequence comparison over tables is
    invalid anywhere else."""
    import fitz
    from check_content_localized import _units_ratio

    conts = [t for t in report.get("tables", []) if t.get("continuation")]
    if not conts:
        return []
    pdf_path = resolve_pdf(out_root, None)
    hay = _cells_hay(cells)
    doc = fitz.open(str(pdf_path))
    try:
        out = []
        for t in conts:
            bbox, pages = t.get("bbox"), t.get("pages") or []
            if not bbox or not pages:
                # Multi-page comparison tables carry no bbox, so there is no region
                # to clip and nothing honest to measure. Say so rather than guess.
                out.append({"table_id": t["table_id"], "pages": pages, "verified": None,
                            "coverage": None,
                            "detail": "no bbox for this region (multi-page comparison table) — "
                                      "the merge cannot be verified either way"})
                continue
            pno = pages[0]
            if pno < 1 or pno > doc.page_count:
                continue
            page = doc[pno - 1]
            region = page.get_text("text", clip=fitz.Rect(*bbox))
            toks = tokenize(region)
            widened = False
            if len(toks) < MIN_REGION_TOKENS:
                # The stub is not the table's area on this page — it is the reprinted
                # HEADER ROW and nothing else. Hungary Data Privacy 180716 gave five
                # such stubs, each a 19pt strip holding "Term Meaning": two tokens
                # cannot satisfy a window or whole-cell test, so all five scored 0% and
                # were recorded as dropped merges, dragging fidelity to 62.1 and failing
                # the document. The rows were all there — 77-89% of each page was present
                # in the tree's cells.
                #
                # A continuation stub means "this table carries on from here", so the
                # region to measure is the rest of the page in the table's own column
                # span, not the strip MinerU happened to leave behind.
                x0, y0, x1, _y1 = bbox
                wide = fitz.Rect(x0, y0, x1, page.rect.y1)
                w_toks = tokenize(page.get_text("text", clip=wide))
                if len(w_toks) > len(toks):
                    toks, widened = w_toks, True
            # whichever shape this table has, presence is presence
            cov = max(_region_covered_by_cells(toks, hay),
                      _units_ratio(toks, cells)) if toks else 0.0
            # Still nothing to measure: say so instead of calling it a loss. `None`
            # scores as an unverifiable merge (0.75), not a failure (0.25).
            if len(toks) < MIN_REGION_TOKENS:
                out.append({"table_id": t["table_id"], "pages": pages, "verified": None,
                            "coverage": None,
                            "detail": f"only {len(toks)} word(s) of text in this region — "
                                      "too little to verify the merge either way"})
                continue
            ok = cov >= CONT_COVERAGE_THRESHOLD
            out.append({
                "table_id": t["table_id"], "pages": pages,
                "verified": ok, "coverage": round(cov, 2),
                "region": "rest of page (stub held only the reprinted header)" if widened else "stub bbox",
                "detail": (f"{cov:.0%} of this region's text is present in the tree's table "
                           "cells — the merged table kept these rows"
                           if ok else
                           f"only {cov:.0%} of this region's text appears in any table cell in "
                           "the tree — the merge appears to have DROPPED these rows"),
            })
        return out
    finally:
        doc.close()


def compute_table_presence(out_root: Path, stage: int = 3) -> dict:
    out_root = Path(out_root)
    s2 = next((p for p in sorted(out_root.glob("0*_*mineru*/stage2_report.json"))), None)
    if s2 is None:
        return {"passed": True, "skipped": True, "reason": "no stage2_report.json found",
                "flags": [], "tables_checked": 0}
    report = json.loads(s2.read_text())
    tables_dir = s2.parent / "tables"

    tree_root = resolve_stage_dir(out_root, stage)
    tree_text = " ".join(_norm(clean_markdown(f.read_text(encoding="utf-8")))
                         for f in iter_content_files(tree_root))
    tree_raw = " ".join(f.read_text(encoding="utf-8") for f in iter_content_files(tree_root))
    # WHAT COUNTS AS "PRESENT" DEPENDS ON THE STAGE, because what the pipeline is
    # ALLOWED to do to a table depends on the stage.
    #
    # Up to stage 3 nothing may restructure a table, so the claim this check makes —
    # "the extracted ROWS reached the output" — means the tree renders them AS a table.
    # Searching all of the document's prose instead cannot support that claim: a table
    # whose text is also printed as flowed prose passes even when the table itself was
    # deleted outright. Measured on Bahamas-sonnet-nocounts, deleting table_001's markup
    # from 01-front-matter.md entirely: 6 of its 9 cells are still "found" because the
    # cover page repeats "Reporting Counsel for Part B:", "Date of Report:" and the
    # responsibility paragraph as prose, so the ratio holds at 0.67 and the deletion is
    # reported as nothing. Against rendered cells the same deletion scores 0.0, and
    # across 124 local jobs / 1497 extracted tables ZERO tables that pass on prose fall
    # below the threshold on cells — a table that genuinely rendered renders as a table.
    #
    # From stage 4 on that is no longer true. The AI pass may lift a label row into a
    # heading, and stage 5 splits a section's questionnaire table across sub-chunk
    # files as prose — both sanctioned, neither a loss. Measured on Jersey__181919's
    # table_003 at stage 5: 3 of its 8 cells are still cells, 4 are present verbatim as
    # prose under their own sub-section, 1 differs. Requiring <td> there manufactures
    # exactly the table-loss false positive this file has twice been corrected for.
    #
    # So: cells-only while the tree must keep its tables, cells-or-prose once it may
    # not. The wider haystack does NOT reopen the deleted-table hole — verified by
    # deleting a rendered table from 05_subchunks on Bahamas-sonnet-nocounts, which
    # raises 2 flags (table_003 0/10 cells, table_004 3/7) against a baseline of 0.
    # It is weaker than the stage-3 rule, though, and the way to close that gap
    # properly is to verify the table against the SOURCE PDF's own region rather than
    # against stage 2's extraction of it — separate work.
    restructuring_allowed = stage >= 4
    cell_hay = _cell_haystack(tree_root)
    haystack = (cell_hay + " \x00 " + tree_text) if restructuring_allowed else cell_hay
    # A whole-cell substring match also has to be relaxed after stage 4, and for a
    # reason that has nothing to do with the AI: MinerU routinely extracts several
    # cells AS ONE. Measured on Bahamas-sonnet-nocounts table_011, one extracted "cell"
    # is 761 characters holding questions (a) (b) (c) and (d); the tree has them as four
    # separate cells with their answers interleaved between them — which is CORRECT, and
    # more correct than the extraction it is being compared against. Whole-cell matching
    # then reports 75 of 156 cells missing on a table that lost nothing.
    #
    # So past stage 3 a cell counts as present when its CONTENT is accounted for, even
    # if the tree split it: maximal contiguous token runs, the same measure
    # check_content_localized uses to tell a restructured span from a lost one. A
    # genuinely deleted table still scores ~0 — its tokens are in no run at all.
    decomposed_hay = ""
    if restructuring_allowed:
        from check_content_localized import _hay
        decomposed_hay = _hay(tokenize(haystack))

    flags, checked = [], 0
    for t in report.get("tables", []):
        if not t.get("ok"):
            continue                      # Stage 2 failure — Fidelity covers that
        tid = t["table_id"]
        md = tables_dir / tid / "table.md"
        if not md.exists():
            continue
        cells = _cells(md.read_text(encoding="utf-8"))
        if not cells:
            continue
        checked += 1
        found = sum(1 for c in cells if _cell_present(c, haystack, decomposed_hay))
        ratio = found / len(cells)
        if ratio >= PRESENT_THRESHOLD:
            continue
        # Prose is still consulted, but only to DESCRIBE a failure, never to excuse
        # one: "the rows are gone but the words survive as prose" and "this content
        # left the document" are different repairs, and the reviewer needs to know
        # which one they are looking at.
        in_prose = sum(1 for c in cells if c in tree_text)
        flags.append({
            "table_id": tid, "pages": t.get("pages", []),
            "rows": t.get("rows"), "cols": t.get("cols"),
            "cells_total": len(cells), "cells_found": found, "found_ratio": round(ratio, 2),
            "cells_found_as_prose": in_prose,
            # A badge with no rows is a different defect from a table that never
            # rendered at all, and points at different code, so distinguish them.
            "badge_in_tree": tid in tree_raw,
            "detail": (f"Stage 2 extracted {t.get('rows')} rows x {t.get('cols')} cols for "
                       f"{tid}, but only {found} of {len(cells)} substantive cells appear in "
                       f"any table the tree renders"
                       + (" — its badge IS present, so the rows were lost after the table was "
                          "spliced in" if tid in tree_raw else
                          " — no badge either, so the table never reached the output")
                       + (f". {in_prose} of them do appear as ordinary prose elsewhere, so "
                          "the text survives but the table structure does not"
                          if in_prose > found else "")),
        })

    from check_content_localized import restructured_units
    cells, _notes = restructured_units(
        [f.read_text(encoding="utf-8") for f in iter_content_files(tree_root)])
    continuations = _verify_continuations(report, out_root, cells)
    cont_failed = [c for c in continuations if c["verified"] is False]

    return {
        "tables_checked": checked,
        "flags": flags,
        "missing_count": len(flags),
        "continuations": continuations,
        "continuations_verified": sum(1 for c in continuations if c["verified"] is True),
        "continuations_failed": len(cont_failed),
        "continuations_unverifiable": sum(1 for c in continuations if c["verified"] is None),
        "passed": not flags and not cont_failed,
    }


def print_report(report: dict, top: int = 20):
    if report.get("skipped"):
        print(YELLOW(f"skipped — {report['reason']}"))
        return
    print(f"{DIM('tables extracted ok and checked:')} {report['tables_checked']}")
    conts = report.get("continuations") or []
    if conts:
        print(f"{DIM('merged page-spanning regions:')} "
              f"{report['continuations_verified']} verified, "
              f"{report['continuations_failed']} dropped, "
              f"{report['continuations_unverifiable']} unverifiable")
        for c in conts:
            mark = GREEN("✓") if c["verified"] else (DIM("?") if c["verified"] is None else RED("✗"))
            print(f"    {mark} {c['table_id']}  {DIM(c['detail'][:96])}")
    if not report["flags"] and not report.get("continuations_failed"):
        print(f"\n{GREEN(BOLD('✓ PASS — every extracted table reached the tree, merges verified'))}")
        return
    for f in report["flags"][:top]:
        print(f"\n    {RED('MISSING')}  {BOLD(f['table_id'])}  "
              f"{DIM('page(s) ' + ','.join(str(p) for p in f['pages']))}")
        print(f"      {DIM(f['detail'])}")
    print()
    print(RED(BOLD(f"✗ FAIL — {report['missing_count']} extracted table(s) are not in the "
                   "final tree")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    banner("Table presence · extracted rows vs the final tree")
    report = compute_table_presence(Path(args.out_root).resolve())
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
