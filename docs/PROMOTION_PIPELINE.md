# Promoting an extraction run

**Status: Phase 1 implemented. Phase 2 implemented for a LOCAL run
(`--with-index`) — destructive vector reload, no alias. The versioned-index / alias
cutover in `docs/VECTOR_INDEX_LIFECYCLE.md` is still not built.**

A run writes to `corpus/<run>/`. The Doc Gallery reads `index/<version>/doc-gallery/` and the
search index reads `index/<version>/`. Nothing carried a document from the first to the second
for a **cluster** run — `scripts/push_hybrid_s3.py` is a local-tree publisher end to end (local
paths, a local PDF base64'd into the viewer, fitz for the page count, an SSO profile no pod
has) — so a run could be reviewed and never published. `extraction_monitor.py:745` and
`corpus_worker.py:787` both say it outright: *"publishing is a separate step against a finished
corpus."*

A **promotion** is that step.

| | |
|---|---|
| Phase 1 | run → published Doc Gallery, at a **staged** index version |
| Phase 2 | that version → searchable in Search Mode and AI Mode |

Phase 1 does not make anything live. It publishes to a new `index/<version>/` and leaves
`index/latest` untouched, so the deployed gallery and the search index are unchanged. That is
deliberate: the point of publishing under a version is that somebody can look at it first.

## Doing it

```bash
# 1. Decide. Reads only; writes nothing. ~20s on a finished run.
python scripts/promote_run.py --run 2026-08-21-02 --plan

# 2. Prove the IAM before doing 20 minutes of work.
python scripts/promote_run.py --run 2026-08-21-02 --probe-write --limit 5

# 3. Do it.
python scripts/promote_run.py --run 2026-08-21-02

# 4. Look at it — published, but nothing points at it.
#    GET /api/doc-gallery/tree?version=2026-08-21-02-p1
```

In the cluster it is `deploy/promotion-job.yaml`. From the UI: the **Promote this run** button
on a run's Doc Gallery scorecard, or **Plan a promotion** in the Extraction tab's Promotion
panel — which records a request and waits for a promoter (see *The service does not start it*).

## What gets promoted

`scorecard.gate ∈ {pass, review}`, in the products `regions/region_map.py::PRODUCTS` names:
**Data Privacy**, **Shareholding Disclosure**, **Marketing Restrictions - Asset Management**.

That product list is not a convenience. A region's identity is product-qualified by
`region_map.qualified()`, so a product outside `PRODUCTS` has no region id, no `content.json`
and no vector rows — promoting its documents would publish gallery entries for content Phase 2
cannot represent. Adding a product is therefore **one edit**, in `PRODUCTS`, and everything
downstream follows: `is_indexable_product_dir()` derives the corpus directory names from it, so
the near-namesakes in the corpus (`170_Data_Privacy_(US_States)`,
`165_Data_Privacy_-_Snippets`) are excluded by the rule rather than by a special case.

Held back, and named in the plan:

| reason | meaning |
|---|---|
| `skip/no_scorecard` | never finished. Not the same statement as "failed" |
| `skip/gate` | `fail` or `error`. `--gate` widens it; the API needs `ACI_ADMIN_PROMOTE_ANY_GATE=1` |
| `skip/product` | not in `PRODUCTS` |
| `blocked/no_viewer` | extracted before `build_review_artefacts` existed. Fix: `scripts/backfill_review_artefacts.py --run <run>` |
| `blocked/slug_collision` | two documents whose slugs collide. Refused rather than letting one overwrite the other |

## Product-scoped runs, and the gallery's own seed step

`--products` (or a run that simply only contains one product's job dirs) scopes what THIS
promotion writes fresh rows for. Phase 2 protects the search index either way: `seed`
downloads `index/<latest>/products/` before rebuilding, and `index_verify` asserts the new
version is a superset — a product this run never touches keeps answering exactly as before.

The Doc Gallery has no such protection **by default**. Phase 1 mints a fresh
`index/<version>/doc-gallery/manifest.json` and writes rows only for what it promotes, so a
run scoped to one product (or one that simply found few eligible documents) publishes a
manifest containing *only* those rows. Cutting over to it then drops every other product from
the Doc Gallery — confirmed against a real promotion: the live manifest held 452 entries
across three products, and an MRAM-only run's Phase 1 would otherwise have published 194,
losing DP's 206 and SD's 139 from the browsable gallery while search kept answering for them
(the seeded half).

`--seed-gallery-from-latest` closes it: before the copy loop, read the live manifest, carry
forward every entry this run's own plan (`todo`, after `--only`/`--limit`/gate filtering —
*not* the `--products` scan scope, since a product can be in scope and still be missing a
region from this particular run) is not about to overwrite, server-side copy its
`viewer.html`/`scorecard.json`/`inspect.html` into the new prefix, and merge those rows into
the SAME manifest commit as the freshly promoted ones. Off by default — a normal
every-product run doesn't need it, and copying forward is not free — but it should be passed
on any promotion that does not, by itself, cover every product `PRODUCTS` names.

## Why it is cheap

`corpus_worker.build_review_artefacts` already wrote `viewer.html` and `inspect.html` **into
the S3 job directory**. So a promotion is:

* one delimiter walk to the job directories (never recursive: 380,609 keys / 64.4s vs 986
  prefixes / 0.9s, measured on run 2026-08-21-02);
* **one scorecard GET per document, for the whole promotion** — the plan reduces each to a
  decision row plus the dozen numbers the gallery card needs, and drops it. Retaining them
  measured 96MB for one 986-document run, in the same process as the vector matrix, and "the
  gallery loaded every scorecard" is what took the service down once;
* two or three server-side `copy_object` calls per document. No PDF is downloaded, no viewer
  is rebuilt, no tree is walked.

`stats` therefore comes from the scorecard, not the PDF. `pages`, `tables_total`,
`tables_converted` and `tables_failed` are exact. `pages_snapshotted` is approximated as the
union of `unvalidatable` pages and failed tables' pages, tagged
`pages_snapshotted_source: "scorecard"` — see `promotion_stats`. **Measure it against
`doc_display_stats` on ~20 real documents before the first full promotion.**

## The one safety property

**Objects are invisible until the manifest names them, and the manifest is written once, at the
end.** A promotion that dies — a spot reclaim, a 403, an operator's stop — leaves the live
gallery byte-for-byte untouched. The resume then re-lists the target, skips what is already
there, and commits at the end of a completed pass.

Everything else is arranged around keeping that true:

* the merge is an **upsert by slug** with **no full-replace branch under any flag**.
  `push_hybrid_s3.py:551` records what that branch did: each 25-document batch stripped every
  `-mineru` row and wrote back only its own, so **455 published documents became 5 manifest
  entries**;
* only documents that were actually copied get rows, so the manifest cannot name a missing key
  (a 404 a reviewer cannot tell apart from a broken viewer);
* the write is conditional (`IfMatch`), so a concurrent writer causes a refusal rather than
  silent loss. `push_hybrid_s3.py`'s own manifest write now carries it too;
* `--manifest-every N` is the only way to give this up. It is off by default.

## Progress

Durable in S3, under the **run**:

```
corpus/<run>/_promotion/<version>/state.json      ~3KB, rewritten per document
corpus/<run>/_promotion/<version>/plan.json       per-document decisions
corpus/<run>/_promotion/<version>/ledger.jsonl    append-only, re-uploaded whole
corpus/<run>/_promotion/<version>/failures.json   the last 200, error text truncated
corpus/<run>/_promotion/<version>/lease.json      the concurrency lock
corpus/<run>/_promotion/<version>/request.json    written by the API
corpus/<run>/_promotion/<version>/stop            drain marker
index/<version>/doc-gallery/_promotion.json       provenance, on the target side
```

Under the run and not the target for three reasons: the extraction role already writes
`corpus/*`, so **a 403 on `index/*` still leaves a readable durable record of the failure**;
the service already reads this prefix (`doc_gallery.extraction_target()`), so the screens need
no new config; and `valid_run` rejects `_`-prefixed names, so `_promotion` can never be
addressed as a run.

**`promotion_id == version`.** Two writers merging into one manifest is the 455→5 incident in
its concurrent form, so making the id equal the version turns "a second concurrent promotion"
into "the same promotion", which the lease refuses. A deliberate second attempt mints `-p2`.

Read it with `GET /api/promotion/progress?run=&promotion=` — one GET. Nothing is cached in a
pod's memory; see `promotion_monitor.py`'s docstring for why that matters.

### The version string

`<run>-p<n>`, e.g. `2026-08-21-02-p1`. The run id is the only string that already identifies
the *content*, and one string has to name `index/<v>/`, `index/<v>/doc-gallery/` **and** (Phase
2) `aci-vectors-<v>` — `docs/INDEX_PIPELINE.md:151`. Validated `^[a-z0-9][a-z0-9._-]{0,48}$` at
mint time, because a version that publishes to S3 happily and then fails at `indices.create`
forty minutes later is the worst possible moment to find out.

### The service does not start it

`POST /api/promotion/request` writes `request.json`; a promoter runs
`promote_run.py --from-request`. The service has no `batch/v1` RBAC and giving a public-facing
pod the right to create Kubernetes Jobs is a separate security decision. Same shape as
`extraction_monitor.mark_retry`, which already works this way. The UI says **"requested —
waiting for a promoter"**, because that is what has happened.

Writes are gated on `ACI_ADMIN_PROMOTE=1` and 404 when it is unset — the `ACI_ADMIN_REINDEX`
pattern. `/api/config` advertises the flag so the UI hides the controls rather than offering a
button that 404s.

## Prerequisite: IAM

`corpus-extract-worker` **cannot do this** — it has `PutObject` on `corpus/*` only, and a
promotion's whole purpose is to write `index/*`. `deploy/promotion-job.yaml` documents the new
`corpus-promote` role. Two grants are deliberately withheld: `PutObject` on `index/latest` (so
"Phase 1 cannot go live" is a permissions fact, not a convention) and `DeleteObject` anywhere (a
lease is released by *writing* `released_at`).

`--plan` never calls `PutObject`, so a green dry run says nothing about whether the principal
can write. That is what `--probe-write` is for: a 403 in two seconds instead of twenty minutes
of copying followed by a failed commit. The extraction path learned this expensively when
silently-swallowed 403s on `corpus/*/_retry/*` cost a day.

## Running the whole thing from a laptop

```bash
aws sso login --sso-session tools1

# 1. Watch it. Brings up OpenSearch + the service with the Promotion panel enabled.
eval "$(aws configure export-credentials --profile dev1 --format env)"
ACI_LOCAL_DATA=./data-r5 docker compose \
    -f docker-compose.yml -f docker-compose.promotion.yml up -d --build
open http://localhost:8000/            # Extraction tab -> Promotion panel

# 2. Decide. Writes nothing.
scripts/promote_local.sh 2026-08-21-02 --plan

# 3. Rehearse on five documents, gallery only.
scripts/promote_local.sh 2026-08-21-02 --probe-write --limit 5

# 4. All the way: gallery, index, cut over, reload the vectors.
scripts/promote_local.sh 2026-08-21-02 --with-index
```

`promote_local.sh` exports the credentials once as static values (so every subprocess and
the container share one identity) and pre-flights the three things that are cheap to check
now and expensive to hit later: a live SSO session, Bedrock reachable in
`ACI_BEDROCK_REGION`, and the vector store reachable — that last one because the load is
the *final* stage, and discovering it unreachable after the pointer has moved leaves search
empty.

### The stages, and the order

| stage | what | resumable |
|---|---|---|
| `gallery_seed` | `--seed-gallery-from-latest` only: carry forward the live gallery's entries this run isn't rewriting | yes |
| `select` `copy` `manifest` `verify` | the gallery (Phase 1 above) | yes |
| `seed` | download `index/<latest>/products/` into `data-<version>/` | yes, per object |
| `content` | download each promoted doc's stage-3 **markdown only**, `tree_to_content.py`, then `merge_guidance_alerts.py` against the incumbent | yes |
| `embed` | one `aci reembed` | yes |
| `flat` | `aci reindex` → `multi.npz` + manifest | cheap re-run |
| `publish` | `aci publish <bucket> <version> **--no-set-latest**` | yes |
| `index_verify` | **the gate** | pure |
| `cutover` | `index/latest = <version>` | one object |
| `vectors` | `load_vectors.py` — delete and refill `aci-vectors` | re-runnable |

Re-running with the same `--version` skips finished stages, so a failure an hour in does not
cost the embedding again.

### Three things that are load-bearing

**Seeding is not optional.** `aci reindex` builds `multi.npz` from whatever is in the data
dir. A dir holding only the promoted regions produces an index containing only those regions,
and flipping `index/latest` to it **deletes every other jurisdiction from search** — no
error, no log, the corpus just gets smaller. So the incumbent version's artifacts are
downloaded first (~1GB; `--seed-from <dir>` uses a local dir instead), and `index_verify`
asserts the result is a superset. That check is the single most valuable thing in this half
of the pipeline.

**The stale `.npz`.** `aci reembed` skips a region whose `.npz` already reports the target
model. Right for a re-embed, exactly wrong here: a promoted region gets a new `content.json`
while its seeded `.npz` still says `titan`, so reembed would skip it and the region would
keep vectors describing the *previous* extraction. So the `content` stage **deletes the
`.npz` of every region it rewrites**, and one `aci reembed` then rebuilds exactly those. No
library change, and the invariant lives next to the code that depends on it. (The general
fix is a content fingerprint on the `.npz` — worth doing when something else needs it.)

**Two documents, one region.** A jurisdiction routinely holds a superseded opinion beside
the current one, and the index keys content by product+jurisdiction — on Marketing
Restrictions - Asset Management that is 17 of 88 jurisdictions. Resolved by `keeper_stems`
(the validated Survey/Memorandum rule, which is why the promotion reads `corpus-src`), then
gate, then score, then doc id. Every collision and its winner is recorded in the progress
object and printed: silently choosing between two legal opinions is not a thing to do
quietly.

### The verify gate

Runs while **nothing a reader can see has changed**, which is why a failure has nothing to
roll back:

1. the manifest has a row count, and names this version;
2. `rows` equals `multi.npz`'s matrix — the check both silent partial loads needed and
   neither had (`count` has always been the *jurisdiction* count);
3. **every region in the live index is present in the new one** (see Seeding);
4. every promoted region made it in — otherwise the gallery offers documents the index
   cannot answer about;
5. every region has readable artifacts and at least one section (five regions once published
   as *empty*, which is invisible rather than obviously broken);
6. the published prefix holds roughly what was built.

On failure: nothing is flipped, `index/<version>/` stays staged, and the fix is to re-run
with the same `--version`.

### What it cannot do

**Cutting over does not make anything searchable for a pod that is already running.**
`/data` is a read-only volume mount whose prefix is fixed at pod creation (`Dockerfile:6-13`
— the artifacts are mounted, not baked), so flipping `index/latest` changes what the *next*
pod resolves and nothing else. A running pod keeps the previous version's `content.json`, and
the newly promoted regions get dropped from results with a `BundleUnavailable` warning per
region until it is replaced:

```bash
kubectl -n <ns> rollout restart deploy/core-index
# locally, against the new artifacts:
ACI_LOCAL_DATA=./data-<version> docker compose \
    -f docker-compose.yml -f docker-compose.promotion.yml up -d --force-recreate core-index
```

The promotion's final report says this, and so does the Promotion panel. Do not read
"cut over" as "live".

**The vector reload is destructive.** `load_vectors.py` deletes `aci-vectors` and refills it,
so there is a window — around four minutes on this corpus — where search returns nothing, and
no rollback target while it runs. That is the trade this mode makes deliberately. A partial
load *raises* rather than reporting success, because 2,000-of-84,197 and 11,000-of-77,939
both once looked like clean runs.

### Rolling back

| after | undo |
|---|---|
| anything up to `publish` | nothing is live; delete `index/<version>/` only to abandon it (**that deletes its gallery too**) |
| `index_verify` failed | nothing to undo — that is why the gate is there |
| `cutover` | `aws s3 cp - s3://<bucket>/index/latest <<< "<previous>"`. Running pods are unaffected |
| `vectors` | re-run `load_vectors.py` against the previous version's artifacts |
| pods rolled | flip the pointer back **first**, then `kubectl rollout undo` |

Requires `index/<previous>/` to still exist — check the bucket's lifecycle policy before
relying on it.

## Still not built



The stages are already declared in `promotion_monitor.STAGES` with `phase: 2` and render greyed
in the UI, because "in the gallery" is not "searchable" and a screen that hid the remaining work
would let a staged promotion read as a finished one. `percent` counts implemented stages only,
so a completed Phase 1 reads 100%.

The local chain above trades safety for simplicity in two places, and both have a designed
replacement that is not built:

1. **Zero-downtime cutover.** Versioned `aci-vectors-<version>` behind the `aci-vectors`
   alias, flipped after the gate — `docs/VECTOR_INDEX_LIFECYCLE.md` requirement 1. The
   reference implementation is already in-repo for the entity index
   (`sync_atlas_search._swap_alias`), and its "a concrete index named like the alias exists —
   drop it first" branch is the one that would execute on the first flip. **That first flip
   deletes the concrete `aci-vectors` index**, so a one-off server-side `_reindex` into
   `aci-vectors-<current>` is a manual, blocking prerequisite per environment.
2. **A readiness gate, so no pod ever serves a mismatched pair.** The alias is global; the
   data mount is per-pod. During any rolling update, old pods (old `content.json`) and new
   pods (new) read through one alias — which is the 13 August configuration. A `/readyz` that
   requires mount version == alias version and `store_rows == n_rows` makes the rollout stall
   until the alias moves; better still,
   `vector_backend.index_name()` preferring the version the pod's own manifest carries makes
   the mismatched state unrepresentable.
3. **A content fingerprint on the `.npz`** (`content_sha`, ~12 lines following the
   `"kinds" in z.files` back-compat precedent), which would let `aci reembed` be correct on
   its own rather than relying on the content stage deleting the file first.
4. **Running it in the cluster.** `deploy/promotion-job.yaml` covers the gallery half. The
   index half needs Bedrock and OpenSearch from the pod, ~20Gi of ephemeral storage, and the
   IAM to write `index/latest`.

Open questions, in order of value:

1. **Does the Deployment pin `INDEX_VERSION` or resolve `index/latest` at startup?** Answerable
   only from the `k8s-deployment` chart. It decides whether cutover completes unaided or hands
   off to the CD pipeline.
2. Marketing Restrictions has no incumbent `content.json`, so its ~88 regions would ship as
   clause bodies only — no curated guidance or alerts. A product decision.
3. `scripts/eval_cases.py`'s gold workbooks cover Data Privacy and Shareholding Disclosure
   only, so MR-AM's first promotion is structurally verified but **not quality-verified**.
4. AI Mode applies a hard `_SEARCH_MIN_SCORE` floor with `k=40`, so a product can be correctly
   indexed and still return nothing there. **"Visible in Search Mode" is a weaker claim than
   "answerable in AI Mode"** — worth measuring on MR-AM before calling the feature done.
