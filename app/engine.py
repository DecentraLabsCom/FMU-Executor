"""FMU execution engine for FMI Co-Simulation and Model Exchange models."""

from __future__ import annotations

import base64
import binascii
import copy
import logging
import math
import json
import re
import platform
from functools import reduce
from operator import mul
import shutil
import tempfile
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Optional

from fmpy import (
    extract as fmpy_extract,
    instantiate_fmu as fmpy_instantiate_fmu,
    platform_tuple,
    read_model_description,
    sharedLibraryExtension,
)
from fmpy.fmi1 import FMICallException
from fmpy.fmi2 import fmi2Discard, fmi2LastSuccessfulTime, fmi2Pending, fmi2Terminated

from . import config
from .fmi3_cosimulation import instantiate_fmi3_cosimulation
from .fmi3_clock_scheduler import Fmi3InputClockSchedule, read_fmi3_input_clock_schedules
from .fmi3_clock_runtime import Fmi3ClockRuntime
from .fmi3_model_exchange import instantiate_fmi3_model_exchange
from .fmi_ls_dae import Fmi3DaeIntegrator, FmiLsDaeDefinition, read_fmi_ls_dae
from .fmi_model_exchange import FmiModelExchangeIntegrator
from .fmu_metadata import describe_model_description
from .simulation_defaults import DEFAULT_STEP_SIZE, resolve_simulation_time_options

logger = logging.getLogger(__name__)


class AdvancePreExecutionError(ValueError):
    """An advance failed before stepping the FMU; report any applied input prefix."""

    def __init__(self, message: str, input_events_applied: int = 0):
        super().__init__(message)
        self.input_events_applied = input_events_applied


class AdvanceExecutionError(RuntimeError):
    """An advance failed after stepping began and reports its applied input prefix."""

    def __init__(self, message: str, input_events_applied: int):
        super().__init__(message)
        self.input_events_applied = input_events_applied


def _ceil_step_count(expected_steps: float, current_time: float, target_time: float, step_size: float) -> int:
    """Round away floating-point noise at an exact model-step boundary."""
    nearest_step = round(expected_steps)
    time_tolerance = max(1e-12, abs(current_time) * 1e-12, abs(target_time) * 1e-12)
    if abs(expected_steps - nearest_step) * step_size <= time_tolerance:
        return int(nearest_step)
    return math.ceil(expected_steps)


def estimate_advance_samples(
    current_time: float,
    target_time: float,
    stop_time: float | None,
    step_size: float,
    output_sample_every_steps: int,
) -> int:
    """Estimate output samples with the same time-boundary tolerance as the engine."""
    effective_target = min(target_time, stop_time) if stop_time is not None else target_time
    expected_steps = max(0.0, effective_target - current_time) / step_size
    if not math.isfinite(expected_steps):
        raise ValueError("targetTime and stepSize produce an invalid step count")
    step_count = _ceil_step_count(expected_steps, current_time, effective_target, step_size)
    return math.ceil(step_count / output_sample_every_steps)


# ── subscription model ───────────────────────────────────────────

@dataclass
class OutputSubscription:
    """Tracks a sim.subscribeOutputs request."""
    variables: Optional[list[str]] = None
    period_ms: int = 100
    max_batch_size: int = 64
    max_hz: Optional[float] = None
    last_emit_monotonic: float = 0.0
    rate_dropped: int = 0

    def min_interval_seconds(self) -> float:
        period_interval = max(1, self.period_ms) / 1000.0
        requested_hz_interval = (
            1.0 / self.max_hz
            if self.max_hz is not None and self.max_hz > 0
            else 0.0
        )
        configured_hz_interval = 1.0 / config.MAX_OUTPUT_SUBSCRIPTION_HZ
        return max(period_interval, requested_hz_interval, configured_hz_interval)

    def seconds_until_due(self, now: float | None = None) -> float:
        now = _time.monotonic() if now is None else now
        return max(0.0, self.min_interval_seconds() - (now - self.last_emit_monotonic))


class FmuSession:
    """Manage one FMI Co-Simulation or Model Exchange session."""

    def __init__(
        self,
        session_id: str,
        fmu_path: Path,
        *,
        access_key: str | None = None,
        expires_at: float | int | str | None = None,
        gateway_context: dict[str, Any] | None = None,
        fmu_sha256: str | None = None,
    ):
        self.session_id = session_id
        self.fmu_path = fmu_path
        self.access_key = access_key or fmu_path.name
        self.expires_at = expires_at
        self.gateway_context = dict(gateway_context or {})
        self.fmu_sha256 = fmu_sha256
        self._extract_dir: Path | None = None
        self._slave: Any | None = None
        self._md = None
        self._fmi_ls_dae: FmiLsDaeDefinition | None = None
        self._fmi_ls_dae_issue: str | None = None
        self._execution_interface: str | None = None
        self._dae_integrator: Fmi3DaeIntegrator | None = None
        self._me_integrator: FmiModelExchangeIntegrator | None = None
        self._fmi3_input_clock_references: list[int] = []
        self._fmi3_clock_schedules: list[Fmi3InputClockSchedule] = []
        self._fmi3_clock_runtime: Fmi3ClockRuntime | None = None
        self._use_me = False
        self._use_dae = False
        self._time: float = 0.0
        self._step_size: float = 0.001
        self._start_time: float = 0.0
        self._stop_time: float | None = None
        self._initial_parameters: dict[str, Any] = {}
        self._time_mode = "simtime"
        self._playback_speed = 1.0
        self._early_return_allowed = False
        self._fmi3_event_mode_used = False
        self._fmi3_cs_next_event_time_defined = False
        self._fmi3_cs_next_event_time: float | None = None
        self._pending_event_outputs: dict[str, Any] | None = None
        self._finished_reason: str | None = None
        self._state: str = "new"
        self._initialised: bool = False
        self._terminated: bool = False
        self._attached: bool = False
        self._attach_deadline: float | None = None
        self._attachment_owner: object | None = None
        # Subscription state
        self.subscription: OutputSubscription | None = None
        self.seq: int = 0
        self._pending_samples: list[dict[str, Any]] = []
        self._pending_queue_drops: int = 0
        self._step_count: int = 0
        self._max_simulation_steps = config.MAX_SIMULATION_STEPS

    # ── lifecycle ────────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        """Extract and read model description. Returns describe dict."""
        if self._md is not None:
            return self._describe()
        self._extract_dir = Path(tempfile.mkdtemp(
            prefix=f"fmu_{self.session_id}_",
            dir=str(config.TEMP_DIR),
        ))
        fmpy_extract(str(self.fmu_path), unzipdir=str(self._extract_dir))
        self._md = read_model_description(str(self.fmu_path))
        try:
            self._fmi_ls_dae = read_fmi_ls_dae(self.fmu_path, self._md)
        except ValueError as exc:
            self._fmi_ls_dae_issue = str(exc)
            logger.warning("FMI-LS-DAE manifest is unavailable for %s: %s", self.access_key, exc)
        available_interfaces = self._available_execution_interfaces()
        if self._fmi_ls_dae is not None and "ModelExchange" in available_interfaces:
            self._execution_interface = "ModelExchange"
        elif "CoSimulation" in available_interfaces:
            self._execution_interface = "CoSimulation"
        elif "ModelExchange" in available_interfaces:
            self._execution_interface = "ModelExchange"
        return self._describe()

    def initialize(
        self,
        start_time: float = 0.0,
        stop_time: float | None = None,
        step_size: float | None = None,
        parameters: dict[str, Any] | None = None,
        time_mode: str = "simtime",
        playback_speed: float = 1.0,
        early_return_allowed: bool = False,
        execution_interface: str | None = None,
    ) -> dict[str, Any]:
        if self._initialised:
            raise RuntimeError("Session already initialised")
        if self._md is None:
            raise RuntimeError("FMU not loaded – call load() first")
        self._fmi3_input_clock_references = []
        self._fmi3_clock_schedules = []
        self._fmi3_clock_runtime = None
        self._fmi3_event_mode_used = False
        self._fmi3_cs_next_event_time_defined = False
        self._fmi3_cs_next_event_time = None
        self._pending_event_outputs = None
        available_interfaces = self._available_execution_interfaces()
        if execution_interface is not None:
            if not isinstance(execution_interface, str) or execution_interface not in {"CoSimulation", "ModelExchange"}:
                raise ValueError("executionInterface must be CoSimulation or ModelExchange")
            if execution_interface not in available_interfaces:
                raise ValueError(f"FMU does not provide an executable {execution_interface} interface")
            self._execution_interface = execution_interface
        elif self._execution_interface not in {"CoSimulation", "ModelExchange"}:
            detail = self._fmi_ls_dae_issue or "FMU has no executable Co-Simulation or Model Exchange binary"
            raise RuntimeError(detail)
        self._use_me = self._execution_interface == "ModelExchange"
        self._use_dae = False

        default_experiment = self._md.defaultExperiment
        default_step_size = getattr(default_experiment, "stepSize", None)
        self._step_size = step_size if step_size is not None else resolve_simulation_time_options(
            {"defaultStepSize": default_step_size},
        )["stepSize"]
        self._start_time = float(start_time)
        self._stop_time = None if stop_time is None else float(stop_time)
        times_to_validate = (self._start_time, self._step_size)
        if self._stop_time is not None:
            times_to_validate += (self._stop_time,)
        if not all(math.isfinite(value) for value in times_to_validate):
            raise ValueError("startTime and stepSize must be finite; stopTime must be finite when provided")
        if self._stop_time is not None and self._stop_time <= self._start_time:
            raise ValueError("stopTime must be greater than startTime")
        if self._stop_time is not None and not math.isfinite(self._stop_time - self._start_time):
            raise ValueError("Simulation duration must be finite")
        if self._step_size <= 0:
            raise ValueError("stepSize must be positive")
        if self._stop_time is None:
            self._max_simulation_steps = None
        if time_mode not in {"simtime", "realtime", "external"}:
            raise ValueError("timeMode must be simtime, realtime, or external")
        if not math.isfinite(playback_speed) or not 0.1 <= playback_speed <= 100.0:
            raise ValueError("playbackSpeed must be finite and between 0.1 and 100")
        if type(early_return_allowed) is not bool:
            raise ValueError("earlyReturnAllowed must be Boolean")
        self._time = self._start_time
        if parameters is not None and not isinstance(parameters, dict):
            raise ValueError("Initialization values must be an object")
        original_parameters = parameters if parameters is not None else {}
        initialization_values = self._merge_declared_start_values(original_parameters)
        dae_enable_variable = self._dae_enable_variable()
        if dae_enable_variable is not None:
            dae_enable_name = str(dae_enable_variable.name)
            requested_dae_value = original_parameters.get(dae_enable_name)
            if not self._use_me and requested_dae_value is True:
                raise ValueError("FMI-LS-DAE can only be enabled with the ModelExchange execution interface")
        validated_parameters = self._validate_values(initialization_values, phase="initialization")
        if self._use_me and dae_enable_variable is not None:
            self._use_dae = validated_parameters.get(str(dae_enable_variable.name)) is True
        initial_clock_references = self._active_fmi3_input_clock_references(validated_parameters)
        self._initial_parameters = copy.deepcopy(original_parameters)
        self._time_mode = time_mode
        self._playback_speed = float(playback_speed)
        self._early_return_allowed = early_return_allowed
        self._finished_reason = None
        self._step_count = 0

        slave = self._instantiate()
        if slave is None:
            raise RuntimeError("FMU instantiation returned no slave")
        self._slave = slave
        structural_variables = [
            variable for variable in self._md.modelVariables
            if self._fmi_major() == "3"
            and getattr(variable, "causality", None) == "structuralParameter"
        ]
        structural_parameters = {
            name: value
            for name, value in validated_parameters.items()
            if any(variable.name == name for variable in structural_variables)
        }
        dimension_value_references = {
            int(dimension.valueReference)
            for variable in self._md.modelVariables
            for dimension in (getattr(variable, "dimensions", None) or [])
            if getattr(dimension, "valueReference", None) is not None
        }
        dimension_values = {
            int(variable.valueReference): int(structural_parameters[variable.name])
            for variable in structural_variables
            if variable.name in structural_parameters
            and int(variable.valueReference) in dimension_value_references
            and str(getattr(variable, "type", "")) == "UInt64"
            and not isinstance(structural_parameters[variable.name], bool)
        }
        self._pending_event_outputs = None
        if self._fmi_major() == "3":
            if structural_variables:
                slave.enterConfigurationMode()
                if structural_parameters:
                    self._apply_validated_values(structural_parameters)
                self._apply_structural_array_shapes(dimension_values)
                slave.exitConfigurationMode()
            slave.enterInitializationMode(
                startTime=self._start_time,
                stopTime=self._stop_time,
            )
        else:
            slave.setupExperiment(
                startTime=self._start_time,
                stopTime=self._stop_time,
            )
            slave.enterInitializationMode()

        remaining_parameters = {
            name: value for name, value in validated_parameters.items()
            if name not in structural_parameters
            and not self._is_fmi3_input_clock(name)
        }
        if remaining_parameters:
            self._apply_validated_values(remaining_parameters)

        initial_clocked_outputs: dict[str, Any] = {}
        initial_unknown_values: dict[int, Any] = {}
        if self._fmi_major() == "3":
            (
                self._fmi3_input_clock_references,
                self._fmi3_clock_schedules,
            ) = read_fmi3_input_clock_schedules(slave, self._md, self._start_time)
            if self._use_dae and self._use_me and self._fmi_ls_dae is not None:
                clocked_output_names = {
                    str(variable.name)
                    for variable in self._effective_output_variables()
                    if variable.causality == "output" and bool(getattr(variable, "clocks", None))
                }
                initial_outputs = self._read_outputs()
                initial_clocked_outputs = {
                    name: value for name, value in initial_outputs.items()
                    if name in clocked_output_names
                }
                initial_unknown_values = self._read_fmi_ls_dae_initial_unknowns()
            elif self._fmi3_model_has_clocks():
                initial_outputs = self._read_outputs()
                self._fmi3_clock_runtime = Fmi3ClockRuntime(
                    slave,
                    self._md,
                    self._fmi3_input_clock_references,
                    self._fmi3_clock_schedules,
                    variable_size=self._variable_size,
                    variable_is_array=self._is_array,
                    normalize_value=self._normalise_output,
                    initial_outputs=initial_outputs,
                )

        slave.exitInitializationMode()
        if self._use_dae:
            if self._fmi_ls_dae is None:
                raise RuntimeError("FMI-LS-DAE declarations are unavailable")
            default_tolerance = getattr(default_experiment, "tolerance", None)
            self._dae_integrator = Fmi3DaeIntegrator(
                slave,
                self._fmi_ls_dae,
                model_description=self._md,
                start_time=self._start_time,
                relative_tolerance=default_tolerance,
                input_clock_references=self._fmi3_input_clock_references,
                clock_schedules=self._fmi3_clock_schedules,
                initial_input_clock_references=initial_clock_references,
                initial_clocked_outputs=initial_clocked_outputs,
                initial_unknown_values=initial_unknown_values,
            )
            self._collect_dae_event_outputs()
        elif self._use_me:
            self._me_integrator = FmiModelExchangeIntegrator(
                slave,
                self._md,
                start_time=self._start_time,
                max_step=self._step_size,
                relative_tolerance=getattr(default_experiment, "tolerance", None),
                clock_runtime=self._fmi3_clock_runtime,
                initial_input_clock_references=initial_clock_references,
            )
            self._collect_me_event_outputs()
        if self._fmi_major() == "3" and self._fmi3_event_mode_used:
            # FMI 3 Co-Simulation enters Event Mode when Event Mode is enabled.
            # Perform the initial discrete-state iteration before Step Mode.
            initial_clock_references = (
                set(self._fmi3_clock_runtime.due_input_clock_references(self._start_time))
                if self._fmi3_clock_runtime is not None
                else set()
            )
            initial_clock_references.update(
                self._active_fmi3_input_clock_references(validated_parameters)
            )
            if self._resolve_fmi3_event(
                slave,
                enter_event_mode=False,
                current_time=self._start_time,
                active_input_clocks=initial_clock_references,
                defer_input_clock_activation=bool(initial_clock_references),
            ):
                self._finished_reason = "fmu_terminated"
                self._state = "stopped"
                self._initialised = True
                self._terminated = False
                return {
                    "sessionId": self.session_id,
                    "time": self._time,
                    "state": self._state,
                    "timeMode": self._time_mode,
                    "playbackSpeed": self._playback_speed,
                    "executionInterface": self._execution_interface,
                }
        self._initialised = True
        self._terminated = False
        self._state = "initialized"

        return {
            "sessionId": self.session_id,
            "time": self._time,
            "state": "initialized",
            "timeMode": self._time_mode,
            "playbackSpeed": self._playback_speed,
            "executionInterface": self._execution_interface,
        }

    def step(self, step_size: float | None = None) -> dict[str, Any]:
        slave = self._require_slave()
        if self._state in {"paused", "stopped", "error"}:
            raise RuntimeError(f"Session cannot step from state {self._state}")
        h = self._step_size if step_size is None else step_size
        if not math.isfinite(h) or h <= 0:
            raise ValueError("stepSize must be positive")
        requested_h = h
        requested_time = self._time + requested_h
        if not math.isfinite(requested_time) or requested_time <= self._time:
            raise ValueError("stepSize does not produce a finite later time")
        remaining = max(0.0, self._stop_time - self._time) if self._stop_time is not None else requested_h
        tolerance = max(1e-12, abs(self._stop_time) * 1e-12) if self._stop_time is not None else 1e-12
        if self._time_mode == "external" and self._stop_time is not None and requested_h > remaining + tolerance:
            raise ValueError("stepSize would advance past stopTime")
        h = min(requested_h, remaining) if self._stop_time is not None else requested_h
        if h <= 0.0:
            self._state = "stopped"
            self._finished_reason = self._finished_reason or "stop_time_reached"
            return self._step_result(self._time, self._time, finished=True)
        self._ensure_step_budget(1)
        self._step_count += 1
        previous_time = self._time
        reached_time, result = self._do_step(slave, h)
        result["requestedTime"] = requested_time
        if h < requested_h - tolerance:
            result["earlyReturn"] = True
        self._time = reached_time
        if result["terminateSimulation"]:
            self._state = "stopped"
            self._finished_reason = "fmu_termination_requested"
        elif self._stop_time is not None and self._time >= self._stop_time - 1e-12:
            self._state = "stopped"
            self._finished_reason = "stop_time_reached"
        else:
            self._state = "running"
        step_result = self._step_result(previous_time, reached_time, **result)
        step_result["sequence"] = self._step_count
        return step_result

    def run_until(self, target_time: float, step_size: float | None = None) -> dict[str, Any]:
        slave = self._require_slave()
        if not math.isfinite(target_time):
            raise ValueError("targetTime must be finite")
        if target_time < self._time:
            raise ValueError("targetTime must not be earlier than current simulation time")
        if self._state in {"paused", "stopped", "error"}:
            raise RuntimeError(f"Session cannot run from state {self._state}")
        requested_target = target_time
        if self._stop_time is not None:
            target_time = min(target_time, self._stop_time)
        h = self._step_size if step_size is None else step_size
        if not math.isfinite(h) or h <= 0:
            raise ValueError("stepSize must be positive")
        expected_steps = max(0.0, target_time - self._time) / h
        if not math.isfinite(expected_steps):
            raise ValueError("MAX_SIMULATION_STEPS_EXCEEDED")
        estimated_steps = _ceil_step_count(expected_steps, self._time, target_time, h)
        if estimated_steps > config.MAX_SIMULATION_STEPS:
            raise ValueError("MAX_SIMULATION_STEPS_EXCEEDED")
        if self._max_simulation_steps is not None and estimated_steps > self._max_simulation_steps:
            raise ValueError("MAX_SIMULATION_STEPS_EXCEEDED")
        self._ensure_step_budget(estimated_steps)
        self._state = "running"
        initial_time = self._time
        steps = 0
        early_return = False
        event_occurred = False
        event_handling_needed = False
        terminate_simulation = False
        consecutive_no_progress_events = 0
        while self._time < target_time - 1e-12:
            remaining = target_time - self._time
            actual_h = min(h, remaining)
            self._ensure_step_budget(1)
            self._step_count += 1
            steps += 1
            previous_time = self._time
            self._time, step_result = self._do_step(slave, actual_h)
            early_return = early_return or step_result["earlyReturn"]
            event_occurred = event_occurred or step_result["eventOccurred"]
            event_handling_needed = event_handling_needed or step_result["eventHandlingNeeded"]
            terminate_simulation = terminate_simulation or step_result["terminateSimulation"]
            if step_result["terminateSimulation"]:
                self._state = "stopped"
                self._finished_reason = "fmu_termination_requested"
                break
            if self._time <= previous_time + 1e-15:
                consecutive_no_progress_events += 1
                if not step_result["eventOccurred"] or consecutive_no_progress_events >= 100:
                    raise RuntimeError("FMI step returned repeatedly without advancing simulation time")
            else:
                consecutive_no_progress_events = 0
        if self._state != "stopped":
            self._state = "stopped" if self._stop_time is not None and self._time >= self._stop_time - 1e-12 else "initialized"
            if self._state == "stopped":
                self._finished_reason = "stop_time_reached"
        return {
            "time": self._time,
            "state": self._state,
            "requestedTime": requested_target,
            "timeAdvanced": max(0.0, self._time - initial_time),
            "steps": steps,
            "earlyReturn": early_return,
            "eventOccurred": event_occurred,
            "eventHandlingNeeded": event_handling_needed,
            "terminateSimulation": terminate_simulation,
            "finished": self._state == "stopped",
            "finishReason": self._finished_reason,
            **self._fmi3_event_time_metadata(),
        }

    def run_until_streaming(
        self,
        target_time: float,
        step_size: float | None = None,
        output_refs: list[int] | None = None,
        input_schedule: list[dict[str, Any]] | None = None,
        _sequence_offset: int = 0,
        _sample_every_steps: int = 1,
        _include_unsampled: bool = False,
        _sample_step_offset: int | None = None,
        _input_events_applied: list[int] | None = None,
        _execution_started: list[bool] | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Step until *target_time*, yielding output snapshots at each step."""
        if type(_sample_every_steps) is not int or _sample_every_steps < 1:
            raise ValueError("sampleEverySteps must be a positive integer")
        if _sample_step_offset is None:
            _sample_step_offset = self._step_count
        if input_schedule:
            sequence_offset = _sequence_offset
            for event in input_schedule:
                event_time = float(event["time"])
                if event_time < self._time - 1e-12:
                    raise ValueError("Scheduled input time must not be earlier than current simulation time")
                if event_time > self._time + 1e-12:
                    for snapshot in self.run_until_streaming(
                        event_time,
                        step_size=step_size,
                        output_refs=output_refs,
                        _sequence_offset=sequence_offset,
                        _sample_every_steps=_sample_every_steps,
                        _include_unsampled=_include_unsampled,
                        _sample_step_offset=_sample_step_offset,
                        _input_events_applied=_input_events_applied,
                        _execution_started=_execution_started,
                    ):
                        sequence_offset = int(snapshot["seq"]) + 1
                        yield snapshot
                if self._state == "stopped":
                    return
                if _input_events_applied is not None:
                    _input_events_applied[0] += 1
                self.set_inputs(event["values"])
            yield from self.run_until_streaming(
                target_time,
                step_size=step_size,
                output_refs=output_refs,
                _sequence_offset=sequence_offset,
                _sample_every_steps=_sample_every_steps,
                _include_unsampled=_include_unsampled,
                _sample_step_offset=_sample_step_offset,
                _input_events_applied=_input_events_applied,
                _execution_started=_execution_started,
            )
            return

        slave = self._require_slave()
        if not math.isfinite(target_time):
            raise ValueError("targetTime must be finite")
        if target_time < self._time:
            raise ValueError("targetTime must not be earlier than current simulation time")
        if self._state in {"paused", "stopped", "error"}:
            raise RuntimeError(f"Session cannot run from state {self._state}")
        if self._stop_time is not None:
            target_time = min(target_time, self._stop_time)
        h = self._step_size if step_size is None else step_size
        if not math.isfinite(h) or h <= 0:
            raise ValueError("stepSize must be positive")
        expected_steps = max(0.0, target_time - self._time) / h
        if not math.isfinite(expected_steps):
            raise ValueError("MAX_SIMULATION_STEPS_EXCEEDED")
        estimated_steps = _ceil_step_count(expected_steps, self._time, target_time, h)
        if estimated_steps > config.MAX_SIMULATION_STEPS:
            raise ValueError("MAX_SIMULATION_STEPS_EXCEEDED")
        if self._max_simulation_steps is not None and estimated_steps > self._max_simulation_steps:
            raise ValueError("MAX_SIMULATION_STEPS_EXCEEDED")
        self._ensure_step_budget(estimated_steps)
        self._state = "running"
        seq = _sequence_offset
        consecutive_no_progress_events = 0
        while self._time < target_time - 1e-12:
            remaining = target_time - self._time
            actual_h = min(h, remaining)
            self._ensure_step_budget(1)
            self._step_count += 1
            previous_time = self._time
            if _execution_started is not None:
                _execution_started[0] = True
            self._time, step_result = self._do_step(slave, actual_h)
            if self._time <= previous_time + 1e-15 and not step_result["terminateSimulation"]:
                consecutive_no_progress_events += 1
                if not step_result["eventOccurred"] or consecutive_no_progress_events >= 100:
                    raise RuntimeError("FMI step returned repeatedly without advancing simulation time")
            else:
                consecutive_no_progress_events = 0
            stop_time_reached = self._stop_time is not None and self._time >= self._stop_time - 1e-12
            finished = step_result["terminateSimulation"] or stop_time_reached
            if step_result["terminateSimulation"]:
                self._state = "stopped"
                self._finished_reason = "fmu_termination_requested"
            elif finished:
                self._state = "stopped"
                self._finished_reason = "stop_time_reached"
            at_requested_target = self._time >= target_time - 1e-12
            has_event = bool(
                step_result.get("eventOccurred")
                or step_result.get("eventHandlingNeeded")
                or step_result.get("earlyReturn")
                or step_result.get("terminateSimulation")
            )
            should_sample = (
                (self._step_count - _sample_step_offset) % _sample_every_steps == 0
                or at_requested_target
                or finished
                or has_event
            )
            outputs = self._read_outputs(output_refs) if should_sample or not _include_unsampled else None
            yield {
                "type": "sim.step",
                "seq": seq,
                "sequence": self._step_count,
                "time": self._time,
                "outputs": outputs,
                **step_result,
                "finished": finished,
                "finishReason": self._finished_reason if finished else None,
            }
            seq += 1
            if step_result["terminateSimulation"]:
                return
            if finished:
                return
        if self._stop_time is not None and self._time >= self._stop_time - 1e-12:
            self._state = "stopped"
            self._finished_reason = "stop_time_reached"
        else:
            self._state = "initialized"

    def _ensure_step_budget(self, additional_steps: int) -> None:
        if self._max_simulation_steps is not None and self._step_count + additional_steps > self._max_simulation_steps:
            raise ValueError("MAX_SIMULATION_STEPS_EXCEEDED")

    def advance(
        self,
        target_time: float,
        max_samples: int = 256,
        step_size: float | None = None,
        input_schedule: list[dict[str, Any]] | None = None,
        output_sample_every_steps: int = 1,
        output_refs: list[int] | None = None,
        max_compute_ms: float | None = None,
    ) -> dict[str, Any]:
        """Advance one bounded request, optionally applying timed inputs and sampling sparsely."""
        started_at = _time.perf_counter()
        if type(max_samples) is not int or not 1 <= max_samples <= 256:
            raise AdvancePreExecutionError("maxSamples must be an integer between 1 and 256")
        if type(output_sample_every_steps) is not int or output_sample_every_steps < 1:
            raise AdvancePreExecutionError("outputSampleEverySteps must be a positive integer")
        if max_compute_ms is not None and (
            isinstance(max_compute_ms, bool)
            or not isinstance(max_compute_ms, (int, float))
            or not math.isfinite(max_compute_ms)
            or not 1 <= max_compute_ms <= 1000
        ):
            raise AdvancePreExecutionError("maxComputeMs must be between 1 and 1000 milliseconds")
        h = self._step_size if step_size is None else step_size
        if not math.isfinite(target_time) or not math.isfinite(h) or h <= 0:
            raise AdvancePreExecutionError("targetTime must be finite and stepSize must be positive")
        if target_time <= self._time + 1e-15:
            raise AdvancePreExecutionError("targetTime must be later than the current simulation time")
        input_events_applied = [0]
        try:
            effective_target = min(target_time, self._stop_time) if self._stop_time is not None else target_time
            requested_steps = _ceil_step_count(
                max(0.0, effective_target - self._time) / h,
                self._time,
                effective_target,
                h,
            )
            if requested_steps > config.MAX_SIMULATION_STEPS:
                raise AdvancePreExecutionError("MAX_SIMULATION_STEPS_EXCEEDED")
            expected_samples = estimate_advance_samples(
                self._time,
                target_time,
                self._stop_time,
                h,
                output_sample_every_steps,
            )
            if expected_samples < 1:
                raise AdvancePreExecutionError("targetTime must advance at least one model sample")
            if input_schedule and expected_samples > max_samples:
                raise AdvancePreExecutionError("ADVANCE_SAMPLE_LIMIT_EXCEEDED")

            outputs = self.get_outputs(output_refs)["outputs"]
            encoded_outputs = json.dumps(outputs, separators=(",", ":"), allow_nan=False)
            sample_bound = 256 + len(encoded_outputs.encode("utf-8"))
            safe_count = min(max_samples, 900_000 // sample_bound)
            if safe_count < 1:
                raise AdvancePreExecutionError("One output sample exceeds the WebSocket frame budget")
            if input_schedule and expected_samples > safe_count:
                raise AdvancePreExecutionError("ADVANCE_SAMPLE_LIMIT_EXCEEDED")
        except AdvancePreExecutionError:
            raise
        except Exception as exc:
            raise AdvancePreExecutionError(str(exc), input_events_applied[0]) from exc

        samples = []
        steps_executed = 0
        stop_reason = "target_reached"
        compute_budget_overrun_ms = 0.0
        execution_started = [False]
        snapshots = self.run_until_streaming(
            target_time,
            step_size=step_size,
            output_refs=output_refs,
            input_schedule=input_schedule,
            _sample_every_steps=output_sample_every_steps,
            _include_unsampled=True,
            _input_events_applied=input_events_applied,
            _execution_started=execution_started,
        )
        try:
            for snapshot in snapshots:
                steps_executed += 1
                elapsed_ms = (_time.perf_counter() - started_at) * 1000
                compute_budget_reached = max_compute_ms is not None and elapsed_ms >= max_compute_ms
                if snapshot["outputs"] is not None or compute_budget_reached:
                    sample_outputs = snapshot["outputs"]
                    if sample_outputs is None:
                        sample_outputs = self._read_outputs(output_refs)
                    samples.append({
                        "time": snapshot["time"],
                        "outputs": sample_outputs,
                        "sequence": snapshot["sequence"],
                        "eventOccurred": bool(snapshot.get("eventOccurred", False)),
                        "eventHandlingNeeded": bool(snapshot.get("eventHandlingNeeded", False)),
                        "earlyReturn": bool(snapshot.get("earlyReturn", False)),
                        "terminateSimulation": bool(snapshot.get("terminateSimulation", False)),
                        "finished": bool(snapshot.get("finished", False)),
                        **{
                            key: snapshot[key]
                            for key in ("nextEventTimeDefined", "nextEventTime")
                            if key in snapshot
                        },
                    })

                if max_compute_ms is not None:
                    compute_budget_overrun_ms = max(0.0, elapsed_ms - max_compute_ms)
                if snapshot.get("finished") or snapshot.get("terminateSimulation"):
                    stop_reason = "simulation_finished"
                    break
                if snapshot.get("earlyReturn"):
                    stop_reason = "early_return"
                    break
                if snapshot["time"] >= target_time - 1e-12:
                    stop_reason = "target_reached"
                    break
                if len(samples) >= safe_count:
                    stop_reason = "max_samples"
                    break
                if compute_budget_reached:
                    stop_reason = "compute_budget"
                    break
        except Exception as exc:
            if not execution_started[0]:
                raise AdvancePreExecutionError(str(exc), input_events_applied[0]) from exc
            raise AdvanceExecutionError(str(exc), input_events_applied[0]) from exc
        finally:
            snapshots.close()
            if self._state == "running":
                self._state = "initialized"

        execution_ms = (_time.perf_counter() - started_at) * 1000
        if not samples:
            if not execution_started[0]:
                raise AdvancePreExecutionError("FMU advance returned no samples", input_events_applied[0])
            raise AdvanceExecutionError("FMU advance returned no samples", input_events_applied[0])
        try:
            sample_bytes = len(json.dumps(samples, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise AdvanceExecutionError("Batched samples are not JSON-compatible", input_events_applied[0]) from exc
        result = {
            "samples": samples,
            "simTime": self._time,
            "finished": self._state == "stopped",
            "finishReason": self._finished_reason,
            "stopReason": stop_reason,
            "stepsExecuted": steps_executed,
            "inputEventsApplied": input_events_applied[0],
            "samplesReturned": len(samples),
            "sampleBytes": sample_bytes,
            "computeMs": execution_ms,
            "computeBudgetOverrunMs": compute_budget_overrun_ms,
            **self._fmi3_event_time_metadata(),
        }
        if max_compute_ms is not None:
            result["maxComputeMs"] = float(max_compute_ms)
        return result

    def set_inputs(self, values: dict[str, Any]) -> None:
        self._ensure_live()
        if self._state in {"stopped", "error"}:
            raise RuntimeError(f"Session cannot accept inputs from state {self._state}")
        validated = self._validate_values(values, phase="runtime")
        if self._dae_integrator is not None and validated:
            try:
                requires_event_mode = self._dae_change_requires_event_mode(validated)
                if requires_event_mode:
                    terminated = self._apply_dae_event_mode_values(validated)
                    if terminated:
                        self._state = "stopped"
                        self._finished_reason = "fmu_termination_requested"
                else:
                    self._apply_validated_values(validated)
                if self._state != "stopped" and not requires_event_mode:
                    self._dae_integrator.restart_after_input_change(self._time, step_size=self._step_size)
                self._collect_dae_event_outputs()
            except Exception:
                self._state = "error"
                raise
            return

        if self._me_integrator is not None and validated:
            active_clock_references = self._active_fmi3_input_clock_references(validated)
            model_values = {
                name: value
                for name, value in validated.items()
                if not self._is_fmi3_input_clock(name)
            }
            terminated = self._me_integrator.apply_input_change(
                self._time,
                lambda: self._apply_validated_values(model_values),
                active_clock_references=active_clock_references,
                refresh_tunable_clocks=self._contains_tunable_parameter(validated),
            )
            self._collect_me_event_outputs()
            if terminated:
                self._state = "stopped"
                self._finished_reason = "fmu_termination_requested"
        elif self._fmi_major() == "3" and self._fmi3_event_mode_used and validated:
            self._apply_fmi3_cosimulation_event(validated)
        else:
            self._apply_validated_values(validated)

    def validate_reconfiguration_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate writes that arrive through FMI 3 Reconfiguration Mode."""
        self._ensure_live()
        return self._validate_values(values, phase="reconfiguration")

    def reconfigure_structural_parameters(self, values: dict[str, Any]) -> None:
        """Apply tunable FMI 3 structural parameters at a legal reconfiguration point."""
        self._ensure_live()
        validated = self._validate_values(values, phase="reconfiguration")
        if self._fmi_major() != "3":
            raise ValueError("Structural reconfiguration requires FMI 3")
        if not self._initialised or self._state in {"stopped", "error"}:
            raise RuntimeError("Structural reconfiguration requires an active simulation")
        if not validated:
            return

        slave = self._require_slave()
        model_description = self._md
        if model_description is None:
            raise RuntimeError("FMU model description is unavailable")
        dimension_value_references = {
            int(dimension.valueReference)
            for variable in model_description.modelVariables
            for dimension in (getattr(variable, "dimensions", None) or [])
            if getattr(dimension, "valueReference", None) is not None
        }
        dimension_values: dict[int, int] = {}
        for name, value in validated.items():
            variable = next((item for item in model_description.modelVariables if item.name == name), None)
            if variable is None:
                raise ValueError(f"Unknown structural parameter '{name}'")
            reference = int(variable.valueReference)
            if reference in dimension_value_references:
                if str(getattr(variable, "type", "")) != "UInt64" or isinstance(value, bool):
                    raise ValueError(f"Structural dimension '{name}' must be UInt64")
                extent = int(value)
                if extent < 0:
                    raise ValueError(f"Reconfigured structural dimension '{name}' must be non-negative")
                dimension_values[reference] = extent
        for variable in model_description.modelVariables:
            reference = int(getattr(variable, "valueReference", -1))
            if reference not in dimension_value_references or reference in dimension_values:
                continue
            getter = getattr(slave, f"get{self._variable_type(variable)}", None)
            current_values = getter([reference]) if getter is not None else []
            current_value: Any = current_values[0] if current_values else self._initial_parameters.get(
                str(variable.name), getattr(variable, "start", None)
            )
            if current_value is None:
                raise ValueError(f"Structural dimension '{variable.name}' has no current value")
            try:
                dimension_extent = int(current_value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"Structural dimension '{variable.name}' must be a non-negative UInt64") from exc
            if isinstance(current_value, bool) or dimension_extent != current_value or dimension_extent < 0:
                raise ValueError(f"Structural dimension '{variable.name}' must be a non-negative UInt64")
            dimension_values[reference] = dimension_extent

        definition = self._fmi_ls_dae
        enable_variable = self._dae_enable_variable()
        enable_name = str(enable_variable.name) if enable_variable is not None else None
        target_dae_mode = (
            validated[enable_name]
            if enable_name is not None and enable_name in validated
            else self._use_dae
        )
        try:
            if self._use_me:
                slave.enterEventMode()
            slave.enterConfigurationMode()
            self._apply_validated_values(validated)
            self._apply_structural_array_shapes(dimension_values)
            slave.exitConfigurationMode()

            if self._use_me:
                initial_clocked_outputs = (
                    self._dae_integrator.clocked_output_cache()
                    if self._dae_integrator is not None
                    else (
                        self._fmi3_clock_runtime.clocked_output_cache()
                        if self._fmi3_clock_runtime is not None
                        else {}
                    )
                )
                if self._dae_integrator is not None:
                    self._dae_integrator.close()
                    self._dae_integrator = None
                if self._me_integrator is not None:
                    self._me_integrator.close()
                    self._me_integrator = None
                self._fmi3_clock_runtime = None

                default_tolerance = getattr(getattr(self._md, "defaultExperiment", None), "tolerance", None)
                if target_dae_mode:
                    if definition is None:
                        raise ValueError("DAE mode is enabled but the FMI-LS-DAE manifest is unavailable")
                    self._dae_integrator = Fmi3DaeIntegrator(
                        slave,
                        definition,
                        model_description=self._md,
                        start_time=self._time,
                        relative_tolerance=default_tolerance,
                        input_clock_references=self._fmi3_input_clock_references,
                        clock_schedules=self._fmi3_clock_schedules,
                        initial_clocked_outputs=initial_clocked_outputs,
                    )
                    self._collect_dae_event_outputs()
                else:
                    self._fmi3_clock_runtime = self._create_fmi3_clock_runtime(initial_clocked_outputs)
                    self._me_integrator = FmiModelExchangeIntegrator(
                        slave,
                        self._md,
                        start_time=self._time,
                        max_step=self._step_size,
                        relative_tolerance=default_tolerance,
                        clock_runtime=self._fmi3_clock_runtime,
                    )
                    self._collect_me_event_outputs()
                self._use_dae = target_dae_mode
        except Exception:
            self._state = "error"
            raise

    def _dae_change_requires_event_mode(self, values: dict[str, Any]) -> bool:
        """Treat runtime input changes and tunable writes as FMI event instants."""
        model_description = self._md
        if model_description is None:
            raise RuntimeError("FMU model description is not loaded")
        variables = {str(variable.name): variable for variable in model_description.modelVariables}
        for name in values:
            variable = variables.get(name)
            if variable is None:
                continue
            causality = str(getattr(variable, "causality", ""))
            variability = str(getattr(variable, "variability", ""))
            if causality == "input" or (causality == "parameter" and variability == "tunable"):
                return True
        return False

    def _apply_dae_event_mode_values(self, values: dict[str, Any]) -> bool:
        """Apply tunable/discrete DAE changes through FMI 3 Event Mode."""
        if self._fmi_major() != "3":
            raise RuntimeError("FMI-LS-DAE event updates require FMI 3")
        if self._dae_integrator is None:
            raise RuntimeError("FMI-LS-DAE event handling is not initialized")
        model_description = self._md
        if model_description is None:
            raise RuntimeError("FMU model description is not loaded")
        variables = {str(variable.name): variable for variable in model_description.modelVariables}
        active_clock_references = [
            int(variables[name].valueReference)
            for name, value in values.items()
            if value is True
            and name in variables
            and str(getattr(variables[name], "type", "")) == "Clock"
        ]
        tunable_parameter_changed = any(
            name in variables
            and str(getattr(variables[name], "causality", "")) == "parameter"
            and str(getattr(variables[name], "variability", "")) == "tunable"
            for name in values
        )
        non_clock_values = {
            name: value
            for name, value in values.items()
            if name not in variables or str(getattr(variables[name], "type", "")) != "Clock"
        }
        return self._dae_integrator.apply_event_mode(
            self._time,
            lambda: self._apply_validated_values(non_clock_values),
            step_size=self._step_size,
            active_clock_references=active_clock_references,
            refresh_tunable_clocks=tunable_parameter_changed,
        )

    def _collect_dae_event_outputs(self) -> None:
        if self._dae_integrator is None:
            return
        outputs = self._dae_integrator.take_event_outputs()
        if outputs:
            self._pending_event_outputs = {**(self._pending_event_outputs or {}), **outputs}

    def capture_fmu_state(self) -> dict[str, Any]:
        """Capture a serialized checkpoint when the source FMU supports it."""
        slave = self._require_slave()
        if not self._supports_serialized_fmu_state():
            raise RuntimeError("FMU state capture is not supported by this model")
        state = None
        try:
            state = slave.getFMUState()
            serialized = slave.serializeFMUState(state)
        except Exception as exc:
            raise RuntimeError("The FMU could not serialize its current state") from exc
        finally:
            if state is not None:
                try:
                    slave.freeFMUState(state)
                except Exception:
                    logger.debug("Could not free captured FMU state for session %s", self.session_id, exc_info=True)
        if not isinstance(serialized, (bytes, bytearray)) or not serialized:
            raise RuntimeError("The FMU returned an invalid serialized state")
        from .fmu_state import MAX_FMU_STATE_BYTES
        if len(serialized) > MAX_FMU_STATE_BYTES:
            raise ValueError("FMU state checkpoint exceeds the size limit")
        execution_mode = (
            "fmiLsDae" if self._use_me and self._use_dae
            else "modelExchange" if self._use_me
            else "coSimulation"
        )
        simulation: dict[str, Any] = {
            "executionMode": execution_mode,
            "time": self._time,
            "startTime": self._start_time,
            "stopTime": self._stop_time,
            "stepSize": self._step_size,
            "stepCount": self._step_count,
            "timeMode": self._time_mode,
            "playbackSpeed": self._playback_speed,
            "earlyReturnAllowed": self._early_return_allowed,
            "initialParameters": copy.deepcopy(self._initial_parameters),
        }
        if self._dae_integrator is not None:
            simulation["fmiLsDae"] = self._dae_integrator.snapshot_event_schedule()
        if self._me_integrator is not None:
            simulation["modelExchange"] = self._me_integrator.snapshot_event_schedule()
        if self._fmi_major() == "3" and not self._use_me:
            fmi3_cosimulation: dict[str, Any] = {
                "nextEventTimeDefined": self._fmi3_cs_next_event_time_defined,
            }
            if self._fmi3_clock_runtime is not None:
                fmi3_cosimulation.update(self._fmi3_clock_runtime.snapshot())
            if self._fmi3_cs_next_event_time_defined and self._fmi3_cs_next_event_time is not None:
                fmi3_cosimulation["nextEventTime"] = self._fmi3_cs_next_event_time
            simulation["fmi3CoSimulation"] = fmi3_cosimulation
        return {
            "fmuSha256": self.fmu_sha256,
            "fmiVersion": str(getattr(self._md, "fmiVersion", "")),
            "platform": f"{platform.system().lower()}/{platform.machine().lower()}",
            "serializedState": base64.b64encode(bytes(serialized)).decode("ascii"),
            "simulation": simulation,
        }

    def restore_fmu_state(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Restore a signed checkpoint into an initialized FMU instance."""
        slave = self._require_slave()
        initialized_for_run = self._state == "initialized"
        if not self._supports_serialized_fmu_state():
            raise RuntimeError("FMU state restore is not supported by this model")
        if not isinstance(snapshot, dict):
            raise ValueError("FMU state checkpoint is invalid or incompatible")
        if snapshot.get("fmuSha256") != self.fmu_sha256:
            raise ValueError("FMU state checkpoint belongs to a different model")
        if str(snapshot.get("fmiVersion") or "") != str(getattr(self._md, "fmiVersion", "")):
            raise ValueError("FMU state checkpoint belongs to a different FMI version")
        expected_platform = f"{platform.system().lower()}/{platform.machine().lower()}"
        if snapshot.get("platform") != expected_platform:
            raise ValueError("FMU state checkpoint belongs to a different execution platform")
        simulation = snapshot.get("simulation")
        encoded_state = snapshot.get("serializedState")
        if not isinstance(simulation, dict) or not isinstance(encoded_state, str) or not encoded_state:
            raise ValueError("FMU state checkpoint is invalid or incompatible")
        from .fmu_state import MAX_FMU_STATE_CHECKPOINT_JSON_BYTES
        if len(encoded_state) > MAX_FMU_STATE_CHECKPOINT_JSON_BYTES:
            raise ValueError("FMU state checkpoint exceeds the size limit")
        raw_model_exchange_event_state: Any = None
        model_exchange_event_state: dict[str, Any] = {}
        raw_dae_event_state: Any = None
        raw_fmi3_cosimulation_clock_state: Any = None
        execution_mode: str | None = None
        fmi3_cs_next_event_time_defined = False
        fmi3_cs_next_event_time: float | None = None
        try:
            serialized = base64.b64decode(encoded_state, validate=True)
            raw_time = simulation["time"]
            raw_step_count = simulation["stepCount"]
            if isinstance(raw_time, bool) or not isinstance(raw_time, (int, float)):
                raise ValueError("invalid time")
            if type(raw_step_count) is not int:
                raise ValueError("invalid step count")
            time_value = float(raw_time)
            step_count = raw_step_count
            initial_parameters = simulation["initialParameters"]
            execution_mode = simulation["executionMode"]
            raw_model_exchange_event_state = simulation.get("modelExchange")
            raw_dae_event_state = simulation.get("fmiLsDae")
            raw_fmi3_cosimulation_clock_state = simulation.get("fmi3CoSimulation")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("FMU state checkpoint is invalid or incompatible") from exc
        if not math.isfinite(time_value) or time_value < self._start_time or (
            self._stop_time is not None and time_value >= self._stop_time
        ):
            raise ValueError("FMU state checkpoint time must fall within the new simulation interval")
        if step_count < 0 or (self._max_simulation_steps is not None and step_count > self._max_simulation_steps):
            raise ValueError("FMU state checkpoint exceeds the simulation step budget")
        if not isinstance(initial_parameters, dict):
            raise ValueError("FMU state checkpoint is invalid or incompatible")
        if execution_mode not in {"coSimulation", "modelExchange", "fmiLsDae"}:
            raise ValueError("FMU state checkpoint has an invalid execution mode")
        checkpoint_uses_me = execution_mode in {"modelExchange", "fmiLsDae"}
        if checkpoint_uses_me != self._use_me:
            raise ValueError("FMU state checkpoint uses a different FMI execution interface")
        checkpoint_uses_dae = execution_mode == "fmiLsDae"
        if checkpoint_uses_dae and (self._fmi_major() != "3" or self._fmi_ls_dae is None):
            raise ValueError("FMU state checkpoint requires unavailable FMI-LS-DAE support")
        if execution_mode == "modelExchange":
            if not isinstance(raw_model_exchange_event_state, dict):
                raise ValueError("FMU state checkpoint is missing Model Exchange event metadata")
            for key, value in raw_model_exchange_event_state.items():
                if not isinstance(key, str):
                    raise ValueError("FMU state checkpoint has invalid Model Exchange event metadata")
                model_exchange_event_state[key] = value
            if self._me_integrator is not None and not self._use_dae:
                self._me_integrator.validate_event_schedule(model_exchange_event_state, time_value)
        if checkpoint_uses_dae and not isinstance(raw_dae_event_state, dict):
            raise ValueError("FMU state checkpoint is missing FMI-LS-DAE event metadata")
        if self._fmi_major() == "3" and not checkpoint_uses_me:
            if not isinstance(raw_fmi3_cosimulation_clock_state, dict):
                raise ValueError("FMU state checkpoint is missing FMI 3 Co-Simulation event metadata")
            raw_next_event_defined = raw_fmi3_cosimulation_clock_state.get("nextEventTimeDefined")
            raw_next_event_time = raw_fmi3_cosimulation_clock_state.get("nextEventTime")
            if type(raw_next_event_defined) is not bool:
                raise ValueError("FMU state checkpoint has invalid FMI 3 Co-Simulation event metadata")
            if raw_next_event_defined:
                if isinstance(raw_next_event_time, bool) or not isinstance(raw_next_event_time, (int, float)):
                    raise ValueError("FMU state checkpoint has invalid FMI 3 Co-Simulation event time")
                fmi3_cs_next_event_time = float(raw_next_event_time)
                if not math.isfinite(fmi3_cs_next_event_time) or fmi3_cs_next_event_time < time_value - max(
                    1e-12,
                    abs(time_value) * 1e-12,
                ):
                    raise ValueError("FMU state checkpoint has an FMI 3 Co-Simulation event before its time")
                fmi3_cs_next_event_time_defined = True
            if self._fmi3_clock_runtime is not None:
                self._fmi3_clock_runtime.validate_snapshot(raw_fmi3_cosimulation_clock_state, time_value)
        state = None
        try:
            state = slave.deserializeFMUState(serialized)
            slave.setFMUState(state)
        except Exception as exc:
            raise ValueError("The FMU could not restore the checkpoint") from exc
        finally:
            if state is not None:
                try:
                    slave.freeFMUState(state)
                except Exception:
                    logger.debug("Could not free restored FMU state for session %s", self.session_id, exc_info=True)
        self._time = time_value
        self._step_count = step_count
        if checkpoint_uses_me:
            if self._dae_integrator is not None:
                self._dae_integrator.close()
                self._dae_integrator = None
            if self._me_integrator is not None:
                self._me_integrator.close()
                self._me_integrator = None
            slave.setTime(self._time)
            if checkpoint_uses_dae:
                dae_definition = self._fmi_ls_dae
                model_description = self._md
                if dae_definition is None or model_description is None:
                    raise ValueError("FMU state checkpoint requires unavailable FMI-LS-DAE metadata")
                clocked_outputs = raw_dae_event_state.get("clockedOutputCache", {})
                if not isinstance(clocked_outputs, dict):
                    raise ValueError("FMU state checkpoint has invalid DAE clocked output metadata")
                self._fmi3_clock_runtime = None
                self._dae_integrator = Fmi3DaeIntegrator(
                    slave,
                    dae_definition,
                    model_description=model_description,
                    start_time=self._time,
                    relative_tolerance=getattr(getattr(self._md, "defaultExperiment", None), "tolerance", None),
                    input_clock_references=self._fmi3_input_clock_references,
                    clock_schedules=self._fmi3_clock_schedules,
                    initial_clocked_outputs=clocked_outputs,
                    restoring_state=True,
                )
                self._dae_integrator.restore_event_schedule(raw_dae_event_state, self._time)
                self._use_dae = True
            else:
                self._fmi3_clock_runtime = (
                    self._create_fmi3_clock_runtime()
                    if self._fmi_major() == "3" and self._fmi3_model_has_clocks()
                    else None
                )
                self._me_integrator = FmiModelExchangeIntegrator(
                    slave,
                    self._md,
                    start_time=self._time,
                    max_step=self._step_size,
                    relative_tolerance=getattr(getattr(self._md, "defaultExperiment", None), "tolerance", None),
                    clock_runtime=self._fmi3_clock_runtime,
                    restoring_state=True,
                )
                self._me_integrator.restore_event_schedule(model_exchange_event_state, self._time)
                self._use_dae = False
            self._pending_event_outputs = None
        if self._fmi_major() == "3" and not checkpoint_uses_me:
            if self._fmi3_clock_runtime is not None:
                self._fmi3_clock_runtime.restore_snapshot(raw_fmi3_cosimulation_clock_state, self._time)
            self._fmi3_cs_next_event_time_defined = fmi3_cs_next_event_time_defined
            self._fmi3_cs_next_event_time = fmi3_cs_next_event_time
        elif not checkpoint_uses_me:
            self._fmi3_cs_next_event_time_defined = False
            self._fmi3_cs_next_event_time = None
        self._initial_parameters = copy.deepcopy(initial_parameters)
        self._finished_reason = None
        self._terminated = False
        # A checkpoint supplied to sim.initialize is restored before the first
        # explicit sim.advance. Keep that newly initialized session runnable;
        # restoring into an existing paused session should remain paused.
        self._state = "initialized" if initialized_for_run else "paused"
        result = {"state": self._state, "simTime": self._time}
        result.update(self._fmi3_event_time_metadata())
        return result

    def _supports_serialized_fmu_state(self) -> bool:
        interface = getattr(self._md, "modelExchange", None) if self._use_me else getattr(self._md, "coSimulation", None)
        if interface is None:
            return False
        return bool(
            getattr(interface, "canGetAndSetFMUstate", getattr(interface, "canGetAndSetFMUState", False))
            and getattr(interface, "canSerializeFMUstate", getattr(interface, "canSerializeFMUState", False))
        )

    def validate_initial_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate initialization parameters before a scenario is charged."""
        if self._terminated:
            raise RuntimeError("Session already terminated")
        if self._initialised:
            raise RuntimeError("Session already initialised")
        return self._validate_values(values, phase="initialization")

    def validate_inputs(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize runtime variable writes without changing FMU state."""
        self._ensure_live()
        if self._state in {"stopped", "error"}:
            raise RuntimeError(f"Session cannot accept inputs from state {self._state}")
        return self._validate_values(values, phase="runtime")

    def validate_input_schedule(self, input_schedule: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate timed runtime writes in one call without changing FMU state."""
        self._ensure_live()
        if self._state in {"stopped", "error"}:
            raise RuntimeError(f"Session cannot accept inputs from state {self._state}")
        return [
            {"time": event["time"], "values": self._validate_values(event["values"], phase="runtime")}
            for event in input_schedule
        ]

    def validate_runtime_input_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate input values against the loaded model before initialization."""
        if self._terminated:
            raise RuntimeError("Session already terminated")
        if self._md is None:
            raise RuntimeError("FMU not loaded – call load() first")
        return self._validate_values(values, phase="runtime")

    def get_outputs(self, refs: list[int] | None = None) -> dict[str, Any]:
        self._ensure_live()
        result = {"time": self._time, "outputs": self._read_outputs(refs)}
        result.update(self._fmi3_event_time_metadata())
        if self._fmi_major() == "3" and not self._use_me:
            result["terminateSimulation"] = self._finished_reason == "fmu_termination_requested"
            result["finished"] = self._state == "stopped"
        return result

    def _fmi3_event_time_metadata(self) -> dict[str, Any]:
        if self._fmi_major() != "3":
            return {}
        if self._use_me:
            integrator = self._dae_integrator if self._use_dae else self._me_integrator
            snapshot = getattr(integrator, "snapshot_event_schedule", None)
            event_schedule = snapshot() if callable(snapshot) else {}
            if isinstance(event_schedule, dict):
                return {
                    key: event_schedule[key]
                    for key in ("nextEventTimeDefined", "nextEventTime")
                    if key in event_schedule
                }
            return {"nextEventTimeDefined": False}
        result: dict[str, Any] = {"nextEventTimeDefined": self._fmi3_cs_next_event_time_defined}
        if self._fmi3_cs_next_event_time_defined and self._fmi3_cs_next_event_time is not None:
            result["nextEventTime"] = self._fmi3_cs_next_event_time
        return result

    def pause(self) -> dict[str, Any]:
        """Pause automatic execution without destroying FMU state."""
        self._ensure_live()
        if self._state == "running":
            self._state = "paused"
        return {"time": self._time, "state": self._state}

    def resume(self) -> dict[str, Any]:
        """Mark the session runnable; the websocket loop performs stepping."""
        self._ensure_live()
        if self._time_mode == "external":
            raise RuntimeError("EXTERNAL_STEP_MODE does not support automatic stepping")
        if self._state not in ("paused", "initialized"):
            raise RuntimeError(f"Cannot resume from state {self._state}")
        self._state = "running"
        return {"time": self._time, "state": self._state}

    def reset(self, defer_initialization: bool = False) -> dict[str, Any]:
        """Recreate the FMU, optionally leaving it loaded for a fresh init call."""
        if self._terminated:
            raise RuntimeError("Session already terminated")
        if self._md is None:
            raise RuntimeError("FMU is not loaded")
        if type(defer_initialization) is not bool:
            raise ValueError("deferInitialization must be Boolean")
        self._release_fmu()
        self._time = self._start_time
        self._finished_reason = None
        self._step_count = 0
        self._pending_samples.clear()
        self._pending_queue_drops = 0
        if self.subscription is not None:
            self.subscription.last_emit_monotonic = 0.0
            self.subscription.rate_dropped = 0
        self._state = "loaded"
        if defer_initialization:
            return {"sessionId": self.session_id, "time": self._time, "state": "loaded"}
        return self.initialize(
            start_time=self._start_time,
            stop_time=self._stop_time,
            step_size=self._step_size,
            parameters=self._initial_parameters,
            time_mode=self._time_mode,
            playback_speed=self._playback_speed,
            early_return_allowed=self._early_return_allowed,
        )

    @property
    def state(self) -> str:
        if self._terminated:
            return "terminated"
        if self._state == "new":
            return "loaded" if self._md is not None else "new"
        return self._state

    def sample_subscription(self) -> dict[str, Any] | None:
        """Collect a subscription sample.  Returns an output-event dict ready
        to send, or ``None`` if the subscription rate-limit has not elapsed."""
        subscription = self.subscription
        if not subscription:
            return None
        if subscription.seconds_until_due() > 0:
            subscription.rate_dropped += 1
            return None

        # Resolve variable name filter → valueReference filter
        var_refs: list[int] | None = None
        if subscription.variables is not None and self._md:
            name_set = set(subscription.variables)
            var_refs = [
                v.valueReference
                for v in self._md.modelVariables
                if v.name in name_set and v.causality == "output"
            ]

        sample = self._read_outputs(var_refs)
        if self.subscription is not subscription:
            return None
        self._pending_samples.append(sample)

        # Enforce max batch size
        if len(self._pending_samples) > subscription.max_batch_size:
            excess = len(self._pending_samples) - subscription.max_batch_size
            self._pending_samples = self._pending_samples[excess:]
            subscription.rate_dropped += excess

        now = _time.monotonic()
        subscription.last_emit_monotonic = now
        values = self._pending_samples[-1]
        batch_size = len(self._pending_samples)
        self._pending_samples.clear()
        dropped = subscription.rate_dropped + self._pending_queue_drops
        subscription.rate_dropped = 0
        self._pending_queue_drops = 0

        payload = {
            "type": "sim.outputs",
            "sessionId": self.session_id,
            "seq": self.seq,
            "dropped": dropped,
            "batchSize": batch_size,
            "simTime": self._time,
            "values": values,
        }
        self.seq += 1
        return payload

    def terminate(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        self._attached = False
        self._attach_deadline = None
        self._attachment_owner = None
        self._release_fmu()
        self._cleanup_temp()

    def mark_detached(self, grace_seconds: float, attachment_owner: object | None = None) -> None:
        # A previous Station websocket can finish its cleanup after a newer
        # websocket has already attached the same FMU session. In that case,
        # the stale cleanup must not detach the newer connection.
        if attachment_owner is not None and self._attachment_owner is not attachment_owner:
            return
        if not self._terminated:
            self._attached = False
            self._attach_deadline = _time.time() + max(0.0, float(grace_seconds))
            self._attachment_owner = None

    def mark_attached(self, attachment_owner: object | None = None) -> None:
        self._attached = True
        self._attach_deadline = None
        self._attachment_owner = attachment_owner

    def can_attach(self, now: float | None = None) -> bool:
        if self._terminated:
            return False
        if not self._attached and self._attach_deadline is None:
            return False
        now = _time.time() if now is None else now
        if self.expires_at is not None:
            try:
                expires_at = float(self.expires_at)
                if not math.isfinite(expires_at) or expires_at <= now:
                    return False
            except (TypeError, ValueError):
                return False
        claims = self.gateway_context.get("claims") or {}
        reservation_end = self.gateway_context.get("reservationEndsAt")
        if reservation_end is None and isinstance(claims, dict):
            reservation_end = claims.get("reservationEndsAt")
        if reservation_end is not None:
            try:
                reservation_ends_at = float(reservation_end)
                if not math.isfinite(reservation_ends_at) or reservation_ends_at <= now:
                    return False
            except (TypeError, ValueError, OverflowError):
                return False
        return self._attach_deadline is None or now < self._attach_deadline

    # ── private ──────────────────────────────────────────────────

    def _ensure_live(self) -> None:
        if self._terminated:
            raise RuntimeError("Session already terminated")
        if not self._initialised:
            raise RuntimeError("Session not initialised")

    def _require_slave(self) -> Any:
        self._ensure_live()
        if self._slave is None:
            raise RuntimeError("FMU slave is not initialized")
        return self._slave

    def _fmi_major(self) -> str:
        return str(getattr(self._md, "fmiVersion", "2.0")).split(".", 1)[0]

    def _has_native_interface_binary(self, interface: Any) -> bool:
        if self._extract_dir is None or interface is None:
            return False
        model_identifier = str(getattr(interface, "modelIdentifier", "") or "")
        if not model_identifier:
            return False
        directories = [platform_tuple]
        if self._fmi_major() == "2":
            legacy_platform = {
                ("windows", "amd64"): "win64",
                ("windows", "x86_64"): "win64",
                ("windows", "x64"): "win64",
                ("windows", "x86"): "win32",
                ("windows", "i386"): "win32",
                ("windows", "i686"): "win32",
                ("linux", "x86_64"): "linux64",
                ("linux", "amd64"): "linux64",
                ("linux", "x86"): "linux32",
                ("linux", "i386"): "linux32",
                ("linux", "i686"): "linux32",
                ("darwin", "x86_64"): "darwin64",
                ("darwin", "amd64"): "darwin64",
            }.get((platform.system().lower(), platform.machine().lower()))
            if legacy_platform:
                directories.insert(0, legacy_platform)
        return any(
            (self._extract_dir / "binaries" / directory / f"{model_identifier}{sharedLibraryExtension}").is_file()
            for directory in directories
        )

    def _available_execution_interfaces(self) -> list[str]:
        if self._md is None:
            return []
        interfaces = []
        if self._md.coSimulation is not None and self._has_native_interface_binary(self._md.coSimulation):
            interfaces.append("CoSimulation")
        if self._md.modelExchange is not None and self._has_native_interface_binary(self._md.modelExchange):
            interfaces.append("ModelExchange")
        return interfaces

    def _dae_enable_variable(self) -> Any | None:
        if self._md is None or self._fmi_ls_dae is None:
            return None
        return next(
            (
                variable
                for variable in self._md.modelVariables
                if getattr(variable, "valueReference", None)
                == self._fmi_ls_dae.enable_dae_value_reference
            ),
            None,
        )

    def _fmi3_model_has_clocks(self) -> bool:
        if self._md is None or self._fmi_major() != "3":
            return False
        return any(
            self._variable_type(variable) == "Clock" or bool(getattr(variable, "clocks", None))
            for variable in self._md.modelVariables
        )

    def _instantiate(self) -> Any:
        """Instantiate through the selected FMI Co-Simulation or Model Exchange API."""
        if self._extract_dir is None or self._md is None:
            raise RuntimeError("FMU is not loaded")
        if self._use_me:
            model_exchange = getattr(self._md, "modelExchange", None)
            if model_exchange is None:
                raise RuntimeError("FMU does not declare Model Exchange")
            if self._fmi_major() == "3":
                return instantiate_fmi3_model_exchange(self._extract_dir, self._md)
            return fmpy_instantiate_fmu(
                str(self._extract_dir),
                self._md,
                fmi_type="ModelExchange",
            )
        # Negotiate FMI 3 early return and event mode only when the FMU exposes
        # Event Mode. FMPy reports the actual reached time on every FMI 3 step.
        co_simulation = self._md.coSimulation
        has_event_mode = bool(getattr(co_simulation, "hasEventMode", False))
        if self._fmi_major() == "3" and self._fmi3_model_has_clocks() and not has_event_mode:
            raise ValueError("FMI 3 Co-Simulation FMUs with Clocks must declare hasEventMode=true")
        self._fmi3_event_mode_used = bool(
            self._fmi_major() == "3" and has_event_mode
        )
        if self._fmi_major() == "3":
            return instantiate_fmi3_cosimulation(
                self._extract_dir,
                self._md,
                early_return_allowed=self._early_return_allowed,
                event_mode_used=self._fmi3_event_mode_used,
            )
        return fmpy_instantiate_fmu(
            str(self._extract_dir),
            self._md,
            fmi_type="CoSimulation",
        )

    def _do_step(self, slave: Any, step_size: float) -> tuple[float, dict[str, Any]]:
        current_time = self._time
        requested_time = current_time + step_size
        if not math.isfinite(requested_time) or requested_time <= current_time:
            raise ValueError("FMI step does not produce a finite later time")
        if self._use_dae:
            if self._dae_integrator is None:
                raise RuntimeError("FMI-LS-DAE integrator is not initialized")
            result = self._dae_integrator.advance(
                current_time,
                requested_time,
                step_size=step_size,
            )
            self._collect_dae_event_outputs()
            return float(result["time"]), {
                "requestedTime": requested_time,
                "earlyReturn": bool(result["earlyReturn"]),
                "eventOccurred": bool(result["eventOccurred"]),
                "eventHandlingNeeded": False,
                "terminateSimulation": bool(result["terminateSimulation"]),
            }
        if self._use_me:
            if self._me_integrator is None:
                raise RuntimeError("FMI Model Exchange integrator is not initialized")
            result = self._me_integrator.advance(current_time, requested_time)
            self._collect_me_event_outputs()
            return float(result["time"]), {
                "requestedTime": requested_time,
                "earlyReturn": bool(result["earlyReturn"]),
                "eventOccurred": bool(result["eventOccurred"]),
                "eventHandlingNeeded": False,
                "terminateSimulation": bool(result["terminateSimulation"]),
            }
        event_handling_needed = False
        terminate_simulation = False
        early_return = False
        event_occurred = False

        if self._fmi_major() == "3":
            communication_step_size = step_size
            tolerance = max(1e-12, abs(current_time) * 1e-12)
            if self._fmi3_event_mode_used and self._fmi3_clock_runtime is not None:
                clock_time = self._fmi3_clock_runtime.next_event_time
            else:
                clock_time = None
            next_fmi3_event_time = (
                self._fmi3_cs_next_event_time
                if self._fmi3_cs_next_event_time_defined
                else None
            )
            due_clocks = (
                set(self._fmi3_clock_runtime.due_input_clock_references(current_time))
                if self._fmi3_event_mode_used and self._fmi3_clock_runtime is not None
                else set()
            )
            time_event_due = (
                next_fmi3_event_time is not None
                and next_fmi3_event_time <= current_time + tolerance
            )
            if self._fmi3_event_mode_used and (due_clocks or time_event_due):
                terminate_simulation = self._handle_fmi3_event(
                    slave,
                    current_time=current_time,
                    active_input_clocks=due_clocks,
                    defer_input_clock_activation=time_event_due,
                )
                if terminate_simulation:
                    reached_time = current_time
                    return reached_time, {
                        "requestedTime": requested_time,
                        "earlyReturn": True,
                        "eventOccurred": True,
                        "eventHandlingNeeded": False,
                        "terminateSimulation": True,
                    }
                clock_time = self._fmi3_clock_runtime.next_event_time if self._fmi3_clock_runtime else None
                next_fmi3_event_time = (
                    self._fmi3_cs_next_event_time
                    if self._fmi3_cs_next_event_time_defined
                    else None
                )
                if any(
                    value is not None and value <= current_time + tolerance
                    for value in (clock_time, next_fmi3_event_time)
                ):
                    raise RuntimeError("FMI 3 event scheduler did not advance past the current time")

            scheduled_event_times = [
                value for value in (clock_time, next_fmi3_event_time) if value is not None
            ]
            if self._fmi3_event_mode_used and scheduled_event_times:
                next_scheduled_event = min(scheduled_event_times)
                if next_scheduled_event < requested_time - tolerance:
                    communication_step_size = next_scheduled_event - current_time
            event_handling_needed, terminate_simulation, early_return, reached_time = slave.doStep(
                currentCommunicationPoint=current_time,
                communicationStepSize=communication_step_size,
            )
            reached_time = float(reached_time)
            scheduled_clock_references = (
                set(self._fmi3_clock_runtime.due_input_clock_references(reached_time))
                if self._fmi3_clock_runtime is not None
                else set()
            )
            time_event_reached = (
                self._fmi3_cs_next_event_time_defined
                and self._fmi3_cs_next_event_time is not None
                and abs(reached_time - self._fmi3_cs_next_event_time) <= tolerance
            )
            if event_handling_needed:
                if not self._fmi3_event_mode_used:
                    raise RuntimeError("FMI_EVENT_MODE_NOT_NEGOTIATED")
                event_occurred = True
                terminate_simulation = self._handle_fmi3_event(
                    slave,
                    current_time=reached_time,
                    active_input_clocks=scheduled_clock_references,
                    defer_input_clock_activation=bool(event_handling_needed or time_event_reached),
                ) or terminate_simulation
            elif scheduled_clock_references or time_event_reached:
                if not self._fmi3_event_mode_used:
                    raise RuntimeError("FMI_EVENT_MODE_NOT_NEGOTIATED")
                event_occurred = True
                terminate_simulation = self._handle_fmi3_event(
                    slave,
                    current_time=reached_time,
                    active_input_clocks=scheduled_clock_references,
                    defer_input_clock_activation=time_event_reached,
                ) or terminate_simulation
            self._collect_fmi3_event_outputs()
            early_return = bool(early_return or reached_time < requested_time - tolerance)
        else:
            try:
                slave.doStep(currentCommunicationPoint=current_time, communicationStepSize=step_size)
                reached_time = requested_time
            except FMICallException as exc:
                if exc.status == fmi2Pending:
                    raise RuntimeError("FMI2_ASYNCHRONOUS_STEP_UNSUPPORTED") from exc
                if exc.status != fmi2Discard:
                    raise
                reached_time = float(slave.getRealStatus(fmi2LastSuccessfulTime))
                try:
                    terminate_simulation = bool(slave.getBooleanStatus(fmi2Terminated))
                except FMICallException:
                    terminate_simulation = False
                early_return = reached_time < requested_time - 1e-12

        if not math.isfinite(reached_time):
            raise RuntimeError("FMI returned a non-finite simulation time")
        tolerance = max(1e-12, abs(requested_time) * 1e-12)
        if reached_time < current_time - tolerance or reached_time > requested_time + tolerance:
            raise RuntimeError("FMI returned a simulation time outside the requested step")
        reached_time = min(requested_time, max(current_time, reached_time))
        if reached_time <= current_time + 1e-15 and not terminate_simulation and not event_handling_needed:
            raise RuntimeError("FMI step returned without advancing simulation time")

        return reached_time, {
            "requestedTime": requested_time,
            "earlyReturn": bool(early_return),
            # Event mode is resolved in the isolated executor before this
            # response returns. Expose occurrence for telemetry, and report
            # that the remote importer still needs event handling as false.
            "eventOccurred": event_occurred,
            "eventHandlingNeeded": False,
            "terminateSimulation": bool(terminate_simulation),
        }

    def _handle_fmi3_event(
        self,
        slave: Any,
        *,
        current_time: float,
        active_input_clocks: set[int] | None = None,
        defer_input_clock_activation: bool = False,
    ) -> bool:
        return self._resolve_fmi3_event(
            slave,
            enter_event_mode=True,
            current_time=current_time,
            active_input_clocks=active_input_clocks,
            defer_input_clock_activation=defer_input_clock_activation,
        )

    def _resolve_fmi3_event(
        self,
        slave: Any,
        *,
        enter_event_mode: bool,
        current_time: float,
        active_input_clocks: set[int] | None = None,
        defer_input_clock_activation: bool = False,
        apply_values: dict[str, Any] | None = None,
        refresh_tunable_clocks: bool = False,
    ) -> bool:
        if enter_event_mode:
            slave.enterEventMode()
        if apply_values:
            self._apply_validated_values(apply_values)
        pending_clocks = set(active_input_clocks or [])
        clock_activation_deferred = bool(defer_input_clock_activation and pending_clocks)
        next_event_time_defined = False
        next_event_time: float | None = None
        for _ in range(1000):
            activated_clocks: set[int] = set()
            if pending_clocks and not clock_activation_deferred and self._fmi3_clock_runtime is not None:
                activated_clocks = self._fmi3_clock_runtime.activate_input_clocks(
                    pending_clocks,
                    current_time,
                )
                pending_clocks.clear()
            (
                needs_update,
                terminate,
                _nominals_changed,
                _states_changed,
                next_event_time_defined,
                next_event_time,
            ) = slave.updateDiscreteStates()
            if type(next_event_time_defined) is not bool and next_event_time_defined not in (0, 1):
                raise RuntimeError("FMU returned an invalid next event time flag")
            next_event_time_defined = bool(next_event_time_defined)
            if next_event_time_defined:
                if (
                    next_event_time is None
                    or isinstance(next_event_time, bool)
                    or not isinstance(next_event_time, (int, float))
                    or not math.isfinite(float(next_event_time))
                ):
                    raise RuntimeError("FMU returned an invalid next event time")
                normalized_next_event_time = float(next_event_time)
                if normalized_next_event_time < current_time - max(1e-12, abs(current_time) * 1e-12):
                    raise RuntimeError("FMU returned a next event time before the current simulation time")
                self._fmi3_cs_next_event_time = normalized_next_event_time
                self._fmi3_cs_next_event_time_defined = True
            else:
                self._fmi3_cs_next_event_time = None
                self._fmi3_cs_next_event_time_defined = False
            if terminate:
                return True
            if self._fmi3_clock_runtime is not None:
                self._fmi3_clock_runtime.capture_event_outputs(activated_clocks)
                self._fmi3_clock_runtime.refresh_after_event(
                    current_time,
                    activated_clocks,
                    refresh_tunable_clocks=refresh_tunable_clocks,
                )
                due_clocks = set(
                    self._fmi3_clock_runtime.due_input_clock_references(current_time)
                )
                refresh_tunable_clocks = False
            else:
                due_clocks = set()
            tolerance = max(1e-12, abs(current_time) * 1e-12)
            time_event_due = (
                self._fmi3_cs_next_event_time_defined
                and self._fmi3_cs_next_event_time is not None
                and self._fmi3_cs_next_event_time <= current_time + tolerance
            )
            if clock_activation_deferred:
                if needs_update or time_event_due:
                    continue
                clock_activation_deferred = False
                continue
            pending_clocks = due_clocks
            if not needs_update:
                if not pending_clocks and not time_event_due:
                    # Output Clocks and their clocked variables can only be
                    # read while the FMU is still in Event Mode.
                    event_outputs = self._read_outputs()
                    self._pending_event_outputs = {
                        **(self._pending_event_outputs or {}),
                        **event_outputs,
                    }
                    slave.enterStepMode()
                    self._collect_fmi3_event_outputs()
                    return False
        raise RuntimeError("FMI_EVENT_ITERATION_LIMIT_EXCEEDED")

    def _apply_fmi3_cosimulation_event(self, values: dict[str, Any]) -> None:
        slave = self._require_slave()
        active_clock_references = self._active_fmi3_input_clock_references(values)
        values_to_set = {
            name: value
            for name, value in values.items()
            if not self._is_fmi3_input_clock(name)
        }
        try:
            terminated = self._resolve_fmi3_event(
                slave,
                enter_event_mode=True,
                current_time=self._time,
                active_input_clocks=active_clock_references,
                apply_values=values_to_set,
                refresh_tunable_clocks=self._contains_tunable_parameter(values),
            )
            if terminated:
                self._state = "stopped"
                self._finished_reason = "fmu_termination_requested"
        except Exception:
            self._state = "error"
            raise

    def _collect_fmi3_event_outputs(self) -> None:
        if self._fmi3_clock_runtime is None:
            return
        outputs = self._fmi3_clock_runtime.take_event_outputs()
        if outputs:
            self._pending_event_outputs = {**(self._pending_event_outputs or {}), **outputs}

    def _create_fmi3_clock_runtime(self, initial_outputs: dict[str, Any] | None = None) -> Fmi3ClockRuntime:
        if self._fmi_major() != "3" or self._md is None or self._slave is None:
            raise RuntimeError("FMI 3 Clock support requires an initialized FMI 3 instance")
        return Fmi3ClockRuntime(
            self._slave,
            self._md,
            self._fmi3_input_clock_references,
            self._fmi3_clock_schedules,
            variable_size=self._variable_size,
            variable_is_array=self._is_array,
            normalize_value=self._normalise_output,
            initial_outputs=initial_outputs,
        )

    def _collect_me_event_outputs(self) -> None:
        if self._me_integrator is None:
            return
        outputs = self._me_integrator.take_event_outputs()
        if outputs:
            self._pending_event_outputs = {**(self._pending_event_outputs or {}), **outputs}

    def _active_fmi3_input_clock_references(self, values: dict[str, Any]) -> set[int]:
        if self._md is None or self._fmi_major() != "3":
            return set()
        return {
            int(variable.valueReference)
            for variable in self._md.modelVariables
            if str(getattr(variable, "name", "")) in values
            and self._variable_type(variable) == "Clock"
            and getattr(variable, "causality", None) == "input"
            and values[str(variable.name)] is True
        }

    def _is_fmi3_input_clock(self, name: str) -> bool:
        if self._md is None or self._fmi_major() != "3":
            return False
        return any(
            str(getattr(variable, "name", "")) == name
            and self._variable_type(variable) == "Clock"
            and getattr(variable, "causality", None) == "input"
            for variable in self._md.modelVariables
        )

    def _contains_tunable_parameter(self, values: dict[str, Any]) -> bool:
        if self._md is None:
            return False
        names = set(values)
        return any(
            str(getattr(variable, "name", "")) in names
            and getattr(variable, "causality", None) == "parameter"
            and getattr(variable, "variability", None) == "tunable"
            for variable in self._md.modelVariables
        )

    def _step_result(
        self,
        previous_time: float,
        reached_time: float,
        *,
        requestedTime: float | None = None,
        earlyReturn: bool = False,
        eventOccurred: bool = False,
        eventHandlingNeeded: bool = False,
        terminateSimulation: bool = False,
        finished: bool = False,
    ) -> dict[str, Any]:
        return {
            "time": reached_time,
            "state": self._state,
            "requestedTime": requestedTime if requestedTime is not None else reached_time,
            "timeAdvanced": max(0.0, reached_time - previous_time),
            "earlyReturn": earlyReturn,
            "eventOccurred": eventOccurred,
            "eventHandlingNeeded": eventHandlingNeeded,
            "terminateSimulation": terminateSimulation,
            "finished": finished or self._state == "stopped",
            "finishReason": self._finished_reason,
            **self._fmi3_event_time_metadata(),
        }

    def _describe(self) -> dict[str, Any]:
        # The isolated worker may only access the staged FMU path, not the
        # catalog root used to resolve access keys. Reuse its parsed model
        # description and avoid opening a large modelDescription.xml twice.
        return describe_model_description(
            self._md,
            fmi_ls_dae=self._fmi_ls_dae,
            fmi_ls_dae_issue=self._fmi_ls_dae_issue,
            execution_interface=self._execution_interface,
        )

    def _merge_declared_start_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """Seed writable FMI starts before applying explicit initialization values."""
        if self._md is None:
            raise RuntimeError("FMU model description is not loaded")

        defaults: dict[str, Any] = {}
        is_fmi3 = self._fmi_major() == "3"
        for variable in self._md.modelVariables:
            start = getattr(variable, "start", None)
            if start is None:
                continue

            causality = str(getattr(variable, "causality", ""))
            variability = str(getattr(variable, "variability", ""))
            variable_type = str(getattr(variable, "type", ""))
            writable_start = (
                (causality == "input" and not (is_fmi3 and variable_type == "Clock"))
                or (causality == "parameter" and variability != "constant")
                or (
                    is_fmi3
                    and causality == "structuralParameter"
                    and (
                        (variability == "fixed" and variable_type == "UInt64")
                        or (variability == "tunable" and variable_type == "Boolean")
                    )
                )
            )
            if writable_start:
                fmi_type = self._variable_type(variable)
                is_array = self._is_array(variable)

                def normalize_scalar(value: Any) -> Any:
                    if fmi_type in ("Real", "Float32", "Float64") and isinstance(value, str):
                        return float(value)
                    if fmi_type == "Binary" and isinstance(value, str):
                        return base64.b64encode(bytes.fromhex(value)).decode("ascii")
                    if fmi_type in ("Boolean", "Clock") and isinstance(value, str):
                        normalised = value.lower()
                        if normalised in {"true", "1"}:
                            return True
                        if normalised in {"false", "0"}:
                            return False
                    return value

                if is_array:
                    if isinstance(start, str):
                        raw_values = start.split()
                    else:
                        raw_values = []

                        def append_array_values(value: Any) -> None:
                            if isinstance(value, (list, tuple)):
                                for item in value:
                                    append_array_values(item)
                            else:
                                raw_values.append(value)

                        append_array_values(start)
                    normalized_start = [normalize_scalar(value) for value in raw_values]
                else:
                    normalized_start = normalize_scalar(start)

                numeric_types = (
                    "Real", "Float32", "Float64", "Integer", "Int8", "UInt8", "Int16", "UInt16",
                    "Int32", "UInt32", "Int64", "UInt64",
                )
                if fmi_type in numeric_types and not self._declared_start_within_range(variable, normalized_start):
                    # A malformed FMU can declare a start outside its own
                    # min/max. Do not turn that metadata defect into an
                    # implicit write that prevents the model from loading.
                    # Explicit client values still pass through strict
                    # range validation below.
                    continue
                defaults[str(variable.name)] = normalized_start

        # Explicit values take precedence, even when they happen to equal the
        # XML start. Some FMUs declare starts in modelDescription.xml without
        # initializing their internal value-reference storage from them.
        return {**defaults, **values}

    @staticmethod
    def _declared_start_within_range(variable: Any, value: Any) -> bool:
        """Return whether every numeric start value respects declared min/max."""
        minimum = getattr(variable, "min", None)
        maximum = getattr(variable, "max", None)
        declared_type = getattr(variable, "declaredType", None)
        if minimum is None and declared_type is not None:
            minimum = getattr(declared_type, "min", None)
        if maximum is None and declared_type is not None:
            maximum = getattr(declared_type, "max", None)
        try:
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                if isinstance(item, str):
                    item = int(item) if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", item) else float(item)
                if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
                    return False
                integer_value = isinstance(item, int)
                lower = int(minimum) if minimum is not None and integer_value else float(minimum) if minimum is not None else None
                upper = int(maximum) if maximum is not None and integer_value else float(maximum) if maximum is not None else None
                if lower is not None and item < lower:
                    return False
                if upper is not None and item > upper:
                    return False
            return True
        except (TypeError, ValueError, OverflowError):
            return False

    def _validate_values(self, params: dict[str, Any], *, phase: str) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("Variable values must be an object")
        if not self._md:
            raise RuntimeError("FMU model description is not loaded")
        var_map = {v.name: v for v in self._md.modelVariables}
        normalized: dict[str, Any] = {}
        dimension_values: dict[int, int] = {}
        if phase in {"initialization", "reconfiguration"} and self._fmi_major() == "3":
            dimension_value_references = {
                int(dimension.valueReference)
                for variable in self._md.modelVariables
                for dimension in (getattr(variable, "dimensions", None) or [])
                if getattr(dimension, "valueReference", None) is not None
            }
            for name, value in params.items():
                var = var_map.get(name)
                if var is None or getattr(var, "causality", None) != "structuralParameter":
                    continue
                self._validate_write_access(var, phase=phase)
                values = self._coerce_values(var, value)
                normalized[name] = values if self._is_array(var) else values[0]
                value_reference = int(var.valueReference)
                if value_reference in dimension_value_references:
                    if str(getattr(var, "type", "")) != "UInt64" or isinstance(values[0], bool):
                        raise ValueError(f"Structural dimension '{name}' must be UInt64")
                    extent = int(values[0])
                    if phase == "initialization" and extent <= 0:
                        raise ValueError(f"Initial structural dimension '{name}' must be positive")
                    dimension_values[value_reference] = extent
        for name, value in params.items():
            if name in normalized:
                continue
            var = var_map.get(name)
            if var is None:
                raise ValueError(f"Unknown FMU variable '{name}'")
            self._validate_write_access(var, phase=phase)
            values = self._coerce_values(var, value, dimension_values=dimension_values)
            normalized[name] = values if self._is_array(var) else values[0]
        return normalized

    def _validate_write_access(self, var: Any, *, phase: str) -> None:
        causality = str(getattr(var, "causality", ""))
        variability = str(getattr(var, "variability", ""))
        if phase == "initialization":
            allowed = (
                causality == "input"
                or (causality == "parameter" and variability != "constant")
                or (
                    self._fmi_major() == "3"
                    and causality == "structuralParameter"
                    and variability in {"fixed", "tunable"}
                )
            )
        elif phase == "runtime":
            allowed = causality == "input" or (
                causality == "parameter" and variability == "tunable"
            )
        elif phase == "reconfiguration":
            allowed = (
                self._fmi_major() == "3"
                and causality == "structuralParameter"
                and variability == "tunable"
            )
        else:
            raise ValueError(f"Unknown variable write phase '{phase}'")
        if not allowed:
            raise ValueError(
                f"FMU variable '{var.name}' cannot be modified during {phase} "
                f"(causality={causality or 'unspecified'}, variability={variability or 'unspecified'})"
            )

    def _apply_validated_values(self, params: dict[str, Any]) -> None:
        """Apply a fully validated set of values by variable name."""
        if not self._md or not self._slave:
            raise RuntimeError("FMU instance is not initialized")
        var_map = {v.name: v for v in self._md.modelVariables}
        for name, value in params.items():
            var = var_map[name]
            vtype = self._variable_type(var)
            values = value if self._is_array(var) else [value]
            setter = getattr(self._slave, f"set{vtype}", None)
            if setter is None:
                raise ValueError(f"FMU variable '{name}' has unsupported type {vtype}")
            setter([int(var.valueReference)], values)

    def _read_outputs(self, refs: list[int] | None = None) -> dict[str, Any]:
        """Read output variables. If *refs* is None, read all outputs."""
        if not self._md or not self._slave:
            return {}
        outputs: dict[str, Any] = {}
        output_variables = self._effective_output_variables()
        output_refs = {int(v.valueReference) for v in output_variables}
        if refs is not None:
            unknown_refs = set(int(ref) for ref in refs) - output_refs
            if unknown_refs:
                raise ValueError("Requested value reference is unknown or is not an output")
        pending_event_outputs = getattr(self, "_pending_event_outputs", None)
        remaining_event_outputs = (
            dict(pending_event_outputs) if pending_event_outputs is not None else None
        )
        clocked_output_cache = (
            self._dae_integrator.clocked_output_cache()
            if self._dae_integrator is not None
            else (
                self._fmi3_clock_runtime.clocked_output_cache()
                if self._fmi3_clock_runtime is not None
                else {}
            )
        )
        for var in output_variables:
            if refs is not None and var.valueReference not in refs:
                continue
            vtype = self._variable_type(var)
            if pending_event_outputs is not None and var.name in pending_event_outputs:
                outputs[var.name] = pending_event_outputs[var.name]
                if remaining_event_outputs is not None:
                    remaining_event_outputs.pop(var.name, None)
                continue
            if vtype == "Clock" and self._fmi_major() == "3":
                # FMI 3 output Clocks are observable in Event Mode, not during
                # Initialization Mode or ordinary Step/Continuous Time Mode.
                outputs[var.name] = False
                continue
            if (self._dae_integrator is not None or self._fmi3_clock_runtime is not None) and bool(
                getattr(var, "clocks", None)
            ):
                if var.name in clocked_output_cache:
                    outputs[var.name] = clocked_output_cache[var.name]
                # FMI 3 only permits reading clocked variables in Initialization
                # Mode or while one of their associated Clocks is active.
                continue
            if (self._dae_integrator is not None or self._fmi3_clock_runtime is not None) and vtype == "Clock":
                outputs[var.name] = False
                continue
            vr = [int(var.valueReference)]
            getter = getattr(self._slave, f"get{vtype}", None)
            if getter is None:
                raise RuntimeError(f"FMU output '{var.name}' uses unsupported type {vtype}")
            size = self._variable_size(var)
            try:
                values = getter(vr, nValues=size) if self._is_array(var) else getter(vr)
            except Exception as exc:
                logger.warning("Could not read FMU output %s (vr=%d)", var.name, var.valueReference)
                raise RuntimeError(f"Could not read FMU output '{var.name}'") from exc
            normalised = [self._normalise_output(vtype, value) for value in list(values)]
            outputs[var.name] = normalised if self._is_array(var) else normalised[0]
        self._pending_event_outputs = remaining_event_outputs or None
        return outputs

    def _effective_output_variables(self) -> list[Any]:
        if self._md is None:
            return []
        model_outputs = [
            variable for variable in self._md.modelVariables
            if getattr(variable, "causality", None) == "output"
        ]
        if not self._use_dae or self._fmi_ls_dae is None:
            return model_outputs
        outputs_by_reference: dict[int, Any] = {}
        for variable in model_outputs:
            reference = getattr(variable, "valueReference", None)
            if isinstance(reference, int) and not isinstance(reference, bool):
                outputs_by_reference.setdefault(reference, variable)
        ordered_outputs = []
        for reference in self._fmi_ls_dae.output_value_references:
            variable = outputs_by_reference.get(reference)
            if variable is None:
                raise RuntimeError(
                    f"FMI-LS-DAE Output value reference {reference} is missing from the model description"
                )
            ordered_outputs.append(variable)
        return ordered_outputs

    def _read_fmi_ls_dae_initial_unknowns(self) -> dict[int, Any]:
        """Snapshot ordered FMI-LS-DAE InitialUnknown variables in Initialization Mode."""
        if self._md is None or self._slave is None or self._fmi_ls_dae is None:
            return {}
        variables_by_reference: dict[int, Any] = {}
        for variable in self._md.modelVariables:
            reference = getattr(variable, "valueReference", None)
            if isinstance(reference, int) and not isinstance(reference, bool):
                variables_by_reference.setdefault(reference, variable)
        values: dict[int, Any] = {}
        for reference in self._fmi_ls_dae.initial_unknown_value_references:
            variable = variables_by_reference.get(reference)
            if variable is None:
                raise RuntimeError(
                    f"FMI-LS-DAE InitialUnknown value reference {reference} is missing from the model description"
                )
            value_type = self._variable_type(variable)
            getter = getattr(self._slave, f"get{value_type}", None)
            if getter is None:
                raise RuntimeError(
                    f"FMI-LS-DAE InitialUnknown '{variable.name}' has an unsupported type {value_type}"
                )
            value_reference = [reference]
            size = self._variable_size(variable)
            try:
                raw_values = (
                    getter(value_reference, nValues=size)
                    if self._is_array(variable)
                    else getter(value_reference)
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Could not read FMI-LS-DAE InitialUnknown '{variable.name}' during initialization"
                ) from exc
            normalized = list(raw_values)
            if len(normalized) != size:
                raise RuntimeError(
                    f"FMI-LS-DAE InitialUnknown '{variable.name}' returned an unexpected value count"
                )
            values[reference] = normalized if self._is_array(variable) else normalized[0]
        return values

    @staticmethod
    def _normalise_type(var: Any) -> str:
        variable_type = str(getattr(var, "type", ""))
        return "Integer" if variable_type == "Enumeration" else variable_type

    def _variable_type(self, var: Any) -> str:
        variable_type = str(getattr(var, "type", ""))
        if variable_type == "Enumeration" and self._fmi_major() == "3":
            return "Int64"
        return self._normalise_type(var)

    def _variable_size(self, var: Any, dimension_values: dict[int, int] | None = None) -> int:
        shape = getattr(var, "shape", None)
        if dimension_values is None and isinstance(shape, (list, tuple)) and shape:
            extents = []
            for extent in shape:
                try:
                    normalised_extent = int(extent)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"FMU array '{getattr(var, 'name', '')}' has an unresolved dimension") from exc
                if normalised_extent < 0 or normalised_extent != extent:
                    raise ValueError(f"FMU array '{getattr(var, 'name', '')}' has an invalid dimension")
                extents.append(normalised_extent)
            return int(reduce(mul, extents, 1))

        dimensions = getattr(var, "dimensions", None)
        if isinstance(dimensions, (list, tuple)) and dimensions:
            extents: list[int] = []
            for dimension in dimensions:
                value_reference = getattr(dimension, "valueReference", None)
                extent: Any = None
                if value_reference is not None:
                    if dimension_values is not None:
                        extent = dimension_values.get(int(value_reference))
                    if extent is None and self._md is not None:
                        referenced_variable = next(
                            (
                                item for item in self._md.modelVariables
                                if int(item.valueReference) == int(value_reference)
                            ),
                            None,
                        )
                        extent = getattr(referenced_variable, "start", None)
                if extent is None:
                    extent = getattr(dimension, "start", None)
                if extent is None:
                    referenced_variable = getattr(dimension, "variable", None)
                    extent = getattr(referenced_variable, "start", None)
                try:
                    normalised_extent = int(extent)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(
                        f"FMU array '{getattr(var, 'name', '')}' has an unresolved dimension"
                    ) from exc
                if normalised_extent < 0:
                    raise ValueError(f"FMU array '{getattr(var, 'name', '')}' has an invalid dimension")
                extents.append(normalised_extent)
            return int(reduce(mul, extents, 1))
        shape = getattr(var, "shape", None)
        if isinstance(shape, (list, tuple)) and shape:
            extents = []
            for extent in shape:
                try:
                    normalised_extent = int(extent)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"FMU array '{getattr(var, 'name', '')}' has an unresolved dimension") from exc
                if normalised_extent < 0 or normalised_extent != extent:
                    raise ValueError(f"FMU array '{getattr(var, 'name', '')}' has an invalid dimension")
                extents.append(normalised_extent)
            return int(reduce(mul, extents, 1))
        return 1

    def _apply_structural_array_shapes(self, dimension_values: dict[int, int]) -> None:
        if self._md is None:
            return
        for variable in self._md.modelVariables:
            dimensions = getattr(variable, "dimensions", None)
            if not isinstance(dimensions, (list, tuple)) or not dimensions:
                continue
            if not any(getattr(dimension, "valueReference", None) is not None for dimension in dimensions):
                continue
            self._variable_size(variable, dimension_values=dimension_values)
            resolved_shape: list[int] = []
            for dimension in dimensions:
                value_reference = getattr(dimension, "valueReference", None)
                if value_reference is not None and int(value_reference) in dimension_values:
                    extent = dimension_values[int(value_reference)]
                elif value_reference is not None:
                    referenced_variable = next(
                        (
                            item for item in self._md.modelVariables
                            if int(item.valueReference) == int(value_reference)
                        ),
                        None,
                    )
                    extent = int(getattr(referenced_variable, "start"))
                elif getattr(dimension, "start", None) is not None:
                    extent = int(dimension.start)
                else:
                    referenced_variable = getattr(dimension, "variable", None)
                    extent = int(getattr(referenced_variable, "start"))
                resolved_shape.append(extent)
            variable.shape = tuple(resolved_shape)
        for attribute, count_attribute in (
            ("derivatives", "numberOfContinuousStates"),
            ("eventIndicators", "numberOfEventIndicators"),
        ):
            variables = getattr(self._md, attribute, None)
            if isinstance(variables, (list, tuple)):
                setattr(
                    self._md,
                    count_attribute,
                    sum(
                        self._variable_size(getattr(unknown, "variable", None))
                        for unknown in variables
                        if getattr(unknown, "variable", None) is not None
                    ),
                )

    @staticmethod
    def _is_array(var: Any) -> bool:
        shape = getattr(var, "shape", None)
        if isinstance(shape, (list, tuple)) and shape:
            return True
        dimensions = getattr(var, "dimensions", None)
        return isinstance(dimensions, (list, tuple)) and bool(dimensions)

    def _coerce_values(
        self,
        var: Any,
        value: Any,
        *,
        dimension_values: dict[int, int] | None = None,
    ) -> list[Any]:
        vtype = self._variable_type(var)
        size = self._variable_size(var, dimension_values=dimension_values)
        is_array = self._is_array(var)
        if is_array and (not isinstance(value, (list, tuple)) or len(value) != size):
            raise ValueError(f"Array input '{var.name}' expects exactly {size} values")
        if not is_array and isinstance(value, (list, tuple, dict)):
            raise ValueError(f"Scalar input '{var.name}' requires a scalar value")
        raw_values = value if is_array else [value]
        if vtype in ("Real", "Float32", "Float64"):
            converted = []
            for item in raw_values:
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise ValueError(f"Input '{var.name}' requires a numeric value")
                try:
                    number = float(item)
                except OverflowError as exc:
                    raise ValueError(f"Input '{var.name}' is outside the numeric range") from exc
                if not math.isfinite(number):
                    raise ValueError(f"Input '{var.name}' must be finite")
                if vtype == "Float32" and abs(number) > 3.4028234663852886e38:
                    raise ValueError(f"Input '{var.name}' is outside the Float32 range")
                self._validate_numeric_range(var, number)
                converted.append(number)
            return converted
        if vtype in ("Integer", "Int8", "UInt8", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"):
            converted = []
            limits = {
                "Integer": (-(2**31), 2**31 - 1),
                "Int8": (-(2**7), 2**7 - 1),
                "UInt8": (0, 2**8 - 1),
                "Int16": (-(2**15), 2**15 - 1),
                "UInt16": (0, 2**16 - 1),
                "Int32": (-(2**31), 2**31 - 1),
                "UInt32": (0, 2**32 - 1),
                "Int64": (-(2**63), 2**63 - 1),
                "UInt64": (0, 2**64 - 1),
            }
            minimum, maximum = limits[vtype]
            for item in raw_values:
                if isinstance(item, str):
                    if len(item) > 20 or re.fullmatch(r"-?(?:0|[1-9][0-9]*)", item) is None or item == "-0":
                        raise ValueError(f"Input '{var.name}' requires an integer value")
                    item = int(item)
                if isinstance(item, bool) or not isinstance(item, int):
                    raise ValueError(f"Input '{var.name}' requires an integer value")
                self._validate_numeric_range(var, item)
                if item < minimum or item > maximum:
                    raise ValueError(f"Input '{var.name}' is outside the {vtype} range")
                converted.append(item)
            return converted
        if vtype in ("Boolean", "Clock"):
            if not all(type(item) is bool for item in raw_values):
                raise ValueError(f"Input '{var.name}' requires Boolean values")
            return list(raw_values)
        if vtype == "String":
            if not all(isinstance(item, str) for item in raw_values):
                raise ValueError(f"Input '{var.name}' requires String values")
            return list(raw_values)
        if vtype == "Binary":
            binary_values: list[str] = []
            for item in raw_values:
                if not isinstance(item, str):
                    raise ValueError("Binary inputs must be valid base64 strings")
                binary_values.append(item)
            try:
                return [base64.b64decode(item, validate=True) for item in binary_values]
            except (binascii.Error, ValueError) as exc:
                raise ValueError("Binary inputs must be valid base64 strings") from exc
        raise ValueError(f"FMU variable type {vtype} is not supported")

    @staticmethod
    def _validate_numeric_range(var: Any, value: float | int) -> None:
        minimum = getattr(var, "min", None)
        maximum = getattr(var, "max", None)
        declared_type = getattr(var, "declaredType", None)
        if minimum is None and declared_type is not None:
            minimum = getattr(declared_type, "min", None)
        if maximum is None and declared_type is not None:
            maximum = getattr(declared_type, "max", None)
        integer_value = isinstance(value, int) and not isinstance(value, bool)
        try:
            if minimum is not None:
                minimum = int(minimum) if integer_value else float(minimum)
            if maximum is not None:
                maximum = int(maximum) if integer_value else float(maximum)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Input '{var.name}' has an invalid declared numeric range") from exc
        if minimum is not None and value < minimum:
            raise ValueError(f"Input '{var.name}' is below its declared minimum")
        if maximum is not None and value > maximum:
            raise ValueError(f"Input '{var.name}' is above its declared maximum")

    @staticmethod
    def _normalise_output(vtype: str, value: Any) -> Any:
        if vtype in ("Real", "Float32", "Float64"):
            return float(value)
        if vtype in ("Int64", "UInt64"):
            return str(int(value))
        if vtype in ("Integer", "Int8", "UInt8", "Int16", "UInt16", "Int32", "UInt32"):
            return int(value)
        if vtype in ("Boolean", "Clock"):
            return bool(value)
        if vtype == "String":
            return value.decode("utf-8") if isinstance(value, bytes) else str(value)
        if vtype == "Binary":
            return base64.b64encode(bytes(value)).decode("ascii")
        return value

    def _release_fmu(self) -> None:
        dae_integrator = getattr(self, "_dae_integrator", None)
        me_integrator = getattr(self, "_me_integrator", None)
        if dae_integrator is not None:
            try:
                dae_integrator.close()
            except Exception:
                logger.debug("FMI-LS-DAE solver release failed for session %s", getattr(self, "session_id", "unknown"), exc_info=True)
            self._dae_integrator = None
        if me_integrator is not None:
            try:
                me_integrator.close()
            except Exception:
                logger.debug("FMI Model Exchange solver release failed for session %s", getattr(self, "session_id", "unknown"), exc_info=True)
            self._me_integrator = None
        slave = getattr(self, "_slave", None)
        if slave is not None:
            try:
                slave.terminate()
            except Exception:
                logger.debug("FMU terminate failed for session %s", getattr(self, "session_id", "unknown"), exc_info=True)
            try:
                slave.freeInstance()
            except Exception:
                logger.debug("FMU freeInstance failed for session %s", getattr(self, "session_id", "unknown"), exc_info=True)
        self._slave = None
        self._fmi3_input_clock_references = []
        self._fmi3_clock_schedules = []
        self._fmi3_clock_runtime = None
        self._fmi3_event_mode_used = False
        self._fmi3_cs_next_event_time_defined = False
        self._fmi3_cs_next_event_time = None
        self._pending_event_outputs = None
        if hasattr(self, "_initialised"):
            self._initialised = False

    def _cleanup_temp(self) -> None:
        extract_dir = getattr(self, "_extract_dir", None)
        if extract_dir and extract_dir.exists():
            try:
                shutil.rmtree(extract_dir, ignore_errors=True)
            except Exception:
                logger.debug(
                    "Could not remove FMU temporary directory %s",
                    extract_dir,
                    exc_info=True,
                )

    def __del__(self) -> None:
        self.terminate()
