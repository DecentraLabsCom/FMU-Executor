"""Deterministic ZIP writer for the Smart Warehouse distributable artifacts."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path


def write_reproducible_zip(source_root: Path, output_path: Path) -> None:
    source_root = Path(source_root).resolve(strict=True)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(path for path in source_root.rglob("*") if path.is_file())

    with zipfile.ZipFile(
        output_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in files:
            if path.is_symlink():
                raise ValueError(f"ZIP source contains a symbolic link: {path}")
            relative_path = path.relative_to(source_root).as_posix()
            entry = zipfile.ZipInfo(relative_path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, path.read_bytes())
