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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import BOLD, DIM, GREEN, RED, YELLOW, banner, resolve_stage_dir, run_cli

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


def compute_table_placement(out_root: Path, stage: int = 1) -> dict:
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
