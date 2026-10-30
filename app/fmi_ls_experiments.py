"""Read a bounded, local subset of FMI-LS-REF experiment declarations."""

from __future__ import annotations

import math
import posixpath
import csv
import io
import zipfile
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit


FMI_LS_REF_MANIFEST_PATH = "extra/org.fmi-standard.fmi-ls-ref/fmi-ls-manifest.xml"
FMI_LS_REF_NAME = "org.fmi-standard.fmi-ls-ref"
MA_HS_EXPERIMENTS_MEDIA_TYPE = "application/x-ma-hs-experiments"
SSP_PARAMETER_SET_MEDIA_TYPE = "application/x-ssp-parameter-set"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_EXPERIMENT_FILE_BYTES = 1024 * 1024
MAX_PARAMETER_FILE_BYTES = 512 * 1024
MAX_TOTAL_PARAMETER_BYTES = 512 * 1024
MAX_STIMULI_FILE_BYTES = 1024 * 1024
MAX_STIMULI_EVENTS = 128
MAX_EXPERIMENT_SETS = 8
MAX_EXPERIMENTS_PER_SET = 16
MA_HS_EXPERIMENTS_NAMESPACE = "http://modelica.org/HSExperiments1/HSExperiments"


def read_fmi_ls_ref_experiments(
    fmu_path: Path,
    *,
    parse_parameter_set: Callable[[bytes], dict[str, Any] | None],
) -> dict[str, Any]:
    """Discover REF experiments and bounded local CSV stimuli.

    Only MA-HS-Experiments declarations, SSP parameter sets, and local
    time/value CSV stimuli are interpreted. References and solver tolerances
    remain unsupported; external URIs are never opened.
    """
    try:
        with zipfile.ZipFile(fmu_path) as archive:
            manifest_data = _read_archive_file(archive, FMI_LS_REF_MANIFEST_PATH, MAX_MANIFEST_BYTES)
            if manifest_data is None or not _safe_xml(manifest_data):
                return {"resourceSets": [], "truncated": False}
            manifest_root = ET.fromstring(manifest_data)
            namespace = "http://fmi-standard.org/fmi-ls-manifest"
            if (
                _local_name(manifest_root) != "fmiReferences"
                or manifest_root.attrib.get(f"{{{namespace}}}fmi-ls-name") != FMI_LS_REF_NAME
            ):
                return {"resourceSets": [], "truncated": False}

            resource_name_counts: dict[str, int] = {}
            for info in archive.infolist():
                resource_name_counts[info.filename] = resource_name_counts.get(info.filename, 0) + 1

            sets: list[dict[str, Any]] = []
            truncated = False
            total_parameter_bytes = 0
            for related in manifest_root:
                if _local_name(related) != "Related":
                    continue
                role = str(related.attrib.get("role") or "").strip().lower()
                if role not in {"experiment", "experiment/smoke-test"}:
                    continue
                if len(sets) >= MAX_EXPERIMENT_SETS:
                    truncated = True
                    continue

                source = str(related.attrib.get("source") or "").strip()
                archive_path = _resolve_archive_uri(FMI_LS_REF_MANIFEST_PATH, source)
                media_type = str(related.attrib.get("type") or "").split(";", 1)[0].strip().lower()
                descriptor = {
                    "source": source[:2048] if archive_path is not None else "",
                    "role": role[:128],
                    "type": media_type[:256],
                    "name": "",
                    "description": str(related.attrib.get("description") or "").strip()[:2048],
                    "status": "unsupported",
                    "reason": "unsupported_experiment_resource",
                    "experiments": [],
                }
                sets.append(descriptor)

                if media_type != MA_HS_EXPERIMENTS_MEDIA_TYPE:
                    descriptor["reason"] = "unsupported_experiment_media_type"
                    continue
                if archive_path is None:
                    descriptor["reason"] = "external_or_unsafe_resource"
                    continue
                if resource_name_counts.get(archive_path) != 1:
                    descriptor["status"] = "unavailable"
                    descriptor["reason"] = "experiment_file_missing_or_ambiguous"
                    continue
                experiment_data = _read_archive_file(archive, archive_path, MAX_EXPERIMENT_FILE_BYTES)
                if experiment_data is None:
                    descriptor["status"] = "unavailable"
                    descriptor["reason"] = "experiment_file_unavailable_or_too_large"
                    continue
                if not _safe_xml(experiment_data):
                    descriptor["status"] = "invalid"
                    descriptor["reason"] = "invalid_experiment_xml"
                    continue
                try:
                    experiment_root = ET.fromstring(experiment_data)
                except ET.ParseError:
                    descriptor["status"] = "invalid"
                    descriptor["reason"] = "invalid_experiment_xml"
                    continue
                if _local_name(experiment_root) != "Experiments":
                    descriptor["status"] = "invalid"
                    descriptor["reason"] = "invalid_experiment_root"
                    continue
                experiment_namespace = _namespace(experiment_root)
                if experiment_namespace not in {"", MA_HS_EXPERIMENTS_NAMESPACE}:
                    descriptor["status"] = "unsupported"
                    descriptor["reason"] = "unsupported_experiment_namespace"
                    continue
                set_name = str(experiment_root.attrib.get("name") or "").strip()
                if not set_name or len(set_name) > 180:
                    descriptor["status"] = "invalid"
                    descriptor["reason"] = "invalid_experiment_set_name"
                    continue
                descriptor["name"] = set_name
                if not descriptor["description"]:
                    descriptor["description"] = str(experiment_root.attrib.get("description") or "").strip()[:2048]

                experiment_tags = {"Experiment", "TransientExperiment"}
                experiments = [child for child in experiment_root if _local_name(child) in experiment_tags]
                descriptor["truncated"] = len(experiments) > MAX_EXPERIMENTS_PER_SET
                if any(
                    _local_name(child) not in experiment_tags | {"Annotations"}
                    for child in experiment_root
                ):
                    descriptor["status"] = "unsupported"
                    descriptor["reason"] = "unsupported_experiment_type"
                    continue
                for child in experiment_root:
                    if _local_name(child) not in experiment_tags:
                        continue
                    if len(descriptor["experiments"]) >= MAX_EXPERIMENTS_PER_SET:
                        break
                    case = _parse_experiment(
                        child,
                        experiment_namespace=experiment_namespace,
                        archive=archive,
                        experiment_archive_path=archive_path,
                        resource_name_counts=resource_name_counts,
                        parse_parameter_set=parse_parameter_set,
                        remaining_parameter_bytes=MAX_TOTAL_PARAMETER_BYTES - total_parameter_bytes,
                    )
                    total_parameter_bytes += case.pop("_parameterBytes", 0)
                    case["eligibleForSmokeProbe"] = role in {"experiment", "experiment/smoke-test"}
                    if not case["eligibleForSmokeProbe"] and case["status"] == "ready":
                        case["status"] = "not_selected"
                        case["reason"] = "experiment_role_is_not_smoke_test"
                    descriptor["experiments"].append(case)

                if not descriptor["experiments"]:
                    descriptor["status"] = "invalid"
                    descriptor["reason"] = "no_experiments"
                elif any(case["status"] == "invalid" for case in descriptor["experiments"]):
                    descriptor["status"] = "invalid"
                    descriptor["reason"] = "invalid_experiment"
                elif any(case["status"] == "ready" for case in descriptor["experiments"]):
                    descriptor["status"] = "ready"
                    descriptor["reason"] = ""
                elif any(case["status"] == "unsupported" for case in descriptor["experiments"]):
                    descriptor["status"] = "unsupported"
                    descriptor["reason"] = "experiment_uses_unsupported_features"
                else:
                    descriptor["status"] = "not_selected"
                    descriptor["reason"] = "experiment_role_is_not_smoke_test"
            return {"resourceSets": sets, "truncated": truncated}
    except (OSError, KeyError, ValueError, ET.ParseError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error):
        return {"resourceSets": [], "truncated": False}


def _parse_experiment(
    element: ET.Element,
    *,
    experiment_namespace: str,
    archive: zipfile.ZipFile,
    experiment_archive_path: str,
    resource_name_counts: dict[str, int],
    parse_parameter_set: Callable[[bytes], dict[str, Any] | None],
    remaining_parameter_bytes: int,
) -> dict[str, Any]:
    name = str(element.attrib.get("name") or "").strip()
    case: dict[str, Any] = {
        "name": name[:180],
        "description": str(element.attrib.get("description") or "").strip()[:2048],
        "options": {},
        "parameters": {},
        "parameterUnits": {},
        "inputSchedule": [],
        "status": "invalid",
        "reason": "invalid_experiment",
    }
    if not name or len(name) > 180:
        case["reason"] = "invalid_experiment_name"
        return case

    for field in ("startTime", "stopTime", "stepSize", "tolerance"):
        raw = element.attrib.get(field)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            case["reason"] = "invalid_experiment_time_option"
            return case
        if not math.isfinite(value):
            case["reason"] = "invalid_experiment_time_option"
            return case
        if field == "stepSize" and value <= 0:
            case["reason"] = "invalid_experiment_step_size"
            return case
        case["options"][field] = value

    children: dict[str, ET.Element] = {}
    for child in element:
        local_name = _local_name(child)
        if _local_name(child) == "Annotations":
            continue
        if _namespace(child) != experiment_namespace:
            case["reason"] = "unsupported_or_ambiguous_experiment_element"
            return case
        if local_name not in {"Parameters", "Stimuli", "References"}:
            case["status"] = "unsupported"
            case["reason"] = "unsupported_experiment_element"
            return case
        if local_name in children:
            case["reason"] = "ambiguous_experiment_element"
            return case
        children[local_name] = child

    if "tolerance" in case["options"]:
        case["status"] = "unsupported"
        case["reason"] = "solver_tolerance_not_supported"
        return case
    if "References" in children:
        case["status"] = "unsupported"
        case["reason"] = "reference_results_not_supported"
        return case

    parameters_element = children.get("Parameters")
    if parameters_element is not None:
        parameter_type = str(parameters_element.attrib.get("type") or SSP_PARAMETER_SET_MEDIA_TYPE)
        if parameter_type.split(";", 1)[0].strip().lower() != SSP_PARAMETER_SET_MEDIA_TYPE:
            case["status"] = "unsupported"
            case["reason"] = "unsupported_parameter_file_type"
            return case
        parameter_source = str(parameters_element.attrib.get("source") or "").strip()
        if not parameter_source:
            case["reason"] = "parameter_file_source_missing"
            return case
        parameter_path = _resolve_archive_uri(
            experiment_archive_path,
            parameter_source,
        )
        if parameter_path is None:
            case["status"] = "unsupported"
            case["reason"] = "external_or_unsafe_parameter_resource"
            return case
        if resource_name_counts.get(parameter_path) != 1:
            case["status"] = "unavailable"
            case["reason"] = "parameter_file_missing_or_ambiguous"
            return case
        parameter_data = _read_archive_file(archive, parameter_path, MAX_PARAMETER_FILE_BYTES)
        if parameter_data is None:
            case["status"] = "unavailable"
            case["reason"] = "parameter_file_unavailable_or_too_large"
            return case
        case["_parameterBytes"] = len(parameter_data)
        if len(parameter_data) > remaining_parameter_bytes:
            case["status"] = "unavailable"
            case["reason"] = "experiment_parameter_data_limit_exceeded"
            return case
        try:
            parsed_parameter_set = parse_parameter_set(parameter_data)
        except (ValueError, OverflowError, ET.ParseError):
            parsed_parameter_set = None
        if not isinstance(parsed_parameter_set, dict):
            case["reason"] = "invalid_ssp_parameter_set"
            return case
        case["parameters"] = parsed_parameter_set.get("parameters", {})
        case["parameterUnits"] = parsed_parameter_set.get("parameterUnits", {})

    stimuli_element = children.get("Stimuli")
    if stimuli_element is not None:
        stimuli_type = str(stimuli_element.attrib.get("type") or "text/csv").split(";", 1)[0].strip().lower()
        if stimuli_type != "text/csv":
            case["status"] = "unsupported"
            case["reason"] = "unsupported_stimuli_file_type"
            return case
        stimuli_source = str(stimuli_element.attrib.get("source") or "").strip()
        if not stimuli_source:
            case["reason"] = "stimuli_file_source_missing"
            return case
        stimuli_path = _resolve_archive_uri(experiment_archive_path, stimuli_source)
        if stimuli_path is None:
            case["status"] = "unsupported"
            case["reason"] = "external_or_unsafe_stimuli_resource"
            return case
        if resource_name_counts.get(stimuli_path) != 1:
            case["status"] = "unavailable"
            case["reason"] = "stimuli_file_missing_or_ambiguous"
            return case
        stimuli_data = _read_archive_file(archive, stimuli_path, MAX_STIMULI_FILE_BYTES)
        if stimuli_data is None:
            case["status"] = "unavailable"
            case["reason"] = "stimuli_file_unavailable_or_too_large"
            return case
        try:
            case["inputSchedule"] = _parse_stimuli_csv(stimuli_data, case["options"])
        except (UnicodeDecodeError, csv.Error, ValueError):
            case["reason"] = "invalid_stimuli_csv"
            return case

    case["status"] = "ready"
    case["reason"] = ""
    return case


def _parse_stimuli_csv(data: bytes, options: dict[str, Any]) -> list[dict[str, Any]]:
    """Read a bounded time/value CSV as ordered, step-aligned input changes."""
    time_options: list[float] = []
    for name in ("startTime", "stopTime", "stepSize"):
        value = options.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("stimuli require finite startTime, stopTime and stepSize")
        time_options.append(float(value))
    start, stop, step = time_options
    if step <= 0 or stop <= start:
        raise ValueError("invalid stimuli time range")

    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows = iter(reader)
    header = next(rows, None)
    if not header or len(header) < 2:
        raise ValueError("stimuli CSV needs time and at least one input column")
    names = [name.strip() for name in header]
    if names[0] != "time" or len(set(names)) != len(names):
        raise ValueError("stimuli CSV must begin with a unique time column")
    if any(not name or len(name) > 512 or any(ord(char) < 32 for char in name) for name in names[1:]):
        raise ValueError("invalid stimuli input name")

    schedule: list[dict[str, Any]] = []
    previous_time: float | None = None
    tolerance = max(1e-9, abs(step) * 1e-7)
    for row in rows:
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != len(names) or len(schedule) >= MAX_STIMULI_EVENTS:
            raise ValueError("stimuli CSV row width or event count is invalid")
        values = [float(cell.strip()) for cell in row]
        if any(not math.isfinite(value) for value in values):
            raise ValueError("stimuli CSV contains a non-finite value")
        time = values[0]
        if time < start or time >= stop or (previous_time is not None and time <= previous_time):
            raise ValueError("stimuli times must be strictly increasing within the experiment")
        step_index = round((time - start) / step)
        if abs(start + step_index * step - time) > tolerance:
            raise ValueError("stimuli times must align to stepSize")
        schedule.append({"time": time, "values": dict(zip(names[1:], values[1:]))})
        previous_time = time

    if not schedule or abs(schedule[0]["time"] - start) > tolerance:
        raise ValueError("stimuli CSV must define inputs at startTime")
    return schedule


def _resolve_archive_uri(base_archive_path: str, source: str) -> str | None:
    if not source or len(source) > 2048 or "\\" in source or "\x00" in source:
        return None
    parsed = urlsplit(source)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path:
        return None
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or "\x00" in decoded_path or decoded_path.startswith("/"):
        return None
    normalized = posixpath.normpath(posixpath.join(posixpath.dirname(base_archive_path), decoded_path))
    if normalized in {"", ".", ".."} or normalized.startswith(("../", "/")):
        return None
    return normalized


def _read_archive_file(archive: zipfile.ZipFile, path: str, maximum_bytes: int) -> bytes | None:
    try:
        info = archive.getinfo(path)
        if info.is_dir() or info.file_size > maximum_bytes:
            return None
        with archive.open(info) as source:
            content = source.read(maximum_bytes + 1)
        return content if len(content) <= maximum_bytes else None
    except (OSError, KeyError, RuntimeError, EOFError, zlib.error):
        return None


def _safe_xml(data: bytes) -> bool:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    lowered = text.lower()
    return "<!doctype" not in lowered and "<!entity" not in lowered


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _namespace(element: ET.Element) -> str:
    return element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else ""
