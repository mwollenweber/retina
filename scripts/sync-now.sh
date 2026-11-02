#!/usr/bin/env bash
# Refresh every data source right now, then republish the map.
#
# The same jobs run on schedules (see scripts/README.md); this is for when you
# don't want to wait. Each source can also be run alone:
#
#   scripts/sync-now.sh            all sources, then publish
#   scripts/sync-now.sh rtcc       City RTCC public safety cameras (ArcGIS)
#   scripts/sync-now.sh osm        OpenStreetMap / Flock plate readers (Overpass)
#   scripts/sync-now.sh traffic    data.nola.gov traffic cameras
#   scripts/sync-now.sh sheet      Project NOLA Google Sheet (geocoded ECS task)
#   scripts/sync-now.sh publish    rebuild data/cameras.geojson and invalidate CloudFront
set -euo pipefail
cd "$(dirname "$0")/.."

JOBS_STACK="${JOBS_STACK:-RetinaJobs}"
out() { aws cloudformation describe-stacks --stack-name "$JOBS_STACK" \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

invoke() {  # invoke <OutputKey> <label>
  local fn; fn=$(out "$1")
  local tmp; tmp=$(mktemp)
  printf '%-8s ' "$2"
  if aws lambda invoke --function-name "$fn" --cli-read-timeout 600 --payload '{}' "$tmp" \
       --query 'FunctionError' --output text | grep -q None; then
    cat "$tmp"; echo
  else
    echo "FAILED"; cat "$tmp"; echo; rm -f "$tmp"; return 1
  fi
  rm -f "$tmp"
}

what="${1:-all}"
case "$what" in
  all)     invoke SyncTrafficFunctionName traffic
           invoke SyncRtccFunctionName rtcc
           invoke SyncOsmFunctionName osm
           scripts/import-sheet.sh          # ends with --publish
           ;;
  rtcc)    invoke SyncRtccFunctionName rtcc;    invoke PublishGeojsonFunctionName publish ;;
  osm)     invoke SyncOsmFunctionName osm;      invoke PublishGeojsonFunctionName publish ;;
  traffic) invoke SyncTrafficFunctionName traffic; invoke PublishGeojsonFunctionName publish ;;
  sheet)   scripts/import-sheet.sh ;;
  publish) invoke PublishGeojsonFunctionName publish ;;
  *) echo "usage: $0 [all|rtcc|osm|traffic|sheet|publish]" >&2; exit 2 ;;
esac
