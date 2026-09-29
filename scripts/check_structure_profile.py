#!/usr/bin/env python3
"""check_structure_profile.py — how is this document SHAPED?

Every other check asks "is the content correct?". This one asks "is the
document's structure plausible?" — and it is the only one that can be answered
by comparing a document to its SIBLINGS rather than to its own source PDF.

Jurisdictions inside one product are written to a shared template: the same
questionnaire, the same section numbering, the same headings. Their extracted
shape should therefore be similar too. When one jurisdiction's chunks are 50x
the size of its siblings', nothing is "missing" in the word-coverage sense —
every word is present — but the heading detection collapsed and the whole
document arrived as one undifferentiated blob. Coverage-based checks are
structurally blind to that: they compare the tree against the PDF, and all the
words ARE in the tree.

This module computes ONE DOCUMENT'S profile only. The cohort comparison
(jurisdiction vs its product's median) is deliberately NOT stored here — see
the note in the dashboard's structure panel: a stored cohort statistic goes
stale the moment a sibling is re-extracted, silently, with nothing to indicate
it. The dashboard recomputes it from whatever siblings exist at view time.

The single most diagnostic number is `concentration_pct`: the share of the
document's words sitting in its one largest chunk. A well-structured document
spreads content across many sections, so this is low. When heading detection
fails there is only one chunk, and it is 100%.

Usage:
    python scripts/check_structure_profile.py out/corpus/<product>/<job>
"""
from __future__ import annotations

import argparse
import json
import re
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (
    BOLD, DIM, GREEN, RED, YELLOW, banner, clean_markdown, iter_content_files,
    resolve_stage_dir, run_cli,
)

# A chunk this small is a fragment, not a section — a heading with nothing under
# it, or a stray line promoted to its own file by over-eager splitting.
TINY_CHUNK_WORDS = 20

# ABSOLUTE red flags, checked without reference to any sibling. The cohort
# comparison finds a document that is unlike its peers; these find one that is
# wrong on its own terms, which matters because a whole product can be broken
# the same way — measured on this corpus, all six documents of
# 160_Bank_Confidentiality_&_Outsourcing extract as a single ~15k-word chunk, so
# not one of them is an outlier relative to the others.
# THE page threshold for the whole pipeline. Three separate constants used to encode
# the same decision (when is a document long enough to need sections?) and drifted
# apart: the MinerU tier split at 8, the structure test judged from 8, and routing
# used 10 — so an 8-9 page document was emitted as ONE chunk and then failed for
# being one chunk. It is one decision, so it is one number. Every other module
# imports this rather than restating it.
BLOB_MIN_PAGES = 10       # a document at least this long ...
BLOB_MAX_CHUNKS = 2       # ... arriving as this few chunks has no structure
BLOB_CONCENTRATION = 80.0  # or with this share of its words in one chunk
SHRED_MEAN_WORDS = 40      # mean chunk this small = shredded into fragments


# ---- the ACCEPT test -------------------------------------------------------
# "Did this document actually divide into usable sections?" — asked identically
# after every tier, so a tier is judged against a bar rather than against whatever
# came before it. Being merely BETTER than a broken result is not the same as being
# good, and adopting on 'better' let 0.0 -> 15.4 count as a success.
#
# A high concentration alone does not mean broken. A legal memo whose Schedule 1 is
# half the document is lopsided AND correct. What separates the two is whether real
# sections exist alongside the big one: a genuine schedule sits among many other
# chunks with substance, a broken blob sits alone among stubs.
LOPSIDED_MIN_CHUNKS = 3        # more than this many chunks ...
LOPSIDED_MIN_SECOND = 150      # ... and a second-largest chunk with real content
# ... and the OTHER chunks must be real sections, not stubs. Without this the
# exemption passed exactly what it was meant to exclude: 124_Marketing_Restrictions
# /Argentina is 88 pages in 5 chunks — 98% of its words in one file and 3 of the
# other 4 under 20 words — and it cleared the test because its second chunk happened
# to hold 770 words. "A big schedule BESIDE real sections" is only true when the
# sections beside it are real; a giant blob flanked by stubs is the blob case.
LOPSIDED_MAX_TINY_SHARE = 0.5
ACCEPT_COMPLETENESS = 90.0     # words present, measured against the document


def structure_is_acceptable(prof: dict) -> tuple[bool, str]:
    """Is this tree's SHAPE usable? -> (ok, human-readable reason).

    Deliberately not a score: the chain needs a yes/no it can apply after every
    tier, and a score invites 'a bit better' to look like 'good enough'."""
    pages = prof.get("pages") or 0
    chunks = prof.get("chunks") or 0
    conc = prof.get("concentration_pct") or 0.0
    second = prof.get("second_chunk_words") or 0
    if not chunks:
        return False, "no content files at all"
    if pages < BLOB_MIN_PAGES:
        return True, f"{pages}-page document \u2014 short enough that one chunk is legitimate"
    if conc < BLOB_CONCENTRATION:
        return True, f"well divided \u2014 largest chunk holds {conc:.0f}% of the words"
    tiny = prof.get("tiny_chunks") or 0
    tiny_share = (tiny / chunks) if chunks else 0.0
    if (chunks > LOPSIDED_MIN_CHUNKS and second >= LOPSIDED_MIN_SECOND
            and tiny_share <= LOPSIDED_MAX_TINY_SHARE):
        return True, (f"lopsided but valid \u2014 {conc:.0f}% sits in one chunk, but {chunks} chunks "
                      f"exist and the second largest still holds {second} words, so this is a "
                      f"large schedule beside real sections, not a blob")
    if conc >= BLOB_CONCENTRATION and tiny_share > LOPSIDED_MAX_TINY_SHARE:
        return False, (f"CONTENT ALL IN ONE CHUNK \u2014 {conc:.0f}% of this document's words sit in a "
                       f"single chunk, and {tiny} of {chunks} chunks are under "
                       f"{TINY_CHUNK_WORDS} words. The rest of the tree is structure without content.")
    return False, (f"CONTENT ALL IN ONE CHUNK \u2014 {conc:.0f}% of the words are in one chunk, across "
                   f"only {chunks} chunk(s), and the second largest holds {second} words")


# Upper bound of each bucket; the last is open-ended. Log-ish spacing because a
# 20-word stub and a 46,000-word blob are both real outcomes in this corpus.
SIZE_BUCKETS = (20, 50, 100, 250, 500, 1000, 2500, 5000)


def _bucket(sizes: list[int]) -> list[dict]:
    out = []
    lo = 0
    for hi in SIZE_BUCKETS:
        out.append({"lo": lo, "hi": hi, "n": sum(1 for s in sizes if lo <= s < hi)})
        lo = hi
    out.append({"lo": lo, "hi": None, "n": sum(1 for s in sizes if s >= lo)})
    return out


def _percentiles(sizes: list[int]) -> dict:
    if not sizes:
        return {}
    s = sorted(sizes)
    def at(p):
        return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]
    return {"p10": at(10), "p25": at(25), "p50": at(50), "p75": at(75), "p90": at(90)}


def _heading_levels(raw: str) -> list[int]:
    """ATX heading depths in one file. The tree writes '# Title' at the top of
    every section file and nests deeper headings under it, so this reflects the
    hierarchy the extractor actually produced."""
    return [len(m.group(1)) for m in re.finditer(r"^(#{1,6})\s+\S", raw, re.MULTILINE)]


def compute_structure_profile(out_root: Path, stage: int = 3) -> dict:
    tree_root = resolve_stage_dir(Path(out_root), stage)

    sizes: list[int] = []
    files: list[dict] = []
    levels: dict[int, int] = {}
    # The tree expresses its hierarchy as DIRECTORIES (a section becomes a folder
    # holding its subsections), not as heading depth inside one file — every leaf
    # file starts at '# '. So nesting has to be read off the path, or max_depth is
    # always 1 and says nothing.
    depths: dict[int, int] = {}
    for f in iter_content_files(tree_root):
        raw = f.read_text(encoding="utf-8", errors="replace")
        n = len(clean_markdown(raw).split())
        for lv in _heading_levels(raw):
            levels[lv] = levels.get(lv, 0) + 1
        if n:
            rel = f.relative_to(tree_root)
            d = len(rel.parts)          # 1 = top level, 2 = one folder deep, ...
            depths[d] = depths.get(d, 0) + 1
            sizes.append(n)
            files.append({"file": str(rel), "words": n, "depth": d})

    # Page count from Stage 1's own report — the extractor's view of the source,
    # so this stays right even when a stage dir is copied around.
    pages = None
    for cand in (Path(out_root) / "01_stage1_extract" / "stage1_report.json",
                 tree_root / "stage1_report.json"):
        if cand.exists():
            try:
                pages = json.loads(cand.read_text()).get("pages")
                break
            except (OSError, json.JSONDecodeError):
                pass

    total = sum(sizes)
    biggest = max(sizes) if sizes else 0
    files.sort(key=lambda x: -x["words"])
    prof = {
        "tree_root": str(tree_root),
        "pages": pages,
        "chunks": len(sizes),
        "total_words": total,
        "mean_chunk_words": round(st.mean(sizes)) if sizes else 0,
        "median_chunk_words": round(st.median(sizes)) if sizes else 0,
        "max_chunk_words": biggest,
        # The lopsided test turns on this: a genuine giant schedule sits beside
        # other real sections, a broken blob sits beside stubs.
        "second_chunk_words": (sorted(sizes, reverse=True)[1] if len(sizes) > 1 else 0),
        "min_chunk_words": min(sizes) if sizes else 0,
        # The headline signal: one chunk holding most of the document means the
        # section structure never materialised.
        "concentration_pct": round(100.0 * biggest / total, 1) if total else 0.0,
        "tiny_chunks": sum(1 for n in sizes if n < TINY_CHUNK_WORDS),
        "chunks_per_page": round(len(sizes) / pages, 2) if pages else None,
        "words_per_page": round(total / pages) if pages else None,
        "heading_levels": {str(k): v for k, v in sorted(levels.items())},
        "headings_total": sum(levels.values()),
        # Nesting, read off the directory tree (see the note where `depths` is
        # built). depth_histogram is the shape of the hierarchy: a template-built
        # product should show the same profile across its jurisdictions.
        "max_depth": max(depths) if depths else 0,
        "depth_histogram": {str(k): v for k, v in sorted(depths.items())},
        # Chunk-size DISTRIBUTION, bucketed rather than raw: a 476-chunk document
        # would otherwise bloat every scorecard with a list nothing reads. Buckets
        # are roughly log-spaced because the interesting range spans three orders
        # of magnitude (a 20-word stub and a 46,000-word blob are both real).
        # Lets the dashboard draw the shape — an even spread vs everything piled
        # in one bucket — which is the difference the numbers alone hide.
        "size_buckets": _bucket(sizes),
        "percentiles": _percentiles(sizes),
        "largest_chunks": files[:5],
    }

    flags = []
    if prof["chunks"] and pages and pages >= BLOB_MIN_PAGES and prof["chunks"] <= BLOB_MAX_CHUNKS:
        flags.append({
            "kind": "no_structure",
            "detail": f"{pages}-page document extracted as only {prof['chunks']} chunk(s) — "
                      "heading detection produced no usable section structure",
        })
    elif prof["concentration_pct"] >= BLOB_CONCENTRATION and prof["chunks"] > 1:
        flags.append({
            "kind": "concentrated",
            "detail": f"{prof['concentration_pct']:.0f}% of this document's words sit in a single "
                      f"chunk ({files[0]['file'] if files else '?'}) — the rest of the tree is "
                      "structure without content",
        })
    if sizes and prof["mean_chunk_words"] < SHRED_MEAN_WORDS and prof["chunks"] > 10:
        flags.append({
            "kind": "shredded",
            "detail": f"mean chunk is only {prof['mean_chunk_words']} words across "
                      f"{prof['chunks']} chunks — the document was split far finer than its "
                      "own headings",
        })
    if prof["tiny_chunks"] and prof["chunks"] and prof["tiny_chunks"] / prof["chunks"] > 0.5:
        flags.append({
            "kind": "many_tiny",
            "detail": f"{prof['tiny_chunks']} of {prof['chunks']} chunks are under "
                      f"{TINY_CHUNK_WORDS} words — mostly empty sections",
        })
    prof["flags"] = flags
    # Advisory by construction: this check describes shape, and an unusual shape
    # is a reason to look, not proof of a defect. Only the absolute flags above
    # can fail it, never a cohort comparison (which this module never sees).
    prof["passed"] = not flags
    return prof


def print_report(report: dict) -> None:
    p = report
    print(f"{DIM('tree :')} {p['tree_root']}")
    print(f"\n{BOLD('pages')}            : {p['pages']}")
    print(f"{BOLD('chunks')}           : {p['chunks']}"
          + (f"   ({p['chunks_per_page']}/page)" if p["chunks_per_page"] else ""))
    print(f"{BOLD('words')}            : {p['total_words']}")
    print(f"{BOLD('chunk words')}      : mean {p['mean_chunk_words']}, median "
          f"{p['median_chunk_words']}, max {p['max_chunk_words']}, min {p['min_chunk_words']}")
    conc = p["concentration_pct"]
    print(f"{BOLD('concentration')}    : "
          + (RED if conc >= BLOB_CONCENTRATION else GREEN)(f"{conc}%")
          + DIM("  (share of all words in the single largest chunk)"))
    print(f"{BOLD('tiny chunks')}      : {p['tiny_chunks']}  {DIM(f'(< {TINY_CHUNK_WORDS} words)')}")
    print(f"{BOLD('headings')}         : {p['headings_total']} total")
    print(f"{BOLD('nesting')}          : max depth {p['max_depth']}"
          f"   {DIM('files per depth: ' + str(p['depth_histogram']))}")
    if p["largest_chunks"]:
        print(f"\n{DIM('largest chunks:')}")
        for c in p["largest_chunks"]:
            print(f"    {c['words']:>7}w  {DIM(c['file'][:70])}")
    if p["flags"]:
        print(f"\n{RED(BOLD('STRUCTURE FLAGS:'))}")
        for f in p["flags"]:
            print(f"    [{f['kind']}] {f['detail']}")
    print()
    print(GREEN(BOLD("✓ PASS — document shape looks plausible")) if p["passed"]
          else RED(BOLD(f"✗ FAIL — {len(p['flags'])} structural flag(s)")))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    banner("Structure profile · document shape")
    report = compute_structure_profile(Path(args.out_root).resolve())
    print_report(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
