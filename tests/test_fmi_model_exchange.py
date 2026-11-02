from __future__ import annotations

from unittest.mock import Mock

from app.fmi_model_exchange import FmiModelExchangeIntegrator


def test_event_iteration_accumulates_state_and_nominal_change_flags():
    integrator = FmiModelExchangeIntegrator.__new__(FmiModelExchangeIntegrator)
    integrator._fmi_major = "3"
    integrator._fmu = Mock()
    integrator._fmu.updateDiscreteStates.side_effect = [
        (True, False, True, False, False, 0.0),
        (False, False, False, True, False, 0.0),
    ]

    assert integrator._update_discrete_states() is False
    assert integrator._event_nominals_changed is True
    assert integrator._event_states_changed is True
    assert integrator._next_event_time_defined is False
    assert integrator._next_event_time is None
