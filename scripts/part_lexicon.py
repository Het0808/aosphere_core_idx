#!/usr/bin/env python3
"""Which part letter does a folder name mean? Learned from the trees that already say.

A survey's clause keys start with a part letter — A1.1, H1.4 — and the converter reads that
letter off the top-level folder (`A-substantial-shareholding`). Some documents' outlines carry
no letters at all: the tree is `02-iceland/01-background-to-tda-disclosure-rules/1.1-….md`,
the converter can derive no key, and the whole document is dropped. Over the DP and SD corpora
that silently discarded ~1.03M words, including 98.5% of Italy Data Privacy.

The letters are not guessable from position — SD part E (short selling) is the ONLY part in
some documents, and a positional guess would call it A — but they are LEARNABLE: ~100 SD and
~75 DP trees do carry them, and they use the same folder names. This module reads those trees
and builds the name -> letter mapping, which is unanimous for part names:

    substantial-shareholding -> A (88 docs)   introduction            -> A (73 docs)
    sensitive-industries     -> B (97 docs)   organisation            -> B (73 docs)
    takeovers                -> C (96 docs)   processing-requirements -> D (73 docs)
    issuer-initiated-…       -> D (94 docs)   breach-response         -> H (72 docs)
    short-selling            -> E (96 docs)   cookies                 -> K (66 docs)

Section names are also learned, but they are NOT unanimous — "how-to-make-a-disclosure" ends
five different parts — so a section name resolves only when the corpus is lopsided about it
(SECT_MIN_SHARE across at least SECT_MIN_DOCS documents). Ambiguous ones are left to the
caller, which carries the previous part forward.

Keys recovered this way are real keys, not positional ones: they line up with every other
jurisdiction, so curated guidance attaches and the agent can compare A3.3 across documents.
"""
from __future__ import annotations

import collections
import json
import os
import re
from pathlib import Path

CACHE_NAME = ".part_lexicon.json"
PART_MIN_DOCS = 3       # a part name must be attested in at least this many trees
SECT_MIN_DOCS = 5
SECT_MIN_SHARE = 0.8    # ... and this much of them must agree on one letter

_LETTER_DIR = re.compile(r"^(?:\d+-)?([A-Ka-k])-")
_ORD = re.compile(r"^\d+[-.]")


def norm(name: str) -> str:
    """Folder OR FILE name -> comparable form: no ordinal, no part letter, no extension,
    lowercase. The extension matters: a loose "03-security.md" names the same section as the
    folder "03-security", and leaving ".md" on meant no file ever matched the lexicon."""
    n = _LETTER_DIR.sub("", name)
    n = _ORD.sub("", n)
    n = re.sub(r"\.md$", "", n, flags=re.I)
    n = re.sub(r"-\d+$", "", n)       # "05-data-sharing-2" is a continuation of data-sharing
    return n.strip().lower()


def part_letter(name: str) -> str | None:
    m = _LETTER_DIR.match(name)
    return m.group(1).upper() if m else None


def _subdirs(d: Path) -> list[str]:
    if not d.is_dir():
        return []
    return sorted(x.name for x in d.iterdir() if x.is_dir() and not x.name.startswith("_"))


def build(product_dir: Path) -> dict:
    """Learn {part name -> letter} and {section name -> letter} from every lettered tree."""
    parts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    sections: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    lettered = 0
    for tree in sorted(product_dir.glob("*/03_stage3_final")):
        tops = _subdirs(tree)
        if not any(part_letter(t) for t in tops):
            continue
        lettered += 1
        for top in tops:
            letter = part_letter(top)
            if not letter:
                continue
            parts[norm(top)][letter] += 1
            for sub in _subdirs(tree / top):
                sections[norm(sub)][letter] += 1

    def winner(counter, min_docs, min_share):
        letter, n = counter.most_common(1)[0]
        total = sum(counter.values())
        return letter if n >= min_docs and n / total >= min_share else None

    return {
        "lettered_trees": lettered,
        "parts": {n: L for n, c in parts.items()
                  if (L := winner(c, PART_MIN_DOCS, 0.9))},
        "sections": {n: L for n, c in sections.items()
                     if (L := winner(c, SECT_MIN_DOCS, SECT_MIN_SHARE))},
    }


def load(product_dir: Path, refresh: bool = False) -> dict:
    """Cached `build`. The converter runs as one subprocess PER DOCUMENT, so rescanning a
    140-tree product for each of them would dominate the build; the cache lives beside the
    trees it was learned from."""
    cache = product_dir / CACHE_NAME
    if not refresh and cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    lex = build(product_dir)
    try:
        cache.write_text(json.dumps(lex, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass          # a read-only corpus is fine; we just pay the scan
    return lex


def letter_for(lex: dict, folder: str) -> str | None:
    """The part letter a folder name means, or None if the corpus does not say clearly."""
    n = norm(folder)
    return lex.get("parts", {}).get(n) or lex.get("sections", {}).get(n)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("product_dir", help="out/corpus/<product>")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    lex = load(Path(args.product_dir), refresh=args.refresh)
    print(f"{lex['lettered_trees']} lettered tree(s) read")
    print(f"  parts    ({len(lex['parts'])}):")
    for n, L in sorted(lex["parts"].items(), key=lambda kv: kv[1]):
        print(f"    {L}  {n}")
    print(f"  sections ({len(lex['sections'])}): "
          + ", ".join(f"{n}->{L}" for n, L in sorted(lex["sections"].items())[:12]) + " …")


if __name__ == "__main__":
    main()
