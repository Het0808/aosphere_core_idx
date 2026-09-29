"""Extraction invariants — the build-time gate against structural misextraction.

Every check here encodes a real failure mode found in the corpus (2026-07-02):
a cover date ("14 November 2025") or contact address ("1 Queen Street") promoted
to a Part heading consumed letter A and silently shifted every real part's letter
in 42 of 105 Shareholding Disclosure jurisdictions — indexed, searchable, and
wrong. These invariants make that class of failure loud instead of silent.

Works on the plain section dicts of a content.json (or ExtractedDoc.sections via
`as_dicts`), so it can gate a fresh build AND audit artifacts already on disk.

`validate_sections` returns (errors, warnings): errors are structural corruption
(fail the build); warnings are suspicious but possibly legitimate (report only).
"""

from __future__ import annotations

import re

# Part titles that are data, not headings: dates ("14 November 2025",
# "2 August, 2025") and addresses ("1 Queen Street") start with a digit.
_DIGIT_START = re.compile(r"^\d")
_DATE_LIKE = re.compile(
    r"^\d{1,2}(st|nd|rd|th)?\s+(of\s+)?"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*", re.I)

# First-part expectations per product (lowercase TOKENS all expected in the Part A
# title — token-based so "Substantial / Controlling Shareholding" passes). A wrong
# first part is how a letter shift manifests; reported as a WARNING because a few
# older-template docs legitimately open differently ("Overview", "Background").
EXPECTED_FIRST_PART: dict[str, tuple[str, ...]] = {
    "Shareholding Disclosure": ("substantial", "shareholding"),
}

MIN_SECTIONS = 25          # a full survey extracts hundreds; below this = broken
MIN_PART_CHILDREN = 1      # a letter part with no children was swallowed (Spain's bare 'A')


def _is_letter_part(s: dict) -> bool:
    return s.get("level") == 0 and bool(re.fullmatch(r"[A-Z]", str(s.get("key", ""))))


def validate_sections(sections: list[dict], product: str | None = None,
                      jurisdiction: str = "") -> tuple[list[str], list[str]]:
    """Structural invariants over extracted sections. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    tag = f"{product or '?'}/{jurisdiction or '?'}"

    parts = [s for s in sections if _is_letter_part(s)]
    if not parts:
        errors.append(f"{tag}: no letter Parts (A..) extracted")
        return errors, warnings

    # 1) A part titled like a date/address is front-matter promoted to a heading —
    #    it shifts every subsequent part's letter. Always an error.
    for p in parts:
        title = str(p.get("title", "")).strip()
        if _DIGIT_START.match(title):
            kind = "date" if _DATE_LIKE.match(title) else "number-led text"
            errors.append(f"{tag}: part {p['key']} titled like a {kind}: {title[:40]!r} "
                          f"(front matter promoted to a Part — letters are shifted)")

    # 2) Wrong first part for the product = shifted or missing Part A (warning:
    #    a handful of older-template docs open with "Overview"/"Background").
    want = EXPECTED_FIRST_PART.get(product or "")
    if want:
        first = str(parts[0].get("title", "")).lower()
        if not all(tok in first for tok in want):
            warnings.append(f"{tag}: first part is {parts[0].get('title', '')[:40]!r}, "
                            f"expected tokens {want}")

    # 3) A letter part with no children AND no content of its own: its internal
    #    headings were all missed (buried as body under nothing) — retrieval for
    #    that part is blind. Childless parts WITH elements are legitimate
    #    single-block parts (e.g. DP "G Employment"), so they pass.
    children: dict[str, int] = {p["key"]: 0 for p in parts}
    for s in sections:
        k = str(s.get("key", ""))
        m = re.match(r"^([A-Z])\d", k)
        if m and m.group(1) in children:
            children[m.group(1)] += 1
    by_key = {str(p.get("key", "")): p for p in parts}
    childless = [letter for letter, n in children.items()
                 if n < MIN_PART_CHILDREN and not by_key[letter].get("elements")]
    for letter in childless:
        errors.append(f"{tag}: part {letter} ({by_key[letter].get('title', '')[:30]!r}) "
                      f"has no child sections and no content — its headings "
                      f"were not detected")

    # Too few sections signals under-extraction — but the real failure (headings
    # missed, content dumped as body) ALSO leaves Parts childless (flagged above) or
    # collapses the Part list. A short doc whose Parts are all present and populated
    # is a genuinely brief survey (e.g. Zimbabwe SD, ~11 pages), not corruption — so
    # warn rather than fail the build unless the structure is also thin.
    if len(sections) < MIN_SECTIONS:
        msg = f"{tag}: only {len(sections)} sections extracted (min {MIN_SECTIONS})"
        if childless or len(parts) < 3:
            errors.append(msg + " — under-extraction (Parts missing/empty)")
        else:
            warnings.append(msg + " — brief survey (Parts all populated)")

    # 4) Duplicate keys make citations ambiguous.
    seen: set[str] = set()
    dups = {k for k in (str(s.get("key", "")) for s in sections) if k in seen or seen.add(k)}
    if dups:
        warnings.append(f"{tag}: duplicate section keys: {sorted(dups)[:8]}")

    # 5) Unusual part count is worth eyeballing (SD surveys run ~5-6, DP ~11+annex).
    if len(parts) > 15:
        warnings.append(f"{tag}: {len(parts)} letter parts — possible heading over-promotion")

    # 6) Body paragraphs promoted to headings show up as very long "titles"
    #    (multi-sentence text where a clause title should be).
    long_titles = [s for s in sections if len(str(s.get("title", ""))) > 220]
    if long_titles:
        k = str(long_titles[0].get("key", "?"))
        warnings.append(f"{tag}: {len(long_titles)} section(s) with paragraph-length "
                        f"titles (body promoted to heading?), e.g. {k}")

    return errors, warnings


def as_dicts(doc) -> list[dict]:
    """ExtractedDoc.sections -> the plain dicts validate_sections expects."""
    return [{"key": s.key, "title": s.title, "level": s.level, "elements": s.elements}
            for s in doc.sections]
