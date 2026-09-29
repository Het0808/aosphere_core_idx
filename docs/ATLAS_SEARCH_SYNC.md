# Atlas Search Sync — Phase 1 (Product → Opinion → Template → Question)

Script: `scripts/sync_atlas_search.py`. Analysis behind it: `docs/MSSQL_ANALYSIS.md`.
Query layer: `GET /api/entity-search` (`service/entity_search.py`) — Spotlight-style
one-box search, results grouped by entityType. Confirmed decisions: single combined
`entities` collection (no per-entity collections); endpoint lives in this repo's
FastAPI app; status filter decided after inspecting `--dry-run` output.

## Hierarchy and metadata inheritance

```
Product (RenderingStyle, 83)
  └─ Opinion (32K rows -> latest version per lineage only)
       │        via OpinionRenderingStyle;  jurisdiction via OpinionJurisdiction
       └─ Template (161)   via TemplateOpinionOrg (org dimension collapsed to DISTINCT)
            └─ Question (3.9K)  via TemplateQuestion
                 └─ Subject (158, grouping) via SubjectQuestion
```

**Opinion versioning:** only the latest version per lineage (`masterOpinionID`
else `parentID` else own id; highest `version` wins) is indexed, and all
template/question/subject ancestry references are remapped to that latest id —
metadata never mixes versions. **Subjects** are question groupings: question docs
carry `subjects [{id,name}]`; subject docs carry their `questions`, plus
templates/products/jurisdictions aggregated from them (opinions omitted to
bound fan-out).

Metadata flows top-to-bottom: an opinion carries its products + jurisdiction; a
template carries its opinions, their products and jurisdictions; a question
carries its templates, their opinions, products and jurisdictions — each as
`{id, name}` subdocuments (jurisdictions as name + ISO-ish code arrays).

## Document shape

```json
{
  "_id": "question:1234",
  "entityType": "question",
  "sourceId": 1234,
  "title": "Are simple e-signatures legal, valid and binding?",
  "description": "…questionDescription + questionComment, HTML stripped…",
  "metadata": {
    "templates":     [{"id": 7,  "name": "ISDA Master"}],
    "opinions":      [{"id": 55, "name": "Singapore"}],
    "products":      [{"id": 3,  "name": "netalytics"}],
    "jurisdictions": ["Singapore"],
    "jurisdictionCodes": ["SG"],
    "status": 1,
    "modifiedDate": "…"
  },
  "source": {"table": "Question", "pk": "questionID"},
  "syncRun": "…", "syncedAt": "…"
}
```

Target: db `aosphere_search`, collection `entities` (override with
`ATLAS_DB` / `ATLAS_COLLECTION`).

## Search index (`entity_search`)

Static mapping (no dynamic fields): `title` gets standard + edgeGram
autocomplete analyzers; `description` standard; `entityType`,
`metadata.*.name`, `jurisdictions` are token (exact filter/facet) + string
(searchable); ids are numbers; `modifiedDate` date. The script creates the
index if missing and updates it if the definition changed — no console work
needed.

## Running

```bash
pip install pymssql pymongo
python scripts/sync_atlas_search.py --dry-run     # no Mongo needed; writes sample docs to data/
python scripts/sync_atlas_search.py --verify      # full sync + sample $search queries
python scripts/sync_atlas_search.py --status all  # include non-live rows
```

Idempotent: upsert by `_id`, then delete docs of that entityType not touched
by the current run. Safe to cron.

## Spotlight endpoint

```
GET /api/entity-search?q=nett                      # all types, top 5 per type
GET /api/entity-search?q=collateral&types=question&jurisdictions=Singapore
GET /api/entity-search?q=isda&productIds=3&limit=10
```

Response: `{query, total, groups: {product: [...], opinion: [...], template: [...],
question: [...]}}`, each hit `{entityType, sourceId, title, description, metadata,
score}`. Scoring: exact title match (boost 5) > title prefix/autocomplete (boost 3)
> description text. Auth: same `require_access` dependency as the other endpoints;
returns 503 if `MONGODB_URI` is unset.

### Entitlement enforcement

Users may hold limited product/jurisdiction subscriptions, so entitlements are
enforced inside the `$search` filter, not left to the client:

- `entitled_jurisdictions(user)` / `entitled_products(user)` (auth.py) read
  Keycloak claims (`jurisdictions`/`regions`…, `products`/`productIds`…);
  `None` (claim not yet mapped) = unrestricted, same convention as `/api/search`.
- Entitlement clauses are AND'ed with user-requested filters — a request for a
  product/jurisdiction outside the entitlement yields no hits, never a bypass.
- Product filters accept renderStyle ids or names and match `metadata.products`
  uniformly: product docs self-reference themselves there, and opinions/
  templates/questions inherit their ancestry, so one `in` clause covers all types.
- Jurisdiction clauses keep jurisdiction-agnostic docs visible (a product has no
  jurisdiction; entitled users still see it) via an `exists` fallback.

## Decisions to confirm

- **`--status` defaults to `1`.** Status enum semantics are undocumented; the
  script prints each table's status distribution at extract time — check that
  the filtered counts look like the "live" set, else rerun with `--status all`
  or another value list.
- Opinions are versioned (`version`, `parentID`, `masterOpinionID` kept in
  metadata). If status=1 still yields near-duplicate versions, add a
  latest-per-master dedupe.
- `TemplateOpinionOrg` includes an org dimension; the sync collapses it to
  distinct template↔opinion pairs (org-specific visibility is a query-time,
  phase-2 concern).
