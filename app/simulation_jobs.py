"""Bounded asynchronous single-run and batch execution jobs."""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from . import auth, config, engine, fmu_storage, process_runner
from .reproducibility import (
    applied_execution_policy,
    attach_evidence,
    build_runtime_metadata,
    canonical_sha256,
    effective_execution_options,
)
from .simulation_store import QuotaExceededError, SimulationStore, reservation_scope
from .simulation_defaults import resolve_simulation_time_options


_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_PARAMETERS_PER_CASE = config.MAX_SIMULATION_PARAMETERS
_MAX_PARAMETER_BYTES = 16 * 1024


def _job_id(requested: str | None) -> str:
    value = str(requested or uuid4().hex)
    if not _JOB_ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="INVALID_SIMULATION_ID")
    return value


def model_execution_policy(context: dict[str, Any] | None) -> dict[str, int]:
    claims = (context or {}).get("claims") or {}
    raw = claims.get("fmuExecutionPolicy", {}) if isinstance(claims, dict) else {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=403, detail="INVALID_FMU_EXECUTION_POLICY")
    allowed = {
        "maxDailyScenarios",
        "maxSteps",
        "maxCpuSeconds",
        "maxMemoryMiB",
        "maxResultBytes",
        "maxInputUpdatesPerWindow",
    }
    if set(raw) - allowed:
        raise HTTPException(status_code=403, detail="INVALID_FMU_EXECUTION_POLICY")
    maxima = {
        "maxDailyScenarios": 10000,
        "maxSteps": config.MAX_SIMULATION_STEPS,
        "maxCpuSeconds": config.WORKER_CPU_SECONDS,
        "maxMemoryMiB": config.WORKER_MEMORY_BYTES // (1024 * 1024),
        "maxResultBytes": config.MAX_STORED_RESULT_BYTES,
        "maxInputUpdatesPerWindow": config.MAX_INPUT_UPDATES_PER_WINDOW,
    }
    defaults = {
        "maxDailyScenarios": config.MAX_SCENARIOS_PER_RESERVATION_PER_DAY,
        "maxSteps": config.MAX_SIMULATION_STEPS,
        "maxCpuSeconds": config.WORKER_CPU_SECONDS,
        "maxMemoryMiB": config.WORKER_MEMORY_BYTES // (1024 * 1024),
        "maxResultBytes": config.MAX_STORED_RESULT_BYTES,
        "maxInputUpdatesPerWindow": config.MAX_INPUT_UPDATES_PER_WINDOW,
    }
    policy = {}
    for key, default in defaults.items():
        value = raw.get(key, default)
        if type(value) is not int or value < 1 or value > maxima[key]:
            raise HTTPException(status_code=403, detail="INVALID_FMU_EXECUTION_POLICY")
        policy[key] = value
    return policy


def execution_deadlines(context: dict[str, Any] | None) -> tuple[float, float | None]:
    claims = (context or {}).get("claims") or {}
    if not isinstance(claims, dict):
        claims = {}
    raw_authorization_expiry = (context or {}).get("executionAuthorizationExpiresAt", claims.get("exp"))
    if raw_authorization_expiry is None:
        raise HTTPException(status_code=403, detail="EXECUTION_AUTHORIZATION_EXPIRY_REQUIRED")
    try:
        authorization_expiry = float(raw_authorization_expiry)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=403, detail="EXECUTION_AUTHORIZATION_EXPIRY_REQUIRED") from exc
    if not math.isfinite(authorization_expiry) or authorization_expiry <= time.time():
        raise HTTPException(status_code=403, detail="EXECUTION_AUTHORIZATION_EXPIRED")
    raw_claim_expiry = claims.get("exp")
    if raw_claim_expiry is not None:
        try:
            if authorization_expiry > float(raw_claim_expiry):
                raise HTTPException(status_code=403, detail="INVALID_EXECUTION_AUTHORIZATION_EXPIRY")
        except (TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(status_code=403, detail="EXECUTION_AUTHORIZATION_EXPIRY_REQUIRED") from exc

    # Reservation end is an independent claim. A short-lived connection/session
    # ticket is intentionally not used as a reservation end time.
    raw_reservation_end = (context or {}).get("reservationEndsAt", claims.get("reservationEndsAt"))
    if raw_reservation_end is None:
        return authorization_expiry, None
    try:
        reservation_ends_at = float(raw_reservation_end)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=403, detail="INVALID_RESERVATION_END") from exc
    if not math.isfinite(reservation_ends_at) or reservation_ends_at <= time.time():
        raise HTTPException(status_code=403, detail="RESERVATION_NOT_ACTIVE")
    return authorization_expiry, reservation_ends_at


def _validate_options(
    access_key: str,
    options: dict[str, Any],
    policy: dict[str, int] | None = None,
    *,
    model_metadata: dict[str, Any] | None = None,
) -> int:
    """Reject unsupported, non-finite, or overly dense model queries early."""
    supported = {
        "backend", "startTime", "stopTime", "stepSize", "earlyReturnAllowed",
        "outputVariables", "outputSampleEverySteps",
    }
    if not isinstance(options, dict) or set(options) - supported:
        raise HTTPException(status_code=422, detail="UNSUPPORTED_SIMULATION_OPTIONS")
    try:
        metadata = model_metadata or fmu_storage.describe(access_key)
        if not metadata.get("supportsCoSimulation"):
            raise HTTPException(status_code=422, detail="FMU_COSIMULATION_REQUIRED")
        numeric_options = resolve_simulation_time_options(metadata, options)
        if any(type(value) not in (int, float) for value in numeric_options.values()):
            raise HTTPException(status_code=422, detail="INVALID_SIMULATION_OPTIONS")
        start, stop, step = (float(value) for value in numeric_options.values())
    except HTTPException:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail="INVALID_SIMULATION_OPTIONS") from exc
    if not all(math.isfinite(value) for value in (start, stop, step)) or stop <= start or step <= 0:
        raise HTTPException(status_code=422, detail="INVALID_SIMULATION_OPTIONS")
    if "earlyReturnAllowed" in options and type(options["earlyReturnAllowed"]) is not bool:
        raise HTTPException(status_code=422, detail="INVALID_SIMULATION_OPTIONS")
    duration = stop - start
    if not math.isfinite(duration):
        raise HTTPException(status_code=422, detail="INVALID_SIMULATION_OPTIONS")
    try:
        steps = math.ceil(duration / step)
    except (OverflowError, ValueError) as exc:
        raise HTTPException(status_code=413, detail="SIMULATION_STEP_LIMIT_EXCEEDED") from exc
    maximum_steps = (policy or {}).get("maxSteps", config.MAX_SIMULATION_STEPS)
    if steps > maximum_steps:
        raise HTTPException(status_code=413, detail="SIMULATION_STEP_LIMIT_EXCEEDED")
    sample_every = options.get("outputSampleEverySteps", 1)
    if type(sample_every) is not int or not 1 <= sample_every <= config.MAX_OUTPUT_SAMPLE_EVERY_STEPS:
        raise HTTPException(status_code=422, detail="INVALID_OUTPUT_SAMPLING")
    selected = options.get("outputVariables")
    if selected is not None:
        if not isinstance(selected, list) or len(selected) > 128 or any(not isinstance(name, str) or not name or len(name) > 256 for name in selected):
            raise HTTPException(status_code=422, detail="INVALID_OUTPUT_SELECTION")
        if len(set(selected)) != len(selected):
            raise HTTPException(status_code=422, detail="INVALID_OUTPUT_SELECTION")
        valid_outputs = {
            item.get("name") for item in metadata.get("modelVariables", [])
            if item.get("causality") == "output"
        }
        if any(name not in valid_outputs for name in selected):
            raise HTTPException(status_code=422, detail="UNKNOWN_OUTPUT_VARIABLE")
    return steps


def _validated_context(context: dict[str, Any] | None, access_key: str) -> tuple[str, dict[str, str]]:
    try:
        auth.validate_gateway_context(context, access_key)
        return reservation_scope(context)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED") from exc


def _validate_parameters(parameters: dict[str, Any]) -> None:
    if not isinstance(parameters, dict) or len(parameters) > _MAX_PARAMETERS_PER_CASE:
        raise HTTPException(status_code=422, detail="SIMULATION_PARAMETER_LIMIT_EXCEEDED")
    if any(not isinstance(key, str) or not key or len(key) > 128 for key in parameters):
        raise HTTPException(status_code=422, detail="INVALID_SIMULATION_PARAMETERS")

    def validate_value(value: Any, *, depth: int = 0) -> int:
        if isinstance(value, list):
            if depth >= 4 or len(value) > 4096:
                raise HTTPException(status_code=422, detail="SIMULATION_PARAMETER_LIMIT_EXCEEDED")
            return sum(validate_value(item, depth=depth + 1) for item in value)
        if value is None or isinstance(value, dict) or not isinstance(value, (str, int, float, bool)):
            raise HTTPException(status_code=422, detail="INVALID_SIMULATION_PARAMETERS")
        if isinstance(value, str) and len(value) > 4096:
            raise HTTPException(status_code=422, detail="SIMULATION_PARAMETER_LIMIT_EXCEEDED")
        return 1

    for value in parameters.values():
        if validate_value(value) > 4096:
            raise HTTPException(status_code=422, detail="SIMULATION_PARAMETER_LIMIT_EXCEEDED")
    try:
        encoded = json.dumps(parameters, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="INVALID_SIMULATION_PARAMETERS") from exc
    if len(encoded.encode("utf-8")) > _MAX_PARAMETER_BYTES:
        raise HTTPException(status_code=413, detail="SIMULATION_PARAMETER_SIZE_LIMIT_EXCEEDED")


class SimulationJobManager:
    def __init__(self, store: SimulationStore):
        self.store = store
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = asyncio.Lock()

    async def submit_single(
        self,
        *,
        access_key: str,
        gateway_context: dict[str, Any],
        parameters: dict[str, Any],
        options: dict[str, Any],
        requested_id: str | None = None,
    ) -> dict[str, Any]:
        self._validate_parameters(parameters)
        policy = model_execution_policy(gateway_context)
        steps = _validate_options(access_key, options, policy)
        return await self._submit(
            job_id=_job_id(requested_id),
            access_key=access_key,
            gateway_context=gateway_context,
            kind="single",
            scenarios=[{"parameters": parameters, "options": options, "steps": steps}],
            stored_parameters=parameters,
            stored_options=options,
            policy=policy,
        )

    async def submit_batch(
        self,
        *,
        access_key: str,
        gateway_context: dict[str, Any],
        scenarios: list[dict[str, Any]],
        options: dict[str, Any],
        requested_id: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= len(scenarios) <= config.MAX_BATCH_CASES:
            raise HTTPException(
                status_code=422,
                detail={"code": "BATCH_SIZE_LIMIT_EXCEEDED", "maximum": config.MAX_BATCH_CASES},
            )
        prepared = []
        total_steps = 0
        policy = model_execution_policy(gateway_context)
        for index, scenario in enumerate(scenarios):
            parameters = scenario.get("parameters") or {}
            case_options = {**options, **(scenario.get("options") or {})}
            self._validate_parameters(parameters)
            steps = _validate_options(access_key, case_options, policy)
            total_steps += steps
            if total_steps > config.MAX_SIMULATION_STEPS * 2:
                raise HTTPException(status_code=413, detail="BATCH_STEP_LIMIT_EXCEEDED")
            label = str(scenario.get("label") or f"case-{index + 1}")[:80]
            prepared.append({"label": label, "parameters": parameters, "options": case_options, "steps": steps})
        return await self._submit(
            job_id=_job_id(requested_id),
            access_key=access_key,
            gateway_context=gateway_context,
            kind="batch",
            scenarios=prepared,
            stored_parameters={
                "scenarios": [
                    {"label": item["label"], "parameters": item["parameters"], "options": item["options"]}
                    for item in prepared
                ]
            },
            stored_options=options,
            policy=policy,
        )

    @staticmethod
    def _validate_parameters(parameters: dict[str, Any]) -> None:
        _validate_parameters(parameters)

    async def _submit(
        self,
        *,
        job_id: str,
        access_key: str,
        gateway_context: dict[str, Any],
        kind: str,
        scenarios: list[dict[str, Any]],
        stored_parameters: dict[str, Any],
        stored_options: dict[str, Any],
        policy: dict[str, int],
    ) -> dict[str, Any]:
        if config.execution_mode() != "process":
            raise HTTPException(status_code=409, detail="ASYNC_EXECUTION_REQUIRES_PROCESS_ISOLATION")
        scope_key, scope = _validated_context(gateway_context, access_key)
        authorization_expires_at, reservation_ends_at = execution_deadlines(gateway_context)
        async with self._lock:
            try:
                capacity_token = engine.reserve_capacity()
            except engine.CapacityExceededError as exc:
                raise HTTPException(status_code=429, detail="STATION_CAPACITY_EXHAUSTED") from exc
            try:
                source_path = fmu_storage.get_fmu_path(access_key)
                source_report = fmu_storage.inspect_fmu(access_key, include_hash=False)
                if not source_report["archiveValid"] or not source_report["modelDescriptionValid"] or not source_report["stationCompatible"]:
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "FMU_NOT_COMPATIBLE", "issues": source_report["issues"][:8]},
                    )
                snapshot_path, model_sha256 = fmu_storage.snapshot_fmu(source_path)
                report = fmu_storage.inspect_fmu_path(snapshot_path, access_key=access_key, include_hash=False)
                if not report["archiveValid"] or not report["modelDescriptionValid"] or not report["stationCompatible"]:
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "FMU_NOT_COMPATIBLE", "issues": report["issues"][:8]},
                    )
                snapshot_metadata = fmu_storage.describe_path(snapshot_path)
                total_steps = 0
                for scenario in scenarios:
                    scenario["steps"] = _validate_options(
                        access_key, scenario["options"], policy, model_metadata=snapshot_metadata,
                    )
                    scenario["effectiveOptions"] = effective_execution_options(
                        snapshot_metadata,
                        scenario["options"],
                    )
                    total_steps += scenario["steps"]
                if kind == "batch" and total_steps > config.MAX_SIMULATION_STEPS * 2:
                    raise HTTPException(status_code=413, detail="BATCH_STEP_LIMIT_EXCEEDED")
                policy_evidence = applied_execution_policy(policy, stored_options)
                metadata = build_runtime_metadata(
                    model_sha256=model_sha256,
                    options=stored_options,
                    policy=policy_evidence,
                    authorization_expires_at=authorization_expires_at,
                    reservation_ends_at=reservation_ends_at,
                )
                metadata["caseOptions"] = [
                    {"label": scenario.get("label"), "options": scenario["effectiveOptions"]}
                    for scenario in scenarios
                ]
                self.store.create_job(
                    job_id=job_id,
                    scope_key=scope_key,
                    scope=scope,
                    fmu_filename=Path(access_key).name,
                    kind=kind,
                    total_cases=len(scenarios),
                    parameters=stored_parameters,
                    options=stored_options,
                    metadata=metadata,
                    authorization_expires_at=authorization_expires_at,
                    reservation_ends_at=reservation_ends_at,
                    maximum_scenarios=policy["maxDailyScenarios"],
                )
            except QuotaExceededError as exc:
                engine.release_capacity(capacity_token)
                raise HTTPException(
                    status_code=429,
                    detail={"code": "RESERVATION_DAILY_SCENARIO_LIMIT", "remaining": max(0, int(exc.args[0]))},
                ) from exc
            except sqlite3.IntegrityError as exc:
                engine.release_capacity(capacity_token)
                raise HTTPException(status_code=409, detail="SIMULATION_ID_ALREADY_EXISTS") from exc
            except HTTPException:
                engine.release_capacity(capacity_token)
                raise
            except (TypeError, ValueError) as exc:
                engine.release_capacity(capacity_token)
                raise HTTPException(status_code=422, detail="INVALID_SIMULATION_REQUEST") from exc
            except Exception:
                engine.release_capacity(capacity_token)
                raise
            cancel_event = threading.Event()
            self._cancel_events[job_id] = cancel_event
            self._tasks[job_id] = asyncio.create_task(
                self._execute(
                    job_id, access_key, kind, scenarios, cancel_event, snapshot_path,
                    model_sha256, authorization_expires_at, reservation_ends_at,
                    capacity_token, policy, policy_evidence,
                ),
                name=f"fmu-job-{job_id}",
            )
        return {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "totalCases": len(scenarios),
            "fmuSha256": model_sha256,
            "statusUrl": f"/internal/fmu/simulations/{job_id}",
            "resultUrl": f"/internal/fmu/simulations/{job_id}/result",
        }

    async def _execute(
        self,
        job_id: str,
        access_key: str,
        kind: str,
        scenarios: list[dict[str, Any]],
        cancel_event: threading.Event,
        path: Path,
        model_sha256: str,
        authorization_expires_at: float,
        reservation_ends_at: float | None,
        capacity_token: str,
        policy: dict[str, int],
        policy_evidence: dict[str, Any],
    ) -> None:
        results: list[dict[str, Any]] = []
        try:
            self.store.update_job(job_id, status="running", completed_cases=0)
            for index, scenario in enumerate(scenarios):
                if cancel_event.is_set():
                    self._finish(job_id, status="cancelled", kind=kind, results=results, finish_reason="cancelled")
                    return
                if time.time() >= authorization_expires_at:
                    self._finish(
                        job_id, status="expired", kind=kind, results=results,
                        error_code="EXECUTION_AUTHORIZATION_EXPIRED", finish_reason="authorization_expired",
                    )
                    return
                if reservation_ends_at is not None and time.time() >= reservation_ends_at:
                    self._finish(
                        job_id, status="expired", kind=kind, results=results,
                        error_code="RESERVATION_ENDED", finish_reason="reservation_ended",
                    )
                    return
                try:
                    result = await asyncio.to_thread(
                        process_runner.run,
                        path,
                        access_key=access_key,
                        parameters=scenario["parameters"],
                        options=scenario["options"],
                        cancel_event=cancel_event,
                        capture_series=True,
                        authorization_expires_at=authorization_expires_at,
                        reservation_ends_at=reservation_ends_at,
                        resource_policy=policy,
                    )
                except process_runner.ProcessExecutionError:
                    raise
                evidenced_result = attach_evidence(
                    result,
                    model_sha256=model_sha256,
                    parameters=scenario["parameters"],
                    options=scenario["effectiveOptions"],
                    policy={
                        **policy_evidence,
                        "outputSampleEverySteps": scenario["effectiveOptions"]["outputSampleEverySteps"],
                    },
                )
                results.append({"index": index, "label": scenario.get("label"), **evidenced_result})
                self.store.update_job(
                    job_id, status="running", completed_cases=len(results),
                )
            payload = self._result_payload(kind, results, partial=False)
            self.store.update_job(
                job_id, status="completed", completed_cases=len(results),
                result=payload, finish_reason="completed", warnings=[],
                result_sha256=canonical_sha256(payload),
            )
        except engine.CapacityExceededError:
            self._finish(
                job_id, status="failed", kind=kind, results=results,
                error_code="STATION_CAPACITY_EXHAUSTED",
            )
        except process_runner.ProcessExecutionError as exc:
            if exc.code == "FMU_EXECUTION_CANCELLED":
                self._finish(job_id, status="cancelled", kind=kind, results=results, finish_reason="cancelled")
            elif exc.code == "EXECUTION_AUTHORIZATION_EXPIRED":
                self._finish(
                    job_id, status="expired", kind=kind, results=results,
                    error_code=exc.code, finish_reason="authorization_expired", warnings=[exc.code],
                )
            elif exc.code == "RESERVATION_ENDED":
                self._finish(
                    job_id, status="expired", kind=kind, results=results,
                    error_code=exc.code, finish_reason="reservation_ended", warnings=[exc.code],
                )
            else:
                self._finish(
                    job_id, status="failed", kind=kind, results=results,
                    error_code=exc.code, finish_reason="failed", warnings=[exc.code],
                )
        except ValueError:
            self._finish(
                job_id, status="failed", kind=kind, results=results,
                error_code="RESULT_SIZE_LIMIT_EXCEEDED", finish_reason="result_size_limit_exceeded",
                warnings=["RESULT_SIZE_LIMIT_EXCEEDED"],
            )
        except Exception:
            self._finish(
                job_id, status="failed", kind=kind, results=results,
                error_code="FMU_EXECUTION_FAILED", finish_reason="failed", warnings=["FMU_EXECUTION_FAILED"],
            )
        finally:
            engine.release_capacity(capacity_token)
            self._cancel_events.pop(job_id, None)
            self._tasks.pop(job_id, None)

    @staticmethod
    def _result_payload(kind: str, results: list[dict[str, Any]], *, partial: bool) -> Any:
        if kind == "single":
            if not results:
                return None
            return {key: value for key, value in results[0].items() if key not in {"index", "label"}}
        return {"cases": results, "partial": partial}

    def _finish(
        self,
        job_id: str,
        *,
        status: str,
        kind: str,
        results: list[dict[str, Any]],
        error_code: str | None = None,
        finish_reason: str | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        result = self._result_payload(kind, results, partial=status != "completed")
        result_sha256 = canonical_sha256(result) if result is not None else None
        try:
            self.store.update_job(
                job_id,
                status=status,
                completed_cases=len(results),
                result=result if result is not None else None,
                error_code=error_code,
                finish_reason=finish_reason or status,
                warnings=warnings or [],
                result_sha256=result_sha256,
            )
        except ValueError:
            self.store.update_job(
                job_id,
                status="failed",
                completed_cases=len(results),
                error_code="RESULT_SIZE_LIMIT_EXCEEDED",
                finish_reason="result_size_limit_exceeded",
                warnings=["RESULT_SIZE_LIMIT_EXCEEDED"],
            )

    async def cancel(self, job_id: str, scope_key: str) -> dict[str, Any] | None:
        row = self.store.get_job(job_id, scope_key)
        if row is None:
            return None
        event = self._cancel_events.get(job_id)
        if event is None:
            return row
        self.store.mark_cancelling(job_id, scope_key)
        event.set()
        return self.store.get_job(job_id, scope_key)

    def status(self, job_id: str, scope_key: str) -> dict[str, Any] | None:
        return self.store.get_job(job_id, scope_key)

    def result(self, job_id: str, scope_key: str) -> dict[str, Any] | None:
        return self.store.get_result(job_id, scope_key)

    def history(self, scope_key: str, *, limit: int, offset: int) -> dict[str, Any]:
        return self.store.list_history(scope_key, limit=limit, offset=offset)

    async def wait(self, job_id: str, scope_key: str) -> dict[str, Any] | None:
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.shield(task)
        return self.store.get_result(job_id, scope_key)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for event in self._cancel_events.values():
            event.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
