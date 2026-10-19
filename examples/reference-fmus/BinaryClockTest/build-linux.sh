#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
build_dir="${BUILD_DIR:-${project_dir}/.build-linux}"
binary_path="${build_dir}/BinaryClockTest.so"

mkdir -p "$build_dir"
gcc \
  -std=c11 \
  -O2 \
  -Wall \
  -Wextra \
  -Werror \
  -fPIC \
  -fvisibility=hidden \
  -shared \
  -o "$binary_path" \
  "${project_dir}/src/BinaryClockTest.c"

python3 "${project_dir}/package_fmu.py" --platform linux --binary "$binary_path"
