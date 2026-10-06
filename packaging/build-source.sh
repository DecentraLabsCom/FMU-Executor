#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=${VERSION:-$(cat "$ROOT/VERSION")}
OUTPUT_DIR=${OUTPUT_DIR:-$ROOT/dist}
mkdir -p "$OUTPUT_DIR"
ARCHIVE="$OUTPUT_DIR/decentralabs-fmu-executor-$VERSION.tar.gz"
tar -czf "$ARCHIVE" -C "$ROOT" \
    --exclude=.git --exclude=.pytest_cache --exclude=__pycache__ \
    VERSION pyproject.toml requirements.txt README.md app tests
(cd "$OUTPUT_DIR" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").SHA256SUMS")
if [ -n "${MINISIGN_SECRET_KEY:-}" ]; then
    command -v minisign >/dev/null 2>&1 || { echo 'minisign is required to sign this source release' >&2; exit 1; }
    minisign -S -s "$MINISIGN_SECRET_KEY" -m "$OUTPUT_DIR/$(basename "$ARCHIVE").SHA256SUMS"
elif [ "${REQUIRE_SIGNATURE:-0}" = 1 ]; then
    echo 'MINISIGN_SECRET_KEY is required for a signed FMU Executor release' >&2
    exit 1
fi
