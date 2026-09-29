"""Mapping of aosphere DOCX Word styles to structural roles.

Derived by inspecting the France Data Privacy survey (DOC 175201). The document
encodes its structure in custom 'DP*' styles, not standard Word headings:

  - DPSectionHead  -> Part heading (level 0, lettered A..K + Glossary/Annex)
  - DPNum1..DPNum4 -> nested numbered sections (the clause hierarchy)
  - DPBodytxt*     -> body prose
  - DPBullet*      -> bullet list items
  - DPQuestionBody -> survey question text
  - DPReaderNote   -> editorial reader notes
"""

from __future__ import annotations

import re

# Heading style -> hierarchy level (0 = Part, deeper = larger number).
# Data Privacy encodes its clause hierarchy in DPNum1..4.
HEADING_LEVELS: dict[str, int] = {
    "DPSectionHead": 0,
    "DPNum1": 1,
    "DPNum2": 2,
    "DPNum3": 3,
    "DPNum4": 4,
    # Some jurisdictions nest deeper (e.g. Austria uses DPNum5-7).
    "DPNum5": 5,
    "DPNum6": 6,
    "DPNum7": 7,
    "DPNum8": 8,
    "DPNum9": 9,
}

# DPSectionHead + DPNum1..4 are aosphere-wide heading styles (they don't match the
# generic numbered-style regex below), so they're recognised for EVERY product —
# some Shareholding memos are authored with the DPNum template (e.g. Cyprus). Other
# templates' numbered styles (DPLevel N / Level N / Heading N) are handled generically
# in docx_extract, so no per-product curation is normally needed. Add a product entry
# here only to force-map a style the generic rule can't infer.
HEADING_LEVELS_BY_PRODUCT: dict[str, dict[str, int]] = {}


def heading_levels_for(product: str | None) -> dict[str, int]:
    """Curated heading-style -> level map: the aosphere base styles (always) plus any
    per-product overrides. Numbered styles not listed here are classified generically."""
    return {**HEADING_LEVELS, **HEADING_LEVELS_BY_PRODUCT.get(product or "", {})}


# Generic fallback for author-specific templates that don't use the curated styles:
# a style name that encodes a depth (Level N / DPLevel N / AltLevel N / Heading N).
GENERIC_HEADING_RE = re.compile(r"^(?:DP|Alt)?Level ?(\d+)$|^Heading ?(\d+)$", re.I)
# A generic numbered style counts as a heading only if its paragraphs are, ON
# MEDIAN across the document, short. Classifying per-STYLE (not per-paragraph)
# means a heading style's occasional long title is still captured, while a body
# style that reuses a "Level N" name stays body. Validated on Shareholding docs:
# heading styles run ~14-40 median, body styles ~90-320.
GENERIC_HEADING_MEDIAN_MAX = 60
# …but a per-paragraph ceiling still applies: a multi-sentence, hundreds-of-chars
# paragraph in a mostly-short style is BODY, not a title (seen in Bahrain SD:
# a ~700-char paragraph became clause A3.1.1.4's "title", leaving the clause
# with no content). Long titles up to this cap are still allowed.
GENERIC_HEADING_TITLE_MAX = 200


def is_plausible_title(text: str) -> bool:
    """Whether a generic-heading-styled paragraph is plausibly a TITLE rather than
    a body paragraph the author styled with the heading style. Long text, or
    moderately long text that reads as sentences (terminal period), is body."""
    t = (text or "").strip()
    if len(t) > GENERIC_HEADING_TITLE_MAX:
        return False
    if len(t) > 120 and t.endswith("."):
        return False  # sentence-shaped: titles this long don't end with a period
    return True
# A numbered style NOT classified as a heading style (mixed use: short sub-headings
# + long body, e.g. "Level 4") still yields headings for its individually SHORT
# paragraphs. This per-paragraph catch complements the per-style median rule.
GENERIC_HEADING_PARA_MAX = 90


def generic_heading_level(style: str) -> int | None:
    """Level encoded by a generic numbered style name (Level1->0, Level2->1, ...),
    or None if the style name doesn't match the numbered-heading pattern. The
    caller decides whether the style is actually a heading (see the per-style
    median-length classification in docx_extract)."""
    m = GENERIC_HEADING_RE.match((style or "").strip())
    return max(0, int(m.group(1) or m.group(2)) - 1) if m else None


# Products whose template encodes the FULL hierarchy in curated styles
# (DPSectionHead/DPNum*): text-numbering promotion is disabled for them — their
# numbered body lines are list items, and promoting them shifts real clause keys
# (e.g. Singapore's marketing-code items becoming J7..J12).
NUMBERING_PROMOTION_SKIP_PRODUCTS: frozenset[str] = frozenset({"Data Privacy"})


def numbering_promotion_enabled(product: str | None) -> bool:
    return (product or "") not in NUMBERING_PROMOTION_SKIP_PRODUCTS


# Max heading nesting level per product. Shareholding Disclosure surveys style deep
# numbered list-items (definitions, directive lists — "In-scope shares;") as headings
# via deep Level-N styles, nesting to L5-L12 and splitting content off the citable
# mid-level clause (leaving it an empty heading). Gold never cites beyond level 3, so
# cap there and let deeper NON-paren content fall to body. Paren sub-items ("(a)") are
# exempt — they're citable (e.g. A4.3.3(e)). Products absent here are effectively
# uncapped (Data Privacy's curated DPNum* max out at level 4).
MAX_HEADING_LEVEL_BY_PRODUCT: dict[str, int] = {"Shareholding Disclosure": 3}
MAX_HEADING_LEVEL_DEFAULT = 8


def max_heading_level(product: str | None) -> int:
    return MAX_HEADING_LEVEL_BY_PRODUCT.get(product or "", MAX_HEADING_LEVEL_DEFAULT)


# Some templates style genuine headings as BODY but keep a strong structural
# numbering prefix ("A.  Substantial Shareholding", "3.4.17  ETFs"). Promote such
# SHORT, non-sentence paragraphs to headings. Only unambiguous section forms
# (letter-part, dotted-decimal) -- NOT "(a)"/"(i)" list markers, which are too
# easily confused with body list items (and inflate clean templates like DP).
# A BARE integer requires a trailing dot ("2.  Overview") — an integer followed
# by a plain space is how cover DATES ("14 November 2025") and contact ADDRESSES
# ("1 Queen Street", "5 Avenue J.F. Kennedy") begin, and promoting those created
# a spurious Part A that shifted every real part's letter in 42 of 105
# Shareholding Disclosure jurisdictions.
_NUM_HEADING = re.compile(r"^\s*(?:([A-Z])\.|(\d+(?:\.\d+)+)\.?|(\d+)\.)[ \t]+\S")


def numbering_heading_level(text: str) -> int | None:
    """Depth implied by a leading section-number ("A."->0, "1."->1, "1.1"->2,
    "3.4.17"->3, capped at 4), or None. Rejects sentence-like text (ends with
    terminal punctuation) so numbered body sentences aren't promoted."""
    text = (text or "").strip()
    if not text or text[-1] in ".;:,":
        return None
    # A trailing tab-separated number is a TOC entry's page number ("2.\tSUMMARY\t31")
    # — TOC lines in custom styles (e.g. "TOC2_1") must not become headings.
    if re.search(r"\t\d+\s*$", text):
        return None
    m = _NUM_HEADING.match(text)
    if not m:
        return None
    if m.group(1):
        return 0  # letter part "A."
    # Numbered LIST ITEMS ("1.\tevidence of their appointment; and") look like
    # numbered headings but start lowercase — real headings are Title Case.
    rest = text[m.end() - 1:].lstrip()
    if rest and rest[0].isalpha() and rest[0].islower():
        return None
    if m.group(3):
        return 1  # "2.  Overview" — dotted bare integer
    parts = m.group(2).split(".")
    return min(len(parts), 4)


# Content style -> element kind (text attached to the current section).
CONTENT_KINDS: dict[str, str] = {
    "DPBodytxt": "body",
    "DPBodytxt1": "body",
    "DPBodytxt2": "body",
    "AODocTxt": "body",
    "Normal": "body",
    "Normal (Web)": "body",
    "DPContactBody": "body",
    "DPBullet": "bullet",
    "DPBullet1": "bullet",
    "List Paragraph": "bullet",
    "DPQuestionBody": "question",
    "DPReaderNote": "readernote",
}

# Front-matter / table-of-contents styles to skip from the body.
SKIP_STYLES: frozenset[str] = frozenset(
    {
        "toc 1",
        "toc 2",
        "toc 3",
        "DPCoverTitle",
        "DPSubtitle",
        "DPDate",
        "DPCoverDate",     # SD cover template variant ("14 November 2025")
        "DPContactName",
        "DPContactHead",   # "Reporting Counsel:" / "Contact:" cover blocks
        "DPTOCHead",
        "DPDisclaimer",
    }
)
