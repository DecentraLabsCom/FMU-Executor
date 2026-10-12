from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import config, fmu_storage
from tests.fmu_fixture import write_test_fmu


@pytest.fixture
def storage_root(monkeypatch, tmp_path):
    root = tmp_path / "fmus"
    state = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr(config, "FMU_ROOT", root)
    monkeypatch.setattr(config, "STATE_DIR", state)
    fmu_storage._quarantine_cache.clear()
    yield root
    fmu_storage._quarantine_cache.clear()


@pytest.mark.parametrize("key", ["../outside.fmu", "folder//model.fmu", ".hidden.fmu", "a\\b.fmu", ""])
def test_access_keys_reject_unsafe_paths(key):
    with pytest.raises(ValueError):
        fmu_storage.validate_access_key(key)


def test_catalog_discovers_direct_and_single_fmu_directories(storage_root):
    write_test_fmu(storage_root / "direct.fmu")
    write_test_fmu(storage_root / "provider" / "model.fmu")
    write_test_fmu(storage_root / "multiple" / "first.fmu")
    write_test_fmu(storage_root / "multiple" / "second.fmu")

    assert fmu_storage.list_fmus() == ["direct.fmu", "multiple", "provider"]
    assert fmu_storage.get_fmu_path("provider") == (storage_root / "provider" / "model.fmu").resolve()
    with pytest.raises(FileNotFoundError):
        fmu_storage.get_fmu_path("multiple")


def test_archive_report_rejects_traversal_duplicates_and_limits(storage_root, monkeypatch):
    traversal = storage_root / "traversal.fmu"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../escape", "bad")
    result = fmu_storage._archive_report(traversal)
    assert result[0] is False
    assert "outside its extraction root" in result[1][0]

    duplicate = storage_root / "duplicate.fmu"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("modelDescription.xml", "one")
        archive.writestr("modelDescription.xml", "two")
    assert "duplicate paths" in fmu_storage._archive_report(duplicate)[1][0]

    valid = write_test_fmu(storage_root / "valid.fmu")
    monkeypatch.setattr(config, "MAX_FMU_ARCHIVE_BYTES", 1)
    assert "compressed size limit" in fmu_storage._archive_report(valid)[1][0]
    monkeypatch.setattr(config, "MAX_FMU_ARCHIVE_BYTES", 10_000_000)
    monkeypatch.setattr(config, "MAX_FMU_ARCHIVE_ENTRIES", 1)
    too_many = fmu_storage._archive_report(valid)
    assert "too many entries" in too_many[1][0]


def test_inspection_reports_missing_invalid_and_incompatible_fmus(storage_root, monkeypatch):
    missing = fmu_storage.inspect_fmu_path(None, access_key="missing.fmu")
    assert missing["issues"] == ["FMU file not found"]

    invalid = storage_root / "invalid.fmu"
    invalid.write_bytes(b"not a zip")
    assert "not a readable ZIP" in fmu_storage.inspect_fmu_path(invalid, access_key="invalid.fmu")["issues"][0]

    missing_description = storage_root / "no-description.fmu"
    with zipfile.ZipFile(missing_description, "w") as archive:
        archive.writestr("payload", b"data")
    assert "no modelDescription.xml" in fmu_storage.inspect_fmu_path(missing_description, access_key="no-description.fmu")["issues"][0]

    valid = write_test_fmu(storage_root / "valid.fmu")
    description = SimpleNamespace(
        fmiVersion="4.0", guid="", coSimulation=None,
        modelExchange=None, scheduledExecution=None,
    )
    monkeypatch.setattr(fmu_storage.fmpy, "read_model_description", lambda _path: description)
    report = fmu_storage.inspect_fmu_path(valid, access_key="valid.fmu")
    assert report["modelDescriptionValid"] is True
    assert report["stationCompatible"] is False
    assert any("FMI 2.0 and FMI 3.0" in issue for issue in report["issues"])
    assert any("no GUID" in issue for issue in report["issues"])


def test_inspection_masks_parser_errors_and_hash_is_optional(storage_root, monkeypatch):
    path = write_test_fmu(storage_root / "broken-model.fmu")
    parser = Mock(side_effect=RuntimeError("internal parser detail"))
    monkeypatch.setattr(fmu_storage.fmpy, "read_model_description", parser)
    report = fmu_storage.inspect_fmu_path(path, access_key="broken-model.fmu", include_hash=False)
    assert report["issues"] == ["Cannot parse model description"]
    assert "sha256" not in report


def test_snapshot_is_content_addressed_and_quarantine_round_trips(storage_root):
    source = write_test_fmu(storage_root / "provider" / "model.fmu")
    snapshot, digest = fmu_storage.snapshot_fmu(source)
    assert snapshot.name == f"{digest}.fmu"
    assert digest == hashlib.sha256(source.read_bytes()).hexdigest()
    second_snapshot, second_digest = fmu_storage.snapshot_fmu(source)
    assert second_snapshot == snapshot and second_digest == digest

    fmu_storage.quarantine("provider", "invalid input contract")
    assert not fmu_storage.fmu_exists("provider")
    assert fmu_storage.is_quarantined("provider")
    info = fmu_storage.quarantine_info("provider")
    assert info is not None
    assert info["reason"] == "invalid input contract"
    assert fmu_storage.list_quarantined()[0]["accessKey"] == "provider"
    assert fmu_storage.unquarantine("provider") is True
    assert fmu_storage.fmu_exists("provider")
    assert fmu_storage.unquarantine("provider") is False


def test_host_binary_mapping_and_missing_storage_root(monkeypatch, tmp_path):
    monkeypatch.setattr(fmu_storage.platform, "system", lambda: "Windows")
    monkeypatch.setattr(fmu_storage.platform, "machine", lambda: "ARM64")
    assert fmu_storage._host_binary_directories() == ["aarch64-windows"]
    monkeypatch.setattr(fmu_storage.platform, "system", lambda: "Darwin")
    assert fmu_storage._host_binary_directories() == ["darwinarm64", "aarch64-darwin"]
    monkeypatch.setattr(fmu_storage.platform, "system", lambda: "Plan9")
    assert fmu_storage._host_binary_directories() == []

    monkeypatch.setattr(config, "FMU_ROOT", tmp_path / "missing-root")
    assert fmu_storage.list_fmus() == []


def test_validation_and_snapshot_report_storage_errors(storage_root, monkeypatch):
    monkeypatch.setattr(fmu_storage, "inspect_fmu", lambda _key: {
        "archiveValid": True, "stationCompatible": True, "issues": [],
    })
    assert fmu_storage.validate_fmu("model.fmu") == (True, "")
    monkeypatch.setattr(fmu_storage, "inspect_fmu", lambda _key: {
        "archiveValid": False, "stationCompatible": False,
        "issues": ["bad archive", "unsupported platform"],
    })
    assert fmu_storage.validate_fmu("model.fmu") == (False, "bad archive; unsupported platform")

    with pytest.raises(ValueError, match="must be a file"):
        fmu_storage.snapshot_fmu(storage_root)
    path = storage_root / "too-large.fmu"
    path.write_bytes(b"too large")
    monkeypatch.setattr(config, "MAX_FMU_ARCHIVE_BYTES", 2)
    with pytest.raises(ValueError, match="compressed size limit"):
        fmu_storage.snapshot_fmu(path)
