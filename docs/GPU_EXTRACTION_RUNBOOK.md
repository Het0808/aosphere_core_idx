# GPU Extraction Runbook — deploying and managing the corpus worker

How to run `Dockerfile.extract` (the GPU extraction worker, `scripts/corpus_worker.py`)
on the dev1 tenant EKS cluster, and what to do when it misbehaves.

Everything below goes through **one GitHub Action**:
[`Build & Deploy Extract Worker`](../.github/workflows/build-extract-image.yml).
You should never need to hand-edit anything in `k8s-deployment`, and you should never
`kubectl apply`, `patch` or `edit` the Job — ArgoCD owns it and will fight you.

---

## The one workflow

Actions → **Build & Deploy Extract Worker** → *Run workflow*.

| Input | What it does |
|---|---|
| `tag` | Image tag to build and/or deploy, e.g. `2026.08.34-dev-02` |
| `shards` | `CORPUS_SHARDS` **and** the Job's `completions`/`parallelism`. One pod and one GPU node per shard. Validated 1–8 |
| `mode` | What the deployed Job *does*: `plan` = rehearsal, `extract` = the real run. **Both deploy and schedule the Job** — see below |
| `build` | Untick to redeploy an existing tag without a ~25 min rebuild |
| `run_id` | S3 output prefix **and the resume key**. **Blank = keep the current one** |
| `environment` | `dev1` |

It builds (optionally), verifies the tag exists in ECR, rewrites
`helm-charts/core-index-extract/values-dev1.yaml` in `k8s-deployment`, and pushes.
ArgoCD picks up the git change and reconciles. There is no PR to raise.

### Recipes

```
Ship a code change          tag=<new>          build=true
Change the shard count      tag=<current>      build=false   shards=8
Dry run, then the real run  tag=<current>      build=false   mode=extract
Start a FRESH extraction    run_id=<new value>
Stop a run                  Actions -> Stop Extract Worker (confirm=yes)
```

### `mode=plan` is a rehearsal, not a no-op

A common misreading: `plan` does **not** mean "just build the image" or "don't deploy".
Both modes deploy the Job, and both provision a real GPU node. `mode` only changes what
the worker does once it is running. If you want to build without deploying, that is not
currently an input — the deploy step always runs.

| | `mode=plan` | `mode=extract` |
|---|---|---|
| Job created in the cluster | yes | yes |
| GPU spot node provisioned | **yes** | yes |
| ~6.9 GiB image pulled | yes | yes |
| Source corpus synced from S3 | yes | yes |
| Existing scorecards listed | yes | yes |
| **Documents extracted** | **no** | yes |
| **Anything written to S3** | **no** | yes |
| Runtime | ~4 min | hours to days |

The pod requests `nvidia.com/gpu: 1` either way, so cluster-autoscaler really does spin
a `g4dn.2xlarge` from zero. That is the point: a `plan` run exercises scale-from-zero,
the device plugin, the image pull and IRSA for real, then exits before spending GPU
hours. It costs a few minutes of spot time.

**Always run `plan` after a code change.** It has already caught an image missing a core
dependency that would otherwise have surfaced partway into a multi-day run.

---

## When it misbehaves, look in Kibana first

The worker emits one JSON event per line for every run, document, stage, heartbeat, Bedrock
call and failure, and the node's log agent already ships this container's stdout. Start with
`event.action: "stage.heartbeat" and aci.stalled_beats >= 5` — that is every wedged document in
the run. The event vocabulary, the field list and the rest of the queries are in
[RUN_LOGGING.md](RUN_LOGGING.md).

---

## `run_id` — the one input that can cost you days

`run_id` names the S3 output prefix (`s3://aosphere-tenant-dev1-core-index/corpus/<run_id>`)
and **resume is keyed on it**. A document is finished when its `scorecard.json` exists
under that prefix; the worker skips those and extracts the rest. There is no force flag.

```
run_id blank      -> keep the current prefix -> RESUME (what you want almost always)
run_id = new      -> empty prefix            -> re-extract the ENTIRE corpus
```

Leave it blank when shipping a new image into a run that is already in flight. Set it
only when you deliberately want a clean baseline.

You do **not** need to change `run_id` to deploy a new image or change the shard count.
The chart names the Job `core-index-extract-<run_id>-<hash of the values>`, so a new tag
or shard count produces a new Job object against the *same* S3 prefix. (A Job's pod
template and `completions` are immutable, so a new object is the only way — but the
naming keeps that decoupled from the run identity.)

Changing `shards` mid-run is safe: the skip rule is per-document, not per-shard, so a
different partition never redoes finished work.

---

## Watching a run

Needs the VPN — the dev1 API server allowlist does not include arbitrary IPs.

```bash
aws eks update-kubeconfig --name aosphere-tenant-dev1-eks-cluster \
  --region eu-west-1 --alias dev1 --profile aosphere-dev1

kubectl --context dev1 -n tenant get job,pod -l app.kubernetes.io/name=core-index-extract
kubectl --context dev1 -n tenant logs -f -l app.kubernetes.io/name=core-index-extract \
  --max-log-requests 8 --tail=20

kubectl --context dev1 get nodes -l workload=gpu-extract
```

Progress is also published to S3 by the worker, and finished documents are visible as
`scorecard.json` keys under the results prefix:

```bash
aws s3 ls --recursive --profile aosphere-dev1 \
  s3://aosphere-tenant-dev1-core-index/corpus/<run_id>/ | grep -c scorecard.json
```

ArgoCD application (lives on the corp-internal cluster, not dev1):

```bash
kubectl --context corp -n argocd get application tenant-dev1-core-index-extract
```

### What normal looks like

```
0:00  workflow pushes the values change
~1m   ArgoCD syncs, Job created, pod Pending
~1m   cluster-autoscaler scales the GPU group 0 -> N
~2m   spot node(s) join; device plugin advertises nvidia.com/gpu
~5m   image pulled (~6.9 GiB, cross-region), worker starts
```

A pod sitting `Pending` for the first couple of minutes is expected. Beyond ~5 minutes,
see below.

### Stopping a run

Actions → **Stop Extract Worker** → pick the environment, set `confirm: yes`. That is
the whole procedure. It sets `enabled: false`, ArgoCD prunes the Job, and the GPU nodes
scale back to zero.

**Do not `kubectl delete` the Job.** ArgoCD's sync policy is
`prune: true, selfHeal: false`, so a hand-deleted Job is not recreated *immediately* —
but the Application then sits `OutOfSync/Missing`, and **the next person to sync it
restarts the extraction**. Stopping through the workflow makes "stopped" the desired
state, so it stays stopped, and leaves a record of who stopped what.

The stop is graceful: SIGTERM plus `terminationGracePeriodSeconds: 900` means each
worker finishes and uploads the document it is on before exiting. Observed on a real
stop: the completed count rose from 82 to 86 *during* shutdown. Nothing already
extracted is lost — uploads happen per document, not at the end.

**To resume**, dispatch *Build & Deploy Extract Worker* with `build=false`,
`mode=extract` and `run_id` **blank**. That workflow sets `enabled: true` itself, so
stop and deploy are exact opposites and neither needs a hand-edited PR. A blank
`run_id` keeps the S3 prefix, so finished documents are skipped.

To abandon a run and start something different, just dispatch a deploy with new inputs —
the old Job is pruned and replaced.

Either way the GPU nodes scale to zero once no pod needs them, so idle cost is nil.

---

## Troubleshooting

### Pod stuck `Pending`, event says "didn't trigger scale-up"

The pod event is often misleading — it blames node affinity regardless of the real
reason. Get the truth from cluster-autoscaler:

```bash
kubectl --context dev1 -n kube-system logs deploy/cluster-autoscaler-aws-cluster-autoscaler \
  --since=10m | grep -E "can't be scheduled|predicate|scale-up"
```

The one that has actually bitten us:

```
predicate "NodeResourcesFit" didn't pass (predicateReasons=[Insufficient ephemeral-storage];
  debugInfo=nodeName: "template-node-for-eks-dev1-gpu-...")
```

CA's synthetic template node for an EKS managed node group carries **no
ephemeral-storage**. The only way to tell it otherwise is the ASG tag
`k8s.io/cluster-autoscaler/node-template/resources/ephemeral-storage`, and **EKS does
not propagate node-group tags to the ASG**, so it never arrives. Any non-zero
`ephemeral-storage` *request* therefore makes scale-from-zero impossible.

**The chart deliberately sets no `ephemeral-storage` request. Do not add one back**
without also adding that ASG tag via an `aws_autoscaling_group_tag` resource. Disk is
still bounded by the emptyDir `sizeLimit` on a 200 GB root volume.

Labels and taints are fine — CA reads those from `eks:DescribeNodegroup`.

### Cannot read logs: "dial tcp ...:10250: i/o timeout"

The node is gone. Failed GPU pods release their node, and the kubelet went with it.

For anything that fails *before* GPU work — imports, config, S3, permissions — reproduce
on the ordinary node group instead, where the node does not vanish and there is no spot
scale-up to wait for. `--plan` needs no GPU:

```yaml
apiVersion: v1
kind: Pod
metadata: { name: core-index-extract-debug, namespace: tenant }
spec:
  restartPolicy: Never
  serviceAccountName: core-index-extract-sa   # same IRSA identity as the Job
  containers:
    - name: worker
      image: 838689936182.dkr.ecr.eu-west-2.amazonaws.com/aosphere-core-index-extract:<tag>
      args: ["--plan"]
      env:
        - { name: CORPUS_RESULTS, value: "s3://aosphere-tenant-dev1-core-index/corpus/<run_id>" }
        - { name: CORPUS_SOURCE,  value: "s3://aosphere-tenant-dev1-core-index/corpus-src" }
        - { name: CORPUS_SHARDS,  value: "1" }
        - { name: JOB_COMPLETION_INDEX, value: "0" }
        - { name: AWS_REGION, value: "eu-west-1" }
        - { name: PYTHONUNBUFFERED, value: "1" }
      resources:
        requests: { cpu: "1", memory: 4Gi }
        limits:   { cpu: "2", memory: 8Gi }
      volumeMounts: [{ name: work, mountPath: /work }]
  volumes: [{ name: work, emptyDir: { sizeLimit: 20Gi } }]
```

Delete it when you are done. It is a diagnostic, not config.

### Worker exits immediately with a "missing" message

A dependency is absent from the image. `Dockerfile.extract` installs
`requirements.txt` plus explicit extras, then `pip install --no-deps .` — and
`--no-deps` skips **extras as well as dependencies**, so anything declared only in a
`pyproject.toml` extra will not be in the image unless it is named explicitly in the
Dockerfile. That is exactly how `pymupdf` went missing: it lives in the `extract` extra,
intentionally kept out of `requirements.txt` because the *serving* image never parses a
document.

If you add an extraction-time dependency, add it to the `RUN pip install` line in
`Dockerfile.extract` as well.

### Pod restarts forever without the Job failing

Should no longer happen. `restartPolicy` is `Never` precisely so
`backoffLimitPerIndex` works: with `OnFailure` the kubelet restarts the *container* in
place, the pod never fails, the per-index counter never increments, and a broken image
pins a GPU node indefinitely. A healthy failure looks like 1 + `backoffLimitPerIndex`
discrete failed pods, then the node scaling away.

### An extra GPU node appears

Expected, and self-correcting. A node joins the cluster before the device plugin
advertises `nvidia.com/gpu`, so for ~40s the pod still cannot be satisfied and CA adds
another node. The surplus is reclaimed once it is unneeded.

It matters at `shards: 8`: the race can push against the node group's `max_size` of 8
and leave a shard waiting. Raise the ceiling before a full-scale run.

---

## Capacity and cost

Per shard: one `g4dn.2xlarge` **spot** node — 8 vCPU, 32 GiB, 1× T4.
Roughly **$0.40/h** in eu-west-1 against $0.752 on-demand.

`g5.2xlarge` spot is currently *more expensive than g4dn on-demand*, so widening the
instance family would cost money rather than save it.

Full corpus, from a real `--plan`: **1088 files → 891 extractions + 197 clones →
61,214 pages**.

| Assumed rate | GPU-hours | `shards=1` | `shards=8` | Spot cost |
|---|---|---|---|---|
| 15.9 s/page (`--secs-per-page` default) | 270 | ~11.3 days | ~34 h | ~$108 |
| 7.9 s/page (`deploy/extraction-job.yaml` comment) | 134 | ~5.6 days | ~17 h | ~$54 |

**These disagree by 2× and the truth is not yet known.** Total GPU-hours are the same at
any shard count — only wall-clock changes. Plan for the higher figure, measure the real
rate in the first hour of a live run, and pass `--secs-per-page` to make later estimates
honest.

`shards: 1` is a smoke-test setting. Use 8 for the real corpus: the node group caps at 8
and the account's "All G and VT Spot" quota (64 vCPU) fits exactly 8 × g4dn.2xlarge.

### Spot interruptions are handled

No Node Termination Handler needed. EKS managed node groups with `capacity_type = SPOT`
enable Capacity Rebalancing and drain automatically. The drain sends SIGTERM; the worker
finishes the document in flight, uploads it and exits 0
(`terminationGracePeriodSeconds: 900`). The most a reclaim costs is one document, and a
replacement pod resumes from S3.

---

## Where things live

| Thing | Where |
|---|---|
| Worker image | `Dockerfile.extract` → ECR `aosphere-core-index-extract` (eu-west-2) |
| Worker code | `scripts/corpus_worker.py` |
| Workflow | `.github/workflows/build-extract-image.yml` |
| Helm chart | `k8s-deployment` → `helm-charts/core-index-extract/` |
| Env values | `k8s-deployment` → `helm-charts/core-index-extract/values-dev1.yaml` |
| ArgoCD app | `tenant-dev1-core-index-extract` (corp-internal cluster) |
| GPU nodes | dev1 node group `dev1-gpu` — g4dn.2xlarge spot, 0→8, taint `nvidia.com/gpu=true:NoSchedule`, label `workload=gpu-extract` |
| Device plugin | `k8s-deployment` → `helm-charts/nvidia-device-plugin/` (kube-system, dev1 only) |
| IAM | `aosphere-tenant-dev1-irsa-core-index-extract-role` — S3 read on `corpus-src/*`, write on `corpus/*`. **Needs `bedrock:InvokeModel`** for Stage 4 AI and the MRAM summary AI route (both call Bedrock from inside this worker); not yet granted as of the summary AI route landing — see the note in `build-extract-image.yml`. No OpenSearch |
| Corpus in | `s3://aosphere-tenant-dev1-core-index/corpus-src/` |
| Results out | `s3://aosphere-tenant-dev1-core-index/corpus/<run_id>/` |
| Promotion worker | `scripts/promote_run.py`, `deploy/promotion-job.yaml` — see **[`PROMOTION_PIPELINE.md`](PROMOTION_PIPELINE.md)** |
| Promotion IAM | `corpus-promote` — a NEW role. The extract role has write on `corpus/*` only; a promotion writes `index/*` |
| Gallery out | `s3://aosphere-tenant-dev1-core-index/index/<version>/doc-gallery/` |

`deploy/extraction-job.yaml` and `deploy/karpenter-gpu-nodepool.yaml` in this repo are
the **original reference manifests**, kept for their reasoning. They are not what runs:
they assume Karpenter, which this platform does not use. The Helm chart in
`k8s-deployment` is the deployed truth.

---

## After the run: promoting it

A finished run is not published anywhere. The gallery reads
`index/<version>/doc-gallery/` and the run is in `corpus/<run_id>/`, so reviewing a run and
publishing it are two different things — reviewing is `?run=` on the Doc Gallery, publishing
is a promotion.

```bash
python scripts/promote_run.py --run <run_id> --plan          # decide; writes nothing
python scripts/promote_run.py --run <run_id> --probe-write   # then do it
```

Promotes `pass` and `review` documents in the three indexable products, into a NEW index
version, and leaves `index/latest` alone — so nothing becomes live and nothing becomes
searchable. Full detail, including the IAM prerequisite and what Phase 2 still needs:
**[`PROMOTION_PIPELINE.md`](PROMOTION_PIPELINE.md)**.

One thing worth knowing here: a document with no `viewer.html` in its job directory cannot be
promoted (a manifest row pointing at a missing viewer is a guaranteed 404). Documents extracted
before `build_review_artefacts` existed are in that state — 123 of them in run 2026-08-21-02 —
and `scripts/backfill_review_artefacts.py --run <run_id>` fixes it. The promotion plan names
them.

## Known gaps

- **The write path is only proven by a real run.** `--plan` never calls `PutObject`, so
  a green dry run does not confirm the S3 write permissions (including the multipart
  actions). The first document upload is the real test.
- **The extract ECR repo is never pruned.** It is deliberately absent from
  `ALLOWED_SERVICES` in `build-ops`, so the ECR cleanup job skips it, and the repo has no
  lifecycle policy. Every tag plus the `cache-amd64` blob accumulates at ~7 GiB each.
- **No S3 gateway endpoint on the dev1 VPC.** All S3 traffic from the pods crosses the
  NAT gateway at $0.045/GB — roughly $3.50 for a full ~76 GB run, plus the image pull.
- **The image is pulled cross-region.** Service repos are not in the ECR replication
  filter, so every fresh spot node pulls ~6.9 GiB from eu-west-2 over NAT. Adding this
  repo to the replication filter would make pulls in-region and noticeably faster.
