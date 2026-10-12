from __future__ import annotations

import base64
import json
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.simulation_store import InputUpdateQuotaExceededError, QuotaExceededError


def _request_with_context(value: str) -> Request:
    return Request({"type": "http", "headers": [(b"x-gateway-context", value.encode("ascii"))]})


def _encode_context(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode("utf-8")).decode("ascii").rstrip("=")


def test_gateway_context_decoding_validates_size_json_and_shape():
    expected = {"labId": "lab-1"}
    assert main._gateway_context_from_request(_request_with_context(_encode_context(expected))) == expected

    with pytest.raises(HTTPException) as oversized:
        main._gateway_context_from_request(_request_with_context("x" * 12001))
    assert oversized.value.status_code == 400
    for value in ("not-base64!", _encode_context(["not", "an", "object"])):
        with pytest.raises(HTTPException) as invalid:
            main._gateway_context_from_request(_request_with_context(value))
        assert invalid.value.status_code == 403


def test_scope_key_and_backend_validation_map_auth_failures(monkeypatch):
    context = {"labId": "lab-1", "reservationKey": "reservation-1"}
    monkeypatch.setattr(main.auth, "validate_gateway_context", Mock())
    monkeypatch.setattr(main, "reservation_scope", lambda _context: ("scope-key", {}))
    assert main._scope_key(context, "model.fmu") == "scope-key"

    monkeypatch.setattr(main.auth, "validate_gateway_context", Mock(side_effect=ValueError("invalid")))
    with pytest.raises(HTTPException) as invalid_scope:
        main._scope_key(context, "model.fmu")
    assert invalid_scope.value.status_code == 403

    with pytest.raises(HTTPException) as missing_scope:
        main._scope_key({}, "")
    assert missing_scope.value.status_code == 403

    monkeypatch.setattr(main.backends, "validate_requested_backend", Mock(side_effect=ValueError("bad backend")))
    with pytest.raises(HTTPException) as bad_backend:
        main._validate_backend_options({"backend": "unknown"})
    assert bad_backend.value.status_code == 400
    monkeypatch.setattr(main.backends, "validate_requested_backend", Mock(side_effect=NotImplementedError("planned")))
    with pytest.raises(HTTPException) as unavailable_backend:
        main._validate_backend_options({"backend": "planned"})
    assert unavailable_backend.value.status_code == 501


def test_scenario_reservation_handles_optional_scope_success_and_quota(monkeypatch, tmp_path):
    context = {"labId": "lab-1", "reservationKey": "reservation-1"}
    main._reserve_simulation_scenario(None, "model.fmu")
    with pytest.raises(HTTPException) as required:
        main._reserve_simulation_scenario(None, "model.fmu", require_scope=True)
    assert required.value.status_code == 403

    monkeypatch.setattr(main, "_scope_key", lambda *_args: "scope-key")
    monkeypatch.setattr(main, "model_execution_policy", lambda _context: {"maxDailyScenarios": 12})
    snapshot = tmp_path / "model.snapshot.fmu"
    monkeypatch.setattr(main.fmu_storage, "get_fmu_path", lambda _key: snapshot)
    monkeypatch.setattr(main.fmu_storage, "snapshot_fmu", lambda _path: (snapshot, "abc123"))
    reserve = Mock(return_value=1)
    monkeypatch.setattr(main._simulation_store, "reserve_scenarios", reserve)

    main._reserve_simulation_scenario(context, "model.fmu")
    reserve.assert_called_once_with("scope-key", 1, model_sha256="abc123", maximum=12)

    reserve.side_effect = QuotaExceededError(0)
    with pytest.raises(HTTPException) as quota:
        main._reserve_simulation_scenario(context, "model.fmu", model_sha256="abc123")
    assert quota.value.status_code == 429
    detail = cast(dict[str, Any], quota.value.detail)
    assert detail["remaining"] == 0


def test_input_reservation_validates_payload_and_maps_quota(monkeypatch):
    context = {"labId": "lab-1", "reservationKey": "reservation-1"}
    with pytest.raises(HTTPException) as missing_scope:
        main._reserve_input_update(None, "model.fmu", {"input": 1})
    assert missing_scope.value.status_code == 403
    assert missing_scope.value.detail == "RESERVATION_SCOPE_REQUIRED"

    monkeypatch.setattr(main, "_scope_key", lambda *_args: "scope-key")
    reserve = Mock(return_value={"remaining": 4})
    monkeypatch.setattr(main._simulation_store, "reserve_input_update", reserve)

    main._reserve_input_update(context, "model.fmu", {"temperature": "é"})
    reserve.assert_called_once_with("scope-key", "model.fmu", len('{"temperature":"é"}'.encode("utf-8")))

    reserve.side_effect = InputUpdateQuotaExceededError("INPUT_UPDATE_TOO_LARGE", 0)
    with pytest.raises(HTTPException) as quota:
        main._reserve_input_update(context, "model.fmu", {"temperature": 1})
    assert quota.value.status_code == 429
    detail = cast(dict[str, Any], quota.value.detail)
    assert detail["code"] == "INPUT_UPDATE_TOO_LARGE"

    reserve.side_effect = None
    with pytest.raises(HTTPException) as invalid:
        main._reserve_input_update(context, "model.fmu", {"temperature": float("nan")})
    assert invalid.value.status_code == 400


@pytest.mark.parametrize(
    "value", [True, "1", float("inf"), float("nan"), 10**10000],
    ids=["bool", "string", "infinity", "nan", "overflow"],
)
def test_strict_number_rejects_non_json_or_non_finite_values(value):
    with pytest.raises(ValueError):
        main._strict_number(value, "stopTime")


def test_active_reservation_deadline_validation(monkeypatch):
    main._validate_active_reservation_deadline({})
    monkeypatch.setattr(main.time, "time", lambda: 100.0)
    main._validate_active_reservation_deadline({"reservationEndsAt": 101})

    for context, expected in (
        ({"claims": ["invalid"]}, "INVALID_RESERVATION_END"),
        ({"reservationEndsAt": "invalid"}, "INVALID_RESERVATION_END"),
        ({"reservationEndsAt": "inf"}, "INVALID_RESERVATION_END"),
        ({"reservationEndsAt": 99}, "RESERVATION_NOT_ACTIVE"),
    ):
        with pytest.raises(HTTPException) as invalid:
            main._validate_active_reservation_deadline(context)
        assert invalid.value.detail == expected
