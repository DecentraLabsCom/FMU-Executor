"""FMU WebSocket command validation and execution protocol."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, cast

from fastapi import HTTPException

from . import auth, config, engine, fmu_storage, process_runner

MAX_ADVANCE_INPUT_EVENTS = 1024
MAX_ADVANCE_INPUT_BYTES = 256 * 1024
MAX_ADVANCE_COMPUTE_MS = 1000


def _session_capabilities() -> dict[str, Any]:
    return {
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
        "advanceComputeBudget": True,
        "advanceSampleSequences": True,
        "maxOutputHz": config.MAX_OUTPUT_SUBSCRIPTION_HZ,
        "inputUpdateQuota": {
            "maxUpdatesPerWindow": config.MAX_INPUT_UPDATES_PER_WINDOW,
            "windowMs": config.INPUT_UPDATE_WINDOW_SECONDS * 1000,
            "maxUpdateBytes": config.MAX_INPUT_UPDATE_BYTES,
            "maxUpdatesPerDay": config.MAX_INPUT_UPDATES_PER_DAY,
            "maxUpdateBytesPerDay": config.MAX_INPUT_UPDATE_BYTES_PER_DAY,
            "maxScheduledEventsPerAdvance": min(MAX_ADVANCE_INPUT_EVENTS, config.MAX_INPUT_UPDATES_PER_WINDOW),
            "maxScheduleBytesPerAdvance": MAX_ADVANCE_INPUT_BYTES,
        },
        "timeMode": ["simtime", "realtime", "external"],
    }


@dataclass(frozen=True)
class SessionCommandServices:
    """Application policies used by session commands, supplied by the API layer."""

    validate_access_key: Callable[[str], str]
    validate_active_reservation_deadline: Callable[[dict[str, Any]], None]
    model_execution_policy: Callable[[dict[str, Any] | None], dict[str, Any]]
    reserve_simulation_scenario: Callable[..., None]
    reserve_input_update: Callable[..., None]
    reserve_input_updates: Callable[..., dict[str, Any]]
    settle_input_updates: Callable[..., None]
    validate_input_schedule: Callable[..., list[dict[str, Any]]]
    strict_number: Callable[[Any, str], float]


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
            "requestedTime", "timeAdvanced", "sequence", "earlyReturn", "eventOccurred", "eventHandlingNeeded",
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
        claims = (gateway_ctx.get("claims") or {})
        exp = claims.get("exp")
        policy = services.model_execution_policy(gateway_ctx)
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
            "capabilities": _session_capabilities(),
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
            "capabilities": _session_capabilities(),
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
            start_time = services.strict_number(options.get("startTime", 0.0), "startTime")
            stop_time = services.strict_number(options.get("stopTime", 1.0), "stopTime")
            step_value = services.strict_number(options["stepSize"], "stepSize") if options.get("stepSize") is not None else None
            playback_speed = services.strict_number(options.get("playbackSpeed", 1.0), "playbackSpeed")
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

    if msg_type == "sim.step":
        step_size = msg.get("deltaT", msg.get("stepSize"))
        try:
            current_point = msg.get("currentCommunicationPoint", msg.get("currentTime"))
            if session._time_mode == "external" and current_point is None:
                raise ValueError("currentCommunicationPoint is required in external-step mode")
            if current_point is not None:
                requested_current_time = services.strict_number(current_point, "currentCommunicationPoint")
                tolerance = max(1e-12, abs(session._time) * 1e-12)
                if abs(requested_current_time - session._time) > tolerance:
                    raise ValueError("currentCommunicationPoint does not match the session time")
            delta = services.strict_number(step_size, "deltaT") if step_size is not None else None
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
            target_time = services.strict_number(msg.get("targetTime"), "targetTime")
            step_size = services.strict_number(msg["stepSize"], "stepSize") if msg.get("stepSize") is not None else None
            max_samples = msg.get("maxSamples", 256)
            if type(max_samples) is not int or not 1 <= max_samples <= 256:
                raise ValueError("maxSamples must be an integer between 1 and 256")
            output_sample_every_steps = msg.get("outputSampleEverySteps", 1)
            if type(output_sample_every_steps) is not int or output_sample_every_steps < 1:
                raise ValueError("outputSampleEverySteps must be a positive integer")
            max_compute_ms = msg.get("maxComputeMs")
            if max_compute_ms is not None and (
                isinstance(max_compute_ms, bool)
                or not isinstance(max_compute_ms, (int, float))
                or not math.isfinite(max_compute_ms)
                or not 1 <= max_compute_ms <= MAX_ADVANCE_COMPUTE_MS
            ):
                raise ValueError(f"maxComputeMs must be between 1 and {MAX_ADVANCE_COMPUTE_MS} milliseconds")
            if session._time_mode == "external":
                raise ValueError("sim.advance requires simtime or realtime mode")

            input_schedule = msg.get("inputSchedule", [])
            if not isinstance(input_schedule, list) or len(input_schedule) > MAX_ADVANCE_INPUT_EVENTS:
                raise ValueError("inputSchedule exceeds the event limit")
            try:
                encoded_schedule = json.dumps(input_schedule, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("inputSchedule is invalid") from exc
            if len(encoded_schedule.encode("utf-8")) > MAX_ADVANCE_INPUT_BYTES:
                raise ValueError("inputSchedule exceeds the byte limit")

            effective_step_size = step_size if step_size is not None else session._step_size
            if effective_step_size <= 0:
                raise ValueError("stepSize must be positive")
            schedule_start = session._time
            previous_time = schedule_start
            normalized_schedule = []
            for event in input_schedule:
                if not isinstance(event, dict) or set(event) != {"time", "values"}:
                    raise ValueError("Each inputSchedule event must contain time and values")
                event_time = services.strict_number(event["time"], "inputSchedule.time")
                values = event["values"]
                if not isinstance(values, dict) or not values:
                    raise ValueError("inputSchedule values must be a non-empty object")
                tolerance = max(1e-9, abs(effective_step_size) * 1e-7)
                step_index = round((event_time - session._start_time) / effective_step_size)
                aligned_time = session._start_time + step_index * effective_step_size
                if (event_time < schedule_start - 1e-12 or event_time > target_time + 1e-12
                        or event_time < previous_time - 1e-12
                        or abs(aligned_time - event_time) > tolerance):
                    raise ValueError("inputSchedule times must be ordered, within the advance interval and aligned to the model step")
                normalized_schedule.append({"time": event_time, "values": values})
                previous_time = event_time
            if normalized_schedule:
                normalized_schedule = await asyncio.to_thread(session.validate_input_schedule, normalized_schedule)

            output_refs = None
            output_variables = msg.get("outputVariables")
            if output_variables is not None:
                if (not isinstance(output_variables, list) or not output_variables
                        or len(output_variables) > 512 or any(not isinstance(name, str) for name in output_variables)
                        or len(set(output_variables)) != len(output_variables)):
                    raise ValueError("outputVariables must be a non-empty array of at most 512 names")
                model_variables = getattr(getattr(session, "_md", None), "modelVariables", None)
                if not model_variables:
                    raise ValueError("model description must be loaded before selecting outputVariables")
                outputs_by_name = {
                    variable.name: variable
                    for variable in model_variables
                    if getattr(variable, "causality", None) == "output"
                }
                if any(name not in outputs_by_name for name in output_variables):
                    raise ValueError("outputVariables contains an unknown or non-output variable")
                output_refs = [int(outputs_by_name[name].valueReference) for name in output_variables]

            reservation = None
            if normalized_schedule:
                reservation = await asyncio.to_thread(
                    services.reserve_input_updates,
                    session.gateway_context,
                    access_key,
                    normalized_schedule,
                )
            try:
                result = await asyncio.to_thread(
                    session.advance,
                    target_time,
                    max_samples=max_samples,
                    step_size=step_size,
                    input_schedule=normalized_schedule,
                    output_sample_every_steps=output_sample_every_steps,
                    output_refs=output_refs,
                    max_compute_ms=max_compute_ms,
                )
            except Exception:
                if reservation:
                    await asyncio.to_thread(
                        services.settle_input_updates,
                        session.gateway_context,
                        access_key,
                        reservation["reservationId"],
                        len(normalized_schedule),
                    )
                raise
            if reservation:
                applied_count = result.get("inputEventsApplied")
                if type(applied_count) is not int or not 0 <= applied_count <= len(normalized_schedule):
                    await asyncio.to_thread(
                        services.settle_input_updates,
                        session.gateway_context,
                        access_key,
                        reservation["reservationId"],
                        len(normalized_schedule),
                    )
                    raise ValueError("inputEventsApplied is outside the reserved schedule")
                await asyncio.to_thread(
                    services.settle_input_updates,
                    session.gateway_context,
                    access_key,
                    reservation["reservationId"],
                    applied_count,
                )
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
            target_time = services.strict_number(target, "targetTime")
            requested_step_size = (
                services.strict_number(step_size, "stepSize")
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
            await asyncio.to_thread(services.reserve_input_update, session.gateway_context, access_key, values)
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
                services.strict_number(msg["maxHz"], "maxHz")
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
