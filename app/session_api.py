"""WebSocket transport and lifecycle for interactive FMU sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from . import config, session_registry

logger = logging.getLogger(__name__)


def create_session_router(
    dispatch_message: Callable[..., Awaitable[dict[str, Any]]],
) -> APIRouter:
    """Create the session endpoint around the application's command dispatcher."""
    router = APIRouter()

    @router.websocket("/internal/fmu/sessions")
    async def ws_sessions(ws: WebSocket):
        # Validate internal token from headers (timing-safe comparison)
        token = ws.headers.get("x-internal-session-token") or ""
        internal_token = config.internal_token()
        if not internal_token or not secrets.compare_digest(token, internal_token):
            await ws.close(code=4001, reason="UNAUTHORIZED")
            return

        await ws.accept()
        session: Any | None = None
        _emitter_task: asyncio.Task | None = None
        _runner_task: asyncio.Task | None = None
        _expiry_task: asyncio.Task | None = None
        attachment_owner = object()
        _output_sampler_changed = asyncio.Event()

        async def _stop_runner() -> None:
            nonlocal _runner_task
            task = _runner_task
            _runner_task = None
            if task and task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        async def _run_session() -> None:
            try:
                simulated_origin = session._time if session else 0.0
                wall_origin = time.monotonic()
                last_progress = wall_origin
                progress_interval = config.FMU_PROGRESS_INTERVAL_MS / 1000.0
                while session and session.state == "running" and not session._terminated:
                    result = await asyncio.to_thread(session.step)
                    now = time.monotonic()
                    finished = bool(result.get("finished")) or session.state == "stopped"
                    if finished or now - last_progress >= progress_interval:
                        await ws.send_text(json.dumps({
                            "type": "sim.progress",
                            "sessionId": session.session_id,
                            "simTime": session._time,
                            "timeAdvanced": result.get("timeAdvanced", 0.0),
                            "earlyReturn": result.get("earlyReturn", False),
                            "eventOccurred": result.get("eventOccurred", False),
                            "eventHandlingNeeded": result.get("eventHandlingNeeded", False),
                            "terminateSimulation": result.get("terminateSimulation", False),
                        }))
                        last_progress = now
                    if finished:
                        await ws.send_text(json.dumps({
                            "type": "sim.state",
                            "sessionId": session.session_id,
                            "state": "stopped",
                            "simTime": session._time,
                            "reason": session._finished_reason,
                        }))
                        return
                    if session._time_mode == "realtime":
                        target_wall_time = wall_origin + (session._time - simulated_origin) / session._playback_speed
                        delay = target_wall_time - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                    else:
                        # Keep the event loop responsive while simulating as fast as
                        # the FMU permits; output subscriptions have their own rate cap.
                        await asyncio.sleep(0)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("FMU realtime runner failed")
                if session and not session._terminated:
                    session._state = "error"
                    try:
                        await ws.send_text(json.dumps({
                            "type": "sim.state",
                            "sessionId": session.session_id,
                            "state": "error",
                            "simTime": session._time,
                        }))
                    except Exception:
                        pass

        def _start_runner() -> None:
            nonlocal _runner_task
            if _runner_task is None or _runner_task.done():
                _runner_task = asyncio.create_task(_run_session())

        async def _expire_session_after_deadline(expiring_session: Any):
            nonlocal session
            try:
                deadlines: list[tuple[float, str]] = []
                if expiring_session.expires_at is not None:
                    auth_expiry = float(expiring_session.expires_at)
                    if not math.isfinite(auth_expiry):
                        raise ValueError("Authorization expiry must be finite")
                    deadlines.append((auth_expiry, "expired"))
                context = getattr(expiring_session, "gateway_context", {}) or {}
                claims = context.get("claims") or {} if isinstance(context, dict) else {}
                reservation_end = context.get("reservationEndsAt") if isinstance(context, dict) else None
                if reservation_end is None and isinstance(claims, dict):
                    reservation_end = claims.get("reservationEndsAt")
                if reservation_end is not None:
                    reservation_ends_at = float(reservation_end)
                    if not math.isfinite(reservation_ends_at):
                        raise ValueError("Reservation end must be finite")
                    deadlines.append((reservation_ends_at, "reservation_ended"))
                if not deadlines:
                    return
                expires_at, finish_reason = min(deadlines, key=lambda item: item[0])
                await asyncio.sleep(max(0.0, expires_at - time.time()))
                if session is not expiring_session:
                    return
                await _stop_runner()
                await asyncio.to_thread(session_registry.remove_session, expiring_session.session_id)
                session = None
                await ws.send_text(json.dumps({
                    "type": "session.closed",
                    "sessionId": expiring_session.session_id,
                    "reason": finish_reason,
                }))
                await ws.close(code=4003, reason="RESERVATION_ENDED" if finish_reason == "reservation_ended" else "SESSION_EXPIRED")
            except asyncio.CancelledError:
                return
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid FMU session expiry session_id=%s", expiring_session.session_id)
            except Exception:
                logger.debug("Unable to close expired FMU session", exc_info=True)

        async def _output_emitter():
            """Background task that streams subscribed outputs to the WS client."""
            try:
                while True:
                    active_session = session
                    subscription = active_session.subscription if active_session else None
                    if (
                        not active_session
                        or not subscription
                        or not active_session._initialised
                        or active_session._terminated
                    ):
                        _output_sampler_changed.clear()
                        await _output_sampler_changed.wait()
                        continue

                    wait_seconds = subscription.seconds_until_due()
                    read_waiter = getattr(active_session, "output_read_wait_seconds", None)
                    if callable(read_waiter):
                        wait_seconds = max(wait_seconds, read_waiter())
                    if wait_seconds > 0:
                        try:
                            await asyncio.wait_for(_output_sampler_changed.wait(), timeout=wait_seconds)
                        except asyncio.TimeoutError:
                            pass
                        _output_sampler_changed.clear()
                        continue

                    payload = await asyncio.to_thread(active_session.sample_subscription)
                    if payload:
                        await ws.send_text(json.dumps(payload, default=str))
            except WebSocketDisconnect:
                logger.debug("Output emitter stopped after WebSocket disconnect")
            except asyncio.CancelledError:
                logger.debug("Output emitter task cancelled")
            except Exception:
                logger.debug("Output emitter stopped", exc_info=True)

        try:
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                msg_type = msg.get("type", "")
                request_id = msg.get("requestId")
                gateway_ctx = msg.get("gatewayContext")

                try:
                    response = await dispatch_message(
                        msg_type,
                        msg,
                        gateway_ctx,
                        session,
                        start_runner=_start_runner,
                        stop_runner=_stop_runner,
                    )
                    if msg_type in ("session.create", "session.attach"):
                        bound_session = response.pop("_session", None)
                        if bound_session is not None:
                            if _expiry_task:
                                _expiry_task.cancel()
                                _expiry_task = None
                            session = bound_session
                            bound_session.mark_attached(attachment_owner)
                            gateway_claims = (bound_session.gateway_context or {}).get("claims", {})
                            gateway_reservation_end = (bound_session.gateway_context or {}).get("reservationEndsAt")
                            if gateway_reservation_end is None and isinstance(gateway_claims, dict):
                                gateway_reservation_end = gateway_claims.get("reservationEndsAt")
                            if bound_session.expires_at is not None or gateway_reservation_end is not None:
                                _expiry_task = asyncio.create_task(_expire_session_after_deadline(bound_session))
                            # Start emitter task on session creation and wake it if it already exists.
                            if _emitter_task is None or _emitter_task.done():
                                _emitter_task = asyncio.create_task(_output_emitter())
                            _output_sampler_changed.set()
                    elif msg_type == "session.terminate":
                        await _stop_runner()
                        if _expiry_task:
                            _expiry_task.cancel()
                            _expiry_task = None
                        if session:
                            await asyncio.to_thread(session_registry.remove_session, session.session_id)
                        session = None
                        _output_sampler_changed.set()
                    elif msg_type in (
                        "sim.initialize",
                        "sim.reset",
                        "sim.resume",
                        "sim.subscribeOutputs",
                        "sim.unsubscribeOutputs",
                    ):
                        _output_sampler_changed.set()

                    if request_id:
                        response["requestId"] = request_id
                    await ws.send_text(json.dumps(response, default=str))

                except HTTPException as exc:
                    detail = exc.detail or ""
                    if isinstance(detail, dict):
                        code = str(detail.get("code") or "ERROR")
                        message = code
                        err: dict[str, Any] = {
                            "type": "error",
                            "code": code,
                            "message": detail.get("message") or message,
                            "retryable": bool(detail.get("retryable", False)),
                        }
                        for field in ("remaining", "retryAfterMs", "maxBatchSize"):
                            if field in detail:
                                err[field] = detail.get(field)
                    else:
                        # Extract short code from detail strings like "INVALID_COMMAND – ..."
                        code = detail.split(" \u2013 ")[0].split(" - ")[0].strip() if detail else "ERROR"
                        err = {"type": "error", "code": code, "message": detail, "retryable": False}
                    if request_id:
                        err["requestId"] = request_id
                    await ws.send_text(json.dumps(err))
                except Exception:
                    logger.exception("FMU WebSocket command failed")
                    err = {
                        "type": "error",
                        "code": "INTERNAL_ERROR",
                        "message": "Internal FMU executor error",
                        "retryable": False,
                    }
                    if request_id:
                        err["requestId"] = request_id
                    await ws.send_text(json.dumps(err))

        except WebSocketDisconnect:
            logger.debug("WebSocket client disconnected")
        finally:
            await _stop_runner()
            if _expiry_task:
                _expiry_task.cancel()
                try:
                    await _expiry_task
                except asyncio.CancelledError:
                    logger.debug("FMU expiry task cancelled during cleanup")
            if _emitter_task:
                _emitter_task.cancel()
                try:
                    await _emitter_task
                except asyncio.CancelledError:
                    logger.debug("WebSocket emitter task cancelled during cleanup")
            if session and not session._terminated:
                session_registry.detach_session(
                    session.session_id,
                    config.FMU_ATTACH_GRACE_SECONDS,
                    attachment_owner=attachment_owner,
                )
    return router
