import importlib.util
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = PROJECT_ROOT / "examples" / "smart-warehouse-cell"


def _load_builder():
    sys.path.insert(0, str(EXAMPLE_ROOT))
    try:
        spec = importlib.util.spec_from_file_location("smart_warehouse_build_webapp", EXAMPLE_ROOT / "build_webapp.py")
        if spec is None or spec.loader is None:
            raise ImportError("Could not load the Smart Warehouse webapp builder.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def test_sdk_sync_copies_each_protocol_module_from_the_canonical_source(tmp_path):
    builder = _load_builder()
    source = tmp_path / "digital-twin-app-protocol"
    destination = tmp_path / "webapp" / "sdk"
    source.mkdir()
    expected = {}
    for filename in ("index.js", "client.js", "fmu-client.js", "batch-controller.js", "input-update-quota.js"):
        payload = f"// canonical {filename}\n".encode()
        (source / filename).write_bytes(payload)
        expected[filename] = payload
    destination.mkdir(parents=True)
    (destination / "obsolete.js").write_text("// stale module\n", encoding="utf-8")

    builder.sync_protocol_sdk(source / "index.js", destination)

    assert {filename: (destination / filename).read_bytes() for filename in expected} == expected
    assert not (destination / "obsolete.js").exists()


def test_checked_in_warehouse_sdk_matches_marketplace_source_when_available():
    marketplace_sdk = PROJECT_ROOT.parent / "Marketplace" / "packages" / "digital-twin-app-protocol"
    if not marketplace_sdk.is_dir():
        pytest.skip("Marketplace SDK checkout is not present in this workspace")

    checked_in_sdk = EXAMPLE_ROOT / "webapp" / "sdk"
    canonical_files = {path.name for path in marketplace_sdk.glob("*.js")}
    checked_in_files = {path.name for path in checked_in_sdk.glob("*.js")}
    assert checked_in_files == canonical_files
    for filename in canonical_files:
        assert (checked_in_sdk / filename).read_bytes() == (marketplace_sdk / filename).read_bytes()
