"""Process boundary for one-shot FMU executions.

Native FMU binaries are third-party code. Keeping batch execution in a
``spawn`` child means a segmentation fault or an unbounded native call does
not take the Station HTTP/WebSocket control plane down with it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
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

from . import config, engine
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
from .simulation_defaults import DEFAULT_STEP_SIZE, resolve_simulation_time_options

if TYPE_CHECKING:
    from .engine import OutputSubscription

logger = logging.getLogger(__name__)


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
    "advance",
    "validate_inputs",
    "validate_input_schedule",
    "set_inputs",
    "get_outputs",
    "pause",
    "resume",
    "reset",
    "capture_fmu_state",
    "restore_fmu_state",
}

_SECRET_ENV_MARKERS = (
    "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY", "API_KEY",
    "AWS_", "AZURE_", "GOOGLE_APPLICATION_CREDENTIALS",
)
_NATIVE_THREAD_ENVIRONMENT = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
)
_WORKER_ENVIRONMENT_LOCK = threading.Lock()
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


def _sanitize_worker_environment(access_key: str) -> None:
    for name in list(os.environ):
        upper_name = name.upper()
        if any(marker in upper_name for marker in _SECRET_ENV_MARKERS):
            os.environ.pop(name, None)
    if access_key in config.NATIVE_THREAD_LIMIT_ACCESS_KEYS:
        for name in _NATIVE_THREAD_ENVIRONMENT:
            os.environ[name] = "1"


def _start_worker_process(process: Any, access_key: str) -> None:
    """Pass per-model native thread limits to a spawned worker before Python imports its target."""
    if access_key not in config.NATIVE_THREAD_LIMIT_ACCESS_KEYS:
        process.start()
        return

    # multiprocessing inherits the parent environment at process creation.
    # Serialize these short snapshots and restore the API process immediately.
    with _WORKER_ENVIRONMENT_LOCK:
        previous = {name: os.environ.get(name) for name in _NATIVE_THREAD_ENVIRONMENT}
        try:
            for name in _NATIVE_THREAD_ENVIRONMENT:
                os.environ[name] = "1"
            process.start()
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


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


def _realtime_snapshot(session: Any, *, include_model: bool = True) -> dict[str, Any]:
    model = getattr(session, "_md", None)
    default_experiment = getattr(model, "defaultExperiment", None)
    variables = []
    for variable in (getattr(model, "modelVariables", ()) or ()) if include_model else ():
        variables.append({
            "name": str(getattr(variable, "name", "")),
            "valueReference": int(getattr(variable, "valueReference", 0)),
            "type": str(getattr(variable, "type", "")),
            "causality": str(getattr(variable, "causality", "")),
        })
    snapshot = {
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
    }
    if include_model:
        snapshot.update({
            "modelVariables": variables,
            "defaultExperiment": {
                "stepSize": getattr(default_experiment, "stepSize", None),
            } if default_experiment is not None else None,
        })
    return snapshot


def _realtime_session_worker(
    connection: Any,
    session_id: str,
    fmu_path: Path,
    access_key: str,
    fmu_sha256: str | None = None,
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
        apply_worker_limits(resource_policy, worker_directory, str(fmu_path), access_key)
        _sanitize_worker_environment(access_key)
        if worker_directory:
            os.chdir(worker_directory)
            os.environ["TMP"] = worker_directory
            os.environ["TEMP"] = worker_directory
            os.environ["TMPDIR"] = worker_directory
            config.TEMP_DIR = Path(worker_directory)
        if session_factory is None:
            from .engine import FmuSession
            session_factory = FmuSession
        session = session_factory(
            session_id,
            fmu_path,
            access_key=access_key,
            fmu_sha256=fmu_sha256,
        )
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
                    "snapshot": _realtime_snapshot(session, include_model=method == "load"),
                })
                continue
            if method == "terminate":
                session.terminate()
                connection.send({
                    "requestId": request_id,
                    "ok": True,
                    "result": None,
                    "snapshot": _realtime_snapshot(session, include_model=method == "load"),
                })
                return
            if method not in _REALTIME_METHODS:
                connection.send({
                    "requestId": request_id,
                    "ok": False,
                    "error": "FMU worker command is not allowlisted",
                    "snapshot": _realtime_snapshot(session, include_model=method == "load"),
                })
                continue
            try:
                result = getattr(session, method)(*args, **kwargs)
                connection.send({
                    "requestId": request_id,
                    "ok": True,
                    "result": result,
                    "snapshot": _realtime_snapshot(session, include_model=method == "load"),
                })
            except Exception as exc:
                failure = {
                    "requestId": request_id,
                    "ok": False,
                    "error": str(exc) or "FMU worker command failed",
                    "errorType": type(exc).__name__,
                    "snapshot": _realtime_snapshot(session, include_model=method == "load"),
                }
                applied_events = getattr(exc, "input_events_applied", None)
                if type(applied_events) is int and applied_events >= 0:
                    failure["inputEventsApplied"] = applied_events
                connection.send(failure)
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
        fmu_sha256: str | None = None,
        resource_policy: dict[str, Any] | None = None,
        _worker_session_factory: Any = None,
    ):
        self.session_id = session_id
        self.fmu_path = Path(fmu_path)
        self.access_key = access_key or self.fmu_path.name
        self.expires_at = expires_at
        self.gateway_context = dict(gateway_context or {})
        self.fmu_sha256 = fmu_sha256
        self._state = "new"
        self._time = 0.0
        self._start_time = 0.0
        self._stop_time = 1.0
        self._step_size = DEFAULT_STEP_SIZE
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
                self.fmu_sha256, _worker_session_factory, start_gate, str(self._worker_directory), resource_policy,
                os.getpid(),
            ),
            name="LabStation-FMU-Realtime",
            worker_directory=self._worker_directory,
        )
        try:
            _start_worker_process(self._process, self.access_key)
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
        variables = snapshot.get("modelVariables")
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
                    error = str(response.get("error") or "FMU realtime command failed")
                    if method == "advance" and response.get("errorType") in {
                        "AdvancePreExecutionError",
                        "AdvanceExecutionError",
                    }:
                        applied_events = response.get("inputEventsApplied", 0)
                        if type(applied_events) is int and applied_events >= 0:
                            error_type = response.get("errorType")
                            if error_type == "AdvancePreExecutionError":
                                raise engine.AdvancePreExecutionError(error, applied_events)
                            raise engine.AdvanceExecutionError(error, applied_events)
                    raise RuntimeError(error)
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
        if not hasattr(self, "_description"):
            self._description = self._rpc("load")
        return self._description

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

    def advance(
        self,
        target_time: float,
        max_samples: int = 256,
        step_size: float | None = None,
        input_schedule: list[dict[str, Any]] | None = None,
        output_sample_every_steps: int = 1,
        output_refs: list[int] | None = None,
        max_compute_ms: float | None = None,
    ) -> dict[str, Any]:
        self._acquire_output_read_or_raise()
        try:
            return self._rpc(
                "advance",
                target_time,
                max_samples=max_samples,
                step_size=step_size,
                input_schedule=input_schedule,
                output_sample_every_steps=output_sample_every_steps,
                output_refs=output_refs,
                max_compute_ms=max_compute_ms,
            )
        finally:
            self._finish_output_read()

    def set_inputs(self, values: dict[str, Any]) -> None:
        self._rpc("set_inputs", values)

    def validate_inputs(self, values: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc("validate_inputs", values)
        return result if isinstance(result, dict) else {}

    def validate_input_schedule(self, input_schedule: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = self._rpc("validate_input_schedule", input_schedule)
        return result if isinstance(result, list) else []

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
    input_schedule: list[dict[str, Any]] | None = None,
    fmu_sha256: str | None = None,
    checkpoint_snapshot: dict[str, Any] | None = None,
    checkpoint_state_path: str | None = None,
    capture_fmu_state: bool = False,
    capture_state_output_path: str | None = None,
) -> None:
    """Worker entry point; imports native FMU code only after OS limits apply."""
    session = None
    try:
        if expected_parent_pid is not None:
            bind_worker_to_parent(expected_parent_pid)
        if start_gate is not None:
            start_gate.wait()
        apply_worker_limits(resource_policy, worker_directory, fmu_path, access_key)
        _sanitize_worker_environment(access_key)
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
            fmu_sha256=fmu_sha256,
        )
        if resource_policy is not None:
            session._max_simulation_steps = min(
                config.MAX_SIMULATION_STEPS,
                int(resource_policy.get("maxSteps", config.MAX_SIMULATION_STEPS)),
            )
        model_metadata = session.load()
        time_options = resolve_simulation_time_options(model_metadata, options)
        start = float(time_options["startTime"])
        stop = float(time_options["stopTime"])
        step_value = float(time_options["stepSize"])
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
        if checkpoint_snapshot is not None:
            if not checkpoint_state_path:
                raise ValueError("FMU state checkpoint data is unavailable")
            checkpoint_bytes = Path(checkpoint_state_path).read_bytes()
            session.restore_fmu_state({
                **checkpoint_snapshot,
                "serializedState": base64.b64encode(checkpoint_bytes).decode("ascii"),
            })
        scheduled_inputs = list(input_schedule or [])
        while scheduled_inputs and float(scheduled_inputs[0]["time"]) <= start + 1e-12:
            session.set_inputs(scheduled_inputs.pop(0)["values"])
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
            for snapshot in session.run_until_streaming(
                stop,
                step_size=step_value,
                input_schedule=scheduled_inputs,
            ):
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
            completed = {
                "type": "sim.done",
                "time": session._time,
                "finishReason": session._finished_reason,
            }
            if capture_fmu_state:
                if not capture_state_output_path:
                    raise RuntimeError("FMU state checkpoint output is unavailable")
                from . import fmu_state

                checkpoint = session.capture_fmu_state()
                serialized_state = base64.b64decode(checkpoint.pop("serializedState"), validate=True)
                if len(serialized_state) > fmu_state.MAX_FMU_STATE_BYTES:
                    raise ValueError("FMU state checkpoint exceeds the size limit")
                checkpoint_path = Path(capture_state_output_path)
                with checkpoint_path.open("xb") as checkpoint_file:
                    checkpoint_file.write(serialized_state)
                if os.name != "nt":
                    checkpoint_path.chmod(0o600)
                completed["checkpointSnapshot"] = checkpoint
            output_queue.put({"kind": "message", "payload": completed})
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
            for snapshot in session.run_until_streaming(
                stop,
                step_size=step_value,
                input_schedule=scheduled_inputs,
            ):
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
            if scheduled_inputs:
                for _snapshot in session.run_until_streaming(
                    stop,
                    step_size=step_value,
                    input_schedule=scheduled_inputs,
                ):
                    pass
                result = {
                    "time": session._time,
                    "finishReason": session._finished_reason,
                }
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
    except Exception as exc:
        logger.exception(
            "Isolated FMU worker failed (accessKey=%s, probe=%s, errorType=%s)",
            access_key,
            probe_only,
            type(exc).__name__,
        )
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
    input_schedule: list[dict[str, Any]] | None = None,
    fmu_sha256: str | None = None,
    checkpoint_snapshot: dict[str, Any] | None = None,
    capture_fmu_state: bool = False,
) -> tuple[Any, Any, WindowsWorkerJob | None, Path, Any]:
    context = mp.get_context("spawn")
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    worker_directory = Path(tempfile.mkdtemp(prefix="worker_", dir=str(config.TEMP_DIR)))
    if os.name != "nt":
        os.chmod(worker_directory, 0o700)
    checkpoint_state_path = None
    capture_state_output_path = None
    worker_checkpoint = None
    try:
        worker_fmu_path = _stage_windows_fmu(fmu_path, worker_directory)
        if checkpoint_snapshot is not None:
            encoded_state = checkpoint_snapshot.get("serializedState")
            if not isinstance(encoded_state, str):
                raise ValueError("FMU state checkpoint is invalid")
            state_bytes = base64.b64decode(encoded_state, validate=True)
            if not state_bytes or len(state_bytes) > 32 * 1024 * 1024:
                raise ValueError("FMU state checkpoint exceeds the size limit")
            checkpoint_path = worker_directory / "fmu-state-input.bin"
            with checkpoint_path.open("xb") as checkpoint_file:
                checkpoint_file.write(state_bytes)
            if os.name != "nt":
                checkpoint_path.chmod(0o600)
            checkpoint_state_path = str(checkpoint_path)
            worker_checkpoint = {
                key: value for key, value in checkpoint_snapshot.items()
                if key != "serializedState"
            }
        if capture_fmu_state:
            capture_state_output_path = str(worker_directory / "fmu-state-output.bin")
    except Exception:
        _cleanup_worker_directory(worker_directory)
        raise
    start_gate = context.Event()
    worker_args = (
        str(worker_fmu_path), access_key, parameters, options, output_queue,
        streaming, capture_series, start_gate, str(worker_directory),
        probe_only, resource_policy, os.getpid(), input_schedule, fmu_sha256,
        worker_checkpoint, checkpoint_state_path, capture_fmu_state,
        capture_state_output_path,
    )
    process = _worker_process(
        context,
        target=_execute_worker,
        args=worker_args,
        name="LabStation-FMU-Worker",
        worker_directory=worker_directory,
    )
    try:
        _start_worker_process(process, access_key)
        worker_pid = process.pid
        if worker_pid is None:
            raise RuntimeError("FMU worker did not receive a process ID")
        job = attach_worker_limits(process, resource_policy)
        _mark_worker_directory(worker_directory, worker_pid)
        start_gate.set()
        # Keep the event alive until the caller has received a worker result.
        # Process.start() drops its copy of _args immediately, while a spawned
        # child may still be unpickling the semaphore-backed event. Returning a
        # strong reference prevents its parent-side finalizer from unlinking
        # the semaphore before the child can rebuild it.
        return process, context, job, worker_directory, start_gate
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
    input_schedule: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run a one-shot simulation in a spawned process."""
    context = mp.get_context("spawn")
    output_queue = context.Queue(maxsize=1)
    try:
        process, _, job, worker_directory, _start_gate = _new_process(
            fmu_path, access_key, parameters, options, output_queue,
            False, capture_series, probe_only, resource_policy, input_schedule,
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
    input_schedule: list[dict[str, Any]] | None = None,
    fmu_sha256: str | None = None,
    gateway_context: dict[str, Any] | None = None,
    checkpoint_snapshot: dict[str, Any] | None = None,
    capture_fmu_state: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield NDJSON payloads from an isolated streaming worker."""
    context = mp.get_context("spawn")
    output_queue = context.Queue(maxsize=64)
    try:
        process, _, job, worker_directory, _start_gate = _new_process(
            fmu_path, access_key, parameters, options, output_queue, True,
            resource_policy=resource_policy,
            input_schedule=input_schedule,
            fmu_sha256=fmu_sha256,
            checkpoint_snapshot=checkpoint_snapshot,
            capture_fmu_state=capture_fmu_state,
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
                    if payload.get("type") == "sim.done" and capture_fmu_state:
                        from . import fmu_state

                        checkpoint_metadata = payload.pop("checkpointSnapshot", None)
                        checkpoint_path = worker_directory / "fmu-state-output.bin"
                        if not isinstance(checkpoint_metadata, dict) or not checkpoint_path.is_file():
                            yield {
                                "type": "error",
                                "code": "FMU_STATE_CAPTURE_FAILED",
                                "message": "The FMU state checkpoint could not be captured",
                                "retryable": False,
                            }
                            break
                        try:
                            serialized_state = checkpoint_path.read_bytes()
                            payload["checkpoint"] = fmu_state.seal_fmu_state_checkpoint(
                                {
                                    **checkpoint_metadata,
                                    "serializedState": base64.b64encode(serialized_state).decode("ascii"),
                                },
                                gateway_context=gateway_context,
                                signing_secret=config.internal_token(),
                                state_dir=config.STATE_DIR,
                            )
                        except (OSError, ValueError) as exc:
                            logger.warning("FMU state checkpoint could not be stored: %s", type(exc).__name__)
                            yield {
                                "type": "error",
                                "code": "FMU_STATE_CAPTURE_FAILED",
                                "message": "The FMU state checkpoint could not be stored",
                                "retryable": False,
                            }
                            break
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
