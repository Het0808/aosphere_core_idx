#!/usr/bin/env python3
"""check_three_way_crosscheck.py — Test 4: three independent extractors vs. the tree.

Test 1's word-coverage check has a real limitation: its "ground truth" (fitz's
own get_text()) is produced by the SAME engine pdf2mdtree itself is built on
(PyMuPDF-only, no model, no network). Any blind spot specific to fitz's text
extraction — an unusual font encoding, text drawn as vector paths instead of
real text objects — would silently pass through on BOTH sides of that
comparison, because both sides depend on it.

This adds two more independent sources:
  - pypdf: a completely separate, pure-Python PDF parser (not a MuPDF wrapper).
  - MinerU (whole-document, not the table-only crop Stage 2 uses): vision-based
    — it reads the RENDERED page, not the PDF's internal text objects at all,
    so it shares no code path with either fitz or pypdf.

A word missing from the tree is bucketed by how many of the three independent
sources agree it's really in the source document:
  - 2-3 agree -> HIGH CONFIDENCE real gap (two differently-built systems,
    including one that doesn't even read the text layer, both say it's there).
  - exactly 1 -> LOW CONFIDENCE — likely a single tool's own quirk/artifact,
    not a real gap in the tree.

Usage:
    python scripts/check_three_way_crosscheck.py out/hybrid/172099 \\
        --mineru-content-list out/hybrid/172099/mineru_full_doc/172099/hybrid_auto/172099_content_list.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_content_compare import (
    BOLD, DIM, GREEN, RED, YELLOW, banner,
    clean_markdown, detect_boilerplate_patterns, iter_content_files,
    mineru_page_texts, page_band_lines, parse_page_range, pdf_page_texts,
    pypdf_page_texts, resolve_pdf,
    resolve_stage_dir, strip_boilerplate, tokenize, run_cli,
)


def find_mineru_content_list(out_root: Path) -> Path | None:
    hits = sorted(out_root.glob("mineru_full_doc/**/*_content_list.json"))
    return hits[0] if hits else None


# ---- paragraph/sentence-level loss -------------------------------------
# A list of individually-missing WORDS is nearly unreviewable: it doesn't say
# whether a whole point was dropped or a stopword differed. What matters for
# review is "is a whole sentence or paragraph gone, and on which page" — so the
# text from each independent extractor is split into blocks and each block is
# checked for presence in the tree as a unit.
MIN_BLOCK_TOKENS = 12       # shorter than this is a fragment, not a point
WINDOW = 8                  # token window used for fuzzy containment
STRIDE = 4
TREE_ABSENT_BELOW = 0.35    # block counts as MISSING from the tree below this
SOURCE_PRESENT_ABOVE = 0.6  # block counts as PRESENT in a source above this
_SENT_SPLIT = re.compile(r"(?<=[.!?;:])\s+(?=[A-Z(“\"\d])")


def _flatten_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _hay(tokens: list[str]) -> str:
    return "\x01" + "\x01".join(tokens) + "\x01"


def _run_in(hay: str, toks: list[str]) -> bool:
    return ("\x01" + "\x01".join(toks) + "\x01") in hay


def _present_ratio(toks: list[str], hay: str) -> float:
    """Fraction of a block's sliding token windows found in `hay`. Windowed
    rather than exact so that harmless differences between two extractors
    (hyphenation, a joined column, a stray ligature) don't read as absence."""
    if not toks:
        return 1.0
    if len(toks) <= WINDOW:
        return 1.0 if _run_in(hay, toks) else 0.0
    wins = [toks[i:i + WINDOW] for i in range(0, len(toks) - WINDOW + 1, STRIDE)]
    return sum(1 for w in wins if _run_in(hay, w)) / len(wins)


def _page_blocks(page_text: str) -> list[str]:
    """Paragraphs, with over-long paragraphs broken into sentences so a finding
    points at one reviewable claim rather than half a page."""
    out = []
    for para in re.split(r"\n\s*\n", page_text or ""):
        para = _flatten_ws(para.replace("\n", " "))
        if not para:
            continue
        if len(tokenize(para)) <= 60:
            out.append(para)
            continue
        buf = ""
        for sent in _SENT_SPLIT.split(para):
            buf = (buf + " " + sent).strip()
            if len(tokenize(buf)) >= 25:
                out.append(buf)
                buf = ""
        if buf:
            out.append(buf)
    return out


def find_missing_blocks(source_pages: dict[str, list[str]], tree_tokens: list[str],
                        min_confidence: int = 2, first_body_page: int = 1) -> list[dict]:
    """Whole sentences/paragraphs that independent extractors see on a page but
    the tree does not have anywhere. `source_pages` maps source name -> 1-indexed
    page texts. Confidence = how many sources independently contain the block, so
    a quirk of one extractor alone can't raise a finding.

    `first_body_page` skips FRONT MATTER. pdf2mdtree starts at the first page the
    PDF outline points to, so the cover page (title, disclaimer, reporting-counsel
    contact, document-control stamp) and the table of contents are deliberately
    never carried into the tree. Reporting them as missing content is pure noise
    and would bury the real findings — on the pilot document it was 11 of 15."""
    tree_hay = _hay(tree_tokens)
    source_hays = {name: _hay(tokenize("".join(pages[1:]))) for name, pages in source_pages.items()}
    n_pages = max((len(p) for p in source_pages.values()), default=1) - 1

    found: dict[tuple, dict] = {}
    for pno in range(max(1, first_body_page), n_pages + 1):
        for name, pages in source_pages.items():
            if pno >= len(pages):
                continue
            for block in _page_blocks(pages[pno]):
                toks = tokenize(block)
                if len(toks) < MIN_BLOCK_TOKENS:
                    continue
                if _present_ratio(toks, tree_hay) >= TREE_ABSENT_BELOW:
                    continue                      # the tree does have it
                key = (pno, tuple(toks[:10]))     # dedupe the same block seen by 2 sources
                entry = found.get(key)
                if entry is None:
                    entry = found[key] = {"page": pno, "text": block, "tokens": len(toks),
                                          "sources": [], "seen_by": {}}
                for sname, shay in source_hays.items():
                    if sname in entry["sources"]:
                        continue
                    ratio = _present_ratio(toks, shay)
                    if ratio >= SOURCE_PRESENT_ABOVE:
                        entry["sources"].append(sname)
                        entry["seen_by"][sname] = round(ratio, 2)
                # keep the longest wording of the same block
                if len(block) > len(entry["text"]):
                    entry["text"] = block

    blocks = [b for b in found.values() if len(b["sources"]) >= min_confidence]
    for b in blocks:
        b["confidence"] = len(b["sources"])
    blocks.sort(key=lambda b: (b["page"], -b["tokens"]))
    return blocks


def compute_three_way_crosscheck(out_root: Path, pdf: str | None = None, stage: int = 3,
                                  mineru_content_list: str | None = None, min_confidence: int = 2,
                                  shortfall_ratio: float = 0.5) -> dict:
    pdf_path = resolve_pdf(out_root, pdf)
    tree_root = resolve_stage_dir(out_root, stage)

    ml_path = Path(mineru_content_list) if mineru_content_list else find_mineru_content_list(out_root)

    page_index: dict[str, set] = {}  # word -> set of 1-indexed pages it was found on, any source

    def _index_pages(pages: list[str]):
        for pno in range(1, len(pages)):
            for w in set(tokenize(pages[pno])):
                page_index.setdefault(w, set()).add(pno)

    fitz_pages = pdf_page_texts(pdf_path)
    band = page_band_lines(pdf_path)
    patterns, boilerplate_lines = detect_boilerplate_patterns(fitz_pages, band_lines=band)
    fitz_pages = strip_boilerplate(fitz_pages, patterns, band_lines=band)
    fitz_counter = Counter(tokenize("".join(fitz_pages[1:])))
    _index_pages(fitz_pages)

    pypdf_pages = pypdf_page_texts(pdf_path)
    # No band here: page_band_lines keys on the exact text FITZ read, and pypdf is a
    # separate parser whose line splitting differs. The index zone alone is correct.
    pypdf_patterns, _ = detect_boilerplate_patterns(pypdf_pages)
    pypdf_pages = strip_boilerplate(pypdf_pages, pypdf_patterns)
    pypdf_counter = Counter(tokenize("".join(pypdf_pages[1:])))
    _index_pages(pypdf_pages)

    mineru_available = ml_path is not None and ml_path.exists()
    mineru_counter = Counter()
    if mineru_available:
        mineru_pages = mineru_page_texts(ml_path)
        # MinerU classifies most running headers/footers as their own block
        # types (already excluded in mineru_page_texts), but not always all of
        # them — run the same heuristic stripper as the other two sources so a
        # boilerplate line MinerU happens to tag as "text" doesn't leak through.
        mineru_patterns, _ = detect_boilerplate_patterns(mineru_pages)
        mineru_pages = strip_boilerplate(mineru_pages, mineru_patterns)
        mineru_counter = Counter(tokenize("".join(mineru_pages[1:])))
        _index_pages(mineru_pages)

    files = list(iter_content_files(tree_root))
    tree_tokens = []
    for f in files:
        tree_tokens.extend(tokenize(clean_markdown(f.read_text(encoding="utf-8"))))
    tree_counter = Counter(tree_tokens)

    sources = {"fitz": fitz_counter, "pypdf": pypdf_counter}
    if mineru_available:
        sources["mineru"] = mineru_counter
    n_sources = len(sources)

    union_vocab = set(fitz_counter) | set(pypdf_counter) | (set(mineru_counter) if mineru_available else set())

    _FOOTNOTE_SUFFIX_RE = re.compile(r"^([a-z]+)(\d+)$")
    _FOOTNOTE_PREFIX_RE = re.compile(r"^(\d+)([a-z]+)$")

    high_conf_missing, low_conf_missing = {}, {}
    for w in union_vocab:
        if w.isdigit():
            continue  # numeric tokens are Test 3's job (numeric integrity), not this test's
        if len(w) < 3:
            continue  # single/double-character tokens are near-universally OCR/extraction
                       # noise (stray glyphs, split ordinal suffixes like "th"), not real content
        m = _FOOTNOTE_SUFFIX_RE.match(w)
        if m and tree_counter.get(m.group(1), 0) > 0:
            # e.g. "shareholder4" from a superscript footnote ref glued to the
            # preceding word with no space in raw extraction — pdf2mdtree
            # correctly splits this into "shareholder" + a separate "[^4]"
            # footnote reference, so the merged token never should have
            # existed; the real word IS in the tree, just not glued to a digit.
            continue
        m = _FOOTNOTE_PREFIX_RE.match(w)
        if m and tree_counter.get(m.group(2), 0) > 0:
            # the reverse case: a footnote number glued to the front of the
            # word that starts its definition, e.g. "31if" from "³¹If the...".
            continue
        tree_ct = tree_counter.get(w, 0)
        src_counts = {name: c.get(w, 0) for name, c in sources.items()}
        nonzero = [c for c in src_counts.values() if c > 0]
        if not nonzero:
            continue
        min_src_ct = min(nonzero)
        # A word is only "missing" if it's entirely absent, or the tree carries
        # a substantially smaller share of it than even the most conservative
        # independent source — a 1-2 occurrence gap out of hundreds (normal
        # extraction variance: footnote relocation, table-cell text handling)
        # is not a gap, it's noise, and flagging it is exactly what drowned out
        # real findings before.
        if tree_ct != 0 and tree_ct >= min_src_ct * shortfall_ratio:
            continue
        confidence = len(nonzero)  # how many of the independent sources agree this word exists at all
        entry = {"word": w, "sources": src_counts, "tree_count": tree_ct, "confidence": confidence,
                 "pages": sorted(page_index.get(w, []))}
        if confidence >= min_confidence:
            high_conf_missing[w] = entry
        else:
            low_conf_missing[w] = entry

    extra_in_tree = {w: c for w, c in tree_counter.items() if w not in union_vocab}

    pairwise = {}
    for a, b in (("fitz", "pypdf"), ("fitz", "mineru"), ("pypdf", "mineru")):
        if a not in sources or b not in sources:
            continue
        ca, cb = sources[a], sources[b]
        agree = sum((ca & cb).values())
        total = max(sum(ca.values()), sum(cb.values()))
        pairwise[f"{a}_vs_{b}"] = round(100 * agree / total, 2) if total else 100.0

    passed = len(high_conf_missing) == 0

    # Paragraph/sentence-level loss, computed from the INDEPENDENT sources only
    # (pypdf, and MinerU when available). fitz is excluded on purpose: the tree is
    # built from fitz, so a block fitz sees is nearly always in the tree, and
    # including it would just dilute the confidence count. With one independent
    # source, min_confidence drops to 1 — otherwise nothing could ever reach 2.
    indep_pages = {"pypdf": pypdf_pages}
    if mineru_available:
        indep_pages["mineru"] = mineru_pages
    # Where the tree's own content actually begins — the lowest page any content
    # file claims. Everything before it is front matter the extractor omits by
    # design (see find_missing_blocks).
    body_starts = [r[0] for r in
                   (parse_page_range(f.read_text(encoding="utf-8")) for f in files) if r]
    first_body_page = min(body_starts) if body_starts else 1
    missing_blocks = find_missing_blocks(
        indep_pages, tree_tokens,
        min_confidence=min(min_confidence, len(indep_pages)),
        first_body_page=first_body_page)

    return {
        "pdf": str(pdf_path),
        "tree_root": str(tree_root),
        "mineru_available": mineru_available,
        "missing_blocks": missing_blocks,
        "missing_block_count": len(missing_blocks),
        "missing_block_pages": sorted({b["page"] for b in missing_blocks}),
        "mineru_content_list": str(ml_path) if ml_path else None,
        "source_totals": {name: sum(c.values()) for name, c in sources.items()},
        "tree_total": sum(tree_counter.values()),
        "pairwise_agreement_pct": pairwise,
        "high_confidence_missing": dict(sorted(high_conf_missing.items(), key=lambda kv: -kv[1]["confidence"])),
        "low_confidence_missing": dict(sorted(low_conf_missing.items(), key=lambda kv: -sum(kv[1]["sources"].values()))),
        "extra_in_tree": dict(Counter(extra_in_tree).most_common()),
        "min_confidence": min_confidence,
        "passed": passed,
    }


def print_report(report: dict, top: int = 30):
    print(f"{DIM('pdf   :')} {report['pdf']}")
    print(f"{DIM('tree  :')} {report['tree_root']}")
    if not report["mineru_available"]:
        print(YELLOW("no whole-document MinerU content_list.json found — running fitz + pypdf only (2-way)"))
    else:
        print(DIM(f"mineru :  {report['mineru_content_list']}"))

    print(f"\n{BOLD('word totals')} : " + "  ".join(f"{name}={n}" for name, n in report["source_totals"].items()) + f"  tree={report['tree_total']}")
    if report["pairwise_agreement_pct"]:
        print(f"{BOLD('pairwise agreement')} : " + "  ".join(f"{k}={v}%" for k, v in report["pairwise_agreement_pct"].items()))

    hi, lo = report["high_confidence_missing"], report["low_confidence_missing"]
    print(f"\n{BOLD('high-confidence missing')} : " + (GREEN if not hi else RED)(f"{len(hi)} word(s) — {report['min_confidence']}+ independent sources agree, tree doesn't have it"))
    for w, e in list(hi.items())[:top]:
        srcs = "/".join(f"{k}:{v}" for k, v in e["sources"].items() if v)
        pgs = ",".join(str(p) for p in e["pages"][:6]) + (" …" if len(e["pages"]) > 6 else "")
        print(f"    {w:>20}   {DIM('found in ' + srcs + ', tree=' + str(e['tree_count']) + ', pages ' + pgs)}")
    print(f"{BOLD('low-confidence missing')}  : {DIM(str(len(lo)) + ' word(s) — only 1 source claims it, likely that source own artifact')}")

    extra = report["extra_in_tree"]
    print(f"{BOLD('extra in tree')}           : " + (GREEN if not extra else YELLOW)(f"{len(extra)} word(s) not found by ANY independent source (possible hallucination/duplication)"))
    for w, c in list(extra.items())[:top]:
        print(f"    {DIM(str(c).rjust(3))}  {w}")

    print()
    if report["passed"]:
        print(GREEN(BOLD("✓ PASS — no high-confidence (multi-source-agreed) content gaps")))
    else:
        print(RED(BOLD(f"✗ FAIL — {len(hi)} high-confidence gap(s), confirmed by {report['min_confidence']}+ independent extractors")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--stage", type=int, default=3, choices=[1, 2, 3])
    ap.add_argument("--mineru-content-list", default=None, help="path to a whole-document MinerU content_list.json (default: auto-detect under out_root/mineru_full_doc/)")
    ap.add_argument("--min-confidence", type=int, default=2, help="min number of independent sources that must agree to call a gap 'high confidence'")
    ap.add_argument("--shortfall-ratio", type=float, default=0.5, help="tree count must fall below (min source count * this ratio) to count as missing; ignored if tree count is 0")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    out_root = Path(args.out_root).resolve()
    banner("Test 4 · three-way cross-check (fitz / pypdf / MinerU)")
    report = compute_three_way_crosscheck(out_root, args.pdf, args.stage, args.mineru_content_list,
                                           args.min_confidence, args.shortfall_ratio)
    print_report(report)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))

    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
