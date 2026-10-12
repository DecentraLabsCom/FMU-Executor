from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from app import engine


def make_live_session(tmp_path: Path, *, version="2.0", state="initialized", time_mode="simtime"):
    model = SimpleNamespace(
        fmiVersion=version,
        coSimulation=SimpleNamespace(modelIdentifier="Model", hasEventMode=False),
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


def _step_result(*, early=False, event=False, event_needed=False, terminate=False):
    return {
        "requestedTime": 0,
        "earlyReturn": early,
        "eventOccurred": event,
        "eventHandlingNeeded": event_needed,
        "terminateSimulation": terminate,
    }


def test_load_extracts_once_and_returns_describe(monkeypatch, tmp_path):
    monkeypatch.setattr(engine.config, "TEMP_DIR", tmp_path)
    extracted = Mock()
    model = SimpleNamespace(fmiVersion="2.0")
    monkeypatch.setattr(engine, "fmpy_extract", extracted)
    monkeypatch.setattr(engine, "read_model_description", Mock(return_value=model))
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
    session._md = cast(Any, SimpleNamespace(coSimulation=None))
    with pytest.raises(RuntimeError, match="Co-Simulation"):
        session.initialize()

    session._md = cast(Any, SimpleNamespace(
        fmiVersion="2.0", coSimulation=SimpleNamespace(modelIdentifier="Model"),
        defaultExperiment=SimpleNamespace(stepSize=0.2), modelVariables=[],
    ))
    session._extract_dir = tmp_path
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
    assert session.get_outputs([3]) == {"time": 0.0, "outputs": {"label": "ready"}}
    with pytest.raises(ValueError, match="unknown or is not an output"):
        session.get_outputs([1])
    slave.getString.side_effect = RuntimeError("private FMU details")
    with pytest.raises(RuntimeError, match="Could not read FMU output 'label'"):
        session.get_outputs()


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
