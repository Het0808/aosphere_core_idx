#!/usr/bin/env python3
"""check_numeric_integrity.py — Test 3: numbers/percentages/thresholds diff.

Generic word-diff treats "30%" and "3%" as two unrelated tokens: if "30%"
silently became "3%", it shows up as one missing + one extra entry, buried in
a long list of ordinary missing words. In a legal/compliance document, that
exact failure mode (a disclosure threshold digit-transposed) is far more
dangerous than a missing stopword and deserves its own report, prioritized
and with page hints.

This isolates numeric tokens (\\d[\\d,.]*%?) from both sides, normalizes
thousands separators so "5,000" == "5000" (formatting drift, not a real
difference), diffs them, and — because numbers are sparse — can afford to
locate which PDF page(s) each missing number actually came from. It also
flags "possible transposition": a missing number and an extra number that are
digit-permutations of each other (30% / 3%, 49% / 94%) and same suffix shape.

Usage:
    python scripts/check_numeric_integrity.py out/hybrid/172099
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
    BOLD, DIM, GREEN, NUMBER_RE, RED, YELLOW, banner,
    clean_markdown, detect_boilerplate_patterns, footnote_ids_in_tree, iter_content_files,
    page_band_lines, parse_page_range, pdf_page_texts, resolve_pdf, resolve_stage_dir,
    strip_boilerplate, tokenize, run_cli,
)


def numeric_tokens(text: str) -> list[str]:
    return NUMBER_RE.findall(text)


def normalize(tok: str) -> str:
    """Strip thousands-separator commas and a bare trailing full-stop (footnote/
    list markers like "5." tokenize identically to a plain "5" elsewhere);
    keep genuine decimal points ("1.1", "10.5") and a trailing %."""
    pct = tok.endswith("%")
    core = tok[:-1] if pct else tok
    core = core.replace(",", "")
    if core.endswith(".") and core.count(".") == 1:
        core = core[:-1]
    return core + ("%" if pct else "")


def digits_only(tok: str) -> str:
    return "".join(c for c in tok if c.isdigit())


def is_known_footnote_marker(tok: str, footnote_ids: set) -> bool:
    """True when `tok` (a normalized numeric key, e.g. "124") is exactly a
    footnote id the tree ITSELF already resolves somewhere as [^124] or
    [^124]:. A digit run the source PDF prints as a bare superscript
    ("...requirement124 applies...", "(Article 3(3))13.") reads to plain-text
    extraction as an ordinary number sitting right next to real prose — so
    the classifier below sees "the number's context survived, but not at
    this exact position" and calls it a real content loss. It never was one:
    the tree has already accounted for this exact digit run as a footnote
    reference/body, just moved into `[^n]` syntax instead of staying inline,
    which is precisely what a correctly-extracted footnote marker looks
    like. Percentages ("124%") and decimals never match here — normalize()
    keeps their suffix, so only a bare integer can collide with an id."""
    return tok.isdigit() and int(tok) in footnote_ids


def looks_footnote_glued(w: str, md_counter: Counter) -> bool:
    """A long number where stripping its last 1-2 digits yields a number the
    tree DOES have is very likely a real number with a footnote-reference
    digit glued on with no separating space in raw extraction (e.g. "3,575"
    followed immediately by footnote marker "65" -> "357565"), not a real
    missing value."""
    core = w[:-1] if w.endswith("%") else w
    if len(core) <= 4:
        return False
    for strip_n in (1, 2):
        prefix = core[:-strip_n]
        if prefix and md_counter.get(prefix, 0) > 0:
            return True
    return False


def is_transposition(a: str, b: str) -> bool:
    """Flag only percentages or 3+ digit numbers (thresholds, law/section refs,
    amounts) — bare 1-2 digit integers are almost always footnote/list markers,
    where a coincidental digit-permutation match (e.g. two unrelated footnotes
    "21" and "12") means nothing."""
    if a == b:
        return False
    # The trailing non-numeric part ("%" or ""), which must match for the pair to
    # be comparable. rstrip() cannot do this: "35%" ends in "%", not a digit, so
    # rstrip("0123456789,.") removes nothing and returns "35%" — meaning "35%" and
    # "53%" compared as different suffixes and NO percentage transposition was ever
    # detectable, despite that being this check's motivating example.
    a_suffix = re.search(r"[^\d,.]*$", a).group(0)
    b_suffix = re.search(r"[^\d,.]*$", b).group(0)
    if a_suffix != b_suffix:
        return False
    da, db = digits_only(a), digits_only(b)
    if len(da) != len(db) or sorted(da) != sorted(db):
        return False
    return a.endswith("%") or len(da) >= 3


# Words either side of a number, used to tell a NOISE finding from a REAL one.
#
# A bare number going missing ("100", "20", "320") is almost always a page
# number, list marker or footnote marker — worth showing, not worth scoring. But
# a number embedded in meaning ("number of countries 230") disappearing TOGETHER
# WITH its surrounding words means an actual statement left the document.
#
# So the test is not about the number at all: it is whether the number's context
# survived. Context still in the tree -> only the marker went -> warning. Context
# gone too -> a whole phrase is missing -> error.
NUM_CTX = 4
MIN_CTX_WORDS = 2       # fewer real words than this and the number stands alone


def _classify_missing(norm_tokens, positions, tree_hay, want, front_matter_end=0):
    """-> {normalized number: (severity, context phrase)}.

    norm_tokens/positions: the PDF's full token stream and where each tracked
    number occurs in it. tree_hay: sentinel-joined tree tokens.

    front_matter_end: token index where the document body begins. The cover page
    and table of contents are never carried into the tree by design, and they are
    dense with numbers ("… 4 Aggregation 24 5 Exemptions 26 …", phone numbers,
    addresses, dates). Without this, 17 of 18 "errors" on the pilot document were
    TOC lines and a reporting-counsel address — technically absent, but not
    content loss, and enough noise to bury the one real finding."""
    out = {}
    for key in want:
        sev, ctx = "warning", ""
        for i in positions.get(key, [])[:6]:      # a few occurrences is plenty
            if i < front_matter_end:
                continue                          # cover page / TOC -> never an error
            before = norm_tokens[max(0, i - NUM_CTX):i]
            after = norm_tokens[i + 1:i + 1 + NUM_CTX]
            words = [w for w in before + after if not any(c.isdigit() for c in w)]
            if len(words) < MIN_CTX_WORDS:
                continue                          # a bare number -> stays a warning
            phrase = before + after
            found = ("\x01" + "\x01".join(phrase) + "\x01") in tree_hay
            if not found:
                # the words around it are missing too, so a real statement went
                sev, ctx = "error", " ".join(before + [key] + after)
                break
            if not ctx:
                ctx = " ".join(before + [key] + after)
        out[key] = (sev, ctx)
    return out


def compute_numeric_integrity(out_root: Path, pdf: str | None = None, stage: int = 3) -> dict:
    pdf_path = resolve_pdf(out_root, pdf)
    tree_root = resolve_stage_dir(out_root, stage)

    pages = pdf_page_texts(pdf_path)
    band = page_band_lines(pdf_path)
    patterns, boilerplate_lines = detect_boilerplate_patterns(pages, band_lines=band)
    clean_pages = strip_boilerplate(pages, patterns, band_lines=band)

    # per-page normalized-number index, for locating where a missing number came from
    page_numbers: dict[str, set[int]] = {}
    pdf_counter = Counter()
    # Normalizing strips the thousands separator so "25,750" and "25750" compare
    # equal — necessary for matching, but the normalized key must never be what
    # gets REPORTED: "25750" appears nowhere in either document, so a reader sees
    # a number that looks fabricated and can't grep for it. Keep the first
    # surface form seen and show that instead.
    surface: dict[str, str] = {}
    for pno in range(1, len(clean_pages)):
        for raw in numeric_tokens(clean_pages[pno]):
            norm = normalize(raw)
            pdf_counter[norm] += 1
            surface.setdefault(norm, raw)
            page_numbers.setdefault(norm, set()).add(pno)

    # Every downstream pass over the tree (numeric counts, the transposition
    # context-probe, the "absent" classifier, and page-range detection) used to
    # re-open and re-read every file separately — 3 unconditional read_text()
    # calls per file plus a 4th whenever any transposition candidate existed.
    # One pass here reads each file exactly once and caches what each consumer
    # needs from it; nothing below this point touches disk again.
    files = list(iter_content_files(tree_root))
    file_tokens: list[list[str]] = []   # tokenize(clean_markdown(...)) per file, reused
    page_ranges: list[tuple[int, int] | None] = []
    md_counter = Counter()
    for f in files:
        raw_text = f.read_text(encoding="utf-8")
        cleaned = clean_markdown(raw_text)
        for raw in numeric_tokens(cleaned):
            md_counter[normalize(raw)] += 1
        file_tokens.append(tokenize(cleaned))
        page_ranges.append(parse_page_range(raw_text))  # needs RAW text — clean_markdown
                                                          # strips the "*Source: ..., page N*"
                                                          # breadcrumb line this reads

    footnote_ids = footnote_ids_in_tree(tree_root)
    missing_raw = pdf_counter - md_counter
    footnote_marker_keys = sorted(
        (k for k in missing_raw if is_known_footnote_marker(k, footnote_ids)), key=int)
    missing = Counter({k: v for k, v in missing_raw.items()
                       if not looks_footnote_glued(k, md_counter)
                       and not is_known_footnote_marker(k, footnote_ids)})
    extra = md_counter - pdf_counter

    # A digit-permutation pair is only evidence of a transposition if the two
    # numbers occupy the SAME PLACE in the text. Without that requirement the
    # check is pure coincidence-mining: a citation-heavy document has ~1,500
    # footnote markers, so 3-digit permutations collide constantly and it reported
    # 115 "transpositions" — 203/320, 088/808, 104/410 — all unrelated footnote
    # numbers, which saturated the Integrity score to 0 and hid anything real.
    #
    # So: take the missing number's context in the PDF and look for that same
    # context in the tree with the OTHER number in its place. That is what a
    # transposition actually looks like, and coincidence cannot fake it.
    def _tok1(s: str) -> str:
        """The form a number takes AFTER tokenize(), which drops '%' and commas.
        The missing/extra keys keep them ("35%"), so looking a key up directly in a
        token stream silently never matches — which disabled percentage detection
        entirely, the single most dangerous class of transposition."""
        tt = tokenize(s)
        return tt[0] if tt else s

    # Both this probe's haystack and _classify_missing's tree_hay below are the
    # SAME sentinel-joined join of every file's tokens — previously built twice
    # (once here, once below) by two separate read_text() passes. Built once,
    # up front, from the cache — used by whichever of the two needs it.
    md_tokens = [t for toks in file_tokens for t in toks]
    tree_hay = "\x01" + "\x01".join(md_tokens) + "\x01"

    transpositions = []
    _cand = [(m, e) for m in missing for e in extra if is_transposition(m, e)]
    if _cand:
        _pdf_toks = tokenize("".join(clean_pages[1:]))
        _pos: dict[str, list[int]] = {}
        _want = {_tok1(m) for m, _ in _cand}
        for i, tok in enumerate(_pdf_toks):
            if tok in _want:
                _pos.setdefault(tok, []).append(i)
        _hay = tree_hay
        for m, e in _cand:
            for i in _pos.get(_tok1(m), [])[:8]:
                before = _pdf_toks[max(0, i - 3):i]
                after = _pdf_toks[i + 1:i + 4]
                if len(before) + len(after) < 3:
                    continue
                probe = "\x01" + "\x01".join(before + [_tok1(e)] + after) + "\x01"
                if probe in _hay:
                    transpositions.append({
                        "missing": surface.get(m, m), "extra": e,
                        "missing_pages": sorted(page_numbers.get(m, [])),
                        "context": " ".join(before + [f"[{m}->{e}]"] + after),
                    })
                    break

    # "absent" (the tree has this number NOWHERE) is a materially different
    # finding from "fewer occurrences than the PDF" — the latter is usually one
    # repeated value that lost some instances inside an already-flagged failed
    # table, not a number that vanished. Reporting both as "missing" overstates
    # the second case badly.
    # Severity by whether the number's CONTEXT survived (see _classify_missing).
    pdf_tokens = tokenize("".join(clean_pages[1:]))
    positions: dict[str, list[int]] = {}
    for i, tok in enumerate(pdf_tokens):
        n = normalize(tok)
        if n in missing:
            positions.setdefault(n, []).append(i)
    # Where the body starts, in token terms: the lowest page any content file
    # claims, converted to a token offset by counting the pages before it.
    starts = [r[0] for r in page_ranges if r]
    first_body_page = min(starts) if starts else 1
    front_matter_end = len(tokenize("".join(clean_pages[1:first_body_page])))
    sev_map = _classify_missing(pdf_tokens, positions, tree_hay, list(missing),
                                front_matter_end)

    missing_out = {}
    for k, v in missing.most_common():
        tree_count = md_counter.get(k, 0)
        sev, ctx = sev_map.get(k, ("warning", ""))
        missing_out[k] = {
            "count": v, "pages": sorted(page_numbers.get(k, [])),
            "surface": surface.get(k, k),
            "tree_count": tree_count,
            "pdf_count": pdf_counter.get(k, 0),
            "absent": tree_count == 0,
            # "error"   = the number AND the words around it are gone -> real loss
            # "warning" = only the bare number went, or its sentence survived
            "severity": sev,
            "context": ctx,
        }
    passed = not transpositions
    return {
        "pdf": str(pdf_path),
        "tree_root": str(tree_root),
        "pdf_numeric_total": sum(pdf_counter.values()),
        "pdf_numeric_distinct": len(pdf_counter),
        "md_numeric_total": sum(md_counter.values()),
        "md_numeric_distinct": len(md_counter),
        "missing": missing_out,
        "missing_absent_count": sum(1 for v in missing_out.values() if v["absent"]),
        "missing_error_count": sum(1 for v in missing_out.values() if v["severity"] == "error"),
        "missing_warning_count": sum(1 for v in missing_out.values() if v["severity"] == "warning"),
        "extra": dict(extra.most_common()),
        "transpositions": transpositions,
        # Numbers excluded from `missing` because the tree already resolves them
        # as a footnote id (see is_known_footnote_marker) — reported so a low
        # "missing" count is explainable, not just quietly smaller.
        "footnote_marker_excluded": footnote_marker_keys[:20],
        "footnote_marker_excluded_count": len(footnote_marker_keys),
        "passed": passed,
    }


def print_report(report: dict, top: int = 40):
    print(f"{DIM('pdf   :')} {report['pdf']}")
    print(f"{DIM('tree  :')} {report['tree_root']}")
    print(f"\n{BOLD('pdf numeric tokens')} : {report['pdf_numeric_total']} ({report['pdf_numeric_distinct']} distinct)")
    print(f"{BOLD('md numeric tokens')}  : {report['md_numeric_total']} ({report['md_numeric_distinct']} distinct)")
    missing, extra = report["missing"], report["extra"]
    print(f"{BOLD('missing')}            : " + (GREEN if not missing else YELLOW)(f"{sum(v['count'] for v in missing.values())} occurrences, {len(missing)} distinct"))
    print(f"{BOLD('extra')}              : " + (GREEN if not extra else YELLOW)(f"{sum(extra.values())} occurrences, {len(extra)} distinct"))
    fn_excl = report.get("footnote_marker_excluded_count", 0)
    if fn_excl:
        print(f"{DIM(f'(also excluded {fn_excl} number(s) already resolved in the tree as a footnote id — not content loss)')}")

    if missing:
        print(f"\n{YELLOW('missing numbers (in PDF, not in tree), with page hints:')}")
        for w, v in list(missing.items())[:top]:
            pgs = v["pages"]
            pg_str = ",".join(str(p) for p in pgs[:8]) + (" …" if len(pgs) > 8 else "")
            shown = v.get("surface", w)
            kind = "absent" if v.get("absent") else f"{v.get('tree_count')}/{v.get('pdf_count')} kept"
            print(f"    {DIM(str(v['count']).rjust(3))}  {shown:>12}  {DIM(kind.ljust(12))}"
                  f"{DIM('pages ' + pg_str)}")
    if extra:
        print(f"\n{YELLOW('extra numbers (in tree, not in PDF):')}")
        for w, c in list(extra.items())[:top]:
            print(f"    {DIM(str(c).rjust(3))}  {w}")

    transpositions = report["transpositions"]
    if transpositions:
        print(f"\n{RED(BOLD('⚠ POSSIBLE TRANSPOSITIONS (digit-permutation, same shape — verify these by hand):'))}")
        for t in transpositions:
            print(f"    {RED(t['missing'])} → {RED(t['extra'])}  {DIM('missing on page(s) ' + ','.join(str(p) for p in t['missing_pages'][:5]))}")
    else:
        print(f"\n{GREEN('no digit-permutation transpositions detected')}")

    print()
    if report["passed"]:
        print(GREEN(BOLD("✓ PASS — no suspected numeric transpositions")))
    else:
        print(RED(BOLD(f"✗ FAIL — {len(transpositions)} suspected transposition(s), verify by hand")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_root")
    ap.add_argument("--pdf", default=None)
    ap.add_argument("--stage", type=int, default=3, choices=[1, 2, 3])
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    out_root = Path(args.out_root).resolve()
    banner("Test 3 · numeric integrity")
    report = compute_numeric_integrity(out_root, args.pdf, args.stage)
    print_report(report)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(DIM(f"\nfull report written to {args.json}"))

    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    run_cli(main)
