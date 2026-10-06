"""Internal auth: token validation and gateway context checks."""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import HTTPException, Request

from . import config

logger = logging.getLogger(__name__)


def validate_internal_token(request: Request) -> None:
    """Check X-Internal-Session-Token header matches the configured secret."""
    expected = config.internal_token()
    if not expected:
        # The service binds to the station network interface by default. An
        # absent token must therefore fail closed instead of exposing the
        # internal FMU control plane anonymously.
        raise HTTPException(status_code=401, detail="FMU_INTERNAL_TOKEN_NOT_CONFIGURED")
    provided = request.headers.get("X-Internal-Session-Token") or ""
    if not secrets.compare_digest(provided, expected):
        logger.warning("Rejected request: invalid internal token")
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")


def validate_gateway_context(ctx: dict | None, access_key: str) -> None:
    """Validate that *ctx* is consistent with the requested *access_key*."""
    if ctx is None:
        raise HTTPException(status_code=400, detail="Missing gatewayContext")

    claims = ctx.get("claims") or {}

    # Check accessKey match
    ctx_key = ctx.get("accessKey") or claims.get("accessKey") or claims.get("fmuFileName")
    if ctx_key and ctx_key != access_key:
        raise HTTPException(status_code=403, detail="FORBIDDEN – accessKey mismatch")

    # Check expiry
    exp = claims.get("exp")
    if exp is not None:
        try:
            if float(exp) <= time.time():
                raise HTTPException(status_code=403, detail="RESERVATION_NOT_ACTIVE")
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=403, detail="RESERVATION_NOT_ACTIVE") from exc

    # Check nbf
    nbf = claims.get("nbf")
    if nbf is not None:
        try:
            if float(nbf) > time.time():
                raise HTTPException(status_code=403, detail="RESERVATION_NOT_ACTIVE")
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=403, detail="RESERVATION_NOT_ACTIVE") from exc


def _binding_value(ctx: dict, key: str) -> str:
    claims = ctx.get("claims") or {}
    value = ctx.get(key) or claims.get(key)
    return str(value or "").strip().lower()


def validate_session_context(ctx: dict | None, expected_ctx: dict | None, access_key: str) -> None:
    """Validate a reconnecting client against the original session binding."""
    if ctx is None:
        raise HTTPException(status_code=400, detail="Missing gatewayContext")
    validate_gateway_context(ctx, access_key)
    if expected_ctx is None:
        raise HTTPException(status_code=403, detail="FORBIDDEN – session context unavailable")

    for key in ("sub", "labId", "reservationKey", "pucHash", "targetGatewayId"):
        if _binding_value(ctx, key) != _binding_value(expected_ctx, key):
            raise HTTPException(status_code=403, detail="FORBIDDEN – session context mismatch")


def extract_access_key_from_context(ctx: dict) -> str | None:
    """Return the canonical access key from a gatewayContext."""
    claims = ctx.get("claims") or {}
    return ctx.get("accessKey") or claims.get("accessKey") or claims.get("fmuFileName")
