# Production image for the aosphere-core-index cross-region search service.
#
# Mirrors the aosphere-ai-playground deployment shape: a hardened python-slim
# image, run by the shared Skandiam/gh-action-library workflow, `uvicorn <app>`.
#
# DATA IS NOT BAKED. The index artifacts are delivered via a VOLUME MOUNT at
# /data (ACI_DATA_DIR), identical locally and in dev/stage/prod:
#   /data/regions/<Jurisdiction>/artifacts/<J>.sections.npz
#   /data/regions/<Jurisdiction>/artifacts/<J>.content.json
#   /data/regions/_multi/multi.npz
# In k8s the mount is backed by the env's S3 bucket (e.g. the S3 CSI / Mountpoint
# driver, or an init-sync), so the CONTAINER NEEDS NO S3 CREDENTIALS. Locally,
# bind-mount ./data. Only the embedding model is baked (it is static app code).
#
# Trivy hardening (same as ai-playground): patch base-image OS CVEs + bump the
# Python build-tooling Trivy scans in site-packages.
ARG PIP_VERSION=26.1
ARG SETUPTOOLS_VERSION=83.0.0   # >=78.1.1 (CVE-2025-47273) and >=83.0.0 (CVE-2026-59890)
ARG WHEEL_VERSION=0.46.2
ARG JARACO_CONTEXT_VERSION=6.1.0

FROM python:3.12-slim

ARG PIP_VERSION
ARG SETUPTOOLS_VERSION
ARG WHEEL_VERSION
ARG JARACO_CONTEXT_VERSION

# Base OS upgrade patches most CVEs. `dist-upgrade` (not plain `upgrade`) is required:
# plain `apt-get upgrade` silently keeps a package back whenever its security fix needs
# to add/remove a dependency, which is exactly how a Debian point-release (e.g. deb13u4)
# lands — a batch of coordinated packages (libc6, libc-bin, gzip, perl-base, ...) that
# `upgrade` skips wholesale while Trivy still reports them as vulnerable. liblzma5 is
# also pinned explicitly so the patched security build (CVE-2026-34743 -> 5.8.1-1+deb13u1)
# lands regardless of rebuild timing and reconciles the dangling dpkg entry Trivy reports
# as on-disk=None.
RUN apt-get update && apt-get dist-upgrade -y \
    && apt-get install --no-install-recommends -y libgomp1 liblzma5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --upgrade \
        "pip>=${PIP_VERSION}" \
        "setuptools>=${SETUPTOOLS_VERSION}" \
        "wheel>=${WHEEL_VERSION}" \
        "jaraco.context>=${JARACO_CONTEXT_VERSION}"

# App code only (data/ is excluded via .dockerignore — it comes from the mount).
#
# Enumerated rather than `COPY . .` for ONE reason: scripts/ has to land in a LATE
# layer (see the COPY near the bottom), and a `COPY . .` here would pull it into this
# one. Everything the service imports or installs from is listed; deploy manifests,
# compose files and uv.lock are build/ops inputs that nothing in the container reads.
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY assets/ ./assets/
COPY config/ ./config/
RUN pip install --no-cache-dir --no-deps .

# Bake the embedding + cross-encoder models so there is no cold-start download (static
# app code). The cross-encoder is still needed for /api/relevance highlighting even when
# search reranking runs on the LLM backend, so bake it via crossencoder_scores() directly
# (calling rerank() here would dispatch to the LLM backend and hit Bedrock at build time).
ENV ACI_FASTEMBED_CACHE=/app/model-cache
RUN python -c "from aosphere_core_index.embeddings.embedder import FastEmbedEmbedder; FastEmbedEmbedder().embed(['warm up the model download'])"
RUN python -c "from aosphere_core_index.embeddings.reranker import crossencoder_scores; crossencoder_scores('warm up', ['warm up the cross-encoder download'])"

# pip is a BUILD tool, not a runtime one — everything above is installed by now and the
# service runs `uvicorn`. Removing it also removes the last Trivy findings in this image,
# which are pip's own VENDORED copies rather than anything we depend on:
# pip/_vendor/vendor.txt pins msgpack==1.1.2 (GHSA-6v7p-g79w-8964, HIGH) and
# setuptools==70.3.0 (CVE-2025-47273 HIGH, CVE-2026-59890 MEDIUM), while the real
# site-packages copies are msgpack 1.2.1 and setuptools 84.0.0 and scan clean. No
# requirements floor can reach inside pip, and pip 26.2.1 is the newest release there is —
# its vendor.txt still pins those two — so there is no upgrade that clears them. Deleting
# pip removes the vulnerable code instead of hiding it, and drops runtime package
# installation as an attack surface. Nothing later in this file uses pip.
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip \
              /usr/local/lib/python3.12/site-packages/pip-*.dist-info

# scripts/ LAST, and on purpose. Two things run from THIS image but are not imported
# by the service, so neither of them survives the image not carrying scripts/:
#
#   * deploy/promotion-job.yaml runs `python scripts/promote_run.py` — deliberately on
#     the API image, not Dockerfile.extract, because the gallery half needs no MinerU
#     and no ~7 GiB of model weights (that file says so itself).
#   * POST /api/admin/entity-reindex spawns `scripts/sync_atlas_search.py` as a
#     subprocess (service/entity_reindex.py), for the private RDS + OpenSearch the pod
#     can already reach.
#
# Putting the copy HERE rather than above keeps the cache win that moving scripts/ out
# of the image was after: a scripts/-only commit — the frequent MRAM/scorecard ones —
# re-runs only this ~1 MB COPY, not the pip install or either model warmup.
COPY scripts/ ./scripts/

ENV PYTHONUNBUFFERED=1
ENV ACI_OFFLINE=1
ENV ACI_DATA_DIR=/data
# Search reranking runs on the in-account, in-EU LLM listwise reranker (Bedrock Claude
# Haiku 4.5): best-measured ranking (+6 hit@1 / +8 @3 full-pipeline vs the cross-encoder),
# no local model on the hot search path, graceful cosine fallback on Bedrock errors. The
# pod's IAM role needs bedrock:InvokeModel on the eu Haiku inference profile + base model.
ENV ACI_RERANK_BACKEND=claude-bedrock
# Bound the cross-encoder reranker's memory. On Linux, PyTorch spawns one intra-op
# thread per host core and glibc hands each thread a 64 MB malloc arena, so idle RSS
# ballooned to ~4.6 GB and grew toward the limit under concurrent load -> OOM (137).
# Capping arenas + threads holds it at ~0.9 GB idle / ~2.1 GB steady-state.
ENV MALLOC_ARENA_MAX=2
ENV OMP_NUM_THREADS=4
ENV MKL_NUM_THREADS=4
ENV TOKENIZERS_PARALLELISM=false
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "aosphere_core_index.service.app:app", "--host", "0.0.0.0", "--port", "8000"]
