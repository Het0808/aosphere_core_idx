"""Jurisdiction aliases + query→jurisdiction matching (pure, dependency-free).

Maps colloquial names, major cities, and privacy regulators found in user queries
to the canonical (bare, unqualified) jurisdiction names used by the index, e.g.
"Seoul" → "South Korea", "Britain" → "United Kingdom", "PIPC" → "South Korea".

`named_jurisdictions(query, regions)` is the single entry point used by the
search path: it whole-word-matches jurisdiction names, US-state names, and these
aliases against the query, and returns the matching region identities — including
product-qualified ones ("Shareholding Disclosure — France"), which are matched on
their bare jurisdiction part.

Rules for adding aliases:
- lowercase keys; values are canonical bare jurisdiction names (region_map style)
- whole-word matched, so short keys are safe from substring hits ("uk" ≠ "ukraine")
- OMIT ambiguous terms (e.g. "us", "opc", "cnpd", "datatilsynet", "pdpc" is kept
  for Singapore as the dominant usage; drop it if Thailand queries misfire)
"""

from __future__ import annotations

import re

from aosphere_core_index.regions.region_map import split_region

ALIASES: dict[str, str] = {
    # -- abbreviations / colloquial country names ------------------------------
    "uk": "United Kingdom", "gb": "United Kingdom", "britain": "United Kingdom",
    "great britain": "United Kingdom", "england": "United Kingdom",
    "scotland": "United Kingdom", "wales": "United Kingdom",
    "northern ireland": "United Kingdom",
    "eu": "European Union", "european commission": "European Union",
    "uae": "United Arab Emirates", "emirates": "United Arab Emirates",
    "difc": "Dubai International Financial Centre",
    "qfc": "Qatar Financial Centre",
    "hk": "Hong Kong",
    "korea": "South Korea", "republic of korea": "South Korea", "s. korea": "South Korea",
    "prc": "China", "mainland china": "China",
    "czechia": "Czech Republic",
    "türkiye": "Turkey", "turkiye": "Turkey",
    "nz": "New Zealand",
    "aus": "Australia", "oz": "Australia",
    "ksa": "Saudi Arabia",
    "roi": "Ireland",
    "dc": "United States - District of Columbia",
    "nyc": "United States - New York",
    # -- capitals / major cities (indexed jurisdictions only) ------------------
    "london": "United Kingdom", "paris": "France", "berlin": "Germany",
    "munich": "Germany", "frankfurt": "Germany", "madrid": "Spain",
    "barcelona": "Spain", "rome": "Italy", "milan": "Italy",
    "vienna": "Austria", "brussels": "Belgium", "lisbon": "Portugal",
    "athens": "Greece", "prague": "Czech Republic", "budapest": "Hungary",
    "warsaw": "Poland", "stockholm": "Sweden", "oslo": "Norway",
    "copenhagen": "Denmark", "helsinki": "Finland", "dublin": "Ireland",
    "zurich": "Switzerland", "geneva": "Switzerland", "bern": "Switzerland",
    "istanbul": "Turkey", "ankara": "Turkey", "belgrade": "Serbia",
    "bucharest": "Romania", "bratislava": "Slovakia", "ljubljana": "Slovenia",
    "reykjavik": "Iceland", "nicosia": "Cyprus", "luxembourg city": "Luxembourg",
    "seoul": "South Korea", "tokyo": "Japan", "beijing": "China",
    "shanghai": "China", "taipei": "Taiwan", "singapore city": "Singapore",
    "bangkok": "Thailand", "jakarta": "Indonesia", "kuala lumpur": "Malaysia",
    "manila": "Philippines", "hanoi": "Vietnam", "ho chi minh city": "Vietnam",
    "mumbai": "India", "delhi": "India", "new delhi": "India",
    "bangalore": "India", "bengaluru": "India",
    "sydney": "Australia", "melbourne": "Australia", "canberra": "Australia",
    "wellington": "New Zealand", "auckland": "New Zealand",
    "dhaka": "Bangladesh", "karachi": "Pakistan", "islamabad": "Pakistan",
    "toronto": "Canada", "ottawa": "Canada", "vancouver": "Canada",
    "montreal": "Canada", "quebec": "Canada",
    "buenos aires": "Argentina", "sao paulo": "Brazil", "são paulo": "Brazil",
    "brasilia": "Brazil", "rio de janeiro": "Brazil", "mexico city": "Mexico",
    "santiago": "Chile", "bogota": "Colombia", "bogotá": "Colombia",
    "lima": "Peru", "montevideo": "Uruguay", "san jose": "Costa Rica",
    "dubai": "United Arab Emirates", "abu dhabi": "United Arab Emirates",
    "riyadh": "Saudi Arabia", "jeddah": "Saudi Arabia",
    "tel aviv": "Israel", "jerusalem": "Israel", "beirut": "Lebanon",
    "muscat": "Oman", "manama": "Bahrain",
    "cairo": "Egypt", "casablanca": "Morocco", "rabat": "Morocco",
    "algiers": "Algeria", "accra": "Ghana", "nairobi": "Kenya",
    "abidjan": "Ivory Coast", "luanda": "Angola",
    "johannesburg": "South Africa", "cape town": "South Africa",
    "pretoria": "South Africa",
    # -- privacy regulators (unambiguous acronyms/names only) ------------------
    "ico": "United Kingdom",                     # Information Commissioner's Office
    "cnil": "France",
    "bfdi": "Germany",
    "aepd": "Spain",
    "garante": "Italy",
    "edpb": "European Union", "edps": "European Union",
    "uodo": "Poland",
    "imy": "Sweden",
    "kvkk": "Turkey",
    "pipc": "South Korea", "picp": "South Korea",  # picp = common typo of pipc
    "ppc": "Japan",
    "pcpd": "Hong Kong",
    "pdpc": "Singapore",
    "oaic": "Australia",
    "anpd": "Brazil",
    "inai": "Mexico",
    "sdaia": "Saudi Arabia",
    "information regulator": "South Africa",
}

# US-state-derived terms too generic to safely match.
TERM_SKIP = {"federal", "states and territories"}

# One compiled alternation over all aliases (longest-first so "new delhi" wins
# over "delhi"). Whole-word via \b; em-dash/period keys still match on \b edges.
_ALIAS_RX = re.compile(
    r"\b(?:" + "|".join(re.escape(a) for a in sorted(ALIASES, key=len, reverse=True)) + r")\b"
)


def match_aliases(query: str) -> set[str]:
    """Canonical bare jurisdiction names whose alias appears in the query."""
    return {ALIASES[m] for m in _ALIAS_RX.findall(f" {query.lower()} ")}


def _match_term(bare: str) -> str:
    """The whole-word term a bare jurisdiction name is matched by: the state name
    for US states ('United States - California (Long Form)' → 'california'),
    the lowercased name otherwise."""
    if bare.startswith("United States - "):
        return re.sub(r"\s*\(.*\)$", "", bare.split(" - ", 1)[1]).strip().lower()
    return bare.lower()


def named_jurisdictions(query: str, regions) -> set[str]:
    """Region identities (possibly product-qualified) explicitly named in the query
    — by jurisdiction name, US-state name, or alias (city/regulator/colloquial)."""
    ql = f" {query.lower()} "
    by_bare: dict[str, set[str]] = {}
    for ident in regions:
        by_bare.setdefault(split_region(ident)[1], set()).add(ident)
    named: set[str] = set()
    for bare, idents in by_bare.items():
        term = _match_term(bare)
        if term and term not in TERM_SKIP and re.search(rf"\b{re.escape(term)}\b", ql):
            named |= idents
    for bare in match_aliases(ql):
        named |= by_bare.get(bare, set())
    return named
