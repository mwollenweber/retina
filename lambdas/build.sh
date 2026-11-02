#!/usr/bin/env bash
# Build one deployment zip per handler into lambdas/build/<name>.zip.
#
# Each zip contains:
#   common/            shared db / sync_log / schema helpers
#   <name>/handler.py  the Lambda entrypoint (handler string: "<name>.handler.handler")
#   site-packages      psycopg[binary] + requests resolved for aarch64 / cp313
#
# Platform tags: the Lambda python3.13 arm64 runtime is Amazon Linux 2023
# (glibc 2.34), so both manylinux_2_28_aarch64 and manylinux2014_aarch64 wheels
# work. psycopg-binary >= 3.3 only publishes manylinux_2_28 for aarch64, so we
# pass both tags and let pip pick the best match.
#
# boto3/botocore are provided by the Lambda runtime and are excluded from the zip.
# Requires pip >= 23 (for --python-version / --platform cross-resolution).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build"
PY="${PYTHON:-python3}"
PY_VERSION="${LAMBDA_PY_VERSION:-3.13}"
PLATFORMS="${LAMBDA_PLATFORMS:-manylinux_2_28_aarch64 manylinux2014_aarch64}"
HANDLERS="${HANDLERS:-sync_traffic sync_osm publish_geojson push_osm}"

rm -rf "$BUILD"
mkdir -p "$BUILD"

# Runtime requirements minus boto3 (provided by the Lambda runtime).
REQ_LAMBDA="$BUILD/requirements-lambda.txt"
grep -viE '^(boto3|botocore)\b' "$HERE/requirements.txt" > "$REQ_LAMBDA"

# Resolve dependencies once for the target platform, then copy into each zip.
DEPS="$BUILD/deps"
mkdir -p "$DEPS"
PLATFORM_ARGS=()
for p in $PLATFORMS; do PLATFORM_ARGS+=(--platform "$p"); done

"$PY" -m pip install \
  --quiet \
  "${PLATFORM_ARGS[@]}" \
  --only-binary=:all: \
  --python-version "$PY_VERSION" \
  --implementation cp \
  --target "$DEPS" \
  -r "$REQ_LAMBDA"

# Trim what we never need at runtime.
find "$DEPS" -type d \( -name '__pycache__' -o -name 'tests' -o -name '*.dist-info' \) -prune -exec rm -rf {} + 2>/dev/null || true
find "$DEPS" -type f \( -name '*.c' -o -name '*.pyi' -o -name '*.pyx' -o -name '*.pxd' \) -delete 2>/dev/null || true

for name in $HANDLERS; do
  if [ ! -f "$HERE/$name/handler.py" ]; then
    echo "skip $name: no handler.py" >&2
    continue
  fi
  stage="$BUILD/stage-$name"
  rm -rf "$stage"
  mkdir -p "$stage"
  cp -R "$DEPS/." "$stage/"
  rsync -a --exclude '__pycache__' "$HERE/common" "$stage/"
  rsync -a --exclude '__pycache__' "$HERE/$name" "$stage/"
  (cd "$stage" && zip -qr -X "$BUILD/$name.zip" .)
  rm -rf "$stage"
  echo "built $BUILD/$name.zip ($(du -h "$BUILD/$name.zip" | cut -f1))"
done

rm -rf "$DEPS" "$REQ_LAMBDA"
echo "done. CDK handler strings: <name>.handler.handler (e.g. sync_traffic.handler.handler)"
