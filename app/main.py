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
import posixpath
import re
import secrets
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
import zlib
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
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
from .session_api import create_session_router
from .session_protocol import (
    SessionCommandServices,
    handle_ws_message as _dispatch_session_command,
)
from .simulation_defaults import resolve_simulation_time_options

logger = logging.getLogger(__name__)

app = FastAPI(title="FMU Executor", version="0.3.2", docs_url=None, redoc_url=None)
MAX_COMPANION_ASSET_BYTES = 50 * 1024 * 1024
MAX_FMI_LS_REF_MANIFEST_BYTES = 1024 * 1024
FMI_LS_REF_MANIFEST_PATH = "extra/org.fmi-standard.fmi-ls-ref/fmi-ls-manifest.xml"
FMI_LS_REF_NAME = "org.fmi-standard.fmi-ls-ref"
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
    """Return a declared or same-stem digital-twin asset to the Gateway."""
    access_key = _validated_access_key(access_key)
    asset_type = asset_type.strip().lower()
    if asset_type not in {"auto", "glb", "threejs-app"}:
        raise HTTPException(status_code=400, detail="INVALID_COMPANION_ASSET_TYPE")
    try:
        root = config.FMU_ROOT.resolve(strict=True)
        fmu_path = fmu_storage.get_fmu_path(access_key).resolve(strict=True)
        fmu_path.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="COMPANION_ASSET_NOT_FOUND") from exc

    content = None
    source = "legacy-sibling"
    selected_type = asset_type
    if asset_type == "auto":
        declared_asset = _read_fmi_ls_ref_visual_asset(fmu_path)
        if declared_asset is not None and declared_asset[1] == "threejs-app":
            content, selected_type = declared_asset
            source = "fmi-ls-ref"
        else:
            app_path = _legacy_companion_path(fmu_path, "threejs-app", root)
            if app_path is not None:
                content = _read_companion_file(app_path)
                selected_type = "threejs-app"
            elif declared_asset is not None:
                content, selected_type = declared_asset
                source = "fmi-ls-ref"
            else:
                glb_path = _legacy_companion_path(fmu_path, "glb", root)
                if glb_path is not None:
                    content = _read_companion_file(glb_path)
                    selected_type = "glb"
    else:
        asset_path = _legacy_companion_path(fmu_path, asset_type, root)
        if asset_path is not None:
            content = _read_companion_file(asset_path)

    if content is None:
        raise HTTPException(status_code=404, detail="COMPANION_ASSET_NOT_FOUND")

    media_type = "model/gltf-binary" if selected_type == "glb" else "application/zip"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Digital-Twin-Asset-Type": selected_type,
            "X-Digital-Twin-Asset-Source": source,
        },
    )


def _normalized_ref_source(source: str) -> str | None:
    """Resolve a relative FMI-LS-REF URI to a safe path inside the FMU."""
    if not source or len(source) > 2048 or "\\" in source or "\x00" in source:
        return None
    parsed = urlsplit(source)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not parsed.path:
        return None
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or "\x00" in decoded_path or decoded_path.startswith("/"):
        return None
    normalized = posixpath.normpath(posixpath.join(
        posixpath.dirname(FMI_LS_REF_MANIFEST_PATH),
        decoded_path,
    ))
    if normalized in {"", ".", ".."} or normalized.startswith("../") or normalized.startswith("/"):
        return None
    return normalized


def _read_fmi_ls_ref_visual_asset(fmu_path) -> tuple[bytes, str] | None:
    """Read the preferred local visual asset declared by an FMI-LS-REF manifest."""
    try:
        with zipfile.ZipFile(fmu_path) as archive:
            manifest_info = archive.getinfo(FMI_LS_REF_MANIFEST_PATH)
            if manifest_info.file_size > MAX_FMI_LS_REF_MANIFEST_BYTES:
                return None
            with archive.open(manifest_info) as source_file:
                manifest = source_file.read(MAX_FMI_LS_REF_MANIFEST_BYTES + 1)
            if len(manifest) > MAX_FMI_LS_REF_MANIFEST_BYTES:
                return None
            lowered_manifest = manifest.lower()
            if b"<!doctype" in lowered_manifest or b"<!entity" in lowered_manifest:
                return None
            root = ET.fromstring(manifest)
            if root.tag.rsplit("}", 1)[-1] != "fmiReferences":
                return None
            fmi_ls_namespace = "http://fmi-standard.org/fmi-ls-manifest"
            if root.attrib.get(f"{{{fmi_ls_namespace}}}fmi-ls-name") != FMI_LS_REF_NAME:
                return None

            candidates: list[tuple[str, str]] = []
            for related in root:
                if related.tag.rsplit("}", 1)[-1] != "Related":
                    continue
                archive_path = _normalized_ref_source(str(related.attrib.get("source") or ""))
                if archive_path is None:
                    continue
                media_type = str(related.attrib.get("type") or "").split(";", 1)[0].strip().lower()
                lowered_path = archive_path.lower()
                if lowered_path.endswith(".webapp.zip") and media_type in {"application/zip", "application/x-zip-compressed"}:
                    candidates.append((archive_path, "threejs-app"))
                elif lowered_path.endswith(".glb") and media_type in {"model/gltf-binary", "application/octet-stream"}:
                    candidates.append((archive_path, "glb"))

            candidates.sort(key=lambda candidate: candidate[1] != "threejs-app")
            names = archive.namelist()
            for archive_path, candidate_type in candidates:
                if names.count(archive_path) != 1:
                    continue
                asset_info = archive.getinfo(archive_path)
                if asset_info.is_dir() or asset_info.file_size > MAX_COMPANION_ASSET_BYTES:
                    continue
                with archive.open(asset_info) as asset_file:
                    content = asset_file.read(MAX_COMPANION_ASSET_BYTES + 1)
                if len(content) <= MAX_COMPANION_ASSET_BYTES:
                    return content, candidate_type
    except (OSError, KeyError, ValueError, ET.ParseError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error):
        return None
    return None


def _legacy_companion_path(fmu_path, asset_type: str, root):
    suffix = ".glb" if asset_type == "glb" else ".webapp.zip"
    try:
        asset_path = fmu_path.with_name(fmu_path.stem + suffix).resolve(strict=True)
        asset_path.relative_to(root)
    except (FileNotFoundError, ValueError):
        return None
    if not asset_path.is_file():
        return None
    return asset_path


def _read_companion_file(asset_path) -> bytes:
    try:
        with asset_path.open("rb") as asset_file:
            content = asset_file.read(MAX_COMPANION_ASSET_BYTES + 1)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="COMPANION_ASSET_UNAVAILABLE") from exc
    if len(content) > MAX_COMPANION_ASSET_BYTES:
        raise HTTPException(status_code=413, detail="COMPANION_ASSET_TOO_LARGE")
    return content


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
                experiment = resolve_simulation_time_options(description)
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
        policy = model_execution_policy(context)
        _simulation_store.reserve_input_update(
            scope_key,
            access_key,
            payload_bytes,
            maximum_window_count=policy["maxInputUpdatesPerWindow"],
            window_seconds=config.INPUT_UPDATE_WINDOW_SECONDS,
        )
    except InputUpdateQuotaExceededError as exc:
        raise _input_update_quota_exception(exc) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="INVALID_INPUT_PAYLOAD") from exc


def _reserve_input_updates(
    context: dict[str, Any] | None,
    access_key: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(context, dict):
        raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED")
    scope_key = _scope_key(context, access_key)
    try:
        payload_sizes = [
            len(json.dumps(event["values"], separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
            for event in events
        ]
        policy = model_execution_policy(context)
        return _simulation_store.reserve_input_updates(
            scope_key,
            access_key,
            payload_sizes,
            maximum_window_count=policy["maxInputUpdatesPerWindow"],
            window_seconds=config.INPUT_UPDATE_WINDOW_SECONDS,
        )
    except InputUpdateQuotaExceededError as exc:
        raise _input_update_quota_exception(exc) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="INVALID_INPUT_PAYLOAD") from exc


def _settle_input_updates(
    context: dict[str, Any] | None,
    access_key: str,
    reservation_id: str,
    applied_count: int,
) -> None:
    if not isinstance(context, dict):
        raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED")
    scope_key = _scope_key(context, access_key)
    try:
        _simulation_store.settle_input_updates(
            scope_key,
            access_key,
            reservation_id,
            applied_count=applied_count,
            maximum_window_count=model_execution_policy(context)["maxInputUpdatesPerWindow"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="INPUT_UPDATE_RESERVATION_INVALID") from exc


def _input_update_quota_exception(exc: InputUpdateQuotaExceededError) -> HTTPException:
    detail: dict[str, Any] = {
        "code": exc.code,
        "remaining": exc.remaining,
        "retryable": exc.retry_after_ms is not None,
    }
    if exc.retry_after_ms is not None:
        detail["retryAfterMs"] = exc.retry_after_ms
    if exc.max_batch_size is not None:
        detail["maxBatchSize"] = exc.max_batch_size
    return HTTPException(status_code=429, detail=detail)


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
        resolved_options = resolve_simulation_time_options(model_metadata, options)
        start = float(resolved_options["startTime"])
        stop = float(resolved_options["stopTime"])
        step = float(resolved_options["stepSize"])
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
                    options=evidence_options,
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
        start = evidence_options["startTime"]
        stop = evidence_options["stopTime"]
        step = evidence_options["stepSize"]
        slot.initialize(
            start_time=start,
            stop_time=stop,
            step_size=step,
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
                    options=evidence_options,
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
                start = evidence_options["startTime"]
                stop = evidence_options["stopTime"]
                step = evidence_options["stepSize"]
                slot.initialize(
                    start_time=start,
                    stop_time=stop,
                    step_size=step,
                    parameters=body.parameters or None,
                    early_return_allowed=body.options.get("earlyReturnAllowed", False),
                )
                for snapshot in slot.run_until_streaming(
                    stop,
                    step_size=step,
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

async def _handle_ws_message(
    msg_type: str,
    msg: dict,
    gateway_ctx: dict | None,
    session: Any | None,
    *,
    start_runner=None,
    stop_runner=None,
) -> dict[str, Any]:
    """Keep the testable main-module entry point while isolating protocol logic."""
    services = SessionCommandServices(
        validate_access_key=_validated_access_key,
        validate_active_reservation_deadline=_validate_active_reservation_deadline,
        model_execution_policy=model_execution_policy,
        reserve_simulation_scenario=_reserve_simulation_scenario,
        reserve_input_update=_reserve_input_update,
        reserve_input_updates=_reserve_input_updates,
        settle_input_updates=_settle_input_updates,
        validate_input_schedule=_validate_input_schedule,
        strict_number=_strict_number,
    )
    return await _dispatch_session_command(
        msg_type,
        msg,
        gateway_ctx,
        session,
        services=services,
        start_runner=start_runner,
        stop_runner=stop_runner,
    )


app.include_router(create_session_router(_handle_ws_message))
