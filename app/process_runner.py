"""Process boundary for one-shot FMU executions.

Native FMU binaries are third-party code. Keeping batch execution in a
``spawn`` child means a segmentation fault or an unbounded native call does
not take the Station HTTP/WebSocket control plane down with it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import multiprocessing as mp
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Iterator
from uuid import uuid4

from . import config
from .isolation import (
    WindowsWorkerJob,
    WorkerIsolationError,
    apply_worker_limits,
    attach_worker_limits,
    bind_worker_to_parent,
    terminate_worker,
    worker_process_is_alive,
)
from .windows_appcontainer import WindowsAppContainerProcess, delete_worker_appcontainer_profile

if TYPE_CHECKING:
    from .engine import OutputSubscription


class ProcessExecutionError(RuntimeError):
    """A child-process execution failed or exceeded its deadline."""

    def __init__(self, message: str, *, code: str = "FMU_EXECUTION_FAILED", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class OutputReadRateLimitError(Exception):
    """A realtime session attempted an output read before its interval elapsed."""

    def __init__(self, retry_after_ms: int):
        super().__init__("Realtime output reads exceeded the per-session rate limit")
        self.retry_after_ms = retry_after_ms


_REALTIME_METHODS = {
    "load",
    "initialize",
    "validate_initial_values",
    "step",
    "run_until",
    "validate_inputs",
    "set_inputs",
    "get_outputs",
    "pause",
    "resume",
    "reset",
}

_SECRET_ENV_MARKERS = (
    "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY", "API_KEY",
    "AWS_", "AZURE_", "GOOGLE_APPLICATION_CREDENTIALS",
)
_WORKER_OWNER_MARKER = ".worker-pid"


def _worker_owner_marker_path(directory: Path) -> Path:
    return directory.parent / f".{directory.name}{_WORKER_OWNER_MARKER}"


def _cleanup_worker_directory(directory: Path) -> None:
    if os.name == "nt" and not delete_worker_appcontainer_profile(directory.name):
        return
    shutil.rmtree(directory, ignore_errors=True)
    try:
        _worker_owner_marker_path(directory).unlink()
    except OSError:
        pass


def cleanup_abandoned_worker_directories() -> int:
    """Remove private worker folders whose spawned process no longer exists."""
    root = config.TEMP_DIR
    if not root.is_dir():
        return 0
    resolved_root = root.resolve()
    removed = 0
    for directory in root.iterdir():
        if not directory.name.startswith(("worker_", "realtime_")) or directory.is_symlink() or not directory.is_dir():
            continue
        try:
            if directory.resolve(strict=True).parent != resolved_root:
                continue
            pid = int(_worker_owner_marker_path(directory).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pid = None
        if pid is not None and worker_process_is_alive(pid):
            continue
        _cleanup_worker_directory(directory)
        if not directory.exists():
            removed += 1
    return removed


def _mark_worker_directory(directory: Path, pid: int) -> None:
    _worker_owner_marker_path(directory).write_text(str(pid), encoding="ascii")


def _worker_process(
    context: Any,
    *,
    target: Any,
    args: tuple[Any, ...],
    name: str,
    worker_directory: Path,
) -> Any:
    if os.name == "nt":
        return WindowsAppContainerProcess(
            target=target,
            args=args,
            name=name,
            worker_directory=worker_directory,
        )
    return context.Process(target=target, args=args, name=name)


def _sanitize_worker_environment() -> None:
    for name in list(os.environ):
        upper_name = name.upper()
        if any(marker in upper_name for marker in _SECRET_ENV_MARKERS):
            os.environ.pop(name, None)


def _stage_windows_fmu(source: Path, worker_directory: Path) -> Path:
    """Give each Windows worker its own verified copy of the pinned archive."""
    if os.name != "nt":
        return source
    target = worker_directory / "model.fmu"
    digest = hashlib.sha256()
    copied_bytes = 0
    try:
        if source.stat().st_size > config.MAX_FMU_ARCHIVE_BYTES:
            raise ProcessExecutionError("FMU snapshot exceeds the archive size limit", code="FMU_ARCHIVE_SIZE_LIMIT_EXCEEDED")
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                copied_bytes += len(chunk)
                if copied_bytes > config.MAX_FMU_ARCHIVE_BYTES:
                    raise ProcessExecutionError("FMU snapshot exceeds the archive size limit", code="FMU_ARCHIVE_SIZE_LIMIT_EXCEEDED")
                digest.update(chunk)
                outgoing.write(chunk)
        expected = source.stem.lower()
        if len(expected) == 64 and all(char in "0123456789abcdef" for char in expected) and digest.hexdigest() != expected:
            raise ProcessExecutionError("FMU snapshot hash changed before execution", code="FMU_SNAPSHOT_HASH_MISMATCH")
        return target
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise


def _realtime_snapshot(session: Any) -> dict[str, Any]:
    model = getattr(session, "_md", None)
    default_experiment = getattr(model, "defaultExperiment", None)
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
        "timeMode": str(getattr(session, "_time_mode", "simtime")),
        "playbackSpeed": float(getattr(session, "_playback_speed", 1.0)),
        "finishedReason": getattr(session, "_finished_reason", None),
        "initialised": bool(session._initialised),
        "terminated": bool(session._terminated),
        "modelVariables": variables,
        "defaultExperiment": {
            "stepSize": getattr(default_experiment, "stepSize", None),
        } if default_experiment is not None else None,
    }


def _realtime_session_worker(
    connection: Any,
    session_id: str,
    fmu_path: Path,
    access_key: str,
    session_factory: Any = None,
    start_gate: Any = None,
    worker_directory: str | None = None,
    resource_policy: dict[str, Any] | None = None,
    expected_parent_pid: int | None = None,
) -> None:
    """Own one realtime FmuSession and service a narrow IPC command set."""
    session = None
    try:
        if expected_parent_pid is not None:
            bind_worker_to_parent(expected_parent_pid)
        if start_gate is not None:
            start_gate.wait()
        apply_worker_limits(resource_policy, worker_directory, str(fmu_path))
        _sanitize_worker_environment()
        if worker_directory:
            os.chdir(worker_directory)
            os.environ["TMP"] = worker_directory
            os.environ["TEMP"] = worker_directory
            os.environ["TMPDIR"] = worker_directory
            config.TEMP_DIR = Path(worker_directory)
        if session_factory is None:
            from .engine import FmuSession
            session_factory = FmuSession
        session = session_factory(session_id, fmu_path, access_key=access_key)
        if resource_policy is not None:
            session._max_simulation_steps = min(
                config.MAX_SIMULATION_STEPS,
                int(resource_policy.get("maxSteps", config.MAX_SIMULATION_STEPS)),
            )
        connection.send({"type": "ready"})
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
    except WorkerIsolationError:
        try:
            connection.send({"type": "startup_error", "code": "FMU_WORKER_ISOLATION_UNAVAILABLE"})
        except (EOFError, OSError):
            pass
    except Exception:
        try:
            connection.send({"type": "startup_error", "code": "FMU_WORKER_START_FAILED"})
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
        resource_policy: dict[str, Any] | None = None,
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
        self._time_mode = "simtime"
        self._playback_speed = 1.0
        self._finished_reason = None
        self._initialised = False
        self._terminated = False
        self._md = None
        self._attached = False
        self._attach_deadline: float | None = None
        self._attachment_owner: object | None = None
        self.seq = 0
        self.subscription: OutputSubscription | None = None
        self._pending_samples: list[dict[str, Any]] = []
        self._pending_queue_drops = 0
        self._lock = threading.RLock()
        self._output_read_lock = threading.Lock()
        self._last_output_read_monotonic: float | None = None
        self._sequence_lock = threading.Lock()
        self._closed = False
        self._request_counter = 0

        context = mp.get_context("spawn")
        self._connection, child_connection = context.Pipe(duplex=True)
        config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self._worker_directory = Path(tempfile.mkdtemp(prefix="realtime_", dir=str(config.TEMP_DIR)))
        if os.name != "nt":
            os.chmod(self._worker_directory, 0o700)
        try:
            worker_fmu_path = _stage_windows_fmu(self.fmu_path, self._worker_directory)
        except Exception:
            _cleanup_worker_directory(self._worker_directory)
            self._connection.close()
            child_connection.close()
            raise
        self._job: WindowsWorkerJob | None = None
        start_gate = context.Event()
        self._process = _worker_process(
            context,
            target=_realtime_session_worker,
            args=(
                child_connection, self.session_id, worker_fmu_path, self.access_key,
                _worker_session_factory, start_gate, str(self._worker_directory), resource_policy,
                os.getpid(),
            ),
            name="LabStation-FMU-Realtime",
            worker_directory=self._worker_directory,
        )
        try:
            self._process.start()
            worker_pid = self._process.pid
            if worker_pid is None:
                raise ProcessExecutionError(
                    "FMU realtime worker did not receive a process ID",
                    code="FMU_WORKER_START_FAILED",
                    retryable=False,
                )
            self._job = attach_worker_limits(self._process, resource_policy)
            _mark_worker_directory(self._worker_directory, worker_pid)
            start_gate.set()
            if not self._connection.poll(min(15.0, config.execution_timeout_seconds())):
                raise ProcessExecutionError("FMU realtime worker did not become ready", code="FMU_WORKER_START_FAILED", retryable=True)
            startup = self._connection.recv()
            if not isinstance(startup, dict) or startup.get("type") != "ready":
                code = startup.get("code") if isinstance(startup, dict) else None
                if code not in {"FMU_WORKER_ISOLATION_UNAVAILABLE", "FMU_WORKER_START_FAILED"}:
                    code = "FMU_WORKER_START_FAILED"
                raise ProcessExecutionError(
                    "FMU realtime worker could not be isolated or initialized",
                    code=code,
                    retryable=code != "FMU_WORKER_ISOLATION_UNAVAILABLE",
                )
        except ProcessExecutionError:
            start_gate.set()
            self._connection.close()
            child_connection.close()
            terminate_worker(self._process, self._job)
            self._job = None
            _cleanup_worker_directory(self._worker_directory)
            self._closed = True
            raise
        except Exception as exc:
            start_gate.set()
            self._connection.close()
            child_connection.close()
            terminate_worker(self._process, self._job)
            self._job = None
            _cleanup_worker_directory(self._worker_directory)
            self._closed = True
            code = "FMU_WORKER_ISOLATION_UNAVAILABLE" if isinstance(exc, WorkerIsolationError) else "FMU_WORKER_START_FAILED"
            raise ProcessExecutionError("FMU realtime worker could not be started or isolated", code=code, retryable=False) from exc
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
        self._time_mode = str(snapshot.get("timeMode", self._time_mode))
        self._playback_speed = float(snapshot.get("playbackSpeed", self._playback_speed))
        self._finished_reason = snapshot.get("finishedReason", self._finished_reason)
        self._initialised = bool(snapshot.get("initialised", self._initialised))
        self._terminated = bool(snapshot.get("terminated", self._terminated))
        variables = snapshot.get("modelVariables", [])
        default_experiment = snapshot.get("defaultExperiment")
        if isinstance(variables, list):
            self._md = SimpleNamespace(modelVariables=[
                SimpleNamespace(**item) for item in variables if isinstance(item, dict)
            ], defaultExperiment=(
                SimpleNamespace(**default_experiment) if isinstance(default_experiment, dict) else None
            ))

    def _rpc(self, method: str, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        with self._lock:
            if self._closed and method != "terminate":
                raise ProcessExecutionError("FMU realtime worker is closed", retryable=True)
            expired_code = self._expired_deadline_code()
            if expired_code and method != "terminate":
                self._stop_process()
                self._terminated = True
                raise ProcessExecutionError(
                    "FMU realtime authorization or reservation ended",
                    code=expired_code,
                    retryable=False,
                )
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
                    expired_code = self._expired_deadline_code()
                    if expired_code and method != "terminate":
                        self._stop_process()
                        self._terminated = True
                        raise ProcessExecutionError(
                            "FMU realtime authorization or reservation ended",
                            code=expired_code,
                            retryable=False,
                        )
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
                expired_code = self._expired_deadline_code()
                if expired_code and method != "terminate":
                    self._stop_process()
                    self._terminated = True
                    raise ProcessExecutionError(
                        "FMU realtime authorization or reservation ended",
                        code=expired_code,
                        retryable=False,
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
        if self._process.pid is not None:
            terminate_worker(self._process, self._job)
            self._job = None
        _cleanup_worker_directory(self._worker_directory)

    def _expired_deadline_code(self, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        if self.expires_at is not None:
            try:
                expires_at = float(self.expires_at)
                if not math.isfinite(expires_at) or expires_at <= now:
                    return "EXECUTION_AUTHORIZATION_EXPIRED"
            except (TypeError, ValueError, OverflowError):
                return "EXECUTION_AUTHORIZATION_EXPIRED"
        claims = self.gateway_context.get("claims") or {}
        reservation_end = self.gateway_context.get("reservationEndsAt")
        if reservation_end is None and isinstance(claims, dict):
            reservation_end = claims.get("reservationEndsAt")
        if reservation_end is not None:
            try:
                reservation_ends_at = float(reservation_end)
                if not math.isfinite(reservation_ends_at) or reservation_ends_at <= now:
                    return "RESERVATION_ENDED"
            except (TypeError, ValueError, OverflowError):
                return "RESERVATION_ENDED"
        return None

    def load(self) -> dict[str, Any]:
        return self._rpc("load")

    def initialize(self, **kwargs: Any) -> dict[str, Any]:
        return self._rpc("initialize", **kwargs)

    def validate_initial_values(self, values: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("validate_initial_values", values)
        return result if isinstance(result, dict) else {}

    def step(self, step_size: float | None = None) -> dict[str, Any]:
        args = () if step_size is None else (step_size,)
        return self._rpc("step", *args)

    def run_until(self, target_time: float, step_size: float | None = None) -> dict[str, Any]:
        if step_size is None:
            return self._rpc("run_until", target_time)
        return self._rpc("run_until", target_time, step_size=step_size)

    def set_inputs(self, values: dict[str, Any]) -> None:
        self._rpc("set_inputs", values)

    def validate_inputs(self, values: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("validate_inputs", values)
        return result if isinstance(result, dict) else {}

    def get_outputs(self, refs: list[int] | None = None) -> dict[str, Any]:
        return self._rpc("get_outputs", refs)

    def _begin_output_read(self, *, blocking: bool) -> tuple[bool, int | None]:
        if not self._output_read_lock.acquire(blocking=blocking):
            return False, None
        min_interval = 1.0 / config.MAX_OUTPUT_SUBSCRIPTION_HZ
        if self._last_output_read_monotonic is not None:
            remaining = min_interval - (time.monotonic() - self._last_output_read_monotonic)
            if remaining > 0:
                self._output_read_lock.release()
                return False, max(1, math.ceil(remaining * 1000))
        return True, None

    def _finish_output_read(self) -> None:
        self._last_output_read_monotonic = time.monotonic()
        self._output_read_lock.release()

    def output_read_wait_seconds(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        min_interval = 1.0 / config.MAX_OUTPUT_SUBSCRIPTION_HZ
        if not self._output_read_lock.acquire(blocking=False):
            return max(0.05, min_interval)
        try:
            if self._last_output_read_monotonic is None:
                return 0.0
            elapsed = now - self._last_output_read_monotonic
            return max(0.0, min_interval - elapsed)
        finally:
            self._output_read_lock.release()

    def _acquire_output_read_or_raise(self) -> None:
        acquired, retry_after_ms = self._begin_output_read(blocking=True)
        if not acquired:
            raise OutputReadRateLimitError(retry_after_ms or 1)

    def get_outputs_limited(self, refs: list[int] | None = None) -> dict[str, Any]:
        self._acquire_output_read_or_raise()
        try:
            return self.get_outputs(refs)
        finally:
            self._finish_output_read()

    def step_with_outputs(self, step_size: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        self._acquire_output_read_or_raise()
        try:
            step_result = self.step(step_size)
            outputs = self.get_outputs()
            return step_result, outputs
        finally:
            self._finish_output_read()

    def run_until_with_outputs(
        self,
        target_time: float,
        step_size: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._acquire_output_read_or_raise()
        try:
            run_result = self.run_until(target_time, step_size=step_size)
            outputs = self.get_outputs()
            return run_result, outputs
        finally:
            self._finish_output_read()

    def pause(self) -> dict[str, Any]:
        return self._rpc("pause")

    def resume(self) -> dict[str, Any]:
        return self._rpc("resume")

    def reset(self, defer_initialization: bool = False) -> dict[str, Any]:
        result = self._rpc("reset", defer_initialization=defer_initialization)
        self._pending_samples.clear()
        self._pending_queue_drops = 0
        if self.subscription is not None:
            self.subscription.last_emit_monotonic = 0.0
            self.subscription.rate_dropped = 0
        return result if isinstance(result, dict) else {}

    def sample_subscription(self) -> dict[str, Any] | None:
        subscription = self.subscription
        if not subscription or not self._initialised or self._terminated:
            return None
        if subscription.seconds_until_due() > 0:
            return None
        acquired, _ = self._begin_output_read(blocking=False)
        if not acquired:
            return None
        try:
            variable_refs = None
            if subscription.variables is not None and self._md:
                names = set(subscription.variables)
                variable_refs = [
                    variable.valueReference for variable in self._md.modelVariables
                    if variable.name in names and variable.causality == "output"
                ]
            sample = self.get_outputs(variable_refs)["outputs"]
        finally:
            self._finish_output_read()
        if self.subscription is not subscription:
            return None
        self._pending_samples.append(sample)
        if len(self._pending_samples) > subscription.max_batch_size:
            excess = len(self._pending_samples) - subscription.max_batch_size
            self._pending_samples = self._pending_samples[excess:]
            subscription.rate_dropped += excess
        now = time.monotonic()
        subscription.last_emit_monotonic = now
        with self._sequence_lock:
            payload = {
                "type": "sim.outputs",
                "sessionId": self.session_id,
                "seq": self.seq,
                "dropped": subscription.rate_dropped + self._pending_queue_drops,
                "batchSize": len(self._pending_samples),
                "simTime": self._time,
                "values": self._pending_samples[-1],
            }
            self.seq += 1
        self._pending_samples.clear()
        subscription.rate_dropped = 0
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
            elif self._job is not None:
                self._job.close()
                self._job = None
            _cleanup_worker_directory(self._worker_directory)
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
        if self._expired_deadline_code(now) is not None:
            return False
        return self._attach_deadline is None or now < self._attach_deadline


def _execute_worker(
    fmu_path: str,
    access_key: str,
    parameters: dict[str, Any],
    options: dict[str, Any],
    output_queue: Any,
    streaming: bool,
    capture_series: bool = False,
    start_gate: Any = None,
    worker_directory: str | None = None,
    probe_only: bool = False,
    resource_policy: dict[str, Any] | None = None,
    expected_parent_pid: int | None = None,
) -> None:
    """Worker entry point; imports native FMU code only after OS limits apply."""
    session = None
    try:
        if expected_parent_pid is not None:
            bind_worker_to_parent(expected_parent_pid)
        if start_gate is not None:
            start_gate.wait()
        apply_worker_limits(resource_policy, worker_directory, fmu_path)
        _sanitize_worker_environment()
        if worker_directory:
            os.chdir(worker_directory)
            os.environ["TMP"] = worker_directory
            os.environ["TEMP"] = worker_directory
            os.environ["TMPDIR"] = worker_directory
            config.TEMP_DIR = Path(worker_directory)

        from .engine import FmuSession

        session = FmuSession(
            f"worker_{mp.current_process().pid}",
            Path(fmu_path),
            access_key=access_key,
        )
        if resource_policy is not None:
            session._max_simulation_steps = min(
                config.MAX_SIMULATION_STEPS,
                int(resource_policy.get("maxSteps", config.MAX_SIMULATION_STEPS)),
            )
        session.load()
        start = float(options.get("startTime", 0.0))
        stop = float(options.get("stopTime", 1.0))
        step = options.get("stepSize")
        step_value = float(step) if step is not None else None
        if probe_only:
            session.initialize(start_time=start, stop_time=stop, step_size=step_value)
            output_queue.put({"kind": "result", "payload": {"type": "fmu.probe", "success": True}})
            return
        session.initialize(
            start_time=start,
            stop_time=stop,
            step_size=step_value,
            parameters=parameters or None,
            early_return_allowed=options.get("earlyReturnAllowed", True),
        )
        requested_outputs = options.get("outputVariables")
        result_size_limit = config.MAX_STORED_RESULT_BYTES
        if resource_policy is not None:
            result_size_limit = min(result_size_limit, int(resource_policy.get("maxResultBytes", result_size_limit)))

        def select_outputs(values: dict[str, Any]) -> dict[str, Any]:
            if requested_outputs is None:
                return values
            return {name: values[name] for name in requested_outputs}

        def result_too_large(payload: Any) -> bool:
            encoded_size = len(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            return encoded_size > result_size_limit

        if streaming:
            result_bytes = 0
            sample_every = max(1, int(options.get("outputSampleEverySteps", 1)))
            step_index = 0
            for snapshot in session.run_until_streaming(stop, step_size=step_value):
                step_index += 1
                is_final = session._finished_reason is not None or snapshot["time"] >= stop
                if step_index % sample_every != 0 and not is_final:
                    continue
                snapshot = {**snapshot, "outputs": select_outputs(snapshot["outputs"])}
                result_bytes += len(json.dumps(snapshot, separators=(",", ":"), allow_nan=False).encode("utf-8"))
                if result_bytes > result_size_limit:
                    output_queue.put({"kind": "error", "code": "RESULT_SIZE_LIMIT_EXCEEDED"})
                    return
                output_queue.put({"kind": "message", "payload": snapshot})
            output_queue.put({
                "kind": "message",
                "payload": {
                    "type": "sim.done",
                    "time": session._time,
                    "finishReason": session._finished_reason,
                },
            })
        elif capture_series:
            initial = session.get_outputs()
            initial_outputs = select_outputs(initial["outputs"])
            time_values = [initial["time"]]
            output_series = {name: [value] for name, value in initial_outputs.items()}
            result_bytes = len(json.dumps(
                {"time": initial["time"], "outputs": initial_outputs},
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8"))
            if result_bytes > result_size_limit:
                output_queue.put({"kind": "error", "code": "RESULT_SIZE_LIMIT_EXCEEDED"})
                return
            sample_every = max(1, int(options.get("outputSampleEverySteps", 1)))
            step_index = 0
            last_sample_time = initial["time"]
            for snapshot in session.run_until_streaming(stop, step_size=step_value):
                step_index += 1
                is_final = session._finished_reason is not None or snapshot["time"] >= stop
                if step_index % sample_every != 0 and not is_final:
                    continue
                snapshot_outputs = select_outputs(snapshot["outputs"])
                sample = {"time": snapshot["time"], "outputs": snapshot_outputs}
                result_bytes += len(json.dumps(sample, separators=(",", ":"), allow_nan=False).encode("utf-8"))
                if result_bytes > result_size_limit:
                    output_queue.put({"kind": "error", "code": "RESULT_SIZE_LIMIT_EXCEEDED"})
                    return
                time_values.append(snapshot["time"])
                last_sample_time = snapshot["time"]
                for name, value in snapshot_outputs.items():
                    output_series.setdefault(name, []).append(value)
            if time_values[-1] != last_sample_time:
                final_outputs = select_outputs(session.get_outputs()["outputs"])
                time_values.append(last_sample_time)
                for name, value in final_outputs.items():
                    output_series.setdefault(name, []).append(value)
            output_queue.put({
                "kind": "result",
                "payload": {
                    "type": "sim.result",
                    "time": time_values,
                    "state": "terminated",
                    "outputs": output_series,
                    "outputVariables": list(output_series),
                    "sampleEverySteps": sample_every,
                    "finishReason": session._finished_reason,
                },
            })
        else:
            result = session.run_until(stop, step_size=step_value)
            outputs = session.get_outputs()
            selected_outputs = select_outputs(outputs.get("outputs", {}))
            payload = {
                "kind": "result",
                "payload": {
                    "type": "sim.result",
                    "time": result["time"],
                    "state": "terminated",
                    "outputs": selected_outputs,
                    "outputVariables": list(selected_outputs),
                    "finishReason": result.get("finishReason"),
                },
            }
            if result_too_large(payload["payload"]):
                output_queue.put({"kind": "error", "code": "RESULT_SIZE_LIMIT_EXCEEDED"})
                return
            output_queue.put(payload)
    except WorkerIsolationError:
        output_queue.put({
            "kind": "error",
            "code": "FMU_WORKER_ISOLATION_UNAVAILABLE",
            "message": "FMU worker isolation could not be applied",
            "retryable": False,
        })
    except Exception:
        output_queue.put({
            "kind": "error",
            "code": "FMU_EXECUTION_FAILED",
            "message": "FMU simulation failed in the isolated worker",
            "retryable": False,
        })
    finally:
        if session is not None:
            session.terminate()


def _new_process(
    fmu_path: Path,
    access_key: str,
    parameters: dict[str, Any],
    options: dict[str, Any],
    output_queue: Any,
    streaming: bool,
    capture_series: bool = False,
    probe_only: bool = False,
    resource_policy: dict[str, Any] | None = None,
) -> tuple[Any, Any, WindowsWorkerJob | None, Path]:
    context = mp.get_context("spawn")
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    worker_directory = Path(tempfile.mkdtemp(prefix="worker_", dir=str(config.TEMP_DIR)))
    if os.name != "nt":
        os.chmod(worker_directory, 0o700)
    try:
        worker_fmu_path = _stage_windows_fmu(fmu_path, worker_directory)
    except Exception:
        _cleanup_worker_directory(worker_directory)
        raise
    start_gate = context.Event()
    worker_args = (
        str(worker_fmu_path), access_key, parameters, options, output_queue,
        streaming, capture_series, start_gate, str(worker_directory),
        probe_only, resource_policy, os.getpid(),
    )
    process = _worker_process(
        context,
        target=_execute_worker,
        args=worker_args,
        name="LabStation-FMU-Worker",
        worker_directory=worker_directory,
    )
    try:
        process.start()
        worker_pid = process.pid
        if worker_pid is None:
            raise RuntimeError("FMU worker did not receive a process ID")
        job = attach_worker_limits(process, resource_policy)
        _mark_worker_directory(worker_directory, worker_pid)
        start_gate.set()
        return process, context, job, worker_directory
    except Exception as exc:
        start_gate.set()
        terminate_worker(process)
        _cleanup_worker_directory(worker_directory)
        code = "FMU_WORKER_ISOLATION_UNAVAILABLE" if isinstance(exc, WorkerIsolationError) else "FMU_WORKER_START_FAILED"
        raise ProcessExecutionError(
            "FMU worker isolation could not be applied" if isinstance(exc, WorkerIsolationError) else "FMU worker could not be started",
            code=code,
            retryable=not isinstance(exc, WorkerIsolationError),
        ) from exc


def run(
    fmu_path: Path,
    *,
    access_key: str,
    parameters: dict[str, Any],
    options: dict[str, Any],
    cancel_event: threading.Event | None = None,
    capture_series: bool = False,
    authorization_expires_at: float | None = None,
    reservation_ends_at: float | None = None,
    probe_only: bool = False,
    resource_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a one-shot simulation in a spawned process."""
    context = mp.get_context("spawn")
    output_queue = context.Queue(maxsize=1)
    try:
        process, _, job, worker_directory = _new_process(
            fmu_path, access_key, parameters, options, output_queue,
            False, capture_series, probe_only, resource_policy,
        )
    except Exception:
        output_queue.close()
        output_queue.join_thread()
        raise
    deadline = time.monotonic() + config.execution_timeout_seconds()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                terminate_worker(process, job)
                job = None
                raise ProcessExecutionError(
                    "FMU simulation was cancelled",
                    code="FMU_EXECUTION_CANCELLED",
                    retryable=False,
                )
            if authorization_expires_at is not None and time.time() >= authorization_expires_at:
                terminate_worker(process, job)
                job = None
                raise ProcessExecutionError(
                    "Execution authorization expired while the FMU worker was running",
                    code="EXECUTION_AUTHORIZATION_EXPIRED",
                    retryable=False,
                )
            if reservation_ends_at is not None and time.time() >= reservation_ends_at:
                terminate_worker(process, job)
                job = None
                raise ProcessExecutionError(
                    "Reservation ended while the FMU worker was running",
                    code="RESERVATION_ENDED",
                    retryable=False,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_worker(process, job)
                job = None
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
        if process.is_alive():
            terminate_worker(process, job)
            job = None
        elif job is not None:
            job.close()
        output_queue.close()
        output_queue.join_thread()
        _cleanup_worker_directory(worker_directory)


def stream(
    fmu_path: Path,
    *,
    access_key: str,
    parameters: dict[str, Any],
    options: dict[str, Any],
    cancel_event: threading.Event | None = None,
    authorization_expires_at: float | None = None,
    reservation_ends_at: float | None = None,
    resource_policy: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield NDJSON payloads from an isolated streaming worker."""
    context = mp.get_context("spawn")
    output_queue = context.Queue(maxsize=64)
    try:
        process, _, job, worker_directory = _new_process(
            fmu_path, access_key, parameters, options, output_queue, True,
            resource_policy=resource_policy,
        )
    except Exception:
        output_queue.close()
        output_queue.join_thread()
        raise
    deadline = time.monotonic() + config.execution_timeout_seconds()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                terminate_worker(process, job)
                job = None
                yield {
                    "type": "error",
                    "code": "FMU_EXECUTION_CANCELLED",
                    "message": "FMU simulation was cancelled",
                    "retryable": False,
                }
                break
            if authorization_expires_at is not None and time.time() >= authorization_expires_at:
                terminate_worker(process, job)
                job = None
                yield {
                    "type": "error",
                    "code": "EXECUTION_AUTHORIZATION_EXPIRED",
                    "message": "Execution authorization expired while the FMU worker was running",
                    "retryable": False,
                }
                break
            if reservation_ends_at is not None and time.time() >= reservation_ends_at:
                terminate_worker(process, job)
                job = None
                yield {
                    "type": "error",
                    "code": "RESERVATION_ENDED",
                    "message": "Reservation ended while the FMU worker was running",
                    "retryable": False,
                }
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_worker(process, job)
                job = None
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
            terminate_worker(process, job)
            job = None
        elif job is not None:
            job.close()
        output_queue.close()
        output_queue.join_thread()
        _cleanup_worker_directory(worker_directory)
