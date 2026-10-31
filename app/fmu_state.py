"""Reservation-bound, opaque checkpoints for serialized FMI state snapshots."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


FMU_STATE_CHECKPOINT_FORMAT = "decentralabs-fmi-state-v1"
MAX_FMU_STATE_BYTES = 32 * 1024 * 1024
MAX_FMU_STATE_CHECKPOINT_JSON_BYTES = (MAX_FMU_STATE_BYTES * 4 + 2) // 3
_MAX_CHECKPOINTS_PER_SCOPE = 16
_MAX_CHECKPOINTS_TOTAL = 512
_MAX_STORED_FMU_STATE_BYTES = 1024 * 1024 * 1024
_CHECKPOINT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SCOPE_CLAIMS = ("sub", "labId", "reservationKey", "pucHash", "targetGatewayId")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _scope_digest(gateway_context: dict[str, Any] | None) -> str:
    context = gateway_context if isinstance(gateway_context, dict) else {}
    claims = context.get("claims")
    claims = claims if isinstance(claims, dict) else {}
    scope = {
        name: str(context.get(name) or claims.get(name) or "").strip().lower()
        for name in _SCOPE_CLAIMS
    }
    return hashlib.sha256(_canonical_json(scope)).hexdigest()


def _checkpoint_directory(state_dir: Path) -> Path:
    directory = Path(state_dir) / "fmu-state-checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if os.name != "nt":
            directory.chmod(0o700)
    except OSError:
        pass
    return directory


def _remove_checkpoint(directory: Path, checkpoint_id: str) -> None:
    for suffix in (".json", ".bin"):
        try:
            (directory / f"{checkpoint_id}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


def _purge_expired(directory: Path, now: float) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for metadata_path in directory.glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("invalid metadata")
            if float(metadata.get("expiresAt", 0)) <= now:
                _remove_checkpoint(directory, metadata_path.stem)
                continue
            state_size = metadata.get("stateSizeBytes")
            state_path = directory / f"{metadata_path.stem}.bin"
            if (
                type(state_size) is not int
                or not 1 <= state_size <= MAX_FMU_STATE_BYTES
                or not state_path.is_file()
                or state_path.stat().st_size != state_size
            ):
                _remove_checkpoint(directory, metadata_path.stem)
                continue
            active.append(metadata)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            _remove_checkpoint(directory, metadata_path.stem)
    for state_path in directory.glob("*.bin"):
        if not (directory / f"{state_path.stem}.json").is_file():
            try:
                state_path.unlink(missing_ok=True)
            except OSError:
                pass
    return active


def seal_fmu_state_checkpoint(
    snapshot: dict[str, Any],
    *,
    gateway_context: dict[str, Any] | None,
    signing_secret: str | None,
    state_dir: Path,
) -> dict[str, Any]:
    if not signing_secret or not isinstance(snapshot, dict):
        raise ValueError("FMU state checkpoint signing is unavailable")
    encoded_state = snapshot.get("serializedState")
    if not isinstance(encoded_state, str):
        raise ValueError("The FMU returned an invalid serialized state")
    try:
        serialized_state = base64.b64decode(encoded_state, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("The FMU returned an invalid serialized state") from exc
    if not serialized_state or len(serialized_state) > MAX_FMU_STATE_BYTES:
        raise ValueError("FMU state checkpoint exceeds the size limit")

    now = time.time()
    directory = _checkpoint_directory(state_dir)
    scope = _scope_digest(gateway_context)
    active = _purge_expired(directory, now)
    scoped = sorted(
        (item for item in active if item.get("scope") == scope),
        key=lambda item: float(item.get("createdAt", 0)),
    )
    while len(scoped) >= _MAX_CHECKPOINTS_PER_SCOPE:
        oldest = scoped.pop(0)
        _remove_checkpoint(directory, str(oldest.get("checkpointId") or ""))
        active = [item for item in active if item is not oldest]
    total_state_bytes = sum(
        int(item.get("stateSizeBytes", 0))
        for item in active
        if type(item.get("stateSizeBytes")) is int
    )
    for oldest in sorted(active, key=lambda item: float(item.get("createdAt", 0))):
        if (
            len(active) < _MAX_CHECKPOINTS_TOTAL
            and total_state_bytes + len(serialized_state) <= _MAX_STORED_FMU_STATE_BYTES
        ):
            break
        _remove_checkpoint(directory, str(oldest.get("checkpointId") or ""))
        total_state_bytes -= int(oldest.get("stateSizeBytes", 0))
        active.remove(oldest)

    checkpoint_id = uuid4().hex
    # Checkpoints are tied to the current reservation and expire within 24 hours.
    expires_at = now + 24 * 60 * 60
    metadata_snapshot = {key: value for key, value in snapshot.items() if key != "serializedState"}
    record = {
        "format": FMU_STATE_CHECKPOINT_FORMAT,
        "checkpointId": checkpoint_id,
        "scope": scope,
        "snapshot": metadata_snapshot,
        "stateSizeBytes": len(serialized_state),
        "createdAt": now,
        "expiresAt": expires_at,
    }
    signature = hmac.new(signing_secret.encode("utf-8"), _canonical_json(record), hashlib.sha256).hexdigest()
    stored_checkpoint = {**record, "signature": signature}
    # Keep the browser-facing FMU state token opaque. Metadata and serialized
    # state remain in the Executor's private state directory.
    checkpoint = {
        "format": FMU_STATE_CHECKPOINT_FORMAT,
        "checkpointId": checkpoint_id,
        "scope": scope,
        "createdAt": now,
        "expiresAt": expires_at,
        "signature": signature,
    }

    state_path = directory / f"{checkpoint_id}.bin"
    metadata_path = directory / f"{checkpoint_id}.json"
    try:
        with state_path.open("xb") as state_file:
            state_file.write(serialized_state)
        if os.name != "nt":
            state_path.chmod(0o600)
        with metadata_path.open("x", encoding="utf-8") as metadata_file:
            metadata_file.write(_canonical_json(stored_checkpoint).decode("utf-8"))
        if os.name != "nt":
            metadata_path.chmod(0o600)
    except OSError as exc:
        _remove_checkpoint(directory, checkpoint_id)
        raise ValueError("The FMU state checkpoint could not be stored") from exc
    return checkpoint


def open_fmu_state_checkpoint(
    checkpoint: Any,
    *,
    gateway_context: dict[str, Any] | None,
    signing_secret: str | None,
    expected_fmu_sha256: str | None,
    state_dir: Path,
) -> dict[str, Any]:
    if not signing_secret or not isinstance(checkpoint, dict):
        raise ValueError("FMU state checkpoint is invalid or incompatible")
    try:
        if len(_canonical_json(checkpoint)) > 4096:
            raise ValueError("FMU state checkpoint is invalid or incompatible")
    except (TypeError, ValueError) as exc:
        raise ValueError("FMU state checkpoint is invalid or incompatible") from exc
    required = {"format", "checkpointId", "scope", "createdAt", "expiresAt", "signature"}
    if set(checkpoint) != required:
        raise ValueError("FMU state checkpoint is invalid or incompatible")
    try:
        expiry = float(checkpoint["expiresAt"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("FMU state checkpoint is invalid or incompatible") from exc
    if checkpoint.get("format") != FMU_STATE_CHECKPOINT_FORMAT:
        raise ValueError("FMU state checkpoint is invalid or incompatible")
    checkpoint_id = checkpoint.get("checkpointId")
    if not isinstance(checkpoint_id, str) or not _CHECKPOINT_ID_RE.fullmatch(checkpoint_id):
        raise ValueError("FMU state checkpoint is invalid or incompatible")
    if checkpoint.get("scope") != _scope_digest(gateway_context) or expiry <= time.time():
        raise ValueError("FMU state checkpoint is expired or belongs to another reservation")
    directory = _checkpoint_directory(state_dir)
    metadata_path = directory / f"{checkpoint_id}.json"
    state_path = directory / f"{checkpoint_id}.bin"
    try:
        stored_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(stored_metadata, dict):
            raise ValueError("FMU state checkpoint is invalid or incompatible")
        stored_body = {key: value for key, value in stored_metadata.items() if key != "signature"}
        expected_signature = hmac.new(
            signing_secret.encode("utf-8"),
            _canonical_json(stored_body),
            hashlib.sha256,
        ).hexdigest()
        token_fields = {key: stored_metadata.get(key) for key in required}
        if (
            token_fields != checkpoint
            or not isinstance(stored_metadata.get("signature"), str)
            or not hmac.compare_digest(stored_metadata["signature"], expected_signature)
        ):
            raise ValueError("FMU state checkpoint is invalid or incompatible")
        serialized_state = state_path.read_bytes()
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("FMU state checkpoint is no longer available") from exc
    snapshot = stored_metadata.get("snapshot")
    state_size = stored_metadata.get("stateSizeBytes")
    if (
        not isinstance(snapshot, dict)
        or not expected_fmu_sha256
        or snapshot.get("fmuSha256") != expected_fmu_sha256
        or type(state_size) is not int
        or not 1 <= state_size <= MAX_FMU_STATE_BYTES
        or len(serialized_state) != state_size
    ):
        raise ValueError("FMU state checkpoint is invalid or incompatible")
    return {**snapshot, "serializedState": base64.b64encode(serialized_state).decode("ascii")}
