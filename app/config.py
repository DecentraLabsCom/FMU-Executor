"""FMU Executor configuration loaded from environment variables."""

from __future__ import annotations

import os
import base64
from pathlib import Path


def _env(key: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(key, default)
    if required and not value:
        raise RuntimeError(f"Required env var {key} is not set")
    return value


# Network
def bind_host() -> str:
    return _env("FMU_EXECUTOR_HOST", "0.0.0.0") or "0.0.0.0"


def bind_port() -> int:
    return int(_env("FMU_EXECUTOR_PORT", "8091") or "8091")

# FMU storage root – each sub-folder or .fmu file is keyed by accessKey
FMU_ROOT: Path = Path(_env("FMU_ROOT", str(Path(__file__).resolve().parent.parent / "fmu-data")) or "")
STATE_DIR: Path = Path(_env("FMU_EXECUTOR_STATE_DIR", str(FMU_ROOT.parent / "state")) or "")

# Internal auth token shared with Gateway's fmu-runner
def internal_token() -> str | None:
    encoded = os.environ.get("FMU_INTERNAL_TOKEN_B64")
    if encoded:
        try:
            padded = encoded + ("=" * (-len(encoded) % 4))
            return base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    token_file = os.environ.get("FMU_INTERNAL_TOKEN_FILE")
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return _env("FMU_INTERNAL_TOKEN")

# Temp directory for FMU extraction during execution
TEMP_DIR: Path = Path(_env("FMU_EXECUTOR_TEMP", str(FMU_ROOT / ".tmp")) or "")

# All execution modes use spawned workers by default; in-process is a diagnostic
# escape hatch for environments that provide their own isolation.
def execution_mode() -> str:
    mode = (_env("FMU_EXECUTION_MODE", "process") or "process").strip().lower()
    return mode if mode in {"process", "in-process"} else "process"


def execution_timeout_seconds() -> float:
    return max(1.0, float(_env("FMU_EXECUTION_TIMEOUT_SECONDS", "3600") or "3600"))


def _bounded_int(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_env(key, str(default)) or str(default))
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _optional_bounded_int(key: str, maximum: int, *, minimum: int = 1) -> int | None:
    """Read an optional quota: blank, zero, or 'unlimited' disables the cap."""
    raw = os.environ.get(key)
    if raw is None or not raw.strip() or raw.strip().lower() in {"unlimited", "none", "infinity"}:
        return None
    if raw.strip() == "0":
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{key} must be a positive integer, zero, or 'unlimited'") from exc
    if value < 0:
        raise RuntimeError(f"{key} must not be negative")
    if value == 0:
        return None
    return min(maximum, max(minimum, value))


MAX_BATCH_CASES: int = _bounded_int("FMU_MAX_BATCH_CASES", 8, 1, 20)
MAX_SCENARIOS_PER_RESERVATION_PER_DAY: int | None = _optional_bounded_int(
    "FMU_MAX_SCENARIOS_PER_RESERVATION_PER_DAY", 10000
)
MAX_INPUT_UPDATE_BYTES: int = _bounded_int("FMU_MAX_INPUT_UPDATE_BYTES", 65536, 1024, 1048576)
MAX_INPUT_UPDATES_PER_DAY: int | None = _optional_bounded_int(
    "FMU_MAX_INPUT_UPDATES_PER_DAY", 1000000
)
MAX_INPUT_UPDATE_BYTES_PER_DAY: int | None = _optional_bounded_int(
    "FMU_MAX_INPUT_UPDATE_BYTES_PER_DAY", 2 * 1024 * 1024 * 1024, minimum=65536
)
PLATFORM_MAX_INPUT_UPDATES_PER_WINDOW: int = 10000
MAX_INPUT_UPDATES_PER_WINDOW: int | None = _optional_bounded_int(
    "FMU_MAX_INPUT_UPDATES_PER_WINDOW", PLATFORM_MAX_INPUT_UPDATES_PER_WINDOW
)
INPUT_UPDATE_WINDOW_SECONDS: int = _bounded_int("FMU_INPUT_UPDATE_WINDOW_SECONDS", 10, 1, 3600)
MAX_OUTPUT_SUBSCRIPTION_HZ: int = _bounded_int("FMU_MAX_OUTPUT_SUBSCRIPTION_HZ", 60, 1, 1000)
MAX_OUTPUT_SUBSCRIPTION_BATCH: int = _bounded_int("FMU_MAX_OUTPUT_SUBSCRIPTION_BATCH", 256, 1, 4096)
FMU_PROGRESS_INTERVAL_MS: int = _bounded_int("FMU_PROGRESS_INTERVAL_MS", 50, 10, 5000)
MAX_SIMULATION_STEPS: int = _bounded_int("FMU_MAX_SIMULATION_STEPS", 10000, 100, 1000000)
MAX_INPUT_SCHEDULE_EVENTS: int = _bounded_int("FMU_MAX_INPUT_SCHEDULE_EVENTS", 128, 1, 200000)
MAX_INPUT_SCHEDULE_BYTES: int = _bounded_int(
    "FMU_MAX_INPUT_SCHEDULE_BYTES", 16 * 1024, 1024, 16 * 1024 * 1024
)
MAX_SIMULATION_PARAMETERS: int = _bounded_int("FMU_MAX_SIMULATION_PARAMETERS", 32, 1, 256)
NATIVE_THREAD_LIMIT_ACCESS_KEYS: frozenset[str] = frozenset(
    value.strip()
    for value in (_env("FMU_NATIVE_THREAD_LIMIT_ACCESS_KEYS", "") or "").split(",")
    if value.strip()
)
MAX_OUTPUT_SAMPLE_EVERY_STEPS: int = _bounded_int("FMU_MAX_OUTPUT_SAMPLE_EVERY_STEPS", 10000, 1, 100000)
HISTORY_RETENTION_DAYS: int = _bounded_int("FMU_HISTORY_RETENTION_DAYS", 7, 1, 30)
MAX_STORED_HISTORY_RECORDS: int = _bounded_int("FMU_MAX_HISTORY_RECORDS", 10000, 100, 100000)
MAX_STORED_RESULT_BYTES: int = _bounded_int("FMU_MAX_RESULT_BYTES", 8 * 1024 * 1024, 65536, 32 * 1024 * 1024)
MAX_TOTAL_HISTORY_BYTES: int = _bounded_int("FMU_MAX_HISTORY_BYTES", 256 * 1024 * 1024, 16 * 1024 * 1024, 2 * 1024 * 1024 * 1024)
MAX_FMU_ARCHIVE_BYTES: int = _bounded_int("FMU_MAX_ARCHIVE_BYTES", 512 * 1024 * 1024, 1024, 4 * 1024 * 1024 * 1024)
MAX_FMU_EXPANDED_BYTES: int = _bounded_int("FMU_MAX_EXPANDED_BYTES", 2 * 1024 * 1024 * 1024, 1024, 16 * 1024 * 1024 * 1024)
MAX_FMU_ARCHIVE_ENTRIES: int = _bounded_int("FMU_MAX_ARCHIVE_ENTRIES", 20000, 1, 200000)
MAX_FMU_MODEL_DESCRIPTION_BYTES: int = _bounded_int(
    "FMU_MAX_MODEL_DESCRIPTION_BYTES", 32 * 1024 * 1024, 1024, 128 * 1024 * 1024
)
WORKER_CPU_SECONDS: int = _bounded_int("FMU_WORKER_CPU_SECONDS", 300, 1, 86400)
WORKER_MEMORY_BYTES: int = _bounded_int("FMU_WORKER_MEMORY_MIB", 1024, 128, 16384) * 1024 * 1024
WORKER_FILE_BYTES: int = _bounded_int("FMU_WORKER_FILE_MIB", 128, 1, 4096) * 1024 * 1024


# OMSimulator is an optional future composition backend. It is deliberately
# not part of the mandatory Windows runtime installation.
def omsimulator_enabled() -> bool:
    return (_env("FMU_OMSIMULATOR_ENABLED", "false") or "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def omsimulator_command() -> str:
    return _env("FMU_OMSIMULATOR_COMMAND", "OMSimulator") or "OMSimulator"


def omsimulator_timeout_seconds() -> float:
    return max(1.0, float(_env("FMU_OMSIMULATOR_TIMEOUT_SECONDS", "3600") or "3600"))

# Session limits
MAX_CONCURRENT_SESSIONS: int = int(_env("FMU_MAX_SESSIONS", "4") or "4")
FMU_ATTACH_GRACE_SECONDS: int = max(0, int(_env("FMU_ATTACH_GRACE_SECONDS", "120") or "120"))

# Logging
def log_level() -> str:
    return _env("FMU_LOG_LEVEL", "INFO") or "INFO"
