"""Thread-safe registry and shared capacity reservations for FMI sessions."""

from __future__ import annotations

import threading
import time as _time
import uuid
from pathlib import Path
from typing import Any, Callable

from . import config


# ── session registry ─────────────────────────────────────────────

_sessions: dict[str, Any] = {}
_sessions_lock = threading.RLock()
_capacity_reservations: set[str] = set()


class CapacityExceededError(RuntimeError):
    """Raised when the Station execution authority has no free slot."""


def create_session(
    fmu_path: Path,
    *,
    access_key: str | None = None,
    expires_at: float | int | str | None = None,
    gateway_context: dict[str, Any] | None = None,
    fmu_sha256: str | None = None,
    session_factory: Callable[..., Any],
    resource_policy: dict[str, Any] | None = None,
) -> Any:
    """Create, attach, and register one session under the shared capacity limit."""
    cleanup_expired_sessions()
    with _sessions_lock:
        if len(_sessions) + len(_capacity_reservations) >= config.MAX_CONCURRENT_SESSIONS:
            raise CapacityExceededError(
                f"Max concurrent sessions ({config.MAX_CONCURRENT_SESSIONS}) reached"
            )
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        session_arguments = (session_id, fmu_path)
        session_options = {
            "access_key": access_key,
            "expires_at": expires_at,
            "gateway_context": gateway_context,
            "fmu_sha256": fmu_sha256,
        }
        if resource_policy is not None:
            session_options["resource_policy"] = resource_policy
        session = session_factory(*session_arguments, **session_options)
        # A newly created WebSocket session is actively being established.
        # Do not let periodic cleanup reap it while its initial worker load RPC
        # is still in flight; the owning connection is attached immediately
        # after the create response is assembled.
        session.mark_attached()
        _sessions[session_id] = session
        return session


def reserve_capacity() -> str:
    """Reserve one shared execution slot before charging a simulation job."""
    cleanup_expired_sessions()
    with _sessions_lock:
        if len(_sessions) + len(_capacity_reservations) >= config.MAX_CONCURRENT_SESSIONS:
            raise CapacityExceededError(
                f"Max concurrent executions ({config.MAX_CONCURRENT_SESSIONS}) reached"
            )
        token = uuid.uuid4().hex
        _capacity_reservations.add(token)
        return token


def release_capacity(token: str | None) -> None:
    if not token:
        return
    with _sessions_lock:
        _capacity_reservations.discard(token)


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
        return len(_sessions) + len(_capacity_reservations)


def terminate_all() -> None:
    with _sessions_lock:
        session_ids = list(_sessions)
    for sid in session_ids:
        remove_session(sid)
    with _sessions_lock:
        _capacity_reservations.clear()
