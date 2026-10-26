"""Stable execution metadata and hashes attached to simulation evidence."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from . import config
from .isolation import worker_isolation_capabilities
from .simulation_defaults import resolve_simulation_time_options


def canonical_sha256(value: Any) -> str:
    packed = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def executor_version() -> str:
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        try:
            return version("decentralabs-fmu-executor")
        except PackageNotFoundError:
            return "unknown"


def fmpy_version() -> str:
    try:
        return version("fmpy")
    except PackageNotFoundError:
        return "unknown"


def execution_policy(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "executionTimeoutSeconds": config.execution_timeout_seconds(),
        "workerCpuSeconds": config.WORKER_CPU_SECONDS,
        "workerMemoryBytes": config.WORKER_MEMORY_BYTES,
        "workerFileBytes": config.WORKER_FILE_BYTES,
        "maxSimulationSteps": config.MAX_SIMULATION_STEPS,
        "maxResultBytes": config.MAX_STORED_RESULT_BYTES,
        "outputSampleEverySteps": options.get("outputSampleEverySteps", 1),
    }


def effective_execution_options(
    model_metadata: dict[str, Any],
    options: dict[str, Any],
    *,
    early_return_default: bool = True,
) -> dict[str, Any]:
    """Record the concrete values the session applies when options are omitted."""
    output_variables = options.get("outputVariables")
    if output_variables is None:
        output_variables = [
            item["name"] for item in model_metadata.get("modelVariables", [])
            if item.get("causality") == "output" and isinstance(item.get("name"), str)
        ]
    time_options = resolve_simulation_time_options(model_metadata, options)
    return {
        "backend": options.get("backend", "fmpy") or "fmpy",
        **{name: float(value) for name, value in time_options.items()},
        "earlyReturnAllowed": options.get("earlyReturnAllowed", early_return_default),
        "outputVariables": list(output_variables),
        "outputSampleEverySteps": options.get("outputSampleEverySteps", 1),
    }


def applied_execution_policy(
    model_policy: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any]:
    """Record configured model limits alongside controls active on this host."""
    mode = config.execution_mode()
    isolation = worker_isolation_capabilities()
    policy = execution_policy(options)
    worker_enabled = mode == "process"
    policy.update({
        "executionMode": mode,
        "workerIsolation": {**isolation, "enabled": worker_enabled},
        "maxDailyScenarios": model_policy["maxDailyScenarios"],
        "maxSteps": model_policy["maxSteps"],
        "executionTimeoutSeconds": config.execution_timeout_seconds() if worker_enabled else None,
        "workerCpuSeconds": min(model_policy["maxCpuSeconds"], config.WORKER_CPU_SECONDS) if worker_enabled else None,
        "workerMemoryBytes": min(
            model_policy["maxMemoryMiB"] * 1024 * 1024,
            config.WORKER_MEMORY_BYTES,
        ) if worker_enabled else None,
        "workerFileBytes": config.WORKER_FILE_BYTES
        if worker_enabled and isolation["temporaryFileLimitPerFile"] else None,
        "maxResultBytes": min(model_policy["maxResultBytes"], config.MAX_STORED_RESULT_BYTES)
        if worker_enabled else None,
        "outputSampleEverySteps": options.get("outputSampleEverySteps", 1),
    })
    return policy


def build_execution_evidence(
    *,
    model_sha256: str,
    parameters: dict[str, Any],
    options: dict[str, Any],
    result: dict[str, Any],
    finish_reason: str,
    warnings: list[str] | None = None,
    policy: dict[str, Any] | None = None,
    result_sha256: str | None = None,
    input_schedule: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "fmuSha256": model_sha256,
        "executorVersion": executor_version(),
        "executorCommit": os.environ.get("FMU_EXECUTOR_COMMIT", "unknown"),
        "fmpyVersion": fmpy_version(),
        "platform": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "parameters": parameters,
        "options": options,
        "policy": policy or execution_policy(options),
        "finishReason": finish_reason,
        "warnings": warnings or [],
        "resultSha256": result_sha256 or canonical_sha256(result),
    }
    if input_schedule:
        evidence["inputSchedule"] = input_schedule
    return evidence


def build_runtime_metadata(
    *,
    model_sha256: str,
    options: dict[str, Any],
    policy: dict[str, Any],
    authorization_expires_at: float,
    reservation_ends_at: float | None,
) -> dict[str, Any]:
    return {
        "fmuSha256": model_sha256,
        "executorVersion": executor_version(),
        "executorCommit": os.environ.get("FMU_EXECUTOR_COMMIT", "unknown"),
        "fmpyVersion": fmpy_version(),
        "platform": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "options": options,
        "policy": policy,
        "authorizationExpiresAt": authorization_expires_at,
        "reservationEndsAt": reservation_ends_at,
    }


def attach_evidence(
    result: dict[str, Any],
    *,
    model_sha256: str,
    parameters: dict[str, Any],
    options: dict[str, Any],
    finish_reason: str | None = None,
    policy: dict[str, Any] | None = None,
    input_schedule: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = dict(result)
    payload["evidence"] = build_execution_evidence(
        model_sha256=model_sha256,
        parameters=parameters,
        options=options,
        result=result,
        finish_reason=finish_reason or result.get("finishReason") or "completed",
        policy=policy,
        input_schedule=input_schedule,
    )
    return payload
