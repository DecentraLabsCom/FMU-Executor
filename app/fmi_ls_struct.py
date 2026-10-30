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
    f"{MAP_KIND_PREFIX}irregular",
}
DOMAIN_KIND = f"{MANIFEST_NAME}.map.domain"
DOMAIN_INPUT_KIND = f"{MANIFEST_NAME}.map.domainInput"
CODOMAIN_KIND = f"{MANIFEST_NAME}.map.codomain"
CODOMAIN_OUTPUT_KIND = f"{MANIFEST_NAME}.map.codomainOutput"
ORDERING_CONSTRAINT_TYPE = f"{MANIFEST_NAME}.orderingConstraint"
ORDERING_CONSTRAINTS = {
    "unordered", "increasing", "decreasing", "strictlyIncreasing", "strictlyDecreasing",
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
            ordering_constraint = raw_member.get("orderingConstraint")
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
                or (ordering_constraint is not None and (
                    variable_kind != DOMAIN_KIND
                    or terminal_kind != f"{MAP_KIND_PREFIX}rectilinearGrid"
                    or ordering_constraint not in ORDERING_CONSTRAINTS
                ))
            ):
                valid = False
                break
            members.append({
                "variableName": variable_name,
                "memberName": member_name,
                "variableKind": variable_kind,
                **({"orderingConstraint": ordering_constraint} if ordering_constraint is not None else {}),
            })
            member_names.add(member_name)
            member_variables.add(variable_name)

        if not valid or not _has_valid_map_member_order(members):
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
                    normalized_member = {
                        "variableName": member.get("variableName"),
                        "memberName": member.get("memberName"),
                        "variableKind": member.get("variableKind"),
                    }
                    annotations = next(
                        (child for child in member if child.tag.rsplit("}", 1)[-1] == "Annotations"),
                        None,
                    )
                    if annotations is not None:
                        ordering_annotations = [
                            child for child in annotations
                            if child.tag.rsplit("}", 1)[-1] == "Annotation"
                            and child.get("type") == ORDERING_CONSTRAINT_TYPE
                        ]
                        if len(ordering_annotations) > 1:
                            normalized_member["orderingConstraint"] = ""
                        elif ordering_annotations:
                            normalized_member["orderingConstraint"] = (ordering_annotations[0].text or "").strip()
                    raw_members.append(normalized_member)
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


def _has_valid_map_member_order(members: list[dict]) -> bool:
    """Check required axes and the immediate pairing of optional map members."""
    domains = [index for index, member in enumerate(members) if member["variableKind"] == DOMAIN_KIND]
    codomains = [index for index, member in enumerate(members) if member["variableKind"] == CODOMAIN_KIND]
    if not domains or not codomains:
        return False

    for index, member in enumerate(members):
        kind = member["variableKind"]
        if kind in {DOMAIN_KIND, DOMAIN_INPUT_KIND} and index >= min(codomains):
            return False
        if kind in {CODOMAIN_KIND, CODOMAIN_OUTPUT_KIND} and index <= max(domains):
            return False
        if kind in {DOMAIN_KIND, CODOMAIN_KIND}:
            continue
        if kind == DOMAIN_INPUT_KIND:
            if index == 0 or members[index - 1]["variableKind"] != DOMAIN_KIND:
                return False
        elif kind == CODOMAIN_OUTPUT_KIND:
            if index == 0 or members[index - 1]["variableKind"] != CODOMAIN_KIND:
                return False
        elif kind.startswith(MAP_KIND_PREFIX):
            return False

    return all(
        member.get("orderingConstraint") in ORDERING_CONSTRAINTS
        for member in members
        if "orderingConstraint" in member
    )
