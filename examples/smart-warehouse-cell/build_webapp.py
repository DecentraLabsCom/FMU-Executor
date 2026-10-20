#!/usr/bin/env python3
"""Package the self-contained static Three.js app companion for the FMU."""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
from pathlib import Path, PurePosixPath

from archive_utils import write_reproducible_zip


ROOT = Path(__file__).resolve().parent
WEBAPP_ROOT = ROOT / "webapp"
OUTPUT = ROOT / "SmartWarehouseCell.webapp.zip"


def build(three_root: Path) -> None:
    three_root = three_root.resolve(strict=True)
    vendor = WEBAPP_ROOT / "vendor"
    if vendor.exists():
        shutil.rmtree(vendor)
    (vendor / "utils").mkdir(parents=True)

    copy_plan = {
        three_root / "build" / "three.module.js": vendor / "three.module.js",
        three_root / "build" / "three.core.js": vendor / "three.core.js",
        three_root / "examples" / "jsm" / "controls" / "OrbitControls.js": vendor / "OrbitControls.js",
        three_root / "examples" / "jsm" / "loaders" / "GLTFLoader.js": vendor / "GLTFLoader.js",
        three_root / "examples" / "jsm" / "environments" / "RoomEnvironment.js": vendor / "RoomEnvironment.js",
        three_root / "examples" / "jsm" / "utils" / "BufferGeometryUtils.js": vendor / "utils" / "BufferGeometryUtils.js",
        three_root / "examples" / "jsm" / "utils" / "SkeletonUtils.js": vendor / "utils" / "SkeletonUtils.js",
        three_root / "LICENSE": vendor / "THREE-LICENSE.txt",
    }
    for source, destination in copy_plan.items():
        if not source.is_file():
            raise FileNotFoundError(f"Three.js source file is missing: {source}")
        shutil.copyfile(source, destination)

    # These examples import the package specifier `three`. Rewrite only that
    # import to the vendored module so the ZIP runs without an import map/CDN.
    for filename in ("OrbitControls.js", "GLTFLoader.js", "RoomEnvironment.js"):
        path = vendor / filename
        source = path.read_text(encoding="utf-8")
        source = source.replace("from 'three'", "from './three.module.js'")
        source = source.replace('from "three"', 'from "./three.module.js"')
        if filename == "GLTFLoader.js":
            source = source.replace("from '../utils/", "from './utils/")
            source = source.replace('from "../utils/', 'from "./utils/')
        path.write_text(source, encoding="utf-8")
    for filename in ("BufferGeometryUtils.js", "SkeletonUtils.js"):
        path = vendor / "utils" / filename
        source = path.read_text(encoding="utf-8")
        source = source.replace("from 'three'", "from '../three.module.js'")
        source = source.replace('from "three"', 'from "../three.module.js"')
        path.write_text(source, encoding="utf-8")

    glb = ROOT / "smart-warehouse-cell.glb"
    if not glb.is_file():
        raise FileNotFoundError("Build the warehouse GLB before the web app ZIP")
    shutil.copyfile(glb, WEBAPP_ROOT / "warehouse.glb")

    bundle_files = {
        path.relative_to(WEBAPP_ROOT).as_posix()
        for path in WEBAPP_ROOT.rglob("*")
        if path.is_file()
    }
    import_pattern = re.compile(r"^\s*(?:import|export)\b.*?\bfrom\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
    for relative_path in sorted(bundle_files):
        if not relative_path.endswith(".js"):
            continue
        source_path = WEBAPP_ROOT / PurePosixPath(relative_path)
        source = source_path.read_text(encoding="utf-8")
        for match in import_pattern.finditer(source):
            specifier = match.group(1)
            if not specifier.startswith(("./", "../")):
                raise ValueError(f"Three.js app contains an unbundled module import: {relative_path}: {specifier}")
            target = posixpath.normpath(posixpath.join(posixpath.dirname(relative_path), specifier))
            if target.startswith("../") or target not in bundle_files:
                raise FileNotFoundError(f"Three.js app module import is missing from the bundle: {relative_path}: {specifier}")

    index = (WEBAPP_ROOT / "index.html").read_text(encoding="utf-8")
    for match in re.finditer(r"(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", index, re.IGNORECASE):
        specifier = match.group(1)
        if specifier.startswith(("data:", "#")):
            continue
        if not specifier.startswith(("./", "../")):
            raise ValueError(f"Three.js app index must use bundled relative assets: {specifier}")
        target = posixpath.normpath(specifier)
        if target.startswith("../") or target not in bundle_files:
            raise FileNotFoundError(f"Three.js app index asset is missing from the bundle: {specifier}")

    write_reproducible_zip(WEBAPP_ROOT, OUTPUT)
    print(f"Built {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--three-root", type=Path, required=True, help="Installed Three.js package directory")
    args = parser.parse_args()
    build(args.three_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
