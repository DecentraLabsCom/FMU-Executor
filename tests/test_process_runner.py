from __future__ import annotations

import queue
import runpy
import sys
from types import SimpleNamespace

import pytest

from app import process_runner


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


def install_run_fakes(monkeypatch, messages, *, alive=False):
    output = FakeQueue(*messages)
    process = FakeProcess(alive=alive)
    monkeypatch.setattr(process_runner.mp, "get_context", lambda method: SimpleNamespace(Queue=lambda **kwargs: output))
    monkeypatch.setattr(process_runner, "_new_process", lambda *args: (process, None))
    monkeypatch.setattr(process_runner.config, "execution_timeout_seconds", lambda: 20)
    return output, process


def test_run_returns_child_payload_and_closes_queue(monkeypatch, tmp_path):
    output, process = install_run_fakes(monkeypatch, [{"kind": "result", "payload": {"type": "sim.result", "time": 2}}])

    result = process_runner.run(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})

    assert result == {"type": "sim.result", "time": 2}
    assert output.closed and output.joined
    assert process.join_calls == [2]
    assert not process.terminated


def test_run_converts_child_errors_to_typed_execution_error(monkeypatch, tmp_path):
    install_run_fakes(monkeypatch, [{"kind": "error", "code": "FMU_EXECUTION_FAILED", "message": "safe message", "retryable": True}])

    with pytest.raises(process_runner.ProcessExecutionError, match="safe message") as exc:
        process_runner.run(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})

    assert exc.value.code == "FMU_EXECUTION_FAILED"
    assert exc.value.retryable is True


def test_run_terminates_worker_on_deadline(monkeypatch, tmp_path):
    output, process = install_run_fakes(monkeypatch, [], alive=True)
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
    install_run_fakes(monkeypatch, [queue.Empty()])

    with pytest.raises(process_runner.ProcessExecutionError) as exc:
        process_runner.run(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})

    assert exc.value.code == "FMU_EXECUTION_FAILED"
    assert exc.value.retryable is True


def test_run_terminates_worker_that_remains_alive_after_join(monkeypatch, tmp_path):
    output, process = install_run_fakes(monkeypatch, [{"kind": "result", "payload": {"ok": True}}], alive=True)

    assert process_runner.run(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={}) == {"ok": True}

    assert process.terminated
    assert process.join_calls == [2, 2]
    assert output.closed and output.joined


def test_stream_yields_child_messages_until_done_and_cleans_up(monkeypatch, tmp_path):
    output = FakeQueue(
        {"kind": "message", "payload": {"type": "sim.outputs", "time": 0.5}},
        {"kind": "message", "payload": {"type": "sim.done", "time": 1}},
    )
    process = FakeProcess()
    monkeypatch.setattr(process_runner.mp, "get_context", lambda method: SimpleNamespace(Queue=lambda **kwargs: output))
    monkeypatch.setattr(process_runner, "_new_process", lambda *args: (process, None))
    monkeypatch.setattr(process_runner.config, "execution_timeout_seconds", lambda: 20)

    assert list(process_runner.stream(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})) == [
        {"type": "sim.outputs", "time": 0.5},
        {"type": "sim.done", "time": 1},
    ]
    assert output.closed and output.joined
    assert process.join_calls == [2]


def test_stream_reports_child_error_without_exposing_extra_fields(monkeypatch, tmp_path):
    output = FakeQueue({"kind": "error", "code": "FMU_EXECUTION_FAILED", "message": "generic", "retryable": True, "secret": "hidden"})
    process = FakeProcess()
    monkeypatch.setattr(process_runner.mp, "get_context", lambda method: SimpleNamespace(Queue=lambda **kwargs: output))
    monkeypatch.setattr(process_runner, "_new_process", lambda *args: (process, None))
    monkeypatch.setattr(process_runner.config, "execution_timeout_seconds", lambda: 20)

    assert list(process_runner.stream(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})) == [
        {"type": "error", "code": "FMU_EXECUTION_FAILED", "message": "generic", "retryable": True}
    ]
    assert output.closed and output.joined


def test_stream_reports_worker_exit_without_result(monkeypatch, tmp_path):
    output = FakeQueue(queue.Empty())
    process = FakeProcess()
    monkeypatch.setattr(process_runner.mp, "get_context", lambda method: SimpleNamespace(Queue=lambda **kwargs: output))
    monkeypatch.setattr(process_runner, "_new_process", lambda *args: (process, None))
    monkeypatch.setattr(process_runner.config, "execution_timeout_seconds", lambda: 20)

    assert list(process_runner.stream(tmp_path / "demo.fmu", access_key="demo.fmu", parameters={}, options={})) == [
        {"type": "error", "code": "FMU_EXECUTION_FAILED", "message": "FMU worker exited without a result"}
    ]


def test_stream_timeout_terminates_child(monkeypatch, tmp_path):
    output = FakeQueue()
    process = FakeProcess(alive=True)
    monkeypatch.setattr(process_runner.mp, "get_context", lambda method: SimpleNamespace(Queue=lambda **kwargs: output))
    monkeypatch.setattr(process_runner, "_new_process", lambda *args: (process, None))
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
    output = FakeQueue()

    process_runner._execute_worker(str(tmp_path / "demo.fmu"), "demo.fmu", {"x": 2}, {"stopTime": 3, "stepSize": 0.25}, output, False)

    assert output.items == [{"kind": "result", "payload": {"type": "sim.result", "time": 3, "state": "terminated", "outputs": {"x": 4}}}]


def test_execute_worker_streams_snapshots_and_completion(monkeypatch, tmp_path):
    class Session:
        _time = 1.0

        def __init__(self, *_args, **_kwargs):
            self.terminated = False

        def load(self): pass
        def initialize(self, **_kwargs): pass
        def run_until_streaming(self, _stop, step_size=None): return iter([{"type": "sim.outputs", "time": 0.5}])
        def terminate(self): self.terminated = True

    monkeypatch.setitem(sys.modules, "app.engine", SimpleNamespace(FmuSession=Session))
    output = FakeQueue()

    process_runner._execute_worker(str(tmp_path / "demo.fmu"), "demo.fmu", {}, {}, output, True)

    assert output.items == [
        {"kind": "message", "payload": {"type": "sim.outputs", "time": 0.5}},
        {"kind": "message", "payload": {"type": "sim.done", "time": 1.0}},
    ]


def test_execute_worker_masks_native_error_details_and_terminates(monkeypatch, tmp_path):
    class Session:
        def __init__(self, *_args, **_kwargs):
            self.terminated = False

        def load(self): raise RuntimeError("secret native stack detail")
        def terminate(self): self.terminated = True

    monkeypatch.setitem(sys.modules, "app.engine", SimpleNamespace(FmuSession=Session))
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
        def Process(self, **kwargs):
            self.kwargs = kwargs
            self.process = FakeProcess()
            return self.process

    context = Context()
    monkeypatch.setattr(process_runner.mp, "get_context", lambda method: (setattr(context, "method", method) or context))
    output = FakeQueue()

    process, returned_context = process_runner._new_process(tmp_path / "model.fmu", "model.fmu", {}, {}, output, True)

    assert context.method == "spawn"
    assert context.kwargs["name"] == "LabStation-FMU-Worker"
    assert context.kwargs["args"][-1] is True
    assert process.started
    assert returned_context is context


class FakeRealtimeConnection:
    def __init__(self):
        self.requests = []
        self.responses = []
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
        result = {"modelName": "isolated-demo"} if method == "load" else {"time": 0.5, "state": "running"}
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
        def Pipe(self, duplex=True):
            assert duplex is True
            return connection, SimpleNamespace(close=lambda: None)

        def Process(self, **kwargs):
            self.kwargs = kwargs
            self.process = process
            return process

    context = Context()
    monkeypatch.setattr(process_runner.mp, "get_context", lambda method: (setattr(context, "method", method) or context))

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
    assert session._md.modelVariables[0].valueReference == 2
    assert [request["method"] for request in connection.requests] == ["load", "step"]


def test_realtime_session_terminates_child_and_closes_ipc(monkeypatch, tmp_path):
    connection = FakeRealtimeConnection()
    process = FakeRealtimeProcess(alive=True)

    class Context:
        def Pipe(self, duplex=True):
            return connection, SimpleNamespace(close=lambda: None)

        def Process(self, **kwargs):
            return process

    monkeypatch.setattr(process_runner.mp, "get_context", lambda _method: Context())
    session = process_runner.RealtimeSession("realtime-2", tmp_path / "demo.fmu", access_key="demo.fmu")

    session.terminate()

    assert connection.requests[-1]["method"] == "terminate"
    assert connection.closed
    assert process.join_calls


def test_realtime_session_runs_in_a_real_spawned_process(tmp_path):
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
    session._terminated = False
    session._attached = False
    session._attach_deadline = None
    session._attachment_owner = None
    session.expires_at = None
    return session


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

    connection = Connection()
    process_runner._realtime_session_worker(connection, "session", tmp_path / "demo.fmu", "demo.fmu", Session)

    assert [response["ok"] for response in connection.outgoing] == [True, True, False, True]
    assert connection.outgoing[1]["snapshot"]["time"] == 0.25
    assert connection.outgoing[2]["error"] == "FMU worker command is not allowlisted"
    assert connection.outgoing[3]["snapshot"]["terminated"] is True


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
