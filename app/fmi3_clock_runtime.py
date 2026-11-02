"""Runtime support for FMI 3 clocks and clocked outputs."""

from __future__ import annotations

import math
from typing import Any, Callable

from .fmi3_clock_scheduler import (
    Fmi3InputClockSchedule,
    consume_fmi3_clock_ticks,
    due_fmi3_clock_references,
    refresh_fmi3_clock_schedules,
)


class Fmi3ClockRuntime:
    """Track importer-scheduled input clocks and values exposed on clock ticks."""

    def __init__(
        self,
        fmu: Any,
        model_description: Any,
        input_clock_references: list[int],
        schedules: list[Fmi3InputClockSchedule],
        *,
        variable_size: Callable[[Any], int],
        variable_is_array: Callable[[Any], bool],
        normalize_value: Callable[[str, Any], Any],
        initial_outputs: dict[str, Any] | None = None,
    ):
        self._fmu = fmu
        self._input_clock_references = set(input_clock_references)
        self._schedules = list(schedules)
        self._variable_size = variable_size
        self._variable_is_array = variable_is_array
        self._normalize_value = normalize_value
        self._clock_variables = {
            int(variable.valueReference): variable
            for variable in (getattr(model_description, "modelVariables", []) or [])
            if getattr(variable, "type", None) == "Clock"
            and isinstance(getattr(variable, "valueReference", None), int)
            and not isinstance(getattr(variable, "valueReference", None), bool)
        }
        self._clocked_outputs = tuple(
            variable
            for variable in (getattr(model_description, "modelVariables", []) or [])
            if getattr(variable, "causality", None) == "output"
            and getattr(variable, "type", None) != "Clock"
            and bool(getattr(variable, "clocks", None))
        )
        self._output_clocks = tuple(
            variable
            for variable in (getattr(model_description, "modelVariables", []) or [])
            if getattr(variable, "type", None) == "Clock"
            and getattr(variable, "causality", None) == "output"
        )
        self._clocked_output_cache = {
            str(variable.name): initial_outputs[str(variable.name)]
            for variable in self._clocked_outputs
            if initial_outputs is not None and str(variable.name) in initial_outputs
        }
        self._pending_outputs: dict[str, Any] = {}

    @property
    def next_event_time(self) -> float | None:
        times = [schedule.next_time for schedule in self._schedules if schedule.next_time is not None]
        return min(times) if times else None

    def due_input_clock_references(self, time: float) -> list[int]:
        return due_fmi3_clock_references(self._schedules, time)

    def activate_input_clocks(self, references: set[int], time: float) -> set[int]:
        """Activate scheduled/input clocks once at the current event instant."""
        active = references & self._input_clock_references
        if active:
            ordered = sorted(active)
            self._fmu.setClock(ordered, [True] * len(ordered))
            consume_fmi3_clock_ticks(self._schedules, active, time)
        return active

    def refresh_after_event(
        self,
        time: float,
        activated_clocks: set[int],
        *,
        refresh_tunable_clocks: bool = False,
    ) -> None:
        refresh_fmi3_clock_schedules(
            self._fmu,
            self._schedules,
            time,
            activated_clocks,
            refresh_tunable_clocks=refresh_tunable_clocks,
        )

    def capture_event_outputs(self, active_input_clocks: set[int]) -> dict[str, Any]:
        """Read output Clocks and clocked outputs while their Clocks are active."""
        required_references: set[int] = {
            int(variable.valueReference) for variable in self._output_clocks
        }
        for variable in self._clocked_outputs:
            required_references.update(self._associated_clock_references(variable))
        for variable in self._output_clocks:
            required_references.update(self._associated_clock_references(variable))

        clock_values: dict[int, bool] = {}
        active_clocks = set(active_input_clocks)
        unqueried = {
            reference
            for reference in required_references
            if reference in self._clock_variables
            and getattr(self._clock_variables[reference], "causality", None) in {"output", "local"}
        }
        for _ in range(len(unqueried) + 1):
            query_references = sorted(
                reference
                for reference in unqueried
                if not self._associated_clock_references(self._clock_variables[reference])
                or self._associated_clock_references(self._clock_variables[reference]).intersection(active_clocks)
            )
            if not query_references:
                break
            values = self._fmu.getClock(query_references)
            if len(values) != len(query_references):
                raise RuntimeError("FMU returned an invalid number of output Clock values")
            for reference, value in zip(query_references, values, strict=True):
                clock_values[reference] = bool(value)
                unqueried.remove(reference)
                if value:
                    active_clocks.add(reference)
        for variable in self._output_clocks:
            parent_clocks = self._associated_clock_references(variable)
            if parent_clocks and not parent_clocks.intersection(active_clocks):
                continue
            reference = int(variable.valueReference)
            if reference in clock_values:
                name = str(variable.name)
                self._pending_outputs[name] = bool(
                    self._pending_outputs.get(name, False) or clock_values[reference]
                )

        for variable in self._clocked_outputs:
            if not self._associated_clock_references(variable).intersection(active_clocks):
                continue
            value_type = str(getattr(variable, "type", ""))
            getter_type = "Int64" if value_type == "Enumeration" else value_type
            getter = getattr(self._fmu, f"get{getter_type}", None)
            if getter is None:
                raise RuntimeError(f"FMI clocked output '{variable.name}' has an unsupported type")
            reference = int(variable.valueReference)
            size = self._variable_size(variable)
            is_array = self._variable_is_array(variable)
            values = getter([reference], nValues=size) if is_array else getter([reference])
            normalized = [self._normalize_value(value_type, value) for value in values]
            value = normalized if is_array else normalized[0]
            name = str(variable.name)
            self._clocked_output_cache[name] = value
            self._pending_outputs[name] = value

        return dict(self._pending_outputs)

    def take_event_outputs(self) -> dict[str, Any]:
        outputs = self._pending_outputs
        self._pending_outputs = {}
        return outputs

    def clocked_output_cache(self) -> dict[str, Any]:
        return dict(self._clocked_output_cache)

    def snapshot(self) -> dict[str, Any]:
        return {
            "clockSchedules": [
                {
                    "valueReference": schedule.value_reference,
                    "intervalVariability": schedule.variability,
                    "interval": schedule.interval,
                    "shift": schedule.shift,
                    "nextTime": schedule.next_time,
                    "lastTickTime": schedule.last_tick_time,
                    "hasTicked": schedule.has_ticked,
                }
                for schedule in self._schedules
            ],
            "clockedOutputCache": dict(self._clocked_output_cache),
        }

    def validate_snapshot(self, metadata: dict[str, Any], current_time: float) -> dict[str, Any]:
        if not math.isfinite(current_time):
            raise ValueError("FMU state checkpoint has an invalid FMI 3 Clock time")
        raw_schedules = metadata.get("clockSchedules")
        if not isinstance(raw_schedules, list) or len(raw_schedules) != len(self._schedules):
            raise ValueError("FMU state checkpoint has invalid FMI 3 Clock metadata")
        raw_cache = metadata.get("clockedOutputCache")
        if not isinstance(raw_cache, dict):
            raise ValueError("FMU state checkpoint has invalid clocked output metadata")
        output_variables = {str(variable.name): variable for variable in self._clocked_outputs}
        if set(raw_cache) != set(output_variables):
            raise ValueError("FMU state checkpoint is missing clocked output values")
        normalized_cache: dict[str, Any] = {}
        for name, value in raw_cache.items():
            variable = output_variables.get(name) if isinstance(name, str) else None
            if variable is None:
                raise ValueError("FMU state checkpoint contains an unknown clocked output")
            size = self._variable_size(variable)
            is_array = self._variable_is_array(variable)
            if is_array and not isinstance(value, list):
                raise ValueError("FMU state checkpoint has an invalid clocked output array")
            values = value if is_array else [value]
            if len(values) != size:
                raise ValueError("FMU state checkpoint has an invalid clocked output size")
            try:
                normalized = [
                    self._normalize_value(str(getattr(variable, "type", "")), item)
                    for item in values
                ]
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("FMU state checkpoint has an invalid clocked output value") from exc
            normalized_cache[name] = normalized if is_array else normalized[0]

        by_reference = {schedule.value_reference: schedule for schedule in self._schedules}
        restored_references: set[int] = set()
        for item in raw_schedules:
            if not isinstance(item, dict):
                raise ValueError("FMU state checkpoint has invalid FMI 3 Clock metadata")
            reference = item.get("valueReference")
            if isinstance(reference, bool) or not isinstance(reference, int):
                raise ValueError("FMU state checkpoint has an invalid FMI 3 Clock reference")
            schedule = by_reference.get(reference)
            if schedule is None or reference in restored_references:
                raise ValueError("FMU state checkpoint has an invalid FMI 3 Clock reference")
            if item.get("intervalVariability") != schedule.variability:
                raise ValueError("FMU state checkpoint Clock configuration does not match the FMU")
            interval = _optional_finite_number(item.get("interval"), "Clock interval")
            next_time = _optional_finite_number(item.get("nextTime"), "Clock next time")
            last_tick = _finite_number(item.get("lastTickTime"), "Clock last tick")
            shift = _finite_number(item.get("shift"), "Clock shift")
            if schedule.variability in {"constant", "fixed", "tunable"}:
                if interval is None or interval <= 0 or shift < 0 or next_time is None:
                    raise ValueError("FMU state checkpoint has an invalid periodic Clock schedule")
            elif interval is not None and interval < 0:
                raise ValueError("FMU state checkpoint has an invalid aperiodic Clock interval")
            elif interval is None and next_time is not None:
                raise ValueError("FMU state checkpoint has an invalid aperiodic Clock schedule")
            has_ticked = item.get("hasTicked")
            if type(has_ticked) is not bool:
                raise ValueError("FMU state checkpoint has invalid Clock tick metadata")
            tolerance = max(1e-12, abs(current_time) * 1e-12)
            if next_time is not None and next_time < current_time - tolerance:
                raise ValueError("FMU state checkpoint contains a Clock before the simulation time")
            if last_tick > current_time + tolerance:
                raise ValueError("FMU state checkpoint contains a Clock tick after the simulation time")
            restored_references.add(reference)
        return normalized_cache

    def restore_snapshot(self, metadata: dict[str, Any], current_time: float) -> None:
        normalized_cache = self.validate_snapshot(metadata, current_time)
        restored = {
            item["valueReference"]: item for item in metadata["clockSchedules"]
        }
        for schedule in self._schedules:
            item = restored[schedule.value_reference]
            schedule.interval = _optional_finite_number(item.get("interval"), "Clock interval")
            schedule.shift = _finite_number(item.get("shift"), "Clock shift")
            schedule.next_time = _optional_finite_number(item.get("nextTime"), "Clock next time")
            schedule.last_tick_time = _finite_number(item.get("lastTickTime"), "Clock last tick")
            schedule.has_ticked = bool(item.get("hasTicked"))
        self._clocked_output_cache = normalized_cache
        self._pending_outputs = {}

    @staticmethod
    def _associated_clock_references(variable: Any) -> set[int]:
        references: set[int] = set()
        for clock in getattr(variable, "clocks", []) or []:
            reference = getattr(clock, "valueReference", clock)
            if isinstance(reference, int) and not isinstance(reference, bool):
                references.add(reference)
        return references


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"FMU state checkpoint has an invalid {label}")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"FMU state checkpoint has an invalid {label}")
    return normalized


def _optional_finite_number(value: Any, label: str) -> float | None:
    return None if value is None else _finite_number(value, label)
