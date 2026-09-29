"""Every jurisdiction the index holds must group under a real continent.

region_for() falls back to "Other" for a name it does not know, which never raises — so a product
whose jurisdictions are spelled differently ends up in a catch-all bucket in the UI and nobody
notices. Marketing Restrictions - Asset Management arrived with 12 of 88 like that: "Abu Dhabi
Global Market (ADGM)", "Qatar (excluding Qatar Financial Centre)", "Canada (Alberta, British
Columbia, Ontario and Quebec)", "Russian Federation", "Macao SAR" — every one a variant of a
jurisdiction the map already listed under some other spelling.
"""

import pytest

from aosphere_core_index.regions.region_map import PRODUCTS, qualified, region_for

MRAM = "Marketing Restrictions - Asset Management"


def test_the_products_the_index_serves_are_in_the_known_order():
    """Order drives the UI's product filter; an unlisted product sorts to the end by accident."""
    assert PRODUCTS[:3] == ["Data Privacy", "Shareholding Disclosure", MRAM]


@pytest.mark.parametrize("jurisdiction, continent", [
    ("Abu Dhabi Global Market (ADGM)", "Middle East"),
    ("Qatar (excluding Qatar Financial Centre)", "Middle East"),
    ("United Arab Emirates (excluding the DIFC and the ADGM)", "Middle East"),
    ("Canada (Alberta, British Columbia, Ontario and Quebec)", "Americas"),
    ("Dominican Republic", "Americas"),
    ("El Salvador", "Americas"),
    ("Guatemala", "Americas"),
    ("Honduras", "Americas"),
    ("Paraguay", "Americas"),
    ("Brunei", "Asia-Pacific"),
    ("Macao SAR", "Asia-Pacific"),
    ("Russian Federation", "Europe"),
])
def test_product_specific_jurisdiction_spellings_group_correctly(jurisdiction, continent):
    assert region_for(qualified(MRAM, jurisdiction)) == continent


def test_grouping_is_shared_across_products():
    """A jurisdiction groups the same way whichever product it arrives under — the UI shows ONE
    continent tree for all of them."""
    for j in ("Austria", "Singapore", "Brazil"):
        assert region_for(qualified(MRAM, j)) == region_for(j)
