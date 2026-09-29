"""A corpus folder name is not the index's spelling of a jurisdiction.

Run 2026-08-21-02 names its Data Privacy job directories BOTH ways in the same run —
"Israel__169421" and "Angola (Data Privacy)__178686", 87 bare and 119 suffixed. The index
has only ever known the bare form. Promoting the raw folder name therefore does not update
"Angola"; it carries the live "Angola" forward untouched from the seed and adds a second
"Angola (Data Privacy)" beside it. Fifty-eight times, on one run.

Nothing downstream notices. `stage_index_verify`'s superset check is SATISFIED by the stale
regions being carried over, and its "promoted regions are in the index" check is satisfied
because the new names really are all there. Both halves of the duplicate are present, both
are searchable, and the older one is silently wrong.

Measured against the live index (2026-08-20-titan-hybrid-r5, 321 regions):
    raw folder name   263 of 321 updated, 59 added, 58 left stale
    canonicalised     321 of 321 updated,  1 added,  0 left stale
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
pytest.importorskip("numpy")

import promote_index as PI  # noqa: E402
from aosphere_core_index.regions.region_map import (  # noqa: E402
    canonical_jurisdiction, qualified)


def row(jur, product="Data Privacy", doc_id="1"):
    return {"product": product, "jurisdiction": jur, "doc_id": doc_id,
            "job": f"x/{jur}__{doc_id}", "gate": "pass", "worst_score": 90.0}


# ---------------- the canonicaliser ----------------
def test_the_suffix_comes_off():
    assert canonical_jurisdiction("Angola (Data Privacy)") == "Angola"


def test_a_bare_name_is_untouched():
    assert canonical_jurisdiction("Israel") == "Israel"


def test_a_us_state_keeps_its_ascii_hyphen():
    # "United States - California" is an ASCII hyphen, not the product em-dash, and has
    # nothing to do with the suffix. It must survive verbatim.
    assert canonical_jurisdiction("United States - California") == "United States - California"


def test_the_suffix_is_only_stripped_at_the_END():
    assert canonical_jurisdiction("Data Privacy (Data Privacy) Ltd") == "Data Privacy (Data Privacy) Ltd"


@pytest.mark.parametrize("name", ["EU Member States", "Hong Kong SAR", "Türkiye"])
def test_no_alias_map_is_applied(name):
    """import_fresh_data maps these to European Union / Hong Kong / Turkey for a different
    data drop. The live index spells all three THIS way; aliasing here orphans 7 regions."""
    assert canonical_jurisdiction(name) == name
    assert canonical_jurisdiction(f"{name} (Data Privacy)") == name


# ---------------- the region a promoted row becomes ----------------
def test_both_folder_forms_reach_the_same_region():
    assert PI.region_of(row("Angola (Data Privacy)")) == PI.region_of(row("Angola"))
    assert PI.region_of(row("Angola")) == "Angola"


def test_a_non_default_product_stays_qualified():
    r = row("France (Data Privacy)", product="Shareholding Disclosure")
    assert PI.region_of(r) == qualified("Shareholding Disclosure", "France")
    assert PI.region_of(r) == "Shareholding Disclosure — France"


def test_the_two_forms_collide_into_one_region_and_are_RESOLVED_not_duplicated():
    """The merge is what makes them one region, so the collision rule must see them."""
    rows = [row("Angola", doc_id="1"), row("Angola (Data Privacy)", doc_id="2")]
    kept, collisions = PI.resolve_collisions(rows, None)
    assert len(kept) == 1, "two spellings of one jurisdiction must not both be promoted"
    assert len(collisions) == 1 and collisions[0]["region"] == "Angola"


def test_the_regression_this_exists_to_prevent():
    """With the raw folder name, these are two regions; the live one is never updated."""
    raw = {qualified(r["product"], r["jurisdiction"])
           for r in (row("Angola"), row("Angola (Data Privacy)"))}
    assert len(raw) == 2, "sanity: the raw names really are distinct"
    assert len({PI.region_of(r)
                for r in (row("Angola"), row("Angola (Data Privacy)"))}) == 1
