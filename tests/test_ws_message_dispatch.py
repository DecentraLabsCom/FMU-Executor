from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import engine, main, process_runner


class Session:
    session_id = "dispatch-session"
    access_key = "demo.fmu"
    expires_at = None
    gateway_context = {"accessKey": "demo.fmu", "claims": {}}
    state = "initialized"
    _time = 0.0
    _stop_time = 1.0
    _step_size = 0.1
    _terminated = False
    _initialised = True
    _md = SimpleNamespace(modelVariables=[SimpleNamespace(name="x", valueReference=4)])
    subscription = None
    _pending_samples = []
    seq = 0

    def load(self): self.loaded = True
    def initialize(self, *, start_time, stop_time, step_size=None, parameters=None):
        self.state = "initialized"
        self._time = start_time
        self._stop_time = stop_time
        self._step_size = step_size or 0.1
        self.initialized_with = (start_time, stop_time, step_size, parameters)
    def resume(self): self.state = "running"
    def pause(self): self.state = "paused"
    def reset(self): self.state = "initialized"; self._time = 0.0
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
    subscribed = call({"type": "sim.subscribeOutputs", "variables": ["x"], "periodMs": 0, "maxBatchSize": 0, "maxHz": 5})
    assert subscribed["periodMs"] == 1 and subscribed["maxBatchSize"] == 1 and subscribed["maxHz"] == 5
    assert call({"type": "sim.unsubscribeOutputs"})["type"] == "sim.unsubscribed"
    assert session.subscription is None and session._pending_samples == []
    assert call({"type": "sim.getState"})["state"] == "initialized"
    assert call({"type": "ping"})["type"] == "session.pong"
    assert call({"type": "session.terminate"})["reason"] == "client_terminated"
    assert session._terminated


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
    monkeypatch.setattr(engine, "create_session", lambda *_args, **_kwargs: new_session)
    created = call({"type": "session.create"}, {"accessKey": "demo.fmu", "claims": {"exp": 99}}, selected_session=None)
    assert created["type"] == "session.created" and created["_session"] is new_session
    assert new_session.loaded

    monkeypatch.setattr(engine, "create_session", lambda *_args, **_kwargs: (_ for _ in ()).throw(engine.CapacityExceededError("full")))
    with pytest.raises(HTTPException) as exc:
        call({"type": "session.create"}, {"accessKey": "demo.fmu", "claims": {}}, selected_session=None)
    assert exc.value.status_code == 429


@pytest.mark.parametrize(
    ("mode", "expected_factory"),
    [("process", process_runner.RealtimeSession), ("in-process", engine.FmuSession)],
)
def test_session_creation_uses_configured_worker_boundary(dispatch, monkeypatch, tmp_path, mode, expected_factory):
    _session, call = dispatch
    new_session = Session()
    created = {}
    monkeypatch.setattr(main.config, "execution_mode", lambda: mode)
    monkeypatch.setattr(main.fmu_storage, "fmu_exists", lambda _key: True)
    monkeypatch.setattr(main.fmu_storage, "get_fmu_path", lambda _key: tmp_path / "demo.fmu")

    def create_session(*args, **kwargs):
        created["args"] = args
        created["kwargs"] = kwargs
        return new_session

    monkeypatch.setattr(engine, "create_session", create_session)

    response = call(
        {"type": "session.create"},
        {"accessKey": "demo.fmu", "claims": {}},
        selected_session=None,
    )

    assert response["_session"] is new_session
    assert created["kwargs"]["session_factory"] is expected_factory


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
    monkeypatch.setattr(engine, "get_attachable_session", lambda _sid: session)
    attached = call({"type": "session.attach", "sessionId": "route-session"}, {"accessKey": "demo.fmu"}, selected_session=None)
    assert attached["type"] == "session.attached"
    with pytest.raises(HTTPException) as exc:
        call({"type": "session.attach", "sessionId": "other"}, {"accessKey": "demo.fmu"})
    assert exc.value.status_code == 403


def test_attach_rejects_missing_expired_and_mismatched_access_key(dispatch, monkeypatch):
    session, call = dispatch
    monkeypatch.setattr(engine, "get_attachable_session", lambda _sid: None)
    with pytest.raises(HTTPException):
        call({"type": "session.attach", "sessionId": "route-session"}, {"accessKey": "demo.fmu"}, selected_session=None)
    with pytest.raises(HTTPException) as exc:
        call({"type": "session.attach"}, {"accessKey": "demo.fmu"})
    assert exc.value.status_code == 401
    monkeypatch.setattr(engine, "get_attachable_session", lambda _sid: session)
    with pytest.raises(HTTPException) as exc:
        call({"type": "session.attach"}, {"accessKey": "another.fmu"})
    assert exc.value.status_code == 403
