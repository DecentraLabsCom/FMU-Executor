"""Input, output, subscription, and session status command handlers."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Callable, cast

from fastapi import HTTPException

from . import config, engine, process_runner
from .session_protocol_common import (
    SessionCommandServices,
    output_read_rate_limit_exception as _output_read_rate_limit_exception,
    sim_outputs_payload as _sim_outputs_payload,
)

IO_COMMAND_TYPES = frozenset({
    "sim.setInputs",
    "sim.getOutputs",
    "sim.subscribeOutputs",
    "sim.unsubscribeOutputs",
    "sim.getState",
    "session.ping",
    "ping",
    "session.terminate",
})

async def handle_io_command(
    msg_type: str,
    msg: dict,
    session: Any,
    access_key: str,
    services: SessionCommandServices,
) -> dict[str, Any]:
    if msg_type not in IO_COMMAND_TYPES:
        raise ValueError(f"Unsupported session command: {msg_type}")

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

    raise AssertionError("Session command handler did not return a response")
