"""Merge one platform build into the canonical BinaryClockTest FMU."""

from __future__ import annotations

import argparse
import os
import tempfile
import zipfile
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
FMU_PATH = PROJECT_DIR.parent / "BinaryClockTest.fmu"
MODEL_DESCRIPTION = PROJECT_DIR / "modelDescription.xml"
PLATFORM_BINARY = {
    "windows": ("x86_64-windows", ".dll"),
    "linux": ("x86_64-linux", ".so"),
}


def _normalized_member_name(name: str) -> str:
    return name.replace("\\", "/")


def package(platform: str, binary: Path) -> None:
    platform_directory, suffix = PLATFORM_BINARY[platform]
    if binary.suffix != suffix or not binary.is_file():
        raise ValueError(f"Expected an existing {suffix} binary for {platform}: {binary}")

    binary_entry = f"binaries/{platform_directory}/BinaryClockTest{suffix}"
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    if FMU_PATH.is_file():
        with zipfile.ZipFile(FMU_PATH) as archive:
            for info in archive.infolist():
                original_name = info.filename
                normalized_name = _normalized_member_name(original_name)
                if normalized_name in {"modelDescription.xml", binary_entry}:
                    continue
                info.filename = normalized_name
                entries.append((info, archive.read(original_name)))

    fd, temporary_name = tempfile.mkstemp(prefix=".BinaryClockTest-", suffix=".fmu", dir=FMU_PATH.parent)
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for info, contents in entries:
                archive.writestr(info, contents)
            archive.writestr("modelDescription.xml", MODEL_DESCRIPTION.read_bytes())
            archive.write(binary, binary_entry)
        with zipfile.ZipFile(temporary_path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise OSError(f"Invalid FMU member after packaging: {bad_member}")
            names = set(archive.namelist())
            required = {"modelDescription.xml", binary_entry}
            if not required.issubset(names):
                raise OSError("The packaged FMU is missing its model description or platform binary.")
        os.replace(temporary_path, FMU_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)

    print(f"Updated {FMU_PATH} with {platform_directory} binary.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=PLATFORM_BINARY, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    package(args.platform, args.binary.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
