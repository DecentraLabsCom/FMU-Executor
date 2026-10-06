"""FMU storage: catalog, discovery, validation and quarantine of provisioned .fmu files."""

from __future__ import annotations

import base64
import logging
import re
import shutil
import time as _time
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
    """Validate an FMU is a parseable Co-Simulation archive.

    Returns ``(True, "")`` on success or ``(False, reason)`` on failure.
    Does NOT auto-quarantine — the caller decides.
    """
    path = _resolve_fmu_path(access_key)
    if path is None:
        return False, "FMU file not found"
    try:
        md = fmpy.read_model_description(str(path))
    except Exception:
        logger.exception("Cannot parse FMU model description for %s", access_key)
        return False, "Cannot parse model description"
    if md.coSimulation is None:
        return False, "FMU does not support Co-Simulation"
    if not md.guid:
        return False, "FMU has no GUID"
    return True, ""


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


def describe(access_key: str) -> dict[str, Any]:
    """Parse the model description and return a normalised dict compatible with the Gateway contract."""
    fmu_path = str(get_fmu_path(access_key))
    md = fmpy.read_model_description(fmu_path)

    variables: list[dict[str, Any]] = []
    for var in md.modelVariables:
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
                if dimension_entry:
                    dimension_entries.append(dimension_entry)
            if dimension_entries:
                entry["dimensions"] = dimension_entries
        if var.start is not None:
            if var.type == "Binary" and isinstance(var.start, (bytes, bytearray)):
                entry["start"] = base64.b64encode(bytes(var.start)).decode("ascii")
            else:
                entry["start"] = var.start
        if var.unit:
            entry["unit"] = var.unit
        if var.min is not None:
            entry["min"] = var.min
        if var.max is not None:
            entry["max"] = var.max
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

    return result
