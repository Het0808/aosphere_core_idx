"""Alias map + query→jurisdiction matching (pure; no index/data needed)."""

from aosphere_core_index.regions.aliases import ALIASES, match_aliases, named_jurisdictions

REGIONS = [
    "South Korea", "United Kingdom", "Germany", "France", "Japan", "India",
    "United States - California (Long Form)", "United States - New York",
    "Shareholding Disclosure — South Korea", "Shareholding Disclosure — United Kingdom",
]


def named(q):
    return named_jurisdictions(q, REGIONS)


# ---- eval-miss patterns this map exists to fix ------------------------------

def test_city_seoul():
    assert "South Korea" in named("What are the breach notification obligations in Seoul?")


def test_colloquial_britain_england():
    assert "United Kingdom" in named("Can we use tracking technologies in Britain?")
    assert "United Kingdom" in named("Do you need consent for email advertising in England?")


def test_regulator_typo_picp():
    assert "South Korea" in named("Do you need to notify data breaches to the PICP?")


def test_regulator_ico():
    assert "United Kingdom" in named("Has the ICO issued guidance on cookies?")


# ---- matching semantics ------------------------------------------------------

def test_exact_name_still_matches():
    assert "Germany" in named("cookie walls in Germany")


def test_us_state_by_state_name():
    assert "United States - California (Long Form)" in named("privacy law in California")


def test_qualified_product_regions_match_on_bare_name():
    # "UK" must also pull the Shareholding Disclosure — United Kingdom identity.
    got = named("major shareholding disclosure thresholds in the UK")
    assert "United Kingdom" in got
    assert "Shareholding Disclosure — United Kingdom" in got


def test_whole_word_no_substring_hits():
    # "uk" must not fire inside "ukraine"; "india" not inside "indiana".
    assert "United Kingdom" not in named("data transfers to ukraine")
    assert "India" not in named("privacy law in indiana")


def test_multiword_alias():
    assert match_aliases("rules in new delhi please") == {"India"}


def test_no_match_returns_empty():
    assert named("what is a data controller?") == set()


def test_alias_values_are_bare_names():
    # Alias targets must never be product-qualified identities.
    assert all(" — " not in v for v in ALIASES.values())
