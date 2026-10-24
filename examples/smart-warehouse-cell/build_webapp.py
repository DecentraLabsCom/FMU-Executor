#!/usr/bin/env python3
"""Package the self-contained static Three.js app companion for the FMU."""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from archive_utils import write_reproducible_zip


ROOT = Path(__file__).resolve().parent
WEBAPP_ROOT = ROOT / "webapp"
OUTPUT = ROOT / "SmartWarehouseCell.webapp.zip"
REQUIRED_HOST_PROTOCOL_MESSAGES = (
    "decentralabs:digital-twin-host-ready",
    "decentralabs:digital-twin-ready",
    "decentralabs:digital-twin-error",
    "decentralabs:fmi-sample",
    "decentralabs:fmi-reset",
    "decentralabs:fmi-simulation-state",
    "decentralabs:fmi-samples",
    "decentralabs:fmi-run-request",
    "decentralabs:fmi-playback-control",
    "playback-rate",
    "playbackControlPosition",
    "bottom-right",
)


def build(three_root: Path) -> None:
    three_root = three_root.resolve(strict=True)
    index = (WEBAPP_ROOT / "index.html").read_text(encoding="utf-8")
    required_interface_markers = (
        'role="group" aria-label="Run controls"',
        'id="duration-value">250</output>',
        'id="simulation-duration" name="stopTime" type="range" min="5" max="300" step="5" value="250"',
        'id="run-simulation" type="button" disabled',
    )
    missing_interface = [marker for marker in required_interface_markers if marker not in index]
    if missing_interface:
        raise ValueError("Smart Warehouse app is missing its run-controls contract: " + ", ".join(missing_interface))
    if re.search(r"<form\b", index, re.IGNORECASE):
        raise ValueError("The sandboxed Smart Warehouse app must use explicit controls instead of form submission")

    default_experiment = ET.parse(ROOT / "modelDescription.xml").getroot().find("{*}DefaultExperiment")
    try:
        default_stop_time = float(default_experiment.attrib["stopTime"]) if default_experiment is not None else None
    except (KeyError, ValueError):
        default_stop_time = None
    if default_stop_time != 250:
        raise ValueError("The default FMU stop time must allow the six-package order to finish at 250 seconds")

    app_script_path = WEBAPP_ROOT / "app.js"
    if not app_script_path.is_file():
        raise FileNotFoundError("Three.js app is missing its root app.js entry point")
    app_script = app_script_path.read_text(encoding="utf-8")
    if "simulationRunButton?.addEventListener('click', requestSimulation)" not in app_script:
        raise ValueError("The sandboxed Smart Warehouse app must start simulations from a button click")
    if "simulationForm?.addEventListener('submit', requestSimulation)" in app_script:
        raise ValueError("The sandboxed Smart Warehouse app must not depend on form submission")

    vendor = WEBAPP_ROOT / "vendor"
    if vendor.exists():
        shutil.rmtree(vendor)
    vendor.mkdir(parents=True)

    copy_plan = {
        three_root / "build" / "three.module.js": vendor / "three.module.js",
        three_root / "build" / "three.core.js": vendor / "three.core.js",
        three_root / "examples" / "jsm" / "controls" / "OrbitControls.js": vendor / "OrbitControls.js",
        three_root / "examples" / "jsm" / "environments" / "RoomEnvironment.js": vendor / "RoomEnvironment.js",
        three_root / "LICENSE": vendor / "THREE-LICENSE.txt",
    }
    for source, destination in copy_plan.items():
        if not source.is_file():
            raise FileNotFoundError(f"Three.js source file is missing: {source}")
        shutil.copyfile(source, destination)

    # These examples import the package specifier `three`. Rewrite only that
    # import to the vendored module so the ZIP runs without an import map/CDN.
    for filename in ("OrbitControls.js", "RoomEnvironment.js"):
        path = vendor / filename
        source = path.read_text(encoding="utf-8")
        source = source.replace("from 'three'", "from './three.module.js'")
        source = source.replace('from "three"', 'from "./three.module.js"')
        path.write_text(source, encoding="utf-8")
    companion_glb = WEBAPP_ROOT / "warehouse.glb"
    if companion_glb.exists():
        raise ValueError("The procedural Three.js app must not bundle the GLB companion scene")

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

    for match in re.finditer(r"(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", index, re.IGNORECASE):
        specifier = match.group(1)
        if specifier.startswith(("data:", "#")):
            continue
        if not specifier.startswith(("./", "../")):
            raise ValueError(f"Three.js app index must use bundled relative assets: {specifier}")
        target = posixpath.normpath(specifier)
        if target.startswith("../") or target not in bundle_files:
            raise FileNotFoundError(f"Three.js app index asset is missing from the bundle: {specifier}")

    if "GLTFLoader" in app_script or "warehouse.glb" in app_script:
        raise ValueError("The Three.js demo app must build its own scene without loading the companion GLB")
    if "createSmartWarehouseScene" not in app_script or "scene.js" not in bundle_files:
        raise ValueError("The Three.js demo app is missing its procedural warehouse scene module")
    missing_protocol_messages = [
        message for message in REQUIRED_HOST_PROTOCOL_MESSAGES if message not in app_script
    ]
    if missing_protocol_messages:
        raise ValueError(
            "Three.js app is missing Marketplace host protocol messages: "
            + ", ".join(missing_protocol_messages)
        )

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
