# MSSQL (dev1) Schema Analysis — Atlas Search Sync

Source: `aosphere-tenant-dev1-db-master...rds.amazonaws.com:1501/dev1`, SQL Server 2022 (RTM-CU24).
Snapshot: 2026-07-07 (`data/mssql_schema_analysis.json`).

## Database shape

332 tables (317 non-empty), 2,435 columns, 332 FKs, all in schema `dbo`. No views, no rowversion/timestamp columns — so **incremental sync must key off the `modifiedDate` / `lastUpdated*` datetime columns**, which every core entity table has.

The tables fall into four groups:

1. **Core content entities** (~15 tables) — opinions, alerts, grids, pages, questions, topics. These carry the titles/descriptions worth indexing.
2. **Mapping/join tables** (~120) — `OpinionJurisdiction`, `OpinionMetaTag`, `gridJurisdiction`, `OpinionProduct`, etc. Not indexed directly; used to attach metadata to entities.
3. **Response/answer data** (~40, largest tables) — `Response` (10.5M), `ResponseText` (5.7M), `AllResponsesOfOpinion` (3.8M). Per-org survey answers; out of scope for a title/description index.
4. **Operational** — `Audits` (15.2M rows, 13.8 GB), login logs, password history, export trackers. Exclude.

## Recommended entities to index

| entityType | Table | PK | title | description | rows | updated col |
|---|---|---|---|---|---|---|
| opinion | Opinion | opinionID | opinionName | AlternativeText | 32,448 | modifiedDate, lastUpdatedOn |
| alert | Alerts | alertID | title | summary (HTML) | 18,724 | publicationDate¹ |
| grid | gridDetails | gridID | gridName / gridTitle | AlternativeText | 1,624 | lastUpdatedDateTime |
| page | PageDetails | pageID | pageName / pageTitle | AlternativeText | 866 | lastUpdatedOn |
| question | Question | questionID | questionTitle | questionDescription, questionComment | 3,921 | modifiedDate |
| topic | speedReadTopic | topicID | title | description | 58 | lastUpdatedDate |
| analysis | Analysis | analysisID | analysisName | analysisDescription | 36 | modifiedDate |
| tag | OpinionTag | opiniontagID | opiniontagName | opiniontagDescription | 57 | modifiedDate |
| productCategory | ProductCategory | categoryID | categoryName | categoryDescription | 9 | modifiedDate |
| announcement | Announcement | announcementID | announcementName, subTitle | description | 55 | modifiedDate |
| externalLink | ExternalLink | (no PK — use ExternalLinkID) | LinkName | Description | 12 | CreatedDate |
| contentTile | OpinionContentTileDetails | OpinionContentTileDetailsID | OpinionContentTileName | — | 35,078 | LastUpdatedDate |

¹ `Alerts` has no modified column; use `publicationDate` + full-refresh fallback.

Optional (decide): `Organisation.orgName` (3,943) and `OrgEntity.orgEntityName` (547) if you want org lookup in the same index. **Excluded deliberately:** `Users` (PII; first/last names are hashed in dev anyway), `Audits*`, `QuestionReference.ReferenceText` (1.9M rows of per-opinion reference text — indexable later as a separate collection if needed), `*DocumentProfile` (filenames, low search value; `OriginalFileName` is decent if doc search is wanted).

## Metadata to attach per document

Joined via mapping tables at ETL time, stored as plain fields for filtering/faceting:

- **jurisdictions**: `Jurisdiction.jurisdictionName/jurisdictionCode` via `OpinionJurisdiction`, `AlertJurisdiction` (49K), `gridJurisdiction`, `PageJurisdiction`.
- **tags**: `OpinionTag.opiniontagName` via `OpinionMetaTag`, `alertMetaTag`, `gridMetaTag`, `PageMetaTag` (all four map into the same tag table).
- **productCategory / analysis**: `Analysis.categoryID → ProductCategory` chain; opinions link via `OpinionAnalysis`.
- **owner org**: `orgID → Organisation.orgName` (Opinion, gridDetails, PageDetails carry orgID).
- **lifecycle**: `status`, `version`, `parentID/masterID` (Opinion, gridDetails, PageDetails are versioned — index only latest/active, filter `status`), `effectiveDate`/`publicationDate`, created/modified dates, source PK + table for traceability.

## Data-quality notes for the ETL

- `Alerts.summary`, `Organisation.addNotes`, `RenderingStyle.*` contain **HTML + entities** (`<p>`, `&ndash;`) — strip tags and decode entities before indexing.
- Many description columns are sparsely populated (`AlternativeText` avg 5.7 chars on Opinion; often a date string, not a description) — index but don't rely on them; boost title.
- `nvarchar(max)` columns (`max_length = -1`) on Alerts/Question — no truncation issues but cap stored length (e.g. 8K) in Mongo.
- Versioned entities: `Opinion.version` + `parentID`, same on grids/pages — dedupe to current version or search returns near-duplicates.
- `ExternalLink` has no PK constraint; `ExternalLinkID` is usable.
- Status columns are ints with unknown enum meanings — verify which values mean "live" before filtering (likely `status = 1`).

## Sync design implication

No CDC/Change Tracking artifacts and no rowversion columns were found, so the rerunnable ETL should do: full extract per entity (row counts are small — everything except contentTile is < 40K rows), upsert by `{entityType, sourceId}`, and delete docs no longer present. Total corpus ≈ **93K documents** — a full resync will take seconds, so watermark logic is optional. Alerts is append-mostly; the rest are low-churn editorial tables.
