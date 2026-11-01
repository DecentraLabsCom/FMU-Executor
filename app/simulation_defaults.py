"""Canonical FMI DefaultExperiment resolution for simulation requests."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


DEFAULT_START_TIME = 0.0
DEFAULT_STOP_DURATION = 1.0
DEFAULT_STEP_SIZE = 0.001


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def resolve_simulation_time_options(
    model_metadata: Mapping[str, Any] | None,
    options: Mapping[str, Any] | None = None,
    *,
    allow_undefined_stop: bool = False,
) -> dict[str, Any]:
    """Resolve time options; interactive sessions may deliberately omit stopTime."""
    metadata = model_metadata if isinstance(model_metadata, Mapping) else {}
    requested = options if isinstance(options, Mapping) else {}

    start_time = _finite_number(metadata.get("defaultStartTime"))
    if start_time is None:
        start_time = DEFAULT_START_TIME

    if allow_undefined_stop and ("stopTime" not in requested or requested.get("stopTime") is None):
        stop_time = None
    else:
        stop_time = _finite_number(metadata.get("defaultStopTime"))
        if stop_time is None or stop_time <= start_time:
            stop_time = start_time + DEFAULT_STOP_DURATION

    step_size = _finite_number(metadata.get("defaultStepSize"))
    if step_size is None or step_size <= 0:
        step_size = DEFAULT_STEP_SIZE

    defaults = {
        "startTime": start_time,
        "stopTime": stop_time,
        "stepSize": step_size,
    }
    resolved = {
        name: requested[name] if name in requested and requested[name] is not None else default
        for name, default in defaults.items()
    }
    if allow_undefined_stop:
        if "stopTime" not in requested or requested.get("stopTime") is None:
            resolved["stopTime"] = None
        else:
            requested_stop = _finite_number(requested.get("stopTime"))
            if requested_stop is None:
                raise ValueError("stopTime must be finite when provided")
            resolved["stopTime"] = requested_stop
    return resolved
