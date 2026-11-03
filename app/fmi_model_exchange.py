"""Incremental FMI 2/3 Model Exchange integration using FMPy's CVode solver."""

from __future__ import annotations

import math
from typing import Any, Callable

from fmpy.sundials import CVodeSolver

from .fmi3_clock_runtime import Fmi3ClockRuntime


class _NoScheduledInput:
    """CVodeSolver callback adapter; session inputs are applied by FmuSession."""

    def apply(self, _time: float, **_kwargs: Any) -> None:
        return None


def _coerce_relative_tolerance(value: float | str | None) -> float:
    if isinstance(value, bool) or value is None:
        return 1e-5
    try:
        tolerance = float(value)
    except (TypeError, ValueError, OverflowError):
        return 1e-5
    return tolerance if math.isfinite(tolerance) and tolerance > 0 else 1e-5


class FmiModelExchangeIntegrator:
    """Integrate an FMI 2/3 Model Exchange FMU between session advances.

    FMPy's CVode solver performs adaptive ODE integration and locates state
    events. This adapter handles the FMI event iteration and scheduled time
    events between those integration intervals.
    """

    MAX_EVENT_ITERATIONS = 1000

    def __init__(
        self,
        fmu: Any,
        model_description: Any,
        *,
        start_time: float,
        max_step: float,
        relative_tolerance: float | str | None,
        clock_runtime: Fmi3ClockRuntime | None = None,
        initial_input_clock_references: set[int] | None = None,
        restoring_state: bool = False,
    ):
        self._fmu = fmu
        self._model_description = model_description
        self._clock_runtime = clock_runtime
        self._initial_input_clock_references = set(initial_input_clock_references or [])
        self._fmi_major = str(model_description.fmiVersion).split(".", 1)[0]
        if self._fmi_major not in {"2", "3"}:
            raise ValueError("Model Exchange execution requires FMI 2 or FMI 3")
        if not math.isfinite(max_step) or max_step <= 0:
            raise ValueError("Model Exchange maximum integration step must be positive")
        relative_tolerance = _coerce_relative_tolerance(relative_tolerance)

        self._event_states_changed = False
        self._event_nominals_changed = False
        self._next_event_time_defined = False
        self._next_event_time: float | None = None
        if not restoring_state:
            next_event_time_defined, next_event_time = self._initialize_event_mode(start_time)
            self._next_event_time_defined = next_event_time_defined
            self._next_event_time = next_event_time

        self._solver: Any | None = CVodeSolver(
            nx=int(getattr(model_description, "numberOfContinuousStates", 0) or 0),
            nz=int(getattr(model_description, "numberOfEventIndicators", 0) or 0),
            get_x=fmu.getContinuousStates,
            set_x=fmu.setContinuousStates,
            get_dx=fmu.getContinuousStateDerivatives if self._fmi_major == "3" else fmu.getDerivatives,
            get_z=fmu.getEventIndicators,
            get_nominals=fmu.getNominalsOfContinuousStates,
            set_time=fmu.setTime,
            input=_NoScheduledInput(),
            startTime=start_time,
            maxStep=max_step,
            relativeTolerance=relative_tolerance,
            maxNumSteps=10_000,
        )

    @property
    def next_event_time_defined(self) -> bool:
        return self._next_event_time_defined

    @property
    def next_event_time(self) -> float | None:
        return self._next_event_time if self._next_event_time_defined else None

    def advance(self, current_time: float, requested_time: float) -> dict[str, Any]:
        """Integrate to the request or the first FMI event, whichever comes first."""
        if self._solver is None:
            raise RuntimeError("Model Exchange integrator is closed")
        if (
            not math.isfinite(current_time)
            or not math.isfinite(requested_time)
            or requested_time <= current_time
        ):
            raise ValueError("Model Exchange target time must be finite and later than the current time")

        tolerance = max(1e-12, abs(current_time) * 1e-12)
        due_clocks = (
            self._clock_runtime.due_input_clock_references(current_time)
            if self._clock_runtime is not None
            else []
        )
        if self._next_event_time_defined:
            if self._next_event_time is None or not math.isfinite(self._next_event_time):
                raise RuntimeError("FMU declared an invalid next event time")
            if self._next_event_time < current_time - tolerance:
                raise RuntimeError("FMU declared a next event time before the current simulation time")
        time_event_due = (
            self._next_event_time_defined
            and self._next_event_time is not None
            and self._next_event_time <= current_time + tolerance
        )
        if due_clocks or time_event_due:
            terminated = self._process_event(
                current_time,
                due_clocks,
                defer_clock_activation=time_event_due,
            )
            if not terminated:
                self._solver.reset(current_time)
            return {
                "time": current_time,
                "eventOccurred": True,
                "terminateSimulation": terminated,
                "earlyReturn": True,
            }

        integration_target = requested_time
        scheduled_event_time = self._next_scheduled_event_time()
        if scheduled_event_time is not None and scheduled_event_time <= requested_time + tolerance:
            integration_target = min(requested_time, scheduled_event_time)

        state_event, _roots_found, reached_time = self._solver.step(current_time, integration_target)
        reached_time = float(reached_time)
        if (
            not math.isfinite(reached_time)
            or reached_time < current_time - tolerance
            or reached_time > integration_target + tolerance
        ):
            raise RuntimeError("Model Exchange solver returned a time outside the requested interval")
        reached_time = min(integration_target, max(current_time, reached_time))
        self._fmu.setTime(reached_time)

        step_event = False
        terminate_simulation = False
        if self._completed_integrator_step_required():
            step_event, terminate_simulation = self._fmu.completedIntegratorStep()
            step_event = bool(step_event)
            terminate_simulation = bool(terminate_simulation)

        due_clocks = (
            self._clock_runtime.due_input_clock_references(reached_time)
            if self._clock_runtime is not None
            else []
        )
        model_time_event_reached = (
            self._next_event_time_defined
            and self._next_event_time is not None
            and abs(reached_time - self._next_event_time) <= tolerance
        )
        clock_event_reached = bool(due_clocks)
        event_occurred = bool(state_event or model_time_event_reached or clock_event_reached or step_event)
        if event_occurred and not terminate_simulation:
            terminate_simulation = self._process_event(
                reached_time,
                due_clocks,
                defer_clock_activation=bool(state_event or model_time_event_reached or step_event),
            )
            if not terminate_simulation:
                # CVodeSolver.reset rereads continuous states and nominals from the
                # FMU. Do this for every event so both FMI event flags are applied,
                # including changes raised by an earlier super-dense iteration.
                self._solver.reset(reached_time)

        return {
            "time": reached_time,
            "eventOccurred": event_occurred,
            "terminateSimulation": terminate_simulation,
            "earlyReturn": reached_time < requested_time - max(1e-12, abs(requested_time) * 1e-12),
        }

    def apply_input_change(
        self,
        current_time: float,
        apply_values: Callable[[], None],
        *,
        active_clock_references: set[int] | None = None,
        refresh_tunable_clocks: bool = False,
    ) -> bool:
        """Apply changed inputs in Event Mode and reinitialize the integrator."""
        if self._solver is None:
            raise RuntimeError("Model Exchange integrator is closed")
        self._fmu.enterEventMode()
        apply_values()
        terminated = self._resolve_event_mode(
            current_time,
            set(active_clock_references or []),
            refresh_tunable_clocks=refresh_tunable_clocks,
        )
        if not terminated:
            # reset() refreshes both state values and nominal-based tolerances.
            self._solver.reset(current_time)
        return terminated

    def take_event_outputs(self) -> dict[str, Any]:
        return self._clock_runtime.take_event_outputs() if self._clock_runtime is not None else {}

    def snapshot_event_schedule(self) -> dict[str, Any]:
        result: dict[str, Any] = {"nextEventTimeDefined": self._next_event_time_defined}
        if self._next_event_time_defined and self._next_event_time is not None:
            result["nextEventTime"] = self._next_event_time
        if self._clock_runtime is not None:
            result["clockRuntime"] = self._clock_runtime.snapshot()
        return result

    def validate_event_schedule(
        self,
        metadata: dict[str, Any],
        simulation_time: float,
    ) -> tuple[bool, float | None]:
        if not math.isfinite(simulation_time):
            raise ValueError("FMU state checkpoint has invalid Model Exchange time")
        if type(metadata.get("nextEventTimeDefined")) is not bool:
            raise ValueError("FMU state checkpoint has invalid Model Exchange event metadata")
        next_defined = metadata["nextEventTimeDefined"]
        next_time = metadata.get("nextEventTime")
        normalized_time: float | None = None
        if next_defined:
            if isinstance(next_time, bool) or not isinstance(next_time, (int, float)):
                raise ValueError("FMU state checkpoint has invalid Model Exchange event time")
            normalized_time = float(next_time)
            if not math.isfinite(normalized_time):
                raise ValueError("FMU state checkpoint has invalid Model Exchange event time")
            if normalized_time < simulation_time - max(
                1e-12,
                abs(simulation_time) * 1e-12,
            ):
                raise ValueError("FMU state checkpoint has a Model Exchange event before its simulation time")
        clock_runtime_state = metadata.get("clockRuntime")
        if self._clock_runtime is not None:
            if not isinstance(clock_runtime_state, dict):
                raise ValueError("FMU state checkpoint is missing Model Exchange Clock metadata")
            self._clock_runtime.validate_snapshot(clock_runtime_state, simulation_time)
        elif clock_runtime_state is not None:
            raise ValueError("FMU state checkpoint contains unexpected Model Exchange Clock metadata")
        return next_defined, normalized_time

    def restore_event_schedule(self, metadata: dict[str, Any], simulation_time: float) -> None:
        self._next_event_time_defined, self._next_event_time = self.validate_event_schedule(
            metadata,
            simulation_time,
        )
        if self._clock_runtime is not None:
            self._clock_runtime.restore_snapshot(metadata["clockRuntime"], simulation_time)

    def reset_after_restore(self, time: float) -> None:
        if self._solver is None:
            raise RuntimeError("Model Exchange integrator is closed")
        self._fmu.setTime(time)
        self._solver.reset(time)

    def close(self) -> None:
        self._solver = None

    def _initialize_event_mode(self, start_time: float) -> tuple[bool, float | None]:
        if self._fmi_major == "2":
            needs_update, terminate, nominals_changed, states_changed, next_defined, next_time = self._fmu.newDiscreteStates()
        else:
            needs_update, terminate, nominals_changed, states_changed, next_defined, next_time = self._fmu.updateDiscreteStates()
        any_nominals_changed = bool(nominals_changed)
        any_states_changed = bool(states_changed)
        if terminate:
            raise RuntimeError("FMU requested termination during Model Exchange initialization")
        for _ in range(self.MAX_EVENT_ITERATIONS - 1):
            if not needs_update:
                break
            if self._fmi_major == "2":
                needs_update, terminate, nominals_changed, states_changed, next_defined, next_time = self._fmu.newDiscreteStates()
            else:
                needs_update, terminate, nominals_changed, states_changed, next_defined, next_time = self._fmu.updateDiscreteStates()
            any_nominals_changed = any_nominals_changed or bool(nominals_changed)
            any_states_changed = any_states_changed or bool(states_changed)
            if terminate:
                raise RuntimeError("FMU requested termination during Model Exchange initialization")
        else:
            raise RuntimeError("FMI_EVENT_ITERATION_LIMIT_EXCEEDED")
        self._event_states_changed = any_states_changed
        self._event_nominals_changed = any_nominals_changed
        # The CVodeSolver is constructed after this initial event iteration; its
        # constructor reads the current states and nominal values from the FMU.
        next_defined, next_time = self._validated_next_event(next_defined, next_time)
        if next_defined and next_time is not None and next_time < start_time - max(1e-12, abs(start_time) * 1e-12):
            raise RuntimeError("FMU declared a next event time before the simulation start time")
        self._next_event_time_defined = next_defined
        self._next_event_time = next_time
        if self._clock_runtime is not None:
            self._clock_runtime.capture_event_outputs(set())
            self._clock_runtime.refresh_after_event(start_time, set())
            due_after_update = set(self._clock_runtime.due_input_clock_references(start_time))
        else:
            due_after_update = set()
        due_after_update.update(self._initial_input_clock_references)
        tolerance = max(1e-12, abs(start_time) * 1e-12)
        time_event_due = next_defined and next_time is not None and next_time <= start_time + tolerance
        if due_after_update or time_event_due:
            terminated = self._resolve_event_mode(
                start_time,
                due_after_update,
                defer_clock_activation=bool(due_after_update),
            )
            if terminated:
                raise RuntimeError("FMU requested termination during Model Exchange initialization")
        else:
            self._fmu.enterContinuousTimeMode()
        return self._next_event_time_defined, self._next_event_time

    def _process_event(
        self,
        current_time: float,
        active_clock_references: list[int] | None = None,
        *,
        defer_clock_activation: bool = False,
    ) -> bool:
        self._fmu.enterEventMode()
        return self._resolve_event_mode(
            current_time,
            set(active_clock_references or []),
            defer_clock_activation=defer_clock_activation,
        )

    def _resolve_event_mode(
        self,
        current_time: float,
        active_clock_references: set[int],
        *,
        defer_clock_activation: bool = False,
        refresh_tunable_clocks: bool = False,
    ) -> bool:
        pending_clocks = set(active_clock_references)
        clock_activation_deferred = bool(defer_clock_activation and pending_clocks)
        for _ in range(self.MAX_EVENT_ITERATIONS):
            activated_clocks: set[int] = set()
            if pending_clocks and not clock_activation_deferred and self._clock_runtime is not None:
                activated_clocks = self._clock_runtime.activate_input_clocks(pending_clocks, current_time)
                pending_clocks.clear()
            terminated = self._update_discrete_states(current_time)
            if self._clock_runtime is not None:
                self._clock_runtime.capture_event_outputs(activated_clocks)
                self._clock_runtime.refresh_after_event(
                    current_time,
                    activated_clocks,
                    refresh_tunable_clocks=refresh_tunable_clocks,
                )
                due_clocks = set(self._clock_runtime.due_input_clock_references(current_time))
            else:
                due_clocks = set()
            if terminated:
                return True
            tolerance = max(1e-12, abs(current_time) * 1e-12)
            time_event_due = (
                self._next_event_time_defined
                and self._next_event_time is not None
                and self._next_event_time <= current_time + tolerance
            )
            if clock_activation_deferred:
                if time_event_due:
                    continue
                clock_activation_deferred = False
                continue
            pending_clocks = due_clocks
            if not pending_clocks and not time_event_due:
                break
        else:
            raise RuntimeError("FMI_EVENT_ITERATION_LIMIT_EXCEEDED")
        self._fmu.enterContinuousTimeMode()
        return False

    def _update_discrete_states(self, current_time: float) -> bool:
        needs_update = True
        terminate = False
        nominals_changed = False
        states_changed = False
        next_defined = False
        next_time: float | None = None
        for _ in range(self.MAX_EVENT_ITERATIONS):
            if self._fmi_major == "2":
                needs_update, terminate, iteration_nominals_changed, iteration_states_changed, next_defined, next_time = self._fmu.newDiscreteStates()
            else:
                needs_update, terminate, iteration_nominals_changed, iteration_states_changed, next_defined, next_time = self._fmu.updateDiscreteStates()
            nominals_changed = nominals_changed or bool(iteration_nominals_changed)
            states_changed = states_changed or bool(iteration_states_changed)
            if terminate or not needs_update:
                break
        else:
            raise RuntimeError("FMI_EVENT_ITERATION_LIMIT_EXCEEDED")
        next_defined, next_time = self._validated_next_event(next_defined, next_time)
        if next_defined and next_time is not None and next_time < current_time - max(
            1e-12,
            abs(current_time) * 1e-12,
        ):
            raise RuntimeError("FMU declared a next event time before the current simulation time")
        self._next_event_time_defined, self._next_event_time = next_defined, next_time
        self._event_states_changed = states_changed
        self._event_nominals_changed = nominals_changed
        return bool(terminate)

    def _next_scheduled_event_time(self) -> float | None:
        event_times: list[float] = []
        if self._next_event_time_defined and self._next_event_time is not None:
            event_times.append(self._next_event_time)
        if self._clock_runtime is not None and self._clock_runtime.next_event_time is not None:
            event_times.append(self._clock_runtime.next_event_time)
        return min(event_times) if event_times else None

    def _completed_integrator_step_required(self) -> bool:
        model_exchange = getattr(self._model_description, "modelExchange", None)
        if self._fmi_major == "2":
            return not bool(getattr(model_exchange, "completedIntegratorStepNotNeeded", False))
        return bool(getattr(model_exchange, "needsCompletedIntegratorStep", False))

    @staticmethod
    def _validated_next_event(is_defined: Any, value: Any) -> tuple[bool, float | None]:
        if type(is_defined) is not bool and is_defined not in (0, 1):
            raise RuntimeError("FMU returned an invalid next event time flag")
        is_defined = bool(is_defined)
        if not is_defined:
            return False, None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise RuntimeError("FMU returned a non-finite next event time")
        return True, float(value)
