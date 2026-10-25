from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path


PACKAGER_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "reference-fmus"
    / "BinaryClockTest"
    / "package_fmu.py"
)


def _load_packager():
    spec = importlib.util.spec_from_file_location("binary_clock_packager", PACKAGER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packager_normalizes_existing_fmu_member_paths(tmp_path: Path, monkeypatch):
    packager = _load_packager()
    assert packager._normalized_member_name(
        r"binaries\x86_64-windows\BinaryClockTest.dll"
    ) == "binaries/x86_64-windows/BinaryClockTest.dll"

    archive_path = tmp_path / "BinaryClockTest.fmu"
    model_description = tmp_path / "modelDescription.xml"
    linux_binary = tmp_path / "BinaryClockTest.so"
    model_description.write_text("<fmiModelDescription/>", encoding="utf-8")
    linux_binary.write_bytes(b"linux-binary")

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("modelDescription.xml", b"<old-model/>")
        for name, contents in (
            ("binaries/x86_64-windows/BinaryClockTest.dll", b"windows-binary"),
            ("binaries/x86_64-linux/BinaryClockTest.so", b"old-linux-binary"),
            ("resources/reference.txt", b"resource"),
        ):
            archive.writestr(name, contents)
    raw_archive = archive_path.read_bytes()
    for name in (
        b"binaries/x86_64-windows/BinaryClockTest.dll",
        b"binaries/x86_64-linux/BinaryClockTest.so",
        b"resources/reference.txt",
    ):
        raw_archive = raw_archive.replace(name, name.replace(b"/", b"\\"))
    archive_path.write_bytes(raw_archive)

    monkeypatch.setattr(packager, "FMU_PATH", archive_path)
    monkeypatch.setattr(packager, "MODEL_DESCRIPTION", model_description)
    packager.package("linux", linux_binary)

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert "binaries/x86_64-windows/BinaryClockTest.dll" in names
        assert "binaries/x86_64-linux/BinaryClockTest.so" in names
        assert "resources/reference.txt" in names
        assert not any("\\" in name for name in names)
        assert archive.read("binaries/x86_64-windows/BinaryClockTest.dll") == b"windows-binary"
        assert archive.read("binaries/x86_64-linux/BinaryClockTest.so") == b"linux-binary"
