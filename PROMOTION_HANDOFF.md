# Handoff: promoting an extraction run (aosphere-core-index)

Paste this as the opening message of a new session, with the repo connected.
Everything below is current as of commit `e9fcc33`.

---

## 1. Where things stand

**Branch `feat/promote-extraction-run`, commit `e9fcc33`, committed but NOT pushed.**

```bash
# run this from your Mac terminal — see §8 for why it wasn't done for you
cd ~/Documents/Projects/aosphere-core-index
git push -u origin feat/promote-extraction-run
```

33 files, +6673/-170. 1120 tests pass (baseline was 931 — 189 new), `ruff check src tests`
clean, which is exactly what CI runs (`.github/workflows/ci.yml`: ruff pinned 0.6.9, then
`pytest -q`).

Branched off `feat/per-product-prompt` (not `main`) — that was the checked-out branch.

## 2. The problem this solves

A GPU extraction run writes to `s3://<bucket>/corpus/<run_id>/`. The Doc Gallery reads
`index/<version>/doc-gallery/` and the search index reads `index/<version>/`. Nothing carried
a document from the first to the second for a **cluster** run: `scripts/push_hybrid_s3.py` is
a local-tree publisher end to end (local paths, a local PDF base64'd into the viewer, `fitz`
for the page count, an SSO profile no pod has). `extraction_monitor.py:745` and
`corpus_worker.py:787` both state the gap outright.

A **promotion** is that missing step, in two halves:

| | |
|---|---|
| Phase 1 | run → published Doc Gallery, at a **staged** index version |
| Phase 2 (`--with-index`) | that version → searchable, and cut over |

## 3. Decisions the user made (do not re-litigate)

- **Phasing**: gallery first, then the full index chain.
- **Eligibility**: `scorecard.gate ∈ {pass, review}`; `fail`/`error` held back and reported.
- **Products**: Data Privacy, Shareholding Disclosure, Marketing Restrictions - Asset
  Management — i.e. exactly `regions/region_map.py::PRODUCTS`, never hardcoded.
- **Run from local**, not the cluster, for the index half.
- **Content source**: download stage-3 trees from the S3 run (not the local `out/corpus`),
  so the gallery and the index provably describe the same documents.
- **Data dir**: a fresh per-promotion `data-<version>/` (gitignored by `data-*/`).
- **Shape**: one orchestrator (`promote_run.py --with-index`), not two commands.
- **Cutover**: destructive — flip `index/latest`, then clear and reload `aci-vectors`. The
  versioned-index-behind-an-alias design was offered and deliberately not taken.
- **Execution model for Phase 1 in-cluster**: K8s Job + durable S3 progress (my
  recommendation; the user never explicitly picked, and it is what was built).

## 4. Facts about this codebase that took real digging

These are load-bearing and non-obvious:

- **`corpus_worker.build_review_artefacts` already writes `viewer.html` and `inspect.html`
  into each S3 job dir.** This is why Phase 1 is a server-side `copy_object`, not a rebuild.
- **Never list a run recursively**: measured 380,609 keys / 64.4s recursive vs 986 prefixes /
  0.9s by delimiter, on run `2026-08-21-02`.
- **Never retain scorecards**: ~97KB each, 96MB for one 986-doc run, in the same process as
  the vector matrix. "The gallery loaded every scorecard" took the service down once.
- **`push_hybrid_s3.py:551`**: a partial publish that stripped every `-mineru` row turned
  **455 published documents into 5 manifest entries**.
- **Data is NOT baked into the image** (`Dockerfile:6-13`) — `/data` is a read-only volume
  mount whose prefix is fixed at pod creation. Verified. This is why a cutover cannot make
  anything live for a running pod.
- **`cli._reindex`'s `count` is the JURISDICTION count**, not rows — so "did the store receive
  every row?" was unanswerable. That is what both silent partial loads needed (2,000 of
  84,197; 11,000 of 77,939). Now fixed: the manifest carries `rows`/`version`/`dim`.
- **`aci reembed` skips by MODEL only** — wrong when the content changed and the model didn't.
- **Two documents, one region is normal**: 17 of 88 on Marketing Restrictions.
  `extract_to_s3.keeper_stems` is the validated resolver (Survey for DP, Memorandum for SD).
- **AI Mode and Search Mode are UI tabs over ONE index.** No `ai_mode` flag exists. AI Mode
  additionally applies a hard `_SEARCH_MIN_SCORE` floor at k=40, so "visible in Search Mode"
  is a weaker claim than "answerable in AI Mode".
- **`_JOB` dicts in `vector_load.py` / `entity_reindex.py` are a documented anti-pattern**
  (`VECTOR_INDEX_LIFECYCLE.md` §2): per-pod memory meant the API said `phase: idle` while a
  load was running.

## 5. What was built

**New**

| path | what |
|---|---|
| `scripts/lib_gallery.py` | prefix/slug/docname helpers, importable **without fitz or MinerU**. Exists because `push_hybrid_s3` pulls `hybrid_extract → pdf2mdtree → fitz` at module scope and a promotion pod has none of it |
| `scripts/promote_run.py` | Phase 1 + the CLI. `plan_run`, `present_slugs`, `copy_doc`, `merge_manifest`, `verify_target`, `Lease`, `PromotionProgress` |
| `scripts/promote_index.py` | Phase 2 stages: seed, content, embed, flat, publish, verify gate, cutover, vectors |
| `scripts/promote_local.sh` | laptop entry point + preflight (SSO, Bedrock, vector store) |
| `src/aosphere_core_index/service/promotion_monitor.py` | `STAGES`, pure `summarize()`, `plan_run`, bounded readers, the S3 key layout |
| `deploy/promotion-job.yaml` | K8s Job for the gallery half + the new IAM role it needs |
| `docker-compose.promotion.yml` | local stack overlay for watching a promotion |
| `docs/PROMOTION_PIPELINE.md` | **the reference doc — read this first** |
| 10 × `tests/test_promotion_*.py` | 189 tests |

**Modified**: `doc_gallery.py` (`walk_run_jobs`, `store(version=)`, manifest TTL cache) ·
`app.py` (7 endpoints, `?version=` on the gallery, `promotion` in `/api/config`) · `web.py`
(Promotion panel on the Extraction tab) · `cli.py` (manifest fields) · `region_map.py`
(`product_from_dir`, `is_indexable_product_dir`) · `s3_readonly.py` (`list_level_objects`) ·
`push_hybrid_s3.py` (re-exports + `IfMatch`) · `publish_nav_only.py` (**pre-existing bug**:
referenced a `PH.PREFIX` that never existed, would `AttributeError` on its first document).

## 6. Invariants that must not be broken

1. **The manifest is written ONCE, at the end**, and the merge is an upsert by slug with **no
   full-replace branch under any flag**. Objects are invisible until the manifest names them,
   so a dead promotion leaves the live gallery untouched. Pinned by
   `test_promotion_manifest_merge.py`.
2. **Seeding is not optional.** An unseeded promotion produces an index containing only the
   promoted regions, and flipping `index/latest` to it deletes every other jurisdiction from
   search — silently. `index_verify` asserts the superset. Pinned by
   `test_promotion_index_gate.py`.
3. **The content stage deletes the `.npz` of every region it rewrites** (see the reembed skip
   rule above). Pinned by `test_promotion_index_content.py`.
4. **The verify gate sits between `publish` and `cutover`**, so a failure has nothing to roll
   back. Pinned by stage-order tests.
5. **Silence outranks the stopping flag** in the derived-state ladder: run `2026-08-20-08`
   read `stopping` with a 1010-hour ETA for 99 hours after its last write.
6. **Never hold job state in module memory** — every answer is one bounded S3 GET.

## 7. How to run it

```bash
aws sso login --sso-session tools1
eval "$(aws configure export-credentials --profile dev1 --format env)"

# watch it
ACI_LOCAL_DATA=./data-r5 docker compose \
    -f docker-compose.yml -f docker-compose.promotion.yml up -d --build
open http://localhost:8000/          # Extraction tab → Promotion panel

scripts/promote_local.sh 2026-08-21-02 --plan                 # writes nothing
scripts/promote_local.sh 2026-08-21-02 --probe-write --limit 5
scripts/promote_local.sh 2026-08-21-02 --with-index           # all the way
```

**Nothing has been run against real S3 yet** — see §8. `--plan` then `--limit 5` first.

Two things to check on the first real run:
- compare ~20 promoted gallery rows against `push_hybrid_s3`-published ones; identical
  rendering is the acceptance test for the scorecard-derived `stats`;
- `pages_snapshotted` is *approximated* from the scorecard (union of `unvalidatable` pages and
  failed tables' pages). Measure it against `doc_display_stats` on ~20 real docs.

## 8. Environment limits hit in that session

- **Could not push**: repo is private, no SSH key in the sandbox, SSH egress blocked (HTTPS
  reaches github.com but has no credentials), no `gh`. Push from the Mac.
- **Could not run against real S3**: the `dev1` SSO profile lives on macOS; the Cowork Linux
  VM has its own home and no AWS config. So every S3 path is tested against fakes only.
- **The repo's `.venv` is a macOS venv**, unusable from the Linux side. A throwaway
  `~/.venv-aci` (Python 3.12 + ruff 0.6.9 + pytest + deps) was created outside the repo for
  lint/test. Recreate with `uv venv --python 3.12` + `uv pip install "ruff==0.6.9" pytest
  fastapi numpy boto3 httpx pydantic-settings "pymupdf>=1.24" lxml pyjwt uvicorn pyyaml
  litellm openai-agents`.
- **File deletion was disabled** in the connected folder; a stale `.git/index.lock` blocked
  git until the user granted delete permission.
- `.claude/` is untracked and pre-existing — deliberately left out of the commit.

## 9. Not built, and the open questions

1. **Zero-downtime cutover**: versioned `aci-vectors-<version>` behind the `aci-vectors`
   alias. `sync_atlas_search._swap_alias` is the in-repo reference. **The first flip deletes
   the concrete `aci-vectors` index**, so a one-off server-side `_reindex` into
   `aci-vectors-<current>` is a manual blocking prerequisite per environment.
2. **`/readyz` gate** (mount version == alias version, `store_rows == n_rows`) plus
   `vector_backend.index_name()` preferring the pod's own manifest version — which makes the
   mismatched-pair state unrepresentable.
3. **`content_sha` on `SectionIndex`** (~12 lines), so `reembed` is correct without relying on
   the content stage deleting the file first.
4. **The index half in-cluster** — needs Bedrock + OpenSearch from the pod, ~20Gi ephemeral,
   and IAM to write `index/latest`.

**Blocking infra for the cluster path**: a new `corpus-promote` IAM role (the extract role has
`PutObject` on `corpus/*` only). Deliberately withheld in Phase 1: `PutObject` on
`index/latest`, and `DeleteObject` anywhere.

**Open questions for the user/team:**

1. **Does the Deployment pin `INDEX_VERSION` or resolve `index/latest` at startup?** Answerable
   only from the `k8s-deployment` chart. Decides whether cutover completes unaided. Highest
   value unknown.
2. Marketing Restrictions has no incumbent `content.json`, so its ~88 regions ship as clause
   bodies only — no curated guidance or alerts. A product decision.
3. `scripts/eval_cases.py`'s gold sets cover DP and SD only, so MR-AM's first promotion is
   structurally verified but **not quality-verified**.
4. AI Mode's score floor: a product can index correctly and still return nothing there. Worth
   measuring on MR-AM.
5. `docker-compose.promotion.yml` **was committed**, unlike the other local compose overlays
   which are gitignored. It has no secrets and the runbook references it — revert if that
   breaks a convention I misread.
