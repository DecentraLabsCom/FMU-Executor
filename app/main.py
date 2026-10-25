"""FMU Executor – FastAPI application.

Internal-only service that runs on Lab Station, providing the FMU execution plane
consumed by Lab Gateway's fmu-runner in ``station`` backend mode.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import secrets
import threading
import time
import zipfile
import zlib
from pathlib import PurePosixPath
from typing import Any, Callable, Protocol, cast

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import auth, backends, config, engine, fmu_storage, process_runner
from .isolation import worker_isolation_capabilities
from .simulation_jobs import (
    SimulationJobManager,
    _validate_parameters,
    _validate_options,
    execution_deadlines,
    model_execution_policy,
)
from .simulation_store import (
    InputUpdateQuotaExceededError,
    QuotaExceededError,
    SimulationStore,
    reservation_scope,
)
from .reproducibility import (
    applied_execution_policy,
    attach_evidence as attach_run_evidence,
    build_execution_evidence,
    effective_execution_options,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="FMU Executor", version="0.3.2", docs_url=None, redoc_url=None)
MAX_COMPANION_ASSET_BYTES = 50 * 1024 * 1024
MAX_COMPANION_APP_FILES = 332
_COMPANION_APP_MEDIA_TYPES = {
    ".bin": "application/octet-stream",
    ".css": "text/css",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".html": "text/html",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript",
    ".json": "application/json",
    ".mjs": "text/javascript",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
_session_cleanup_task: asyncio.Task | None = None
_simulation_store = SimulationStore()
_simulation_jobs = SimulationJobManager(_simulation_store)


async def _cleanup_sessions_loop() -> None:
    try:
        while True:
            await asyncio.to_thread(engine.cleanup_expired_sessions)
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        return

# ── Startup / shutdown ───────────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    global _session_cleanup_task
    logging.basicConfig(level=getattr(logging, config.log_level(), logging.INFO))
    config.FMU_ROOT.mkdir(parents=True, exist_ok=True)
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    abandoned_workers = await asyncio.to_thread(process_runner.cleanup_abandoned_worker_directories)
    if abandoned_workers:
        logger.info("Removed %s abandoned FMU worker directories", abandoned_workers)
    await asyncio.to_thread(_simulation_store.initialize)
    # Reap abandoned sessions before startup returns. The background loop is
    # deliberately periodic; relying on its first scheduled iteration races
    # with immediate TestClient/server shutdown on short-lived processes.
    await asyncio.to_thread(engine.cleanup_expired_sessions)
    logger.info(
        "FMU Executor starting – root=%s, port=%s, max_sessions=%s",
        config.FMU_ROOT, config.bind_port(), config.MAX_CONCURRENT_SESSIONS,
    )
    if _session_cleanup_task is None or _session_cleanup_task.done():
        _session_cleanup_task = asyncio.create_task(_cleanup_sessions_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _session_cleanup_task
    if _session_cleanup_task:
        _session_cleanup_task.cancel()
        try:
            await _session_cleanup_task
        except asyncio.CancelledError:
            pass
        _session_cleanup_task = None
    logger.info("Shutting down – terminating all sessions")
    await _simulation_jobs.shutdown()
    await asyncio.to_thread(engine.terminate_all)


# ── Dependency ───────────────────────────────────────────────────

async def _check_token(request: Request) -> None:
    auth.validate_internal_token(request)


# ── Health ───────────────────────────────────────────────────────

@app.get("/internal/health")
async def health():
    fmu_count = len(fmu_storage.list_fmus())
    active = engine.active_session_count()
    return {
        "status": "UP",
        "fmuCount": fmu_count,
        "quarantinedCount": len(fmu_storage.list_quarantined()),
        "activeSessions": active,
        "activeExecutions": active,
        "maxSessions": config.MAX_CONCURRENT_SESSIONS,
        "maxConcurrentExecutions": config.MAX_CONCURRENT_SESSIONS,
        "availableCapacity": max(0, config.MAX_CONCURRENT_SESSIONS - active),
        "executionMode": config.execution_mode(),
        "workerIsolation": {**worker_isolation_capabilities(), "enabled": config.execution_mode() == "process"},
        "backends": backends.backend_status(),
        "timestamp": time.time(),
    }


@app.get("/internal/fmu/capacity", dependencies=[Depends(_check_token)])
async def capacity():
    """Expose the Station execution authority to the provider backend."""
    active = engine.active_session_count()
    capacity = config.MAX_CONCURRENT_SESSIONS
    return {
        "capacity": capacity,
        "active": active,
        "available": max(0, capacity - active),
        "activeExecutions": active,
        "maxConcurrentExecutions": capacity,
    }


@app.get("/internal/fmu/backends", dependencies=[Depends(_check_token)])
async def backend_capabilities():
    """Expose current and planned execution backends to diagnostics/UI."""
    return {
        "executionMode": config.execution_mode(),
        "workerIsolation": {**worker_isolation_capabilities(), "enabled": config.execution_mode() == "process"},
        "backends": backends.backend_status(),
    }


# ── Catalog ──────────────────────────────────────────────────────

def _validated_access_key(access_key: str) -> str:
    try:
        return fmu_storage.validate_access_key(access_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="INVALID_FMU_ACCESS_KEY") from exc


@app.get("/internal/fmu/catalog", dependencies=[Depends(_check_token)])
async def catalog(access_key: str = Header(..., alias="X-FMU-Access-Key")):
    access_key = _validated_access_key(access_key)
    if not fmu_storage.fmu_exists(access_key):
        raise HTTPException(404, "FMU_NOT_FOUND")
    desc = fmu_storage.describe(access_key)
    return {
        "accessKey": access_key,
        "fmus": [{"filename": access_key, "path": access_key, "source": "station"}],
        "describe": desc,
    }


@app.get("/internal/fmu/companion-asset", dependencies=[Depends(_check_token)])
async def companion_asset(
    access_key: str = Header(..., alias="X-FMU-Access-Key"),
    asset_type: str = Query("glb", alias="assetType"),
):
    """Return a same-stem GLB or static application ZIP to the Gateway."""
    access_key = _validated_access_key(access_key)
    asset_type = asset_type.strip().lower()
    if asset_type not in {"glb", "threejs-app"}:
        raise HTTPException(status_code=400, detail="INVALID_COMPANION_ASSET_TYPE")
    suffix = ".glb" if asset_type == "glb" else ".webapp.zip"
    media_type = "model/gltf-binary" if asset_type == "glb" else "application/zip"
    try:
        root = config.FMU_ROOT.resolve(strict=True)
        fmu_path = fmu_storage.get_fmu_path(access_key).resolve(strict=True)
        fmu_path.relative_to(root)
        asset_path = fmu_path.with_name(fmu_path.stem + suffix).resolve(strict=True)
        asset_path.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="COMPANION_ASSET_NOT_FOUND") from exc

    try:
        if not asset_path.is_file():
            raise HTTPException(status_code=404, detail="COMPANION_ASSET_NOT_FOUND")
        with asset_path.open("rb") as asset_file:
            content = asset_file.read(MAX_COMPANION_ASSET_BYTES + 1)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=503, detail="COMPANION_ASSET_UNAVAILABLE") from exc

    if len(content) > MAX_COMPANION_ASSET_BYTES:
        raise HTTPException(status_code=413, detail="COMPANION_ASSET_TOO_LARGE")

    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, no-store"},
    )


# ── Describe ─────────────────────────────────────────────────────

@app.get("/internal/fmu/companion-app/{asset_path:path}", dependencies=[Depends(_check_token)])
async def companion_app_asset(
    asset_path: str,
    access_key: str = Header(..., alias="X-FMU-Access-Key"),
):
    """Serve one file from an FMU's sibling web application archive."""
    access_key = _validated_access_key(access_key)
    path_parts = asset_path.split("/")
    if (
        not asset_path
        or "\\" in asset_path
        or len(path_parts) > 8
        or any(not part or part in {".", ".."} for part in path_parts)
        or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", part) is None for part in path_parts)
    ):
        raise HTTPException(status_code=404, detail="COMPANION_APP_ASSET_NOT_FOUND")

    normalized_path = PurePosixPath(asset_path).as_posix()
    media_type = _COMPANION_APP_MEDIA_TYPES.get(PurePosixPath(normalized_path).suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=404, detail="COMPANION_APP_ASSET_NOT_FOUND")

    try:
        root = config.FMU_ROOT.resolve(strict=True)
        fmu_path = fmu_storage.get_fmu_path(access_key).resolve(strict=True)
        fmu_path.relative_to(root)
        archive_path = fmu_path.with_name(fmu_path.stem + ".webapp.zip").resolve(strict=True)
        archive_path.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="COMPANION_APP_NOT_FOUND") from exc

    try:
        if not archive_path.is_file():
            raise HTTPException(status_code=404, detail="COMPANION_APP_NOT_FOUND")
        if archive_path.stat().st_size > MAX_COMPANION_ASSET_BYTES:
            raise HTTPException(status_code=413, detail="COMPANION_APP_TOO_LARGE")
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_COMPANION_APP_FILES:
                raise HTTPException(status_code=413, detail="COMPANION_APP_HAS_TOO_MANY_FILES")
            matches = [
                info for info in entries
                if info.filename.casefold() == normalized_path.casefold()
            ]
            if len(matches) != 1 or matches[0].filename != normalized_path or matches[0].is_dir():
                raise HTTPException(status_code=404, detail="COMPANION_APP_ASSET_NOT_FOUND")
            info = matches[0]
            file_mode = (info.external_attr >> 16) & 0o170000
            if info.flag_bits & 0x1 or file_mode not in (0, 0o100000):
                raise HTTPException(status_code=404, detail="COMPANION_APP_ASSET_NOT_FOUND")
            if info.file_size > MAX_COMPANION_ASSET_BYTES:
                raise HTTPException(status_code=413, detail="COMPANION_APP_ASSET_TOO_LARGE")
            with archive.open(info) as source:
                content = source.read(MAX_COMPANION_ASSET_BYTES + 1)
    except HTTPException:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyError, EOFError, zlib.error) as exc:
        raise HTTPException(status_code=400, detail="COMPANION_APP_ARCHIVE_INVALID") from exc

    if len(content) > MAX_COMPANION_ASSET_BYTES:
        raise HTTPException(status_code=413, detail="COMPANION_APP_ASSET_TOO_LARGE")
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/internal/fmu/describe", dependencies=[Depends(_check_token)])
async def describe(
    access_key: str = Header(..., alias="X-FMU-Access-Key"),
    public_only: bool = Query(False, alias="publicOnly"),
    causality: str | None = Query(
        None,
        pattern="^(input|output|parameter|calculatedParameter|structuralParameter|independent|local)$",
    ),
):
    access_key = _validated_access_key(access_key)
    if not fmu_storage.fmu_exists(access_key):
        raise HTTPException(404, "FMU_NOT_FOUND")
    return fmu_storage.describe(access_key, public_only=public_only, causality=causality)


# ── Validate / Quarantine ────────────────────────────────────────

@app.post("/internal/fmu/validate/{access_key:path}", dependencies=[Depends(_check_token)])
async def validate_fmu(
    access_key: str,
    auto_quarantine: bool = Query(False),
    probe: bool = Query(False),
):
    """Report archive and host compatibility, with an optional isolated load probe."""
    access_key = _validated_access_key(access_key)
    report = fmu_storage.inspect_fmu(access_key)
    probe_failure_code = None
    report["workerIsolation"] = {
        **worker_isolation_capabilities(),
        "enabled": probe or config.execution_mode() == "process",
    }
    if probe and report["archiveValid"] and report["stationCompatible"]:
        try:
            token = engine.reserve_capacity()
        except engine.CapacityExceededError:
            raise HTTPException(status_code=429, detail="STATION_CAPACITY_EXHAUSTED")
        try:
            path, digest = fmu_storage.snapshot_fmu(fmu_storage.get_fmu_path(access_key))
            report = fmu_storage.inspect_fmu_path(path, access_key=access_key)
            report["workerIsolation"] = {
                **worker_isolation_capabilities(),
                "enabled": True,
            }
            if report["archiveValid"] and report["modelDescriptionValid"] and report["stationCompatible"]:
                description = fmu_storage.describe_path(path)
                experiment = {
                    "startTime": description.get("defaultStartTime", 0.0),
                    "stopTime": description.get("defaultStopTime", 1.0),
                }
                if description.get("defaultStepSize") is not None:
                    experiment["stepSize"] = description["defaultStepSize"]
                await asyncio.to_thread(
                    process_runner.run,
                    path,
                    access_key=access_key,
                    parameters={},
                    options=experiment,
                    probe_only=True,
                )
                report["executionChecked"] = True
                report["dependenciesChecked"] = True
                report["probeResult"] = "passed"
                report["sha256"] = digest
            else:
                report["probeResult"] = "failed"
        except process_runner.ProcessExecutionError as exc:
            probe_failure_code = exc.code
            infrastructure_failure = exc.code in {
                "FMU_WORKER_ISOLATION_UNAVAILABLE",
                "FMU_WORKER_START_FAILED",
            }
            report["executionChecked"] = not infrastructure_failure
            report["dependenciesChecked"] = not infrastructure_failure
            report["probeResult"] = "failed"
            report["issues"].append(f"Isolated instantiation probe failed ({exc.code})")
        finally:
            engine.release_capacity(token)
    valid = report["archiveValid"] and report["modelDescriptionValid"] and report["stationCompatible"]
    if probe:
        valid = valid and report.get("probeResult") == "passed"
    report["valid"] = valid
    probe_infrastructure_errors = {"FMU_WORKER_ISOLATION_UNAVAILABLE", "FMU_WORKER_START_FAILED"}
    if auto_quarantine and not valid and probe_failure_code not in probe_infrastructure_errors:
        fmu_storage.quarantine(access_key, "; ".join(report["issues"][:5]))
        report["quarantined"] = True
    elif auto_quarantine and probe_failure_code in probe_infrastructure_errors:
        report["quarantined"] = False
        report["quarantineSkippedReason"] = "FMU_PROBE_INFRASTRUCTURE_UNAVAILABLE"
    return report


@app.post("/internal/fmu/quarantine/{access_key:path}", dependencies=[Depends(_check_token)])
async def quarantine_fmu(access_key: str, reason: str = Query("manual")):
    """Quarantine an FMU explicitly."""
    access_key = _validated_access_key(access_key)
    fmu_storage.quarantine(access_key, reason)
    return {"accessKey": access_key, "quarantined": True, "reason": reason}


@app.delete("/internal/fmu/quarantine/{access_key:path}", dependencies=[Depends(_check_token)])
async def unquarantine_fmu(access_key: str):
    """Restore a quarantined FMU."""
    access_key = _validated_access_key(access_key)
    restored = fmu_storage.unquarantine(access_key)
    return {"accessKey": access_key, "restored": restored}


@app.get("/internal/fmu/quarantine", dependencies=[Depends(_check_token)])
async def list_quarantined():
    """List all quarantined FMUs."""
    return {"quarantined": fmu_storage.list_quarantined()}


# ── Simulation run ───────────────────────────────────────────────

class SimulationBody(BaseModel):
    accessKey: str
    claims: dict = Field(default_factory=dict)
    gatewayContext: dict | None = None
    simId: str | None = None
    labId: str | None = None
    reservationKey: str | None = None
    parameters: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)
    inputSchedule: list[dict] = Field(
        default_factory=list,
        max_length=config.MAX_INPUT_SCHEDULE_EVENTS,
    )


class BatchScenario(BaseModel):
    label: str | None = Field(default=None, max_length=80)
    parameters: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)


class BatchSimulationBody(BaseModel):
    accessKey: str
    gatewayContext: dict
    batchId: str | None = None
    options: dict = Field(default_factory=dict)
    scenarios: list[BatchScenario] = Field(min_length=1, max_length=config.MAX_BATCH_CASES)


def _gateway_context_from_request(request: Request) -> dict[str, Any]:
    encoded = request.headers.get("X-Gateway-Context") or ""
    if len(encoded) > 12000:
        raise HTTPException(status_code=400, detail="GATEWAY_CONTEXT_TOO_LARGE")
    try:
        padded = encoded + ("=" * (-len(encoded) % 4))
        context = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED") from exc
    if not isinstance(context, dict):
        raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED")
    return context


def _scope_key(context: dict[str, Any], requested_access_key: str | None = None) -> str:
    try:
        access_key = requested_access_key or auth.extract_access_key_from_context(context)
        if not access_key:
            raise ValueError("gatewayContext has no FMU access key")
        auth.validate_gateway_context(context, _validated_access_key(access_key))
        return reservation_scope(context)[0]
    except (HTTPException, ValueError) as exc:
        raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED") from exc


def _reserve_simulation_scenario(
    context: dict[str, Any] | None,
    access_key: str,
    *,
    require_scope: bool = False,
    model_sha256: str | None = None,
) -> None:
    if not isinstance(context, dict):
        if require_scope:
            raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED")
        return
    claims = context.get("claims") or {}
    if not isinstance(claims, dict):
        claims = {}
    lab_id = context.get("labId") or claims.get("labId")
    reservation_key = context.get("reservationKey") or claims.get("reservationKey")
    if not lab_id or not reservation_key:
        if require_scope:
            raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED")
        return
    try:
        if model_sha256 is None:
            _, model_sha256 = fmu_storage.snapshot_fmu(fmu_storage.get_fmu_path(access_key))
        policy = model_execution_policy(context)
        _simulation_store.reserve_scenarios(
            _scope_key(context, access_key),
            1,
            model_sha256=model_sha256,
            maximum=policy["maxDailyScenarios"],
        )
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "RESERVATION_DAILY_SCENARIO_LIMIT", "remaining": max(0, int(exc.args[0]))},
        ) from exc


def _reserve_input_update(context: dict[str, Any] | None, access_key: str, values: dict[str, Any]) -> None:
    if not isinstance(context, dict):
        raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED")
    scope_key = _scope_key(context, access_key)
    try:
        payload_bytes = len(
            json.dumps(values, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        _simulation_store.reserve_input_update(scope_key, access_key, payload_bytes)
    except InputUpdateQuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": exc.code, "remaining": exc.remaining},
        ) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="INVALID_INPUT_PAYLOAD") from exc


def _validate_backend_options(options: dict[str, Any]) -> None:
    try:
        backends.validate_requested_backend(options)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _strict_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a JSON number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} is outside the numeric range") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _validate_input_schedule(
    input_schedule: list[dict[str, Any]],
    *,
    options: dict[str, Any],
    model_metadata: dict[str, Any],
    fmu_path: Any,
    access_key: str,
) -> list[dict[str, Any]]:
    if not input_schedule:
        return []
    if len(input_schedule) > config.MAX_INPUT_SCHEDULE_EVENTS:
        raise HTTPException(status_code=422, detail="INPUT_SCHEDULE_LIMIT_EXCEEDED")

    try:
        encoded = json.dumps(input_schedule, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="INVALID_INPUT_SCHEDULE") from exc
    if len(encoded.encode("utf-8")) > config.MAX_INPUT_SCHEDULE_BYTES:
        raise HTTPException(status_code=413, detail="INPUT_SCHEDULE_TOO_LARGE")

    try:
        start = float(options.get("startTime", 0.0))
        stop = float(options.get("stopTime", 1.0))
        step = float(options.get("stepSize", model_metadata.get("defaultStepSize") or 0.001))
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail="INVALID_INPUT_SCHEDULE") from exc
    if not all(math.isfinite(value) for value in (start, stop, step)) or step <= 0 or stop <= start:
        raise HTTPException(status_code=422, detail="INVALID_INPUT_SCHEDULE")

    try:
        fmi_major = int(str(model_metadata.get("fmiVersion") or "").split(".", 1)[0])
    except (TypeError, ValueError):
        fmi_major = 0
    simulation_type = str(
        model_metadata.get("simulationType") or model_metadata.get("simulationKind") or ""
    ).lower().replace("_", "").replace("-", "")
    supports_runtime_tunable_parameters = (
        fmi_major in {2, 3} and simulation_type.replace(" ", "") == "cosimulation"
    )
    schedulable_names = {
        item.get("name")
        for item in model_metadata.get("modelVariables", [])
        if item.get("causality") == "input"
        or (
            supports_runtime_tunable_parameters
            and item.get("causality") == "parameter"
            and item.get("variability") == "tunable"
        )
    }
    normalized_schedule: list[dict[str, Any]] = []
    seen_values: set[tuple[float, str]] = set()
    previous_time = start
    for event in input_schedule:
        if not isinstance(event, dict) or set(event) != {"time", "values"}:
            raise HTTPException(status_code=422, detail="INVALID_INPUT_SCHEDULE")
        event_time = event["time"]
        if isinstance(event_time, bool) or not isinstance(event_time, (int, float)):
            raise HTTPException(status_code=422, detail="INVALID_INPUT_SCHEDULE")
        event_time = float(event_time)
        if not math.isfinite(event_time) or event_time < start or event_time >= stop or event_time < previous_time:
            raise HTTPException(status_code=422, detail="INVALID_INPUT_SCHEDULE")
        step_index = round((event_time - start) / step)
        tolerance = max(1e-9, abs(step) * 1e-7)
        if abs(start + step_index * step - event_time) > tolerance:
            raise HTTPException(status_code=422, detail="INPUT_SCHEDULE_TIME_MUST_ALIGN_TO_STEP")
        values = event["values"]
        _validate_parameters(values)
        if not values or any(name not in schedulable_names for name in values):
            raise HTTPException(status_code=422, detail="INPUT_SCHEDULE_CONTAINS_UNSUPPORTED_VARIABLE")
        for name in values:
            identity = (event_time, name)
            if identity in seen_values:
                raise HTTPException(status_code=422, detail="DUPLICATE_SCHEDULED_VARIABLE")
            seen_values.add(identity)
        normalized_schedule.append({"time": event_time, "values": values})
        previous_time = event_time

    validator = engine.FmuSession(
        f"schedule_validation_{secrets.token_hex(6)}",
        fmu_path,
        access_key=access_key,
    )
    try:
        validator.load()
        for event in normalized_schedule:
            try:
                event["values"] = validator.validate_runtime_input_values(event["values"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise HTTPException(status_code=422, detail="INVALID_SCHEDULED_INPUT_VALUE") from exc
    finally:
        validator.terminate()

    return normalized_schedule


def _validate_active_reservation_deadline(context: dict[str, Any]) -> None:
    claims = context.get("claims") or {}
    if not isinstance(claims, dict):
        raise HTTPException(status_code=403, detail="INVALID_RESERVATION_END")
    raw_end = context.get("reservationEndsAt", claims.get("reservationEndsAt"))
    if raw_end is None:
        return
    try:
        end = float(raw_end)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=403, detail="INVALID_RESERVATION_END") from exc
    if not math.isfinite(end):
        raise HTTPException(status_code=403, detail="INVALID_RESERVATION_END")
    if end <= time.time():
        raise HTTPException(status_code=403, detail="RESERVATION_NOT_ACTIVE")


@app.post("/internal/fmu/simulations/run", dependencies=[Depends(_check_token)])
async def run_simulation(body: SimulationBody):
    access_key = _validated_access_key(body.accessKey)
    if body.gatewayContext is not None:
        auth.validate_gateway_context(body.gatewayContext, access_key)
        if config.execution_mode() != "process":
            raise HTTPException(status_code=409, detail="FMU_PROCESS_ISOLATION_REQUIRED")
    if not fmu_storage.fmu_exists(access_key):
        raise HTTPException(404, "FMU_NOT_FOUND")
    _validate_backend_options(body.options)
    _validate_parameters(body.parameters or {})
    policy = model_execution_policy(body.gatewayContext)
    _validate_options(access_key, body.options, policy)
    if body.gatewayContext is not None:
        auth_expiry, reservation_end = execution_deadlines(body.gatewayContext)
    else:
        auth_expiry = reservation_end = None

    source_path = fmu_storage.get_fmu_path(access_key)
    source_report = fmu_storage.inspect_fmu(access_key, include_hash=False)
    if not source_report["archiveValid"] or not source_report["modelDescriptionValid"] or not source_report["stationCompatible"]:
        raise HTTPException(status_code=422, detail={"code": "FMU_NOT_COMPATIBLE", "issues": source_report["issues"][:8]})
    fmu_path, model_sha256 = fmu_storage.snapshot_fmu(source_path)
    report = fmu_storage.inspect_fmu_path(fmu_path, access_key=access_key, include_hash=False)
    if not report["archiveValid"] or not report["modelDescriptionValid"] or not report["stationCompatible"]:
        raise HTTPException(status_code=422, detail={"code": "FMU_NOT_COMPATIBLE", "issues": report["issues"][:8]})
    model_metadata = fmu_storage.describe_path(fmu_path)
    _validate_options(access_key, body.options, policy, model_metadata=model_metadata)
    input_schedule = _validate_input_schedule(
        body.inputSchedule,
        options=body.options,
        model_metadata=model_metadata,
        fmu_path=fmu_path,
        access_key=access_key,
    )
    evidence_options = effective_execution_options(
        model_metadata,
        body.options,
        early_return_default=config.execution_mode() == "process",
    )
    slot: engine.FmuSession | None = None
    capacity_token = None
    try:
        capacity_token = engine.reserve_capacity()
        if config.execution_mode() != "process":
            slot = engine.FmuSession(
                f"run_{secrets.token_hex(6)}", fmu_path, access_key=access_key,
            )
    except engine.CapacityExceededError as exc:
        raise HTTPException(429, "STATION_CAPACITY_EXHAUSTED") from exc
    try:
        if body.gatewayContext is not None:
            await asyncio.to_thread(
                _reserve_simulation_scenario,
                body.gatewayContext,
                access_key,
                require_scope=True,
                model_sha256=model_sha256,
            )
        if config.execution_mode() == "process":
            cancel_event = threading.Event()
            worker_task = asyncio.create_task(
                asyncio.to_thread(
                    process_runner.run,
                    fmu_path,
                    access_key=access_key,
                    parameters=body.parameters or {},
                    options=body.options,
                    cancel_event=cancel_event,
                    authorization_expires_at=auth_expiry,
                    reservation_ends_at=reservation_end,
                    resource_policy=policy,
                    input_schedule=input_schedule,
                )
            )
            try:
                result = await asyncio.shield(worker_task)
            except asyncio.CancelledError:
                cancel_event.set()
                try:
                    await asyncio.shield(worker_task)
                except Exception:
                    pass
                raise
            return attach_run_evidence(
                result,
                model_sha256=model_sha256,
                parameters=body.parameters or {},
                options=evidence_options,
                policy=applied_execution_policy(policy, evidence_options),
                input_schedule=input_schedule,
            )

        if slot is None:
            raise RuntimeError("In-process FMU session was not initialized")
        slot.load()
        start = body.options.get("startTime", 0.0)
        stop = body.options.get("stopTime", 1.0)
        step = body.options.get("stepSize")
        slot.initialize(
            start_time=float(start),
            stop_time=float(stop),
            step_size=float(step) if step is not None else None,
            parameters=body.parameters or None,
            early_return_allowed=body.options.get("earlyReturnAllowed", False),
        )
        if input_schedule:
            for _snapshot in slot.run_until_streaming(
                float(stop),
                step_size=float(step) if step is not None else None,
                input_schedule=input_schedule,
            ):
                pass
            result = {"time": slot._time, "finishReason": slot._finished_reason}
        else:
            result = slot.run_until(float(stop), step_size=float(step) if step is not None else None)
        outputs = slot.get_outputs()
        selected_outputs = outputs.get("outputs", {})
        if body.options.get("outputVariables") is not None:
            selected_outputs = {name: selected_outputs[name] for name in body.options["outputVariables"]}
        result_payload = {
            "type": "sim.result",
            "time": result["time"],
            "state": "terminated",
            "outputs": selected_outputs,
            "outputVariables": list(selected_outputs),
            "finishReason": result.get("finishReason"),
        }
        return attach_run_evidence(
            result_payload,
            model_sha256=model_sha256,
            parameters=body.parameters or {},
            options=evidence_options,
            policy=applied_execution_policy(policy, evidence_options),
            input_schedule=input_schedule,
        )
    except process_runner.ProcessExecutionError as exc:
        status = {
            "FMU_EXECUTION_TIMEOUT": 504,
            "FMU_ARCHIVE_SIZE_LIMIT_EXCEEDED": 413,
            "FMU_SNAPSHOT_HASH_MISMATCH": 409,
            "EXECUTION_AUTHORIZATION_EXPIRED": 403,
            "RESERVATION_ENDED": 403,
            "RESULT_SIZE_LIMIT_EXCEEDED": 413,
            "FMU_WORKER_ISOLATION_UNAVAILABLE": 503,
            "FMU_WORKER_START_FAILED": 503,
            "FMU_PROCESS_ISOLATION_REQUIRED": 409,
        }.get(exc.code, 500)
        raise HTTPException(status_code=status, detail=exc.code) from exc
    finally:
        if slot is not None:
            slot.terminate()
        engine.release_capacity(capacity_token)


@app.post("/internal/fmu/simulations/jobs", status_code=202, dependencies=[Depends(_check_token)])
async def submit_simulation_job(body: SimulationBody):
    if body.inputSchedule:
        raise HTTPException(status_code=422, detail="INPUT_SCHEDULE_NOT_SUPPORTED_FOR_ASYNC_JOBS")
    access_key = _validated_access_key(body.accessKey)
    if not isinstance(body.gatewayContext, dict):
        raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED")
    _scope_key(body.gatewayContext, access_key)
    if not fmu_storage.fmu_exists(access_key):
        raise HTTPException(status_code=404, detail="FMU_NOT_FOUND")
    _validate_backend_options(body.options)
    return await _simulation_jobs.submit_single(
        access_key=access_key,
        gateway_context=body.gatewayContext,
        parameters=body.parameters,
        options=body.options,
        requested_id=body.simId,
    )


@app.post("/internal/fmu/simulations/batches", status_code=202, dependencies=[Depends(_check_token)])
async def submit_simulation_batch(body: BatchSimulationBody):
    access_key = _validated_access_key(body.accessKey)
    _scope_key(body.gatewayContext, access_key)
    if not fmu_storage.fmu_exists(access_key):
        raise HTTPException(status_code=404, detail="FMU_NOT_FOUND")
    _validate_backend_options(body.options)
    for scenario in body.scenarios:
        _validate_backend_options({**body.options, **scenario.options})
    return await _simulation_jobs.submit_batch(
        access_key=access_key,
        gateway_context=body.gatewayContext,
        scenarios=[scenario.model_dump() for scenario in body.scenarios],
        options=body.options,
        requested_id=body.batchId,
    )


@app.get("/internal/fmu/simulations/history", dependencies=[Depends(_check_token)])
async def simulation_history(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=config.MAX_STORED_HISTORY_RECORDS),
):
    scope_key = _scope_key(_gateway_context_from_request(request))
    return _simulation_jobs.history(scope_key, limit=limit, offset=offset)


@app.get("/internal/fmu/simulations/{job_id}/result", dependencies=[Depends(_check_token)])
async def simulation_result(job_id: str, request: Request):
    scope_key = _scope_key(_gateway_context_from_request(request))
    result = _simulation_jobs.result(job_id, scope_key)
    if result is None:
        raise HTTPException(status_code=404, detail="SIMULATION_NOT_FOUND")
    if result["status"] in {"queued", "running", "cancelling"}:
        raise HTTPException(status_code=409, detail={"code": "SIMULATION_NOT_FINISHED", "status": result["status"]})
    return result


@app.get("/internal/fmu/simulations/{job_id}", dependencies=[Depends(_check_token)])
async def simulation_status(job_id: str, request: Request):
    scope_key = _scope_key(_gateway_context_from_request(request))
    status = _simulation_jobs.status(job_id, scope_key)
    if status is None:
        raise HTTPException(status_code=404, detail="SIMULATION_NOT_FOUND")
    return status


@app.post("/internal/fmu/simulations/{job_id}/cancel", dependencies=[Depends(_check_token)])
async def cancel_simulation_job(job_id: str, request: Request):
    scope_key = _scope_key(_gateway_context_from_request(request))
    result = await _simulation_jobs.cancel(job_id, scope_key)
    if result is None:
        raise HTTPException(status_code=404, detail="SIMULATION_NOT_FOUND")
    return result


# ── Simulation stream (NDJSON) ───────────────────────────────────

@app.post("/internal/fmu/simulations/stream", dependencies=[Depends(_check_token)])
async def stream_simulation(body: SimulationBody):
    access_key = _validated_access_key(body.accessKey)
    if body.gatewayContext is not None:
        auth.validate_gateway_context(body.gatewayContext, access_key)
        if config.execution_mode() != "process":
            raise HTTPException(status_code=409, detail="FMU_PROCESS_ISOLATION_REQUIRED")
    if not fmu_storage.fmu_exists(access_key):
        raise HTTPException(404, "FMU_NOT_FOUND")
    _validate_backend_options(body.options)
    _validate_parameters(body.parameters or {})
    policy = model_execution_policy(body.gatewayContext)
    _validate_options(access_key, body.options, policy)
    if body.gatewayContext is not None:
        auth_expiry, reservation_end = execution_deadlines(body.gatewayContext)
    else:
        auth_expiry = reservation_end = None
    source_report = fmu_storage.inspect_fmu(access_key, include_hash=False)
    if not source_report["archiveValid"] or not source_report["modelDescriptionValid"] or not source_report["stationCompatible"]:
        raise HTTPException(status_code=422, detail={"code": "FMU_NOT_COMPATIBLE", "issues": source_report["issues"][:8]})
    fmu_path, model_sha256 = fmu_storage.snapshot_fmu(fmu_storage.get_fmu_path(access_key))
    report = fmu_storage.inspect_fmu_path(fmu_path, access_key=access_key, include_hash=False)
    if not report["archiveValid"] or not report["modelDescriptionValid"] or not report["stationCompatible"]:
        raise HTTPException(status_code=422, detail={"code": "FMU_NOT_COMPATIBLE", "issues": report["issues"][:8]})
    model_metadata = fmu_storage.describe_path(fmu_path)
    _validate_options(access_key, body.options, policy, model_metadata=model_metadata)
    input_schedule = _validate_input_schedule(
        body.inputSchedule,
        options=body.options,
        model_metadata=model_metadata,
        fmu_path=fmu_path,
        access_key=access_key,
    )
    evidence_options = effective_execution_options(
        model_metadata,
        body.options,
        early_return_default=config.execution_mode() == "process",
    )
    slot: engine.FmuSession | None = None
    capacity_token = None
    try:
        capacity_token = engine.reserve_capacity()
        if config.execution_mode() != "process":
            slot = engine.FmuSession(f"stream_{secrets.token_hex(6)}", fmu_path, access_key=access_key)
    except engine.CapacityExceededError as exc:
        raise HTTPException(status_code=429, detail="STATION_CAPACITY_EXHAUSTED") from exc

    try:
        if body.gatewayContext is not None:
            await asyncio.to_thread(
                _reserve_simulation_scenario,
                body.gatewayContext,
                access_key,
                require_scope=True,
                model_sha256=model_sha256,
            )
    except Exception:
        if slot is not None:
            slot.terminate()
        engine.release_capacity(capacity_token)
        raise

    capacity_release_lock = threading.Lock()
    capacity_released = False

    def _release_stream_capacity() -> None:
        nonlocal capacity_released
        with capacity_release_lock:
            if capacity_released:
                return
            capacity_released = True
        try:
            if slot is not None:
                slot.terminate()
        finally:
            engine.release_capacity(capacity_token)

    def _generate():
        stream_digest = hashlib.sha256()

        def encode_line(payload: dict[str, Any]) -> str:
            return json.dumps(payload, default=str, separators=(",", ":"), allow_nan=False) + "\n"

        try:
            if config.execution_mode() == "process":
                for snapshot in process_runner.stream(
                    fmu_path,
                    access_key=access_key,
                    parameters=body.parameters or {},
                    options=body.options,
                    authorization_expires_at=auth_expiry,
                    reservation_ends_at=reservation_end,
                    resource_policy=policy,
                    input_schedule=input_schedule,
                ):
                    if snapshot.get("type") == "sim.done":
                        policy_evidence = applied_execution_policy(policy, evidence_options)
                        evidence = build_execution_evidence(
                            model_sha256=model_sha256,
                            parameters=body.parameters or {},
                            options=evidence_options,
                            result={"ndjsonSha256": stream_digest.hexdigest()},
                            finish_reason=snapshot.get("finishReason") or "completed",
                            policy=policy_evidence,
                            result_sha256=stream_digest.hexdigest(),
                            input_schedule=input_schedule,
                        )
                        snapshot = {**snapshot, "fmuSha256": model_sha256, "evidence": evidence}
                    line = encode_line(snapshot)
                    if snapshot.get("type") not in {"sim.done", "error"}:
                        stream_digest.update(line.encode("utf-8"))
                    yield line
            else:
                if slot is None:
                    raise RuntimeError("In-process FMU session was not initialized")
                slot.load()
                start = body.options.get("startTime", 0.0)
                stop = body.options.get("stopTime", 1.0)
                step = body.options.get("stepSize")
                slot.initialize(
                    start_time=float(start),
                    stop_time=float(stop),
                    step_size=float(step) if step is not None else None,
                    parameters=body.parameters or None,
                    early_return_allowed=body.options.get("earlyReturnAllowed", False),
                )
                for snapshot in slot.run_until_streaming(
                    float(stop),
                    step_size=float(step) if step is not None else None,
                    input_schedule=input_schedule,
                ):
                    line = encode_line(snapshot)
                    stream_digest.update(line.encode("utf-8"))
                    yield line
                final_payload = {
                    "type": "sim.done",
                    "time": float(slot._time),
                    "finishReason": slot._finished_reason,
                }
                final_evidence = build_execution_evidence(
                    model_sha256=model_sha256,
                    parameters=body.parameters or {},
                    options=evidence_options,
                    result={"ndjsonSha256": stream_digest.hexdigest()},
                    finish_reason=slot._finished_reason or "completed",
                    policy=applied_execution_policy(policy, evidence_options),
                    result_sha256=stream_digest.hexdigest(),
                    input_schedule=input_schedule,
                )
                yield encode_line({**final_payload, "fmuSha256": model_sha256, "evidence": final_evidence})
        except process_runner.ProcessExecutionError as exc:
            logger.warning("FMU streaming execution stopped: %s", exc.code)
            yield json.dumps({"type": "error", "code": exc.code, "retryable": exc.retryable}) + "\n"
        except Exception:
            logger.exception("FMU streaming simulation failed")
            yield json.dumps({"type": "error", "message": "FMU simulation failed"}) + "\n"
        finally:
            _release_stream_capacity()

    return StreamingResponse(
        _generate(),
        media_type="application/x-ndjson",
        background=BackgroundTask(_release_stream_capacity),
    )


# ── Realtime WebSocket sessions ──────────────────────────────────

@app.websocket("/internal/fmu/sessions")
async def ws_sessions(ws: WebSocket):
    # Validate internal token from headers (timing-safe comparison)
    token = ws.headers.get("x-internal-session-token") or ""
    internal_token = config.internal_token()
    if not internal_token or not secrets.compare_digest(token, internal_token):
        await ws.close(code=4001, reason="UNAUTHORIZED")
        return

    await ws.accept()
    session: Any | None = None
    _emitter_task: asyncio.Task | None = None
    _runner_task: asyncio.Task | None = None
    _expiry_task: asyncio.Task | None = None
    attachment_owner = object()
    _output_sampler_changed = asyncio.Event()

    async def _stop_runner() -> None:
        nonlocal _runner_task
        task = _runner_task
        _runner_task = None
        if task and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_session() -> None:
        try:
            simulated_origin = session._time if session else 0.0
            wall_origin = time.monotonic()
            last_progress = wall_origin
            progress_interval = config.FMU_PROGRESS_INTERVAL_MS / 1000.0
            while session and session.state == "running" and not session._terminated:
                result = await asyncio.to_thread(session.step)
                now = time.monotonic()
                finished = bool(result.get("finished")) or session.state == "stopped"
                if finished or now - last_progress >= progress_interval:
                    await ws.send_text(json.dumps({
                        "type": "sim.progress",
                        "sessionId": session.session_id,
                        "simTime": session._time,
                        "timeAdvanced": result.get("timeAdvanced", 0.0),
                        "earlyReturn": result.get("earlyReturn", False),
                        "eventOccurred": result.get("eventOccurred", False),
                        "eventHandlingNeeded": result.get("eventHandlingNeeded", False),
                        "terminateSimulation": result.get("terminateSimulation", False),
                    }))
                    last_progress = now
                if finished:
                    await ws.send_text(json.dumps({
                        "type": "sim.state",
                        "sessionId": session.session_id,
                        "state": "stopped",
                        "simTime": session._time,
                        "reason": session._finished_reason,
                    }))
                    return
                if session._time_mode == "realtime":
                    target_wall_time = wall_origin + (session._time - simulated_origin) / session._playback_speed
                    delay = target_wall_time - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    # Keep the event loop responsive while simulating as fast as
                    # the FMU permits; output subscriptions have their own rate cap.
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("FMU realtime runner failed")
            if session and not session._terminated:
                session._state = "error"
                try:
                    await ws.send_text(json.dumps({
                        "type": "sim.state",
                        "sessionId": session.session_id,
                        "state": "error",
                        "simTime": session._time,
                    }))
                except Exception:
                    pass

    def _start_runner() -> None:
        nonlocal _runner_task
        if _runner_task is None or _runner_task.done():
            _runner_task = asyncio.create_task(_run_session())

    async def _expire_session_after_deadline(expiring_session: Any):
        nonlocal session
        try:
            deadlines: list[tuple[float, str]] = []
            if expiring_session.expires_at is not None:
                auth_expiry = float(expiring_session.expires_at)
                if not math.isfinite(auth_expiry):
                    raise ValueError("Authorization expiry must be finite")
                deadlines.append((auth_expiry, "expired"))
            context = getattr(expiring_session, "gateway_context", {}) or {}
            claims = context.get("claims") or {} if isinstance(context, dict) else {}
            reservation_end = context.get("reservationEndsAt") if isinstance(context, dict) else None
            if reservation_end is None and isinstance(claims, dict):
                reservation_end = claims.get("reservationEndsAt")
            if reservation_end is not None:
                reservation_ends_at = float(reservation_end)
                if not math.isfinite(reservation_ends_at):
                    raise ValueError("Reservation end must be finite")
                deadlines.append((reservation_ends_at, "reservation_ended"))
            if not deadlines:
                return
            expires_at, finish_reason = min(deadlines, key=lambda item: item[0])
            await asyncio.sleep(max(0.0, expires_at - time.time()))
            if session is not expiring_session:
                return
            await _stop_runner()
            await asyncio.to_thread(engine.remove_session, expiring_session.session_id)
            session = None
            await ws.send_text(json.dumps({
                "type": "session.closed",
                "sessionId": expiring_session.session_id,
                "reason": finish_reason,
            }))
            await ws.close(code=4003, reason="RESERVATION_ENDED" if finish_reason == "reservation_ended" else "SESSION_EXPIRED")
        except asyncio.CancelledError:
            return
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid FMU session expiry session_id=%s", expiring_session.session_id)
        except Exception:
            logger.debug("Unable to close expired FMU session", exc_info=True)

    async def _output_emitter():
        """Background task that streams subscribed outputs to the WS client."""
        try:
            while True:
                active_session = session
                subscription = active_session.subscription if active_session else None
                if (
                    not active_session
                    or not subscription
                    or not active_session._initialised
                    or active_session._terminated
                ):
                    _output_sampler_changed.clear()
                    await _output_sampler_changed.wait()
                    continue

                wait_seconds = subscription.seconds_until_due()
                read_waiter = getattr(active_session, "output_read_wait_seconds", None)
                if callable(read_waiter):
                    wait_seconds = max(wait_seconds, read_waiter())
                if wait_seconds > 0:
                    try:
                        await asyncio.wait_for(_output_sampler_changed.wait(), timeout=wait_seconds)
                    except asyncio.TimeoutError:
                        pass
                    _output_sampler_changed.clear()
                    continue

                payload = await asyncio.to_thread(active_session.sample_subscription)
                if payload:
                    await ws.send_text(json.dumps(payload, default=str))
        except WebSocketDisconnect:
            logger.debug("Output emitter stopped after WebSocket disconnect")
        except asyncio.CancelledError:
            logger.debug("Output emitter task cancelled")
        except Exception:
            logger.debug("Output emitter stopped", exc_info=True)

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type", "")
            request_id = msg.get("requestId")
            gateway_ctx = msg.get("gatewayContext")

            try:
                response = await _handle_ws_message(
                    msg_type,
                    msg,
                    gateway_ctx,
                    session,
                    start_runner=_start_runner,
                    stop_runner=_stop_runner,
                )
                if msg_type in ("session.create", "session.attach"):
                    bound_session = response.pop("_session", None)
                    if bound_session is not None:
                        if _expiry_task:
                            _expiry_task.cancel()
                            _expiry_task = None
                        session = bound_session
                        bound_session.mark_attached(attachment_owner)
                        gateway_claims = (bound_session.gateway_context or {}).get("claims", {})
                        gateway_reservation_end = (bound_session.gateway_context or {}).get("reservationEndsAt")
                        if gateway_reservation_end is None and isinstance(gateway_claims, dict):
                            gateway_reservation_end = gateway_claims.get("reservationEndsAt")
                        if bound_session.expires_at is not None or gateway_reservation_end is not None:
                            _expiry_task = asyncio.create_task(_expire_session_after_deadline(bound_session))
                        # Start emitter task on session creation and wake it if it already exists.
                        if _emitter_task is None or _emitter_task.done():
                            _emitter_task = asyncio.create_task(_output_emitter())
                        _output_sampler_changed.set()
                elif msg_type == "session.terminate":
                    await _stop_runner()
                    if _expiry_task:
                        _expiry_task.cancel()
                        _expiry_task = None
                    if session:
                        await asyncio.to_thread(engine.remove_session, session.session_id)
                    session = None
                    _output_sampler_changed.set()
                elif msg_type in (
                    "sim.initialize",
                    "sim.reset",
                    "sim.resume",
                    "sim.subscribeOutputs",
                    "sim.unsubscribeOutputs",
                ):
                    _output_sampler_changed.set()

                if request_id:
                    response["requestId"] = request_id
                await ws.send_text(json.dumps(response, default=str))

            except HTTPException as exc:
                detail = exc.detail or ""
                if isinstance(detail, dict):
                    code = str(detail.get("code") or "ERROR")
                    message = code
                    err: dict[str, Any] = {
                        "type": "error",
                        "code": code,
                        "message": detail.get("message") or message,
                        "retryable": bool(detail.get("retryable", False)),
                    }
                    for field in ("remaining", "retryAfterMs"):
                        if field in detail:
                            err[field] = detail.get(field)
                else:
                    # Extract short code from detail strings like "INVALID_COMMAND – ..."
                    code = detail.split(" \u2013 ")[0].split(" - ")[0].strip() if detail else "ERROR"
                    err = {"type": "error", "code": code, "message": detail, "retryable": False}
                if request_id:
                    err["requestId"] = request_id
                await ws.send_text(json.dumps(err))
            except Exception:
                logger.exception("FMU WebSocket command failed")
                err = {
                    "type": "error",
                    "code": "INTERNAL_ERROR",
                    "message": "Internal FMU executor error",
                    "retryable": False,
                }
                if request_id:
                    err["requestId"] = request_id
                await ws.send_text(json.dumps(err))

    except WebSocketDisconnect:
        logger.debug("WebSocket client disconnected")
    finally:
        await _stop_runner()
        if _expiry_task:
            _expiry_task.cancel()
            try:
                await _expiry_task
            except asyncio.CancelledError:
                logger.debug("FMU expiry task cancelled during cleanup")
        if _emitter_task:
            _emitter_task.cancel()
            try:
                await _emitter_task
            except asyncio.CancelledError:
                logger.debug("WebSocket emitter task cancelled during cleanup")
        if session and not session._terminated:
            engine.detach_session(
                session.session_id,
                config.FMU_ATTACH_GRACE_SECONDS,
                attachment_owner=attachment_owner,
            )


def _sim_outputs_payload(
    session: engine.FmuSession,
    outputs: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    next_sequence = getattr(session, "next_sequence", None)
    sequence = next_sequence() if callable(next_sequence) else session.seq
    payload = {
        "type": "sim.outputs",
        "sessionId": session.session_id,
        "seq": sequence,
        "dropped": 0,
        "simTime": session._time,
        "values": outputs,
    }
    if not callable(next_sequence):
        session.seq += 1
    if metadata:
        for key in (
            "requestedTime", "timeAdvanced", "earlyReturn", "eventOccurred", "eventHandlingNeeded",
            "terminateSimulation", "finished", "finishReason", "steps",
        ):
            if key in metadata:
                payload[key] = metadata[key]
    return payload


class _OutputReadRateLimitErrorLike(Protocol):
    retry_after_ms: int


def _output_read_rate_limit_exception(exc: _OutputReadRateLimitErrorLike) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "code": "OUTPUT_READ_RATE_LIMITED",
            "message": str(exc),
            "retryable": True,
            "retryAfterMs": exc.retry_after_ms,
        },
    )


async def _handle_ws_message(
    msg_type: str,
    msg: dict,
    gateway_ctx: dict | None,
    session: Any | None,
    *,
    start_runner=None,
    stop_runner=None,
) -> dict[str, Any]:
    """Dispatch a single WS message and return a response dict."""

    if msg_type == "session.create":
        if session is not None:
            raise HTTPException(400, "INVALID_COMMAND – session already created")
        if gateway_ctx is None:
            raise HTTPException(400, "Missing gatewayContext")

        access_key = auth.extract_access_key_from_context(gateway_ctx)
        if not access_key:
            raise HTTPException(400, "Missing accessKey in gatewayContext")
        access_key = _validated_access_key(access_key)
        auth.validate_gateway_context(gateway_ctx, access_key)
        _validate_active_reservation_deadline(gateway_ctx)
        if config.execution_mode() != "process":
            raise HTTPException(status_code=409, detail="FMU_PROCESS_ISOLATION_REQUIRED")

        if not fmu_storage.fmu_exists(access_key):
            raise HTTPException(404, "FMU_NOT_FOUND")
        source_report = fmu_storage.inspect_fmu(access_key, include_hash=False)
        if not source_report["archiveValid"] or not source_report["modelDescriptionValid"] or not source_report["stationCompatible"]:
            raise HTTPException(status_code=422, detail={"code": "FMU_NOT_COMPATIBLE", "issues": source_report["issues"][:8]})
        fmu_path, model_sha256 = fmu_storage.snapshot_fmu(fmu_storage.get_fmu_path(access_key))
        report = fmu_storage.inspect_fmu_path(fmu_path, access_key=access_key, include_hash=False)
        if not report["archiveValid"] or not report["modelDescriptionValid"] or not report["stationCompatible"]:
            raise HTTPException(status_code=422, detail={"code": "FMU_NOT_COMPATIBLE", "issues": report["issues"][:8]})
        claims = (gateway_ctx.get("claims") or {})
        exp = claims.get("exp")
        policy = model_execution_policy(gateway_ctx)
        try:
            session_factory = (
                process_runner.RealtimeSession
                if config.execution_mode() == "process"
                else engine.FmuSession
            )
            new_session = await asyncio.to_thread(
                engine.create_session,
                fmu_path,
                access_key=access_key,
                expires_at=exp,
                gateway_context=gateway_ctx,
                session_factory=session_factory,
                resource_policy=policy if config.execution_mode() == "process" else None,
            )
        except engine.CapacityExceededError as exc:
            raise HTTPException(429, "STATION_CAPACITY_EXHAUSTED") from exc
        new_session.fmu_sha256 = model_sha256
        try:
            await asyncio.to_thread(new_session.load)
        except Exception:
            await asyncio.to_thread(engine.remove_session, new_session.session_id)
            raise

        return {
            "type": "session.created",
            "sessionId": new_session.session_id,
            "serverTime": time.time(),
            "expiresAt": new_session.expires_at,
            "fmuSha256": model_sha256,
            "capabilities": {
                "modelDescribe": True,
                "getState": True,
                "start": True,
                "pause": True,
                "resume": True,
                "reset": True,
                "step": True,
                "setInputs": True,
                "streamOutputs": True,
                "advanceSamples": True,
                "maxOutputHz": config.MAX_OUTPUT_SUBSCRIPTION_HZ,
                "timeMode": ["simtime", "realtime", "external"],
            },
            "_session": new_session,
        }

    if msg_type == "session.attach":
        requested_session_id = str(msg.get("sessionId") or "").strip()
        if session is None:
            if not requested_session_id:
                raise HTTPException(400, "INVALID_COMMAND – missing sessionId")
            if not isinstance(gateway_ctx, dict):
                raise HTTPException(400, "Missing gatewayContext")
            attached_session = engine.get_attachable_session(requested_session_id)
            if attached_session is None:
                raise HTTPException(400, "INVALID_COMMAND – no active session")
        else:
            if requested_session_id and requested_session_id != session.session_id:
                raise HTTPException(403, "FORBIDDEN – sessionId mismatch")
            attached_session = engine.get_attachable_session(session.session_id)
            if attached_session is None:
                raise HTTPException(401, "SESSION_EXPIRED")

        attach_context = (
            attached_session.gateway_context
            if session is not None and gateway_ctx is None
            else gateway_ctx
        )
        if not isinstance(attach_context, dict):
            raise HTTPException(400, "Missing gatewayContext")
        attach_access_key = auth.extract_access_key_from_context(attach_context)
        if not attach_access_key:
            raise HTTPException(400, "Missing accessKey in gatewayContext")
        auth.validate_session_context(
            attach_context,
            attached_session.gateway_context,
            attached_session.access_key,
        )
        if attach_access_key != attached_session.access_key:
            raise HTTPException(403, "FORBIDDEN – accessKey mismatch")
        return {
            "type": "session.attached",
            "sessionId": attached_session.session_id,
            "serverTime": time.time(),
            "expiresAt": attached_session.expires_at,
            "state": attached_session.state,
            "_session": attached_session,
        }

    # All remaining commands require a live session
    if session is None:
        raise HTTPException(400, "INVALID_COMMAND – no active session")

    # Always validate the context captured at session creation. A later
    # command must not be able to omit or replace exp with a non-expiring
    # context while reusing the already-created FMU session.
    auth.validate_gateway_context(session.gateway_context, session.access_key)
    command_context = gateway_ctx or session.gateway_context
    access_key = auth.extract_access_key_from_context(command_context)
    if not access_key:
        raise HTTPException(400, "Missing accessKey in gatewayContext")
    access_key = _validated_access_key(access_key)
    if access_key != session.access_key:
        raise HTTPException(403, "FORBIDDEN – accessKey mismatch")
    if gateway_ctx is not None:
        auth.validate_gateway_context(command_context, access_key)

    if msg_type == "model.describe":
        include_variables = msg.get("includeVariables", True)
        if type(include_variables) is not bool:
            raise HTTPException(400, "INVALID_COMMAND - includeVariables must be boolean")
        desc = await asyncio.to_thread(session.load)
        if not include_variables:
            desc = {key: value for key, value in desc.items() if key != "modelVariables"}
        return {"type": "model.description", "sessionId": session.session_id, **desc}

    if msg_type == "sim.initialize":
        options = msg.get("options", {})
        root_params = msg.get("parameters", {})
        if not isinstance(options, dict) or not isinstance(root_params, dict):
            raise HTTPException(status_code=422, detail="INVALID_SIMULATION_REQUEST")
        option_params = options.get("inputs", root_params)
        if not isinstance(option_params, dict) or (root_params and option_params != root_params):
            raise HTTPException(status_code=422, detail="INVALID_SIMULATION_REQUEST - conflicting inputs")
        params = option_params
        time_mode = options.get("timeMode", "simtime")
        if not isinstance(time_mode, str) or time_mode not in {"simtime", "realtime", "external"}:
            raise HTTPException(status_code=400, detail="INVALID_TIME_MODE")
        try:
            start_time = _strict_number(options.get("startTime", 0.0), "startTime")
            stop_time = _strict_number(options.get("stopTime", 1.0), "stopTime")
            step_value = _strict_number(options["stepSize"], "stepSize") if options.get("stepSize") is not None else None
            playback_speed = _strict_number(options.get("playbackSpeed", 1.0), "playbackSpeed")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="INVALID_SIMULATION_OPTIONS") from exc
        effective_step = step_value if step_value is not None else (
            float(session._md.defaultExperiment.stepSize)
            if session._md and session._md.defaultExperiment and session._md.defaultExperiment.stepSize
            else 0.001
        )
        if (
            not all(math.isfinite(value) for value in (start_time, stop_time, effective_step, playback_speed))
            or stop_time <= start_time
            or not math.isfinite(stop_time - start_time)
            or effective_step <= 0
            or not 0.1 <= playback_speed <= 100.0
        ):
            raise HTTPException(status_code=400, detail="INVALID_SIMULATION_OPTIONS")
        early_return_allowed = options.get("earlyReturnAllowed", False)
        if type(early_return_allowed) is not bool:
            raise HTTPException(status_code=400, detail="INVALID_SIMULATION_OPTIONS")
        try:
            await asyncio.to_thread(session.validate_initial_values, params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"INVALID_INPUT - {exc}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=f"INVALID_SESSION_STATE - {exc}") from exc
        await asyncio.to_thread(
            _reserve_simulation_scenario,
            session.gateway_context,
            access_key,
            model_sha256=getattr(session, "fmu_sha256", None),
        )
        if stop_runner is not None:
            await stop_runner()
        try:
            await asyncio.to_thread(
                session.initialize,
                start_time=start_time,
                stop_time=stop_time,
                step_size=step_value,
                parameters=params or None,
                time_mode=time_mode,
                playback_speed=playback_speed,
                early_return_allowed=early_return_allowed,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"INVALID_SIMULATION_OPTIONS - {exc}") from exc
        return {
            "type": "sim.state",
            "sessionId": session.session_id,
            "state": session.state,
            "simTime": session._time,
            "timeMode": session._time_mode,
            "playbackSpeed": session._playback_speed,
        }

    if msg_type == "sim.start":
        if session._time_mode == "external":
            raise HTTPException(409, "EXTERNAL_STEP_MODE")
        if session.state not in ("initialized", "paused"):
            raise HTTPException(409, f"Cannot start from state {session.state}")
        await asyncio.to_thread(session.resume)
        if start_runner is not None:
            start_runner()
        return {
            "type": "sim.state",
            "sessionId": session.session_id,
            "state": session.state,
            "simTime": session._time,
        }

    if msg_type == "sim.pause":
        if stop_runner is not None:
            await stop_runner()
        await asyncio.to_thread(session.pause)
        return {
            "type": "sim.state",
            "sessionId": session.session_id,
            "state": session.state,
            "simTime": session._time,
        }

    if msg_type == "sim.resume":
        if session._time_mode == "external":
            raise HTTPException(409, "EXTERNAL_STEP_MODE")
        await asyncio.to_thread(session.resume)
        if start_runner is not None:
            start_runner()
        return {
            "type": "sim.state",
            "sessionId": session.session_id,
            "state": session.state,
            "simTime": session._time,
        }

    if msg_type == "sim.reset":
        defer_initialization = msg.get("deferInitialization", False)
        if type(defer_initialization) is not bool:
            raise HTTPException(400, "INVALID_RESET_OPTIONS")
        if not defer_initialization:
            await asyncio.to_thread(
                _reserve_simulation_scenario,
                session.gateway_context,
                access_key,
                model_sha256=getattr(session, "fmu_sha256", None),
            )
        if stop_runner is not None:
            await stop_runner()
        try:
            await asyncio.to_thread(session.reset, defer_initialization=defer_initialization)
        except RuntimeError as exc:
            raise HTTPException(409, f"INVALID_SESSION_STATE - {exc}") from exc
        except ValueError as exc:
            raise HTTPException(400, f"INVALID_RESET_OPTIONS - {exc}") from exc
        return {
            "type": "sim.state",
            "sessionId": session.session_id,
            "state": session.state,
            "simTime": session._time,
        }

    if msg_type == "sim.step":
        step_size = msg.get("deltaT", msg.get("stepSize"))
        try:
            current_point = msg.get("currentCommunicationPoint", msg.get("currentTime"))
            if session._time_mode == "external" and current_point is None:
                raise ValueError("currentCommunicationPoint is required in external-step mode")
            if current_point is not None:
                requested_current_time = _strict_number(current_point, "currentCommunicationPoint")
                tolerance = max(1e-12, abs(session._time) * 1e-12)
                if abs(requested_current_time - session._time) > tolerance:
                    raise ValueError("currentCommunicationPoint does not match the session time")
            delta = _strict_number(step_size, "deltaT") if step_size is not None else None
            step_with_outputs = getattr(session, "step_with_outputs", None)
            if callable(step_with_outputs):
                step_with_outputs_fn = cast(
                    Callable[[float | None], tuple[dict[str, Any], dict[str, Any]]],
                    step_with_outputs,
                )
                step_result, outputs = await asyncio.to_thread(step_with_outputs_fn, delta)
            else:
                step_result = await asyncio.to_thread(session.step, delta)
                outputs = await asyncio.to_thread(session.get_outputs)
        except process_runner.OutputReadRateLimitError as exc:
            raise _output_read_rate_limit_exception(exc) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"INVALID_STEP - {exc}") from exc
        except RuntimeError as exc:
            raise HTTPException(409, f"INVALID_SESSION_STATE - {exc}") from exc
        return _sim_outputs_payload(session, outputs["outputs"], step_result)

    if msg_type == "sim.advance":
        try:
            target_time = _strict_number(msg.get("targetTime"), "targetTime")
            step_size = _strict_number(msg["stepSize"], "stepSize") if msg.get("stepSize") is not None else None
            max_samples = msg.get("maxSamples", 256)
            if type(max_samples) is not int or not 1 <= max_samples <= 256:
                raise ValueError("maxSamples must be an integer between 1 and 256")
            if session._time_mode == "external":
                raise ValueError("sim.advance requires simtime or realtime mode")
            result = await asyncio.to_thread(session.advance, target_time, max_samples=max_samples, step_size=step_size)
        except process_runner.OutputReadRateLimitError as exc:
            raise _output_read_rate_limit_exception(exc) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"INVALID_ADVANCE - {exc}") from exc
        except RuntimeError as exc:
            raise HTTPException(409, f"INVALID_SESSION_STATE - {exc}") from exc
        return {"type": "sim.samples", "sessionId": session.session_id, **result}

    if msg_type == "sim.runUntil":
        target = msg.get("time", msg.get("targetTime"))
        if target is None:
            raise HTTPException(400, "INVALID_COMMAND – missing targetTime")
        if session._time_mode == "external":
            raise HTTPException(409, "EXTERNAL_STEP_MODE")
        step_size = msg.get("stepSize", msg.get("deltaT"))
        try:
            target_time = _strict_number(target, "targetTime")
            requested_step_size = (
                _strict_number(step_size, "stepSize")
                if step_size is not None
                else None
            )
            run_until_with_outputs = getattr(session, "run_until_with_outputs", None)
            if callable(run_until_with_outputs):
                run_until_with_outputs_fn = cast(
                    Callable[[float, float | None], tuple[dict[str, Any], dict[str, Any]]],
                    run_until_with_outputs,
                )
                run_result, outputs = await asyncio.to_thread(
                    run_until_with_outputs_fn,
                    target_time,
                    requested_step_size,
                )
            else:
                run_result = await asyncio.to_thread(
                    session.run_until,
                    target_time,
                    step_size=requested_step_size,
                )
                outputs = await asyncio.to_thread(session.get_outputs)
        except process_runner.OutputReadRateLimitError as exc:
            raise _output_read_rate_limit_exception(exc) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"INVALID_RUN_UNTIL - {exc}") from exc
        except RuntimeError as exc:
            raise HTTPException(409, f"INVALID_SESSION_STATE - {exc}") from exc
        return _sim_outputs_payload(session, outputs["outputs"], run_result)

    if msg_type == "sim.setInputs":
        values = msg.get("values", {})
        if not isinstance(values, dict):
            raise HTTPException(400, "sim.setInputs requires an object 'values'")
        if values:
            try:
                await asyncio.to_thread(session.validate_inputs, values)
            except ValueError as exc:
                raise HTTPException(400, f"INVALID_INPUT - {exc}") from exc
            except RuntimeError as exc:
                raise HTTPException(409, f"INVALID_SESSION_STATE - {exc}") from exc
            await asyncio.to_thread(_reserve_input_update, session.gateway_context, access_key, values)
        try:
            await asyncio.to_thread(session.set_inputs, values)
        except ValueError as exc:
            raise HTTPException(400, f"INVALID_INPUT - {exc}") from exc
        except RuntimeError as exc:
            raise HTTPException(409, f"INVALID_SESSION_STATE - {exc}") from exc
        return {
            "type": "sim.inputs.updated",
            "sessionId": session.session_id,
            "simTime": session._time,
        }

    if msg_type == "sim.getOutputs":
        variables = msg.get("variables")
        if variables is not None:
            if not isinstance(variables, list):
                raise HTTPException(400, "sim.getOutputs requires 'variables' as array")
            model_description = session._md
            if model_description is None:
                raise HTTPException(409, "MODEL_DESCRIPTION_NOT_LOADED")
            output_names = {
                variable.name for variable in model_description.modelVariables
                if variable.causality == "output"
            }
            if any(not isinstance(name, str) for name in variables) or set(variables) - output_names:
                raise HTTPException(400, "UNKNOWN_OUTPUT_VARIABLE")
            selected = set(variables)
            refs = [
                int(variable.valueReference)
                for variable in model_description.modelVariables
                if variable.name in selected and variable.causality == "output"
            ]
        else:
            refs = msg.get("valueReferences")
            if refs is not None and (
                not isinstance(refs, list)
                or any(isinstance(ref, bool) or not isinstance(ref, int) for ref in refs)
            ):
                raise HTTPException(400, "INVALID_VALUE_REFERENCES")
        limited_get_outputs = getattr(session, "get_outputs_limited", None)
        try:
            if callable(limited_get_outputs):
                get_outputs_limited_fn = cast(
                    Callable[[list[int] | None], dict[str, Any]],
                    limited_get_outputs,
                )
                result = await asyncio.to_thread(get_outputs_limited_fn, refs)
            else:
                result = await asyncio.to_thread(session.get_outputs, refs)
        except process_runner.OutputReadRateLimitError as exc:
            raise _output_read_rate_limit_exception(exc) from exc
        return _sim_outputs_payload(session, result["outputs"])

    if msg_type == "sim.subscribeOutputs":
        variables = msg.get("variables")
        if variables is not None and not isinstance(variables, list):
            raise HTTPException(400, "sim.subscribeOutputs requires 'variables' as array")
        if variables is not None:
            output_names = {
                variable.name for variable in (session._md.modelVariables if session._md else [])
                if variable.causality == "output"
            }
            if any(not isinstance(name, str) for name in variables) or set(variables) - output_names:
                raise HTTPException(400, "UNKNOWN_OUTPUT_VARIABLE")
        try:
            period_value = msg.get("periodMs", 100)
            batch_value = msg.get("maxBatchSize", 64)
            if isinstance(period_value, bool) or not isinstance(period_value, int):
                raise ValueError("periodMs must be an integer")
            if isinstance(batch_value, bool) or not isinstance(batch_value, int):
                raise ValueError("maxBatchSize must be an integer")
            period_ms = period_value
            max_batch_size = batch_value
            max_hz = (
                _strict_number(msg["maxHz"], "maxHz")
                if msg.get("maxHz") is not None
                else float(config.MAX_OUTPUT_SUBSCRIPTION_HZ)
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "INVALID_OUTPUT_SUBSCRIPTION") from exc
        if (
            not 1 <= period_ms <= 60000
            or not 1 <= max_batch_size <= config.MAX_OUTPUT_SUBSCRIPTION_BATCH
            or (max_hz is not None and (not math.isfinite(max_hz) or not 0 < max_hz <= config.MAX_OUTPUT_SUBSCRIPTION_HZ))
        ):
            raise HTTPException(400, "INVALID_OUTPUT_SUBSCRIPTION")
        subscription = engine.OutputSubscription(
            variables=variables,
            period_ms=period_ms,
            max_batch_size=max_batch_size,
            max_hz=max_hz,
        )
        session.subscription = subscription
        return {
            "type": "sim.subscribed",
            "sessionId": session.session_id,
            "periodMs": subscription.period_ms,
            "maxBatchSize": subscription.max_batch_size,
            "maxHz": subscription.max_hz,
        }

    if msg_type == "sim.unsubscribeOutputs":
        session.subscription = None
        session._pending_samples.clear()
        return {
            "type": "sim.unsubscribed",
            "sessionId": session.session_id,
        }

    if msg_type == "sim.getState":
        return {
            "type": "sim.state",
            "sessionId": session.session_id,
            "state": session.state,
            "simTime": session._time,
        }

    if msg_type in ("session.ping", "ping"):
        return {
            "type": "session.pong",
            "sessionId": session.session_id,
            "serverTime": time.time(),
        }

    if msg_type == "session.terminate":
        await asyncio.to_thread(session.terminate)
        return {
            "type": "session.closed",
            "sessionId": session.session_id,
            "reason": "client_terminated",
        }

    raise HTTPException(400, f"INVALID_COMMAND – unknown type {msg_type!r}")
