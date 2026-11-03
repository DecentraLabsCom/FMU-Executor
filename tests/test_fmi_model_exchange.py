from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from app import fmi_model_exchange
from app.fmi_model_exchange import FmiModelExchangeIntegrator


def test_event_iteration_accumulates_state_and_nominal_change_flags():
    integrator = FmiModelExchangeIntegrator.__new__(FmiModelExchangeIntegrator)
    integrator._fmi_major = "3"
    integrator._fmu = Mock()
    integrator._fmu.updateDiscreteStates.side_effect = [
        (True, False, True, False, False, 0.0),
        (False, False, False, True, False, 0.0),
    ]

    assert integrator._update_discrete_states(0.0) is False
    assert integrator._event_nominals_changed is True
    assert integrator._event_states_changed is True
    assert integrator._next_event_time_defined is False
    assert integrator._next_event_time is None


def test_model_exchange_integrator_accepts_fmi_default_experiment_tolerance_string(monkeypatch):
    solver_arguments = {}

    def create_solver(**kwargs):
        solver_arguments.update(kwargs)
        return object()

    monkeypatch.setattr(fmi_model_exchange, "CVodeSolver", create_solver)
    fmu = Mock()
    fmu.updateDiscreteStates.return_value = (False, False, False, False, False, 0.0)
    model_description = SimpleNamespace(
        fmiVersion="3.0",
        numberOfContinuousStates=1,
        numberOfEventIndicators=0,
    )

    FmiModelExchangeIntegrator(
        fmu,
        model_description,
        start_time=0.0,
        max_step=0.01,
        relative_tolerance="1e-6",
    )

    assert solver_arguments["relativeTolerance"] == 1e-6
