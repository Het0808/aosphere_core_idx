#!/usr/bin/env bash
# Build the index for every jurisdiction, ONE AT A TIME (one process per
# jurisdiction) so the embedding runtime's memory is fully reclaimed between
# each. Resumable: already-built jurisdictions are skipped. Finishes by
# rebuilding the flat multi-index + manifest.
#
#   ./scripts/build_all.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Load read-only source credentials for the build (S3 GetObject/ListBucket).
set -a; [ -f .env ] && . ./.env; set +a

echo "Listing jurisdictions…"
uv run aci list-jurisdictions 2>/dev/null > /tmp/aci_jurisdictions.txt
total=$(grep -c . /tmp/aci_jurisdictions.txt)
echo "$total jurisdictions"

i=0
while IFS= read -r region; do
  [ -z "$region" ] && continue
  i=$((i + 1))
  echo "[$i/$total] $region"
  # One process per jurisdiction → memory released on exit. bundle skips if done.
  uv run aci bundle "$region" || echo "  !! failed: $region (continuing)"
done < /tmp/aci_jurisdictions.txt

echo "Rebuilding flat multi-index + manifest…"
uv run aci reindex
echo "Done."
