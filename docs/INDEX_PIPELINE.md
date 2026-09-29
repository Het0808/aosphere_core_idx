# Index build & delivery pipeline (all jurisdictions, grouped by region)

Goal: build the GraphIndex for **all ~130 jurisdictions**, tag each by its
**region group** (Europe / Americas / APAC / MEA / …), publish the artifacts to
an **S3 index bucket**, and have the Docker image **sync them at build time** so
the deployed service is self-contained.

Terminology: **jurisdiction** = a country/zone (France, Germany, …);
**region** = a group of jurisdictions. The flat index carries both as metadata.

---

## 1. Region grouping

- A mapping `jurisdiction → region` lives in `src/aosphere_core_index/regions/region_map.py`
  (seeded from continent/geo grouping; overridable to match aosphere's own grouping).
- Every section-index row and every `content.json` gains a `region` field alongside
  `jurisdiction` + `clause_key`.
- Search results group **region → jurisdiction → clause**; the UI region chips become
  region groups (collapsible) with jurisdictions under them.

Decision needed: confirm the canonical region grouping (use aosphere's product/region
taxonomy if one exists, else the geo default).

---

## 2. Build (the "indexer")

A single command builds everything, resumably:

```
aci build-all [--regions Europe,APAC] [--jurisdictions France,Germany] \
              [--workers N] [--embedder fastembed|titan] [--force]
```

Per jurisdiction (skip if up-to-date via a content hash / source ETag):
1. read-only S3 fetch of `working/<Jurisdiction>/` (docx + sidecars)
2. extract → typed sections (clause keys) + footnotes + chunks
3. semantic alert mapping (last 6 months)
4. embed sections → `<J>.sections.npz`
5. write `<J>.content.json` (tagged with region + jurisdiction)

Then build the combined flat index `_multi/multi.npz` (region + jurisdiction + clause_key
per row) and a `manifest.json` (versions, per-jurisdiction hash, counts, model name).

Properties:
- **Resumable / idempotent** — a jurisdiction is rebuilt only if its source changed.
- **Parallel** — `--workers` over jurisdictions (embedding is the cost; ~1 min/jurisdiction
  on FastEmbed CPU → ~2 h for 130, or fast + ~$1–7 on Titan).
- **Two embedder backends** behind the existing `Embedder` interface (FastEmbed now,
  Titan when Bedrock access lands) — model name recorded in the manifest so a model
  change invalidates cleanly.

---

## 3. Publish to S3 (versioned)

Artifacts go to a **writable index bucket** (NOT the read-only source bucket):

```
s3://<INDEX_BUCKET>/index/<version>/
    manifest.json
    regions/<Jurisdiction>/artifacts/<J>.sections.npz
    regions/<Jurisdiction>/artifacts/<J>.content.json
    _multi/multi.npz
s3://<INDEX_BUCKET>/index/latest        # pointer file -> <version>
```

```
aci publish --bucket <INDEX_BUCKET> --version 2026.06.23   # uploads + updates latest
```

- Versioned prefixes → reproducible builds + instant rollback (pin a version).
- `latest` pointer for "build the newest".
- Idempotent upload (only changed objects).

Decisions needed: the **index bucket name** and an **IAM principal with write** to it
(the current `s3-operation-service` user is read-only). The indexer also needs read on
the source bucket (the existing read-only policy) + write on the index bucket.

---

## 4. Sync at Docker build time

The image stays self-contained; the data comes from S3 at build:

**Recommended — CI step before build (OIDC role on the runner):**
```yaml
- uses: aws-actions/configure-aws-credentials@v6
  with: { role-to-assume: ${{ vars.AWS_ROLE_ARN }}, aws-region: ${{ vars.AWS_REGION }} }
- run: |
    VERSION=$(aws s3 cp s3://$INDEX_BUCKET/index/latest -)
    aws s3 sync s3://$INDEX_BUCKET/index/$VERSION/ data/
- run: docker build -t ... .        # existing Dockerfile COPYs data/ → baked in
```
No credentials in the image; `data/` is populated in the build context, then `COPY . .`
bakes it. The Dockerfile is unchanged.

**Alternative — sync inside the Dockerfile** (BuildKit secret mount), if the build must
own the pull:
```dockerfile
RUN --mount=type=secret,id=aws \
    aws s3 sync s3://$INDEX_BUCKET/index/$VERSION/ /app/data/
```
Build pin: pass `--build-arg INDEX_VERSION=<v>` so an image is tied to an index version.

---

## 5. Refresh & ops

- **Alerts append frequently** → run `build-all` on a schedule (or just re-map alerts +
  re-publish) to refresh `content.json` without re-embedding sections; bump version.
- **Add a jurisdiction** → it's auto-discovered; build only it, rebuild `multi.npz`, publish.
- **Cost** — embedding is the only real cost; near-zero on FastEmbed (compute you run),
  ~$1–7 one-time on Titan. Storage/transfer negligible (~0.4 GB for 130 regions).
- **Size note** — at 130 regions `multi.npz` (~100 MB+) exceeds git's file limit, which is
  exactly why it lives in S3, not the repo.

---

## Build order (proposed)

1. Region map + add `region` to section index / content / multi-index metadata.
2. `aci build-all` (resumable, parallel, two embedder backends).
3. `aci publish` (versioned S3 layout + manifest + `latest`).
4. CI: OIDC + `s3 sync` step before the existing docker build; pin version.
5. UI: group results region → jurisdiction → clause.

## Inputs needed before building
- Writable **index bucket** name + an IAM role/keys with write to it (+ read on source).
- **Embedder** for the full run: FastEmbed (free/local, ~2 h) or Titan (needs Bedrock access).
- Confirm the **region grouping** taxonomy.

---

## Serving the index: versioning, rollout, rollback

Publishing artifacts to S3 is only half the pipeline — the other half is getting them into
the vector store without an outage. The 2026-08-13 cutover to PDF-only extraction failed
four ways doing exactly that (a silently truncated load, status that lived in one pod's
memory, pods serving new content against old vectors, and a delete-then-fill that left the
corpus unsearchable).

**[VECTOR_INDEX_LIFECYCLE.md](VECTOR_INDEX_LIFECYCLE.md)** carries the requirements that
came out of it, with the evidence: versioned indexes behind an alias so rollout and
rollback are a single atomic flip, load status stored in OpenSearch rather than a pod,
readiness gated on the index version being live and complete, a resumable load that runs
as a job rather than a thread in a serving pod, and a batch size sized for 1024-dim
vectors instead of text rows.

One version string should name all three artifacts — the S3 index prefix, the Doc Library
prefix (`index/<version>/doc-gallery`) and the vector index (`aci-vectors-<version>`) — so
a rollback moves them together.

## Promotion: where a run enters this pipeline

A finished GPU extraction run reaches this pipeline through a **promotion**
(`scripts/promote_run.py`, `docs/PROMOTION_PIPELINE.md`).

Phase 1 publishes the run's documents to `index/<version>/doc-gallery/` and stops there: it
never writes `index/latest`, so the staged version is readable (`?version=` on the Doc Gallery)
but nothing points at it and nothing is searchable.

Phase 2 is the rest of this document, driven by the same job and the same version string —
which is what makes the "one version names all three artifacts" rule above enforceable rather
than aspirational: `index/<v>/`, `index/<v>/doc-gallery/` and `aci-vectors-<v>`. Two
prerequisites in this file's own code are called out there: `_reindex()`'s `manifest.json`
needs `rows`/`version`/`dim` (its `count` is the jurisdiction count, so "rows loaded == rows
expected" is currently unanswerable), and `aci publish` needs to write a
`_publish_complete.json` last so a reader can tell a complete prefix from one mid-upload.
