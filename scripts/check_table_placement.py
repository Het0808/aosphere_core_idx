#!/usr/bin/env python3
"""check_table_placement.py — Rule A page-range invariant: does every table's
ACTUAL page fall inside the page range of the section pdf2mdtree assigned it
to?

pdf2mdtree assigns section membership by finding where each TOC heading's
title matches in the linear paragraph stream (see headings_manifest.json,
written by pdf2mdtree.py right after `matched` is computed) — a text-position
search, not a geometric one. That search can drift: if a heading's title
match lands later in the stream than the heading's true position, everything
between — prose AND table placeholders alike — silently renders under the
PREVIOUS section instead.

This check doesn't fix that (see docs/GEOMETRY_MATCHING_DESIGN.md for the
planned single-pass geometric rewrite) — it catches the symptom cheaply, from
data pdf2mdtree already writes to disk: each table already knows its own
page (tables_manifest.json); each heading already knows the page its title
matched on (headings_manifest.json). A table whose page falls outside its
assigned section's [start, end) page range — beyond a small tolerance for
ordinary TOC/rendering drift — gets flagged [PLACEMENT UNCERTAIN] instead of
silently trusted.

Usage:
    python scripts/check_table_placement.py out/hybrid/172099
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (BOLD, DIM, GREEN, RED, YELLOW, StageDirectoryNotFoundError,
                                 banner, parse_page_range, resolve_stage_dir, run_cli)

# TOC page numbers drift a little from rendering/edits even in a healthy PDF;
# don't flag a table that's merely 1 page off from its section's boundary.
PAGE_TOLERANCE = 1


def _section_ranges(headings: list[dict]) -> list[dict]:
    """headings is already in document order (see pdf2mdtree's `matched` list).
    Section i's range is [its own page, the NEXT heading's page) — any level,
    since a table belongs to whichever heading (at any depth) is currently
    open, not just its nearest same-level sibling."""
    ranges = []
    for i, h in enumerate(headings):
        end = headings[i + 1]["page"] - 1 if i + 1 < len(headings) else None
        ranges.append({"title": h["title"], "level": h["level"],
                       "start": h["page"], "end": end})
    return ranges


def _section_for_page(ranges: list[dict], page: int) -> dict | None:
    """The LAST section whose start <= page — i.e. whichever heading is most
    recently 'open' by the time this page is reached, matching pdf2mdtree's
    own content_between() semantics."""
    candidate = None
    for r in ranges:
        if r["start"] <= page:
            candidate = r
        else:
            break
    return candidate


# "**⚙ MinerU-extracted table** — table_010, pages 125–131, 26 rows × 12 cols": the badge
# the pipeline writes above every extracted table, so the OUTPUT itself says which source
# pages that table came from.
_BADGE = re.compile(r"MinerU-extracted table\*\*\s*[—-]\s*(table_\d+),\s*pages?\s*(\d+)(?:[–-](\d+))?")
_NOT_CONTENT = ("README.md", "PIPELINE_SUMMARY.md")


def _tree_chunks(tree: Path) -> list[dict]:
    """Every content chunk of an output tree with its source page range.

    A file inside a sub-folder (stage 5 splits a section into its subsections) is held
    to the range of the WHOLE folder, not its own: Stage 2 extracts one table per
    section and every subsection file carries the table's badge, so a subsection that
    legitimately shows part of a section-wide table must not read as having strayed."""
    chunks = []
    for path in sorted(tree.rglob("*.md")):
        if path.name in _NOT_CONTENT or path.name.endswith("REPORT.md"):
            continue
        raw = path.read_text(encoding="utf-8", errors="ignore")
        chunks.append({"rel": path.relative_to(tree).as_posix(), "dir": path.parent,
                       "raw": raw, "range": parse_page_range(raw)})
    folder: dict[Path, list[tuple[int, int]]] = {}
    for c in chunks:
        if c["range"]:
            folder.setdefault(c["dir"], []).append(c["range"])
    for c in chunks:
        if c["dir"] != tree and folder.get(c["dir"]):
            c["range"] = (min(r[0] for r in folder[c["dir"]]), max(r[1] for r in folder[c["dir"]]))
    return chunks


def _tree_flags(out_root: Path, tree_stage: int, already: set) -> list[dict]:
    """Tables that sit in an output chunk whose page range does not contain them.

    The Stage 1 comparison above judges a table by its PAGE against the section its page
    belongs to, so it cannot see a table that is on the right page and in the wrong FILE:
    Spain__176285 table_010 (pages 125-131, the "10. LICENCE" table) is written at the end
    of "9 PROSPECTUS REGULATION", a chunk that covers pages 122-125, while "10 LICENCE"
    is left holding page snapshots. The badge in the output says where the table came
    from and the chunk header says what the chunk covers; a table running more than
    PAGE_TOLERANCE beyond its chunk is not that chunk's content."""
    try:
        tree = resolve_stage_dir(out_root, tree_stage)
    except StageDirectoryNotFoundError:
        return []
    chunks = _tree_chunks(tree)
    flags = []
    for c in chunks:
        if not c["range"]:
            continue
        lo, hi = c["range"]
        for m in _BADGE.finditer(c["raw"]):
            tid, p1 = m.group(1), int(m.group(2))
            p2 = int(m.group(3) or m.group(2))
            if tid in already or (p1 >= lo - PAGE_TOLERANCE and p2 <= hi + PAGE_TOLERANCE):
                continue
            home = [h for h in chunks if h is not c and h["range"]
                    and h["range"][0] - PAGE_TOLERANCE <= p1 and p2 <= h["range"][1] + PAGE_TOLERANCE]
            home = min(home, key=lambda h: h["range"][1] - h["range"][0], default=None)
            flags.append({
                "table_id": tid, "pages": list(range(p1, p2 + 1)), "file": c["rel"],
                "expected_section": home["rel"] if home else None,
                "expected_range": list(home["range"]) if home else None,
                "detail": (f"table spans pages {p1}-{p2} but {c['rel']} covers pages {lo}-{hi}"
                           f" (±{PAGE_TOLERANCE} tolerance), so it is not that chunk's content"
                           + (f"; {home['rel']} covers pages {home['range'][0]}-{home['range'][1]}"
                              " and is where it belongs" if home else "")
                           + " — the table was filed under the preceding section."),
            })
            already.add(tid)
    return flags


def compute_table_placement(out_root: Path, stage: int = 1,
                            tree_stage: int | None = None) -> dict:
    stage1 = resolve_stage_dir(out_root, stage)
    headings_path = stage1 / "headings_manifest.json"
    tables_path = stage1 / "tables_manifest.json"

    if not headings_path.exists() or not tables_path.exists():
        return {"passed": True, "flags": [], "skipped": True,
                "reason": f"{headings_path.name} or {tables_path.name} not found under {stage1}"}

    headings = json.loads(headings_path.read_text()).get("headings", [])
    tables = json.loads(tables_path.read_text()).get("tables", [])
    ranges = _section_ranges(headings)

    flags = []
    for t in tables:
        page = t["pages"][0]
        sec = _section_for_page(ranges, page)
        if sec is None:
            flags.append({
                "table_id": t["table_id"], "pages": t["pages"],
                "expected_section": None,
                "detail": "no heading found at or before this table's page — "
                          "it would render before any section (front matter?) or "
                          "the outline failed to resolve entirely.",
            })
            continue
        lo = sec["start"] - PAGE_TOLERANCE
        hi = (sec["end"] + PAGE_TOLERANCE) if sec["end"] is not None else float("inf")
        if not (lo <= page <= hi):
            flags.append({
                "table_id": t["table_id"], "pages": t["pages"],
                "expected_section": sec["title"],
                "expected_range": [sec["start"], sec["end"]],
                "detail": f"table is on page {page}, but its assigned section "
                          f"\"{sec['title']}\" spans pages {sec['start']}"
                          f"{'-' + str(sec['end']) if sec['end'] else '+'} "
                          f"(±{PAGE_TOLERANCE} tolerance) — likely rendered under the wrong heading.",
            })

    # The scored OUTPUT tree (stage 3 unless a later one is being scored): where did each
    # table actually get written?
    flags += _tree_flags(Path(out_root), tree_stage or 3, {f["table_id"] for f in flags})

    return {
        "passed": not flags,
        "headings_total": len(headings),
        "tables_total": len(tables),
        "flags": flags,
    }


def print_report(report: dict):
    if report.get("skipped"):
        print(YELLOW(f"skipped — {report['reason']}"))
        return
    print(f"{DIM('headings:')} {report['headings_total']}    {DIM('tables:')} {report['tables_total']}")
    if not report["flags"]:
        print(f"\n{GREEN(BOLD('✓ PASS — every table falls inside its assigned section page range'))}")
        return
    n = len(report["flags"])
    print(f"\n{RED(BOLD(f'⚠ {n} table(s) with uncertain placement:'))}")
    for f in report["flags"]:
        print(f"    {RED(f['table_id'])} (p.{','.join(str(p) for p in f['pages'])})  {DIM(f['detail'])}")
    print(f"\n{RED(BOLD(f'✗ FAIL — {n} table(s) need manual placement review'))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--stage", type=int, default=1, choices=[1])
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    out_root = Path(args.out_root).resolve()
    banner("Rule A · table placement (page-range invariant)")
    report = compute_table_placement(out_root, args.stage)
    print_report(report)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))

    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
