"""FMU WebSocket command validation and execution protocol."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from fastapi import HTTPException

from . import (
    auth,
    config,
    engine,
    fmu_storage,
    fmu_state,
    process_runner,
    session_registry,
)
from .simulation_defaults import resolve_simulation_time_options
from .session_protocol_common import (
    SessionCommandServices,
    output_read_rate_limit_exception as _output_read_rate_limit_exception,
    session_capabilities as _session_capabilities,
)
from .session_protocol_execution import EXECUTION_COMMAND_TYPES, handle_execution_command
from .session_protocol_io import IO_COMMAND_TYPES, handle_io_command


async def _handle_session_create(
    msg: dict,
    gateway_ctx: dict | None,
    session: Any | None,
    services: SessionCommandServices,
) -> dict[str, Any]:
    if session is not None:
        raise HTTPException(400, "INVALID_COMMAND – session already created")
    if gateway_ctx is None:
        raise HTTPException(400, "Missing gatewayContext")

    access_key = auth.extract_access_key_from_context(gateway_ctx)
    if not access_key:
        raise HTTPException(400, "Missing accessKey in gatewayContext")
    access_key = services.validate_access_key(access_key)
    auth.validate_gateway_context(gateway_ctx, access_key)
    services.validate_active_reservation_deadline(gateway_ctx)
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
    claims = gateway_ctx.get("claims") or {}
    policy = services.model_execution_policy(gateway_ctx)
    try:
        session_factory = (
            process_runner.RealtimeSession
            if config.execution_mode() == "process"
            else engine.FmuSession
        )
        new_session = await asyncio.to_thread(
            session_registry.create_session,
            fmu_path,
            access_key=access_key,
            expires_at=claims.get("exp"),
            gateway_context=gateway_ctx,
            fmu_sha256=model_sha256,
            session_factory=session_factory,
            resource_policy=policy if config.execution_mode() == "process" else None,
        )
    except session_registry.CapacityExceededError as exc:
        raise HTTPException(429, "STATION_CAPACITY_EXHAUSTED") from exc
    new_session.fmu_sha256 = model_sha256
    try:
        model_metadata = await asyncio.to_thread(new_session.load)
    except Exception:
        await asyncio.to_thread(session_registry.remove_session, new_session.session_id)
        raise

    return {
        "type": "session.created",
        "sessionId": new_session.session_id,
        "serverTime": time.time(),
        "expiresAt": new_session.expires_at,
        "fmuSha256": model_sha256,
        "capabilities": _session_capabilities(policy["maxInputUpdatesPerWindow"], model_metadata),
        "_session": new_session,
    }


async def _handle_sim_initialize(
    msg: dict,
    session: Any,
    access_key: str,
    services: SessionCommandServices,
    stop_runner=None,
) -> dict[str, Any]:
    options = msg.get("options", {})
    root_params = msg.get("parameters", {})
    if not isinstance(options, dict) or not isinstance(root_params, dict):
        raise HTTPException(status_code=422, detail="INVALID_SIMULATION_REQUEST")
    option_params = options.get("inputs", root_params)
    if not isinstance(option_params, dict) or (root_params and option_params != root_params):
        raise HTTPException(status_code=422, detail="INVALID_SIMULATION_REQUEST - conflicting inputs")
    params = option_params
    checkpoint_snapshot = None
    if "checkpoint" in msg:
        try:
            checkpoint_snapshot = fmu_state.open_fmu_state_checkpoint(
                msg.get("checkpoint"),
                gateway_context=session.gateway_context,
                signing_secret=config.internal_token(),
                expected_fmu_sha256=getattr(session, "fmu_sha256", None),
                state_dir=config.STATE_DIR,
            )
        except ValueError as exc:
            raise HTTPException(400, "INVALID_FMU_STATE_CHECKPOINT") from exc
    time_mode = options.get("timeMode", "simtime")
    if not isinstance(time_mode, str) or time_mode not in {"simtime", "realtime", "external"}:
        raise HTTPException(status_code=400, detail="INVALID_TIME_MODE")
    try:
        model_metadata = await asyncio.to_thread(session.load)
        time_options = resolve_simulation_time_options(model_metadata, options)
        start_time = services.strict_number(time_options["startTime"], "startTime")
        stop_time = services.strict_number(time_options["stopTime"], "stopTime")
        step_value = services.strict_number(time_options["stepSize"], "stepSize")
        playback_speed = services.strict_number(options.get("playbackSpeed", 1.0), "playbackSpeed")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="INVALID_SIMULATION_OPTIONS") from exc
    if (
        not all(math.isfinite(value) for value in (start_time, stop_time, step_value, playback_speed))
        or stop_time <= start_time
        or not math.isfinite(stop_time - start_time)
        or step_value <= 0
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
        services.reserve_simulation_scenario,
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
        if checkpoint_snapshot is not None:
            await asyncio.to_thread(session.restore_fmu_state, checkpoint_snapshot)
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


async def _handle_fmu_state_message(
    msg_type: str,
    msg: dict,
    session: Any,
    stop_runner=None,
) -> dict[str, Any]:
    if msg_type == "sim.captureFMUState":
        try:
            if stop_runner is not None:
                await stop_runner()
            if session.state == "running":
                await asyncio.to_thread(session.pause)
            snapshot = await asyncio.to_thread(session.capture_fmu_state)
            checkpoint = fmu_state.seal_fmu_state_checkpoint(
                snapshot,
                gateway_context=session.gateway_context,
                signing_secret=config.internal_token(),
                state_dir=config.STATE_DIR,
            )
        except process_runner.OutputReadRateLimitError as exc:
            raise _output_read_rate_limit_exception(exc) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "type": "sim.fmuState",
            "sessionId": session.session_id,
            "simTime": session._time,
            "checkpoint": checkpoint,
        }

    variables = msg.get("variables", [])
    if (
        not isinstance(variables, list)
        or len(variables) > 512
        or any(not isinstance(name, str) or not name for name in variables)
        or len(set(variables)) != len(variables)
    ):
        raise HTTPException(400, "INVALID_FMU_STATE_OPTIONS")
    try:
        if stop_runner is not None:
            await stop_runner()
        if session.state == "running":
            await asyncio.to_thread(session.pause)
        snapshot = fmu_state.open_fmu_state_checkpoint(
            msg.get("checkpoint"),
            gateway_context=session.gateway_context,
            signing_secret=config.internal_token(),
            expected_fmu_sha256=getattr(session, "fmu_sha256", None),
            state_dir=config.STATE_DIR,
        )
        restored = await asyncio.to_thread(session.restore_fmu_state, snapshot)
        outputs: dict[str, Any] = {}
        if variables:
            model_description = session._md
            if model_description is None:
                raise HTTPException(409, "MODEL_DESCRIPTION_NOT_LOADED")
            outputs_by_name = {
                variable.name: variable
                for variable in model_description.modelVariables
                if variable.causality == "output"
            }
            if set(variables) - set(outputs_by_name):
                raise HTTPException(400, "UNKNOWN_OUTPUT_VARIABLE")
            refs = [int(outputs_by_name[name].valueReference) for name in variables]
            outputs_snapshot = await asyncio.to_thread(session.get_outputs, refs)
            outputs = outputs_snapshot.get("outputs", {})
    except ValueError as exc:
        raise HTTPException(400, "INVALID_FMU_STATE_CHECKPOINT") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "type": "sim.state",
        "sessionId": session.session_id,
        "state": restored.get("state", session.state),
        "simTime": restored.get("simTime", session._time),
        "outputs": outputs,
    }


async def handle_ws_message(
    msg_type: str,
    msg: dict,
    gateway_ctx: dict | None,
    session: Any | None,
    *,
    services: SessionCommandServices,
    start_runner=None,
    stop_runner=None,
) -> dict[str, Any]:
    """Dispatch a single WS message and return a response dict."""

    if msg_type == "session.create":
        return await _handle_session_create(msg, gateway_ctx, session, services)

    if msg_type == "session.attach":
        requested_session_id = str(msg.get("sessionId") or "").strip()
        if session is None:
            if not requested_session_id:
                raise HTTPException(400, "INVALID_COMMAND – missing sessionId")
            if not isinstance(gateway_ctx, dict):
                raise HTTPException(400, "Missing gatewayContext")
            attached_session = session_registry.get_attachable_session(requested_session_id)
            if attached_session is None:
                raise HTTPException(400, "INVALID_COMMAND – no active session")
        else:
            if requested_session_id and requested_session_id != session.session_id:
                raise HTTPException(403, "FORBIDDEN – sessionId mismatch")
            attached_session = session_registry.get_attachable_session(session.session_id)
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
        attach_policy = services.model_execution_policy(attached_session.gateway_context)
        model_metadata = await asyncio.to_thread(attached_session.load)
        return {
            "type": "session.attached",
            "sessionId": attached_session.session_id,
            "serverTime": time.time(),
            "expiresAt": attached_session.expires_at,
            "state": attached_session.state,
            "capabilities": _session_capabilities(attach_policy["maxInputUpdatesPerWindow"], model_metadata),
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
    access_key = services.validate_access_key(access_key)
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
        return await _handle_sim_initialize(msg, session, access_key, services, stop_runner)

    if msg_type in {"sim.captureFMUState", "sim.restoreFMUState"}:
        return await _handle_fmu_state_message(msg_type, msg, session, stop_runner)

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
                services.reserve_simulation_scenario,
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

    if msg_type in EXECUTION_COMMAND_TYPES:
        return await handle_execution_command(msg_type, msg, session, access_key, services)
    if msg_type in IO_COMMAND_TYPES:
        return await handle_io_command(msg_type, msg, session, access_key, services)

    raise HTTPException(400, f"INVALID_COMMAND – unknown type {msg_type!r}")
