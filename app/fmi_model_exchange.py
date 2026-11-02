"""Incremental FMI 2/3 Model Exchange integration using FMPy's CVode solver."""

from __future__ import annotations

import math
from typing import Any, Callable

from fmpy.sundials import CVodeSolver


class _NoScheduledInput:
    """CVodeSolver callback adapter; session inputs are applied by FmuSession."""

    def apply(self, _time: float, **_kwargs: Any) -> None:
        return None


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
        relative_tolerance: float | None,
    ):
        self._fmu = fmu
        self._model_description = model_description
        self._fmi_major = str(model_description.fmiVersion).split(".", 1)[0]
        if self._fmi_major not in {"2", "3"}:
            raise ValueError("Model Exchange execution requires FMI 2 or FMI 3")
        if not math.isfinite(max_step) or max_step <= 0:
            raise ValueError("Model Exchange maximum integration step must be positive")
        if relative_tolerance is None or not math.isfinite(relative_tolerance) or relative_tolerance <= 0:
            relative_tolerance = 1e-5

        next_event_time_defined, next_event_time = self._initialize_event_mode()
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
        if self._next_event_time_defined:
            if self._next_event_time is None or not math.isfinite(self._next_event_time):
                raise RuntimeError("FMU declared an invalid next event time")
            if self._next_event_time < current_time - tolerance:
                raise RuntimeError("FMU declared a next event time before the current simulation time")
            if self._next_event_time <= current_time + tolerance:
                terminated = self._process_event()
                if not terminated:
                    self._solver.reset(current_time)
                return {
                    "time": current_time,
                    "eventOccurred": True,
                    "terminateSimulation": terminated,
                    "earlyReturn": True,
                }

        integration_target = requested_time
        is_time_event = False
        if self._next_event_time_defined and self._next_event_time is not None:
            if self._next_event_time <= requested_time + tolerance:
                integration_target = min(requested_time, self._next_event_time)
                is_time_event = abs(integration_target - self._next_event_time) <= tolerance

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
        if getattr(self._model_description.modelExchange, "needsCompletedIntegratorStep", False):
            step_event, terminate_simulation = self._fmu.completedIntegratorStep()
            step_event = bool(step_event)
            terminate_simulation = bool(terminate_simulation)

        event_occurred = bool(state_event or is_time_event or step_event)
        if event_occurred and not terminate_simulation:
            terminate_simulation = self._process_event()
            if not terminate_simulation:
                self._solver.reset(reached_time)

        return {
            "time": reached_time,
            "eventOccurred": event_occurred,
            "terminateSimulation": terminate_simulation,
            "earlyReturn": reached_time < requested_time - max(1e-12, abs(requested_time) * 1e-12),
        }

    def apply_input_change(self, current_time: float, apply_values: Callable[[], None]) -> bool:
        """Apply changed inputs in Event Mode and reinitialize the integrator."""
        if self._solver is None:
            raise RuntimeError("Model Exchange integrator is closed")
        self._fmu.enterEventMode()
        apply_values()
        terminated = self._update_discrete_states()
        if not terminated:
            self._fmu.enterContinuousTimeMode()
            self._solver.reset(current_time)
        return terminated

    def snapshot_event_schedule(self) -> dict[str, Any]:
        result: dict[str, Any] = {"nextEventTimeDefined": self._next_event_time_defined}
        if self._next_event_time_defined and self._next_event_time is not None:
            result["nextEventTime"] = self._next_event_time
        return result

    @staticmethod
    def validate_event_schedule(
        metadata: dict[str, Any],
        simulation_time: float,
    ) -> tuple[bool, float | None]:
        if not math.isfinite(simulation_time):
            raise ValueError("FMU state checkpoint has invalid Model Exchange time")
        if type(metadata.get("nextEventTimeDefined")) is not bool:
            raise ValueError("FMU state checkpoint has invalid Model Exchange event metadata")
        next_defined = metadata["nextEventTimeDefined"]
        next_time = metadata.get("nextEventTime")
        if not next_defined:
            return False, None
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
        return True, normalized_time

    def restore_event_schedule(self, metadata: dict[str, Any], simulation_time: float) -> None:
        self._next_event_time_defined, self._next_event_time = self.validate_event_schedule(
            metadata,
            simulation_time,
        )

    def reset_after_restore(self, time: float) -> None:
        if self._solver is None:
            raise RuntimeError("Model Exchange integrator is closed")
        self._fmu.setTime(time)
        self._solver.reset(time)

    def close(self) -> None:
        self._solver = None

    def _initialize_event_mode(self) -> tuple[bool, float | None]:
        if self._fmi_major == "2":
            needs_update, terminate, _nominals_changed, _states_changed, next_defined, next_time = self._fmu.newDiscreteStates()
        else:
            needs_update, terminate, _nominals_changed, _states_changed, next_defined, next_time = self._fmu.updateDiscreteStates()
        if terminate:
            raise RuntimeError("FMU requested termination during Model Exchange initialization")
        for _ in range(self.MAX_EVENT_ITERATIONS - 1):
            if not needs_update:
                break
            if self._fmi_major == "2":
                needs_update, terminate, _nominals_changed, _states_changed, next_defined, next_time = self._fmu.newDiscreteStates()
            else:
                needs_update, terminate, _nominals_changed, _states_changed, next_defined, next_time = self._fmu.updateDiscreteStates()
            if terminate:
                raise RuntimeError("FMU requested termination during Model Exchange initialization")
        else:
            raise RuntimeError("FMI_EVENT_ITERATION_LIMIT_EXCEEDED")
        next_defined, next_time = self._validated_next_event(next_defined, next_time)
        self._fmu.enterContinuousTimeMode()
        return next_defined, next_time

    def _process_event(self) -> bool:
        self._fmu.enterEventMode()
        terminated = self._update_discrete_states()
        if not terminated:
            self._fmu.enterContinuousTimeMode()
        return terminated

    def _update_discrete_states(self) -> bool:
        needs_update = True
        terminate = False
        next_defined = False
        next_time: float | None = None
        for _ in range(self.MAX_EVENT_ITERATIONS):
            if self._fmi_major == "2":
                needs_update, terminate, _nominals_changed, _states_changed, next_defined, next_time = self._fmu.newDiscreteStates()
            else:
                needs_update, terminate, _nominals_changed, _states_changed, next_defined, next_time = self._fmu.updateDiscreteStates()
            if terminate or not needs_update:
                break
        else:
            raise RuntimeError("FMI_EVENT_ITERATION_LIMIT_EXCEEDED")
        self._next_event_time_defined, self._next_event_time = self._validated_next_event(next_defined, next_time)
        return bool(terminate)

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
