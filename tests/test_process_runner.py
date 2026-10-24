from __future__ import annotations

import queue
import runpy
import sys
import threading
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app import process_runner
from tests.fmu_fixture import write_test_fmu


def test_realtime_model_description_is_cached_for_the_immutable_session():
    session = process_runner.RealtimeSession.__new__(process_runner.RealtimeSession)
    session._rpc = Mock(return_value={"modelName": "snapshot", "modelVariables": []})
    assert session.load() == session.load()
    session._rpc.assert_called_once_with("load")


class FakeQueue:
    def __init__(self, *items):
        self.items = list(items)
        self.closed = False
        self.joined = False

    def get(self, timeout=None):
        if not self.items:
            raise queue.Empty
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def put(self, item):
        self.items.append(item)

    def close(self):
        self.closed = True

    def join_thread(self):
        self.joined = True


class FakeProcess:
    def __init__(self, *, alive=False):
        self.alive = alive
        self.pid = 12345
        self.started = False
        self.terminated = False
        self.join_calls = []

    def start(self):
        self.started = True

    def join(self, timeout=None):
        self.join_calls.append(timeout)

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.terminated = True
        self.alive = False


def install_run_fakes(monkeypatch, messages, tmp_path, *, alive=False):
    output = FakeQueue(*messages)
    process = FakeProcess(alive=alive)
    worker_directory = tmp_path / "worker"
    worker_directory.mkdir()
    monkeypatch.setattr(process_runner.mp, "get_context", lambda method: SimpleNamespace(Queue=lambda **kwargs: output))
    monkeypatch.setattr(process_runner, "_new_process", lambda *args, **kwargs: (process, None, None, worker_directory, None))
    monkeypatch.setattr(process_runner.config, "execution_timeout_seconds", lambda: 20)
    return output, process


def install_stream_fakes(monkeypatch, tmp_path, output, process):
    worker_directory = tmp_path / "worker"
    worker_directory.mkdir()
    monkeypatch.setattr(process_runner.mp, "get_context", lambda _method: SimpleNamespace(Queue=lambda **_kwargs: output))
    monkeypatch.setattr(process_runner, "_new_process", lambda *args, **kwargs: (process, None, None, worker_directory, None))
    monkeypatch.setattr(process_runner.config, "execution_timeout_seconds", lambda: 20)
    return worker_directory


def test_run_returns_child_payload_and_closes_queue(monkeypatch, tmp_path):
    output, process = install_run_fakes(monkeypatch, [{"kind": "result", "payload": {"type": "sim.result", "time": 2}}], tmp_path)

    result = process_runner.run(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})

    assert result == {"type": "sim.result", "time": 2}
    assert output.closed and output.joined
    assert process.join_calls == []
    assert not process.terminated


def test_run_converts_child_errors_to_typed_execution_error(monkeypatch, tmp_path):
    install_run_fakes(monkeypatch, [{"kind": "error", "code": "FMU_EXECUTION_FAILED", "message": "safe message", "retryable": True}], tmp_path)

    with pytest.raises(process_runner.ProcessExecutionError, match="safe message") as exc:
        process_runner.run(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})

    assert exc.value.code == "FMU_EXECUTION_FAILED"
    assert exc.value.retryable is True


def test_run_terminates_worker_on_deadline(monkeypatch, tmp_path):
    output, process = install_run_fakes(monkeypatch, [], tmp_path, alive=True)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(process_runner.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(process_runner.config, "execution_timeout_seconds", lambda: 1)

    with pytest.raises(process_runner.ProcessExecutionError) as exc:
        process_runner.run(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})

    assert exc.value.code == "FMU_EXECUTION_TIMEOUT"
    assert exc.value.retryable is True
    assert process.terminated
    assert output.closed and output.joined


def test_run_reports_worker_exit_without_result(monkeypatch, tmp_path):
    install_run_fakes(monkeypatch, [queue.Empty()], tmp_path)

    with pytest.raises(process_runner.ProcessExecutionError) as exc:
        process_runner.run(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})

    assert exc.value.code == "FMU_EXECUTION_FAILED"
    assert exc.value.retryable is True


def test_run_terminates_worker_that_remains_alive_after_join(monkeypatch, tmp_path):
    output, process = install_run_fakes(monkeypatch, [{"kind": "result", "payload": {"ok": True}}], tmp_path, alive=True)

    assert process_runner.run(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={}) == {"ok": True}

    assert process.terminated
    assert process.join_calls == [2]
    assert output.closed and output.joined


@pytest.mark.parametrize(
    ("event", "kwargs", "expected_code"),
    [
        (threading.Event(), {}, "FMU_EXECUTION_CANCELLED"),
        (None, {"authorization_expires_at": 999}, "EXECUTION_AUTHORIZATION_EXPIRED"),
        (None, {"reservation_ends_at": 999}, "RESERVATION_ENDED"),
    ],
)
def test_run_enforces_cancellation_and_authorization_deadlines(monkeypatch, tmp_path, event, kwargs, expected_code):
    if event is not None:
        event.set()
    else:
        monkeypatch.setattr(process_runner.time, "time", lambda: 1000)
    output, process = install_run_fakes(monkeypatch, [], tmp_path, alive=True)
    with pytest.raises(process_runner.ProcessExecutionError) as error:
        process_runner.run(
            tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={},
            cancel_event=event, **kwargs,
        )
    assert error.value.code == expected_code
    assert process.terminated
    assert output.closed and output.joined


def test_run_closes_queue_when_worker_cannot_start(monkeypatch, tmp_path):
    output = FakeQueue()
    monkeypatch.setattr(process_runner.mp, "get_context", lambda _method: SimpleNamespace(Queue=lambda **_kwargs: output))
    monkeypatch.setattr(process_runner, "_new_process", Mock(side_effect=process_runner.ProcessExecutionError("no worker")))
    with pytest.raises(process_runner.ProcessExecutionError, match="no worker"):
        process_runner.run(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})
    assert output.closed and output.joined


def test_worker_directory_cleanup_uses_live_owner_marker(monkeypatch, tmp_path):
    root = tmp_path / "tmp"
    dead = root / "worker_dead"
    live = root / "realtime_live"
    invalid = root / "worker_invalid"
    for directory in (dead, live, invalid):
        directory.mkdir(parents=True)
    process_runner._mark_worker_directory(dead, 101)
    process_runner._mark_worker_directory(live, 202)
    process_runner._worker_owner_marker_path(invalid).write_text("not-a-pid", encoding="ascii")
    monkeypatch.setattr(process_runner.config, "TEMP_DIR", root)
    monkeypatch.setattr(process_runner, "worker_process_is_alive", lambda pid: pid == 202)
    delete_profile = Mock()
    monkeypatch.setattr(process_runner, "delete_worker_appcontainer_profile", delete_profile)

    assert process_runner.cleanup_abandoned_worker_directories() == 2
    assert delete_profile.call_count == (2 if os.name == "nt" else 0)
    assert not dead.exists() and not invalid.exists()
    assert live.exists()
    assert process_runner.cleanup_abandoned_worker_directories() == 0


def test_worker_environment_removes_credentials_only(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "secret")
    monkeypatch.setenv("GATEWAY_TOKEN", "secret")
    monkeypatch.setenv("WORKER_SAFE_VALUE", "retained")
    process_runner._sanitize_worker_environment()
    assert "AWS_ACCESS_KEY_ID" not in os.environ
    assert "GATEWAY_TOKEN" not in os.environ
    assert os.environ["WORKER_SAFE_VALUE"] == "retained"


def test_windows_fmu_staging_copies_and_checks_content_hash(monkeypatch, tmp_path):
    source = tmp_path / "source.fmu"
    source.write_bytes(b"archive bytes")
    worker = tmp_path / "worker"
    worker.mkdir()
    monkeypatch.setattr(process_runner, "os", SimpleNamespace(name="nt"))
    staged = process_runner._stage_windows_fmu(source, worker)
    assert staged.read_bytes() == b"archive bytes"

    digest_named = tmp_path / ("0" * 64 + ".fmu")
    digest_named.write_bytes(b"changed archive")
    target = tmp_path / "worker-mismatch"
    target.mkdir()
    with pytest.raises(process_runner.ProcessExecutionError) as error:
        process_runner._stage_windows_fmu(digest_named, target)
    assert error.value.code == "FMU_SNAPSHOT_HASH_MISMATCH"
    assert not (target / "model.fmu").exists()


def test_realtime_snapshot_exports_only_normalized_model_metadata():
    snapshot = process_runner._realtime_snapshot(SimpleNamespace(
        state="loaded", _time=0.5, _start_time=0.0, _stop_time=2.0, _step_size=0.1,
        _initialised=True, _terminated=False,
        _md=SimpleNamespace(
            modelVariables=[SimpleNamespace(name="y", valueReference=2, type="Real", causality="output")],
            defaultExperiment=SimpleNamespace(stepSize=0.01),
        ),
    ))
    assert snapshot["state"] == "loaded"
    assert snapshot["time"] == 0.5
    assert snapshot["modelVariables"] == [{"name": "y", "valueReference": 2, "type": "Real", "causality": "output"}]
    assert snapshot["defaultExperiment"] == {"stepSize": 0.01}


def test_stream_yields_child_messages_until_done_and_cleans_up(monkeypatch, tmp_path):
    output = FakeQueue(
        {"kind": "message", "payload": {"type": "sim.outputs", "time": 0.5}},
        {"kind": "message", "payload": {"type": "sim.done", "time": 1}},
    )
    process = FakeProcess()
    install_stream_fakes(monkeypatch, tmp_path, output, process)

    assert list(process_runner.stream(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})) == [
        {"type": "sim.outputs", "time": 0.5},
        {"type": "sim.done", "time": 1},
    ]
    assert output.closed and output.joined
    assert process.join_calls == []


def test_stream_reports_child_error_without_exposing_extra_fields(monkeypatch, tmp_path):
    output = FakeQueue({"kind": "error", "code": "FMU_EXECUTION_FAILED", "message": "generic", "retryable": True, "secret": "hidden"})
    process = FakeProcess()
    install_stream_fakes(monkeypatch, tmp_path, output, process)

    assert list(process_runner.stream(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})) == [
        {"type": "error", "code": "FMU_EXECUTION_FAILED", "message": "generic", "retryable": True}
    ]
    assert output.closed and output.joined


def test_stream_reports_worker_exit_without_result(monkeypatch, tmp_path):
    output = FakeQueue(queue.Empty())
    process = FakeProcess()
    install_stream_fakes(monkeypatch, tmp_path, output, process)

    assert list(process_runner.stream(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})) == [
        {"type": "error", "code": "FMU_EXECUTION_FAILED", "message": "FMU worker exited without a result"}
    ]


def test_stream_timeout_terminates_child(monkeypatch, tmp_path):
    output = FakeQueue()
    process = FakeProcess(alive=True)
    install_stream_fakes(monkeypatch, tmp_path, output, process)
    monkeypatch.setattr(process_runner.config, "execution_timeout_seconds", lambda: 1)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(process_runner.time, "monotonic", lambda: next(ticks))

    assert list(process_runner.stream(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})) == [
        {
            "type": "error",
            "code": "FMU_EXECUTION_TIMEOUT",
            "message": "FMU worker exceeded its execution deadline",
            "retryable": True,
        }
    ]
    assert process.terminated
    assert output.closed and output.joined


def test_execute_worker_returns_result_and_always_terminates(monkeypatch, tmp_path):
    class Session:
        _time = 0.9

        def __init__(self, *_args, **_kwargs):
            self.terminated = False

        def load(self): pass
        def initialize(self, **kwargs): self.initialize_kwargs = kwargs
        def run_until(self, stop, step_size=None): return {"time": stop}
        def get_outputs(self): return {"outputs": {"x": 4}}
        def terminate(self): self.terminated = True

    engine = SimpleNamespace(FmuSession=Session)
    monkeypatch.setitem(sys.modules, "app.engine", engine)
    monkeypatch.setattr(process_runner, "apply_worker_limits", lambda *_args, **_kwargs: None)
    output = FakeQueue()

    process_runner._execute_worker(str(tmp_path / "demo.fmu"), "demo.fmu", {"x": 2}, {"stopTime": 3, "stepSize": 0.25}, output, False)

    assert output.items == [{"kind": "result", "payload": {
        "type": "sim.result", "time": 3.0, "state": "terminated", "outputs": {"x": 4},
        "outputVariables": ["x"], "finishReason": None,
    }}]


def test_execute_worker_streams_snapshots_and_completion(monkeypatch, tmp_path):
    class Session:
        _time = 1.0

        def __init__(self, *_args, **_kwargs):
            self.terminated = False
            self._finished_reason = None

        def load(self): pass
        def initialize(self, **_kwargs): pass
        def run_until_streaming(self, _stop, step_size=None, input_schedule=None): return iter([{"type": "sim.outputs", "time": 0.5, "outputs": {}}])
        def terminate(self): self.terminated = True

    monkeypatch.setitem(sys.modules, "app.engine", SimpleNamespace(FmuSession=Session))
    monkeypatch.setattr(process_runner, "apply_worker_limits", lambda *_args, **_kwargs: None)
    output = FakeQueue()

    process_runner._execute_worker(str(tmp_path / "demo.fmu"), "demo.fmu", {}, {}, output, True)

    assert output.items == [
        {"kind": "message", "payload": {"type": "sim.outputs", "time": 0.5, "outputs": {}}},
        {"kind": "message", "payload": {"type": "sim.done", "time": 1.0, "finishReason": None}},
    ]


def test_execute_worker_masks_native_error_details_and_terminates(monkeypatch, tmp_path):
    class Session:
        def __init__(self, *_args, **_kwargs):
            self.terminated = False

        def load(self): raise RuntimeError("secret native stack detail")
        def terminate(self): self.terminated = True

    monkeypatch.setitem(sys.modules, "app.engine", SimpleNamespace(FmuSession=Session))
    monkeypatch.setattr(process_runner, "apply_worker_limits", lambda *_args, **_kwargs: None)
    output = FakeQueue()

    process_runner._execute_worker(str(tmp_path / "demo.fmu"), "demo.fmu", {}, {}, output, False)

    assert output.items == [{
        "kind": "error",
        "code": "FMU_EXECUTION_FAILED",
        "message": "FMU simulation failed in the isolated worker",
        "retryable": False,
    }]


def test_new_process_uses_spawn_and_starts_named_worker(monkeypatch, tmp_path):
    class Context:
        method: str | None = None

        def Event(self):
            return threading.Event()

        def Process(self, **kwargs):
            self.kwargs = kwargs
            self.process = FakeProcess()
            return self.process

    context = Context()
    monkeypatch.setattr(process_runner.mp, "get_context", lambda method: (setattr(context, "method", method) or context))
    monkeypatch.setattr(process_runner.config, "TEMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(process_runner, "attach_worker_limits", lambda *_args, **_kwargs: None)
    def make_process(_context, **kwargs):
        context.kwargs = kwargs
        context.process = FakeProcess()
        return context.process
    monkeypatch.setattr(process_runner, "_worker_process", make_process)
    write_test_fmu(tmp_path / "model.fmu")
    output = FakeQueue()

    process, returned_context, job, worker_directory, _start_gate = process_runner._new_process(
        tmp_path / "model.fmu", "model.fmu", {}, {}, output, True,
    )

    assert context.method == "spawn"
    assert context.kwargs["name"] == "LabStation-FMU-Worker"
    assert context.kwargs["args"][5] is True
    assert process.started
    assert returned_context is context
    assert job is None
    process_runner._cleanup_worker_directory(worker_directory)


class FakeRealtimeConnection:
    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = [{"type": "ready"}]
        self.closed = False

    def send(self, request):
        self.requests.append(request)
        method = request["method"]
        snapshot = {
            "state": "loaded" if method == "load" else "running",
            "time": 0.5 if method == "step" else 0.0,
            "startTime": 0.0,
            "stopTime": 2.0,
            "stepSize": 0.5,
            "initialised": method != "load",
            "terminated": method == "terminate",
            "modelVariables": [{"name": "y", "valueReference": 2, "type": "Real", "causality": "output"}],
        }
        if method == "load":
            result = {"modelName": "isolated-demo"}
        elif method == "get_outputs":
            result = {"outputs": {"y": 0.5}}
        else:
            result = {"time": 0.5, "state": "running"}
        self.responses.append({"requestId": request["requestId"], "ok": True, "result": result, "snapshot": snapshot})

    def poll(self, _timeout=None):
        return bool(self.responses)

    def recv(self):
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class FakeRealtimeProcess(FakeProcess):
    pass


class SpawnSafeSession:
    """Small picklable session used to exercise the real spawn/IPC boundary."""

    def __init__(self, *_args, **_kwargs):
        self._time = 0.0
        self._start_time = 0.0
        self._stop_time = 1.0
        self._step_size = 0.1
        self._initialised = False
        self._terminated = False
        self._state = "new"
        self._md = SimpleNamespace(modelVariables=[
            SimpleNamespace(name="y", valueReference=2, type="Real", causality="output")
        ])

    @property
    def state(self):
        return "terminated" if self._terminated else self._state

    def load(self):
        self._state = "loaded"
        return {"modelName": "spawn-safe"}

    def initialize(self, *, start_time=0.0, stop_time=1.0, step_size=None, parameters=None):
        self._time = start_time
        self._start_time = start_time
        self._stop_time = stop_time
        self._step_size = step_size or 0.1
        self._initialised = True
        self._state = "initialized"
        return {"time": self._time, "state": self._state}

    def step(self, step_size=None):
        self._time += step_size or self._step_size
        self._state = "running"
        return {"time": self._time, "state": self._state}

    def get_outputs(self, refs=None):
        return {"time": self._time, "outputs": {"y": self._time}}

    def terminate(self):
        self._terminated = True


def test_realtime_session_uses_spawn_worker_and_mirrors_worker_state(monkeypatch, tmp_path):
    connection = FakeRealtimeConnection()
    process = FakeRealtimeProcess(alive=True)

    class Context:
        method: str | None = None

        def Pipe(self, duplex=True):
            assert duplex is True
            return connection, SimpleNamespace(close=lambda: None)

        def Event(self):
            return threading.Event()

        def Process(self, **kwargs):
            self.kwargs = kwargs
            self.process = process
            return process

    context = Context()
    monkeypatch.setattr(process_runner.mp, "get_context", lambda method: (setattr(context, "method", method) or context))
    monkeypatch.setattr(process_runner.config, "TEMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(process_runner, "attach_worker_limits", lambda *_args, **_kwargs: None)
    def make_process(_context, **kwargs):
        context.kwargs = kwargs
        context.process = process
        return process
    monkeypatch.setattr(process_runner, "_worker_process", make_process)
    write_test_fmu(tmp_path / "demo.fmu")

    session = process_runner.RealtimeSession("realtime-1", tmp_path / "demo.fmu", access_key="demo.fmu")
    description = session.load()
    result = session.step(0.5)

    assert context.method == "spawn"
    assert context.kwargs["name"] == "LabStation-FMU-Realtime"
    assert context.kwargs["target"] is process_runner._realtime_session_worker
    assert description == {"modelName": "isolated-demo"}
    assert result == {"time": 0.5, "state": "running"}
    assert session.state == "running"
    assert session._time == 0.5
    assert session._md is not None
    assert session._md.modelVariables[0].valueReference == 2
    assert [request["method"] for request in connection.requests] == ["load", "step"]


def test_realtime_session_terminates_child_and_closes_ipc(monkeypatch, tmp_path):
    connection = FakeRealtimeConnection()
    process = FakeRealtimeProcess(alive=True)

    class Context:
        def Pipe(self, duplex=True):
            return connection, SimpleNamespace(close=lambda: None)

        def Event(self):
            return threading.Event()

        def Process(self, **kwargs):
            return process

    monkeypatch.setattr(process_runner.mp, "get_context", lambda _method: Context())
    monkeypatch.setattr(process_runner.config, "TEMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(process_runner, "attach_worker_limits", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(process_runner, "_worker_process", lambda _context, **_kwargs: process)
    write_test_fmu(tmp_path / "demo.fmu")
    session = process_runner.RealtimeSession("realtime-2", tmp_path / "demo.fmu", access_key="demo.fmu")

    session.terminate()

    assert connection.requests[-1]["method"] == "terminate"
    assert connection.closed
    assert process.join_calls


@pytest.mark.skipif(os.name == "nt", reason="AppContainer launch is covered with isolated WinAPI mocks")
def test_realtime_session_runs_in_a_real_spawned_process(monkeypatch, tmp_path):
    monkeypatch.setattr(process_runner.config, "TEMP_DIR", tmp_path / "tmp")
    write_test_fmu(tmp_path / "demo.fmu")
    session = process_runner.RealtimeSession(
        "spawn-session",
        tmp_path / "demo.fmu",
        access_key="demo.fmu",
        _worker_session_factory=SpawnSafeSession,
    )
    try:
        assert session.load() == {"modelName": "spawn-safe"}
        assert session.initialize(start_time=1.0, stop_time=2.0, step_size=0.25)["state"] == "initialized"
        assert session.step()["time"] == 1.25
        assert session.get_outputs()["outputs"] == {"y": 1.25}
        assert session.state == "running"
    finally:
        session.terminate()
    assert not session._process.is_alive()


def bare_realtime_session():
    session = process_runner.RealtimeSession.__new__(process_runner.RealtimeSession)
    session.gateway_context = {}
    session._output_read_lock = threading.Lock()
    session._last_output_read_monotonic = None
    session._terminated = False
    session._attached = False
    session._attach_deadline = None
    session._attachment_owner = None
    session.expires_at = None
    return session


def rpc_realtime_session(tmp_path, connection):
    session = bare_realtime_session()
    worker_directory = tmp_path / "rpc-worker"
    worker_directory.mkdir()
    session.session_id = "rpc-session"
    session._closed = False
    session._process = FakeRealtimeProcess(alive=True)
    session._job = None
    session._connection = connection
    session._worker_directory = worker_directory
    session._lock = threading.RLock()
    session._request_counter = 0
    session._state = "new"
    session._time = 0.0
    session._start_time = 0.0
    session._stop_time = 1.0
    session._step_size = 0.1
    session._time_mode = "simtime"
    session._playback_speed = 1.0
    session._finished_reason = None
    session._sequence_lock = threading.Lock()
    session._pending_samples = []
    session._pending_queue_drops = 0
    session._initialised = True
    session._md = SimpleNamespace(modelVariables=[
        SimpleNamespace(name="y", valueReference=2, causality="output"),
    ])
    session.seq = 0
    session.subscription = None
    connection.responses.clear()
    return session


def test_realtime_rpc_methods_update_state_and_batch_subscriptions(monkeypatch, tmp_path):
    from app import engine

    connection = FakeRealtimeConnection()
    session = rpc_realtime_session(tmp_path, connection)
    monkeypatch.setattr(process_runner.time, "monotonic", lambda: 10.0)

    assert session.load() == {"modelName": "isolated-demo"}
    assert session.initialize(stop_time=2.0)["state"] == "running"
    assert session.validate_initial_values({"y": 1})["state"] == "running"
    assert session.step(0.25)["time"] == 0.5
    assert session.run_until(1.0, 0.1)["time"] == 0.5
    assert session.set_inputs({"x": 2}) is None
    assert session.validate_inputs({"x": 2})["state"] == "running"
    assert session.get_outputs([2]) == {"outputs": {"y": 0.5}}
    assert session.pause()["state"] == "running"
    assert session.resume()["state"] == "running"

    session.subscription = engine.OutputSubscription(variables=["y"], period_ms=1, max_batch_size=2)
    session._pending_queue_drops = 1
    sample = session.sample_subscription()
    assert sample is not None
    assert sample["values"] == {"y": 0.5}
    assert sample["dropped"] == 1 and sample["seq"] == 0
    assert session.next_sequence() == 1
    assert session.reset(defer_initialization=True)["state"] == "running"
    assert session.subscription is not None
    assert session.subscription.rate_dropped == 0

    session.terminate()
    assert session._closed and session.state == "terminated"
    assert connection.closed


def test_realtime_output_reads_share_a_rate_limit(monkeypatch, tmp_path):
    from app.engine import OutputSubscription

    connection = FakeRealtimeConnection()
    session = rpc_realtime_session(tmp_path, connection)
    now = [50.0]
    monkeypatch.setattr(process_runner.time, "monotonic", lambda: now[0])

    assert session.output_read_wait_seconds(now=now[0]) == 0.0
    first = session.get_outputs_limited([2])
    assert first == {"outputs": {"y": 0.5}}
    assert session.output_read_wait_seconds(now=now[0] + 0.001) > 0

    with pytest.raises(process_runner.OutputReadRateLimitError) as limited:
        session.get_outputs_limited([2])
    assert limited.value.retry_after_ms > 0

    session.subscription = OutputSubscription(variables=["y"], period_ms=1)
    session._output_read_lock.acquire()
    try:
        assert session.output_read_wait_seconds(now=now[0]) >= 0.05
        assert session.sample_subscription() is None
    finally:
        session._output_read_lock.release()

    now[0] += 0.02
    assert session.get_outputs_limited([2]) == {"outputs": {"y": 0.5}}


def test_realtime_step_and_run_until_include_rate_limited_outputs(monkeypatch, tmp_path):
    connection = FakeRealtimeConnection()
    session = rpc_realtime_session(tmp_path, connection)
    now = [80.0]
    monkeypatch.setattr(process_runner.time, "monotonic", lambda: now[0])

    step_result, step_outputs = session.step_with_outputs(0.25)
    assert step_result["time"] == 0.5
    assert step_outputs == {"outputs": {"y": 0.5}}

    now[0] += 0.02
    run_result, run_outputs = session.run_until_with_outputs(1.0, 0.1)
    assert run_result["time"] == 0.5
    assert run_outputs == {"outputs": {"y": 0.5}}
    assert [request["method"] for request in connection.requests] == [
        "step", "get_outputs", "run_until", "get_outputs",
    ]


def test_realtime_rpc_rejects_closed_dead_expired_and_malformed_workers(monkeypatch, tmp_path):
    session = rpc_realtime_session(tmp_path, FakeRealtimeConnection())
    session._closed = True
    with pytest.raises(process_runner.ProcessExecutionError, match="closed"):
        session.load()

    session._closed = False
    session._process.alive = False
    with pytest.raises(process_runner.ProcessExecutionError, match="exited unexpectedly"):
        session.load()

    session._process.alive = True
    session.expires_at = 999
    monkeypatch.setattr(process_runner.time, "time", lambda: 1000)
    with pytest.raises(process_runner.ProcessExecutionError) as error:
        session.load()
    assert error.value.code == "EXECUTION_AUTHORIZATION_EXPIRED"

    connection = FakeRealtimeConnection()
    session = rpc_realtime_session(tmp_path, connection)

    def bad_response(request):
        connection.responses.append({
            "requestId": "wrong", "ok": True,
            "snapshot": {"state": "loaded", "time": 0},
        })

    connection.send = bad_response
    with pytest.raises(process_runner.ProcessExecutionError, match="invalid response"):
        session.load()


def test_realtime_rpc_maps_child_command_errors(tmp_path):
    connection = FakeRealtimeConnection()
    session = rpc_realtime_session(tmp_path, connection)
    original_send = connection.send

    def rejected(request):
        original_send(request)
        connection.responses[-1]["ok"] = False
        connection.responses[-1]["error"] = "safe child error"

    connection.send = rejected
    with pytest.raises(RuntimeError, match="safe child error"):
        session.load()


def test_realtime_attachment_grace_period_and_owner_are_enforced(monkeypatch):
    session = bare_realtime_session()
    owner = object()
    other_owner = object()
    monkeypatch.setattr(process_runner.time, "time", lambda: 1000.0)

    session.mark_attached(owner)
    session.mark_detached(30, attachment_owner=other_owner)
    assert session._attached is True

    session.mark_detached(30, attachment_owner=owner)
    assert session._attached is False
    assert session.can_attach() is True
    assert session.can_attach(now=1030.0) is False

    session.mark_attached(owner)
    assert session.can_attach() is True
    session.mark_detached(10, attachment_owner=other_owner)
    assert session._attached is True
    session.mark_detached(10, attachment_owner=owner)
    assert session._attached is False
    assert session.can_attach(now=1009.0) is True
    assert session.can_attach(now=1010.0) is False


@pytest.mark.parametrize("expires_at", [1000.0, "invalid"])
def test_realtime_attachment_rejects_expired_or_invalid_expiry(monkeypatch, expires_at):
    session = bare_realtime_session()
    session.mark_attached()
    session.expires_at = expires_at
    monkeypatch.setattr(process_runner.time, "time", lambda: 1000.0)

    assert session.can_attach() is False


def test_terminated_realtime_session_remains_non_attachable():
    session = bare_realtime_session()
    session._terminated = True

    session.mark_detached(5)
    session.mark_attached()

    assert session._attached is True
    assert session.can_attach() is False


def test_registry_releases_crashed_worker_even_while_client_is_attached(monkeypatch):
    from unittest.mock import Mock

    from app import engine

    dead_session = SimpleNamespace(
        _terminated=True,
        _attached=True,
        can_attach=lambda _now=None: True,
        terminate=Mock(),
    )
    monkeypatch.setattr(engine, "_sessions", {"dead-session": dead_session})

    engine.cleanup_expired_sessions()

    assert engine.get_session("dead-session") is None
    dead_session.terminate.assert_called_once_with()


def test_realtime_worker_dispatches_only_allowlisted_session_methods(monkeypatch, tmp_path):
    class Session:
        def __init__(self, *_args, **_kwargs):
            self._time = 0.0
            self._start_time = 0.0
            self._state = "new"
            self._stop_time = 1.0
            self._step_size = 0.1
            self._initialised = False
            self._terminated = False
            self._md = SimpleNamespace(modelVariables=[])

        @property
        def state(self):
            return self._state

        def load(self):
            self._state = "loaded"
            return {"modelName": "test"}

        def step(self, amount=0.1):
            self._time += amount
            self._state = "running"
            return {"time": self._time, "state": self._state}

        def terminate(self):
            self._terminated = True

    class Connection:
        def __init__(self):
            self.incoming = [
                {"requestId": "1", "method": "load", "args": [], "kwargs": {}},
                {"requestId": "2", "method": "step", "args": [0.25], "kwargs": {}},
                {"requestId": "3", "method": "arbitrary_shell", "args": [], "kwargs": {}},
                {"requestId": "4", "method": "terminate", "args": [], "kwargs": {}},
            ]
            self.outgoing = []

        def recv(self):
            if not self.incoming:
                raise EOFError
            return self.incoming.pop(0)

        def send(self, response):
            self.outgoing.append(response)

        def close(self):
            pass

    monkeypatch.setattr(process_runner, "apply_worker_limits", lambda *_args, **_kwargs: None)
    connection = Connection()
    process_runner._realtime_session_worker(connection, "session", tmp_path / "demo.fmu", "demo.fmu", Session)

    assert connection.outgoing[0] == {"type": "ready"}
    assert [response["ok"] for response in connection.outgoing[1:]] == [True, True, False, True]
    assert connection.outgoing[2]["snapshot"]["time"] == 0.25
    assert connection.outgoing[3]["error"] == "FMU worker command is not allowlisted"
    assert connection.outgoing[4]["snapshot"]["terminated"] is True


def test_module_entrypoint_does_not_start_uvicorn_when_spawn_reimports_it():
    from unittest.mock import patch
    import sys

    entrypoint = sys.modules.pop("app.__main__", None)
    try:
        with patch("uvicorn.run") as run_server:
            runpy.run_module("app.__main__", run_name="__mp_main__")
        run_server.assert_not_called()
    finally:
        if entrypoint is not None:
            sys.modules["app.__main__"] = entrypoint
