from __future__ import annotations

import base64
import platform
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from app import engine
from app.fmi3_clock_runtime import Fmi3ClockRuntime


def make_live_session(tmp_path: Path, *, version="2.0", state="initialized", time_mode="simtime"):
    model = SimpleNamespace(
        fmiVersion=version,
        coSimulation=SimpleNamespace(modelIdentifier="Model", hasEventMode=False),
        modelExchange=None,
        defaultExperiment=None,
        modelVariables=[],
    )
    session = engine.FmuSession("edge-test", tmp_path / "model.fmu")
    session._extract_dir = tmp_path
    session._md = cast(Any, model)
    session._slave = Mock()
    session._time = 0.0
    session._start_time = 0.0
    session._stop_time = 1.0
    session._step_size = 0.25
    session._initialised = True
    session._terminated = False
    session._state = state
    session._time_mode = time_mode
    session._max_simulation_steps = 100
    return session


def _model_description(session: engine.FmuSession) -> Any:
    model = session._md
    assert model is not None
    return model


def _fmu_slave(session: engine.FmuSession) -> Any:
    slave = session._slave
    assert slave is not None
    return slave


def _set_model_variables(session: engine.FmuSession, variables: list[SimpleNamespace]) -> None:
    model = session._md
    assert model is not None
    setattr(model, "modelVariables", variables)


def _step_result(*, early=False, event=False, event_needed=False, terminate=False):
    return {
        "requestedTime": 0,
        "earlyReturn": early,
        "eventOccurred": event,
        "eventHandlingNeeded": event_needed,
        "terminateSimulation": terminate,
    }


def _state_checkpoint(mode: str, event_metadata: dict[str, Any]) -> dict[str, Any]:
    simulation: dict[str, Any] = {
        "executionMode": mode,
        "time": 0.5,
        "stepCount": 2,
        "initialParameters": {},
    }
    simulation.update(event_metadata)
    return {
        "fmuSha256": "checkpoint-model-hash",
        "fmiVersion": "3.0",
        "platform": f"{platform.system().lower()}/{platform.machine().lower()}",
        "serializedState": base64.b64encode(b"serialized-fmu-state").decode("ascii"),
        "simulation": simulation,
    }


def test_fmi3_cosimulation_checkpoint_preserves_next_event_without_clocks(tmp_path):
    session = make_live_session(tmp_path, version="3.0")
    session.fmu_sha256 = "checkpoint-model-hash"
    model = _model_description(session)
    model.coSimulation.canGetAndSetFMUstate = True
    model.coSimulation.canSerializeFMUstate = True
    session._fmi3_cs_next_event_time_defined = True
    session._fmi3_cs_next_event_time = 0.75
    session._time = 0.5
    slave = _fmu_slave(session)
    slave.getFMUState.return_value = object()
    slave.serializeFMUState.return_value = b"serialized-fmu-state"

    checkpoint = session.capture_fmu_state()
    metadata = checkpoint["simulation"]["fmi3CoSimulation"]
    assert metadata == {"nextEventTimeDefined": True, "nextEventTime": 0.75}

    slave.deserializeFMUState.return_value = object()
    restored = session.restore_fmu_state(checkpoint)
    assert restored["nextEventTimeDefined"] is True
    assert restored["nextEventTime"] == 0.75


def test_fmi3_dae_checkpoint_restores_by_rebuilding_ode_initialized_session(monkeypatch, tmp_path):
    session = make_live_session(tmp_path, version="3.0")
    session.fmu_sha256 = "checkpoint-model-hash"
    model = _model_description(session)
    model.coSimulation = None
    model.modelExchange = SimpleNamespace(
        modelIdentifier="ModelExchange",
        canGetAndSetFMUstate=True,
        canSerializeFMUstate=True,
    )
    model.defaultExperiment = SimpleNamespace(tolerance=1e-6)
    session._execution_interface = "ModelExchange"
    session._use_me = True
    session._fmi_ls_dae = cast(Any, SimpleNamespace())
    old_integrator = Mock()
    session._me_integrator = old_integrator
    slave = _fmu_slave(session)
    slave.deserializeFMUState.return_value = object()

    created: dict[str, Any] = {}

    class RestoredDaeIntegrator:
        def __init__(self, *_args, **kwargs):
            created["kwargs"] = kwargs
            self.restored_schedule = None

        def close(self):
            return None

        def restore_event_schedule(self, schedule, current_time):
            self.restored_schedule = (schedule, current_time)

    monkeypatch.setattr(engine, "Fmi3DaeIntegrator", RestoredDaeIntegrator)
    event_state = {"nextEventTimeDefined": False, "clockSchedules": [], "clockedOutputCache": {}}
    checkpoint = _state_checkpoint("fmiLsDae", {"fmiLsDae": event_state})

    restored = session.restore_fmu_state(checkpoint)

    assert restored["simTime"] == 0.5
    assert session._use_dae is True
    restored_integrator = cast(Any, session._dae_integrator)
    assert restored_integrator.restored_schedule == (event_state, 0.5)
    assert created["kwargs"]["restoring_state"] is True
    old_integrator.close.assert_called_once_with()
    slave.setTime.assert_called_once_with(0.5)


def test_fmi3_model_exchange_checkpoint_restores_by_rebuilding_dae_initialized_session(monkeypatch, tmp_path):
    session = make_live_session(tmp_path, version="3.0")
    session.fmu_sha256 = "checkpoint-model-hash"
    model = _model_description(session)
    model.coSimulation = None
    model.modelExchange = SimpleNamespace(
        modelIdentifier="ModelExchange",
        canGetAndSetFMUstate=True,
        canSerializeFMUstate=True,
    )
    model.defaultExperiment = SimpleNamespace(tolerance=1e-6)
    session._execution_interface = "ModelExchange"
    session._use_me = True
    session._use_dae = True
    old_integrator = Mock()
    session._dae_integrator = old_integrator
    slave = _fmu_slave(session)
    slave.deserializeFMUState.return_value = object()

    created: dict[str, Any] = {}

    class RestoredMeIntegrator:
        def __init__(self, *_args, **kwargs):
            created["kwargs"] = kwargs
            self.restored_schedule = None

        def close(self):
            return None

        def restore_event_schedule(self, schedule, current_time):
            self.restored_schedule = (schedule, current_time)

    monkeypatch.setattr(engine, "FmiModelExchangeIntegrator", RestoredMeIntegrator)
    event_state = {"nextEventTimeDefined": False}
    checkpoint = _state_checkpoint("modelExchange", {"modelExchange": event_state})

    restored = session.restore_fmu_state(checkpoint)

    assert restored["simTime"] == 0.5
    assert session._use_dae is False
    restored_integrator = cast(Any, session._me_integrator)
    assert restored_integrator.restored_schedule == (event_state, 0.5)
    assert created["kwargs"]["restoring_state"] is True
    old_integrator.close.assert_called_once_with()
    slave.setTime.assert_called_once_with(0.5)


def test_load_extracts_once_and_returns_describe(monkeypatch, tmp_path):
    monkeypatch.setattr(engine.config, "TEMP_DIR", tmp_path)
    extracted = Mock()
    model = SimpleNamespace(fmiVersion="2.0", coSimulation=None, modelExchange=None)
    monkeypatch.setattr(engine, "fmpy_extract", extracted)
    monkeypatch.setattr(engine, "read_model_description", Mock(return_value=model))
    monkeypatch.setattr(engine, "read_fmi_ls_dae", Mock(return_value=None))
    session = engine.FmuSession("load-test", tmp_path / "source.fmu")
    monkeypatch.setattr(session, "_describe", lambda: {"modelName": "Model"})

    assert session.load() == {"modelName": "Model"}
    assert session.load() == {"modelName": "Model"}
    extracted.assert_called_once()
    assert session._md is model


def test_initialization_validates_model_and_finite_time_contract(tmp_path, monkeypatch):
    session = engine.FmuSession("init-test", tmp_path / "model.fmu")
    with pytest.raises(RuntimeError, match="call load"):
        session.initialize()
    session._md = cast(Any, SimpleNamespace(coSimulation=None, modelExchange=None))
    with pytest.raises(RuntimeError, match="no executable Co-Simulation or Model Exchange binary"):
        session.initialize()

    session._md = cast(Any, SimpleNamespace(
        fmiVersion="2.0", coSimulation=SimpleNamespace(modelIdentifier="Model"),
        modelExchange=None, defaultExperiment=SimpleNamespace(stepSize=0.2), modelVariables=[],
    ))
    session._extract_dir = tmp_path
    monkeypatch.setattr(session, "_available_execution_interfaces", lambda: ["CoSimulation"])
    session._execution_interface = "CoSimulation"
    for kwargs, message in (
        ({"start_time": float("nan")}, "must be finite"),
        ({"stop_time": 0}, "greater than startTime"),
        ({"step_size": 0}, "stepSize must be positive"),
        ({"time_mode": "invalid"}, "timeMode must be"),
        ({"playback_speed": 101}, "playbackSpeed must be"),
        ({"early_return_allowed": 1}, "earlyReturnAllowed must be Boolean"),
        ({"parameters": []}, "Initialization values must be an object"),
    ):
        with pytest.raises(ValueError, match=message):
            session.initialize(**cast(dict[str, Any], kwargs))

    slave = Mock()
    monkeypatch.setattr(session, "_instantiate", lambda: slave)
    initialized = session.initialize(parameters={})
    assert initialized["time"] == 0.0
    assert initialized["state"] == "initialized"
    assert slave.setupExperiment.call_count == 1
    assert slave.enterInitializationMode.call_count == 1
    assert slave.exitInitializationMode.call_count == 1


def test_step_clamps_to_stop_and_enforces_state_and_external_time(tmp_path, monkeypatch):
    session = make_live_session(tmp_path)
    session._time = 0.9
    monkeypatch.setattr(session, "_do_step", lambda _slave, h: (session._time + h, _step_result()))
    result = session.step(0.4)
    assert result["time"] == 1.0
    assert result["requestedTime"] == pytest.approx(1.3)
    assert result["earlyReturn"] is True
    assert result["finishReason"] == "stop_time_reached"

    session._time_mode = "external"
    session._state = "initialized"
    with pytest.raises(ValueError, match="past stopTime"):
        session.step(0.5)
    session._state = "paused"
    with pytest.raises(RuntimeError, match="cannot step"):
        session.step()
    session._state = "running"
    with pytest.raises(ValueError, match="stepSize must be positive"):
        session.step(0)

    session._time_mode = "simtime"
    assert session._stop_time is not None
    session._time = session._stop_time
    result = session.step()
    assert result["finished"] is True
    assert result["finishReason"] == "stop_time_reached"


def test_run_until_aggregates_events_and_limits_steps(tmp_path, monkeypatch):
    session = make_live_session(tmp_path)
    advances = iter((0.2, 0.2, 0.1))

    def do_step(_slave, h):
        reached = session._time + next(advances)
        return reached, _step_result(early=h < 0.2, event=reached > 0.2, event_needed=True)

    monkeypatch.setattr(session, "_do_step", do_step)
    result = session.run_until(0.5, step_size=0.2)
    assert result["time"] == pytest.approx(0.5)
    assert result["steps"] == 3
    assert result["eventOccurred"] is True
    assert result["eventHandlingNeeded"] is True
    assert result["state"] == "initialized"

    session._max_simulation_steps = 1
    with pytest.raises(ValueError, match="MAX_SIMULATION_STEPS_EXCEEDED"):
        session.run_until(0.9, step_size=0.1)

    session._max_simulation_steps = 100
    monkeypatch.setattr(session, "_do_step", lambda _slave, _h: (session._time, _step_result()))
    with pytest.raises(RuntimeError, match="without advancing"):
        session.run_until(0.6, step_size=0.1)


def test_streaming_steps_stop_on_completion_or_fmu_termination(tmp_path, monkeypatch):
    session = make_live_session(tmp_path)
    session._step_size = 0.5
    monkeypatch.setattr(session, "_read_outputs", lambda _refs=None: {"y": 3.0})
    monkeypatch.setattr(session, "_do_step", lambda _slave, h: (session._time + h, _step_result()))
    frames = list(session.run_until_streaming(1.0, output_refs=[2]))
    assert [frame["seq"] for frame in frames] == [0, 1]
    assert frames[-1]["finished"] is True
    assert frames[-1]["finishReason"] == "stop_time_reached"

    session = make_live_session(tmp_path)
    monkeypatch.setattr(
        session, "_do_step",
        lambda _slave, _h: (session._time, _step_result(terminate=True)),
    )
    terminated = list(session.run_until_streaming(1.0))
    assert len(terminated) == 1 and terminated[0]["terminateSimulation"] is True

    session = make_live_session(tmp_path)
    with pytest.raises(ValueError, match="targetTime must be finite"):
        list(session.run_until_streaming(float("inf")))
    with pytest.raises(ValueError, match="earlier than current"):
        list(session.run_until_streaming(-1))


def test_fmi2_discard_and_fmi3_event_mode_are_handled(tmp_path, monkeypatch):
    session = make_live_session(tmp_path)
    slave = cast(Any, session._slave)
    slave.doStep.side_effect = engine.FMICallException("doStep", engine.fmi2Discard)
    slave.getRealStatus.return_value = 0.2
    slave.getBooleanStatus.side_effect = engine.FMICallException("getBooleanStatus", engine.fmi2Discard)
    reached, flags = session._do_step(slave, 0.5)
    assert reached == 0.2 and flags["earlyReturn"] is True
    assert flags["terminateSimulation"] is False

    slave.doStep.side_effect = engine.FMICallException("doStep", engine.fmi2Pending)
    with pytest.raises(RuntimeError, match="ASYNCHRONOUS_STEP_UNSUPPORTED"):
        session._do_step(slave, 0.1)

    session = make_live_session(tmp_path, version="3.0")
    session._fmi3_event_mode_used = True
    slave = cast(Any, session._slave)
    slave.doStep.return_value = (True, False, False, 0.1)
    slave.updateDiscreteStates.return_value = (False, False, False, False, 0.0, 0.0)
    reached, flags = session._do_step(slave, 0.1)
    assert reached == 0.1 and flags["eventOccurred"] is True
    slave.enterEventMode.assert_called_once_with()
    slave.enterStepMode.assert_called_once_with()


def test_fmi3_triggered_clock_is_captured_before_step_mode(tmp_path):
    session = make_live_session(tmp_path, version="3.0")
    clock = SimpleNamespace(
        name="heartbeat", type="Clock", causality="output", valueReference=2,
        shape=None, dimensions=[],
    )
    _set_model_variables(session, [clock])
    session._fmi3_event_mode_used = True
    slave = cast(Any, session._slave)
    slave.doStep.return_value = (True, False, False, 0.1)
    slave.updateDiscreteStates.return_value = (False, False, False, False, False, 0.0)
    slave.getClock.return_value = [True]
    session._fmi3_clock_runtime = Fmi3ClockRuntime(
        slave,
        session._md,
        [],
        [],
        variable_size=session._variable_size,
        variable_is_array=session._is_array,
        normalize_value=session._normalise_output,
    )

    reached, flags = session._do_step(slave, 0.1)

    assert reached == 0.1 and flags["eventOccurred"] is True
    assert session._read_outputs() == {"heartbeat": True}
    assert session._read_outputs() == {"heartbeat": False}
    calls = [call[0] for call in slave.mock_calls]
    assert calls.index("getClock") < calls.index("enterStepMode")


def test_fmi3_fixed_structural_parameter_controls_initial_array_size(tmp_path):
    session = make_live_session(tmp_path, version="3.0")
    structural_size = SimpleNamespace(
        name="n", type="UInt64", causality="structuralParameter", variability="fixed",
        valueReference=7, min="1", max=None, start="2", declaredType=None, shape=None, dimensions=[],
    )
    input_array = SimpleNamespace(
        name="u", type="Float64", causality="input", variability="continuous",
        valueReference=8, min=None, max=None, declaredType=None, shape=(2,),
        dimensions=[SimpleNamespace(start=None, valueReference=7, variable=structural_size)],
    )
    _set_model_variables(session, [structural_size, input_array])
    session._initialised = False

    assert session.validate_initial_values({"n": 3, "u": [1.0, 2.0, 3.0]}) == {
        "n": 3,
        "u": [1.0, 2.0, 3.0],
    }
    session._apply_structural_array_shapes({7: 3})
    assert input_array.shape == (3,)
    assert session._variable_size(input_array) == 3
    with pytest.raises(ValueError, match="expects exactly 3 values"):
        session.validate_initial_values({"n": 3, "u": [1.0, 2.0]})


def test_fmi3_tunable_boolean_structural_parameter_is_valid_at_initialization(tmp_path):
    session = make_live_session(tmp_path, version="3.0")
    dae_selector = SimpleNamespace(
        name="enableDAE", type="Boolean", causality="structuralParameter", variability="tunable",
        valueReference=7, min=None, max=None, start=False, declaredType=None, shape=None, dimensions=[],
    )
    _set_model_variables(session, [dae_selector])
    session._initialised = False

    assert session.validate_initial_values({"enableDAE": True}) == {"enableDAE": True}
    assert session._merge_declared_start_values({}) == {"enableDAE": False}
    with pytest.raises(ValueError, match="cannot be modified during runtime"):
        session.validate_runtime_input_values({"enableDAE": True})


def test_fmi3_dae_selector_explicit_false_keeps_ode_mode(tmp_path, monkeypatch):
    session = engine.FmuSession("dae-false-test", tmp_path / "model.fmu")
    session._extract_dir = tmp_path
    selector = SimpleNamespace(
        name="enableDAE", type="Boolean", causality="structuralParameter", variability="tunable",
        valueReference=7, min=None, max=None, start=False, declaredType=None, shape=None, dimensions=[],
    )
    model = SimpleNamespace(
        fmiVersion="3.0", guid="dae-false-guid", coSimulation=None,
        modelExchange=SimpleNamespace(modelIdentifier="ModelExchange", needsCompletedIntegratorStep=False),
        defaultExperiment=None, modelVariables=[selector],
    )
    session._md = cast(Any, model)
    session._fmi_ls_dae = cast(Any, SimpleNamespace(
        enable_dae_value_reference=7,
        output_value_references=(),
        initial_unknown_value_references=(),
    ))
    monkeypatch.setattr(session, "_available_execution_interfaces", lambda: ["ModelExchange"])
    monkeypatch.setattr(session, "_execution_interface", "ModelExchange")
    fmu = Mock()
    monkeypatch.setattr(engine, "instantiate_fmi3_model_exchange", lambda *_args: fmu)
    integrator = Mock()
    integrator.take_event_outputs.return_value = {}
    monkeypatch.setattr(engine, "FmiModelExchangeIntegrator", lambda *_args, **_kwargs: integrator)
    dae_integrator = Mock()
    dae_integrator.take_event_outputs.return_value = {}
    monkeypatch.setattr(engine, "Fmi3DaeIntegrator", lambda *_args, **_kwargs: dae_integrator)

    session.initialize(parameters={"enableDAE": False}, execution_interface="ModelExchange")

    fmu.setBoolean.assert_called_once_with([7], [False])
    assert session._use_dae is False
    assert session._me_integrator is integrator
    assert session._dae_integrator is None
    dae_integrator.assert_not_called()


def test_fmi3_dae_selector_defaults_to_the_fmi_start_value(tmp_path, monkeypatch):
    session = engine.FmuSession("dae-default-test", tmp_path / "model.fmu")
    session._extract_dir = tmp_path
    selector = SimpleNamespace(
        name="enableDAE", type="Boolean", causality="structuralParameter", variability="tunable",
        valueReference=7, min=None, max=None, start=False, declaredType=None, shape=None, dimensions=[],
    )
    model = SimpleNamespace(
        fmiVersion="3.0", guid="dae-default-guid", coSimulation=None,
        modelExchange=SimpleNamespace(modelIdentifier="ModelExchange", needsCompletedIntegratorStep=False),
        defaultExperiment=None, modelVariables=[selector],
    )
    session._md = cast(Any, model)
    session._fmi_ls_dae = cast(Any, SimpleNamespace(
        enable_dae_value_reference=7,
        output_value_references=(),
        initial_unknown_value_references=(),
    ))
    monkeypatch.setattr(session, "_available_execution_interfaces", lambda: ["ModelExchange"])
    monkeypatch.setattr(session, "_execution_interface", "ModelExchange")
    fmu = Mock()
    monkeypatch.setattr(engine, "instantiate_fmi3_model_exchange", lambda *_args: fmu)
    integrator = Mock()
    integrator.take_event_outputs.return_value = {}
    monkeypatch.setattr(engine, "FmiModelExchangeIntegrator", lambda *_args, **_kwargs: integrator)
    dae_integrator = Mock()
    dae_integrator.take_event_outputs.return_value = {}
    monkeypatch.setattr(engine, "Fmi3DaeIntegrator", lambda *_args, **_kwargs: dae_integrator)

    session.initialize(execution_interface="ModelExchange")

    fmu.setBoolean.assert_called_once_with([7], [False])
    assert session._use_dae is False
    assert session._me_integrator is integrator
    assert session._dae_integrator is None
    dae_integrator.assert_not_called()


def test_initialize_rejects_an_execution_interface_without_a_binary(tmp_path, monkeypatch):
    session = engine.FmuSession("missing-interface-test", tmp_path / "model.fmu")
    session._md = cast(Any, SimpleNamespace(
        fmiVersion="3.0", coSimulation=SimpleNamespace(modelIdentifier="CS"),
        modelExchange=SimpleNamespace(modelIdentifier="ME"), defaultExperiment=None, modelVariables=[],
    ))
    monkeypatch.setattr(session, "_available_execution_interfaces", lambda: ["CoSimulation"])

    with pytest.raises(ValueError, match="does not provide an executable ModelExchange"):
        session.initialize(execution_interface="ModelExchange")


def test_value_validation_and_output_reads_cover_fmi_types(tmp_path):
    session = make_live_session(tmp_path, version="3.0")
    integer = SimpleNamespace(
        name="count", type="Enumeration", causality="input", variability="discrete",
        valueReference=1, min=None, max=None, declaredType=None, shape=None, dimensions=[],
    )
    constant = SimpleNamespace(
        name="fixed", type="Real", causality="parameter", variability="constant",
        valueReference=2, min=None, max=None, declaredType=None, shape=None, dimensions=[],
    )
    output = SimpleNamespace(
        name="label", type="String", causality="output", valueReference=3,
        shape=None, dimensions=[],
    )
    model = cast(Any, session._md)
    model.modelVariables = [integer, constant, output]

    session._initialised = False
    assert session.validate_initial_values({"count": 3}) == {"count": 3}
    with pytest.raises(ValueError, match="cannot be modified during initialization"):
        session.validate_initial_values({"fixed": 1})
    session._initialised = True
    with pytest.raises(ValueError, match="cannot be modified during runtime"):
        session.validate_inputs({"fixed": 1})
    with pytest.raises(ValueError, match="Unknown FMU variable"):
        session.validate_inputs({"missing": 1})
    with pytest.raises(ValueError, match="outside the Int64 range"):
        session.validate_inputs({"count": 2**63})

    slave = cast(Any, session._slave)
    slave.getString.return_value = [b"ready"]
    assert session.get_outputs([3]) == {
        "time": 0.0,
        "outputs": {"label": "ready"},
        "nextEventTimeDefined": False,
        "terminateSimulation": False,
        "finished": False,
    }
    with pytest.raises(ValueError, match="unknown or is not an output"):
        session.get_outputs([1])
    slave.getString.side_effect = RuntimeError("private FMU details")
    with pytest.raises(RuntimeError, match="Could not read FMU output 'label'"):
        session.get_outputs()


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_runtime_tunable_parameters_are_allowed_at_cosimulation_points(tmp_path, version):
    session = make_live_session(tmp_path, version=version)
    tunable = SimpleNamespace(
        name="tunableGain", type="Real", causality="parameter", variability="tunable",
        valueReference=1, min=None, max=None, declaredType=None, shape=None, dimensions=[],
    )
    fixed = SimpleNamespace(
        name="fixedGain", type="Real", causality="parameter", variability="fixed",
        valueReference=2, min=None, max=None, declaredType=None, shape=None, dimensions=[],
    )
    cast(Any, session._md).modelVariables = [tunable, fixed]
    session._initialised = True

    assert session.validate_inputs({"tunableGain": 2.5}) == {"tunableGain": 2.5}
    with pytest.raises(ValueError, match="cannot be modified during runtime"):
        session.validate_inputs({"fixedGain": 1.5})


def test_attach_and_lifecycle_guards_cover_expiry_reset_and_termination(tmp_path, monkeypatch):
    session = make_live_session(tmp_path)
    session.mark_detached(10)
    session._attach_deadline = 10
    assert session.can_attach(now=1)
    assert not session.can_attach(now=11)
    session.expires_at = "not-a-time"
    assert not session.can_attach(now=1)
    session.expires_at = None
    session.gateway_context = {"claims": {"reservationEndsAt": "invalid"}}
    assert not session.can_attach(now=1)

    session.gateway_context = {}
    session._state = "running"
    assert session.pause()["state"] == "paused"
    assert session.resume()["state"] == "running"
    session._time_mode = "external"
    with pytest.raises(RuntimeError, match="EXTERNAL_STEP_MODE"):
        session.resume()

    session._time_mode = "simtime"
    session.subscription = engine.OutputSubscription()
    session.subscription.rate_dropped = 3
    session._pending_samples = [{"y": 1}]
    session._pending_queue_drops = 2
    session._initialised = False
    session._state = "loaded"
    monkeypatch.setattr(session, "initialize", lambda **_kwargs: {"state": "initialized"})
    assert session.reset(defer_initialization=True)["state"] == "loaded"
    assert session.subscription.rate_dropped == 0
    assert session._pending_samples == []
    assert session.reset()["state"] == "initialized"

    session.terminate()
    session.terminate()
    assert session.state == "terminated"
