"""Shared policies and response helpers for WebSocket session commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from fastapi import HTTPException

from . import config


MAX_ADVANCE_INPUT_EVENTS = 1024
MAX_ADVANCE_INPUT_BYTES = 256 * 1024
MAX_ADVANCE_COMPUTE_MS = 1000


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


def session_capabilities(
    max_input_updates_per_window: int | None,
    model_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    model_capabilities = model_metadata.get("capabilities", {}) if isinstance(model_metadata, dict) else {}
    can_get_state = bool(
        model_capabilities.get("canGetAndSetFMUstate", model_capabilities.get("canGetAndSetFMUState", False))
    )
    can_serialize_state = bool(
        model_capabilities.get("canSerializeFMUstate", model_capabilities.get("canSerializeFMUState", False))
    )
    fmu_state_supported = bool(
        isinstance(model_metadata, dict)
        and model_metadata.get("simulationType") == "CoSimulation"
        and can_get_state
        and can_serialize_state
        and config.internal_token()
    )
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
        "advanceInputValues": True,
        "advanceComputeBudget": True,
        "advanceSampleSequences": True,
        "maxOutputHz": config.MAX_OUTPUT_SUBSCRIPTION_HZ,
        "inputUpdateQuota": {
            "maxUpdatesPerWindow": max_input_updates_per_window,
            "windowMs": config.INPUT_UPDATE_WINDOW_SECONDS * 1000,
            "maxUpdateBytes": config.MAX_INPUT_UPDATE_BYTES,
            "maxUpdatesPerDay": config.MAX_INPUT_UPDATES_PER_DAY,
            "maxUpdateBytesPerDay": config.MAX_INPUT_UPDATE_BYTES_PER_DAY,
            "maxScheduledEventsPerAdvance": (
                min(MAX_ADVANCE_INPUT_EVENTS, max_input_updates_per_window)
                if max_input_updates_per_window is not None
                else MAX_ADVANCE_INPUT_EVENTS
            ),
            "maxScheduleBytesPerAdvance": MAX_ADVANCE_INPUT_BYTES,
        },
        "timeMode": ["simtime", "realtime", "external"],
        "fmuState": {
            "getAndSet": fmu_state_supported,
            "serialize": fmu_state_supported,
        },
    }


def sim_outputs_payload(
    session: Any,
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


def output_read_rate_limit_exception(exc: _OutputReadRateLimitErrorLike) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "code": "OUTPUT_READ_RATE_LIMITED",
            "message": str(exc),
            "retryable": True,
            "retryAfterMs": exc.retry_after_ms,
        },
    )
