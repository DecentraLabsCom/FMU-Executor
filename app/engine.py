"""FMU execution engine – wraps FMPy for Co-Simulation execution."""

from __future__ import annotations

import base64
import logging
from functools import reduce
from operator import mul
import shutil
import tempfile
import threading
import time as _time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Optional

from fmpy import (
    extract as fmpy_extract,
    instantiate_fmu as fmpy_instantiate_fmu,
    read_model_description,
)

from . import config

logger = logging.getLogger(__name__)


# ── subscription model ───────────────────────────────────────────

@dataclass
class OutputSubscription:
    """Tracks a sim.subscribeOutputs request."""
    variables: Optional[list[str]] = None
    period_ms: int = 100
    max_batch_size: int = 64
    max_hz: Optional[float] = None
    last_emit_monotonic: float = 0.0
    rate_dropped: int = 0

    def min_interval_seconds(self) -> float:
        period_interval = max(1, self.period_ms) / 1000.0
        hz_interval = 0.0
        if self.max_hz is not None and self.max_hz > 0:
            hz_interval = 1.0 / self.max_hz
        return max(period_interval, hz_interval)


class FmuSession:
    """Manage one loaded FMI 2/FMI 3 Co-Simulation session."""

    def __init__(
        self,
        session_id: str,
        fmu_path: Path,
        *,
        access_key: str | None = None,
        expires_at: float | int | str | None = None,
        gateway_context: dict[str, Any] | None = None,
    ):
        self.session_id = session_id
        self.fmu_path = fmu_path
        self.access_key = access_key or fmu_path.name
        self.expires_at = expires_at
        self.gateway_context = dict(gateway_context or {})
        self._extract_dir: Path | None = None
        self._slave: Any | None = None
        self._md = None
        self._time: float = 0.0
        self._step_size: float = 0.001
        self._start_time: float = 0.0
        self._stop_time: float = 1.0
        self._parameters: dict[str, Any] = {}
        self._state: str = "new"
        self._initialised: bool = False
        self._terminated: bool = False
        self._attached: bool = False
        self._attach_deadline: float | None = None
        self._attachment_owner: object | None = None
        # Subscription state
        self.subscription: OutputSubscription | None = None
        self.seq: int = 0
        self._pending_samples: list[dict[str, Any]] = []
        self._pending_queue_drops: int = 0

    # ── lifecycle ────────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        """Extract and read model description. Returns describe dict."""
        if self._md is not None:
            return self._describe()
        self._extract_dir = Path(tempfile.mkdtemp(
            prefix=f"fmu_{self.session_id}_",
            dir=str(config.TEMP_DIR),
        ))
        fmpy_extract(str(self.fmu_path), unzipdir=str(self._extract_dir))
        self._md = read_model_description(str(self.fmu_path))
        return self._describe()

    def initialize(
        self,
        start_time: float = 0.0,
        stop_time: float = 1.0,
        step_size: float | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._initialised:
            raise RuntimeError("Session already initialised")
        if self._md is None:
            raise RuntimeError("FMU not loaded – call load() first")
        if self._md.coSimulation is None:
            raise RuntimeError("FMU does not support Co-Simulation")

        self._step_size = step_size if step_size is not None else (
            float(self._md.defaultExperiment.stepSize)
            if self._md.defaultExperiment and self._md.defaultExperiment.stepSize
            else 0.001
        )
        self._start_time = float(start_time)
        self._stop_time = float(stop_time)
        if self._stop_time <= self._start_time:
            raise ValueError("stopTime must be greater than startTime")
        if self._step_size <= 0:
            raise ValueError("stepSize must be positive")
        self._time = self._start_time
        self._parameters = dict(parameters or {})

        slave = self._instantiate()
        if slave is None:
            raise RuntimeError("FMU instantiation returned no slave")
        self._slave = slave
        if self._fmi_major() == "3":
            slave.enterInitializationMode(
                startTime=self._start_time,
                stopTime=self._stop_time,
            )
        else:
            slave.setupExperiment(
                startTime=self._start_time,
                stopTime=self._stop_time,
            )
            slave.enterInitializationMode()

        if self._parameters:
            self._apply_parameters(self._parameters)

        slave.exitInitializationMode()
        self._initialised = True
        self._terminated = False
        self._state = "initialized"

        return {"sessionId": self.session_id, "time": self._time, "state": "initialized"}

    def step(self, step_size: float | None = None) -> dict[str, Any]:
        slave = self._require_slave()
        if self._state == "paused":
            raise RuntimeError("Session is paused")
        h = step_size or self._step_size
        if h <= 0:
            raise ValueError("stepSize must be positive")
        slave.doStep(currentCommunicationPoint=self._time, communicationStepSize=h)
        self._time += h
        self._state = "running"
        return {"time": self._time, "state": "running"}

    def run_until(self, target_time: float, step_size: float | None = None) -> dict[str, Any]:
        slave = self._require_slave()
        if target_time < self._time:
            raise ValueError("targetTime must not be earlier than current simulation time")
        if self._state == "paused":
            raise RuntimeError("Session is paused")
        h = step_size or self._step_size
        if h <= 0:
            raise ValueError("stepSize must be positive")
        self._state = "running"
        while self._time < target_time - 1e-12:
            remaining = target_time - self._time
            actual_h = min(h, remaining)
            slave.doStep(currentCommunicationPoint=self._time, communicationStepSize=actual_h)
            self._time += actual_h
        return {"time": self._time, "state": "running"}

    def run_until_streaming(
        self,
        target_time: float,
        step_size: float | None = None,
        output_refs: list[int] | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Step until *target_time*, yielding output snapshots at each step."""
        slave = self._require_slave()
        if target_time < self._time:
            raise ValueError("targetTime must not be earlier than current simulation time")
        if self._state == "paused":
            raise RuntimeError("Session is paused")
        h = step_size or self._step_size
        if h <= 0:
            raise ValueError("stepSize must be positive")
        self._state = "running"
        seq = 0
        while self._time < target_time - 1e-12:
            remaining = target_time - self._time
            actual_h = min(h, remaining)
            slave.doStep(currentCommunicationPoint=self._time, communicationStepSize=actual_h)
            self._time += actual_h
            outputs = self._read_outputs(output_refs)
            yield {"type": "sim.step", "seq": seq, "time": self._time, "outputs": outputs}
            seq += 1

    def set_inputs(self, values: dict[str, Any]) -> None:
        self._ensure_live()
        self._apply_parameters(values)
        self._parameters.update(values)

    def get_outputs(self, refs: list[int] | None = None) -> dict[str, Any]:
        self._ensure_live()
        return {"time": self._time, "outputs": self._read_outputs(refs)}

    def pause(self) -> dict[str, Any]:
        """Pause automatic execution without destroying FMU state."""
        self._ensure_live()
        if self._state == "running":
            self._state = "paused"
        return {"time": self._time, "state": self._state}

    def resume(self) -> dict[str, Any]:
        """Mark the session runnable; the websocket loop performs stepping."""
        self._ensure_live()
        if self._state not in ("paused", "initialized"):
            raise RuntimeError(f"Cannot resume from state {self._state}")
        self._state = "running"
        return {"time": self._time, "state": self._state}

    def reset(self) -> dict[str, Any]:
        """Reset the FMU to its original initialization options."""
        self._ensure_live()
        self._release_fmu()
        return self.initialize(
            start_time=self._start_time,
            stop_time=self._stop_time,
            step_size=self._step_size,
            parameters=self._parameters,
        )

    @property
    def state(self) -> str:
        if self._terminated:
            return "terminated"
        if self._state == "new":
            return "loaded" if self._md is not None else "new"
        return self._state

    def sample_subscription(self) -> dict[str, Any] | None:
        """Collect a subscription sample.  Returns an output-event dict ready
        to send, or ``None`` if the subscription rate-limit has not elapsed."""
        if not self.subscription:
            return None

        # Resolve variable name filter → valueReference filter
        var_refs: list[int] | None = None
        if self.subscription.variables is not None and self._md:
            name_set = set(self.subscription.variables)
            var_refs = [
                v.valueReference
                for v in self._md.modelVariables
                if v.name in name_set and v.causality == "output"
            ]

        sample = self._read_outputs(var_refs)
        self._pending_samples.append(sample)

        # Enforce max batch size
        if len(self._pending_samples) > self.subscription.max_batch_size:
            excess = len(self._pending_samples) - self.subscription.max_batch_size
            self._pending_samples = self._pending_samples[excess:]
            self.subscription.rate_dropped += excess

        now = _time.monotonic()
        min_interval = self.subscription.min_interval_seconds()
        if (now - self.subscription.last_emit_monotonic) < min_interval:
            self.subscription.rate_dropped += 1
            return None

        self.subscription.last_emit_monotonic = now
        values = self._pending_samples[-1]
        batch_size = len(self._pending_samples)
        self._pending_samples.clear()
        dropped = self.subscription.rate_dropped + self._pending_queue_drops
        self.subscription.rate_dropped = 0
        self._pending_queue_drops = 0

        payload = {
            "type": "sim.outputs",
            "sessionId": self.session_id,
            "seq": self.seq,
            "dropped": dropped,
            "batchSize": batch_size,
            "simTime": self._time,
            "values": values,
        }
        self.seq += 1
        return payload

    def terminate(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        self._attached = False
        self._attach_deadline = None
        self._attachment_owner = None
        self._release_fmu()
        self._cleanup_temp()

    def mark_detached(self, grace_seconds: float, attachment_owner: object | None = None) -> None:
        # A previous Station websocket can finish its cleanup after a newer
        # websocket has already attached the same FMU session. In that case,
        # the stale cleanup must not detach the newer connection.
        if attachment_owner is not None and self._attachment_owner is not attachment_owner:
            return
        if not self._terminated:
            self._attached = False
            self._attach_deadline = _time.time() + max(0.0, float(grace_seconds))
            self._attachment_owner = None

    def mark_attached(self, attachment_owner: object | None = None) -> None:
        self._attached = True
        self._attach_deadline = None
        self._attachment_owner = attachment_owner

    def can_attach(self, now: float | None = None) -> bool:
        if self._terminated:
            return False
        if not self._attached and self._attach_deadline is None:
            return False
        now = _time.time() if now is None else now
        if self.expires_at is not None:
            try:
                if float(self.expires_at) <= now:
                    return False
            except (TypeError, ValueError):
                return False
        return self._attach_deadline is None or now < self._attach_deadline

    # ── private ──────────────────────────────────────────────────

    def _ensure_live(self) -> None:
        if self._terminated:
            raise RuntimeError("Session already terminated")
        if not self._initialised:
            raise RuntimeError("Session not initialised")

    def _require_slave(self) -> Any:
        self._ensure_live()
        if self._slave is None:
            raise RuntimeError("FMU slave is not initialized")
        return self._slave

    def _fmi_major(self) -> str:
        return str(getattr(self._md, "fmiVersion", "2.0")).split(".", 1)[0]

    def _instantiate(self) -> Any:
        """Instantiate through FMPy's FMI 2/FMI 3 selector."""
        if self._extract_dir is None or self._md is None:
            raise RuntimeError("FMU is not loaded")
        # instantiate_fmu() already calls instantiate() internally.
        return fmpy_instantiate_fmu(
            str(self._extract_dir),
            self._md,
            fmi_type="CoSimulation",
        )

    def _describe(self) -> dict[str, Any]:
        from . import fmu_storage
        # Re-use the same normalised describe logic
        return fmu_storage.describe(self.access_key)

    def _apply_parameters(self, params: dict[str, Any]) -> None:
        """Set variable values by name."""
        if not self._md or not self._slave:
            return
        var_map = {v.name: v for v in self._md.modelVariables}
        for name, value in params.items():
            var = var_map.get(name)
            if var is None:
                logger.warning("Unknown variable %r – skipped", name)
                continue
            vtype = self._normalise_type(var)
            values = self._coerce_values(var, value)
            setter = getattr(self._slave, f"set{vtype}", None)
            if setter is None:
                raise ValueError(f"FMU variable type {vtype} is not supported")
            setter([int(var.valueReference)], values)

    def _read_outputs(self, refs: list[int] | None = None) -> dict[str, Any]:
        """Read output variables. If *refs* is None, read all outputs."""
        if not self._md or not self._slave:
            return {}
        outputs: dict[str, Any] = {}
        for var in self._md.modelVariables:
            if var.causality != "output":
                continue
            if refs is not None and var.valueReference not in refs:
                continue
            vr = [int(var.valueReference)]
            vtype = self._normalise_type(var)
            try:
                getter = getattr(self._slave, f"get{vtype}", None)
                if getter is None:
                    continue
                size = self._variable_size(var)
                values = getter(vr) if size == 1 else getter(vr, nValues=size)
                normalised = [self._normalise_output(vtype, value) for value in list(values)]
                outputs[var.name] = normalised[0] if size == 1 else normalised
            except Exception:
                logger.debug("Could not read %s (vr=%d)", var.name, var.valueReference)
        return outputs

    @staticmethod
    def _normalise_type(var: Any) -> str:
        variable_type = str(getattr(var, "type", ""))
        return "Integer" if variable_type == "Enumeration" else variable_type

    @staticmethod
    def _variable_size(var: Any) -> int:
        shape = getattr(var, "shape", None)
        if isinstance(shape, (list, tuple)) and shape:
            return int(reduce(mul, (int(extent) for extent in shape), 1))
        dimensions = getattr(var, "dimensions", None)
        if isinstance(dimensions, (list, tuple)) and dimensions:
            extents: list[int] = []
            for dimension in dimensions:
                start = getattr(dimension, "start", None)
                if start is None:
                    return 1
                extents.append(int(start))
            return int(reduce(mul, extents, 1))
        return 1

    @classmethod
    def _coerce_values(cls, var: Any, value: Any) -> list[Any]:
        vtype = cls._normalise_type(var)
        size = cls._variable_size(var)
        raw_values = value if size > 1 else [value]
        if size > 1 and (not isinstance(value, (list, tuple)) or len(value) != size):
            raise ValueError(f"Array input '{var.name}' expects {size} values")
        if vtype in ("Real", "Float32", "Float64"):
            return [float(item) for item in raw_values]
        if vtype in ("Integer", "Int8", "UInt8", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"):
            return [int(item) for item in raw_values]
        if vtype in ("Boolean", "Clock"):
            return [bool(item) for item in raw_values]
        if vtype == "String":
            return [item.decode("utf-8") if isinstance(item, bytes) else str(item) for item in raw_values]
        if vtype == "Binary":
            try:
                return [item if isinstance(item, (bytes, bytearray)) else base64.b64decode(str(item), validate=True) for item in raw_values]
            except Exception as exc:
                raise ValueError("Binary inputs must be valid base64 strings") from exc
        raise ValueError(f"FMU variable type {vtype} is not supported")

    @staticmethod
    def _normalise_output(vtype: str, value: Any) -> Any:
        if vtype in ("Real", "Float32", "Float64"):
            return float(value)
        if vtype in ("Int64", "UInt64"):
            return str(int(value))
        if vtype in ("Integer", "Int8", "UInt8", "Int16", "UInt16", "Int32", "UInt32"):
            return int(value)
        if vtype in ("Boolean", "Clock"):
            return bool(value)
        if vtype == "String":
            return value.decode("utf-8") if isinstance(value, bytes) else str(value)
        if vtype == "Binary":
            return base64.b64encode(bytes(value)).decode("ascii")
        return value

    def _release_fmu(self) -> None:
        if self._slave is not None:
            try:
                self._slave.terminate()
            except Exception:
                logger.debug("FMU terminate failed for session %s", self.session_id, exc_info=True)
            try:
                self._slave.freeInstance()
            except Exception:
                logger.debug("FMU freeInstance failed for session %s", self.session_id, exc_info=True)
        self._slave = None
        self._initialised = False

    def _cleanup_temp(self) -> None:
        extract_dir = getattr(self, "_extract_dir", None)
        if extract_dir and extract_dir.exists():
            try:
                shutil.rmtree(extract_dir, ignore_errors=True)
            except Exception:
                logger.debug(
                    "Could not remove FMU temporary directory %s",
                    extract_dir,
                    exc_info=True,
                )

    def __del__(self) -> None:
        self.terminate()


# ── session registry ─────────────────────────────────────────────

_sessions: dict[str, Any] = {}
_sessions_lock = threading.RLock()


class CapacityExceededError(RuntimeError):
    """Raised when the Station execution authority has no free slot."""


def create_session(
    fmu_path: Path,
    *,
    access_key: str | None = None,
    expires_at: float | int | str | None = None,
    gateway_context: dict[str, Any] | None = None,
    session_factory: Any = None,
) -> Any:
    cleanup_expired_sessions()
    with _sessions_lock:
        if len(_sessions) >= config.MAX_CONCURRENT_SESSIONS:
            raise CapacityExceededError(
                f"Max concurrent sessions ({config.MAX_CONCURRENT_SESSIONS}) reached"
            )
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        factory = session_factory or FmuSession
        session = factory(
            session_id,
            fmu_path,
            access_key=access_key,
            expires_at=expires_at,
            gateway_context=gateway_context,
        )
        _sessions[session_id] = session
        return session


def get_session(session_id: str) -> Any | None:
    with _sessions_lock:
        return _sessions.get(session_id)


def get_attachable_session(session_id: str) -> Any | None:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None or not session.can_attach():
        if session is not None and not session.can_attach():
            remove_session(session_id)
        return None
    return session


def detach_session(
    session_id: str,
    grace_seconds: float,
    *,
    attachment_owner: object | None = None,
) -> None:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is not None:
        session.mark_detached(grace_seconds, attachment_owner)


def cleanup_expired_sessions(now: float | None = None) -> None:
    now = _time.time() if now is None else now
    with _sessions_lock:
        expired = [
            session_id
            for session_id, session in list(_sessions.items())
            if session._terminated or (not session._attached and not session.can_attach(now))
        ]
    for session_id in expired:
        remove_session(session_id)


def remove_session(session_id: str) -> None:
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session:
        session.terminate()


def active_session_count() -> int:
    cleanup_expired_sessions()
    with _sessions_lock:
        return len(_sessions)


def terminate_all() -> None:
    with _sessions_lock:
        session_ids = list(_sessions)
    for sid in session_ids:
        remove_session(sid)
