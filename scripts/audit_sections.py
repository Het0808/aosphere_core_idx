"""Audit SECTION/heading extraction coverage across a corpus.

Extraction quality is upstream of retrieval quality: a heading we fail to turn
into a section becomes buried body text, so questions about it retrieve poorly.
This audit flags docs where the extractor likely MISSES structure, without needing
a heavyweight parser for every file.

For each docx it compares the sections extract_docx() produced against
"candidate headings" — short paragraphs that either (a) use a numbered heading
style (Level N / Heading N / DPNum), or (b) begin with a structural numbering
prefix (A., 1., 1.2.3, (a), (iv)) regardless of style (these are headings that
authoring templates styled as body). A candidate not captured as a section is a
likely miss. Aggregates the missed ones by Word style so you can see which body
styles hide headings (candidates for a promotion rule).

  .venv/bin/python scripts/audit_sections.py [<drop_dir_or_glob> ...] [--show N] [--worst N]

Defaults to both product drops under RAG-json_docx_v1_2026-06-30/.
"""
from __future__ import annotations

import glob
import json
import re
import sys
from collections import Counter, defaultdict

import docx

from aosphere_core_index.extract.docx_extract import extract_docx
from aosphere_core_index.extract.styles import (
    CONTENT_KINDS, GENERIC_HEADING_RE, SKIP_STYLES, numbering_heading_level,
)

_WS = re.compile(r"\s+")
_CANDIDATE_MAXLEN = 90
_CURATED_HEADING = re.compile(r"^(DPSectionHead|DPNum\d)$")
# Content styles are never headings — a numbered line in one is body (an
# enumeration or a survey question), not a missed heading.
_NON_HEADING_STYLES = set(CONTENT_KINDS) | {"DPQuestionBody"}


def norm(s: str) -> str:
    return _WS.sub(" ", (s or "").replace("\xa0", " ")).strip().lower()


def is_candidate_heading(text: str, style: str) -> bool:
    """A paragraph that SHOULD have become a section: a heading style, or a short
    non-sentence line with a strong section-number prefix (same rule the extractor
    uses to promote body-styled headings). Deliberately strict so clean templates
    (whose body carries incidental numbered lines) aren't over-flagged."""
    if not text or len(text) > _CANDIDATE_MAXLEN:
        return False
    if style.startswith("toc") or style in SKIP_STYLES:
        return False
    if _CURATED_HEADING.match(style) or GENERIC_HEADING_RE.match(style):
        return True
    return style not in _NON_HEADING_STYLES and numbering_heading_level(text) is not None


def audit_doc(docx_path: str, meta_path: str):
    dm = json.load(open(meta_path))
    dm = dm[0] if isinstance(dm, list) else dm
    doc = extract_docx(docx_path, doc_meta=dm, source_key="x")
    titles = {norm(s.title) for s in doc.sections}
    d = docx.Document(docx_path)
    missed_by_style = Counter()
    missed_samples = []
    candidates = 0
    for p in d.paragraphs:
        t = p.text.strip()
        style = (p.style.name if p.style else "") or ""
        if not is_candidate_heading(t, style):
            continue
        candidates += 1
        n = norm(t)
        # captured if its (numbering-stripped) text matches a section title
        stripped = norm(re.sub(r"^\s*(?:[A-Z]\.|\d+(?:\.\d+)*\.?|\([a-z0-9ivxlc]+\))\s+", "", t)) or n
        if n in titles or stripped in titles or any(n in tt or tt in n for tt in titles if len(tt) > 10):
            continue
        missed_by_style[style] += 1
        if len(missed_samples) < 6:
            missed_samples.append(f"[{style}] {t[:60]}")
    return {
        "product": str(dm.get("RENDERSTYLENAME", "")),
        "jurisdiction": str(dm.get("JURISDICTIONNAME", "")),
        "sections": len(doc.sections),
        "candidates": candidates,
        "missed": sum(missed_by_style.values()),
        "missed_by_style": missed_by_style,
        "samples": missed_samples,
    }


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--show=")), 6)
    worst = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--worst=")), 15)
    roots = args or ["RAG-json_docx_v1_2026-06-30/104_Shareholding_Disclosure/2026-06-30",
                     "RAG-json_docx_v1_2026-06-30/155_Data_Privacy/2026-06-30"]
    jur_dirs = []
    for r in roots:
        jur_dirs += [d for d in glob.glob(f"{r}/*") if glob.glob(f"{d}/*.docx")]
    rows, agg_style = [], Counter()
    for d in sorted(jur_dirs):
        docxs = glob.glob(f"{d}/*.docx")
        meta = f"{d}/Doc_metadata.json"
        if not docxs or not glob.glob(meta):
            continue
        try:
            r = audit_doc(docxs[0], meta)
        except Exception as e:  # noqa: BLE001
            print(f"  !! {d.rsplit('/',1)[-1]}: {type(e).__name__}: {str(e)[:80]}")
            continue
        rows.append(r)
        agg_style.update(r["missed_by_style"])

    rows.sort(key=lambda r: r["missed"], reverse=True)
    by_prod = defaultdict(list)
    for r in rows:
        by_prod[r["product"]].append(r)
    print(f"\naudited {len(rows)} docs across {len(by_prod)} products\n")
    for prod, rs in by_prod.items():
        miss = sum(r["missed"] for r in rs)
        cand = sum(r["candidates"] for r in rs)
        print(f"[{prod or '?'}] {len(rs)} docs | sections={sum(r['sections'] for r in rs)} "
              f"| candidate headings={cand} | likely-missed={miss} ({100*miss/max(cand,1):.0f}%)")
    print(f"\n=== worst {worst} docs by likely-missed headings ===")
    for r in rows[:worst]:
        print(f"  {r['jurisdiction'][:22]:22} {r['product'][:14]:14} sections={r['sections']:4} "
              f"missed={r['missed']:4}  top-styles={dict(r['missed_by_style'].most_common(3))}")
    print("\n=== missed headings aggregated by Word style (which styles hide headings) ===")
    for style, n in agg_style.most_common(15):
        print(f"  {style:20} {n}")
    print(f"\n=== samples from the single worst doc ({rows[0]['jurisdiction'] if rows else '-'}) ===")
    for s in (rows[0]["samples"] if rows else [])[:show]:
        print(f"  {s}")


if __name__ == "__main__":
    main()
