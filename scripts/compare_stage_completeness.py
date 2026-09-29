#!/usr/bin/env python3
"""compare_stage_completeness.py — recovered/regressed text, heading coverage
and table row completeness between two stages of one job.

Built to answer one question the scorecard alone cannot: not just "how
complete is stage 5", but "compared to stage 3, what did the AI pass (stage 4)
and subchunking (stage 5) actually change" -- text it recovered, text it lost,
headings that stopped covering the source outline, and tables that lost rows.

Three checks, each against its own ground truth:

  text      check_content_localized's per-span dropped-token lists, matched
            by the actual missing text rather than by file. Stage 5's tree is
            a different shape from stage 3's (subchunking splits one section
            into several files), so matching by file path would silently
            miss every span that moved along with its section.

  headings  01_stage1_extract/headings_manifest.json -- the PDF's own
            bookmark outline, captured before either stage touched anything.
            Ground truth once, checked against both trees, rather than
            diffing the two trees' headings against each other (which would
            call a heading "missing" only if regressed, not if it had ALREADY
            been missing at stage 3 -- both are worth knowing separately).

  tables    02_stage2_mineru_tables/stage2_report.json's per-table row count
            -- MinerU's own count, from before either stage rewrote anything.
            Same reasoning as headings: ground truth once, not tree-vs-tree.

Usage:
    python scripts/compare_stage_completeness.py <job_root> [--from-stage 3] [--to-stage 5] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from check_content_localized import compute_content_localized  # noqa: E402
from lib_content_compare import resolve_stage_dir, tokenize  # noqa: E402
from lib_validate import final_stage  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Text: recovered vs regressed vs still-missing
# ---------------------------------------------------------------------------

def _span_key(tokens: list[str]) -> str:
    return " ".join(tokens).strip().lower()


def text_diff(job_root: Path, from_stage: int, to_stage: int) -> dict:
    a = compute_content_localized(job_root, stage=from_stage)
    b = compute_content_localized(job_root, stage=to_stage)

    def spans(report: dict) -> dict:
        out = {}
        for r in report["results"]:
            for d in r["dropped"]:
                key = _span_key(d["tokens"])
                if key and key not in out:      # first occurrence wins -- same
                    out[key] = {"file": r["file"], "text": " ".join(d["tokens"]),
                               "token_count": len(d["tokens"])}
        return out

    A, B = spans(a), spans(b)
    recovered = [v for k, v in A.items() if k not in B]
    regressed = [v for k, v in B.items() if k not in A]
    still_missing = [v for k, v in B.items() if k in A]
    by_size = lambda v: -v["token_count"]  # noqa: E731
    return {
        "from_stage": from_stage, "to_stage": to_stage,
        "dropped_spans_at_from": len(A), "dropped_spans_at_to": len(B),
        "recovered": sorted(recovered, key=by_size),
        "regressed": sorted(regressed, key=by_size),
        "still_missing": sorted(still_missing, key=by_size),
    }


# ---------------------------------------------------------------------------
# 2. Headings: does every outline title still exist in the tree?
# ---------------------------------------------------------------------------

# A group label the stage-4 pass folds INTO the heading. The page prints the label on
# its own line above the title ("APPENDIX 2" over "DISCLAIMERS: CLOSED-ENDED FUND") and
# stage 4 is told to fold the orphan into the H1, so stage 3 carries "2 Disclaimers:
# Closed-Ended Fund" and stage 4 carries "Appendix 2 Disclaimers: Closed-Ended Fund".
# The NUMBER is deliberately kept -- only the family word is dropped -- so appendix 2
# still cannot be mistaken for appendix 3.
_GROUP_WORD = re.compile(r"^(?:appendices|appendix|annexures?|annexes?|annexe|annex"
                         r"|schedules?|exhibits?)\s+(?=\d)")


def _norm_heading(title: str) -> str:
    # Tokenized, not just lowercased whitespace -- a plain string compare called "10.
    # LICENCE" missing when the outline has "10 LICENCE" (measured on Jersey/181919:
    # subchunking's own renumbering pass adds a period after the number). tokenize()
    # is the same tokenizer every other check in this pipeline diffs text with, so a
    # heading is "the same" here exactly when the rest of the pipeline would call it
    # the same words -- punctuation was never the content.
    #
    # The group word goes too, on BOTH sides, because the outline and the post-AI tree
    # disagree about it by design and neither is wrong: the bookmark names the appendix
    # "1 Disclaimers: Open-Ended Fund", stage 4 heads it "Appendix 1 Disclaimers:
    # Open-Ended Fund", and an exact compare called every one of them a heading the tree
    # had lost. Measured over the 16-09-26 corpus: 187 of the 197 headings reported
    # MISSING at stage 5 were this rewrite, spread across all 54 documents -- so the
    # panel flagged every document and the 10 real losses were indistinguishable noise.
    return _GROUP_WORD.sub("", " ".join(tokenize(title)))


# A bookmark that numbers a division the PAGE prints unnumbered. The PDF outline names
# the ELTIF supplementals' last division "1 Disclaimers"; the page heads it "DISCLAIMERS",
# stage 3 carries "DISCLAIMERS", and so does the shipped tree -- the ordinal exists only in
# the bookmark. Same family as the _GROUP_WORD fold above (the two sides disagree about a
# label neither took from the body text), and this check asks about a heading's TEXT, not
# its numbering, so the text being there is the answer.
#
# Only a BARE integer: "7.1 Private Placement Regime" keeps its number, because a section
# and its sub-section differ by exactly that and folding it would let one stand in for the
# other. And only where the fold is UNAMBIGUOUS -- see _fold_bare_ordinals: an outline with
# both "1 Disclaimers" and "2 Disclaimers" folds both onto one key, and a tree holding a
# single "Disclaimers" would then answer for both.
#
# Matched against the RAW title, before tokenize() flattens "7.1" to "7 1": on the
# normalized form a sub-section number is indistinguishable from a section number
# followed by a word, and stripping the leading "7 " would turn "7.1 Private Placement
# Regime" into "1 private placement regime".
_BARE_ORDINAL = re.compile(r"^\s*\d+\.?\s+(?=\S)")


def _fold_bare_ordinals(outline: list[dict]) -> dict[str, str]:
    """normalized title -> the key to match on. Identity except where dropping a leading
    bare ordinal is safe: the folded form must be unique across the outline, and must not
    collide with any entry's unfolded key."""
    titles = [h.get("title") or "" for h in outline]
    norms = [_norm_heading(t) for t in titles]
    taken = set(norms)
    counts: dict[str, int] = {}
    pairs = []
    for t, n in zip(titles, norms):
        stripped = _BARE_ORDINAL.sub("", t)
        f = _norm_heading(stripped) if stripped != t and stripped.strip() else n
        pairs.append((n, f))
        if f != n:
            counts[f] = counts.get(f, 0) + 1
    return {n: (f if f != n and counts.get(f) == 1 and f not in taken else n)
            for n, f in pairs}


def _tree_heading_titles(tree_root: Path) -> set[str]:
    out = set()
    for f in sorted(tree_root.rglob("*.md")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^#{1,6}\s+(.+)$", line.strip())
            if m:
                out.add(_norm_heading(m.group(1)))
    return out


# pdf2mdtree INVENTS this entry — it is not a heading the PDF prints. When paragraphs
# sit before the first recognized heading it inserts a synthetic level-1 "Front Matter"
# so that content is not dropped (see pdf2mdtree, `matched.insert(0, ...)`). Asking
# whether it "reaches a heading in the tree" is asking whether a later stage happened to
# keep a label that was never in the source, and the answer swings on nothing: the three
# ELTIF supplementals in the 16-09-26 corpus (Denmark 148204, Italy 181491, Sweden
# 138020) have 7-entry outlines, so this one phantom alone is 14 points of a coverage
# rate. Real losses only.
_SYNTHETIC_OUTLINE_TITLES = {"front matter"}


def heading_coverage(job_root: Path, from_stage: int, to_stage: int) -> dict:
    manifest_p = job_root / "01_stage1_extract" / "headings_manifest.json"
    try:
        outline = (json.loads(manifest_p.read_text()) or {}).get("headings") or []
    except (OSError, ValueError):
        return {"error": f"no {manifest_p.name}", "outline_count": 0}
    outline = [h for h in outline
               if (h.get("title") or "").strip().casefold() not in _SYNTHETIC_OUTLINE_TITLES]

    from_titles = _tree_heading_titles(resolve_stage_dir(job_root, from_stage))
    to_titles = _tree_heading_titles(resolve_stage_dir(job_root, to_stage))

    fold = _fold_bare_ordinals(outline)

    def missing_from(titles: set[str]) -> list[dict]:
        out = []
        for h in outline:
            n = _norm_heading(h["title"])
            if n not in titles and fold[n] not in titles:
                out.append(h)
        return out

    return {
        "from_stage": from_stage, "to_stage": to_stage,
        "outline_count": len(outline),
        "missing_at_from": missing_from(from_titles),
        "missing_at_to": missing_from(to_titles),
    }


# ---------------------------------------------------------------------------
# 3. Tables: row count vs MinerU's own count
#
# Keyed by PAGE RANGE, not table_id. Measured on Jersey/181919: table_id is
# not a stable identity across a tree at all -- badges for "table_011" and
# "table_012" each appear TWICE in 03_stage3_final, naming two genuinely
# different tables at different page ranges with different row counts. (The
# job's stage3_final holds more content files than the verified outline has
# level-1 headings -- 25 vs 16 -- which looks like a real duplication in how
# this document's tree was assembled, separate from anything stage 4/5 did;
# worth its own look.) A table's page range, from stage2_report.json -- written
# once, before either stage rewrote anything -- does not have this problem.
# ---------------------------------------------------------------------------

_BADGE_RE = re.compile(r"MinerU-extracted table\*\*\s*—\s*(table_\d+),\s*pages?\s*(\d+)(?:[–-](\d+))?")
_TABLE_BLOCK_RE = re.compile(r"<table>.*?</table>", re.S)
_TR_RE = re.compile(r"<tr[ >]")


def _table_row_counts(tree_root: Path) -> dict[tuple[int, int], dict]:
    """(first_page, last_page) -> {rows, ids_seen}, total <tr> count across
    every <table> block attributed to that page range, walked in document
    order across the whole tree.

    Not "whatever is in the file with the badge": subchunking can split one
    MinerU table's rows across several sibling files when a page-spanning
    table's continuation rows land in a later subsection chunk. Only the
    FIRST fragment carries the badge (measured on Jersey/181919's table at
    pages 39-55 -- a 106-row table whose rows are split across all 6 files of
    a subchunked section, with the badge naming it only in the first). So a
    table's rows are attributed to whichever badge most recently preceded
    them, walking every file in path order -- which recovers the truth for a
    table that stayed in one file too, since its own badge immediately
    precedes it."""
    out: dict[tuple[int, int], dict] = {}
    current: tuple[int, int] | None = None
    for f in sorted(tree_root.rglob("*.md"), key=lambda p: str(p.relative_to(tree_root))):
        text = f.read_text(encoding="utf-8", errors="replace")
        events = []
        for m in _BADGE_RE.finditer(text):
            first = int(m.group(2))
            last = int(m.group(3)) if m.group(3) else first
            events.append((m.start(), "badge", (m.group(1), (first, last))))
        events += [(m.start(), "table", m.group(0)) for m in _TABLE_BLOCK_RE.finditer(text)]
        events.sort(key=lambda e: e[0])
        for _, kind, val in events:
            if kind == "badge":
                tid, current = val
                out.setdefault(current, {"rows": 0, "ids_seen": set()})
                out[current]["ids_seen"].add(tid)
            elif current is not None:
                out[current]["rows"] += len(_TR_RE.findall(val))
    return out


def table_row_completeness(job_root: Path, from_stage: int, to_stage: int) -> dict:
    s2 = next(iter(sorted(job_root.glob("0*_*mineru*/stage2_report.json"))), None)
    if s2 is None:
        return {"error": "no stage2_report.json found", "tables": []}
    report = json.loads(s2.read_text())
    ground_truth = {}
    for t in report.get("tables", []):
        if not t.get("ok"):
            continue
        pages = t.get("pages") or []
        if not pages:
            continue
        ground_truth[(min(pages), max(pages))] = {"table_id": t["table_id"], "rows": t.get("rows")}

    from_rows = _table_row_counts(resolve_stage_dir(job_root, from_stage))
    to_rows = _table_row_counts(resolve_stage_dir(job_root, to_stage))

    rows = []
    for key, gt in sorted(ground_truth.items()):
        fr, to = from_rows.get(key), to_rows.get(key)
        rows.append({
            "table_id": gt["table_id"], "pages": key, "mineru_rows": gt["rows"],
            "rows_at_from": fr["rows"] if fr else None,
            "rows_at_to": to["rows"] if to else None,
            # A table entirely missing from a stage's tree is its own kind of
            # loss, distinct from a present table with the wrong row count --
            # the fidelity dimension already catches the first; this adds the
            # second, which a whole-table check cannot see.
            "missing_at_from": fr is None, "missing_at_to": to is None,
            "row_delta_to_vs_from": (None if fr is None or to is None
                                    else to["rows"] - fr["rows"]),
        })
    return {"from_stage": from_stage, "to_stage": to_stage,
           "mineru_table_count": len(ground_truth), "tables": rows}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_text_section(d: dict) -> None:
    print(f"\n=== TEXT: stage {d['from_stage']} -> stage {d['to_stage']} "
         f"({d['dropped_spans_at_from']} dropped spans -> {d['dropped_spans_at_to']}) ===")
    print(f"  recovered:     {len(d['recovered'])}  (missing at {d['from_stage']}, "
         f"present at {d['to_stage']})")
    print(f"  regressed:     {len(d['regressed'])}  (present at {d['from_stage']}, "
         f"missing at {d['to_stage']})")
    print(f"  still missing: {len(d['still_missing'])}  (missing at both)")
    for label, items in (("REGRESSED", d["regressed"]), ("RECOVERED", d["recovered"])):
        if not items:
            continue
        print(f"\n  -- {label} (largest first) --")
        for v in items[:10]:
            print(f"    [{v['token_count']:>3}tok] {v['file']}: {v['text'][:100]}"
                 + ("…" if len(v["text"]) > 100 else ""))
        if len(items) > 10:
            print(f"    ... and {len(items) - 10} more")


def _print_heading_section(d: dict) -> None:
    if d.get("error"):
        print(f"\n=== HEADINGS: {d['error']} — skipped ===")
        return
    print(f"\n=== HEADINGS: {d['outline_count']} in the source outline "
         f"(stage {d['from_stage']} -> stage {d['to_stage']}) ===")
    print(f"  missing at stage {d['from_stage']}: {len(d['missing_at_from'])}")
    for h in d["missing_at_from"]:
        print(f"    - {h['title']} (p{h['page']})")
    print(f"  missing at stage {d['to_stage']}: {len(d['missing_at_to'])}")
    for h in d["missing_at_to"]:
        print(f"    - {h['title']} (p{h['page']})")


def _print_table_section(d: dict) -> None:
    if d.get("error"):
        print(f"\n=== TABLES: {d['error']} — skipped ===")
        return
    print(f"\n=== TABLES: {d['mineru_table_count']} tables MinerU extracted "
         f"(stage {d['from_stage']} -> stage {d['to_stage']}) ===")
    bad = [t for t in d["tables"] if t["missing_at_from"] or t["missing_at_to"]
          or (t["row_delta_to_vs_from"] not in (None, 0))]
    if not bad:
        print("  every table present at both stages with an unchanged row count")
        return
    for t in bad:
        fr = "MISSING" if t["missing_at_from"] else t["rows_at_from"]
        to = "MISSING" if t["missing_at_to"] else t["rows_at_to"]
        flag = ""
        if t["row_delta_to_vs_from"]:
            flag = f"  ({'+' if t['row_delta_to_vs_from'] > 0 else ''}{t['row_delta_to_vs_from']} rows)"
        pages = f"p{t['pages'][0]}" if t["pages"][0] == t["pages"][1] else f"p{t['pages'][0]}-{t['pages'][1]}"
        print(f"    {t['table_id']} ({pages}): MinerU={t['mineru_rows']}  "
             f"stage{d['from_stage']}={fr}  stage{d['to_stage']}={to}{flag}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_root", help="a job dir, e.g. out/corpus/<product>/<label>")
    ap.add_argument("--from-stage", type=int, default=3)
    ap.add_argument("--to-stage", type=int, default=None,
                    help="defaults to the latest stage actually on disk (3, 4 or 5)")
    ap.add_argument("--json", action="store_true", help="machine-readable output only")
    args = ap.parse_args()

    job_root = Path(args.job_root).resolve()
    if not job_root.is_dir():
        print(f"not a directory: {job_root}")
        return 2
    to_stage = args.to_stage if args.to_stage is not None else final_stage(job_root)
    if to_stage == args.from_stage:
        print(f"stage {to_stage} is the only tree on disk -- nothing to compare")
        return 1

    result = {
        "job_root": str(job_root),
        "text": text_diff(job_root, args.from_stage, to_stage),
        "headings": heading_coverage(job_root, args.from_stage, to_stage),
        "tables": table_row_completeness(job_root, args.from_stage, to_stage),
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"comparing {job_root.name}: stage {args.from_stage} -> stage {to_stage}")
    _print_text_section(result["text"])
    _print_heading_section(result["headings"])
    _print_table_section(result["tables"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
