"""FMU storage: catalog, discovery, validation and quarantine of provisioned .fmu files."""

from __future__ import annotations

import base64
import math
import hashlib
import logging
import os
import platform
import re
import shutil
import stat
import tempfile
import time as _time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import fmpy

from . import config

logger = logging.getLogger(__name__)

# ── quarantine state ─────────────────────────────────────────────

# In-memory quarantine index: accessKey → {reason, timestamp}.
# Written to disk as `<FMU_ROOT>/.quarantine/<accessKey>.reason`.
_quarantine_cache: dict[str, dict[str, Any]] = {}

_ACCESS_KEY_RE = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,127}/)*"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:\.fmu)?"
)


def validate_access_key(access_key: str) -> str:
    """Validate and normalize an FMU access key.

    Access keys may identify a direct ``.fmu`` file or a provisioned
    subdirectory containing one FMU.  They are deliberately restricted to
    safe path components so every storage operation can share one boundary.
    """
    value = str(access_key).strip()
    if _ACCESS_KEY_RE.fullmatch(value) is None:
        raise ValueError("Invalid FMU access key")
    return value


def _quarantine_dir() -> Path:
    return config.FMU_ROOT.resolve() / ".quarantine"


def _iter_storage_entries(root: Path):
    """Yield resolved entries that are physically contained by *root*.

    The filesystem is enumerated from the trusted root first.  User input is
    only compared with the resulting relative keys and is never used to build
    a filesystem path.
    """
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        return
    for entry in resolved_root.rglob("*"):
        relative = entry.relative_to(resolved_root)
        if ".quarantine" in relative.parts:
            continue
        try:
            resolved_entry = entry.resolve()
            resolved_entry.relative_to(resolved_root)
        except ValueError:
            logger.warning("Storage symlink outside root blocked: %s", entry)
            continue
        yield resolved_entry


def _single_fmu(directory: Path, root: Path) -> Path | None:
    """Return the only direct FMU in *directory*, if there is exactly one."""
    resolved_root = root.resolve()
    fmus: list[Path] = []
    for entry in directory.iterdir():
        if entry.is_file() and entry.suffix == ".fmu":
            try:
                resolved_entry = entry.resolve()
                resolved_entry.relative_to(resolved_root)
            except ValueError:
                logger.warning("FMU symlink outside root blocked: %s", entry)
                continue
            fmus.append(resolved_entry)
    return fmus[0] if len(fmus) == 1 else None


def _find_storage_entry(root: Path, access_key: str) -> Path | None:
    """Find a provisioned file or single-FMU directory by its access key."""
    resolved_root = root.resolve()
    for entry in _iter_storage_entries(resolved_root) or ():
        relative_key = entry.relative_to(resolved_root).as_posix()
        if relative_key != access_key:
            continue
        if entry.is_file() and entry.suffix == ".fmu":
            return entry
        if entry.is_dir() and _single_fmu(entry, resolved_root) is not None:
            return entry
    return None


def _load_quarantine_cache() -> None:
    """Load quarantine entries from disk into memory (idempotent)."""
    q = _quarantine_dir()
    if not q.is_dir():
        return
    for entry in q.rglob("*.reason"):
        key = entry.relative_to(q).with_suffix("").as_posix()
        if key not in _quarantine_cache:
            try:
                reason = entry.read_text(encoding="utf-8").strip()
            except OSError:
                reason = "unknown"
            _quarantine_cache[key] = {
                "reason": reason,
                "timestamp": entry.stat().st_mtime,
            }


def quarantine(access_key: str, reason: str) -> None:
    """Move an FMU to quarantine so it is excluded from the active catalog."""
    access_key = validate_access_key(access_key)
    q = _quarantine_dir()
    q.mkdir(parents=True, exist_ok=True)

    candidate = _find_storage_entry(config.FMU_ROOT, access_key)
    if candidate is not None:
        relative = candidate.relative_to(config.FMU_ROOT.resolve())
        quarantined_target = q.joinpath(*relative.parts)
        reason_file = quarantined_target.with_name(quarantined_target.name + ".reason")
        reason_file.parent.mkdir(parents=True, exist_ok=True)
        reason_file.write_text(reason, encoding="utf-8")
        if not quarantined_target.exists():
            shutil.move(str(candidate), str(quarantined_target))

    _quarantine_cache[access_key] = {
        "reason": reason,
        "timestamp": _time.time(),
    }
    logger.warning("Quarantined FMU %s: %s", access_key, reason)


def unquarantine(access_key: str) -> bool:
    """Restore a quarantined FMU back to the active catalog.  Returns True on success."""
    access_key = validate_access_key(access_key)
    q = _quarantine_dir()
    quarantined = _find_storage_entry(q, access_key)

    restored = False
    if quarantined is not None:
        relative = quarantined.relative_to(q.resolve())
        target = config.FMU_ROOT.resolve().joinpath(*relative.parts)
        reason_file = quarantined.with_name(quarantined.name + ".reason")
        if not target.exists():
            shutil.move(str(quarantined), str(target))
            restored = True
        if reason_file.exists():
            reason_file.unlink()
    _quarantine_cache.pop(access_key, None)
    if restored:
        logger.info("Restored FMU %s from quarantine", access_key)
    return restored


def is_quarantined(access_key: str) -> bool:
    try:
        access_key = validate_access_key(access_key)
    except ValueError:
        return False
    _load_quarantine_cache()
    return access_key in _quarantine_cache


def quarantine_info(access_key: str) -> dict[str, Any] | None:
    try:
        access_key = validate_access_key(access_key)
    except ValueError:
        return None
    _load_quarantine_cache()
    return _quarantine_cache.get(access_key)


def list_quarantined() -> list[dict[str, Any]]:
    _load_quarantine_cache()
    return [
        {"accessKey": k, **v}
        for k, v in sorted(_quarantine_cache.items())
    ]


# ── validation ───────────────────────────────────────────────────

def validate_fmu(access_key: str) -> tuple[bool, str]:
    """Validate an FMU archive, engine capability, and host binary availability.

    Returns ``(True, "")`` on success or ``(False, reason)`` on failure.
    Does NOT auto-quarantine — the caller decides.
    """
    report = inspect_fmu(access_key)
    if report["archiveValid"] and report["stationCompatible"]:
        return True, ""
    return False, "; ".join(report["issues"][:5]) or "FMU is not executable on this Station"


def _host_binary_directories() -> list[str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        if machine in {"amd64", "x86_64", "x64"}:
            return ["win64", "x86_64-windows"]
        if machine in {"arm64", "aarch64"}:
            return ["aarch64-windows"]
        if machine in {"i386", "i686", "x86"}:
            return ["win32", "x86-windows"]
    elif system == "linux":
        if machine in {"x86_64", "amd64"}:
            return ["linux64", "x86_64-linux"]
        if machine in {"aarch64", "arm64"}:
            return ["aarch64-linux"]
    elif system == "darwin":
        if machine in {"x86_64", "amd64"}:
            return ["darwin64", "x86_64-darwin"]
        if machine in {"arm64", "aarch64"}:
            return ["darwinarm64", "aarch64-darwin"]
    return []


def _archive_report(path: Path) -> tuple[bool, list[str], str | None, int | None, int | None, int | None]:
    issues: list[str] = []
    digest = hashlib.sha256()
    compressed_size = expanded_size = entry_count = None
    try:
        compressed_size = path.stat().st_size
        if compressed_size > config.MAX_FMU_ARCHIVE_BYTES:
            return False, ["FMU archive exceeds the compressed size limit"], None, compressed_size, None, None
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            entry_count = len(entries)
            if entry_count > config.MAX_FMU_ARCHIVE_ENTRIES:
                return False, ["FMU archive contains too many entries"], digest.hexdigest(), compressed_size, 0, entry_count
            expanded_size = 0
            seen_names: set[str] = set()
            for entry in entries:
                name = entry.filename.replace("\\", "/")
                if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
                    issues.append("FMU archive contains a path outside its extraction root")
                    break
                normalized_name = name.rstrip("/")
                if os.name == "nt":
                    normalized_name = normalized_name.casefold()
                if normalized_name in seen_names:
                    issues.append("FMU archive contains duplicate paths")
                    break
                seen_names.add(normalized_name)
                if entry.flag_bits & 0x1:
                    issues.append("FMU archive contains encrypted entries")
                    break
                expanded_size += entry.file_size
                if expanded_size > config.MAX_FMU_EXPANDED_BYTES:
                    issues.append("FMU archive exceeds the expanded size limit")
                    break
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False, ["FMU is not a readable ZIP archive"], None, compressed_size, expanded_size, entry_count
    return not issues, issues, digest.hexdigest(), compressed_size, expanded_size, entry_count


def inspect_fmu(access_key: str, *, include_hash: bool = True) -> dict[str, Any]:
    """Report archive validity separately from host compatibility and probing."""
    path = _resolve_fmu_path(access_key)
    return inspect_fmu_path(path, access_key=access_key, include_hash=include_hash)


def inspect_fmu_path(path: Path | None, *, access_key: str, include_hash: bool = True) -> dict[str, Any]:
    """Inspect a specific FMU file, including immutable execution snapshots."""
    host_dirs = _host_binary_directories()
    report: dict[str, Any] = {
        "accessKey": access_key,
        "archiveValid": False,
        "modelDescriptionValid": False,
        "stationCompatible": False,
        "executionChecked": False,
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "compatibleBinaryDirectories": host_dirs,
        "nativeBinaryPresent": False,
        "dependenciesChecked": False,
        "limits": {
            "maxCompressedBytes": config.MAX_FMU_ARCHIVE_BYTES,
            "maxExpandedBytes": config.MAX_FMU_EXPANDED_BYTES,
            "maxEntries": config.MAX_FMU_ARCHIVE_ENTRIES,
            "maxModelDescriptionBytes": config.MAX_FMU_MODEL_DESCRIPTION_BYTES,
        },
        "issues": [],
    }
    if path is None:
        report["issues"] = ["FMU file not found"]
        return report

    archive_valid, archive_issues, digest, compressed_size, expanded_size, entry_count = _archive_report(path)
    report["archiveValid"] = archive_valid
    report["issues"].extend(archive_issues)
    report["compressedSizeBytes"] = compressed_size
    report["expandedSizeBytes"] = expanded_size
    report["archiveEntryCount"] = entry_count
    if include_hash and digest:
        report["sha256"] = digest
    if not archive_valid:
        return report

    try:
        with zipfile.ZipFile(path) as archive:
            model_description = archive.getinfo("modelDescription.xml")
            report["modelDescriptionSizeBytes"] = model_description.file_size
            if model_description.file_size > config.MAX_FMU_MODEL_DESCRIPTION_BYTES:
                report["issues"].append("FMU model description exceeds the size limit")
                return report
    except KeyError:
        report["issues"].append("FMU has no modelDescription.xml")
        return report
    except (OSError, zipfile.BadZipFile):
        report["issues"].append("FMU model description is not readable")
        return report

    try:
        description = fmpy.read_model_description(str(path))
    except Exception:
        logger.exception("Cannot parse FMU model description for %s", access_key)
        report["issues"].append("Cannot parse model description")
        return report

    fmi_version = str(description.fmiVersion or "")
    report["fmiVersion"] = fmi_version
    report["guid"] = str(description.guid or "")
    report["modelDescriptionValid"] = True
    report["capabilities"] = {
        "coSimulation": description.coSimulation is not None,
        "modelExchange": description.modelExchange is not None,
        "scheduledExecution": getattr(description, "scheduledExecution", None) is not None,
    }
    if fmi_version.split(".", 1)[0] not in {"2", "3"}:
        report["issues"].append("Executor supports FMI 2.0 and FMI 3.0 only")
    if description.coSimulation is None:
        report["issues"].append("FMU does not provide Co-Simulation")
    if not description.guid:
        report["issues"].append("FMU has no GUID")

    model_identifier = str(getattr(description.coSimulation, "modelIdentifier", "") or "")
    report["modelIdentifier"] = model_identifier
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", model_identifier):
        report["issues"].append("FMU Co-Simulation modelIdentifier is missing or invalid")

    extensions = {
        "windows": (".dll",),
        "linux": (".so",),
        "darwin": (".dylib", ".so"),
    }.get(platform.system().lower(), ())
    expected_binary_names = {
        f"binaries/{directory}/{model_identifier}{extension}"
        for directory in host_dirs
        for extension in extensions
    } if model_identifier else set()
    try:
        with zipfile.ZipFile(path) as archive:
            names = {entry.filename.replace("\\", "/") for entry in archive.infolist()}
        matched = expected_binary_names.intersection(names)
        if os.name == "nt" and not matched:
            lower_names = {name.lower(): name for name in names}
            matched = {lower_names[name.lower()] for name in expected_binary_names if name.lower() in lower_names}
        found_binaries = sorted(matched)
        present = bool(found_binaries)
    except (OSError, zipfile.BadZipFile):
        present = False
        found_binaries = []
    report["nativeBinaryPresent"] = present
    report["nativeBinaryEntries"] = found_binaries
    if description.coSimulation is not None and host_dirs and not present:
        report["issues"].append("FMU has no Co-Simulation binary for this Station platform")
    if not host_dirs:
        report["issues"].append("Station operating system or architecture is unsupported")
    report["stationCompatible"] = not report["issues"]
    return report


def snapshot_fmu(path: Path) -> tuple[Path, str]:
    """Copy an FMU into immutable, content-addressed state for a whole job."""
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("FMU snapshot source must be a file")
    if path.stat().st_size > config.MAX_FMU_ARCHIVE_BYTES:
        raise ValueError("FMU archive exceeds the compressed size limit")
    snapshot_dir = config.STATE_DIR / "fmu-snapshots"
    snapshot_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(snapshot_dir, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=snapshot_dir)
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as destination, path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                if destination.tell() + len(chunk) > config.MAX_FMU_ARCHIVE_BYTES:
                    raise ValueError("FMU archive exceeds the compressed size limit")
                digest.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        sha256 = digest.hexdigest()
        snapshot_path = snapshot_dir / f"{sha256}.fmu"
        if snapshot_path.exists():
            existing_digest = hashlib.sha256()
            with snapshot_path.open("rb") as existing:
                for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                    existing_digest.update(chunk)
            if existing_digest.hexdigest() == sha256:
                os.unlink(temporary_name)
            else:
                try:
                    snapshot_path.chmod(stat.S_IWUSR | stat.S_IRUSR)
                except OSError:
                    pass
                os.replace(temporary_name, snapshot_path)
        else:
            os.replace(temporary_name, snapshot_path)
        try:
            snapshot_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            logger.warning("Could not mark FMU snapshot read-only: %s", snapshot_path)
        return snapshot_path, sha256
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _resolve_fmu_path(access_key: str) -> Path | None:
    """Return the absolute path to the .fmu file for *access_key*, or None.

    Quarantined FMUs are excluded.
    """
    try:
        access_key = validate_access_key(access_key)
    except ValueError:
        return None
    if is_quarantined(access_key):
        return None
    candidate = _find_storage_entry(config.FMU_ROOT, access_key)
    if candidate is None:
        return None
    if candidate.is_file():
        return candidate
    return _single_fmu(candidate, config.FMU_ROOT)


def list_fmus() -> list[str]:
    """Return all available access keys."""
    root = config.FMU_ROOT
    if not root.is_dir():
        return []
    keys: list[str] = []
    for child in sorted(root.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_file() and child.suffix == ".fmu":
            keys.append(child.name)
        elif child.is_dir():
            if any(child.glob("*.fmu")):
                keys.append(child.name)
    return keys


def fmu_exists(access_key: str) -> bool:
    return _resolve_fmu_path(access_key) is not None


def get_fmu_path(access_key: str) -> Path:
    """Return the path or raise FileNotFoundError."""
    path = _resolve_fmu_path(access_key)
    if path is None:
        raise FileNotFoundError(f"FMU not found for accessKey={access_key!r}")
    return path


def describe(
    access_key: str,
    *,
    public_only: bool = False,
    causality: str | None = None,
) -> dict[str, Any]:
    """Parse the model description and return a normalised dict compatible with the Gateway contract."""
    return describe_path(
        get_fmu_path(access_key),
        public_only=public_only,
        causality=causality,
    )


def describe_path(
    path: Path,
    *,
    public_only: bool = False,
    causality: str | None = None,
) -> dict[str, Any]:
    """Describe a specific immutable FMU file."""
    md = fmpy.read_model_description(str(path))
    return describe_model_description(
        md,
        public_only=public_only,
        causality=causality,
        display_unit_inverses=_display_unit_inverse_map(path),
    )


def _display_unit_inverse_map(path: Path) -> dict[tuple[str, str], bool] | None:
    """Read FMI 3 inverse display units that the installed FMPy parser may omit."""
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("modelDescription.xml")
            if info.file_size > config.MAX_FMU_MODEL_DESCRIPTION_BYTES:
                return None
            with archive.open(info) as source:
                root = ET.parse(source).getroot()
    except (OSError, KeyError, RuntimeError, ET.ParseError, zipfile.BadZipFile):
        return None

    def children_named(element: ET.Element, name: str):
        return (child for child in element if child.tag.rsplit("}", 1)[-1] == name)

    inverse_units: dict[tuple[str, str], bool] = {}
    unit_definitions = next(children_named(root, "UnitDefinitions"), None)
    if unit_definitions is None:
        return inverse_units
    for unit in children_named(unit_definitions, "Unit"):
        unit_name = unit.get("name")
        if not unit_name:
            continue
        for display in children_named(unit, "DisplayUnit"):
            display_name = display.get("name")
            inverse = str(display.get("inverse", "false")).strip().lower() in {"true", "1"}
            if display_name and inverse:
                inverse_units[(unit_name, display_name)] = True
    return inverse_units


def describe_model_description(
    md: Any,
    *,
    public_only: bool = False,
    causality: str | None = None,
    display_unit_inverses: dict[tuple[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Normalize a parsed FMI description without reopening the FMU archive."""

    def finite_number(value: Any, *, integer: bool = False) -> int | float | None:
        if value is None:
            return None
        try:
            parsed = int(value) if integer else float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if isinstance(parsed, float) and not math.isfinite(parsed):
            return None
        return parsed

    def display_unit_for(var: Any, declared_type: Any) -> dict[str, Any] | None:
        unit_name = getattr(var, "unit", None) or getattr(declared_type, "unit", None)
        display_name = getattr(var, "displayUnit", None) or getattr(declared_type, "displayUnit", None)
        if not isinstance(unit_name, str) or not unit_name or not isinstance(display_name, str) or not display_name:
            return None

        unit = next(
            (item for item in (getattr(md, "unitDefinitions", None) or []) if getattr(item, "name", None) == unit_name),
            None,
        )
        display = next(
            (item for item in (getattr(unit, "displayUnits", None) or []) if getattr(item, "name", None) == display_name),
            None,
        )
        if display is None:
            return None

        try:
            factor = float(getattr(display, "factor", 1.0))
            offset = float(getattr(display, "offset", 0.0))
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(factor) or factor == 0 or not math.isfinite(offset):
            return None

        fmi_major_version = str(getattr(md, "fmiVersion", "")).split(".", 1)[0]
        raw_inverse = getattr(display, "inverse", None)
        if raw_inverse is None:
            if fmi_major_version == "3" and display_unit_inverses is None:
                return None
            inverse = bool(display_unit_inverses and display_unit_inverses.get((unit_name, display_name), False))
        else:
            inverse = raw_inverse is True or str(raw_inverse).strip().lower() in {"true", "1"}
        if inverse and (fmi_major_version == "1" or offset != 0):
            return None

        # The public metadata uses one consistent equation for every FMI
        # version: display = factor * model_value + offset.
        if str(getattr(md, "fmiVersion", "")).split(".", 1)[0] == "1":
            factor, offset = 1.0 / factor, -offset / factor
        variable_relative = getattr(var, "relativeQuantity", None)
        type_relative = getattr(declared_type, "relativeQuantity", None)
        if (
            variable_relative is True
            or type_relative is True
            or str(variable_relative).strip().lower() in {"true", "1"}
            or str(type_relative).strip().lower() in {"true", "1"}
        ):
            offset = 0.0
        result: dict[str, Any] = {"name": display_name, "factor": factor, "offset": offset}
        if inverse:
            result["inverse"] = True
        return result

    integer_types = {
        "Integer", "Enumeration", "Int8", "UInt8", "Int16", "UInt16",
        "Int32", "UInt32", "Int64", "UInt64",
    }

    variables: list[dict[str, Any]] = []
    for var in md.modelVariables:
        # Local variables are internal state, not part of the FMI interface.
        # FMI's independent variable (usually time) and future causality values
        # remain available to callers of the reduced description.
        if public_only and var.causality == "local":
            continue
        if causality is not None and var.causality != causality:
            continue
        declared_type = getattr(var, "declaredType", None)
        entry: dict[str, Any] = {
            "name": var.name,
            "valueReference": var.valueReference,
            "causality": var.causality,
            "variability": var.variability,
        }
        if var.type:
            entry["type"] = var.type
        dimensions = getattr(var, "dimensions", None)
        if isinstance(dimensions, (list, tuple)) and dimensions:
            dimension_entries = []
            for dimension in dimensions:
                dimension_entry: dict[str, Any] = {}
                start = getattr(dimension, "start", None)
                if start is not None:
                    dimension_entry["start"] = start
                value_reference = getattr(dimension, "valueReference", None)
                if value_reference is not None:
                    dimension_entry["valueReference"] = int(value_reference)
                dimension_variable = getattr(dimension, "variable", None)
                dimension_variable_name = getattr(dimension_variable, "name", None)
                if isinstance(dimension_variable_name, str) and dimension_variable_name:
                    dimension_entry["variableName"] = dimension_variable_name[:256]
                if dimension_entry:
                    dimension_entries.append(dimension_entry)
            if dimension_entries:
                entry["dimensions"] = dimension_entries
        if var.start is not None:
            if var.type == "Binary" and isinstance(var.start, (bytes, bytearray)):
                entry["start"] = base64.b64encode(bytes(var.start)).decode("ascii")
            else:
                entry["start"] = var.start
        initial = getattr(var, "initial", None)
        if initial is not None:
            entry["initial"] = str(initial)
        nominal = finite_number(
            getattr(var, "nominal", None),
            integer=var.type in integer_types,
        )
        if nominal is not None:
            entry["nominal"] = nominal
        unit = getattr(var, "unit", None) or getattr(declared_type, "unit", None)
        if unit:
            entry["unit"] = unit
        quantity = getattr(var, "quantity", None) or getattr(declared_type, "quantity", None)
        if quantity:
            entry["quantity"] = str(quantity)[:256]
        display_name = getattr(var, "displayName", None)
        if isinstance(display_name, str) and display_name.strip():
            entry["displayName"] = display_name.strip()[:256]
        description = getattr(var, "description", None)
        if isinstance(description, str) and description.strip():
            entry["description"] = description.strip()[:4096]
        minimum = getattr(var, "min", None)
        if minimum is None:
            minimum = getattr(declared_type, "min", None)
        maximum = getattr(var, "max", None)
        if maximum is None:
            maximum = getattr(declared_type, "max", None)
        normalized_min = finite_number(minimum, integer=var.type in integer_types)
        normalized_max = finite_number(maximum, integer=var.type in integer_types)
        if normalized_min is not None:
            entry["min"] = normalized_min
        if normalized_max is not None:
            entry["max"] = normalized_max
        display_unit = display_unit_for(var, declared_type)
        if display_unit is not None:
            entry["displayUnit"] = display_unit
        if var.type == "Enumeration" and declared_type is not None:
            items = []
            for item in getattr(declared_type, "items", []) or []:
                value = finite_number(getattr(item, "value", None), integer=True)
                name = getattr(item, "name", None)
                if value is None or not isinstance(name, str) or not name.strip():
                    continue
                option = {"value": value, "name": name.strip()[:256]}
                item_description = getattr(item, "description", None)
                if isinstance(item_description, str) and item_description.strip():
                    option["description"] = item_description.strip()[:1024]
                items.append(option)
            if items and len(items) <= 256:
                entry["enumItems"] = items
        if declared_type is not None:
            declared_type_entry: dict[str, Any] = {}
            for field in ("name", "type", "description", "unit", "quantity", "displayUnit"):
                value = getattr(declared_type, field, None)
                if value is not None and value != "":
                    declared_type_entry[field] = str(value)[:4096]
            declared_minimum = getattr(declared_type, "min", None)
            declared_maximum = getattr(declared_type, "max", None)
            if declared_minimum is not None:
                declared_type_entry["min"] = finite_number(
                    declared_minimum,
                    integer=var.type in integer_types,
                )
            if declared_maximum is not None:
                declared_type_entry["max"] = finite_number(
                    declared_maximum,
                    integer=var.type in integer_types,
                )
            declared_items = []
            for item in getattr(declared_type, "items", []) or []:
                value = finite_number(getattr(item, "value", None), integer=True)
                name = getattr(item, "name", None)
                if value is None or not isinstance(name, str) or not name.strip():
                    continue
                item_entry = {"value": value, "name": name.strip()[:256]}
                item_description = getattr(item, "description", None)
                if isinstance(item_description, str) and item_description.strip():
                    item_entry["description"] = item_description.strip()[:1024]
                declared_items.append(item_entry)
            if declared_items and len(declared_items) <= 256:
                declared_type_entry["items"] = declared_items
            if declared_type_entry:
                entry["declaredType"] = declared_type_entry
        variables.append(entry)

    cs = md.coSimulation
    me = md.modelExchange

    result: dict[str, Any] = {
        "modelName": md.modelName,
        "guid": md.guid,
        "fmiVersion": md.fmiVersion,
        "supportsCoSimulation": cs is not None,
        "supportsModelExchange": me is not None,
        "modelVariables": variables,
    }

    for field, maximum_length in (
        ("description", 4096),
        ("author", 256),
        ("version", 128),
        ("license", 256),
        ("generationTool", 256),
    ):
        value = getattr(md, field, None)
        if isinstance(value, str) and value.strip():
            result[field] = value.strip()[:maximum_length]

    capabilities: dict[str, Any] = {}
    interface = cs or me
    if interface is not None:
        for attribute in (
            "canGetAndSetFMUstate",
            "canSerializeFMUstate",
            "canHandleVariableCommunicationStepSize",
            "providesDirectionalDerivative",
            "providesAdjointDerivatives",
        ):
            value = getattr(interface, attribute, None)
            if value is not None:
                capabilities[attribute] = bool(value)
        fixed_step = finite_number(getattr(interface, "fixedInternalStepSize", None))
        if fixed_step is not None:
            capabilities["fixedInternalStepSize"] = fixed_step
    if capabilities:
        result["capabilities"] = capabilities

    if cs is not None:
        result["fmiType"] = "CoSimulation"
        result["simulationKind"] = "coSimulation"
        result["simulationType"] = "CoSimulation"
    elif me is not None:
        result["fmiType"] = "ModelExchange"
        result["simulationKind"] = "modelExchange"
        result["simulationType"] = "ModelExchange"

    exp = md.defaultExperiment
    if exp:
        if exp.startTime is not None:
            result["defaultStartTime"] = float(exp.startTime)
        if exp.stopTime is not None:
            result["defaultStopTime"] = float(exp.stopTime)
        if exp.stepSize is not None:
            result["defaultStepSize"] = float(exp.stepSize)
        if exp.tolerance is not None:
            result["defaultTolerance"] = float(exp.tolerance)

    return result
