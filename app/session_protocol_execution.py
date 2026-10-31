"""Validation and execution handlers for simulation advancement commands."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any, Callable, cast

from fastapi import HTTPException

from . import engine, process_runner
from .session_protocol_common import (
    MAX_ADVANCE_COMPUTE_MS,
    MAX_ADVANCE_INPUT_BYTES,
    MAX_ADVANCE_INPUT_EVENTS,
    SessionCommandServices,
    output_read_rate_limit_exception as _output_read_rate_limit_exception,
    sim_outputs_payload as _sim_outputs_payload,
)

EXECUTION_COMMAND_TYPES = frozenset({"sim.step", "sim.advance", "sim.runUntil"})

async def handle_execution_command(
    msg_type: str,
    msg: dict,
    session: Any,
    access_key: str,
    services: SessionCommandServices,
) -> dict[str, Any]:
    if msg_type not in EXECUTION_COMMAND_TYPES:
        raise ValueError(f"Unsupported session command: {msg_type}")

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
            input_schedule = msg.get("inputSchedule", [])
            input_values = msg.get("inputValues", {})
            if not isinstance(input_values, dict):
                raise ValueError("inputValues must be an object")
            if (
                not isinstance(input_schedule, list)
                or len(input_schedule) + bool(input_values) > MAX_ADVANCE_INPUT_EVENTS
            ):
                raise ValueError("Advance input updates exceed the event limit")

            effective_step_size = step_size if step_size is not None else session._step_size
            if effective_step_size <= 0:
                raise ValueError("stepSize must be positive")
            if target_time <= session._time + 1e-15:
                raise ValueError("targetTime must be later than the current simulation time")
            if session._time_mode == "external":
                current_point = msg.get("currentCommunicationPoint")
                if current_point is None:
                    raise ValueError("currentCommunicationPoint is required in external-advance mode")
                current_point = services.strict_number(current_point, "currentCommunicationPoint")
                current_tolerance = max(1e-12, abs(session._time) * 1e-12)
                if abs(current_point - session._time) > current_tolerance:
                    raise ValueError("currentCommunicationPoint does not match simulation time")
                tolerance = max(1e-12, abs(session._stop_time) * 1e-12)
                if target_time > session._stop_time + tolerance:
                    raise ValueError("targetTime would advance past stopTime")
            schedule_start = session._time
            previous_time = schedule_start
            wire_schedule = []
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
                wire_schedule.append({"time": event_time, "values": values})
                normalized_schedule.append({"time": event_time, "values": values})
                previous_time = event_time
            if normalized_schedule:
                normalized_schedule = await asyncio.to_thread(session.validate_input_schedule, normalized_schedule)

            if input_values:
                normalized_input_values = await asyncio.to_thread(session.validate_inputs, input_values)
                normalized_schedule.insert(0, {"time": schedule_start, "values": normalized_input_values})
                wire_schedule.insert(0, {"time": schedule_start, "values": input_values})
            try:
                encoded_schedule = json.dumps(wire_schedule, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("Advance input updates are invalid") from exc
            if len(encoded_schedule.encode("utf-8")) > MAX_ADVANCE_INPUT_BYTES:
                raise ValueError("Advance input updates exceed the byte limit")
            if normalized_schedule:
                expected_samples = engine.estimate_advance_samples(
                    session._time,
                    target_time,
                    session._stop_time,
                    effective_step_size,
                    output_sample_every_steps,
                )
                if expected_samples < 1:
                    raise ValueError("targetTime must advance at least one model sample")
                if expected_samples > max_samples:
                    raise ValueError("ADVANCE_SAMPLE_LIMIT_EXCEEDED")

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
                    wire_schedule,
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
            except (engine.AdvancePreExecutionError, engine.AdvanceExecutionError) as exc:
                if reservation:
                    await asyncio.to_thread(
                        services.settle_input_updates,
                        session.gateway_context,
                        access_key,
                        reservation["reservationId"],
                        exc.input_events_applied,
                    )
                raise
            except process_runner.OutputReadRateLimitError:
                if reservation:
                    await asyncio.to_thread(
                        services.settle_input_updates,
                        session.gateway_context,
                        access_key,
                        reservation["reservationId"],
                        0,
                    )
                raise
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

    raise AssertionError("Session command handler did not return a response")
