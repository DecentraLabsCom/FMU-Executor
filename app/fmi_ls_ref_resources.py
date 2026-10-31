"""Read public FMI-LS-REF resources, parameter sets, and visual assets."""

from __future__ import annotations

import hashlib
import math
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from typing import Any

from .fmi_ls_ref_archive import (
    FMI_LS_REF_MANIFEST_PATH,
    FMI_LS_REF_NAME,
    FMI_LS_REF_NAMESPACE,
    is_safe_xml,
    local_name,
    read_archive_file,
    read_fmi_ls_ref_manifest,
    resolve_archive_uri,
)


MAX_COMPANION_ASSET_BYTES = 50 * 1024 * 1024
MAX_PARAMETER_SET_BYTES = 512 * 1024
MAX_TOTAL_PARAMETER_SET_BYTES = 4 * 1024 * 1024
MAX_PARAMETER_SETS = 24
MAX_PARAMETERS_PER_SET = 2048
MAX_RELATED_RESOURCES = 128
_SSP_PARAMETER_SET_MEDIA_TYPE = "application/x-ssp-parameter-set"
_FLOAT_PARAMETER_TYPES = {"Real", "Float32", "Float64"}
_INTEGER_PARAMETER_TYPES = {
    "Integer", "Int8", "UInt8", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64",
}
_SSV_PARAMETER_TYPES = _FLOAT_PARAMETER_TYPES | _INTEGER_PARAMETER_TYPES | {
    "Boolean", "String", "Enumeration",
}


def read_related_resources(fmu_path: str | Path) -> dict[str, Any]:
    """Return bounded descriptors for local resources declared by the REF manifest."""
    empty = {"manifestName": None, "manifestVersion": None, "resources": []}
    try:
        with zipfile.ZipFile(fmu_path) as archive:
            manifest = read_fmi_ls_ref_manifest(archive)
            if manifest is None:
                return empty
            root, member_counts = manifest

            resources: list[dict[str, Any]] = []
            identity_counts: dict[str, int] = {}
            for related in root:
                if local_name(related) != "Related":
                    continue
                source = str(related.attrib.get("source") or "").strip()
                archive_path = resolve_archive_uri(FMI_LS_REF_MANIFEST_PATH, source)
                if archive_path is None:
                    # External and unsafe references are not included in public metadata.
                    continue

                role = str(related.attrib.get("role") or "").strip()[:128]
                media_type = str(related.attrib.get("type") or "").split(";", 1)[0].strip().lower()[:256]
                description = str(related.attrib.get("description") or "").strip()[:2048]
                labels: list[dict[str, str]] = []
                for child in related:
                    if local_name(child) != "Label" or len(labels) >= 32:
                        continue
                    label_name = str(child.attrib.get("name") or "").strip()[:256]
                    if not label_name:
                        continue
                    label_description = str(child.attrib.get("description") or "").strip()[:1024]
                    labels.append({"name": label_name, "description": label_description})

                identity = hashlib.sha256(
                    f"{role}\0{media_type}\0{source}".encode("utf-8")
                ).hexdigest()[:24]
                identity_counts[identity] = identity_counts.get(identity, 0) + 1
                resource_id = identity if identity_counts[identity] == 1 else f"{identity}_{identity_counts[identity]}"
                available = member_counts.get(archive_path) == 1
                if available:
                    try:
                        available = not archive.getinfo(archive_path).is_dir()
                    except KeyError:
                        available = False

                resources.append({
                    "id": resource_id,
                    "role": role,
                    "type": media_type,
                    "source": source[:2048],
                    "description": description,
                    "labels": labels,
                    "availableInFmu": available,
                })
                if len(resources) >= MAX_RELATED_RESOURCES:
                    break

            return {
                "manifestName": FMI_LS_REF_NAME,
                "manifestVersion": str(root.attrib.get(
                    f"{{{FMI_LS_REF_NAMESPACE}}}fmi-ls-version"
                ) or "")[:64],
                "resources": resources,
            }
    except (OSError, KeyError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile, zlib.error):
        return empty


def parse_ssv_parameter(parameter: ET.Element) -> tuple[str, Any, str | None]:
    """Parse one supported scalar or flat-array SSP/SSV parameter."""
    name = str(parameter.attrib.get("name") or "").strip()
    if not name or len(name) > 512:
        raise ValueError("invalid SSV parameter name")

    typed_elements = [
        child for child in parameter
        if local_name(child) in _SSV_PARAMETER_TYPES | {"Binary"}
    ]
    if len(typed_elements) != 1:
        raise ValueError("invalid SSV parameter type")
    value_element = typed_elements[0]
    value_type = local_name(value_element)
    if value_type == "Binary":
        raise ValueError("binary SSV values are not supported by the simulation input form")

    unit = value_element.attrib.get("unit")
    if unit and value_type not in _FLOAT_PARAMETER_TYPES:
        raise ValueError("SSV units are only supported for floating-point parameters")

    value_children = [child for child in value_element if local_name(child) == "Value"]
    raw_values = [child.text or "" for child in value_children]
    if value_children and "value" in value_element.attrib:
        raise ValueError("SSV value cannot use both an attribute and Value elements")
    if not value_children:
        raw_value = value_element.attrib.get("value")
        if raw_value is None:
            raise ValueError("SSV parameter value is missing")
        if value_type not in {"String", "Enumeration"} and len(value_element) > 0:
            raise ValueError("invalid SSV value children")
        raw_values = [raw_value]

    if not raw_values or any(len(value) > 65536 for value in raw_values):
        raise ValueError("SSV parameter value exceeds the supported size")
    is_array = any(local_name(child) == "Dimension" for child in parameter) or len(raw_values) > 1
    if len(raw_values) == 1 and value_type not in {"String", "Enumeration"}:
        parts = raw_values[0].strip().split()
        if len(parts) > 1:
            raw_values = parts
            is_array = True

    def convert(raw: str) -> Any:
        if value_type in _FLOAT_PARAMETER_TYPES:
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError("non-finite SSV value")
            return number
        if value_type in _INTEGER_PARAMETER_TYPES:
            return int(raw, 10)
        if value_type == "Boolean":
            lowered = raw.strip().lower()
            if lowered in {"true", "1"}:
                return True
            if lowered in {"false", "0"}:
                return False
            raise ValueError("invalid SSV boolean")
        if value_type == "Enumeration":
            return raw.strip()
        return raw

    values = [convert(raw) for raw in raw_values]
    return name, values if is_array else values[0], unit


def parse_ssv_parameter_set(
    data: bytes,
    *,
    related: ET.Element,
    preset_number: int,
) -> dict[str, Any] | None:
    """Parse a bounded SSP/SSV parameter set into the public preset shape."""
    if len(data) > MAX_PARAMETER_SET_BYTES or not is_safe_xml(data):
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    if local_name(root) != "ParameterSet" or root.attrib.get("version") not in {"1.0", "2.0"}:
        return None
    name = str(root.attrib.get("name") or "").strip()
    if not name or len(name) > 180:
        return None
    parameters_element = next((child for child in root if local_name(child) == "Parameters"), None)
    if parameters_element is None:
        return None
    parameter_nodes = [child for child in parameters_element if local_name(child) == "Parameter"]
    if not parameter_nodes or len(parameter_nodes) > MAX_PARAMETERS_PER_SET:
        return None

    parameters: dict[str, Any] = {}
    parameter_units: dict[str, str] = {}
    for parameter_node in parameter_nodes:
        variable_name, value, unit = parse_ssv_parameter(parameter_node)
        if variable_name in parameters:
            return None
        parameters[variable_name] = value
        if unit:
            parameter_units[variable_name] = unit

    description = str(related.attrib.get("description") or "").strip()
    if not description:
        label = next((child for child in related if local_name(child) == "Label"), None)
        description = str(label.attrib.get("description") or "").strip() if label is not None else ""
    return {
        "id": f"preset-{preset_number}",
        "name": name,
        "description": description[:1024],
        "parameters": parameters,
        "parameterUnits": parameter_units,
    }


def parse_experiment_parameter_set(data: bytes) -> dict[str, Any] | None:
    """Return the parameter values needed by the REF experiment interpreter."""
    parsed = parse_ssv_parameter_set(
        data,
        related=ET.Element("Related"),
        preset_number=1,
    )
    if parsed is None:
        return None
    return {
        "parameters": parsed["parameters"],
        "parameterUnits": parsed["parameterUnits"],
    }


def read_parameter_sets(fmu_path: str | Path) -> list[dict[str, Any]]:
    """Read bounded local SSP parameter-set references from an FMU."""
    presets: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(fmu_path) as archive:
            manifest = read_fmi_ls_ref_manifest(archive)
            if manifest is None:
                return presets
            root, member_counts = manifest

            referenced: list[tuple[ET.Element, str]] = []
            for related in root:
                if local_name(related) != "Related":
                    continue
                if str(related.attrib.get("role") or "").strip().lower() != "parameter":
                    continue
                media_type = str(related.attrib.get("type") or "").split(";", 1)[0].strip().lower()
                if media_type != _SSP_PARAMETER_SET_MEDIA_TYPE:
                    continue
                archive_path = resolve_archive_uri(
                    FMI_LS_REF_MANIFEST_PATH,
                    str(related.attrib.get("source") or ""),
                )
                if archive_path is not None:
                    referenced.append((related, archive_path))
                    if len(referenced) >= MAX_PARAMETER_SETS:
                        break

            total_bytes = 0
            for related, archive_path in referenced:
                if member_counts.get(archive_path) != 1:
                    continue
                try:
                    info = archive.getinfo(archive_path)
                except KeyError:
                    continue
                if info.is_dir() or info.file_size > MAX_PARAMETER_SET_BYTES:
                    continue
                if total_bytes + info.file_size > MAX_TOTAL_PARAMETER_SET_BYTES:
                    break
                content = read_archive_file(
                    archive,
                    archive_path,
                    MAX_PARAMETER_SET_BYTES,
                    member_counts=member_counts,
                )
                if content is None:
                    continue
                total_bytes += len(content)
                try:
                    parsed = parse_ssv_parameter_set(
                        content,
                        related=related,
                        preset_number=len(presets) + 1,
                    )
                except (ValueError, OverflowError):
                    parsed = None
                if parsed is not None:
                    presets.append(parsed)
    except (OSError, KeyError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile, zlib.error):
        return []
    return presets


def read_visual_asset(fmu_path: str | Path) -> tuple[bytes, str] | None:
    """Read the preferred declared web app or GLB from an FMI-LS-REF manifest."""
    try:
        with zipfile.ZipFile(fmu_path) as archive:
            manifest = read_fmi_ls_ref_manifest(archive)
            if manifest is None:
                return None
            root, member_counts = manifest

            candidates: list[tuple[str, str]] = []
            for related in root:
                if local_name(related) != "Related":
                    continue
                archive_path = resolve_archive_uri(
                    FMI_LS_REF_MANIFEST_PATH,
                    str(related.attrib.get("source") or ""),
                )
                if archive_path is None:
                    continue
                media_type = str(related.attrib.get("type") or "").split(";", 1)[0].strip().lower()
                lowered_path = archive_path.lower()
                if lowered_path.endswith(".webapp.zip") and media_type in {
                    "application/zip", "application/x-zip-compressed",
                }:
                    candidates.append((archive_path, "threejs-app"))
                elif lowered_path.endswith(".glb") and media_type in {
                    "model/gltf-binary", "application/octet-stream",
                }:
                    candidates.append((archive_path, "glb"))

            candidates.sort(key=lambda candidate: candidate[1] != "threejs-app")
            for archive_path, candidate_type in candidates:
                content = read_archive_file(
                    archive,
                    archive_path,
                    MAX_COMPANION_ASSET_BYTES,
                    member_counts=member_counts,
                )
                if content is not None:
                    return content, candidate_type
    except (OSError, KeyError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile, zlib.error):
        return None
    return None
