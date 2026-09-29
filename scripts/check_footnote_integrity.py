#!/usr/bin/env python3
"""check_footnote_integrity.py — do footnote references and bodies still match up?

A footnote only works if both halves survive extraction: the inline marker in the
prose, and the body at the bottom of the page. Losing either breaks the citation,
and the two failures have DIFFERENT causes, so both are reported separately:

  DANGLING REFERENCE   the prose cites [^n] but no body exists anywhere in the
                       tree. The reader cannot follow the citation. Either the
                       body was never extracted, or (seen on a real document) the
                       source PDF itself has a numbering gap.

  ORPHAN DEFINITION    a body [^n]: exists but nothing cites it. This is the more
                       interesting one: it means the INLINE MARKER was lost. Those
                       markers are ~5pt superscripts sitting against 10.6pt body
                       text, so they are the fragile half — and a dropped marker
                       is silent, because the footnote still appears at the bottom
                       and looks fine.

  UNRESOLVED BODY      the body is a placeholder ("footnote body not present in
                       source PDF"), i.e. Stage 1 looked and the source had
                       nothing. Reported as informational, not as our defect.

Resolution is WHOLE-TREE, not per file. pdf2mdtree emits a body in whichever
section the PDF happened to put it, so a reference in one file is routinely
defined in another — checking per file reports those as broken when they are not.
On the pilot document one page had 48 inline references and 24 local bodies, so a
per-file check would have called half of them dangling.

Usage:
    python scripts/check_footnote_integrity.py out/hybrid_extractions/_ui/<job_id>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (
    BOLD, DIM, FOOTNOTE_DEF_RE, FOOTNOTE_REF_RE, GREEN, RED, YELLOW,
    banner, iter_content_files, resolve_stage_dir, run_cli,
)

# A reference is any [^n] NOT followed by a colon (that would be a definition).
_DEF_RE = FOOTNOTE_DEF_RE
_REF_RE = FOOTNOTE_REF_RE
_UNRESOLVED = re.compile(r"footnote body not present", re.I)


# A document's footnotes are numbered by a human: single digits to a few hundred, and 1533
# is the largest plausible id anywhere in this corpus. Anything vastly bigger is not a
# footnote — it is a long number in the text that FOOTNOTE_REF_RE read as one ("[^11341434]"
# in Poland's I-changes-in-regulation.md). Left in, that single marker stretched the sequence
# from 1 to 11,341,434 and the gap enumeration below emitted 11,339,913 findings: a 4.9GB
# scorecard that OOM-killed the service when the Doc Library read it, and a 4.9GB upload that
# looked like a hung S3 push. The document itself was fine — 18 real content gaps.
_MAX_PLAUSIBLE_ID = 5000
# Defence in depth. Even within a plausible range a pathological document could hole through
# thousands of ids, and no verdict is improved by the ten-thousandth identical advisory. The
# count is kept so the summary can still say how many there were.
_MAX_GAPS_REPORTED = 200


def _sequence_gaps(ids: set[int], min_ids: int = 4) -> list[int]:
    """A document numbers its footnotes in strictly increasing order, so any
    integer between the smallest and largest OBSERVED id that never appears
    as either a reference or a body is a footnote that lost BOTH halves —
    the one failure mode dangling/orphan detection above cannot see, because
    neither leaves a trace to resolve.

    Requires at least `min_ids` observed ids before trusting the "sequential"
    assumption at all — with only a couple of footnotes, a gap is just as
    likely to mean the document numbers them non-consecutively (e.g. tied to
    clause numbers) as to mean one went missing."""
    ids = {i for i in ids if 0 < i <= _MAX_PLAUSIBLE_ID}
    if len(ids) < min_ids:
        return []
    lo, hi = min(ids), max(ids)
    gaps = [n for n in range(lo, hi + 1) if n not in ids]
    return gaps[:_MAX_GAPS_REPORTED]


def compute_footnote_integrity(out_root: Path, stage: int = 3) -> dict:
    tree_root = resolve_stage_dir(Path(out_root), stage)

    refs: dict[str, list[str]] = defaultdict(list)     # id -> files citing it
    defs: dict[str, list[str]] = defaultdict(list)     # id -> files defining it
    unresolved: dict[str, str] = {}
    body_by_id: dict[str, str] = {}    # id -> first body text seen, for the duplicate check below
    for f in iter_content_files(tree_root):
        rel = str(f.relative_to(tree_root))
        raw = f.read_text(encoding="utf-8")
        for m in _DEF_RE.finditer(raw):
            fid, body = m.group(1), m.group(2)
            defs[fid].append(rel)
            body_by_id.setdefault(fid, body.strip())
            if _UNRESOLVED.search(body):
                unresolved[fid] = rel
        for m in _REF_RE.finditer(raw):
            refs[m.group(1)].append(rel)

    # Two DIFFERENT footnote numbers sharing byte-identical body text is
    # WORTH A GLANCE, not proof of anything: it's the signature a running
    # footer/doc-control line leaves when it gets mistaken for a footnote
    # body (see pdf2mdtree.py's repeated_text / process_fn_lines) — but a
    # legal document also does this legitimately, citing the exact same
    # short provision ("Section 2 of the Capital Markets Law.") as two
    # unrelated footnotes. Nothing in the OUTPUT distinguishes the two cases
    # (that requires the source PDF's page layout, which this check never
    # sees), so this stays advisory and reported, never a `passed` failure.
    # Short bodies are excluded: "See above." legitimately repeats constantly
    # and would swamp this with noise.
    by_text: dict[str, list[str]] = defaultdict(list)
    for fid, body in body_by_id.items():
        if len(body) >= 15:
            by_text[body].append(fid)
    duplicate_bodies = [{"ids": sorted(ids, key=_sortkey), "text": text}
                        for text, ids in by_text.items() if len(ids) > 1]

    dangling = [{"id": k, "cited_in": sorted(set(v))[:4], "cite_count": len(v)}
                for k, v in sorted(refs.items(), key=lambda kv: _sortkey(kv[0]))
                if k not in defs]
    orphan_defs = [{"id": k, "defined_in": sorted(set(v))[:4]}
                   for k, v in sorted(defs.items(), key=lambda kv: _sortkey(kv[0]))
                   if k not in refs]
    # A reference resolved by a body in a DIFFERENT file is normal, not a defect —
    # counted only so the UI can avoid marking it broken.
    cross_file = sum(1 for k, v in refs.items()
                     if k in defs and not set(v) & set(defs[k]))

    numeric_ids = {int(k) for k in (set(refs) | set(defs)) if k.isdigit()}
    seq_gaps = _sequence_gaps(numeric_ids)

    return {
        "tree_root": str(tree_root),
        "refs_total": len(refs),
        "defs_total": len(defs),
        "dangling_refs": dangling,
        "orphan_definitions": orphan_defs,
        "unresolved_bodies": [{"id": k, "file": v} for k, v in
                              sorted(unresolved.items(), key=lambda kv: _sortkey(kv[0]))],
        "dangling_count": len(dangling),
        "orphan_definition_count": len(orphan_defs),
        "unresolved_count": len(unresolved),
        "cross_file_resolved": cross_file,
        # A footnote whose reference AND body are BOTH gone leaves no [^n]
        # anywhere for dangling/orphan detection to catch — only the hole it
        # leaves in the numbering can. Reported, not folded into `passed`:
        # it's an inference from numbering, not a directly observed defect,
        # so it stays advisory the same way check_scorecard treats Integrity.
        # the range the GAP CHECK trusted, not the raw min/max: reporting "footnotes here run
        # 1-11341434" alongside 13 gaps would be incoherent.
        "sequence_range": ([min(plausible), max(plausible)] if (
            plausible := {i for i in numeric_ids if 0 < i <= _MAX_PLAUSIBLE_ID}) else None),
        "sequence_ids_ignored": sorted(i for i in numeric_ids if i > _MAX_PLAUSIBLE_ID),
        "sequence_gaps": seq_gaps,
        "sequence_gap_count": len(seq_gaps),
        # See the note above by_text: real evidence of a bug in SOME cases,
        # a genuine repeated citation in others, and the output alone can't
        # tell which — so, like sequence_gaps, advisory only.
        "duplicate_bodies": duplicate_bodies,
        "duplicate_body_count": len(duplicate_bodies),
        # Both halves breaking is a real defect. An unresolved BODY is the source
        # document's gap, so it must not fail the check on its own.
        "passed": not dangling and not orphan_defs,
    }


def _sortkey(fid: str):
    return (0, int(fid)) if fid.isdigit() else (1, fid)


def print_report(report: dict, top: int = 15):
    print(f"{DIM('references:')} {report['refs_total']}    {DIM('bodies:')} {report['defs_total']}"
          f"    {DIM('resolved across files:')} {report['cross_file_resolved']}")
    d, o, u = (report["dangling_refs"], report["orphan_definitions"],
               report["unresolved_bodies"])
    if d:
        print(f"\n{RED(BOLD('DANGLING REFERENCES — cited in the prose, no body anywhere:'))}")
        for x in d[:top]:
            print(f"    [^{x['id']}]  cited {x['cite_count']}x  {DIM(', '.join(x['cited_in'])[:74])}")
    if o:
        print(f"\n{RED(BOLD('ORPHAN BODIES — a footnote exists but nothing cites it '))}"
              f"{DIM('(the ~5pt inline marker was lost)')}")
        for x in o[:top]:
            print(f"    [^{x['id']}]  {DIM(', '.join(x['defined_in'])[:74])}")
    if u:
        print(f"\n{YELLOW('unresolved bodies (the source PDF has no such footnote):')} "
              + ", ".join(f"[^{x['id']}]" for x in u[:12]))
    dup = report.get("duplicate_bodies") or []
    if dup:
        print(f"\n{YELLOW(BOLD('DUPLICATE BODIES — different footnote numbers, byte-identical text '))}"
              f"{DIM('(could be a running footer misread as a footnote, or just a repeated citation — worth a glance)')}")
        for x in dup[:top]:
            ids = ", ".join(f"[^{i}]" for i in x["ids"])
            print(f"    {ids}  {DIM(x['text'][:70])}")
    gaps = report.get("sequence_gaps") or []
    if gaps:
        lo, hi = report["sequence_range"]
        print(f"\n{YELLOW(BOLD(f'NUMBERING GAP — footnotes run {lo}-{hi}, but these never appear '))}"
              f"{YELLOW('as either a reference or a body anywhere:')}")
        print("    " + ", ".join(f"[^{n}]" for n in gaps[:top]))
    print()
    if report["passed"]:
        print(GREEN(BOLD("✓ PASS — every reference has a body and every body is cited")))
    else:
        print(RED(BOLD(f"✗ FAIL — {report['dangling_count']} dangling reference(s), "
                       f"{report['orphan_definition_count']} uncited body(ies)")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    banner("Footnote integrity · references vs bodies, both directions")
    report = compute_footnote_integrity(Path(args.out_root).resolve())
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
