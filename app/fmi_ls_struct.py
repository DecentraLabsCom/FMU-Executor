"""Read FMI-LS-STRUCT map declarations from an FMI 3 FMU archive."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile
import zlib


MANIFEST_NAME = "org.fmi-standard.fmi-ls-struct"
MANIFEST_PATH = f"extra/{MANIFEST_NAME}/fmi-ls-manifest.xml"
TERMINALS_PATH = "terminalsAndIcons/terminalsAndIcons.xml"
MATCHING_RULE = f"{MANIFEST_NAME}.map"
MAP_KIND_PREFIX = f"{MANIFEST_NAME}.map."
MAP_TERMINAL_KINDS = {
    f"{MAP_KIND_PREFIX}rectilinearGrid",
    f"{MAP_KIND_PREFIX}irregularGrid",
}
MAX_XML_BYTES = 1024 * 1024
MAX_MAPS = 128
MAX_MEMBERS_PER_MAP = 512
MAX_TOTAL_MAP_MEMBERS = 4096
_MANIFEST_NAMESPACE = "http://fmi-standard.org/fmi-ls-manifest"


def normalize_fmi_ls_struct(
    value,
    *,
    variable_names: Sequence[str] | None = None,
) -> dict | None:
    """Validate and bound a normalized STRUCT payload from a trusted backend."""
    if not isinstance(value, Mapping) or value.get("manifestName") != MANIFEST_NAME:
        return None

    version = value.get("manifestVersion")
    if not isinstance(version, str) or not version.strip() or len(version) > 64:
        return None
    known_variables = set(variable_names) if variable_names is not None else None
    raw_maps = value.get("maps")
    if not isinstance(raw_maps, list) or len(raw_maps) > MAX_MAPS:
        return None

    maps = []
    seen_names: set[str] = set()
    total_members = 0
    for raw_map in raw_maps:
        if not isinstance(raw_map, Mapping):
            continue
        name = raw_map.get("name")
        terminal_kind = raw_map.get("terminalKind")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 256
            or name in seen_names
            or not isinstance(terminal_kind, str)
            or terminal_kind not in MAP_TERMINAL_KINDS
            or raw_map.get("matchingRule") != MATCHING_RULE
        ):
            continue

        raw_members = raw_map.get("members")
        if not isinstance(raw_members, list) or not 1 <= len(raw_members) <= MAX_MEMBERS_PER_MAP:
            continue
        if total_members + len(raw_members) > MAX_TOTAL_MAP_MEMBERS:
            continue
        members = []
        member_names: set[str] = set()
        member_variables: set[str] = set()
        valid = True
        for raw_member in raw_members:
            if not isinstance(raw_member, Mapping):
                valid = False
                break
            variable_name = raw_member.get("variableName")
            member_name = raw_member.get("memberName")
            variable_kind = raw_member.get("variableKind")
            if (
                not isinstance(variable_name, str)
                or not variable_name
                or len(variable_name) > 512
                or not isinstance(member_name, str)
                or not member_name
                or len(member_name) > 256
                or not isinstance(variable_kind, str)
                or not variable_kind
                or len(variable_kind) > 256
                or (known_variables is not None and variable_name not in known_variables)
                or member_name in member_names
                or variable_name in member_variables
            ):
                valid = False
                break
            members.append({
                "variableName": variable_name,
                "memberName": member_name,
                "variableKind": variable_kind,
            })
            member_names.add(member_name)
            member_variables.add(variable_name)

        if not valid or not any(member["variableKind"] == f"{MANIFEST_NAME}.map.domain" for member in members):
            continue
        if not any(member["variableKind"] == f"{MANIFEST_NAME}.map.codomain" for member in members):
            continue

        maps.append({
            "name": name.strip(),
            "terminalKind": terminal_kind,
            "matchingRule": MATCHING_RULE,
            "members": members,
        })
        seen_names.add(name)
        total_members += len(members)

    return {
        "manifestName": MANIFEST_NAME,
        "manifestVersion": version.strip(),
        "maps": maps,
    }


def read_fmi_ls_struct(
    fmu_path: str | Path,
    *,
    fmi_version: str,
    variable_names: Sequence[str] | None = None,
) -> dict | None:
    """Read map terminals from a bounded FMI-LS-STRUCT manifest in an FMU."""
    if str(fmi_version).split(".", 1)[0] != "3":
        return None

    try:
        with zipfile.ZipFile(fmu_path) as archive:
            if archive.namelist().count(MANIFEST_PATH) != 1 or archive.namelist().count(TERMINALS_PATH) != 1:
                return None
            manifest_info = archive.getinfo(MANIFEST_PATH)
            terminals_info = archive.getinfo(TERMINALS_PATH)
            if manifest_info.file_size > MAX_XML_BYTES or terminals_info.file_size > MAX_XML_BYTES:
                return None
            manifest_bytes = archive.read(manifest_info)
            terminals_bytes = archive.read(terminals_info)
        if len(manifest_bytes) > MAX_XML_BYTES or len(terminals_bytes) > MAX_XML_BYTES:
            return None
        manifest_text = manifest_bytes.decode("utf-8-sig")
        terminals_text = terminals_bytes.decode("utf-8-sig")
        if any(token in data.lower() for data in (manifest_text, terminals_text) for token in ("<!doctype", "<!entity")):
            return None

        manifest_root = ET.fromstring(manifest_text)
        if (
            manifest_root.tag.rsplit("}", 1)[-1] != "fmiLayeredStandardManifest"
            or manifest_root.attrib.get(f"{{{_MANIFEST_NAMESPACE}}}fmi-ls-name") != MANIFEST_NAME
        ):
            return None
        version = str(manifest_root.attrib.get(f"{{{_MANIFEST_NAMESPACE}}}fmi-ls-version") or "")[:64]
        if not version:
            return None

        terminals_root = ET.fromstring(terminals_text)
        if (
            terminals_root.tag.rsplit("}", 1)[-1] != "fmiTerminalsAndIcons"
            or terminals_root.attrib.get("fmiVersion") != "3.0"
        ):
            return None

        maps = []
        terminals = next(
            (child for child in terminals_root if child.tag.rsplit("}", 1)[-1] == "Terminals"),
            None,
        )
        if terminals is not None:
            for terminal in terminals:
                if terminal.tag.rsplit("}", 1)[-1] != "Terminal":
                    continue
                raw_members = []
                for member in terminal:
                    if member.tag.rsplit("}", 1)[-1] != "TerminalMemberVariable":
                        continue
                    raw_members.append({
                        "variableName": member.get("variableName"),
                        "memberName": member.get("memberName"),
                        "variableKind": member.get("variableKind"),
                    })
                maps.append({
                    "name": terminal.get("name"),
                    "terminalKind": terminal.get("terminalKind"),
                    "matchingRule": terminal.get("matchingRule"),
                    "members": raw_members,
                })
                if len(maps) >= MAX_MAPS:
                    break

        return normalize_fmi_ls_struct({
            "manifestName": MANIFEST_NAME,
            "manifestVersion": version,
            "maps": maps,
        }, variable_names=variable_names)
    except (OSError, KeyError, ValueError, UnicodeDecodeError, ET.ParseError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error):
        return None
