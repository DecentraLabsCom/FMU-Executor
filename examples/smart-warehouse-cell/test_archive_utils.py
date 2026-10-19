import os
import zipfile

from archive_utils import write_reproducible_zip


def test_reproducible_zip_ignores_source_file_timestamps_and_permissions(tmp_path):
    source = tmp_path / "webapp"
    source.mkdir()
    first_file = source / "app.js"
    first_file.write_bytes(b"console.log('warehouse')")
    (source / "index.html").write_bytes(b"<!doctype html><html></html>")
    first_file.chmod(0o600)
    os.utime(first_file, (1_600_000_000, 1_600_000_000))

    first_archive = tmp_path / "first.zip"
    write_reproducible_zip(source, first_archive)

    first_file.chmod(0o755)
    os.utime(first_file, (1_700_000_000, 1_700_000_000))
    second_archive = tmp_path / "second.zip"
    write_reproducible_zip(source, second_archive)

    assert first_archive.read_bytes() == second_archive.read_bytes()
    with zipfile.ZipFile(second_archive) as archive:
        assert archive.namelist() == ["app.js", "index.html"]
        assert archive.read("app.js") == b"console.log('warehouse')"
        assert all(entry.date_time == (1980, 1, 1, 0, 0, 0) for entry in archive.infolist())
