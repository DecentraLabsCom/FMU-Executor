from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app import engine, main, process_runner, session_registry


class Session:
    session_id = "dispatch-session"
    access_key = "demo.fmu"
    expires_at = None
    gateway_context = {
        "accessKey": "demo.fmu",
        "labId": "lab-1",
        "reservationKey": "reservation-1",
        "claims": {},
    }
    state = "initialized"
    _time = 0.0
    _stop_time = 1.0
    _start_time = 0.0
    _step_size = 0.1
    _time_mode = "simtime"
    _playback_speed = 1.0
    _terminated = False
    _initialised = True
    _md = SimpleNamespace(modelVariables=[SimpleNamespace(name="x", valueReference=4, causality="output", type="Real")])
    subscription = None
    _pending_samples = []
    seq = 0

    def load(self): self.loaded = True; return {"modelName": "demo"}
    def validate_initial_values(self, values):
        return values
    def validate_inputs(self, values):
        return values
    def initialize(self, *, start_time, stop_time, step_size=None, parameters=None, time_mode="simtime", playback_speed=1.0, early_return_allowed=False):
        self.state = "initialized"
        self._time = start_time
        self._stop_time = stop_time
        self._step_size = step_size or 0.1
        self._time_mode = time_mode
        self._playback_speed = playback_speed
        self.initialized_with = (start_time, stop_time, step_size, parameters)
    def resume(self): self.state = "running"
    def pause(self): self.state = "paused"
    def reset(self, defer_initialization=False): self.state = "initialized"; self._time = 0.0
    def step(self, delta=None): self._time += delta or self._step_size
    def run_until(self, target, step_size=None): self._time = target
    def set_inputs(self, values): self.inputs = values
    def get_outputs(self, refs=None): self.output_refs = refs; return {"outputs": {"x": 9}}
    def terminate(self): self._terminated = True; self.state = "terminated"


@pytest.fixture
def dispatch(monkeypatch):
    monkeypatch.setattr(main.auth, "validate_gateway_context", lambda *_args: None)
    monkeypatch.setattr(main.auth, "extract_access_key_from_context", lambda context: context.get("accessKey") if context else None)
    monkeypatch.setattr(main.fmu_storage, "describe", lambda _key: {"modelName": "demo"})
    monkeypatch.setattr(main, "_reserve_simulation_scenario", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main._simulation_store, "reserve_input_update", Mock(return_value={}))
    session = Session()

    def call(message, gateway_context=None, selected_session=session, **kwargs):
        return asyncio.run(main._handle_ws_message(message.get("type", ""), message, gateway_context, selected_session, **kwargs))

    return session, call


def test_live_session_commands_cover_state_model_and_data_operations(dispatch, monkeypatch):
    session, call = dispatch
    response = call({"type": "model.describe"})
    assert response["type"] == "model.description" and response["modelName"] == "demo"

    stopped = call({"type": "sim.initialize", "options": {"startTime": 2, "stopTime": 4, "stepSize": 0.25}, "parameters": {"x": 3}}, stop_runner=lambda: asyncio.sleep(0))
    assert stopped["state"] == "initialized" and session.initialized_with == (2.0, 4.0, 0.25, {"x": 3})
    started = call({"type": "sim.start"}, start_runner=lambda: None)
    assert started["state"] == "running"
    assert call({"type": "sim.pause"}, stop_runner=lambda: asyncio.sleep(0))["state"] == "paused"
    assert call({"type": "sim.resume"}, start_runner=lambda: None)["state"] == "running"
    assert call({"type": "sim.reset"}, stop_runner=lambda: asyncio.sleep(0))["state"] == "initialized"

    stepped = call({"type": "sim.step", "deltaT": 0.2})
    assert stepped["type"] == "sim.outputs" and stepped["values"] == {"x": 9}
    advanced = call({"type": "sim.runUntil", "targetTime": 3, "stepSize": 0.5})
    assert advanced["simTime"] == 3
    assert call({"type": "sim.setInputs", "values": {"x": 5}})["type"] == "sim.inputs.updated"
    assert session.inputs == {"x": 5}
    selected = call({"type": "sim.getOutputs", "variables": ["x"]})
    assert selected["values"] == {"x": 9} and session.output_refs == [4]
    subscribed = call({"type": "sim.subscribeOutputs", "variables": ["x"], "periodMs": 1, "maxBatchSize": 1, "maxHz": 5})
    assert subscribed["periodMs"] == 1 and subscribed["maxBatchSize"] == 1 and subscribed["maxHz"] == 5
    assert call({"type": "sim.unsubscribeOutputs"})["type"] == "sim.unsubscribed"
    assert session.subscription is None and session._pending_samples == []
    assert call({"type": "sim.getState"})["state"] == "initialized"


def test_advance_batches_use_the_same_authorized_session_and_validate_before_execution(dispatch):
    session, call = dispatch
    session.advance = Mock(return_value={"samples": [{"time": 0.1, "outputs": {"x": 1}}], "simTime": 0.1, "finished": False})
    result = call({"type": "sim.advance", "targetTime": 2, "maxSamples": 256, "stepSize": 0.1})
    assert result["type"] == "sim.samples" and result["sessionId"] == session.session_id
    session.advance.assert_called_once_with(
        2.0,
        max_samples=256,
        step_size=0.1,
        input_schedule=[],
        output_sample_every_steps=1,
        output_refs=None,
        max_compute_ms=None,
    )
    with pytest.raises(HTTPException):
        call({"type": "sim.advance", "targetTime": 2, "maxSamples": 257})
    assert session.advance.call_count == 1
    assert call({"type": "ping"})["type"] == "session.pong"
    assert call({"type": "session.terminate"})["reason"] == "client_terminated"
    assert session._terminated


def test_advance_reserves_scheduled_input_quota_and_settles_only_applied_events(dispatch, monkeypatch):
    session, call = dispatch
    schedule = [
        {"time": 0.1, "values": {"input": 1}},
        {"time": 0.2, "values": {"input": 2}},
    ]
    session.validate_input_schedule = Mock(side_effect=lambda events: events)
    session.advance = Mock(return_value={
        "samples": [{"time": 0.2, "outputs": {"x": 1}}],
        "simTime": 0.2,
        "finished": False,
        "inputEventsApplied": 1,
    })
    reserve = Mock(return_value={"reservationId": "scheduled-input-reservation"})
    settle = Mock()
    monkeypatch.setattr(main, "_reserve_input_updates", reserve)
    monkeypatch.setattr(main, "_settle_input_updates", settle)

    result = call({
        "type": "sim.advance",
        "targetTime": 0.2,
        "maxSamples": 2,
        "stepSize": 0.1,
        "inputSchedule": schedule,
    })

    assert result["type"] == "sim.samples"
    reserve.assert_called_once_with(session.gateway_context, "demo.fmu", schedule)
    session.advance.assert_called_once_with(
        0.2,
        max_samples=2,
        step_size=0.1,
        input_schedule=schedule,
        output_sample_every_steps=1,
        output_refs=None,
        max_compute_ms=None,
    )
    settle.assert_called_once_with(
        session.gateway_context,
        "demo.fmu",
        "scheduled-input-reservation",
        1,
    )


def test_advance_releases_scheduled_quota_when_output_rate_limit_prevents_execution(dispatch, monkeypatch):
    session, call = dispatch
    schedule = [{"time": 0.1, "values": {"input": 1}}]
    session.validate_input_schedule = Mock(side_effect=lambda events: events)
    session.advance = Mock(side_effect=process_runner.OutputReadRateLimitError(100))
    reserve = Mock(return_value={"reservationId": "scheduled-input-reservation"})
    settle = Mock()
    monkeypatch.setattr(main, "_reserve_input_updates", reserve)
    monkeypatch.setattr(main, "_settle_input_updates", settle)

    with pytest.raises(HTTPException) as error:
        call({
            "type": "sim.advance",
            "targetTime": 0.2,
            "maxSamples": 2,
            "stepSize": 0.1,
            "inputSchedule": schedule,
        })

    assert error.value.status_code == 429
    session.advance.assert_called_once()
    settle.assert_called_once_with(
        session.gateway_context,
        "demo.fmu",
        "scheduled-input-reservation",
        0,
    )


def test_live_session_commands_reject_invalid_shapes_and_states(dispatch):
    session, call = dispatch
    with pytest.raises(HTTPException) as exc:
        call({"type": "sim.setInputs", "values": []})
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        call({"type": "sim.getOutputs", "variables": "x"})
    with pytest.raises(HTTPException):
        call({"type": "sim.subscribeOutputs", "variables": "x"})
    with pytest.raises(HTTPException) as exc:
        call({"type": "sim.subscribeOutputs", "variables": ["x"], "periodMs": 0})
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        call({"type": "sim.runUntil"})
    assert exc.value.status_code == 400
    session.state = "created"
    with pytest.raises(HTTPException) as exc:
        call({"type": "sim.start"})
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        call({"type": "not-a-command"})
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        call({"type": "session.ping"}, selected_session=None)


def test_session_creation_and_capacity_failure(dispatch, monkeypatch, tmp_path):
    _session, call = dispatch
    new_session = Session()
    monkeypatch.setattr(main.fmu_storage, "fmu_exists", lambda _key: True)
    monkeypatch.setattr(main.fmu_storage, "get_fmu_path", lambda _key: tmp_path / "demo.fmu")
    monkeypatch.setattr(main.config, "execution_mode", lambda: "process")
    monkeypatch.setattr(main.fmu_storage, "inspect_fmu", lambda *_args, **_kwargs: {
        "archiveValid": True, "modelDescriptionValid": True, "stationCompatible": True, "issues": [],
    })
    monkeypatch.setattr(main.fmu_storage, "snapshot_fmu", lambda path: (path, "a" * 64))
    monkeypatch.setattr(main.fmu_storage, "inspect_fmu_path", lambda *_args, **_kwargs: {
        "archiveValid": True, "modelDescriptionValid": True, "stationCompatible": True, "issues": [],
    })
    monkeypatch.setattr(session_registry, "create_session", lambda *_args, **_kwargs: new_session)
    created = call({"type": "session.create"}, {"accessKey": "demo.fmu", "claims": {"exp": time.time() + 3600}}, selected_session=None)
    assert created["type"] == "session.created" and created["_session"] is new_session
    assert new_session.loaded
    assert created["capabilities"]["inputUpdateQuota"]["maxUpdatesPerWindow"] == main.config.MAX_INPUT_UPDATES_PER_WINDOW
    assert created["capabilities"]["inputUpdateQuota"]["windowMs"] == main.config.INPUT_UPDATE_WINDOW_SECONDS * 1000

    monkeypatch.setattr(session_registry, "create_session", lambda *_args, **_kwargs: (_ for _ in ()).throw(session_registry.CapacityExceededError("full")))
    with pytest.raises(HTTPException) as exc:
        call({"type": "session.create"}, {"accessKey": "demo.fmu", "claims": {}}, selected_session=None)
    assert exc.value.status_code == 429


@pytest.mark.parametrize(
    ("mode", "expected_factory"),
    [("process", process_runner.RealtimeSession)],
)
def test_session_creation_uses_configured_worker_boundary(dispatch, monkeypatch, tmp_path, mode, expected_factory):
    _session, call = dispatch
    new_session = Session()
    created = {}
    monkeypatch.setattr(main.config, "execution_mode", lambda: mode)
    monkeypatch.setattr(main.fmu_storage, "fmu_exists", lambda _key: True)
    monkeypatch.setattr(main.fmu_storage, "get_fmu_path", lambda _key: tmp_path / "demo.fmu")
    monkeypatch.setattr(main.fmu_storage, "inspect_fmu", lambda *_args, **_kwargs: {
        "archiveValid": True, "modelDescriptionValid": True, "stationCompatible": True, "issues": [],
    })
    monkeypatch.setattr(main.fmu_storage, "snapshot_fmu", lambda path: (path, "a" * 64))
    monkeypatch.setattr(main.fmu_storage, "inspect_fmu_path", lambda *_args, **_kwargs: {
        "archiveValid": True, "modelDescriptionValid": True, "stationCompatible": True, "issues": [],
    })

    def create_session(*args, **kwargs):
        created["args"] = args
        created["kwargs"] = kwargs
        return new_session

    monkeypatch.setattr(session_registry, "create_session", create_session)

    response = call(
        {"type": "session.create"},
        {"accessKey": "demo.fmu", "claims": {}},
        selected_session=None,
    )

    assert response["_session"] is new_session
    assert created["kwargs"]["session_factory"] is expected_factory


def test_session_creation_rejects_in_process_execution(dispatch, monkeypatch):
    _session, call = dispatch
    monkeypatch.setattr(main.config, "execution_mode", lambda: "in-process")
    with pytest.raises(HTTPException) as error:
        call(
            {"type": "session.create"},
            {"accessKey": "demo.fmu", "claims": {}},
            selected_session=None,
        )
    assert error.value.status_code == 409
    assert error.value.detail == "FMU_PROCESS_ISOLATION_REQUIRED"


def test_session_creation_rejects_missing_context_access_key_and_fmu(dispatch, monkeypatch):
    _session, call = dispatch
    with pytest.raises(HTTPException):
        call({"type": "session.create"}, selected_session=None)
    with pytest.raises(HTTPException):
        call({"type": "session.create"}, {"claims": {}}, selected_session=None)
    monkeypatch.setattr(main.fmu_storage, "fmu_exists", lambda _key: False)
    with pytest.raises(HTTPException) as exc:
        call({"type": "session.create"}, {"accessKey": "missing.fmu", "claims": {}}, selected_session=None)
    assert exc.value.status_code == 404


def test_attach_requires_a_live_context_and_matching_session(dispatch, monkeypatch):
    session, call = dispatch
    with pytest.raises(HTTPException):
        call({"type": "session.attach"}, selected_session=None)
    with pytest.raises(HTTPException):
        call({"type": "session.attach", "sessionId": "route-session"}, {"accessKey": "demo.fmu"}, selected_session=None)
    monkeypatch.setattr(session_registry, "get_attachable_session", lambda _sid: session)
    attached = call(
        {"type": "session.attach", "sessionId": "route-session"},
        {"accessKey": "demo.fmu", "labId": "lab-1", "reservationKey": "reservation-1"},
        selected_session=None,
    )
    assert attached["type"] == "session.attached"
    assert attached["capabilities"]["inputUpdateQuota"]["maxScheduledEventsPerAdvance"] == 1024
    with pytest.raises(HTTPException) as exc:
        call({"type": "session.attach", "sessionId": "other"}, {"accessKey": "demo.fmu"})
    assert exc.value.status_code == 403


def test_attach_rejects_missing_expired_and_mismatched_access_key(dispatch, monkeypatch):
    session, call = dispatch
    monkeypatch.setattr(session_registry, "get_attachable_session", lambda _sid: None)
    with pytest.raises(HTTPException):
        call({"type": "session.attach", "sessionId": "route-session"}, {"accessKey": "demo.fmu"}, selected_session=None)
    with pytest.raises(HTTPException) as exc:
        call({"type": "session.attach"}, {"accessKey": "demo.fmu"})
    assert exc.value.status_code == 401
    monkeypatch.setattr(session_registry, "get_attachable_session", lambda _sid: session)
    with pytest.raises(HTTPException) as exc:
        call({"type": "session.attach"}, {"accessKey": "another.fmu"})
    assert exc.value.status_code == 403
