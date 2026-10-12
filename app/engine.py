"""FMU execution engine – wraps FMPy for Co-Simulation execution."""

from __future__ import annotations

import base64
import binascii
import copy
import logging
import math
from functools import reduce
from operator import mul
import shutil
import tempfile
import threading
import time as _time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Optional

from fmpy import (
    extract as fmpy_extract,
    instantiate_fmu as fmpy_instantiate_fmu,
    read_model_description,
)
from fmpy.fmi1 import FMICallException
from fmpy.fmi2 import fmi2Discard, fmi2LastSuccessfulTime, fmi2Pending, fmi2Terminated

from . import config

logger = logging.getLogger(__name__)


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
        hz_interval = 0.0
        if self.max_hz is not None and self.max_hz > 0:
            hz_interval = 1.0 / self.max_hz
        return max(period_interval, hz_interval)


class FmuSession:
    """Manage one loaded FMI 2/FMI 3 Co-Simulation session."""

    def __init__(
        self,
        session_id: str,
        fmu_path: Path,
        *,
        access_key: str | None = None,
        expires_at: float | int | str | None = None,
        gateway_context: dict[str, Any] | None = None,
    ):
        self.session_id = session_id
        self.fmu_path = fmu_path
        self.access_key = access_key or fmu_path.name
        self.expires_at = expires_at
        self.gateway_context = dict(gateway_context or {})
        self._extract_dir: Path | None = None
        self._slave: Any | None = None
        self._md = None
        self._time: float = 0.0
        self._step_size: float = 0.001
        self._start_time: float = 0.0
        self._stop_time: float = 1.0
        self._initial_parameters: dict[str, Any] = {}
        self._time_mode = "simtime"
        self._playback_speed = 1.0
        self._early_return_allowed = False
        self._fmi3_event_mode_used = False
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
        return self._describe()

    def initialize(
        self,
        start_time: float = 0.0,
        stop_time: float = 1.0,
        step_size: float | None = None,
        parameters: dict[str, Any] | None = None,
        time_mode: str = "simtime",
        playback_speed: float = 1.0,
        early_return_allowed: bool = False,
    ) -> dict[str, Any]:
        if self._initialised:
            raise RuntimeError("Session already initialised")
        if self._md is None:
            raise RuntimeError("FMU not loaded – call load() first")
        if self._md.coSimulation is None:
            raise RuntimeError("FMU does not support Co-Simulation")

        self._step_size = step_size if step_size is not None else (
            float(self._md.defaultExperiment.stepSize)
            if self._md.defaultExperiment and self._md.defaultExperiment.stepSize
            else 0.001
        )
        self._start_time = float(start_time)
        self._stop_time = float(stop_time)
        if not all(math.isfinite(value) for value in (self._start_time, self._stop_time, self._step_size)):
            raise ValueError("startTime, stopTime and stepSize must be finite")
        if self._stop_time <= self._start_time:
            raise ValueError("stopTime must be greater than startTime")
        if not math.isfinite(self._stop_time - self._start_time):
            raise ValueError("Simulation duration must be finite")
        if self._step_size <= 0:
            raise ValueError("stepSize must be positive")
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
        validated_parameters = self._validate_values(original_parameters, phase="initialization")
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
        if self._fmi_major() == "3":
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

        if validated_parameters:
            self._apply_validated_values(validated_parameters)

        slave.exitInitializationMode()
        self._initialised = True
        self._terminated = False
        self._state = "initialized"

        return {
            "sessionId": self.session_id,
            "time": self._time,
            "state": "initialized",
            "timeMode": self._time_mode,
            "playbackSpeed": self._playback_speed,
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
        remaining = max(0.0, self._stop_time - self._time)
        tolerance = max(1e-12, abs(self._stop_time) * 1e-12)
        if self._time_mode == "external" and requested_h > remaining + tolerance:
            raise ValueError("stepSize would advance past stopTime")
        h = min(requested_h, remaining)
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
        elif self._time >= self._stop_time - 1e-12:
            self._state = "stopped"
            self._finished_reason = "stop_time_reached"
        else:
            self._state = "running"
        return self._step_result(previous_time, reached_time, **result)

    def run_until(self, target_time: float, step_size: float | None = None) -> dict[str, Any]:
        slave = self._require_slave()
        if not math.isfinite(target_time):
            raise ValueError("targetTime must be finite")
        if target_time < self._time:
            raise ValueError("targetTime must not be earlier than current simulation time")
        if self._state in {"paused", "stopped", "error"}:
            raise RuntimeError(f"Session cannot run from state {self._state}")
        requested_target = target_time
        target_time = min(target_time, self._stop_time)
        h = self._step_size if step_size is None else step_size
        if not math.isfinite(h) or h <= 0:
            raise ValueError("stepSize must be positive")
        expected_steps = max(0.0, target_time - self._time) / h
        if not math.isfinite(expected_steps) or expected_steps > config.MAX_SIMULATION_STEPS:
            raise ValueError("MAX_SIMULATION_STEPS_EXCEEDED")
        self._ensure_step_budget(math.ceil(expected_steps))
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
            self._state = "stopped" if self._time >= self._stop_time - 1e-12 else "initialized"
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
        }

    def run_until_streaming(
        self,
        target_time: float,
        step_size: float | None = None,
        output_refs: list[int] | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Step until *target_time*, yielding output snapshots at each step."""
        slave = self._require_slave()
        if not math.isfinite(target_time):
            raise ValueError("targetTime must be finite")
        if target_time < self._time:
            raise ValueError("targetTime must not be earlier than current simulation time")
        if self._state in {"paused", "stopped", "error"}:
            raise RuntimeError(f"Session cannot run from state {self._state}")
        target_time = min(target_time, self._stop_time)
        h = self._step_size if step_size is None else step_size
        if not math.isfinite(h) or h <= 0:
            raise ValueError("stepSize must be positive")
        expected_steps = max(0.0, target_time - self._time) / h
        if not math.isfinite(expected_steps) or expected_steps > config.MAX_SIMULATION_STEPS:
            raise ValueError("MAX_SIMULATION_STEPS_EXCEEDED")
        self._ensure_step_budget(math.ceil(expected_steps))
        self._state = "running"
        seq = 0
        consecutive_no_progress_events = 0
        while self._time < target_time - 1e-12:
            remaining = target_time - self._time
            actual_h = min(h, remaining)
            self._ensure_step_budget(1)
            self._step_count += 1
            previous_time = self._time
            self._time, step_result = self._do_step(slave, actual_h)
            if self._time <= previous_time + 1e-15 and not step_result["terminateSimulation"]:
                consecutive_no_progress_events += 1
                if not step_result["eventOccurred"] or consecutive_no_progress_events >= 100:
                    raise RuntimeError("FMI step returned repeatedly without advancing simulation time")
            else:
                consecutive_no_progress_events = 0
            finished = step_result["terminateSimulation"] or self._time >= self._stop_time - 1e-12
            if step_result["terminateSimulation"]:
                self._state = "stopped"
                self._finished_reason = "fmu_termination_requested"
            elif finished:
                self._state = "stopped"
                self._finished_reason = "stop_time_reached"
            outputs = self._read_outputs(output_refs)
            yield {
                "type": "sim.step",
                "seq": seq,
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
        if self._time >= self._stop_time - 1e-12:
            self._state = "stopped"
            self._finished_reason = "stop_time_reached"
        else:
            self._state = "initialized"

    def _ensure_step_budget(self, additional_steps: int) -> None:
        if self._step_count + additional_steps > config.MAX_SIMULATION_STEPS:
            raise ValueError("MAX_SIMULATION_STEPS_EXCEEDED")

    def set_inputs(self, values: dict[str, Any]) -> None:
        self._ensure_live()
        if self._state in {"stopped", "error"}:
            raise RuntimeError(f"Session cannot accept inputs from state {self._state}")
        validated = self._validate_values(values, phase="runtime")
        self._apply_validated_values(validated)

    def validate_initial_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate initialization parameters before a scenario is charged."""
        if self._terminated:
            raise RuntimeError("Session already terminated")
        if self._initialised:
            raise RuntimeError("Session already initialised")
        return self._validate_values(values, phase="initialization")

    def validate_inputs(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize runtime inputs without changing FMU state."""
        self._ensure_live()
        if self._state in {"stopped", "error"}:
            raise RuntimeError(f"Session cannot accept inputs from state {self._state}")
        return self._validate_values(values, phase="runtime")

    def get_outputs(self, refs: list[int] | None = None) -> dict[str, Any]:
        self._ensure_live()
        return {"time": self._time, "outputs": self._read_outputs(refs)}

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
        if not self.subscription:
            return None

        # Resolve variable name filter → valueReference filter
        var_refs: list[int] | None = None
        if self.subscription.variables is not None and self._md:
            name_set = set(self.subscription.variables)
            var_refs = [
                v.valueReference
                for v in self._md.modelVariables
                if v.name in name_set and v.causality == "output"
            ]

        sample = self._read_outputs(var_refs)
        self._pending_samples.append(sample)

        # Enforce max batch size
        if len(self._pending_samples) > self.subscription.max_batch_size:
            excess = len(self._pending_samples) - self.subscription.max_batch_size
            self._pending_samples = self._pending_samples[excess:]
            self.subscription.rate_dropped += excess

        now = _time.monotonic()
        min_interval = self.subscription.min_interval_seconds()
        if (now - self.subscription.last_emit_monotonic) < min_interval:
            self.subscription.rate_dropped += 1
            return None

        self.subscription.last_emit_monotonic = now
        values = self._pending_samples[-1]
        batch_size = len(self._pending_samples)
        self._pending_samples.clear()
        dropped = self.subscription.rate_dropped + self._pending_queue_drops
        self.subscription.rate_dropped = 0
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
                if float(self.expires_at) <= now:
                    return False
            except (TypeError, ValueError):
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

    def _instantiate(self) -> Any:
        """Instantiate through FMPy's FMI 2/FMI 3 selector."""
        if self._extract_dir is None or self._md is None:
            raise RuntimeError("FMU is not loaded")
        # Negotiate FMI 3 early return and event mode only when the FMU exposes
        # Event Mode. FMPy reports the actual reached time on every FMI 3 step.
        co_simulation = self._md.coSimulation
        self._fmi3_event_mode_used = bool(
            self._fmi_major() == "3" and getattr(co_simulation, "hasEventMode", False)
        )
        return fmpy_instantiate_fmu(
            str(self._extract_dir),
            self._md,
            fmi_type="CoSimulation",
            early_return_allowed=self._fmi_major() == "3" and self._early_return_allowed,
            event_mode_used=self._fmi3_event_mode_used,
        )

    def _do_step(self, slave: Any, step_size: float) -> tuple[float, dict[str, Any]]:
        current_time = self._time
        requested_time = current_time + step_size
        if not math.isfinite(requested_time) or requested_time <= current_time:
            raise ValueError("FMI step does not produce a finite later time")
        event_handling_needed = False
        terminate_simulation = False
        early_return = False

        if self._fmi_major() == "3":
            event_handling_needed, terminate_simulation, early_return, reached_time = slave.doStep(
                currentCommunicationPoint=current_time,
                communicationStepSize=step_size,
            )
            reached_time = float(reached_time)
            if event_handling_needed:
                if not self._fmi3_event_mode_used:
                    raise RuntimeError("FMI_EVENT_MODE_NOT_NEGOTIATED")
                terminate_simulation = self._handle_fmi3_event(slave) or terminate_simulation
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
            "eventOccurred": bool(event_handling_needed),
            "eventHandlingNeeded": False,
            "terminateSimulation": bool(terminate_simulation),
        }

    @staticmethod
    def _handle_fmi3_event(slave: Any) -> bool:
        slave.enterEventMode()
        for _ in range(1000):
            needs_update, terminate, *_ = slave.updateDiscreteStates()
            if terminate:
                return True
            if not needs_update:
                slave.enterStepMode()
                return False
        raise RuntimeError("FMI_EVENT_ITERATION_LIMIT_EXCEEDED")

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
        }

    def _describe(self) -> dict[str, Any]:
        from . import fmu_storage
        # Re-use the same normalised describe logic
        return fmu_storage.describe(self.access_key)

    def _validate_values(self, params: dict[str, Any], *, phase: str) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("Variable values must be an object")
        if not self._md:
            raise RuntimeError("FMU model description is not loaded")
        var_map = {v.name: v for v in self._md.modelVariables}
        normalized: dict[str, Any] = {}
        for name, value in params.items():
            var = var_map.get(name)
            if var is None:
                raise ValueError(f"Unknown FMU variable '{name}'")
            self._validate_write_access(var, phase=phase)
            values = self._coerce_values(var, value)
            normalized[name] = values if self._is_array(var) else values[0]
        return normalized

    @staticmethod
    def _validate_write_access(var: Any, *, phase: str) -> None:
        causality = str(getattr(var, "causality", ""))
        variability = str(getattr(var, "variability", ""))
        if phase == "initialization":
            allowed = (
                causality == "input"
                or (causality == "parameter" and variability != "constant")
            )
        elif phase == "runtime":
            allowed = causality == "input"
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
        output_variables = [v for v in self._md.modelVariables if v.causality == "output"]
        output_refs = {int(v.valueReference) for v in output_variables}
        if refs is not None:
            unknown_refs = set(int(ref) for ref in refs) - output_refs
            if unknown_refs:
                raise ValueError("Requested value reference is unknown or is not an output")
        for var in self._md.modelVariables:
            if var.causality != "output":
                continue
            if refs is not None and var.valueReference not in refs:
                continue
            vr = [int(var.valueReference)]
            vtype = self._variable_type(var)
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
        return outputs

    @staticmethod
    def _normalise_type(var: Any) -> str:
        variable_type = str(getattr(var, "type", ""))
        return "Integer" if variable_type == "Enumeration" else variable_type

    def _variable_type(self, var: Any) -> str:
        variable_type = str(getattr(var, "type", ""))
        if variable_type == "Enumeration" and self._fmi_major() == "3":
            return "Int64"
        return self._normalise_type(var)

    @staticmethod
    def _variable_size(var: Any) -> int:
        shape = getattr(var, "shape", None)
        if isinstance(shape, (list, tuple)) and shape:
            extents = []
            for extent in shape:
                try:
                    normalised_extent = int(extent)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"FMU array '{getattr(var, 'name', '')}' has an unresolved dimension") from exc
                if normalised_extent <= 0 or normalised_extent != extent:
                    raise ValueError(f"FMU array '{getattr(var, 'name', '')}' has an invalid dimension")
                extents.append(normalised_extent)
            return int(reduce(mul, extents, 1))
        dimensions = getattr(var, "dimensions", None)
        if isinstance(dimensions, (list, tuple)) and dimensions:
            extents: list[int] = []
            for dimension in dimensions:
                start = getattr(dimension, "start", None)
                if start is None:
                    raise ValueError(f"FMU array '{getattr(var, 'name', '')}' has an unresolved dimension")
                if int(start) <= 0:
                    raise ValueError(f"FMU array '{getattr(var, 'name', '')}' has an invalid dimension")
                extents.append(int(start))
            return int(reduce(mul, extents, 1))
        return 1

    @staticmethod
    def _is_array(var: Any) -> bool:
        shape = getattr(var, "shape", None)
        if isinstance(shape, (list, tuple)) and shape:
            return True
        dimensions = getattr(var, "dimensions", None)
        return isinstance(dimensions, (list, tuple)) and bool(dimensions)

    def _coerce_values(self, var: Any, value: Any) -> list[Any]:
        vtype = self._variable_type(var)
        size = self._variable_size(var)
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
            if not all(isinstance(item, str) for item in raw_values):
                raise ValueError("Binary inputs must be valid base64 strings")
            try:
                return [base64.b64decode(item, validate=True) for item in raw_values]
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
        if self._slave is not None:
            try:
                self._slave.terminate()
            except Exception:
                logger.debug("FMU terminate failed for session %s", self.session_id, exc_info=True)
            try:
                self._slave.freeInstance()
            except Exception:
                logger.debug("FMU freeInstance failed for session %s", self.session_id, exc_info=True)
        self._slave = None
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


# ── session registry ─────────────────────────────────────────────

_sessions: dict[str, Any] = {}
_sessions_lock = threading.RLock()


class CapacityExceededError(RuntimeError):
    """Raised when the Station execution authority has no free slot."""


def create_session(
    fmu_path: Path,
    *,
    access_key: str | None = None,
    expires_at: float | int | str | None = None,
    gateway_context: dict[str, Any] | None = None,
    session_factory: Any = None,
) -> Any:
    cleanup_expired_sessions()
    with _sessions_lock:
        if len(_sessions) >= config.MAX_CONCURRENT_SESSIONS:
            raise CapacityExceededError(
                f"Max concurrent sessions ({config.MAX_CONCURRENT_SESSIONS}) reached"
            )
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        factory = session_factory or FmuSession
        session = factory(
            session_id,
            fmu_path,
            access_key=access_key,
            expires_at=expires_at,
            gateway_context=gateway_context,
        )
        _sessions[session_id] = session
        return session


def get_session(session_id: str) -> Any | None:
    with _sessions_lock:
        return _sessions.get(session_id)


def get_attachable_session(session_id: str) -> Any | None:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None or not session.can_attach():
        if session is not None and not session.can_attach():
            remove_session(session_id)
        return None
    return session


def detach_session(
    session_id: str,
    grace_seconds: float,
    *,
    attachment_owner: object | None = None,
) -> None:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is not None:
        session.mark_detached(grace_seconds, attachment_owner)


def cleanup_expired_sessions(now: float | None = None) -> None:
    now = _time.time() if now is None else now
    with _sessions_lock:
        expired = [
            session_id
            for session_id, session in list(_sessions.items())
            if session._terminated or (not session._attached and not session.can_attach(now))
        ]
    for session_id in expired:
        remove_session(session_id)


def remove_session(session_id: str) -> None:
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session:
        session.terminate()


def active_session_count() -> int:
    cleanup_expired_sessions()
    with _sessions_lock:
        return len(_sessions)


def terminate_all() -> None:
    with _sessions_lock:
        session_ids = list(_sessions)
    for sid in session_ids:
        remove_session(sid)
