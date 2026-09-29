# Run logging — debugging an extraction from Kibana

## Why this exists

On the MRAM run, Canada and New Zealand appeared to hang and there was nothing to look at
afterwards. Three separate things caused that, and all three are now closed:

| What was missing | Why it hid the problem |
|---|---|
| Nothing was printed *during* a stage | The S3 ledger gets a row only when a document **finishes or fails**, so a hang produces no row at all. `progress.json` was rewritten only on a stage transition, so a hang inside one stage left a timestamp quietly ageing. |
| Pod stdout is not durable | `kubectl logs` works only while the pod exists. On spot capacity it frequently does not. |
| Stdout was prose | `[stage1] pages=57 …` cannot be filtered, counted, or alerted on. |

The fix is **one JSON object per line on stdout**. The node's log agent already ships this
container's stdout to Kibana, so a print here is an indexed document there — no Elasticsearch
client in the pod, no endpoint, no API key, nothing to flush, and nothing that can fail while
the run is failing.

```
scripts/corpus_worker.py ─┐
scripts/run_corpus.py     ├─ log.event(...) → stdout → log agent (node) → Kibana
scripts/summary_ai_*.py   ┘
```

## What the cluster actually runs (confirmed 2026-09-17)

Read off a live record from the tenant Kibana:

| | |
|---|---|
| Agent | **Filebeat 9.0.0**, DaemonSet `filebeat-beat-filebeat`, namespace `elastic-stack`, ECK-managed |
| Input | `filestream` over `/var/log/containers/*.log` — i.e. **container stdout**, exactly the path these events take |
| Data stream | `logs-tenant-stage1-*` (backing indices `.ds-logs-tenant-stage1-<date>-<n>`) |
| Cluster seen | account `865626945245`, **eu-west-2**, node group `stage1-ng-…`, `m6a.2xlarge`, on-demand |
| JSON | **Parsed and merged at the root.** The sample record carries `log.level`, `log.logger`, `log.origin.*` and `service.name` as dotted root fields taken from the line's own JSON |

Two consequences, both already handled in the code:

**1. Dotted root keys are exactly right.** `aci.stage`, `event.action` and `log.level` land as
queryable fields alongside Filebeat's own, in the same shape as `log.logger` does in that
record. Keep `ACI_LOG_FORMAT=json`.

**2. A key Filebeat also sets is a collision, and with `overwrite_keys` ours wins.** That is a
trap, not a feature: Filebeat's `host.name` is the **node** hostname, and emitting the pod name
there would silently destroy it. The same collision is visible unresolved in the cluster's own
records — `ecs.version` ends up two-valued, `8.0.0` and `1.6.0`, from precisely this merge. So
the worker emits `aci.pod` / `aci.node` and never touches `host.*`, `kubernetes.*`,
`container.*`, `agent.*`, `input.*` or `ecs.*`. There is a test pinning that.

### Fields Filebeat adds for free

Do not duplicate these — filter on them:

`kubernetes.pod.name` · `kubernetes.namespace` · `kubernetes.node.name` ·
`kubernetes.labels.*` · `kubernetes.node.labels.*` (instance type, zone, capacity type —
so "every stall is on spot" is a one-filter question) · `container.id` · `container.image.name` ·
`host.name` (the node) · `stream` (`stdout`/`stderr`) · `log.file.path` · `agent.*`

## Turning it on and off

`ACI_LOG_FORMAT`, set in [`deploy/extraction-job.yaml`](../deploy/extraction-job.yaml):

- **`json`** (default) — for an agent that parses JSON stdout into fields. Every key below
  becomes its own queryable field.
- **`logfmt`** — for an agent that does **not**. A JSON blob would land as one opaque
  `message` string; `stage=stage2_mineru pages=57` stays greppable and can be split by an
  ingest pipeline later. Same events, same field names.
- **`off`** — emit nothing structured.

Which one is right is a property of the cluster's log agent, so it is a deploy-time value
rather than a code change. Check by expanding one existing `corpus-extract` line in Kibana: if
a line that is JSON arrives as separate fields, use `json`.

## The event vocabulary

Every event carries `event.action`. One filter per row:

| `event.action` | When | The fields that matter |
|---|---|---|
| `run.start` | worker boot | `aci.run_id`, `aci.shard`/`aci.shards`, `aci.mode`, `aci.results`, `aci.argv` |
| `run.env` | worker boot | `aci.env.*` — GPU name, VRAM, torch/triton/MinerU versions, resolved models dir, `/dev/shm` size, C compiler |
| `run.resume` | worker boot | `aci.done_already`, `aci.permanent`, `aci.marked_retry` |
| `run.plan` | `mode=plan` | `aci.jobs`, `aci.pages`, `aci.estimate_hours` |
| `sources.start` / `sources.end` | the 1.23 GB source sync, before any work | `event.duration_s`, `aci.source_files` |
| `doc.start` | per document | the whole `aci.*` identity block below |
| `stage.enter` | each stage **and each fallback tier** | `aci.stage` |
| `stage.heartbeat` | **every 60s** | see *The stuck detector* |
| `stage.fallback` | the chain is entered | `aci.fallback_reason`, `aci.fallback_first.*` |
| `stage.end` | each stage | `event.duration_s`, `event.outcome`, `error.type` on failure |
| `bedrock.call.start` | **before** a model call | `aci.model`, `aci.images`, `aci.payload_mb`, `aci.max_tokens` |
| `bedrock.call.end` | after it | `event.duration_s`, `aci.tokens_in`/`out`, `aci.stop_reason` |
| `bedrock.retry` | each retried attempt | `aci.attempt`, `aci.http_status`, `error.type` |
| `stage4.config` | stage 4 starts | `aci.model`, `aci.text_model` — which model the money was spent on |
| `doc.scored` | every completed document | `aci.gate_extraction`, `aci.weakest_dimension`, `aci.dim.*` (one field per dimension), `aci.chunks` |
| `doc.scored_post_ai` | stage 4/5 ran | `aci.gate_post_ai`, `aci.dim.*`, **`aci.drop.*`**, `aci.worst_drop_dimension` |
| `section.rejected` | **per section** the AI pass refused | `aci.section` (the file), `aci.section_reason`, `aci.stop_reason`, `aci.rows_before`/`after` |
| `section.lossy` | **per section** accepted but missing cell content | `aci.section`, `aci.cell_chars_lost`, `aci.cell_chars_lost_pct` |
| `doc.end` | per document | `aci.gate`, `aci.worst`, `aci.status`, `aci.route`, `aci.cost_usd`, `aci.step.*` |
| `doc.fail` | uncaught exception | `error.stack_trace` (**full**), `aci.failed_stage` |
| `doc.classified` | after `doc.fail` | `aci.cause`, `aci.permanent`, `aci.retryable` |
| `upload.end` | per document | `aci.upload_bytes`, `aci.upload_mb_s` |
| `shard.progress` | per document | `aci.shard_done`/`aci.shard_jobs`, `aci.pages_done` |
| `run.signal` / `run.stopping` | SIGTERM, spot reclaim | `aci.signal`, `aci.reason`, `aci.remaining` |
| `run.end` | worker exit | `aci.ok`, `aci.failed`, `aci.upload_failed`, `aci.stopped_early` |

### Stages 4 and 5, and the failures that used to be invisible

Three things about the AI pass were previously only ever a `print`, and all three are the kind
of thing you find out about weeks later:

- **A pass that accepted nothing.** `sections_accepted: 0` of 8 still writes a
  `04_stage4_ai/` directory and still reports `gate=pass`, because the gate reads stages 1-3
  and nothing downstream reads a stage-4 rejection count. That is the 2026-09-08 run: gate=pass,
  178s, $0.00, zero sections processed. `stage.end` for `stage4_ai` now carries the counts and
  goes to `warn` when nothing was accepted.
- **A swallowed failure.** `_run_stage4_in_pipeline` catches everything — correctly, since a
  paid pass that fails must leave a completed extraction completed — so an outage (an IAM
  `AccessDenied` on `bedrock:InvokeModel`, say) produced `status: done, gate: pass`, no AI
  output, and no error anywhere. Now it emits `stage.end` with `event.outcome: failure` and
  **`aci.swallowed: true`**, which is the field that separates "the run continued
  deliberately" from "the run was fine".
- **Quality the AI pass destroyed.** Measured on Bahamas 183503: completeness 99.2 → 94.6 and
  fidelity 91.9 → 45.8, and, in the code's own words, *no screen has ever said so*.
  `doc.scored_post_ai` emits one `aci.drop.<dimension>` field per dimension that lost ground,
  and goes to `warn` when any drop is ≥ 1.0 — so a regression is a sortable column across the
  whole run instead of two scorecards per document opened by hand.

### Which section of the document failed

The aggregate says *a* section failed, never which — so stage 4 emits one event per section
it got wrong, named by its file, which maps straight to a heading in the document.

- **`section.rejected`** — `ok: false`. The AI output was refused and stage 3's text kept, so
  the document is unharmed; the pass simply bought nothing there. `aci.section_reason` is the
  half that matters: it separates *"the prompt did not work on this table"* from *"nothing
  ran"*. A run with no AWS credentials failed all 8 sections in four seconds and reported
  `0/8 accepted, $0.0000` — indistinguishable on screen from a model that read every page and
  declined to change anything.
- **`section.lossy`** — `ok: true` **and cell content went missing anyway**. The dangerous
  one: it was accepted, so it ships, and the loss shows up only by comparing cell characters
  before and after. This is the document-level fidelity collapse located to the individual
  section that caused it.
- `aci.stop_reason: max_tokens` is a silent truncation rather than a refusal — the section
  comes back plausible and short.

Per-section metrics are `aci.section_cost_usd` / `aci.section_tokens_*`, deliberately **not**
`aci.cost_usd` / `aci.tokens_*`: those carry the document total on `stage.end`, and a Kibana
sum over a field meaning both would double-count every run's spend. Section events are capped
at 40 per document, with a `section.rejected.truncated` record saying how many were omitted.

Stage 5 reports an end event only, never an entry: it runs inside stage 4's call, so its
timing is readable only after it has finished, and announcing an entry then would put a
`stage.enter` *after* the stage it describes. Heartbeats during sub-chunking therefore say
`stage4_ai`; separating them live would need a callback inside `run_stage4`, which is a bigger
change than the signal is worth.

`doc.start` without a matching `doc.end` or `doc.fail` **is** the hang. `run_one` is wrapped
rather than instrumented internally precisely so that no return path can go missing and
manufacture a false one.

## The identity block (bound once, on every line)

This is the MDC idea: bind at the seam where identity changes, and every line below inherits
it — the call site that forgot to pass `jurisdiction` is still findable.

`service.name` · `service.version` (image tag) · `aci.pod` · `aci.node` ·
`aci.run_id` · `aci.shard` · `aci.proc_id` · `aci.product` · `aci.jurisdiction` ·
`aci.doc_id` · `aci.label` · **`aci.doc_run_id`** · `aci.doc_index`/`aci.doc_total` ·
`aci.pages` · `aci.stage`

**`aci.doc_run_id`** is the one to reach for. A fresh id per *attempt*, so filtering on it
returns exactly the lines of one pass and nothing from the retry that reprocessed the same
jurisdiction an hour later — and a stuck document is almost always one that has been attempted
more than once.

## The stuck detector

A heartbeat alone only says "still alive". `stage.heartbeat` samples the things that move when
work is happening and stop when it is not:

| Field | Reading it |
|---|---|
| `aci.out_bytes_delta` | Job-directory growth since the last beat. **Zero is the interesting value.** |
| `aci.stalled_beats` | Consecutive beats with no growth. At 5 the event's `log.level` becomes `warn`. |
| `aci.child_status` | `D` = blocked on I/O · `Z` = MinerU died and was never reaped · no child during `stage2_mineru` = the stall is in our own code |
| `aci.cpu_pct` | 0% with no output = deadlock or network wait; 100% with no output = runaway loop. Different bugs. |
| `aci.gpu_mem_used_mb` | 0 during `stage2_mineru` means it is not on the GPU at all |
| `aci.scratch_free_gb` | The 60Gi `ephemeral-storage` limit filling looks exactly like a hang from outside |
| `aci.stage_elapsed_s` | Time in the current stage — **not** a stuck signal on its own: a 165-page document and a hang are indistinguishable by elapsed time |

## Queries worth saving

Scope every one of these to the workers first — the data stream carries the whole tenant:

```
kubernetes.namespace: "aosphere-extraction" and aci.run_id: "<the run>"
```

```
# every wedged document in the run — make this an ALERT, not a dashboard panel
event.action: "stage.heartbeat" and aci.stalled_beats >= 5

# started and never finished (run the same window for doc.end/doc.fail and diff)
event.action: "doc.start"

# where the time goes: average event.duration_s, split by aci.stage, by aci.product
event.action: "stage.end"

# the MRAM Bedrock suspect: calls that ran long, and every retry behind them
event.action: "bedrock.call.end" and event.duration_s > 300
event.action: "bedrock.retry"

# failures ranked by cause
event.action: "doc.classified"     → top values of aci.cause × aci.failed_stage

# the AI pass did nothing, or broke something, while still reporting a pass
event.action: "stage.end" and aci.stage: "stage4_ai" and aci.sections_accepted: 0
aci.swallowed: true
event.action: "doc.scored_post_ai" and aci.worst_drop >= 1.0

# WHICH section failed, across the whole run — top values of aci.section, aci.section_reason
event.action: "section.rejected"
event.action: "section.lossy" and aci.cell_chars_lost_pct > 10
aci.stop_reason: "max_tokens"

# one document's whole story, in order
aci.doc_run_id: "<id from any line>"
```

## Rules this follows

- **No clause text, ever.** Counts, durations, page numbers, model ids and gates only. The
  data-residency guardrail in [PRODUCTIONIZATION_PLAN.md](PRODUCTIONIZATION_PLAN.md) applies to
  logs as much as to inference.
- **No big arrays.** `pages_snapshotted` and the full warning list stay in `report.json` on S3,
  for the reason [`_log_stage1`](../scripts/corpus_worker.py) already gives: 60 lines per
  document is ~65,000 lines per run, each its own Kibana record with the useful fields buried.
- **Nothing here may raise.** Every probe and every emit is individually guarded — a
  diagnostic that can fail the run it is diagnosing is worse than no diagnostic.
- **The S3 progress channel stays.** `_progress/shard-N.json`, `ledger-N.jsonl` and
  `_failures/` are independently useful and readable without cluster or Kibana access. The
  heartbeat thread now drives both, so they cannot disagree about whether the worker is alive.
