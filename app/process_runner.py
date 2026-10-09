"""Process boundary for one-shot FMU executions.

Native FMU binaries are third-party code. Keeping batch execution in a
``spawn`` child means a segmentation fault or an unbounded native call does
not take the Station HTTP/WebSocket control plane down with it.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import uuid4

from . import config


class ProcessExecutionError(RuntimeError):
    """A child-process execution failed or exceeded its deadline."""

    def __init__(self, message: str, *, code: str = "FMU_EXECUTION_FAILED", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


_REALTIME_METHODS = {
    "load",
    "initialize",
    "step",
    "run_until",
    "set_inputs",
    "get_outputs",
    "pause",
    "resume",
    "reset",
}


def _realtime_snapshot(session: Any) -> dict[str, Any]:
    model = getattr(session, "_md", None)
    variables = []
    for variable in getattr(model, "modelVariables", ()) or ():
        variables.append({
            "name": str(getattr(variable, "name", "")),
            "valueReference": int(getattr(variable, "valueReference", 0)),
            "type": str(getattr(variable, "type", "")),
            "causality": str(getattr(variable, "causality", "")),
        })
    return {
        "state": session.state,
        "time": float(session._time),
        "startTime": float(session._start_time),
        "stopTime": float(session._stop_time),
        "stepSize": float(session._step_size),
        "initialised": bool(session._initialised),
        "terminated": bool(session._terminated),
        "modelVariables": variables,
    }


def _realtime_session_worker(
    connection: Any,
    session_id: str,
    fmu_path: Path,
    access_key: str,
    session_factory: Any = None,
) -> None:
    """Own one realtime FmuSession and service a narrow IPC command set."""
    session = None
    try:
        if session_factory is None:
            from .engine import FmuSession
            session_factory = FmuSession
        session = session_factory(session_id, fmu_path, access_key=access_key)
        while True:
            request = connection.recv()
            request_id = request.get("requestId") if isinstance(request, dict) else None
            method = request.get("method") if isinstance(request, dict) else None
            args = request.get("args", []) if isinstance(request, dict) else None
            kwargs = request.get("kwargs", {}) if isinstance(request, dict) else None
            if not isinstance(request_id, str) or not isinstance(args, list) or not isinstance(kwargs, dict):
                connection.send({
                    "requestId": request_id,
                    "ok": False,
                    "error": "FMU worker request is invalid",
                    "snapshot": _realtime_snapshot(session),
                })
                continue
            if method == "terminate":
                session.terminate()
                connection.send({
                    "requestId": request_id,
                    "ok": True,
                    "result": None,
                    "snapshot": _realtime_snapshot(session),
                })
                return
            if method not in _REALTIME_METHODS:
                connection.send({
                    "requestId": request_id,
                    "ok": False,
                    "error": "FMU worker command is not allowlisted",
                    "snapshot": _realtime_snapshot(session),
                })
                continue
            try:
                result = getattr(session, method)(*args, **kwargs)
                connection.send({
                    "requestId": request_id,
                    "ok": True,
                    "result": result,
                    "snapshot": _realtime_snapshot(session),
                })
            except Exception as exc:
                connection.send({
                    "requestId": request_id,
                    "ok": False,
                    "error": str(exc) or "FMU worker command failed",
                    "snapshot": _realtime_snapshot(session),
                })
    except (EOFError, OSError):
        pass
    finally:
        if session is not None and not session._terminated:
            session.terminate()
        try:
            connection.close()
        except Exception:
            pass


class RealtimeSession:
    """Parent-side proxy for a single spawned realtime FMU process."""

    def __init__(
        self,
        session_id: str,
        fmu_path: Path,
        *,
        access_key: str | None = None,
        expires_at: float | int | str | None = None,
        gateway_context: dict[str, Any] | None = None,
        _worker_session_factory: Any = None,
    ):
        self.session_id = session_id
        self.fmu_path = Path(fmu_path)
        self.access_key = access_key or self.fmu_path.name
        self.expires_at = expires_at
        self.gateway_context = dict(gateway_context or {})
        self._state = "new"
        self._time = 0.0
        self._start_time = 0.0
        self._stop_time = 1.0
        self._step_size = 0.001
        self._initialised = False
        self._terminated = False
        self._md = None
        self._attached = False
        self._attach_deadline: float | None = None
        self._attachment_owner: object | None = None
        self.seq = 0
        self._pending_samples: list[dict[str, Any]] = []
        self._pending_queue_drops = 0
        self._lock = threading.RLock()
        self._sequence_lock = threading.Lock()
        self._closed = False
        self._request_counter = 0

        context = mp.get_context("spawn")
        self._connection, child_connection = context.Pipe(duplex=True)
        self._process = context.Process(
            target=_realtime_session_worker,
            args=(child_connection, self.session_id, self.fmu_path, self.access_key, _worker_session_factory),
            name="LabStation-FMU-Realtime",
        )
        try:
            self._process.start()
        except Exception as exc:
            self._connection.close()
            child_connection.close()
            self._closed = True
            raise ProcessExecutionError("FMU realtime worker could not be started", retryable=True) from exc
        child_connection.close()
        self.subscription = None

    @property
    def state(self) -> str:
        return "terminated" if self._terminated else self._state

    def _apply_snapshot(self, snapshot: Any) -> None:
        if not isinstance(snapshot, dict):
            raise ProcessExecutionError("FMU realtime worker returned an invalid state")
        self._state = str(snapshot.get("state", self._state))
        self._time = float(snapshot.get("time", self._time))
        self._start_time = float(snapshot.get("startTime", self._start_time))
        self._stop_time = float(snapshot.get("stopTime", self._stop_time))
        self._step_size = float(snapshot.get("stepSize", self._step_size))
        self._initialised = bool(snapshot.get("initialised", self._initialised))
        self._terminated = bool(snapshot.get("terminated", self._terminated))
        variables = snapshot.get("modelVariables", [])
        if isinstance(variables, list):
            self._md = SimpleNamespace(modelVariables=[
                SimpleNamespace(**item) for item in variables if isinstance(item, dict)
            ])

    def _rpc(self, method: str, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        with self._lock:
            if self._closed and method != "terminate":
                raise ProcessExecutionError("FMU realtime worker is closed", retryable=True)
            if not self._process.is_alive() and method != "terminate":
                self._terminated = True
                raise ProcessExecutionError("FMU realtime worker exited unexpectedly", retryable=True)
            self._request_counter += 1
            request_id = f"{self.session_id}:{self._request_counter}:{uuid4().hex}"
            try:
                self._connection.send({
                    "requestId": request_id,
                    "method": method,
                    "args": list(args),
                    "kwargs": kwargs,
                })
                deadline = time.monotonic() + (timeout or config.execution_timeout_seconds())
                response = None
                while time.monotonic() < deadline:
                    if self._connection.poll(min(0.5, max(0.0, deadline - time.monotonic()))):
                        response = self._connection.recv()
                        break
                    if not self._process.is_alive():
                        break
                if response is None:
                    worker_exited = not self._process.is_alive()
                    self._stop_process()
                    self._terminated = True
                    if worker_exited:
                        raise ProcessExecutionError(
                            "FMU realtime worker exited without a response",
                            retryable=True,
                        )
                    raise ProcessExecutionError(
                        "FMU realtime worker exceeded its command deadline",
                        code="FMU_EXECUTION_TIMEOUT",
                        retryable=True,
                    )
                if not isinstance(response, dict) or response.get("requestId") != request_id:
                    self._stop_process()
                    self._terminated = True
                    raise ProcessExecutionError("FMU realtime worker returned an invalid response", retryable=True)
                self._apply_snapshot(response.get("snapshot"))
                if not response.get("ok"):
                    raise RuntimeError(str(response.get("error") or "FMU realtime command failed"))
                return response.get("result")
            except (EOFError, OSError) as exc:
                self._stop_process()
                self._terminated = True
                raise ProcessExecutionError("FMU realtime worker exited unexpectedly", retryable=True) from exc

    def _stop_process(self) -> None:
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(timeout=2)
        if self._process.is_alive():
            kill = getattr(self._process, "kill", None)
            if kill:
                kill()
            self._process.join(timeout=2)

    def load(self) -> dict[str, Any]:
        return self._rpc("load")

    def initialize(self, **kwargs: Any) -> dict[str, Any]:
        return self._rpc("initialize", **kwargs)

    def step(self, step_size: float | None = None) -> dict[str, Any]:
        args = () if step_size is None else (step_size,)
        return self._rpc("step", *args)

    def run_until(self, target_time: float, step_size: float | None = None) -> dict[str, Any]:
        if step_size is None:
            return self._rpc("run_until", target_time)
        return self._rpc("run_until", target_time, step_size=step_size)

    def set_inputs(self, values: dict[str, Any]) -> None:
        self._rpc("set_inputs", values)

    def get_outputs(self, refs: list[int] | None = None) -> dict[str, Any]:
        return self._rpc("get_outputs", refs)

    def pause(self) -> dict[str, Any]:
        return self._rpc("pause")

    def resume(self) -> dict[str, Any]:
        return self._rpc("resume")

    def reset(self) -> dict[str, Any]:
        return self._rpc("reset")

    def sample_subscription(self) -> dict[str, Any] | None:
        if not self.subscription or not self._initialised or self._terminated:
            return None
        variable_refs = None
        if self.subscription.variables is not None and self._md:
            names = set(self.subscription.variables)
            variable_refs = [
                variable.valueReference for variable in self._md.modelVariables
                if variable.name in names and variable.causality == "output"
            ]
        sample = self.get_outputs(variable_refs)["outputs"]
        self._pending_samples.append(sample)
        if len(self._pending_samples) > self.subscription.max_batch_size:
            excess = len(self._pending_samples) - self.subscription.max_batch_size
            self._pending_samples = self._pending_samples[excess:]
            self.subscription.rate_dropped += excess
        min_interval = self.subscription.min_interval_seconds()
        now = time.monotonic()
        if now - self.subscription.last_emit_monotonic < min_interval:
            self.subscription.rate_dropped += 1
            return None
        self.subscription.last_emit_monotonic = now
        with self._sequence_lock:
            payload = {
                "type": "sim.outputs",
                "sessionId": self.session_id,
                "seq": self.seq,
                "dropped": self.subscription.rate_dropped + self._pending_queue_drops,
                "batchSize": len(self._pending_samples),
                "simTime": self._time,
                "values": self._pending_samples[-1],
            }
            self.seq += 1
        self._pending_samples.clear()
        self.subscription.rate_dropped = 0
        self._pending_queue_drops = 0
        return payload

    def next_sequence(self) -> int:
        with self._sequence_lock:
            sequence = self.seq
            self.seq += 1
            return sequence

    def terminate(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._process.is_alive():
                try:
                    self._rpc("terminate", timeout=3)
                except Exception:
                    self._stop_process()
            self._process.join(timeout=2)
            if self._process.is_alive():
                self._stop_process()
            try:
                self._connection.close()
            except Exception:
                pass
            self._closed = True
            self._terminated = True
            self._state = "terminated"
            self._attached = False
            self._attach_deadline = None
            self._attachment_owner = None

    def mark_detached(self, grace_seconds: float, attachment_owner: object | None = None) -> None:
        if attachment_owner is not None and self._attachment_owner is not attachment_owner:
            return
        if not self._terminated:
            self._attached = False
            self._attach_deadline = time.time() + max(0.0, float(grace_seconds))
            self._attachment_owner = None

    def mark_attached(self, attachment_owner: object | None = None) -> None:
        self._attached = True
        self._attach_deadline = None
        self._attachment_owner = attachment_owner

    def can_attach(self, now: float | None = None) -> bool:
        if self._terminated or (not self._attached and self._attach_deadline is None):
            return False
        now = time.time() if now is None else now
        if self.expires_at is not None:
            try:
                if float(self.expires_at) <= now:
                    return False
            except (TypeError, ValueError):
                return False
        return self._attach_deadline is None or now < self._attach_deadline


def _execute_worker(
    fmu_path: str,
    access_key: str,
    parameters: dict[str, Any],
    options: dict[str, Any],
    output_queue: Any,
    streaming: bool,
) -> None:
    """Worker entry point; imports the engine only inside the child process."""
    from .engine import FmuSession

    session = FmuSession(
        f"worker_{mp.current_process().pid}",
        Path(fmu_path),
        access_key=access_key,
    )
    try:
        session.load()
        start = float(options.get("startTime", 0.0))
        stop = float(options.get("stopTime", 1.0))
        step = options.get("stepSize")
        step_value = float(step) if step is not None else None
        session.initialize(
            start_time=start,
            stop_time=stop,
            step_size=step_value,
            parameters=parameters or None,
        )
        if streaming:
            result_bytes = 0
            for snapshot in session.run_until_streaming(stop, step_size=step_value):
                result_bytes += len(json.dumps(snapshot, separators=(",", ":"), allow_nan=False).encode("utf-8"))
                if result_bytes > config.MAX_STORED_RESULT_BYTES:
                    output_queue.put({"kind": "error", "code": "RESULT_SIZE_LIMIT_EXCEEDED"})
                    return
                output_queue.put({"kind": "message", "payload": snapshot})
            output_queue.put({
                "kind": "message",
                "payload": {"type": "sim.done", "time": session._time},
            })
        else:
            initial = session.get_outputs()
            time_values = [initial["time"]]
            output_series = {name: [value] for name, value in initial["outputs"].items()}
            result_bytes = len(json.dumps(initial, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            for snapshot in session.run_until_streaming(stop, step_size=step_value):
                result_bytes += len(
                    json.dumps(
                        {"time": snapshot["time"], "outputs": snapshot["outputs"]},
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                )
                if result_bytes > config.MAX_STORED_RESULT_BYTES:
                    output_queue.put({"kind": "error", "code": "RESULT_SIZE_LIMIT_EXCEEDED"})
                    return
                time_values.append(snapshot["time"])
                for name, value in snapshot["outputs"].items():
                    output_series.setdefault(name, []).append(value)
            output_queue.put({
                "kind": "result",
                "payload": {
                    "type": "sim.result",
                    "time": time_values,
                    "state": "terminated",
                    "outputs": output_series,
                    "outputVariables": list(output_series),
                },
            })
    except Exception:
        output_queue.put({
            "kind": "error",
            "code": "FMU_EXECUTION_FAILED",
            "message": "FMU simulation failed in the isolated worker",
            "retryable": False,
        })
    finally:
        session.terminate()


def _new_process(
    fmu_path: Path,
    access_key: str,
    parameters: dict[str, Any],
    options: dict[str, Any],
    output_queue: Any,
    streaming: bool,
) -> tuple[Any, Any]:
    context = mp.get_context("spawn")
    process = context.Process(
        target=_execute_worker,
        args=(str(fmu_path), access_key, parameters, options, output_queue, streaming),
        name="LabStation-FMU-Worker",
    )
    process.start()
    return process, context


def run(
    fmu_path: Path,
    *,
    access_key: str,
    parameters: dict[str, Any],
    options: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run a one-shot simulation in a spawned process."""
    context = mp.get_context("spawn")
    output_queue = context.Queue(maxsize=1)
    process, _ = _new_process(fmu_path, access_key, parameters, options, output_queue, False)
    deadline = time.monotonic() + config.execution_timeout_seconds()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                if process.is_alive():
                    process.terminate()
                raise ProcessExecutionError(
                    "FMU simulation was cancelled",
                    code="FMU_EXECUTION_CANCELLED",
                    retryable=False,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.terminate()
                raise ProcessExecutionError(
                    "FMU worker exceeded its execution deadline",
                    code="FMU_EXECUTION_TIMEOUT",
                    retryable=True,
                )
            try:
                message = output_queue.get(timeout=min(remaining, 1.0))
                break
            except queue.Empty as exc:
                if not process.is_alive():
                    raise ProcessExecutionError(
                        "FMU worker exited without a result",
                        code="FMU_EXECUTION_FAILED",
                        retryable=True,
                    ) from exc
        if message.get("kind") == "error":
            raise ProcessExecutionError(
                message.get("message", "FMU simulation failed"),
                code=message.get("code", "FMU_EXECUTION_FAILED"),
                retryable=bool(message.get("retryable", False)),
            )
        return message["payload"]
    finally:
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        output_queue.close()
        output_queue.join_thread()


def stream(
    fmu_path: Path,
    *,
    access_key: str,
    parameters: dict[str, Any],
    options: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield NDJSON payloads from an isolated streaming worker."""
    context = mp.get_context("spawn")
    output_queue = context.Queue(maxsize=64)
    process, _ = _new_process(fmu_path, access_key, parameters, options, output_queue, True)
    deadline = time.monotonic() + config.execution_timeout_seconds()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                yield {
                    "type": "error",
                    "code": "FMU_EXECUTION_CANCELLED",
                    "message": "FMU simulation was cancelled",
                    "retryable": False,
                }
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield {
                    "type": "error",
                    "code": "FMU_EXECUTION_TIMEOUT",
                    "message": "FMU worker exceeded its execution deadline",
                    "retryable": True,
                }
                break
            try:
                message = output_queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if not process.is_alive():
                    yield {
                        "type": "error",
                        "code": "FMU_EXECUTION_FAILED",
                        "message": "FMU worker exited without a result",
                    }
                    break
                continue
            kind = message.get("kind")
            if kind == "message":
                payload = message.get("payload")
                if isinstance(payload, dict):
                    yield payload
                if isinstance(payload, dict) and payload.get("type") == "sim.done":
                    break
            elif kind == "error":
                yield {
                    "type": "error",
                    "code": message.get("code", "FMU_EXECUTION_FAILED"),
                    "message": message.get("message", "FMU simulation failed"),
                    "retryable": bool(message.get("retryable", False)),
                }
                break
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        output_queue.close()
        output_queue.join_thread()
