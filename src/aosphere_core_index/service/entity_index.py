"""OpenSearch lexical index definition for Spotlight entity search.

Single source of truth for the index alias, analyzers, field mappings, and the
completion-suggester contexts — imported by the loader (scripts/sync_atlas_search.py)
and the query layer (service/entity_backend.py) so the index shape and the query
shape can never drift.

Mirrors the Atlas Search index built by scripts/sync_atlas_search.py, feature for
feature, on OpenSearch primitives:

  * `english` analyzer          <- Atlas `lucene.english` (stemming + stopwords)
  * `aci_edge_ngram` 2..15      <- Atlas autocomplete edgeGram minGrams 2 maxGrams 15
                                   (+ asciifolding == Atlas foldDiacritics)
  * `title_suggest` completion  -> FST auto-suggest with entityType / jurisdiction /
                                   product category contexts (entitlement-aware,
                                   typo-tolerant). No Atlas analog.

Documents keep the EXACT shape produced by the sync's extract() — this module only
adds the derived `title_suggest` completion field at index time (see suggest_field).

Env: ACI_ENTITY_INDEX (alias, default "aci-entities"),
     ACI_ENTITY_OS_REPLICAS (default 0; set >=1 in prod).
"""
from __future__ import annotations

import os

# The alias the app queries. The loader writes to a fresh concrete index
# (`{ALIAS}-{runstamp}`) and atomically repoints this alias for zero-downtime reindex.
INDEX_ALIAS = os.getenv("ACI_ENTITY_INDEX", "aci-entities")

# Completion-suggester JURISDICTION context sentinels. OpenSearch OR's multiple completion
# context types (they can't express AND), so only jurisdiction — the primary per-region
# entitlement — is pushed into the FST; entityType/product are post-filtered on _source in
# entity_backend.suggest(). The `contexts` block is mandatory but a single context suffices.
#   CTX_ANY : indexed on EVERY doc; the value an UNRESTRICTED query filters by (match-all).
#   CTX_ALL : indexed only on jurisdiction-AGNOSTIC docs (products, etc.); a RESTRICTED query
#             includes it alongside the allowed jurisdictions so agnostic docs stay visible —
#             reproducing the Atlas `exists`-mustNot fallback in _jurisdiction_clause. Docs
#             that DO carry a jurisdiction never get CTX_ALL, so a non-matching filter hides
#             them.
CTX_ALL = "_all"
CTX_ANY = "_any"

# Metadata text paths (full-word / phrase scoring) and the small-vocabulary paths that
# also carry an edge-ngram `.prefix` subfield for as-you-type matching. Kept here so the
# query layer and the mapping agree on exactly which fields exist.
META_TEXT_PATHS = [
    "metadata.categories.name", "metadata.products.name", "metadata.jurisdictions",
    "metadata.templates.name", "metadata.subjects.name", "metadata.opinions.name",
    "metadata.mainQuestions.name", "metadata.breadcrumbs",
]
META_PREFIX_PATHS = [
    "metadata.categories.name", "metadata.products.name", "metadata.jurisdictions",
    "metadata.templates.name", "metadata.subjects.name",
]

_ANALYSIS = {
    "filter": {
        # Atlas edgeGram: minGrams 2, maxGrams 15.
        "aci_edge_ngram_filter": {"type": "edge_ngram", "min_gram": 2, "max_gram": 15},
    },
    "analyzer": {
        # Index-time: fold + lowercase, then emit edge n-grams (prefix search).
        "aci_edge_ngram": {
            "type": "custom", "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding", "aci_edge_ngram_filter"],
        },
        # Search-time: fold + lowercase only — do NOT n-gram the query, or "sing"
        # would also match on its own "si"/"in" fragments and over-recall.
        "aci_edge_search": {
            "type": "custom", "tokenizer": "standard",
            "filter": ["lowercase", "asciifolding"],
        },
    },
}


def _prefix_subfield() -> dict:
    return {"type": "text", "analyzer": "aci_edge_ngram", "search_analyzer": "aci_edge_search"}


def _name_field(prefix: bool) -> dict:
    """`name` inside a {id, name} ref: english text (scoring) + keyword (exact `terms`
    filter) + optional edge-ngram prefix (autocomplete on small vocabularies)."""
    fields: dict = {"kw": {"type": "keyword", "ignore_above": 1024}}
    if prefix:
        fields["prefix"] = _prefix_subfield()
    return {"type": "text", "analyzer": "english", "fields": fields}


def _ref(prefix: bool) -> dict:
    """{id, name} sub-document (array). Arrays of objects flatten natively — no `nested`
    type needed, because filters query `.id` OR `.name` independently, never correlated."""
    return {"properties": {"id": {"type": "long"}, "name": _name_field(prefix)}}


def _string_field(prefix: bool = False) -> dict:
    """A plain string (array), e.g. metadata.jurisdictions."""
    fields: dict = {"kw": {"type": "keyword", "ignore_above": 1024}}
    if prefix:
        fields["prefix"] = _prefix_subfield()
    return {"type": "text", "analyzer": "english", "fields": fields}


def mappings() -> dict:
    """Field mappings. `dynamic: false` keeps unmapped metadata (orgId, version,
    effectiveDate, breadcrumbs paths, …) in _source (returned to the UI) without
    indexing it — matching Atlas, which only indexed the fields declared below."""
    return {
        "dynamic": False,
        "properties": {
            "entityType": {"type": "keyword"},
            "sourceId": {"type": "long"},
            "title": {
                "type": "text", "analyzer": "english",
                "fields": {
                    "kw": {"type": "keyword", "ignore_above": 1024},
                    "prefix": _prefix_subfield(),
                },
            },
            # FST completion suggester: jurisdiction-aware (single category context) +
            # typo-tolerant (fuzzy at query time). Populated explicitly by the loader
            # (suggest_field); entityType/product are post-filtered in entity_backend.
            "title_suggest": {
                "type": "completion",
                "analyzer": "simple",
                "preserve_separators": True,
                "preserve_position_increments": True,
                "max_input_length": 100,
                "contexts": [
                    {"name": "jurisdiction", "type": "category"},
                ],
            },
            "description": {"type": "text", "analyzer": "english"},
            "metadata": {
                "type": "object",
                "properties": {
                    "products": _ref(prefix=True),
                    "categories": _ref(prefix=True),
                    "templates": _ref(prefix=True),
                    "subjects": _ref(prefix=True),
                    "opinions": _ref(prefix=False),
                    "mainQuestions": _ref(prefix=False),
                    "breadcrumbs": {"type": "text", "analyzer": "english"},
                    "jurisdictions": _string_field(prefix=True),
                    "jurisdictionCodes": {"type": "keyword"},
                    "status": {"type": "integer"},
                    "modifiedDate": {"type": "date"},
                },
            },
        },
    }


def settings(replicas: int | None = None) -> dict:
    if replicas is None:
        replicas = int(os.getenv("ACI_ENTITY_OS_REPLICAS", "0"))
    return {
        "index": {
            "number_of_shards": 1,
            "number_of_replicas": replicas,
            "analysis": _ANALYSIS,
        }
    }


def index_body(replicas: int | None = None) -> dict:
    return {"settings": settings(replicas), "mappings": mappings()}


def suggest_contexts(doc: dict) -> dict:
    """The completion-suggester `jurisdiction` context for a document: the doc's
    jurisdictions (or [CTX_ALL] when it carries none, so agnostic docs stay visible under a
    restricted filter), always plus CTX_ANY (the unrestricted-query match-all token)."""
    jurs = [j for j in ((doc.get("metadata") or {}).get("jurisdictions") or []) if j]
    return {"jurisdiction": (jurs or [CTX_ALL]) + [CTX_ANY]}


def suggest_field(doc: dict) -> dict | None:
    """The `title_suggest` completion value for a doc, or None when it has no title
    (completion inputs must be non-empty). Called by the loader per doc."""
    title = (doc.get("title") or "").strip()
    if not title:
        return None
    return {"input": [title], "contexts": suggest_contexts(doc)}
