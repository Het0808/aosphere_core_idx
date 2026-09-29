"""Pure query-builder / filter / highlight-reshape helpers for the OpenSearch
Spotlight backend (no live cluster or numpy/fastapi needed)."""

from aosphere_core_index.service import entity_backend as eb
from aosphere_core_index.service import entity_index as ei

# ------------------------------------------------------------------- build_query

def test_build_query_boost_ladder_and_msm():
    q = eb.build_query("netting", filters=[])["bool"]
    assert q["minimum_should_match"] == 1
    assert q["filter"] == []
    boosts = [c.get("match", {}).get("title", {}).get("boost")
              for c in q["should"] if "match" in c and "title" in c["match"]]
    assert 5 in boosts  # title text clause boost
    # phrase-over-metadata is the top of the ladder (boost 8)
    phrase = next(c for c in q["should"] if c.get("multi_match", {}).get("type") == "phrase")
    assert phrase["multi_match"]["boost"] == 8
    assert phrase["multi_match"]["fields"] == ei.META_TEXT_PATHS


def test_build_query_title_is_fuzzy():
    q = eb.build_query("singaore", filters=[])["bool"]
    title = next(c for c in q["should"] if c.get("match", {}).get("title"))
    assert title["match"]["title"]["fuzziness"] == "AUTO"


def test_build_query_prefix_clauses_cover_small_vocab():
    q = eb.build_query("ab", filters=[])["bool"]
    prefix_fields = {next(iter(c["match"])) for c in q["should"]
                     if "match" in c and next(iter(c["match"])).endswith(".prefix")}
    assert "title.prefix" in prefix_fields
    for p in ei.META_PREFIX_PATHS:
        assert f"{p}.prefix" in prefix_fields


def test_build_query_passes_filters_through():
    filters = [{"terms": {"entityType": ["question"]}}]
    assert eb.build_query("x", filters)["bool"]["filter"] == filters


# ---------------------------------------------------------------------- filters

def test_jurisdiction_filter_keeps_agnostic_docs():
    f = eb._jurisdiction_filter(["Singapore"])["bool"]
    assert f["minimum_should_match"] == 1
    assert {"terms": {"metadata.jurisdictions.kw": ["Singapore"]}} in f["should"]
    # jurisdiction-agnostic docs (no metadata.jurisdictions) must still pass
    assert any("must_not" in s.get("bool", {}) for s in f["should"])


def test_product_filter_splits_ids_and_names():
    f = eb._product_filter(["3", "netalytics", "-7"])["bool"]
    assert {"terms": {"metadata.products.id": [3, -7]}} in f["should"]
    assert {"terms": {"metadata.products.name.kw": ["netalytics"]}} in f["should"]


def test_filters_and_entitlements_with_requests():
    f = eb._filters(
        types=["question"], entitled_jurisdictions=["France", "Germany"],
        entitled_products=None, jurisdictions=["France"], product_ids=[3],
        products=[], templates=["ISDA Master"], subjects=[], categories=[])
    assert {"terms": {"entityType": ["question"]}} in f
    assert {"terms": {"metadata.products.id": [3]}} in f
    assert {"terms": {"metadata.templates.name.kw": ["ISDA Master"]}} in f
    # both the entitlement and the requested jurisdiction produce a clause (AND'd)
    juris_clauses = [c for c in f if c.get("bool", {}).get("should")
                     and any("jurisdictions" in str(s) for s in c["bool"]["should"])]
    assert len(juris_clauses) == 2


def test_filters_empty_when_unrestricted():
    assert eb._filters(
        types=None, entitled_jurisdictions=None, entitled_products=None,
        jurisdictions=[], product_ids=[], products=[], templates=[],
        subjects=[], categories=[]) == []


# ------------------------------------------------------------------- highlights

def test_split_fragment_marks_hits():
    frag = f"types of {eb._HL_PRE}netting{eb._HL_POST} rules"
    assert eb._split_fragment(frag) == [
        {"value": "types of ", "type": "text"},
        {"value": "netting", "type": "hit"},
        {"value": " rules", "type": "text"},
    ]


def test_reshape_highlights_atlas_shape():
    hl = {"title": [f"{eb._HL_PRE}Singapore{eb._HL_POST}"],
          "metadata.templates.name": [f"ISDA {eb._HL_PRE}Master{eb._HL_POST}"]}
    out = eb._reshape_highlights(hl)
    assert {"path": "title",
            "texts": [{"value": "Singapore", "type": "hit"}]} in out
    tpl = next(e for e in out if e["path"] == "metadata.templates.name")
    assert tpl["texts"] == [{"value": "ISDA ", "type": "text"},
                            {"value": "Master", "type": "hit"}]


def test_reshape_highlights_none():
    assert eb._reshape_highlights(None) == []


# -------------------------------------------------------------- effective (contexts)

def test_effective_unrestricted():
    assert eb._effective(None, []) is None
    assert eb._effective(None, ["France"]) == ["France"]


def test_effective_entitlement_only_and_intersection():
    assert eb._effective(["France", "Germany"], []) == ["France", "Germany"]
    assert eb._effective(["France", "Germany"], ["France", "Japan"]) == ["France"]


# --------------------------------------------------------- entity_index contexts

def test_suggest_contexts_agnostic_doc_gets_sentinel():
    doc = {"entityType": "product", "title": "netalytics",
           "metadata": {"products": [{"id": 3, "name": "netalytics"}]}}
    ctx = ei.suggest_contexts(doc)
    # single jurisdiction context; no jurisdictions -> [CTX_ALL, CTX_ANY]
    assert ctx == {"jurisdiction": [ei.CTX_ALL, ei.CTX_ANY]}


def test_suggest_contexts_with_jurisdictions():
    doc = {"entityType": "opinion", "title": "Singapore",
           "metadata": {"jurisdictions": ["Singapore"], "products": []}}
    ctx = ei.suggest_contexts(doc)
    # a doc WITH a jurisdiction never gets CTX_ALL (stays hidden under a non-matching filter)
    assert ctx == {"jurisdiction": ["Singapore", ei.CTX_ANY]}


def test_suggest_field_requires_title():
    assert ei.suggest_field({"entityType": "product", "title": "",
                             "metadata": {}}) is None
    sf = ei.suggest_field({"entityType": "product", "title": "ISDA",
                           "metadata": {"jurisdictions": ["France"]}})
    assert sf["input"] == ["ISDA"]
    assert sf["contexts"] == {"jurisdiction": ["France", ei.CTX_ANY]}


def test_product_allowed_agnostic_and_match():
    assert eb._product_allowed({"metadata": {}}, ["3"]) is True            # agnostic
    assert eb._product_allowed(
        {"metadata": {"products": [{"id": 3, "name": "Netalytics"}]}}, ["3"]) is True
    assert eb._product_allowed(
        {"metadata": {"products": [{"id": 3, "name": "Netalytics"}]}}, ["Netalytics"]) is True
    assert eb._product_allowed(
        {"metadata": {"products": [{"id": 9, "name": "Other"}]}}, ["3"]) is False


# ----------------------------------------------------------------------- backend

def test_backend_default_and_override(monkeypatch):
    monkeypatch.delenv("ACI_ENTITY_SEARCH_BACKEND", raising=False)
    assert eb.backend() == "opensearch"
    monkeypatch.setenv("ACI_ENTITY_SEARCH_BACKEND", "Atlas")
    assert eb.backend() == "atlas"
