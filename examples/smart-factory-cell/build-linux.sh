#!/usr/bin/env bash
set -euo pipefail

example_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
work_path="$example_root/.build-linux"
output_path="$example_root/SmartFactoryCell-linux64.fmu"
binary_path="$work_path/binaries/linux64"
resource_path="$work_path/resources"

command -v gcc >/dev/null 2>&1 || { echo 'GCC is required to build the Linux x64 FMI binary.' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'Python 3 is required to package the Linux x64 FMU.' >&2; exit 1; }

python3 "$example_root/generate_scene.py"
rm -rf -- "$work_path"
mkdir -p "$binary_path" "$resource_path"
cp "$example_root/modelDescription.xml" "$work_path/modelDescription.xml"
cp "$example_root/smart-factory-cell.glb" "$resource_path/smart-factory-cell.glb"

gcc -std=c11 -fPIC -shared -O2 -Wall -Wextra \
  -o "$binary_path/SmartFactoryCell.so" \
  "$example_root/smart_factory_cell.c" -lm

python3 - "$work_path" "$output_path" <<'PY'
from pathlib import Path
import sys
import zipfile

source = Path(sys.argv[1])
target = Path(sys.argv[2])
with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(source).as_posix())
PY

echo "Built $output_path"
