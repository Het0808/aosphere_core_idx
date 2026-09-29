"""Spotlight-style entity search over the MongoDB Atlas `entity_search` index.

GET /api/entity-search?q=nett&types=opinion,question&jurisdictions=Singapore
                      &productIds=3&limit=5

One $search against the combined `entities` collection (synced from MSSQL by
scripts/sync_atlas_search.py); results come back grouped by entityType, like
Spotlight's categories. Title autocomplete + full-text over title/description.

Env: MONGODB_URI (required), ATLAS_DB (default aosphere_search),
     ATLAS_COLLECTION (default entities), ATLAS_SEARCH_INDEX (default entity_search).
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from aosphere_core_index.service import entity_backend
from aosphere_core_index.service.auth import (
    entitled_jurisdictions,
    entitled_products,
    require_access,
)

log = logging.getLogger(__name__)
router = APIRouter()

_Q_MAX = 500
_TYPES = {"product", "opinion", "template", "question", "subject", "jurisdiction"}
_client = None


def _mongo_uri() -> str | None:
    """MONGODB_URI, with optional MONGODB_USERNAME/MONGODB_PASSWORD injected
    percent-encoded (passwords with @ : / # etc. break raw URI parsing)."""
    from urllib.parse import quote_plus

    uri = os.environ.get("MONGODB_URI")
    user = os.environ.get("MONGODB_USERNAME")
    pwd = os.environ.get("MONGODB_PASSWORD")
    if uri and user and pwd:
        scheme, sep, rest = uri.partition("://")
        rest = rest.rsplit("@", 1)[-1]
        uri = f"{scheme}{sep}{quote_plus(user)}:{quote_plus(pwd)}@{rest}"
    return uri


def _collection():
    global _client
    uri = _mongo_uri()
    if not uri:
        raise HTTPException(503, "entity search not configured (MONGODB_URI unset)")
    if _client is None:
        from pymongo import MongoClient

        kwargs = {}
        try:  # python.org macOS installs lack system CA access; use certifi
            import certifi
            kwargs["tlsCAFile"] = certifi.where()
        except ImportError:
            pass
        _client = MongoClient(uri, serverSelectionTimeoutMS=5000, **kwargs)
    return _client[os.environ.get("ATLAS_DB", "aosphere_search")][
        os.environ.get("ATLAS_COLLECTION", "entities")]


def _csv(value: str | None, cap: int = 50) -> list[str]:
    items = [v.strip() for v in (value or "").split(",") if v.strip()]
    if len(items) > cap:
        raise HTTPException(400, f"too many values (max {cap})")
    return items


def _jurisdiction_clause(names: list[str]) -> dict:
    """Restrict to the given jurisdictions WITHOUT dropping jurisdiction-agnostic
    docs (products carry no metadata.jurisdictions; an empty array is unindexed,
    so `exists` is false for them)."""
    return {"compound": {
        "should": [
            {"in": {"path": "metadata.jurisdictions", "value": names}},
            {"compound": {"mustNot": [
                {"exists": {"path": "metadata.jurisdictions"}}]}},
        ],
        "minimumShouldMatch": 1,
    }}


def _product_clause(values: list[str]) -> dict:
    """Restrict to the given products; accepts renderStyle ids and/or names.
    Every doc carries metadata.products (products self-reference), so a plain
    `in` is safe. A doc passes if it matches ANY allowed id or name."""
    ids = [int(v) for v in values if v.lstrip("-").isdigit()]
    names = [v for v in values if not v.lstrip("-").isdigit()]
    should: list[dict] = []
    if ids:
        should.append({"in": {"path": "metadata.products.id", "value": ids}})
    if names:
        should.append({"in": {"path": "metadata.products.name", "value": names}})
    return {"compound": {"should": should, "minimumShouldMatch": 1}}


def _atlas_filters(wanted, allowed_juris, allowed_prods, req_juris, pids,
                   req_prods, req_templates, req_subjects, req_categories) -> list[dict]:
    """Atlas `compound.filter` clauses: entitlements (from Keycloak claims; None =
    unrestricted) AND'd with any user-requested filters, so a request can only narrow
    WITHIN the entitlement — asking for a product/jurisdiction you lack yields no hits."""
    filters: list[dict] = []
    if wanted != _TYPES:
        filters.append({"in": {"path": "entityType", "value": sorted(wanted)}})
    if allowed_juris:
        filters.append(_jurisdiction_clause(allowed_juris))
    if allowed_prods:
        filters.append(_product_clause(allowed_prods))
    if req_juris:
        filters.append(_jurisdiction_clause(req_juris))
    if pids:
        filters.append({"in": {"path": "metadata.products.id", "value": pids}})
    if req_prods:
        filters.append(_product_clause(req_prods))       # accepts names and/or ids
    for values, path in ((req_templates, "metadata.templates.name"),
                         (req_subjects, "metadata.subjects.name"),
                         (req_categories, "metadata.categories.name")):
        if values:
            filters.append({"in": {"path": path, "value": values}})
    return filters


def _atlas_search(q: str, size: int, filters: list[dict]) -> list[dict]:
    """The Atlas `$search` relevance ladder (phrase-name 8 > title 5 > title-prefix 3 >
    meta-text 3 > meta-prefix 2 > description 1) + highlights. Returns the flat scored
    hit list the handler groups/pages. entity_backend.search() is the OpenSearch twin."""
    # metadata name fields: full-word matches on all, prefix (autocomplete) on the small
    # vocabularies — so typing "ab" surfaces a product whose category is "abc", etc.
    meta_text_paths = ["metadata.categories.name", "metadata.products.name",
                       "metadata.jurisdictions", "metadata.templates.name",
                       "metadata.subjects.name", "metadata.opinions.name",
                       "metadata.mainQuestions.name", "metadata.breadcrumbs"]
    meta_ac_paths = ["metadata.categories.name", "metadata.products.name",
                     "metadata.jurisdictions", "metadata.templates.name",
                     "metadata.subjects.name"]
    operator = {
        "compound": {
            "should": [
                {"phrase": {"query": q, "path": meta_text_paths,
                            "score": {"boost": {"value": 8}}}},
                {"text": {"query": q, "path": "title", "score": {"boost": {"value": 5}}}},
                {"autocomplete": {"query": q, "path": "title",
                                  "score": {"boost": {"value": 3}}}},
                {"text": {"query": q, "path": meta_text_paths,
                          "score": {"boost": {"value": 3}}}},
                *[{"autocomplete": {"query": q, "path": p,
                                    "score": {"boost": {"value": 2}}}}
                  for p in meta_ac_paths],
                {"text": {"query": q, "path": "description"}},
            ],
            "minimumShouldMatch": 1,
            **({"filter": filters} if filters else {}),
        }
    }
    pipeline = [
        {"$search": {"index": os.environ.get("ATLAS_SEARCH_INDEX", "entity_search"),
                     **operator,
                     "highlight": {"path": ["title", "description", *meta_text_paths]}}},
        {"$limit": size},
        {"$project": {"_id": 0, "entityType": 1, "sourceId": 1, "title": 1,
                      "description": 1, "metadata": 1,
                      "score": {"$meta": "searchScore"},
                      "highlights": {"$meta": "searchHighlights"}}},
    ]
    return list(_collection().aggregate(pipeline))


@router.get("/spotlight", response_class=HTMLResponse)
def spotlight_page() -> str:
    """Spotlight-style search UI (auth happens client-side + on the API)."""
    from aosphere_core_index.service.spotlight_web import SPOTLIGHT_PAGE

    return SPOTLIGHT_PAGE


@router.get("/api/entity-search")
def entity_search(
    q: str = Query(..., min_length=1, max_length=_Q_MAX),
    types: str | None = Query(None, description="csv of product,opinion,template,question"),
    jurisdictions: str | None = Query(None, description="csv of jurisdiction names"),
    productIds: str | None = Query(None, description="csv of product (renderStyle) ids"),
    products: str | None = Query(None, description="csv of product names and/or ids"),
    templates: str | None = Query(None, description="csv of template names"),
    subjects: str | None = Query(None, description="csv of subject names"),
    categories: str | None = Query(None, description="csv of product-category names"),
    limit: int = Query(5, ge=1, le=25, description="max hits per entity type"),
    offset: int = Query(0, ge=0, le=500, description="per-type offset for paging "
                                                     "(infinite scroll)"),
    minScoreRatio: float = Query(0.25, ge=0.0, le=1.0,
                                 description="drop hits scoring below this fraction of "
                                             "the top hit (prunes weak term-OR matches)"),
    suggest: bool = Query(False, description="fast title-autocomplete mode: returns "
                                             "top title suggestions only"),
    user: dict = Depends(require_access),
):
    wanted = set(_csv(types)) or _TYPES
    if not wanted <= _TYPES:
        raise HTTPException(400, f"unknown types: {sorted(wanted - _TYPES)}")

    # Entitlements (from Keycloak claims; None = unrestricted).
    allowed_juris = entitled_jurisdictions(user)
    allowed_prods = entitled_products(user)
    # user-requested filters (narrow WITHIN entitlements)
    req_juris = _csv(jurisdictions)
    try:
        pids = [int(p) for p in _csv(productIds, cap=20)]
    except ValueError:
        raise HTTPException(400, "productIds must be integers")
    req_prods = _csv(products)
    req_templates = _csv(templates)
    req_subjects = _csv(subjects)
    req_categories = _csv(categories)
    os_types = sorted(wanted) if wanted != _TYPES else None
    use_os = entity_backend.backend() == "opensearch"

    if suggest:
        # Title-prefix suggestions for ghost-text/Tab completion; entitlement
        # filters still enforced on both backends.
        try:
            if use_os:
                raw = entity_backend.suggest(
                    q=q, types=os_types, entitled_jurisdictions=allowed_juris,
                    entitled_products=allowed_prods, jurisdictions=req_juris,
                    product_ids=pids, products=req_prods, templates=req_templates,
                    subjects=req_subjects, categories=req_categories)
            else:
                filters = _atlas_filters(wanted, allowed_juris, allowed_prods, req_juris,
                                         pids, req_prods, req_templates, req_subjects,
                                         req_categories)
                pipeline = [
                    {"$search": {"index": os.environ.get("ATLAS_SEARCH_INDEX", "entity_search"),
                                 "compound": {"must": [{"autocomplete": {"query": q, "path": "title"}}],
                                              **({"filter": filters} if filters else {})}}},
                    {"$limit": 8},
                    {"$project": {"_id": 0, "title": 1, "entityType": 1,
                                  "score": {"$meta": "searchScore"}}},
                ]
                raw = list(_collection().aggregate(pipeline))
        except Exception:
            log.exception("entity suggest failed")
            raise HTTPException(502, "entity search backend error")
        seen, suggestions = set(), []
        for h in raw:  # dedupe identical titles across entity types
            key = (h.get("title") or "").lower()
            if key and key not in seen:
                seen.add(key)
                suggestions.append({"title": h["title"], "entityType": h["entityType"]})
        return {"query": q, "suggestions": suggestions}

    # headroom so no type starves; grows with the page being requested
    size = min((offset + limit) * len(wanted) * 4, 3000)
    try:
        if use_os:
            hits = entity_backend.search(
                q=q, size=size, types=os_types, entitled_jurisdictions=allowed_juris,
                entitled_products=allowed_prods, jurisdictions=req_juris,
                product_ids=pids, products=req_prods, templates=req_templates,
                subjects=req_subjects, categories=req_categories)
        else:
            hits = _atlas_search(q, size, _atlas_filters(
                wanted, allowed_juris, allowed_prods, req_juris, pids, req_prods,
                req_templates, req_subjects, req_categories))
    except HTTPException:
        raise
    except Exception:
        log.exception("entity search failed")
        raise HTTPException(502, "entity search backend error")

    # Long natural-language queries term-OR match almost anything; keep only
    # hits competitive with the best one.
    if hits and minScoreRatio > 0:
        cutoff = hits[0]["score"] * minScoreRatio
        hits = [h for h in hits if h["score"] >= cutoff]

    by_type: dict[str, list[dict]] = {t: [] for t in sorted(wanted)}
    for h in hits:
        bucket = by_type.get(h["entityType"])
        if bucket is not None:
            bucket.append(h)
    groups = {t: v[offset:offset + limit] for t, v in by_type.items()}
    has_more = any(len(v) > offset + limit for v in by_type.values())
    return {"query": q, "offset": offset, "hasMore": has_more,
            "total": sum(len(v) for v in groups.values()), "groups": groups}
