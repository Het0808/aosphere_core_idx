"""Geo-default region grouping: jurisdiction -> region (continent group).

Five groups: Europe, Americas, Asia-Pacific, Middle East, Africa. US states are
handled by a prefix rule ("United States - *" -> Americas). Override this map to
match an aosphere-canonical taxonomy later without touching callers.
"""

from __future__ import annotations

import re

REGIONS = ["Europe", "Americas", "Asia-Pacific", "Middle East", "Africa", "Other"]

# ---- Products -------------------------------------------------------------
# The index is multi-product. A region's identity is product-qualified: Data
# Privacy (the original product) keeps its bare jurisdiction name for back-compat
# ("France"); every other product prefixes it with an em-dash delimiter
# ("Shareholding Disclosure — France"). The em-dash can't collide with US-state
# names, which use an ASCII hyphen ("United States - California").
DEFAULT_PRODUCT = "Data Privacy"
PRODUCT_DELIM = " — "
# Non-default products that carry a qualified region prefix (display name == prefix).
KNOWN_PRODUCTS = ["Shareholding Disclosure", "Marketing Restrictions - Asset Management"]
PRODUCTS = [DEFAULT_PRODUCT, *KNOWN_PRODUCTS]


# A corpus directory is numbered by the publisher's product id ("155_Data_Privacy").
# The number is theirs, not ours, and it is not part of any identity the index uses.
_PRODUCT_DIR_PREFIX = re.compile(r"^\d+_")


# The corpus names a Data Privacy job directory either "Israel__169421" or
# "Angola (Data Privacy)__178686" — both forms, side by side, in the same run. The index
# has only ever known the bare form, so the suffix has to come off before a jurisdiction
# becomes a region id.
_PRODUCT_SUFFIX = re.compile(r"\s*\(Data Privacy\)$")


def canonical_jurisdiction(name: str) -> str:
    """The jurisdiction a corpus folder names, as the index spells it.

    Of run 2026-08-21-02's 206 Data Privacy documents, 119 are folder-named
    "<Region> (Data Privacy)" and 87 are bare "<Region>". Left alone, the suffixed ones
    become regions of their own: promoting that run would have carried "Angola" forward
    from the live index untouched and added a SECOND "Angola (Data Privacy)" beside it,
    58 times over — stale content and fresh, both searchable, both plausible. Nothing
    catches it, because the superset check the cutover gate runs is satisfied by carrying
    the stale ones over, and the promoted regions really are all present.

    Stripping the suffix takes this run from 263 to 321 of 321 live regions updated, with
    exactly one genuinely new region left over. Note that no ALIAS map belongs here:
    `scripts/import_fresh_data.py` maps EU Member States -> European Union, Hong Kong SAR
    -> Hong Kong and Türkiye -> Turkey for a different data drop, and the live index spells
    all three the FIRST way. Applying it here orphans seven live regions.
    """
    return _PRODUCT_SUFFIX.sub("", name).strip()


def product_from_dir(name: str) -> str:
    """The clean product label for a corpus product directory.

    "155_Data_Privacy" -> "Data Privacy", "124_Marketing_Restrictions_-_Asset_Management"
    -> "Marketing Restrictions - Asset Management" (which is KNOWN_PRODUCTS[1] exactly).
    """
    return _PRODUCT_DIR_PREFIX.sub("", name).replace("_", " ")


def is_indexable_product_dir(name: str) -> bool:
    """Whether a corpus product directory names a product this index can actually serve.

    `PRODUCTS` is the allow-list, and it is the allow-list for a reason rather than a
    convenience: a region's identity is product-qualified by `qualified()`, so a product
    that is not in this list has no region id, no content.json and no rows — promoting its
    documents would publish gallery entries for content the search index cannot represent.
    Adding a product is therefore ONE edit, here, and everything downstream follows.

    Note the corpus carries near-namesakes that are deliberately NOT in the list —
    "170_Data_Privacy_(US_States)", "165_Data_Privacy_-_Snippets" — and they map to
    labels of their own, so they are excluded by this without a special case.
    """
    return product_from_dir(name) in PRODUCTS


def qualified(product: str, jurisdiction: str) -> str:
    """Product-qualified region name. Data Privacy stays bare for back-compat."""
    if not product or product == DEFAULT_PRODUCT:
        return jurisdiction
    return f"{product}{PRODUCT_DELIM}{jurisdiction}"


def split_region(name: str) -> tuple[str, str]:
    """(product, jurisdiction) for a region name — inverse of qualified()."""
    if PRODUCT_DELIM in name:
        product, jurisdiction = name.split(PRODUCT_DELIM, 1)
        return product, jurisdiction
    return DEFAULT_PRODUCT, name


def product_of(name: str) -> str:
    return split_region(name)[0]


def match_identities(name: str, identities) -> list[str]:
    """Region identities matching a possibly-imprecise name an LLM (or user) typed.

    Accepts: the exact identity ("Shareholding Disclosure — France"), the same with
    an ASCII hyphen instead of the em-dash, or a BARE jurisdiction name ("France" —
    matches every product's France within `identities`). Case-insensitive.
    """
    n = (name or "").strip().lower()
    if not n:
        return []

    def _norm(s: str) -> str:
        return " ".join(s.lower().replace("—", "-").split())

    exact = [i for i in identities if i.lower() == n]
    fuzzy = [i for i in identities if _norm(i) == _norm(name)]
    bare = [i for i in identities if split_region(i)[1].lower() == n]
    # Ordered union: exact identity first (so single-identity consumers like
    # read_clauses stay deterministic — bare 'France' reads the Data Privacy doc),
    # then delimiter-variant matches, then every product's <name> (so a search
    # filter of 'France' covers both products when both are in scope).
    return list(dict.fromkeys(exact + fuzzy + bare))


_BY_REGION: dict[str, list[str]] = {
    "Europe": [
        "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech Republic",
        "Denmark", "EU Member States", "Estonia", "European Union", "Finland", "France",
        "Georgia", "Germany", "Gibraltar", "Greece", "Guernsey", "Hungary", "Iceland",
        "Russian Federation",
        "Ireland", "Isle of Man", "Italy", "Jersey", "Latvia", "Liechtenstein",
        "Lithuania", "Luxembourg", "Malta", "Monaco", "Netherlands", "Norway", "Poland",
        "Portugal", "Romania", "Russia", "Serbia", "Slovakia", "Slovenia", "Spain",
        "Sweden", "Switzerland", "Turkey", "Ukraine", "United Kingdom",
    ],
    "Americas": [
        "Argentina", "Bahamas", "Bermuda", "Brazil", "British Virgin Islands", "Canada",
        "Canada (Ontario)", "Cayman Islands", "Chile", "Colombia", "Costa Rica",
        "Curaçao", "Ecuador", "Jamaica", "Mexico", "Panama", "Peru", "Puerto Rico",
        "Uruguay", "Venezuela",
        "Canada (Alberta, British Columbia, Ontario and Quebec)", "Dominican Republic",
        "El Salvador", "Guatemala", "Honduras", "Paraguay",
    ],
    "Asia-Pacific": [
        "Australia", "Bangladesh", "China", "Hong Kong", "Hong Kong SAR", "India",
        "Indonesia", "Japan", "Kazakhstan (excluding AIFC)", "Malaysia",
        "Marshall Islands", "New Zealand", "Pakistan", "Papua New Guinea", "Philippines",
        "Singapore", "South Korea", "Sri Lanka", "Taiwan", "Thailand", "Vietnam",
        "Brunei", "Macao SAR",
    ],
    "Middle East": [
        "Bahrain", "Dubai International Financial Centre", "Israel", "Jordan", "Kuwait",
        "Lebanon", "Oman", "Qatar", "Qatar Financial Centre", "Saudi Arabia",
        "United Arab Emirates",
        # Marketing Restrictions - Asset Management names these differently, and an unknown
        # name silently groups under "Other" rather than failing, so it is invisible.
        "Abu Dhabi Global Market (ADGM)", "Qatar (excluding Qatar Financial Centre)",
        "United Arab Emirates (excluding the DIFC and the ADGM)",
    ],
    "Africa": [
        "Algeria", "Angola", "Botswana", "Egypt", "Ghana", "Ivory Coast", "Kenya",
        "Mauritius", "Morocco", "Namibia", "Niger", "Nigeria", "South Africa", "Tunisia",
        "Uganda", "Zambia", "Zimbabwe",
    ],
}

_JURISDICTION_TO_REGION: dict[str, str] = {
    j: region for region, members in _BY_REGION.items() for j in members
}


def region_for(jurisdiction: str) -> str:
    """Return the region group (continent) for a jurisdiction (geo default). Accepts a
    product-qualified name ("Shareholding Disclosure — France") or a bare one — the
    product prefix is stripped so grouping is common across products."""
    _, jur = split_region(jurisdiction)
    if jur.startswith("United States"):
        return "Americas"
    return _JURISDICTION_TO_REGION.get(jur, "Other")
