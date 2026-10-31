"""FMU WebSocket command validation and execution protocol."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from . import auth
from .session_protocol_common import SessionCommandServices
from .session_protocol_execution import EXECUTION_COMMAND_TYPES, handle_execution_command
from .session_protocol_io import IO_COMMAND_TYPES, handle_io_command
from .session_protocol_lifecycle import (
    LIFECYCLE_COMMAND_TYPES,
    SESSION_BOOTSTRAP_COMMAND_TYPES,
    handle_lifecycle_command,
    handle_session_bootstrap,
)


async def handle_ws_message(
    msg_type: str,
    msg: dict,
    gateway_ctx: dict | None,
    session: Any | None,
    *,
    services: SessionCommandServices,
    start_runner=None,
    stop_runner=None,
) -> dict[str, Any]:
    """Authorize a session command once, then dispatch it to its handler."""
    if msg_type in SESSION_BOOTSTRAP_COMMAND_TYPES:
        return await handle_session_bootstrap(msg_type, msg, gateway_ctx, session, services)

    if session is None:
        raise HTTPException(400, "INVALID_COMMAND – no active session")

    # Validate the creation context even when a later command omits it.
    auth.validate_gateway_context(session.gateway_context, session.access_key)
    command_context = gateway_ctx or session.gateway_context
    access_key = auth.extract_access_key_from_context(command_context)
    if not access_key:
        raise HTTPException(400, "Missing accessKey in gatewayContext")
    access_key = services.validate_access_key(access_key)
    if access_key != session.access_key:
        raise HTTPException(403, "FORBIDDEN – accessKey mismatch")
    if gateway_ctx is not None:
        auth.validate_gateway_context(command_context, access_key)

    if msg_type in LIFECYCLE_COMMAND_TYPES:
        return await handle_lifecycle_command(
            msg_type,
            msg,
            session,
            access_key,
            services,
            start_runner=start_runner,
            stop_runner=stop_runner,
        )
    if msg_type in EXECUTION_COMMAND_TYPES:
        return await handle_execution_command(msg_type, msg, session, access_key, services)
    if msg_type in IO_COMMAND_TYPES:
        return await handle_io_command(msg_type, msg, session, access_key, services)

    raise HTTPException(400, f"INVALID_COMMAND – unknown type {msg_type!r}")
