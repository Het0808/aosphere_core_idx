#!/usr/bin/env python3
"""check_appendix_integrity.py — did every appendix the memo prints become its own chunk?

These memos do NOT head their appendix divisions with a heading. They print a bare
ALL-CAPS label on one line and the descriptive title on the next:

    **APPENDIX 3**
    **DISCLAIMERS: INVESTMENT MANAGEMENT & ADVISORY SERVICES**

Stage 1 splits on headings, so whether appendix 3 becomes a chunk depends entirely on
the bookmark outline (or the font heuristic) having independently found that TITLE
line. Usually it does and the boundary lands correctly by luck. Where it does not, the
label stays body text, the whole appendix is absorbed into the appendix above it, and
NOTHING downstream can tell: word coverage is unchanged (the text is still in the
tree), placement divides by headings that exist, and the heading census only knows the
sections the outline named — which is precisely the outline that missed this one.

Measured over the 54-document 16-09-26 corpus: Iceland and Malta each lost appendices
3 AND 4 into appendix 2, Kazakhstan lost 4, Philippines lost 5 into appendix 4, and
Singapore lost 4 into appendix 3 — where stage 4 then deleted its ~8KB outright.
Every one of those five documents gated PASS, at 93.8-96.7.

WHAT COUNTS AS A MISSED DIVISION

A label is `owned` when it opens a section: carried in that file's H1, or standing in
its opening before any body text. It is `buried` when real prose follows it INSIDE the
same file. Only a label that is buried and owned nowhere is reported.

Three discriminations do the work, each measured against the corpus rather than assumed:

  ALL-CAPS ONLY        "APPENDIX 3" is the memo's own division; "Appendix 3" is a
                       reference to one. Malaysia's appendix 5 quotes the Malaysian
                       SC's "Foreign Funds Guidelines" verbatim, title-case Appendix
                       1-4 headings and all — four false positives that vanish when
                       the match is case-sensitive.

  PROSE MUST FOLLOW    A label with nothing after it is the ORPHAN case, not a missed
                       division: the label fell one paragraph short and sits at the
                       end of the section BEFORE the one it names, which is what
                       pdf2mdtree's --group-labels pass exists to move. The appendix
                       itself is fine. Requiring prose after the label took the flag
                       rate over every tree on disk from 116/350 to 23/350.

  ARTEFACTS DON'T      a footnote body, a horizontal rule or an image link trailing an
  COUNT AS PROSE       orphaned label is not the appendix's content. Uruguay's
                       "APPENDIX 2" is followed by `[^26]: Restatement on Rules of
                       the Securities Market...` and by nothing else.

Product-scoped (product_rules.APPENDIX_CHUNK_PRODUCTS): the convention above is 124's.
Any other product scores None and passes, the same way `stage4` is None for the
documents that never run it.

Read off STAGE 3, not the final tree, on purpose. Stage 4's prompt tells the model to
fold an orphaned "APPENDIX N" into the heading and delete the line; Russian Federation
89570 shows it deleting the label without folding it, so the evidence only survives in
the deterministic tree. The defect is created upstream of stage 4 in any case.

Usage:
    python scripts/check_appendix_integrity.py out/corpus/<product>/<job>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (  # noqa: E402
    BOLD, DIM, GREEN, RED, banner, iter_content_files, resolve_stage_dir, run_cli,
)
from product_rules import appendix_chunk_checked, product_for_job  # noqa: E402

# Case-SENSITIVE by construction — see ALL-CAPS ONLY above. Written out rather than
# built with `re.I` so the intent survives anyone tidying the flags.
_WORDS = (r"APPENDICES|APPENDIX|ANNEXURES|ANNEXURE|ANNEXES|ANNEXE|ANNEX"
          r"|SCHEDULES|SCHEDULE|EXHIBITS|EXHIBIT")
# The label ALONE on its line, bold or bare, with optional trailing punctuation. The
# same "alone on the line" test pdf2mdtree._GROUP_LABEL_RE uses, and for the same
# reason: it is what separates a division from a cross-reference ("see Appendix 2.A").
_LABEL = re.compile(rf"^\s*(?:\*\*|__)?\s*({_WORDS})\s+(\d{{1,3}})"
                    r"\s*[:.\-–—]?\s*(?:\*\*|__)?\s*$")
# The same label carried in the section's own H1, which is where stage 4 folds it to.
# Case-insensitive here: an H1 is already established as a division, so the title-case
# false positives the body match has to exclude cannot arise.
_H1_LABEL = re.compile(rf"^#\s+(?:\*\*)?\s*({_WORDS})\s+(\d{{1,3}})\b", re.I)
_CRUMB = re.compile(r"^\*Source:")
_FOOTNOTE_DEF = re.compile(r"^\[\^[^\]]+\]:")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_IMAGE = re.compile(r"^\s*!\[")

# Prose after the label before it counts as a swallowed division. 80 sits in the middle
# of a flat region — the flag count over every tree on disk is identical at 40, 80 and
# 120 — and below the 221 characters of Kazakhstan's appendix 4, the smallest real one
# in the corpus. The cliff is at 0 (116 flags), which is the orphan case flooding in.
MIN_BODY_CHARS = 80


def _label_key(word: str, num: str) -> str:
    """'app3' / 'sch2' — the family's first three letters plus the number.

    Keyed by family, not just number, because a document can print both an APPENDIX 1
    and a SCHEDULE 1 (Guernsey 174507 does) and they are different divisions.
    """
    return f"{word.lower()[:3]}{int(num)}"


def _prose_chars(line: str) -> int:
    """Characters of real section prose on this line; 0 for structure and artefacts."""
    s = line.strip()
    if (not s or s.startswith("#") or _CRUMB.match(s) or _FOOTNOTE_DEF.match(s)
            or _RULE.match(line) or _IMAGE.match(line)):
        return 0
    return len(re.sub(r"[*_#>`|-]", " ", re.sub(r"<[^>]+>", " ", s)).strip())


def scan_file(text: str) -> tuple[set[str], list[tuple[str, int]]]:
    """(labels this file OWNS, [(label, prose chars after it)] it BURIES)."""
    owned: set[str] = set()
    buried: list[tuple[str, int]] = []
    lines = text.split("\n")
    m = _H1_LABEL.match(lines[0]) if lines else None
    if m:
        owned.add(_label_key(m.group(1), m.group(2)))
    body_started = False
    pending: list | None = None          # [label, prose chars seen since]
    for line in lines:
        lm = _LABEL.match(line)
        if lm:
            if pending and pending[1] >= MIN_BODY_CHARS:
                buried.append((pending[0], pending[1]))
            key = _label_key(lm.group(1), lm.group(2))
            if body_started:
                pending = [key, 0]
            else:
                owned.add(key)           # still in the section's opening
                pending = None
            continue
        n = _prose_chars(line)
        if not n:
            continue
        body_started = True
        if pending:
            pending[1] += n
    if pending and pending[1] >= MIN_BODY_CHARS:
        buried.append((pending[0], pending[1]))
    return owned, buried


def compute_appendix_integrity(out_root: Path, stage: int = 3) -> dict:
    out_root = Path(out_root)
    product = product_for_job(out_root)
    if not appendix_chunk_checked(product):
        # Not this product's convention. None rather than a pass, so the dimension
        # reports "not applicable" instead of clean evidence it never gathered.
        return {"available": False, "passed": True, "score": None,
                "product": product, "labels_total": 0, "swallowed": [],
                "swallowed_count": 0,
                "reason": f"product {product!r} is not in APPENDIX_CHUNK_PRODUCTS"}
    tree_root = resolve_stage_dir(out_root, stage)
    owned: set[str] = set()
    buried: dict[str, dict] = {}
    for p in iter_content_files(tree_root):
        o, b = scan_file(p.read_text(encoding="utf-8", errors="replace"))
        owned |= o
        for key, chars in b:
            buried.setdefault(key, {"label": key, "absorbed_into": str(p.relative_to(tree_root)),
                                    "prose_chars": chars})
    swallowed = [v for k, v in sorted(buried.items()) if k not in owned]
    total = len(owned) + len(swallowed)
    return {
        "available": True,
        "tree_root": str(tree_root),
        "product": product,
        "labels_total": total,
        "labels_own_chunk": len(owned),
        "swallowed": swallowed,
        "swallowed_count": len(swallowed),
        # Share of the document's printed divisions that became chunks. 100 when the
        # document prints none, which is the honest answer: nothing was missed.
        "score": round(100.0 * len(owned) / total, 1) if total else 100.0,
        "passed": not swallowed,
    }


def print_report(report: dict) -> None:
    if not report.get("available"):
        print(DIM(f"skipped — {report.get('reason')}"))
        return
    print(f"{DIM('divisions printed:')} {report['labels_total']}"
          f"    {DIM('with a chunk of their own:')} {report['labels_own_chunk']}")
    for s in report["swallowed"]:
        print(f"\n{RED(BOLD('SWALLOWED — ' + s['label'].upper() + ' never became a chunk'))}")
        print(f"    its {s['prose_chars']} character(s) of content are inside "
              f"{DIM(s['absorbed_into'])}")
    print()
    if report["passed"]:
        print(GREEN(BOLD("✓ PASS — every printed appendix division has its own chunk")))
    else:
        print(RED(BOLD(f"✗ FAIL — {report['swallowed_count']} division(s) absorbed "
                       "into the section above")))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--stage", type=int, default=3)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    banner("Appendix integrity · printed divisions vs chunks")
    report = compute_appendix_integrity(Path(args.out_root).resolve(), stage=args.stage)
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
