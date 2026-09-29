"""Stage a fresh RAG data drop into data/regions/<region>/source/.

The drop lays out one folder per jurisdiction, named like "France (Data Privacy)",
each holding the DOCX + Doc_metadata.json + RAG_rated_answers.json (no alerts).
We normalise the folder name to our canonical region name (strip the
" (Data Privacy)" suffix + a few aliases), then copy the docx + the two JSON
sidecars into data/regions/<region>/source/, PRESERVING any existing
Survey_Alerts*.json (the drop has none, and alerts still map against the new
index). Dropped regions (per user decision) are moved aside, not deleted.

  .venv/bin/python scripts/import_fresh_data.py --dry-run
  .venv/bin/python scripts/import_fresh_data.py --apply
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

DROP = Path("RAG-json_docx_v1_2026-06-30/155_Data_Privacy/2026-06-30")
REGIONS = Path("data/regions")

# folder-name (after stripping " (Data Privacy)") -> canonical region name
ALIAS = {
    "EU Member States": "European Union",
    "Hong Kong SAR": "Hong Kong",
    "Türkiye": "Turkey",
}
# Regions to retire (replaced by a fresh-data equivalent, per user decision).
RETIRE = ["Niger", "Qatar Financial Centre"]

SIDE = ("Doc_metadata.json", "RAG_rated_answers.json")
DOC_EXT = (".docx",)


def canonical(folder: str) -> str:
    base = re.sub(r"\s*\(Data Privacy\)$", "", folder).strip()
    return ALIAS.get(base, base)


def main() -> None:
    apply = "--apply" in sys.argv
    if not DROP.exists():
        sys.exit(f"drop folder not found: {DROP}")
    folders = sorted(f for f in os.listdir(DROP) if (DROP / f).is_dir())
    existing = {p.name for p in REGIONS.iterdir() if p.is_dir()}

    plan, problems = [], []
    for f in folders:
        region = canonical(f)
        src = DROP / f
        docx = sorted([p for p in src.iterdir() if p.suffix.lower() in DOC_EXT])
        meta = [s for s in SIDE if (src / s).exists()]
        if not docx:
            problems.append(f"{f}: NO docx")
        if "Doc_metadata.json" not in meta or "RAG_rated_answers.json" not in meta:
            problems.append(f"{f}: missing {set(SIDE) - set(meta)}")
        plan.append((f, region, [d.name for d in docx], region in existing))

    print(f"drop folders: {len(folders)}  |  will map to {len({r for _,r,_,_ in plan})} regions\n")
    new = [(f, r) for f, r, _, e in plan if not e]
    print(f"NEW regions (no current dir): {[r for _,r in new] or 'none'}")
    print(f"RETIRE (move aside): {RETIRE}\n")
    if problems:
        print("!! DATA PROBLEMS:")
        for p in problems:
            print("  ", p)
        print()

    if not apply:
        for f, r, docs, e in plan:
            print(f"  {'(new) ' if not e else '      '}{f!r:46} -> {r!r:40} {docs}")
        print("\nDRY RUN — re-run with --apply to stage.")
        return

    for f, region, docs, _ in plan:
        src = DROP / f
        dest = REGIONS / region / "source"
        dest.mkdir(parents=True, exist_ok=True)
        # clear stale docx/pdf + the two sidecars we refresh; KEEP Survey_Alerts*.
        for old in dest.iterdir():
            if old.suffix.lower() in (".docx", ".pdf") or old.name in SIDE:
                old.unlink()
        for p in src.iterdir():
            if p.suffix.lower() in DOC_EXT or p.name in SIDE:
                shutil.copy2(p, dest / p.name)
    for r in RETIRE:
        d = REGIONS / r
        if d.exists():
            shutil.move(str(d), str(d) + ".retired")
            print(f"  retired: {r} -> {r}.retired")
    print(f"\nStaged {len(plan)} regions. Fresh source is in data/regions/<region>/source/.")


if __name__ == "__main__":
    main()
