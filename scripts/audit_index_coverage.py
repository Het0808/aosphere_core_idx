#!/usr/bin/env python3
"""audit_index_coverage — what of the extracted text never reaches the index?

Every .md file in a stage-3 tree is content someone extracted on purpose. The converter
turns a file into a section only if it can derive a KEY for it, and everything else is
dropped silently — no error, no warning, and no way to notice except a jurisdiction going
missing or a lawyer asking why a glossary is unsearchable. Both happened.

So this asks the question directly, per document: which files got a key, which did not, and
why. It imports the converter's own derive_key/extra_key, so it cannot drift from the
behaviour it is auditing.

    scripts/audit_index_coverage.py                       # whole corpus, summary
    scripts/audit_index_coverage.py --detail 155_Data_Privacy/Qatar\\ \\(Data\\ Privacy\\)__172654
    scripts/audit_index_coverage.py --product 104_Shareholding_Disclosure --top 20

Reasons a file is dropped:
  instructions  the whole document is the survey's own instructions, not a jurisdiction's
                answers — deliberately not indexed
  no-key        the path yields neither a clause key nor an extras key — the document's
                tree carries no A-K part letters at all (its outline had none), so keys
                cannot be derived faithfully
  navigation    README / index / contents — deliberate, it holds no content of its own
  report        the pipeline's own CONVERSION_REPORT / STAGE3_REPORT
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tree_to_content as T  # noqa: E402  — the converter IS the specification

REPO = HERE.parent
REPORTS = {"README.md", "STAGE3_REPORT.md", "CONVERSION_REPORT.md"}
_NAV = re.compile(r"^(readme|index|contents|table-of-contents)$", re.I)

# 71 documents in the corpus are not surveys at all: they are the survey's own INSTRUCTIONS
# ("This Survey is intended to provide details of… Your responses should reflect all laws"),
# filed under a jurisdiction like any other document. Their 152,833 words must NOT be indexed
# — a question about Angola answered with survey admin text is worse than no answer — so
# counting them as loss overstated the problem by more than half. Recognised by their own
# section names, which are the same in every one of them.
_INSTRUCTION_SECTIONS = {"scope", "content", "how-to-respond-to-questions-in-this-survey",
                         "how-to-respond", "sources", "sample-responses", "instructions",
                         "checklist-for-completion-of-survey"}


def is_instructions_tree(tree: Path) -> bool:
    """True when every top-level section of the tree is survey front matter."""
    import part_lexicon as L
    tops = [p.name for p in tree.iterdir() if p.is_dir() and not p.name.startswith("_")]
    names = {L.norm(t) for t in tops} or {L.norm(p.stem) for p in tree.glob("*.md")}
    return bool(names) and names <= _INSTRUCTION_SECTIONS


def audit_tree(tree: Path) -> dict:
    """Per-file verdict for one stage-3 tree.

    Mirrors the converter exactly, INCLUDING the part-letter inference for outlines that
    carry none — otherwise this reports as lost the content that build() now keys."""
    kept, dropped = [], []
    extra_seen: dict = {}
    instructions = is_instructions_tree(tree)
    wrapper, letters = T.infer_structure(str(tree))
    for p in sorted(tree.rglob("*.md")):
        rel = os.path.relpath(p, tree)
        words = len(p.read_text(encoding="utf-8", errors="ignore").split())
        if p.name in REPORTS:
            dropped.append((rel, words, "report"))
            continue
        if instructions:
            dropped.append((rel, words, "instructions"))
            continue
        canon = T.canonical_rel(rel, wrapper, letters) if (wrapper or letters) else rel
        key = T.derive_key(canon) or T.extra_key(rel, extra_seen)
        if key:
            kept.append((rel, words, key))
        else:
            stem = os.path.splitext(p.name)[0]
            dropped.append((rel, words, "navigation" if _NAV.match(stem) else "no-key"))
    return {"kept": kept, "dropped": dropped,
            "kept_words": sum(w for _, w, _ in kept),
            "lost_words": sum(w for _, w, r in dropped if r == "no-key"),
            "instructions": instructions}


def trees(product: str | None) -> list[Path]:
    root = REPO / "out" / "corpus"
    pat = f"{product}/*/03_stage3_final" if product else "*/*/03_stage3_final"
    return sorted(root.glob(pat))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--product", default=None, help="limit to one product folder")
    ap.add_argument("--detail", default=None, help="one job (<product>/<job>) — list its files")
    ap.add_argument("--top", type=int, default=10, help="worst N documents to list")
    args = ap.parse_args()

    if args.detail:
        tree = REPO / "out" / "corpus" / args.detail / "03_stage3_final"
        r = audit_tree(tree)
        print(f"{args.detail}\n  kept {len(r['kept'])} file(s) / {r['kept_words']:,} words")
        for rel, w, why in r["dropped"]:
            print(f"  DROPPED [{why:10s}] {w:6,d}w  {rel}")
        return

    rows, totals = [], {"kept": 0, "lost": 0, "nav": 0, "report": 0, "instructions": 0}
    for tree in trees(args.product):
        r = audit_tree(tree)
        job = f"{tree.parent.parent.name}/{tree.parent.name}"
        rows.append((r["lost_words"], r["kept_words"], len(r["dropped"]), job))
        totals["kept"] += r["kept_words"]
        totals["lost"] += r["lost_words"]
        totals["instructions"] += sum(w for _, w, why in r["dropped"] if why == "instructions")
        totals["nav"] += sum(w for _, w, why in r["dropped"] if why == "navigation")
        totals["report"] += sum(w for _, w, why in r["dropped"] if why == "report")

    total_text = totals["kept"] + totals["lost"]
    print(f"{len(rows)} document(s) audited")
    print(f"  indexed          {totals['kept']:10,d} words")
    print(f"  DROPPED (no key) {totals['lost']:10,d} words  "
          f"({100 * totals['lost'] / max(1, total_text):.1f}% of extracted text)")
    print(f"  survey instructions {totals['instructions']:7,d} words  "
          f"(deliberate — not jurisdiction content)")
    print(f"  navigation       {totals['nav']:10,d} words  (deliberate)")
    print(f"  pipeline reports {totals['report']:10,d} words  (deliberate)")
    lost = [r for r in rows if r[0] > 0]
    print(f"\n{len(lost)} document(s) lose content; worst {min(args.top, len(lost))}:")
    for lost_w, kept_w, ndrop, job in sorted(lost, reverse=True)[:args.top]:
        share = 100 * lost_w / max(1, lost_w + kept_w)
        print(f"  {lost_w:8,d}w lost ({share:5.1f}% of the doc)  kept {kept_w:7,d}w  {job[:56]}")


if __name__ == "__main__":
    main()
