from __future__ import annotations

import queue
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
