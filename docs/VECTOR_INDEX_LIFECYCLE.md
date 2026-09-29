# Vector index lifecycle — versioned, observable, and gated on readiness

How `aci-vectors` should be built, swapped and rolled back. Written after the 2026-08-13
cutover from the docx-derived index to the PDF-derived one, which failed in four distinct
ways — each of which maps to one requirement below.

## What went wrong on 13 Aug (the evidence)

| symptom | cause |
|---|---|
| every search returned **500** for ~an hour | dev's OpenSearch still held docx-era vectors while `/data` had moved to the PDF index. Hits named regions (`European Union`, `Hong Kong`, `Turkey`, `United States - States and Territories`) whose content.json no longer exists; `get_bundle` raised and took the whole request down |
| the reindex **silently truncated** at 2,000 docs, twice | `helpers.bulk(..., raise_on_error=False)` with `batch=2000`. Each vector is 1024 floats, so one chunk is ~16 MB of JSON from a memory-limited pod. At `batch=500` (~4 MB) the same load ran to completion |
| the load looked like it had **never run** | job status lives in one pod's memory (`vector_load._STATE`). The POST landed on one replica, every GET on another, so the API answered `phase: idle` while a job was live — and answered `idle` again after it died |
| search returned **200 with no hits** for ~30 min | the index existed with 2.6% of the corpus. Nothing distinguishes "loaded" from "loading" from "died half-way" |

## Requirements

### 1. Indexes are versioned; traffic moves by alias

Write to `aci-vectors-<index-version>` (e.g. `aci-vectors-2026-08-13-titan-hybrid-r2`) and
have the service read through the alias `aci-vectors`. Never delete-then-fill the index the
service is reading.

    aci-vectors-2026-07-07-titan-chunked   <- alias points here, still serving
    aci-vectors-2026-08-13-titan-hybrid-r2 <- loading, invisible to traffic
    (verify count == expected, then move the alias atomically)

* **Rollback is an alias flip**, seconds, and the previous index is still there.
* **No downtime**: today the loader deletes first, so the corpus is unsearchable for the
  ~4 minutes of the load — and if the load dies, the truncated index keeps serving.
* Keep the previous N versions; prune older ones on a schedule, never as part of a load.

Pair this with the S3 index version and the Doc Library prefix, which already follow
`index/<version>/` — one version string should name all three.

### 2. Load status lives in OpenSearch, not in a pod

A small status index (`aci-index-status`), one document per load:

    {"version": "...", "phase": "loading|done|failed", "done": 43500, "total": 76068,
     "started_at": ..., "finished_at": ..., "error": null, "host": "<pod>"}

Written at the start, on each batch, and on terminal state. Any replica can then answer
`GET /api/admin/reindex` truthfully, the state survives the pod that ran the load, and a
dead loader is visible as `phase: loading` that stopped advancing rather than as silence.

`raise_on_error=False` must stop swallowing the outcome: record the bulk error count and
fail the job when `count != total`.

### 3. Pod readiness depends on the index being ready

A pod must not take traffic while the vectors it needs are absent or half-loaded. Readiness
should check that the index version the pod's `/data` mount resolves to is the version the
alias points at, and that its document count matches the manifest.

That is what stops the two failure modes we hit: new pods serving new content against old
vectors (500s), and pods serving a 2.6%-loaded index (empty results). During a rollout the
new pods stay unready until their index version is live, so the old pods keep serving —
which is the behaviour a rolling deploy already knows how to wait for.

### 4. The load is resumable, and does not run inside a serving pod

`_id` is currently the row ordinal (`str(i)`), so ids are meaningless across corpora and a
partial load cannot be continued — only restarted from a delete. Key documents by
`<region>|<key>|<kind>|<chunk>` instead: re-running becomes idempotent, and a resumed load
skips what is already there.

The load itself is a batch job (a Kubernetes Job or a CLI run against the domain), not a
background thread in a request-serving pod that a rolling deploy or a liveness probe can
kill mid-flight. Today it is the latter, which is why a deploy silently truncated it.

### 5. Batch size is a property of the payload

`batch=2000` is a sane default for text rows and a trap for 1024-dim vectors. Default to
500 for a vector load, and derive it from the dimension if it is ever configurable.

## Sequence, once the above exists

    aci publish <bucket> <version> --no-set-latest     # data to S3, nothing live yet
    load  aci-vectors-<version>                        # job; status in aci-index-status
    verify count == manifest total                     # gate
    flip  index/latest -> <version>                    # data + gallery follow the version
    flip  alias aci-vectors -> aci-vectors-<version>   # traffic moves, atomically
    roll pods                                          # readiness holds them until ready

Rollback: flip the alias and `index/latest` back. Both are single writes, and the previous
index and S3 prefix are untouched.
