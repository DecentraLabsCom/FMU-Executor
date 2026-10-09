"""FMU Executor – FastAPI application.

Internal-only service that runs on Lab Station, providing the FMU execution plane
consumed by Lab Gateway's fmu-runner in ``station`` backend mode.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from typing import Any

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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import auth, backends, config, engine, fmu_storage, process_runner
from .simulation_jobs import SimulationJobManager
from .simulation_store import QuotaExceededError, SimulationStore, reservation_scope

logger = logging.getLogger(__name__)

app = FastAPI(title="FMU Executor", version="0.2.1", docs_url=None, redoc_url=None)
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
    await asyncio.to_thread(_simulation_store.initialize)
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


# ── Describe ─────────────────────────────────────────────────────

@app.get("/internal/fmu/describe", dependencies=[Depends(_check_token)])
async def describe(access_key: str = Header(..., alias="X-FMU-Access-Key")):
    access_key = _validated_access_key(access_key)
    if not fmu_storage.fmu_exists(access_key):
        raise HTTPException(404, "FMU_NOT_FOUND")
    return fmu_storage.describe(access_key)


# ── Validate / Quarantine ────────────────────────────────────────

@app.post("/internal/fmu/validate/{access_key:path}", dependencies=[Depends(_check_token)])
async def validate_fmu(access_key: str, auto_quarantine: bool = Query(False)):
    """Validate an FMU and optionally quarantine it if broken."""
    access_key = _validated_access_key(access_key)
    ok, reason = fmu_storage.validate_fmu(access_key)
    if not ok and auto_quarantine:
        fmu_storage.quarantine(access_key, reason)
    return {"accessKey": access_key, "valid": ok, "reason": reason}


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
) -> None:
    if isinstance(context, dict):
        claims = context.get("claims") or {}
        if not isinstance(claims, dict):
            claims = {}
        lab_id = context.get("labId") or claims.get("labId")
        reservation_key = context.get("reservationKey") or claims.get("reservationKey")
    else:
        lab_id = reservation_key = None
    if not lab_id or not reservation_key:
        if require_scope:
            raise HTTPException(status_code=403, detail="RESERVATION_SCOPE_REQUIRED")
        return
    try:
        _simulation_store.reserve_scenarios(_scope_key(context, access_key), 1)
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "RESERVATION_DAILY_SCENARIO_LIMIT", "remaining": max(0, int(exc.args[0]))},
        ) from exc


def _validate_backend_options(options: dict[str, Any]) -> None:
    try:
        backends.validate_requested_backend(options)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/internal/fmu/simulations/run", dependencies=[Depends(_check_token)])
async def run_simulation(body: SimulationBody):
    access_key = _validated_access_key(body.accessKey)
    if body.gatewayContext is not None:
        auth.validate_gateway_context(body.gatewayContext, access_key)
        await asyncio.to_thread(_reserve_simulation_scenario, body.gatewayContext, access_key, require_scope=True)
    if not fmu_storage.fmu_exists(access_key):
        raise HTTPException(404, "FMU_NOT_FOUND")
    _validate_backend_options(body.options)

    fmu_path = fmu_storage.get_fmu_path(access_key)
    try:
        slot = engine.create_session(fmu_path)
    except engine.CapacityExceededError as exc:
        raise HTTPException(429, "STATION_CAPACITY_EXHAUSTED") from exc
    try:
        if config.execution_mode() == "process":
            return await asyncio.to_thread(
                process_runner.run,
                fmu_path,
                access_key=access_key,
                parameters=body.parameters or {},
                options=body.options,
            )

        slot.load()
        start = body.options.get("startTime", 0.0)
        stop = body.options.get("stopTime", 1.0)
        step = body.options.get("stepSize")
        slot.initialize(
            start_time=float(start),
            stop_time=float(stop),
            step_size=float(step) if step is not None else None,
            parameters=body.parameters or None,
        )
        result = slot.run_until(float(stop), step_size=float(step) if step is not None else None)
        outputs = slot.get_outputs()
        return {
            "type": "sim.result",
            "time": result["time"],
            "state": "terminated",
            "outputs": outputs.get("outputs", {}),
        }
    except process_runner.ProcessExecutionError as exc:
        raise HTTPException(status_code=504 if exc.code == "FMU_EXECUTION_TIMEOUT" else 500, detail=exc.code) from exc
    finally:
        engine.remove_session(slot.session_id)


@app.post("/internal/fmu/simulations/jobs", status_code=202, dependencies=[Depends(_check_token)])
async def submit_simulation_job(body: SimulationBody):
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
        await asyncio.to_thread(_reserve_simulation_scenario, body.gatewayContext, access_key, require_scope=True)
    if not fmu_storage.fmu_exists(access_key):
        raise HTTPException(404, "FMU_NOT_FOUND")
    _validate_backend_options(body.options)
    fmu_path = fmu_storage.get_fmu_path(access_key)
    try:
        slot = engine.create_session(fmu_path)
    except engine.CapacityExceededError:
        return StreamingResponse(
            iter([json.dumps({
                "type": "error",
                "code": "STATION_CAPACITY_EXHAUSTED",
                "message": "Station execution capacity is exhausted",
                "retryable": True,
            }) + "\n"]),
            media_type="application/x-ndjson",
        )

    def _generate():
        try:
            if config.execution_mode() == "process":
                for snapshot in process_runner.stream(
                    fmu_path,
                    access_key=access_key,
                    parameters=body.parameters or {},
                    options=body.options,
                ):
                    yield json.dumps(snapshot, default=str) + "\n"
            else:
                slot.load()
                start = body.options.get("startTime", 0.0)
                stop = body.options.get("stopTime", 1.0)
                step = body.options.get("stepSize")
                slot.initialize(
                    start_time=float(start),
                    stop_time=float(stop),
                    step_size=float(step) if step is not None else None,
                    parameters=body.parameters or None,
                )
                for snapshot in slot.run_until_streaming(
                    float(stop),
                    step_size=float(step) if step is not None else None,
                ):
                    yield json.dumps(snapshot, default=str) + "\n"
                yield json.dumps({"type": "sim.done", "time": float(stop)}) + "\n"
        except Exception:
            logger.exception("FMU streaming simulation failed")
            yield json.dumps({"type": "error", "message": "FMU simulation failed"}) + "\n"
        finally:
            engine.remove_session(slot.session_id)

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


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
            while session and session.state == "running" and not session._terminated:
                await asyncio.to_thread(session.step)
                await ws.send_text(json.dumps({
                    "type": "sim.progress",
                    "sessionId": session.session_id,
                    "simTime": session._time,
                }))
                if session._time >= session._stop_time - 1e-12:
                    session._state = "stopped"
                    await ws.send_text(json.dumps({
                        "type": "sim.state",
                        "sessionId": session.session_id,
                        "state": "stopped",
                        "simTime": session._time,
                    }))
                    return
                await asyncio.sleep(min(max(session._step_size, 0.01), 1.0))
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
            expires_at = expiring_session.expires_at
            if expires_at is None:
                return
            expires_at = float(expires_at)
            await asyncio.sleep(max(0.0, expires_at - time.time()))
            if session is not expiring_session or expiring_session._terminated:
                return
            await asyncio.to_thread(engine.remove_session, expiring_session.session_id)
            session = None
            await ws.send_text(json.dumps({
                "type": "session.closed",
                "sessionId": expiring_session.session_id,
                "reason": "expired",
            }))
            await ws.close(code=4003, reason="SESSION_EXPIRED")
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
                if session and session.subscription and session._initialised and not session._terminated:
                    payload = await asyncio.to_thread(session.sample_subscription)
                    if payload:
                        await ws.send_text(json.dumps(payload, default=str))
                await asyncio.sleep(0.01)  # 10 ms polling resolution
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
                        if bound_session.expires_at is not None:
                            _expiry_task = asyncio.create_task(_expire_session_after_deadline(bound_session))
                        # Start emitter task on session creation
                        if _emitter_task is None or _emitter_task.done():
                            _emitter_task = asyncio.create_task(_output_emitter())
                elif msg_type == "session.terminate":
                    await _stop_runner()
                    if _expiry_task:
                        _expiry_task.cancel()
                        _expiry_task = None
                    if session:
                        await asyncio.to_thread(engine.remove_session, session.session_id)
                    session = None

                if request_id:
                    response["requestId"] = request_id
                await ws.send_text(json.dumps(response, default=str))

            except HTTPException as exc:
                detail = exc.detail or ""
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


def _sim_outputs_payload(session: engine.FmuSession, outputs: dict[str, Any]) -> dict[str, Any]:
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
    return payload


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

        if not fmu_storage.fmu_exists(access_key):
            raise HTTPException(404, "FMU_NOT_FOUND")
        fmu_path = fmu_storage.get_fmu_path(access_key)
        claims = (gateway_ctx.get("claims") or {})
        exp = claims.get("exp")
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
            )
        except engine.CapacityExceededError as exc:
            raise HTTPException(429, "STATION_CAPACITY_EXHAUSTED") from exc
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
                "timeMode": ["simtime"],
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
        desc = fmu_storage.describe(session.access_key)
        return {"type": "model.description", "sessionId": session.session_id, **desc}

    if msg_type == "sim.initialize":
        options = msg.get("options", {})
        params = msg.get("parameters", {})
        if not isinstance(options, dict) or not isinstance(params, dict):
            raise HTTPException(status_code=422, detail="INVALID_SIMULATION_REQUEST")
        await asyncio.to_thread(_reserve_simulation_scenario, session.gateway_context, access_key)
        if stop_runner is not None:
            await stop_runner()
        await asyncio.to_thread(
            session.initialize,
            start_time=float(options.get("startTime", 0.0)),
            stop_time=float(options.get("stopTime", 1.0)),
            step_size=float(options.get("stepSize")) if options.get("stepSize") is not None else None,
            parameters=params or None,
        )
        return {
            "type": "sim.state",
            "sessionId": session.session_id,
            "state": session.state,
            "simTime": session._time,
        }

    if msg_type == "sim.start":
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
        await asyncio.to_thread(_reserve_simulation_scenario, session.gateway_context, access_key)
        if stop_runner is not None:
            await stop_runner()
        await asyncio.to_thread(session.reset)
        return {
            "type": "sim.state",
            "sessionId": session.session_id,
            "state": session.state,
            "simTime": session._time,
        }

    if msg_type == "sim.step":
        step_size = msg.get("deltaT", msg.get("stepSize"))
        await asyncio.to_thread(session.step, float(step_size) if step_size is not None else None)
        outputs = await asyncio.to_thread(session.get_outputs)
        return _sim_outputs_payload(session, outputs["outputs"])

    if msg_type == "sim.runUntil":
        target = msg.get("time", msg.get("targetTime"))
        if target is None:
            raise HTTPException(400, "INVALID_COMMAND – missing targetTime")
        step_size = msg.get("stepSize", msg.get("deltaT"))
        await asyncio.to_thread(
            session.run_until,
            float(target),
            step_size=float(step_size) if step_size is not None else None,
        )
        outputs = await asyncio.to_thread(session.get_outputs)
        return _sim_outputs_payload(session, outputs["outputs"])

    if msg_type == "sim.setInputs":
        values = msg.get("values", {})
        if not isinstance(values, dict):
            raise HTTPException(400, "sim.setInputs requires an object 'values'")
        if values:
            await asyncio.to_thread(_reserve_simulation_scenario, session.gateway_context, access_key)
        await asyncio.to_thread(session.set_inputs, values)
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
            selected = set(variables)
            refs = [
                int(variable.valueReference)
                for variable in model_description.modelVariables
                if variable.name in selected
            ]
        else:
            refs = msg.get("valueReferences")
        result = await asyncio.to_thread(session.get_outputs, refs)
        return _sim_outputs_payload(session, result["outputs"])

    if msg_type == "sim.subscribeOutputs":
        variables = msg.get("variables")
        if variables is not None and not isinstance(variables, list):
            raise HTTPException(400, "sim.subscribeOutputs requires 'variables' as array")
        subscription = engine.OutputSubscription(
            variables=variables,
            period_ms=max(1, int(msg.get("periodMs", 100))),
            max_batch_size=max(1, int(msg.get("maxBatchSize", 64))),
            max_hz=float(msg["maxHz"]) if msg.get("maxHz") is not None else None,
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
