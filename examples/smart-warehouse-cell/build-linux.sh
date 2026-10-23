#!/usr/bin/env bash
set -euo pipefail

example_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
work_path="$example_root/.build-linux"
output_path="$example_root/SmartWarehouseCell-linux64.fmu"
binary_path="$work_path/binaries/linux64"
resource_path="$work_path/resources"
command -v gcc >/dev/null 2>&1 || { echo 'GCC is required to build the Linux x64 FMI binary.' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo 'Python 3 is required to package the Linux x64 FMU.' >&2; exit 1; }

scene_path="$example_root/smart-warehouse-cell.glb"
test -f "$scene_path" || { echo 'The Three.js-exported GLB scene is missing.' >&2; exit 1; }
rm -rf -- "$work_path"
mkdir -p "$binary_path" "$resource_path"
cp "$example_root/modelDescription.xml" "$work_path/modelDescription.xml"
cp "$example_root/smart-warehouse-cell.glb" "$resource_path/smart-warehouse-cell.glb"
gcc -std=c11 -fPIC -shared -O2 -Wall -Wextra -o "$binary_path/SmartWarehouseCell.so" "$example_root/warehouse.c" -lm
python3 - "$example_root" "$work_path" "$output_path" <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from archive_utils import write_reproducible_zip
source, target = Path(sys.argv[2]), Path(sys.argv[3])
write_reproducible_zip(source, target)
PY
echo "Built $output_path"
