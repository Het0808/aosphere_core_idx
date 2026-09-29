"""Product-qualified region identity round-trip + region grouping."""

from aosphere_core_index.regions.region_map import (
    DEFAULT_PRODUCT,
    qualified,
    region_for,
    split_region,
)


def test_default_product_stays_bare():
    assert qualified(DEFAULT_PRODUCT, "France") == "France"
    assert qualified("", "France") == "France"


def test_other_product_is_qualified():
    assert qualified("Shareholding Disclosure", "France") == "Shareholding Disclosure — France"


def test_split_roundtrip():
    for product, jur in [
        (DEFAULT_PRODUCT, "France"),
        ("Shareholding Disclosure", "United Kingdom"),
        (DEFAULT_PRODUCT, "United States - California (Long Form)"),
    ]:
        assert split_region(qualified(product, jur)) == (product, jur)


def test_us_state_hyphen_does_not_split():
    # ASCII hyphen (US states) must not be confused with the em-dash delimiter.
    assert split_region("United States - New York") == (DEFAULT_PRODUCT, "United States - New York")


def test_region_for():
    assert region_for("France") == "Europe"
    assert region_for("United States - California (Long Form)") == "Americas"
    assert region_for("South Korea") == "Asia-Pacific"
    assert region_for("Atlantis") == "Other"


IDENTITIES = ["France", "Australia", "Shareholding Disclosure — France",
              "Shareholding Disclosure — Australia", "United States - New York"]


def test_match_identities_exact():
    from aosphere_core_index.regions.region_map import match_identities
    assert match_identities("Shareholding Disclosure — France", IDENTITIES) == [
        "Shareholding Disclosure — France"]


def test_match_identities_hyphen_for_emdash():
    # LLMs routinely type an ASCII hyphen where the identity uses an em-dash
    from aosphere_core_index.regions.region_map import match_identities
    assert match_identities("Shareholding Disclosure - France", IDENTITIES) == [
        "Shareholding Disclosure — France"]


def test_match_identities_bare_name_matches_all_products():
    from aosphere_core_index.regions.region_map import match_identities
    assert set(match_identities("france", IDENTITIES)) == {
        "France", "Shareholding Disclosure — France"}


def test_match_identities_bare_name_within_product_scope():
    # the actual bug: SD-only selection + agent typing 'Australia'
    from aosphere_core_index.regions.region_map import match_identities
    sd_only = [i for i in IDENTITIES if i.startswith("Shareholding")]
    assert match_identities("Australia", sd_only) == ["Shareholding Disclosure — Australia"]


def test_match_identities_us_state_hyphen_untouched():
    from aosphere_core_index.regions.region_map import match_identities
    assert match_identities("United States - New York", IDENTITIES) == [
        "United States - New York"]


def test_match_identities_unknown_and_empty():
    from aosphere_core_index.regions.region_map import match_identities
    assert match_identities("Atlantis", IDENTITIES) == []
    assert match_identities("", IDENTITIES) == []
