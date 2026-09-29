"""Numbering-marker detection shared by every MinerU-block consumer.

Pure text-pattern classification — no knowledge of any target tree shape.
`kg_export.py` (generic KG chunks) needs to recognize "A." / "1.1" / "(a)" /
"(i)" markers, in list items and in plain paragraphs alike, then place each at
the right depth. `marker_level()` below is that shared placement logic: each family
nests relative to the nearest ancestor of a genuinely higher-order family —
not whatever's merely deepest on the stack — which is what makes "A." -> "1."
-> "1.1" -> "(i)" nest one level at a time (each finds its real container,
however deep unrelated content in between currently is), while a lone
"1. Methodology" -> "1.1 Data Collection" with NO enclosing "A." still starts
at the top rather than being pushed down a level for no reason. Consecutive
same-family markers ("1.", "2.", "3." or "A.", "B.") land as siblings instead
of each nesting under the last, for the same reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A bare integer REQUIRES the trailing dot ("2. Overview") — without it, "14
# November 2025" (a cover date) and "1 Queen Street" (an address) parse as
# valid decimal markers too, which is exactly the historical bug class that
# promoted front matter to a Part and shifted every subsequent letter. A
# dotted multi-part number ("1.1", "1.1.1") is unambiguous without one.
_DECIMAL_RE = re.compile(r"^(?:(\d+(?:\.\d+)+)\.?|(\d+)\.)\s+\S")
_ALPHA_RE = re.compile(r"^([A-Z])\.\s+\S")
_PAREN_RE = re.compile(r"^\(([a-zA-Z]+)\)\s*\S")
_ROMAN_RE = re.compile(r"^[ivxlcdm]+$", re.I)
_AMBIGUOUS_SINGLE = frozenset("ivx")  # (i)/(v)/(x): ambiguous between alpha and roman
# A TOC line matches a real marker ("1. Restrictions on Investment 34") but
# ends in a bare trailing page number — verified against a real MinerU list
# block (vlm-engine puts the whole TOC in one list, one entry per item).
TOC_TAIL_RE = re.compile(r"\s\d{1,4}$")


@dataclass
class Marker:
    family: str  # "decimal" | "upper_alpha" | "paren_alpha" | "paren_roman"
    depth: int   # decimal: dot count ("1.1"->2); everything else: always 1


def detect_marker(text: str, *, roman_open: bool, parent_is_alpha: bool) -> Marker | None:
    """Recognized marker at the start of `text`, or None.

    `roman_open`: a roman-numbered item has already been opened in the current
    list run (scope that per list, not per document — see each consumer).
    `parent_is_alpha`: the section/item this text would nest under was itself
    opened by a paren-alpha ("(a)") marker — the common a/b/c -> i/ii/iii
    nesting pattern, used to resolve the single-letter roman/alpha ambiguity.
    """
    text = (text or "").strip()
    if TOC_TAIL_RE.search(text):
        return None  # a TOC entry, not a real heading — e.g. "1. Overview 3"
    m = _DECIMAL_RE.match(text)
    if m:
        num = m.group(1) or m.group(2)
        return Marker("decimal", len(num.split(".")))
    if _ALPHA_RE.match(text):
        return Marker("upper_alpha", 1)
    m = _PAREN_RE.match(text)
    if not m:
        return None
    token = m.group(1).lower()
    if len(token) == 1:
        if token in _AMBIGUOUS_SINGLE:
            return Marker("paren_roman" if (roman_open or parent_is_alpha) else "paren_alpha", 1)
        return Marker("paren_alpha", 1)
    return Marker("paren_roman" if _ROMAN_RE.match(token) else "paren_alpha", 1)


# Outer-to-inner ordering: a decimal item nests under the nearest upper_alpha
# ancestor (if any); a paren_alpha item nests under the nearest decimal-or-
# upper_alpha ancestor; a paren_roman item nests under the nearest
# paren_alpha-or-higher ancestor — roman is genuinely ONE level deeper than
# alpha, not a peer of it: real documents use "(a) ... when: (i) ... (ii)
# ..." with roman as (a)'s own sub-items. Ranking them equal was verified
# wrong against a real document with two UNRELATED roman lists five pages
# apart, each under a different (a)/(f)-style container — ranking them equal
# made both skip past their true container to the same distant decimal
# ancestor, colliding on the same clause key. Unmarked frames (plain
# text_level headings, or the document root) rank as -1 — always "more
# outer" than any marker.
_FAMILY_RANK = {"upper_alpha": 0, "decimal": 1, "paren_alpha": 2, "paren_roman": 3}


def marker_level(marker: Marker, open_frames: list[tuple[int, str | None]], *, root_level: int = 0) -> int:
    """The level `marker` should open at, given the (level, family) of every
    currently-open ancestor. Walks from the deepest frame inward looking for
    the nearest one that OUTRANKS this marker's family (a decimal marker
    looks for upper_alpha-or-unmarked; a paren_alpha marker looks for
    decimal/upper_alpha/unmarked; a paren_roman marker looks for
    paren_alpha/decimal/upper_alpha/unmarked) and nests relative to THAT — not
    whatever merely happens to be deepest. This is what makes a new "B."
    correctly pop all the way back to the top even if the last thing
    processed was a deeply-nested "(v)", and what makes "1. Overview"
    self-anchor at the top when no "A." exists rather than nesting under one
    that happens to be open in some OTHER unrelated document.

    An un-numbered heading (family=None — "Part A", "APPENDIX 1", a plain
    text_level heading with no recognized marker) ranks as the most-outer
    tier of all, same as the document root: numbered content found under it
    is expected to nest inside it (a plain "Part A" followed by "1. Intro" is
    the common case). Known, accepted narrow miss: MinerU sometimes tags
    front-matter (a cover-page title, "Contents") at the SAME text_level as a
    real Part, with nothing to distinguish "this is front matter" from "this
    is a genuine container" using only (level, family) — such a heading gets
    treated as a real container too, so a Part immediately following it can
    end up nested one level under it rather than beside it. Not fixed: doing
    so would break the far more common legitimate-container case above, and
    this is strictly a display/depth cosmetic (breadcrumb has one extra
    level), not a mis-attribution of content to the wrong clause.

    `root_level` is the fallback once the whole stack is exhausted without
    finding ANY open frame at all — the caller's own "top of everything"
    (kg_export.py's synthetic root sits at level 0 and is always present in
    `open_frames`, so this never actually triggers there; a caller with no
    synthetic root can pass -1 so a bare Part with nothing open yet still
    resolves to level 0).
    """
    rank = _FAMILY_RANK[marker.family]
    base = root_level
    for level, family in reversed(open_frames):
        if _FAMILY_RANK.get(family, -1) < rank:
            base = level
            break
    return base + (marker.depth if marker.family == "decimal" else 1)


def split_list_items(block: dict) -> list[str]:
    """One string per enumerated item. `list_items` is MinerU's real field
    name (verified against a real vlm-engine block); `items` is kept as a
    fallback in case another backend version uses it, and a bare `text` split
    is the last resort."""
    for key in ("list_items", "items"):
        items = block.get(key)
        if isinstance(items, list) and items:
            return [str(i).strip() for i in items if str(i).strip()]
    return [ln.strip() for ln in (block.get("text") or "").splitlines() if ln.strip()]


def list_has_roman_evidence(items: list[str]) -> bool:
    """Whether ANY item in a list has an unambiguous multi-character roman
    marker ("(ii)", "(iii)") — used to seed `roman_open` for the WHOLE list
    before processing its first item. Without this, a list starting "(i),
    (ii), (iii)" would misclassify "(i)" as paren_alpha (roman_open is only
    set true AFTER a confirmed roman item, and "(i)" alone is ambiguous with
    nothing preceding it to disambiguate) — then, because paren_roman ranks
    deeper than paren_alpha, "(ii)" would wrongly nest under the misclassified
    "(i)" instead of beside it. Looking ahead within the same list sidesteps
    the chicken-and-egg problem entirely."""
    for item in items:
        m = _PAREN_RE.match((item or "").strip())
        if m and len(m.group(1)) > 1 and _ROMAN_RE.match(m.group(1)):
            return True
    return False
