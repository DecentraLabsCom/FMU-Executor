"""Bounded asynchronous single-run and batch execution jobs."""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from . import auth, config, engine, fmu_storage, process_runner
from .simulation_store import QuotaExceededError, SimulationStore, reservation_scope


_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_PARAMETERS_PER_CASE = 32
_MAX_PARAMETER_BYTES = 16 * 1024


def _job_id(requested: str | None) -> str:
    value = str(requested or uuid4().hex)
    if not _JOB_ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="INVALID_SIMULATION_ID")
    return value


def _validate_options(access_key: str, options: dict[str, Any]) -> int:
    """Reject unsupported, non-finite, or overly dense model queries early."""
    if not isinstance(options, dict) or set(options) - {"startTime", "stopTime", "stepSize"}:
        raise HTTPException(status_code=422, detail="UNSUPPORTED_SIMULATION_OPTIONS")
    try:
        metadata = fmu_storage.describe(access_key)
        if not metadata.get("supportsCoSimulation"):
            raise HTTPException(status_code=422, detail="FMU_COSIMULATION_REQUIRED")
        start = float(options.get("startTime", 0.0))
        stop = float(options.get("stopTime", 1.0))
        default_step = metadata.get("defaultStepSize") or 0.001
        step = float(options.get("stepSize", default_step))
    except HTTPException:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail="INVALID_SIMULATION_OPTIONS") from exc
    if not all(math.isfinite(value) for value in (start, stop, step)) or stop <= start or step <= 0:
        raise HTTPException(status_code=422, detail="INVALID_SIMULATION_OPTIONS")
    steps = math.ceil((stop - start) / step)
    if steps > config.MAX_SIMULATION_STEPS:
        raise HTTPException(status_code=413, detail="SIMULATION_STEP_LIMIT_EXCEEDED")
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
        steps = _validate_options(access_key, options)
        return await self._submit(
            job_id=_job_id(requested_id),
            access_key=access_key,
            gateway_context=gateway_context,
            kind="single",
            scenarios=[{"parameters": parameters, "options": options, "steps": steps}],
            stored_parameters=parameters,
            stored_options=options,
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
        for index, scenario in enumerate(scenarios):
            parameters = scenario.get("parameters") or {}
            case_options = {**options, **(scenario.get("options") or {})}
            self._validate_parameters(parameters)
            steps = _validate_options(access_key, case_options)
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
    ) -> dict[str, Any]:
        if config.execution_mode() != "process":
            raise HTTPException(status_code=409, detail="ASYNC_EXECUTION_REQUIRES_PROCESS_ISOLATION")
        scope_key, scope = _validated_context(gateway_context, access_key)
        async with self._lock:
            active = [task for task in self._tasks.values() if not task.done()]
            if len(active) >= config.MAX_CONCURRENT_SESSIONS:
                raise HTTPException(status_code=429, detail="STATION_CAPACITY_EXHAUSTED")
            try:
                self.store.create_job(
                    job_id=job_id,
                    scope_key=scope_key,
                    scope=scope,
                    fmu_filename=Path(access_key).name,
                    kind=kind,
                    total_cases=len(scenarios),
                    parameters=stored_parameters,
                    options=stored_options,
                )
            except QuotaExceededError as exc:
                raise HTTPException(
                    status_code=429,
                    detail={"code": "RESERVATION_DAILY_SCENARIO_LIMIT", "remaining": max(0, int(exc.args[0]))},
                ) from exc
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="SIMULATION_ID_ALREADY_EXISTS") from exc
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="INVALID_SIMULATION_REQUEST") from exc
            cancel_event = threading.Event()
            self._cancel_events[job_id] = cancel_event
            self._tasks[job_id] = asyncio.create_task(
                self._execute(job_id, access_key, kind, scenarios, cancel_event),
                name=f"fmu-job-{job_id}",
            )
        return {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "totalCases": len(scenarios),
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
    ) -> None:
        results: list[dict[str, Any]] = []
        try:
            path = fmu_storage.get_fmu_path(access_key)
            self.store.update_job(job_id, status="running", completed_cases=0)
            for index, scenario in enumerate(scenarios):
                if cancel_event.is_set():
                    self._finish(job_id, status="cancelled", kind=kind, results=results)
                    return
                slot = engine.create_session(path, access_key=access_key)
                try:
                    result = await asyncio.to_thread(
                        process_runner.run,
                        path,
                        access_key=access_key,
                        parameters=scenario["parameters"],
                        options=scenario["options"],
                        cancel_event=cancel_event,
                        capture_series=True,
                    )
                finally:
                    engine.remove_session(slot.session_id)
                results.append({"index": index, "label": scenario.get("label"), **result})
                self.store.update_job(
                    job_id, status="running", completed_cases=len(results),
                )
            self.store.update_job(
                job_id, status="completed", completed_cases=len(results),
                result=self._result_payload(kind, results, partial=False),
            )
        except engine.CapacityExceededError:
            self._finish(
                job_id, status="failed", kind=kind, results=results,
                error_code="STATION_CAPACITY_EXHAUSTED",
            )
        except process_runner.ProcessExecutionError as exc:
            if exc.code == "FMU_EXECUTION_CANCELLED":
                self._finish(job_id, status="cancelled", kind=kind, results=results)
            else:
                self._finish(job_id, status="failed", kind=kind, results=results, error_code=exc.code)
        except ValueError:
            self.store.update_job(
                job_id, status="failed", completed_cases=len(results), error_code="RESULT_SIZE_LIMIT_EXCEEDED"
            )
        except Exception:
            self._finish(job_id, status="failed", kind=kind, results=results, error_code="FMU_EXECUTION_FAILED")
        finally:
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
    ) -> None:
        result = self._result_payload(kind, results, partial=status != "completed")
        try:
            self.store.update_job(
                job_id,
                status=status,
                completed_cases=len(results),
                result=result if result is not None else None,
                error_code=error_code,
            )
        except ValueError:
            self.store.update_job(
                job_id,
                status="failed",
                completed_cases=len(results),
                error_code="RESULT_SIZE_LIMIT_EXCEEDED",
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
