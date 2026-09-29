#!/usr/bin/env bash
# Phase 2 of the two-account build: build jurisdictions from the locally-mirrored
# source cache (scripts/fetch_sources.py), embedding via Bedrock in a separate
# account. One process per jurisdiction so memory is reclaimed; resumable (skips
# already-built). Reads jurisdiction names from arg file (default: all remaining).
#
#   set -a; . ./.env; set +a            # default creds = source account (alerts S3)
#   ACI_BEDROCK_PROFILE=dev1 ./scripts/build_offline.sh /tmp/aci_remaining.txt
set -uo pipefail
cd "$(dirname "$0")/.."

LIST="${1:-/tmp/aci_remaining.txt}"
export ACI_SOURCE_OFFLINE="${ACI_SOURCE_OFFLINE:-1}"
export ACI_EMBED_BACKEND="${ACI_EMBED_BACKEND:-titan}"
export ACI_BEDROCK_REGION="${ACI_BEDROCK_REGION:-eu-west-1}"

total=$(grep -c . "$LIST"); i=0; ok=0; fail=0
echo "building $total jurisdictions (offline source, backend=$ACI_EMBED_BACKEND, profile=${ACI_BEDROCK_PROFILE:-default})"
while IFS= read -r j; do
  [ -z "$j" ] && continue
  i=$((i + 1))
  if [ -f "data/regions/$j/artifacts/$j.sections.npz" ] && [ -f "data/regions/$j/artifacts/$j.content.json" ]; then
    echo "[$i/$total] skip (built): $j"; ok=$((ok + 1)); continue
  fi
  echo "[$i/$total] building: $j"
  if uv run aci bundle "$j" >/dev/null 2>>/tmp/aci_build_errors.log; then
    ok=$((ok + 1))
  else
    fail=$((fail + 1)); echo "  !! FAILED: $j (see /tmp/aci_build_errors.log)"
  fi
done < "$LIST"
echo "BUILD COMPLETE: ok=$ok fail=$fail / $total"
