"""Process boundary for one-shot FMU executions.

Native FMU binaries are third-party code. Keeping batch execution in a
``spawn`` child means a segmentation fault or an unbounded native call does
not take the Station HTTP/WebSocket control plane down with it.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import time
from pathlib import Path
from typing import Any, Iterator

from . import config


class ProcessExecutionError(RuntimeError):
    """A child-process execution failed or exceeded its deadline."""

    def __init__(self, message: str, *, code: str = "FMU_EXECUTION_FAILED", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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
            for snapshot in session.run_until_streaming(stop, step_size=step_value):
                output_queue.put({"kind": "message", "payload": snapshot})
            output_queue.put({
                "kind": "message",
                "payload": {"type": "sim.done", "time": session._time},
            })
        else:
            result = session.run_until(stop, step_size=step_value)
            outputs = session.get_outputs()
            output_queue.put({
                "kind": "result",
                "payload": {
                    "type": "sim.result",
                    "time": result["time"],
                    "state": "terminated",
                    "outputs": outputs.get("outputs", {}),
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
) -> dict[str, Any]:
    """Run a one-shot simulation in a spawned process."""
    context = mp.get_context("spawn")
    output_queue = context.Queue(maxsize=1)
    process, _ = _new_process(fmu_path, access_key, parameters, options, output_queue, False)
    deadline = time.monotonic() + config.execution_timeout_seconds()
    try:
        while True:
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
) -> Iterator[dict[str, Any]]:
    """Yield NDJSON payloads from an isolated streaming worker."""
    context = mp.get_context("spawn")
    output_queue = context.Queue(maxsize=64)
    process, _ = _new_process(fmu_path, access_key, parameters, options, output_queue, True)
    deadline = time.monotonic() + config.execution_timeout_seconds()
    try:
        while True:
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
