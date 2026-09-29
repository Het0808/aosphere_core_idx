#!/usr/bin/env bash
# Run a promotion from this laptop, with the environment the index chain needs.
#
#   scripts/promote_local.sh <run_id> --plan              # decide; writes nothing
#   scripts/promote_local.sh <run_id> --limit 5           # gallery only, 5 documents
#   scripts/promote_local.sh <run_id> --with-index        # all the way, and cut over
#
# Everything after <run_id> is passed straight to scripts/promote_run.py.
#
# The preflight below exists because every one of these failures is cheap to detect now and
# expensive to hit later: an expired SSO session forty minutes into an embed, a vector store
# that was never reachable, a Bedrock region with no Titan.
set -euo pipefail

RUN="${1:-}"
if [[ -z "$RUN" ]]; then
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi
shift

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PROFILE="${AWS_PROFILE:-dev1}"
BUCKET="${ACI_EXTRACTION_BUCKET:-aosphere-tenant-dev1-core-index}"
export ACI_EMBED_BACKEND="${ACI_EMBED_BACKEND:-titan}"
export ACI_BEDROCK_REGION="${ACI_BEDROCK_REGION:-eu-west-1}"
export ACI_VECTOR_BACKEND="${ACI_VECTOR_BACKEND:-opensearch}"
export ACI_VECTOR_INDEX="${ACI_VECTOR_INDEX:-aci-vectors}"
export ACI_OPENSEARCH_URL="${ACI_OPENSEARCH_URL:-http://localhost:9200}"
export AWS_REGION="${AWS_REGION:-eu-west-1}"
export PYTHONPATH="${PYTHONPATH:-$REPO/src}"

PY="${PY:-$REPO/.venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3)"

say() { printf '  %-34s %s\n' "$1" "$2"; }

echo "preflight"
# 1. Credentials. Exported as STATIC values so every subprocess (aci, load_vectors) and the
#    docker container see the same identity, rather than each re-resolving the SSO profile.
if [[ -z "${AWS_ACCESS_KEY_ID:-}" ]]; then
  if ! eval "$(aws configure export-credentials --profile "$PROFILE" --format env 2>/dev/null)"; then
    echo "  ✗ no AWS credentials for profile '$PROFILE'."
    echo "    aws sso login --sso-session tools1"
    echo '    eval "$(aws configure export-credentials --profile '"$PROFILE"' --format env)"'
    exit 3
  fi
fi
WHO="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo '')"
[[ -n "$WHO" ]] || { echo "  ✗ credentials present but not valid (expired SSO session?)"; exit 3; }
say "identity" "$WHO"
say "bucket" "s3://$BUCKET"
say "index/latest" "$(aws s3 cp "s3://$BUCKET/index/latest" - 2>/dev/null || echo '(unreadable)')"

WITH_INDEX=0
for a in "$@"; do [[ "$a" == "--with-index" ]] && WITH_INDEX=1; done

if [[ "$WITH_INDEX" == 1 ]]; then
  # 2. Bedrock, in the region the embedder will actually call.
  if aws bedrock list-foundation-models --region "$ACI_BEDROCK_REGION" \
        --query "modelSummaries[?contains(modelId,'titan-embed-text-v2')].modelId" \
        --output text >/dev/null 2>&1; then
    say "bedrock ($ACI_BEDROCK_REGION)" "reachable"
  else
    echo "  ⚠ could not list Bedrock models in $ACI_BEDROCK_REGION — the embed stage may"
    echo "    fail after the seed and content stages have already run."
  fi
  # 3. The vector store. Checked NOW because the load is the last stage: discovering an
  #    unreachable store after the pointer has been flipped leaves search empty.
  if curl -fsS --max-time 5 "$ACI_OPENSEARCH_URL" >/dev/null 2>&1; then
    say "vector store" "$ACI_OPENSEARCH_URL reachable"
  else
    echo "  ✗ $ACI_OPENSEARCH_URL is not reachable, and --with-index ends by reloading it."
    echo "    Start it:  docker compose up -d opensearch"
    echo "    Or skip that stage with --no-reload-vectors."
    exit 4
  fi
fi
echo

exec "$PY" scripts/promote_run.py --run "$RUN" --bucket "$BUCKET" \
  --region "$AWS_REGION" --profile "$PROFILE" "$@"
