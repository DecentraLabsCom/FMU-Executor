"""Bounded, fail-closed access to FMI-LS-REF resources inside an FMU."""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Mapping
from urllib.parse import unquote, urlsplit


FMI_LS_REF_MANIFEST_PATH = "extra/org.fmi-standard.fmi-ls-ref/fmi-ls-manifest.xml"
FMI_LS_REF_NAME = "org.fmi-standard.fmi-ls-ref"
FMI_LS_REF_NAMESPACE = "http://fmi-standard.org/fmi-ls-manifest"
MAX_FMI_LS_REF_MANIFEST_BYTES = 1024 * 1024


def archive_member_counts(archive: zipfile.ZipFile) -> dict[str, int]:
    """Count paths once so callers can reject ambiguous duplicate members."""
    counts: dict[str, int] = {}
    for info in archive.infolist():
        counts[info.filename] = counts.get(info.filename, 0) + 1
    return counts


def read_archive_file(
    archive: zipfile.ZipFile,
    path: str,
    maximum_bytes: int,
    *,
    member_counts: Mapping[str, int] | None = None,
) -> bytes | None:
    """Read one bounded regular member, optionally requiring an unambiguous path."""
    if not path or isinstance(maximum_bytes, bool) or maximum_bytes < 0:
        return None
    if member_counts is not None and member_counts.get(path) != 1:
        return None
    try:
        info = archive.getinfo(path)
        if info.is_dir() or info.file_size > maximum_bytes:
            return None
        with archive.open(info) as source:
            content = source.read(maximum_bytes + 1)
        return content if len(content) <= maximum_bytes else None
    except (OSError, KeyError, RuntimeError, EOFError, NotImplementedError, zipfile.BadZipFile, zlib.error):
        return None


def is_safe_xml(data: bytes) -> bool:
    """Accept UTF-8 XML without DTDs or entity declarations."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    lowered = text.lower()
    return "<!doctype" not in lowered and "<!entity" not in lowered


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def namespace(element: ET.Element) -> str:
    return element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""


def resolve_archive_uri(base_archive_path: str, source: str) -> str | None:
    """Resolve a relative URI without allowing it to escape the FMU archive."""
    if not source or len(source) > 2048 or "\\" in source or "\x00" in source:
        return None
    try:
        parsed = urlsplit(source)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path:
        return None
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or "\x00" in decoded_path or decoded_path.startswith("/"):
        return None
    normalized = posixpath.normpath(
        posixpath.join(posixpath.dirname(base_archive_path), decoded_path)
    )
    if normalized in {"", ".", ".."} or normalized.startswith(("../", "/")):
        return None
    return normalized


def read_fmi_ls_ref_manifest(
    archive: zipfile.ZipFile,
) -> tuple[ET.Element, dict[str, int]] | None:
    """Return a validated REF manifest and the archive path index.

    An absent, oversized, malformed, unsafe, or ambiguous manifest is treated as
    unavailable. The returned counts let resource readers reject duplicate ZIP
    names without rescanning the central directory for every reference.
    """
    try:
        member_counts = archive_member_counts(archive)
        if member_counts.get(FMI_LS_REF_MANIFEST_PATH) != 1:
            return None
        manifest_data = read_archive_file(
            archive,
            FMI_LS_REF_MANIFEST_PATH,
            MAX_FMI_LS_REF_MANIFEST_BYTES,
            member_counts=member_counts,
        )
        if manifest_data is None or not is_safe_xml(manifest_data):
            return None
        root = ET.fromstring(manifest_data)
    except (OSError, ValueError, ET.ParseError, RuntimeError, EOFError, zipfile.BadZipFile, zlib.error):
        return None

    if (
        local_name(root) != "fmiReferences"
        or root.attrib.get(f"{{{FMI_LS_REF_NAMESPACE}}}fmi-ls-name") != FMI_LS_REF_NAME
    ):
        return None
    return root, member_counts
