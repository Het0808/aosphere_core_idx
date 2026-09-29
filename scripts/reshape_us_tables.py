"""One-time migration: reduce the shared multi-state comparison tables in each
US-state content.json to that state's own row(s).

The US-state surveys embed the SAME ~37k-char all-states tables (A2.2, B2, ...) in
every state's doc — 28M+ duplicated chars across 56 docs. That one bad shape broke
per-state retrieval and forced runtime/prompt workarounds. This rewrites the stored
content so each state's clauses are genuinely about that state.

Uses the SAME reduction as section_index.answer_text (the embeddings were built with
it via `aci reembed`), so the existing .npz vectors stay valid — no re-embed needed.
Idempotent: already-small tables are left untouched.

  .venv/bin/python scripts/reshape_us_tables.py [--dry-run]
"""
import glob
import json
import sys

from aosphere_core_index.embeddings.section_index import _table_for_state, state_of

DRY = "--dry-run" in sys.argv


def main() -> None:
    paths = sorted(glob.glob("data/regions/*/artifacts/*.content.json"))
    total_before = total_after = 0
    changed_docs = 0
    for p in paths:
        jur = p.split("/")[2]
        state = state_of(jur)
        if not state:
            continue
        content = json.loads(open(p).read())
        doc_changed = False
        before = after = 0
        for s in content["sections"]:
            for e in s["elements"]:
                if e["kind"] != "table" or len(e["text"]) <= 2000:
                    continue
                reduced = _table_for_state(e["text"], state)
                before += len(e["text"])
                after += len(reduced)
                if reduced != e["text"]:
                    e["text"] = reduced
                    doc_changed = True
        total_before += before
        total_after += after
        if doc_changed:
            changed_docs += 1
            if not DRY:
                open(p, "w").write(json.dumps(content, separators=(",", ":")))
            print(f"  {jur:42} tables {before:>8,} -> {after:>7,} chars")
    print(f"\n{'DRY-RUN: ' if DRY else ''}reshaped {changed_docs} US-state docs | "
          f"big-table chars {total_before:,} -> {total_after:,} "
          f"(saved {total_before - total_after:,})")


if __name__ == "__main__":
    main()
