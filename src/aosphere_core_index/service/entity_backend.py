"""OpenSearch backend for Spotlight entity search.

The lexical counterpart to embeddings/vector_backend.py: it owns the OpenSearch
query/suggest DSL for the `entity_search` (Spotlight) feature and returns rows in the
SAME normalized shape the Atlas `$search` pipeline produced, so the FastAPI handler in
service/entity_search.py can dispatch to either backend by ACI_ENTITY_SEARCH_BACKEND
without changing its grouping / score-ratio pruning / paging.

  * search()  -> flat list of {entityType, sourceId, title, description, metadata,
                 score, highlights} (highlights reshaped to the Atlas searchHighlights
                 shape the UI parses; see _reshape_highlights).
  * suggest() -> flat list of {title, entityType} from the FST completion suggester
                 (typo-tolerant, entitlement-aware via category contexts).

Connection/auth (SigV4 for AWS, plain HTTP for local Docker) is reused wholesale from
embeddings.vector_backend._os_client — one OpenSearch cluster serves both the k-NN
vector index and this lexical index, just different indices.
"""
from __future__ import annotations

import os

from aosphere_core_index.service.entity_index import (
    CTX_ALL,
    CTX_ANY,
    INDEX_ALIAS,
    META_PREFIX_PATHS,
    META_TEXT_PATHS,
)

# Private-use-area sentinels wrap highlighted terms so we can split fragments back into
# {value, type} segments without colliding with real content (unlike <em>…</em>).
_HL_PRE = ""
_HL_POST = ""


def backend() -> str:
    """Which entity-search backend is active: 'opensearch' (default) or 'atlas'."""
    return os.getenv("ACI_ENTITY_SEARCH_BACKEND", "opensearch").strip().lower()


def _client():
    # Reuse the vector backend's client factory: SigV4/IAM for an AWS managed domain or
    # serverless (ACI_OPENSEARCH_AWS_SERVICE), plain HTTP for a local Docker cluster.
    from aosphere_core_index.embeddings.vector_backend import _os_client

    return _os_client()


def _timeout() -> int:
    return int(os.getenv("ACI_OPENSEARCH_TIMEOUT", "60"))


# --------------------------------------------------------------------- filter clauses

def _jurisdiction_filter(names: list[str]) -> dict:
    """Restrict to `names` WITHOUT dropping jurisdiction-agnostic docs (products carry no
    metadata.jurisdictions). Mirrors entity_search._jurisdiction_clause."""
    return {"bool": {
        "should": [
            {"terms": {"metadata.jurisdictions.kw": names}},
            {"bool": {"must_not": {"exists": {"field": "metadata.jurisdictions"}}}},
        ],
        "minimum_should_match": 1,
    }}


def _product_filter(values: list[str]) -> dict:
    """Restrict to products by renderStyle id and/or name (a doc passes on ANY match).
    Mirrors entity_search._product_clause."""
    ids = [int(v) for v in values if str(v).lstrip("-").isdigit()]
    names = [v for v in values if not str(v).lstrip("-").isdigit()]
    should: list[dict] = []
    if ids:
        should.append({"terms": {"metadata.products.id": ids}})
    if names:
        should.append({"terms": {"metadata.products.name.kw": names}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def _filters(
    *, types, entitled_jurisdictions, entitled_products, jurisdictions,
    product_ids, products, templates, subjects, categories,
) -> list[dict]:
    """Assemble the bool.filter clauses — entitlements AND'd with user-requested filters,
    exactly like the Atlas handler builds its compound.filter list."""
    f: list[dict] = []
    if types:
        f.append({"terms": {"entityType": list(types)}})
    if entitled_jurisdictions:
        f.append(_jurisdiction_filter(entitled_jurisdictions))
    if entitled_products:
        f.append(_product_filter(entitled_products))
    if jurisdictions:
        f.append(_jurisdiction_filter(jurisdictions))
    if product_ids:
        f.append({"terms": {"metadata.products.id": list(product_ids)}})
    if products:
        f.append(_product_filter(products))
    if templates:
        f.append({"terms": {"metadata.templates.name.kw": list(templates)}})
    if subjects:
        f.append({"terms": {"metadata.subjects.name.kw": list(subjects)}})
    if categories:
        f.append({"terms": {"metadata.categories.name.kw": list(categories)}})
    return f


def build_query(q: str, filters: list[dict]) -> dict:
    """The Spotlight relevance ladder as a bool query. Boosts mirror the Atlas
    compound.should ladder in entity_search.py (phrase-name 8 > title 5 > title-prefix 3
    > meta-text 3 > meta-prefix 2 > description 1). `fuzziness:AUTO` on the title adds
    typo tolerance Atlas autocomplete lacked."""
    should: list[dict] = [
        # a query that IS a metadata value (whole phrase) ranks highest
        {"multi_match": {"query": q, "type": "phrase", "fields": META_TEXT_PATHS,
                         "boost": 8}},
        {"match": {"title": {"query": q, "boost": 5, "fuzziness": "AUTO"}}},
        {"match": {"title.prefix": {"query": q, "boost": 3}}},
        {"multi_match": {"query": q, "fields": META_TEXT_PATHS, "boost": 3}},
        *[{"match": {f"{p}.prefix": {"query": q, "boost": 2}}} for p in META_PREFIX_PATHS],
        {"match": {"description": {"query": q}}},
    ]
    return {"bool": {"should": should, "minimum_should_match": 1, "filter": filters}}


# ------------------------------------------------------------------------- highlights

def _split_fragment(frag: str) -> list[dict]:
    """One highlighted fragment (with _HL_PRE/_HL_POST around hits) -> alternating
    {value, type: 'text'|'hit'} segments — the Atlas searchHighlights `texts` shape."""
    texts: list[dict] = []
    for i, chunk in enumerate(frag.split(_HL_PRE)):
        if i == 0:
            if chunk:
                texts.append({"value": chunk, "type": "text"})
            continue
        hit, _, rest = chunk.partition(_HL_POST)
        if hit:
            texts.append({"value": hit, "type": "hit"})
        if rest:
            texts.append({"value": rest, "type": "text"})
    return texts


def _reshape_highlights(hl: dict | None) -> list[dict]:
    """OpenSearch `highlight` ({field: [fragments]}) -> Atlas searchHighlights shape
    ([{path, texts:[{value, type}]}], one entry per fragment) so spotlight_web.py's
    render()/metaLine() parse it unchanged."""
    out: list[dict] = []
    for path, frags in (hl or {}).items():
        for frag in frags:
            texts = _split_fragment(frag)
            if texts:
                out.append({"path": path, "texts": texts})
    return out


# ------------------------------------------------------------------------ public API

_SOURCE_FIELDS = ["entityType", "sourceId", "title", "description", "metadata"]


def search(
    *, q: str, size: int, types=None, entitled_jurisdictions=None,
    entitled_products=None, jurisdictions=None, product_ids=None, products=None,
    templates=None, subjects=None, categories=None,
) -> list[dict]:
    """Run the main Spotlight query; return a flat scored list in the Atlas hit shape."""
    filters = _filters(
        types=types, entitled_jurisdictions=entitled_jurisdictions,
        entitled_products=entitled_products, jurisdictions=jurisdictions,
        product_ids=product_ids, products=products, templates=templates,
        subjects=subjects, categories=categories,
    )
    body = {
        "size": size,
        "track_total_hits": False,
        "query": build_query(q, filters),
        "_source": _SOURCE_FIELDS,
        "highlight": {
            "pre_tags": [_HL_PRE], "post_tags": [_HL_POST],
            "fields": {f: {} for f in ["title", "description", *META_TEXT_PATHS]},
        },
    }
    resp = _client().search(index=INDEX_ALIAS, body=body, request_timeout=_timeout())
    hits: list[dict] = []
    for h in resp["hits"]["hits"]:
        src = h.get("_source", {})
        hits.append({
            "entityType": src.get("entityType"),
            "sourceId": src.get("sourceId"),
            "title": src.get("title"),
            "description": src.get("description"),
            "metadata": src.get("metadata") or {},
            "score": float(h.get("_score") or 0.0),
            "highlights": _reshape_highlights(h.get("highlight")),
        })
    return hits


def _effective(entitled, requested) -> list[str] | None:
    """Combine an entitlement set (or None = unrestricted) with a requested set for a
    completion category context. None => omit the context (match all). Both present =>
    intersection (a request can only narrow within the entitlement)."""
    req = [str(v) for v in (requested or [])]
    if entitled is None:
        return req or None
    ent = {str(v) for v in entitled}
    if not req:
        return [str(v) for v in entitled]
    return [v for v in req if v in ent]


def _product_allowed(src: dict, allowed: list[str]) -> bool:
    """True if the doc's products intersect `allowed` (by id OR name), or it has none
    (agnostic) — mirrors _product_filter's id/name match + the agnostic fallback."""
    prods = (src.get("metadata") or {}).get("products") or []
    if not prods:
        return True
    tokens: set[str] = set()
    for p in prods:
        if p.get("id") is not None:
            tokens.add(str(p["id"]))
        if p.get("name"):
            tokens.add(str(p["name"]))
    return bool(tokens & set(allowed))


def suggest(
    *, q: str, size: int = 8, types=None, entitled_jurisdictions=None,
    entitled_products=None, jurisdictions=None, product_ids=None, products=None,
    templates=None, subjects=None, categories=None,
) -> list[dict]:
    """FST completion suggester over `title_suggest`, typo-tolerant (fuzzy). Returns
    [{title, entityType}], deduped by title, capped at `size`.

    OpenSearch OR's multiple completion context types, so only JURISDICTION (the primary
    entitlement) is enforced in the FST context — unrestricted filters by CTX_ANY (every
    doc), restricted by its allowed values + CTX_ALL (agnostic docs stay visible).
    entityType (requested types) and product (entitlement ∩ request) are AND-enforced by
    post-filtering _source; we over-fetch candidates to absorb the drops."""
    juris = _effective(entitled_jurisdictions, list(jurisdictions or []))
    jur_ctx = [*juris, CTX_ALL] if juris is not None else [CTX_ANY]
    want_types = set(types) if types else None
    prod_req = [str(p) for p in (product_ids or [])] + list(products or [])
    eff_prod = _effective(entitled_products, prod_req)  # None = unrestricted

    completion = {
        "field": "title_suggest",
        "size": max(size * 6, 50),  # over-fetch: entityType/product post-filter drops some
        "skip_duplicates": True,
        "fuzzy": {"fuzziness": "AUTO"},
        "contexts": {"jurisdiction": jur_ctx},
    }
    body = {"_source": ["title", "entityType", "metadata.products"],
            "suggest": {"spot": {"prefix": q, "completion": completion}}}
    resp = _client().search(index=INDEX_ALIAS, body=body, request_timeout=_timeout())
    out: list[dict] = []
    seen: set[str] = set()
    for opt in resp.get("suggest", {}).get("spot", [{}])[0].get("options", []):
        src = opt.get("_source") or {}
        title = src.get("title") or opt.get("text")
        if not title or title.lower() in seen:
            continue
        if want_types and src.get("entityType") not in want_types:
            continue
        if eff_prod is not None and not _product_allowed(src, eff_prod):
            continue
        seen.add(title.lower())
        out.append({"title": title, "entityType": src.get("entityType")})
        if len(out) >= size:
            break
    return out
