"""Tests for the FMU Executor internal API.

These tests use FastAPI TestClient with a mock FMU to validate the internal
contract that Lab Gateway's fmu-runner expects from the station backend.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path):
    """Point config at a temporary FMU root and reset sessions between tests."""
    fmu_root = tmp_path / "fmu-data"
    fmu_root.mkdir()
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()

    with patch.dict(os.environ, {
        "FMU_ROOT": str(fmu_root),
        "FMU_EXECUTOR_TEMP": str(temp_dir),
        "FMU_INTERNAL_TOKEN": "test-secret",
        "FMU_MAX_SESSIONS": "4",
        "FMU_ATTACH_GRACE_SECONDS": "120",
        "FMU_LOG_LEVEL": "WARNING",
    }):
        # Re-import config so the patched env takes effect
        import importlib
        from app import config as cfg
        importlib.reload(cfg)
        try:
            yield fmu_root
        finally:
            # Cleanup while the test configuration is still active.
            from app.engine import terminate_all
            terminate_all()
            # Clear quarantine cache between tests
            from app.fmu_storage import _quarantine_cache
            _quarantine_cache.clear()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


@pytest.fixture
def auth_headers():
    return {"X-Internal-Session-Token": "test-secret"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self, client, auth_headers):
        resp = client.get("/internal/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "UP"
        assert "fmuCount" in body
        assert "activeSessions" in body

    def test_health_no_auth_required(self, client):
        """Health endpoint doesn't require internal token."""
        resp = client.get("/internal/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_describe_requires_token(self, client):
        resp = client.get("/internal/fmu/describe", headers={"X-FMU-Access-Key": "test.fmu"})
        assert resp.status_code == 401

    def test_describe_rejects_wrong_token(self, client):
        resp = client.get(
            "/internal/fmu/describe",
            headers={"X-Internal-Session-Token": "wrong", "X-FMU-Access-Key": "test.fmu"},
        )
        assert resp.status_code == 401

    def test_catalog_requires_token(self, client):
        resp = client.get("/internal/fmu/catalog", headers={"X-FMU-Access-Key": "test.fmu"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Describe / Catalog – 404 when FMU missing
# ---------------------------------------------------------------------------

class TestDescribe:
    def test_describe_404_when_missing(self, client, auth_headers):
        resp = client.get("/internal/fmu/describe", headers={**auth_headers, "X-FMU-Access-Key": "nonexistent.fmu"})
        assert resp.status_code == 404

    def test_catalog_404_when_missing(self, client, auth_headers):
        resp = client.get("/internal/fmu/catalog", headers={**auth_headers, "X-FMU-Access-Key": "nonexistent.fmu"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Describe / Catalog – with a mock FMU
# ---------------------------------------------------------------------------

class TestDescribeWithFmu:
    """Tests that use a mocked fmpy.read_model_description."""

    def _mock_model_description(self):
        md = MagicMock()
        md.modelName = "TestModel"
        md.guid = "{test-guid-1234}"
        md.fmiVersion = "2.0"
        md.coSimulation = MagicMock()
        md.coSimulation.modelIdentifier = "TestModel"
        md.modelExchange = None
        md.defaultExperiment = MagicMock()
        md.defaultExperiment.startTime = "0.0"
        md.defaultExperiment.stopTime = "1.0"
        md.defaultExperiment.stepSize = "0.01"

        var1 = MagicMock()
        var1.name = "x"
        var1.valueReference = 0
        var1.causality = "output"
        var1.variability = "continuous"
        var1.type = "Real"
        var1.start = None
        var1.unit = "m"
        var1.min = None
        var1.max = None

        var2 = MagicMock()
        var2.name = "u"
        var2.valueReference = 1
        var2.causality = "input"
        var2.variability = "continuous"
        var2.type = "Real"
        var2.start = "0.0"
        var2.unit = None
        var2.min = None
        var2.max = None

        md.modelVariables = [var1, var2]
        return md

    def test_describe_returns_model_info(self, client, auth_headers, _isolate_config):
        fmu_root = _isolate_config
        # Create a dummy .fmu file
        dummy_fmu = fmu_root / "TestModel.fmu"
        dummy_fmu.write_bytes(b"PK\x03\x04dummy")

        md = self._mock_model_description()
        with patch("fmpy.read_model_description", return_value=md):
            resp = client.get("/internal/fmu/describe", headers={**auth_headers, "X-FMU-Access-Key": "TestModel.fmu"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["modelName"] == "TestModel"
        assert body["guid"] == "{test-guid-1234}"
        assert body["fmiVersion"] == "2.0"
        assert body["supportsCoSimulation"] is True
        assert body["supportsModelExchange"] is False
        assert body["fmiType"] == "CoSimulation"
        assert len(body["modelVariables"]) == 2
        assert body["defaultStartTime"] == 0.0
        assert body["defaultStopTime"] == 1.0
        assert body["defaultStepSize"] == 0.01

    def test_describe_serializes_fmi3_binary_start_as_base64(self, client, auth_headers, _isolate_config):
        dummy_fmu = _isolate_config / "BinaryModel.fmu"
        dummy_fmu.write_bytes(b"PK\x03\x04dummy")

        md = MagicMock()
        md.modelName = "BinaryModel"
        md.guid = "{binary-guid}"
        md.fmiVersion = "3.0"
        md.coSimulation = MagicMock()
        md.modelExchange = None
        md.defaultExperiment = None

        binary = MagicMock()
        binary.name = "blob"
        binary.valueReference = 11
        binary.causality = "input"
        binary.variability = "discrete"
        binary.type = "Binary"
        binary.start = b"\x01\x02"
        binary.unit = None
        binary.min = None
        binary.max = None
        md.modelVariables = [binary]

        with patch("fmpy.read_model_description", return_value=md):
            response = client.get(
                "/internal/fmu/describe",
                headers={**auth_headers, "X-FMU-Access-Key": "BinaryModel.fmu"},
            )

        assert response.status_code == 200
        assert response.json()["modelVariables"][0]["start"] == "AQI="

    def test_catalog_returns_files_and_describe(self, client, auth_headers, _isolate_config):
        fmu_root = _isolate_config
        dummy_fmu = fmu_root / "TestModel.fmu"
        dummy_fmu.write_bytes(b"PK\x03\x04dummy")

        md = self._mock_model_description()
        with patch("fmpy.read_model_description", return_value=md):
            resp = client.get("/internal/fmu/catalog", headers={**auth_headers, "X-FMU-Access-Key": "TestModel.fmu"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["accessKey"] == "TestModel.fmu"
        assert any(f["filename"] == "TestModel.fmu" for f in body["fmus"])
        assert body["describe"]["modelName"] == "TestModel"


# ---------------------------------------------------------------------------
# FMU storage - path traversal protection
# ---------------------------------------------------------------------------

class TestPathTraversal:
    @pytest.mark.parametrize("route", [
        "/internal/fmu/describe",
        "/internal/fmu/catalog",
    ])
    def test_header_routes_reject_traversal(self, client, auth_headers, route):
        resp = client.get(
            route,
            headers={**auth_headers, "X-FMU-Access-Key": "../../etc/passwd"},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize(("method", "route"), [
        ("POST", "/internal/fmu/validate/..%2Foutside.fmu"),
        ("POST", "/internal/fmu/quarantine/..%2Foutside.fmu"),
        ("DELETE", "/internal/fmu/quarantine/..%2Foutside.fmu"),
    ])
    def test_path_routes_reject_traversal(self, client, auth_headers, method, route):
        resp = client.request(method, route, headers=auth_headers)
        assert resp.status_code == 400

    def test_storage_rejects_invalid_access_key(self, _isolate_config):
        from app import fmu_storage

        with pytest.raises(ValueError, match="Invalid FMU access key"):
            fmu_storage.quarantine("../outside.fmu", "test")

    def test_symlinked_directory_outside_root_is_not_resolved(
        self, _isolate_config, tmp_path
    ):
        from app import fmu_storage

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "outside.fmu").write_bytes(b"PK")
        link = _isolate_config / "linked-model"
        try:
            link.symlink_to(outside, target_is_directory=True)
            file_link = _isolate_config / "linked-file.fmu"
            file_link.symlink_to(outside / "outside.fmu")
        except OSError:
            pytest.skip("Creating directory symlinks requires elevated Windows privileges")

        assert fmu_storage.fmu_exists("linked-model") is False
        assert fmu_storage.fmu_exists("linked-file.fmu") is False

    def test_nested_directory_access_key_remains_supported(self, _isolate_config):
        from app import fmu_storage

        model_dir = _isolate_config / "provider"
        model_dir.mkdir()
        (model_dir / "demo.fmu").write_bytes(b"PK")

        assert fmu_storage.fmu_exists("provider") is True
        assert fmu_storage.get_fmu_path("provider") == model_dir / "demo.fmu"


# ---------------------------------------------------------------------------
# Run simulation – 404 when FMU missing
# ---------------------------------------------------------------------------

class TestRunSimulation:
    def test_run_404_when_fmu_missing(self, client, auth_headers):
        resp = client.post(
            "/internal/fmu/simulations/run",
            headers=auth_headers,
            json={"accessKey": "missing.fmu", "parameters": {}, "options": {"stopTime": 0.5}},
        )
        assert resp.status_code == 404

    def test_stream_404_when_fmu_missing(self, client, auth_headers):
        resp = client.post(
            "/internal/fmu/simulations/stream",
            headers=auth_headers,
            json={"accessKey": "missing.fmu", "parameters": {}, "options": {"stopTime": 0.5}},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# WebSocket – auth validation
# ---------------------------------------------------------------------------

class TestWebSocketAuth:
    def test_ws_rejects_when_token_is_not_configured(self, client, monkeypatch):
        from starlette.websockets import WebSocketDisconnect

        monkeypatch.setattr("app.main.config.internal_token", lambda: "")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/internal/fmu/sessions"):
                pass
        assert exc_info.value.code == 4001

    def test_ws_rejects_missing_token(self, client):
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/internal/fmu/sessions"):
                pass
        assert exc_info.value.code == 4001

    def test_ws_rejects_wrong_token(self, client):
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "wrong"},
            ):
                pass
        assert exc_info.value.code == 4001


# ---------------------------------------------------------------------------
# WebSocket – session lifecycle (with mock)
# ---------------------------------------------------------------------------

class TestWebSocketSession:
    def test_session_create_requires_gateway_context(self, client, auth_headers):
        with client.websocket_connect(
            "/internal/fmu/sessions",
            headers={"X-Internal-Session-Token": "test-secret"}
        ) as ws:
            ws.send_text(json.dumps({
                "type": "session.create",
                "requestId": "req1",
            }))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "error"
            assert resp["requestId"] == "req1"

    def test_session_create_rejects_traversal_access_key(self, client):
        with client.websocket_connect(
            "/internal/fmu/sessions",
            headers={"X-Internal-Session-Token": "test-secret"}
        ) as ws:
            ws.send_text(json.dumps({
                "type": "session.create",
                "requestId": "traversal",
                "gatewayContext": {
                    "mode": "station",
                    "accessKey": "../outside.fmu",
                    "claims": {"accessKey": "../outside.fmu"},
                },
            }))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "error"
            assert resp["requestId"] == "traversal"
            assert resp["code"] == "INVALID_FMU_ACCESS_KEY"

    def test_session_create_validates_access_key(self, client):
        with client.websocket_connect(
            "/internal/fmu/sessions",
            headers={"X-Internal-Session-Token": "test-secret"}
        ) as ws:
            ws.send_text(json.dumps({
                "type": "session.create",
                "requestId": "req2",
                "gatewayContext": {
                    "mode": "station",
                    "accessKey": "nonexistent.fmu",
                    "claims": {},
                },
            }))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "error"
            assert "FMU_NOT_FOUND" in resp.get("code", "")

    def test_expired_session_rejects_subsequent_command(self, client, _isolate_config):
        import time

        expires_at = time.time() + 3600
        with _mock_fmu_patches(_isolate_config):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "test-secret"},
            ) as ws:
                ws.send_text(json.dumps({
                    "type": "session.create",
                    "requestId": "expiry-create",
                    "gatewayContext": {
                        "mode": "station",
                        "accessKey": "Test.fmu",
                        "claims": {"exp": expires_at},
                    },
                }))
                created = json.loads(ws.receive_text())
                assert created["type"] == "session.created"

                with patch("app.auth.time.time", return_value=expires_at + 1):
                    ws.send_text(json.dumps({
                        "type": "sim.getState",
                        "requestId": "expired-command",
                        "gatewayContext": {
                            "mode": "station",
                            "accessKey": "Test.fmu",
                            "claims": {},
                        },
                    }))
                    response = json.loads(ws.receive_text())

                assert response["type"] == "error"
                assert response["code"] == "RESERVATION_NOT_ACTIVE"

    def test_session_expiry_timer_closes_the_station_session(self, client, _isolate_config):
        import time

        expires_at = time.time() + 0.05
        with _mock_fmu_patches(_isolate_config):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "test-secret"},
            ) as ws:
                ws.send_text(json.dumps({
                    "type": "session.create",
                    "requestId": "timer-create",
                    "gatewayContext": {
                        "mode": "station",
                        "accessKey": "Test.fmu",
                        "claims": {"exp": expires_at},
                    },
                }))
                created = json.loads(ws.receive_text())
                assert created["type"] == "session.created"

                closed = json.loads(ws.receive_text())

                assert closed["type"] == "session.closed"
                assert closed["sessionId"] == created["sessionId"]
                assert closed["reason"] == "expired"

    def test_ping_pong_without_session(self, client):
        with client.websocket_connect(
            "/internal/fmu/sessions",
            headers={"X-Internal-Session-Token": "test-secret"}
        ) as ws:
            ws.send_text(json.dumps({
                "type": "session.ping",
                "requestId": "ping1",
            }))
            resp = json.loads(ws.receive_text())
            # ping requires a session in current implementation, so it should error
            assert resp["requestId"] == "ping1"


# ---------------------------------------------------------------------------
# FMU listing
# ---------------------------------------------------------------------------

class TestFmuListing:
    def test_list_empty(self, _isolate_config):
        from app import fmu_storage
        assert fmu_storage.list_fmus() == []

    def test_list_detects_fmu_files(self, _isolate_config):
        fmu_root = _isolate_config
        (fmu_root / "Model1.fmu").write_bytes(b"PK")
        (fmu_root / "Model2.fmu").write_bytes(b"PK")
        (fmu_root / "readme.txt").write_text("not an fmu")

        from app import fmu_storage
        keys = fmu_storage.list_fmus()
        assert "Model1.fmu" in keys
        assert "Model2.fmu" in keys
        assert "readme.txt" not in keys

    def test_list_detects_fmu_in_subdirs(self, _isolate_config):
        fmu_root = _isolate_config
        sub = fmu_root / "my-model"
        sub.mkdir()
        (sub / "model.fmu").write_bytes(b"PK")

        from app import fmu_storage
        keys = fmu_storage.list_fmus()
        assert "my-model" in keys


# ---------------------------------------------------------------------------
# Engine - OutputSubscription
# ---------------------------------------------------------------------------

class TestOutputSubscription:
    def test_subscription_default_values(self):
        from app.engine import OutputSubscription
        sub = OutputSubscription()
        assert sub.period_ms == 100
        assert sub.max_batch_size == 64
        assert sub.max_hz is None
        assert sub.min_interval_seconds() == 0.1

    def test_subscription_with_max_hz(self):
        from app.engine import OutputSubscription
        sub = OutputSubscription(max_hz=5.0)
        # period_ms=100 → 0.1s, max_hz=5 → 0.2s, max_hz wins
        assert sub.min_interval_seconds() == 0.2

    def test_subscription_hz_dominates(self):
        from app.engine import OutputSubscription
        sub = OutputSubscription(period_ms=10, max_hz=2.0)
        # period_ms=10 → 0.01s, max_hz=2 → 0.5s, 0.5 wins
        assert sub.min_interval_seconds() == 0.5


# ---------------------------------------------------------------------------
# WebSocket – subscribe/unsubscribe
# ---------------------------------------------------------------------------

class TestWebSocketSubscription:
    def test_subscribe_requires_session(self, client):
        with client.websocket_connect(
            "/internal/fmu/sessions",
            headers={"X-Internal-Session-Token": "test-secret"},
        ) as ws:
            ws.send_text(json.dumps({
                "type": "sim.subscribeOutputs",
                "requestId": "req-sub",
                "variables": ["x"],
            }))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "error"
            assert resp["requestId"] == "req-sub"

    def test_unsubscribe_requires_session(self, client):
        with client.websocket_connect(
            "/internal/fmu/sessions",
            headers={"X-Internal-Session-Token": "test-secret"},
        ) as ws:
            ws.send_text(json.dumps({
                "type": "sim.unsubscribeOutputs",
                "requestId": "req-unsub",
            }))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "error"
            assert resp["requestId"] == "req-unsub"

    def test_subscribe_validates_variables_type(self, client, _isolate_config):
        """variables must be a list or None, not a string."""
        fmu_root = _isolate_config
        dummy_fmu = fmu_root / "Test.fmu"
        dummy_fmu.write_bytes(b"PK\x03\x04dummy")

        md = MagicMock()
        md.modelName = "Test"
        md.guid = "{g}"
        md.fmiVersion = "2.0"
        md.coSimulation = MagicMock()
        md.coSimulation.modelIdentifier = "Test"
        md.modelExchange = None
        md.defaultExperiment = MagicMock()
        md.defaultExperiment.startTime = "0"
        md.defaultExperiment.stopTime = "1"
        md.defaultExperiment.stepSize = "0.1"
        md.modelVariables = []

        with patch("app.engine.read_model_description", return_value=md), \
             patch("app.engine.fmpy_extract", return_value=str(fmu_root)), \
             patch("fmpy.read_model_description", return_value=md):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "test-secret"},
            ) as ws:
                ws.send_text(json.dumps({
                    "type": "session.create",
                    "requestId": "req-create",
                    "gatewayContext": {
                        "mode": "station",
                        "accessKey": "Test.fmu",
                        "claims": {},
                    },
                }))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "session.created"

                # Send subscribe with invalid variables (string instead of list)
                ws.send_text(json.dumps({
                    "type": "sim.subscribeOutputs",
                    "requestId": "req-bad-sub",
                    "variables": "not-a-list",
                }))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "error"
                assert resp["requestId"] == "req-bad-sub"


# ---------------------------------------------------------------------------
# Auth - gateway context validation
# ---------------------------------------------------------------------------

class TestGatewayContextValidation:
    def test_expired_claims_rejected(self):
        import time
        from fastapi import HTTPException
        from app.auth import validate_gateway_context
        ctx = {
            "accessKey": "test.fmu",
            "claims": {"exp": time.time() - 100},
        }
        with pytest.raises(HTTPException) as exc_info:
            validate_gateway_context(ctx, "test.fmu")
        assert "RESERVATION_NOT_ACTIVE" in exc_info.value.detail

    def test_nbf_in_future_rejected(self):
        import time
        from fastapi import HTTPException
        from app.auth import validate_gateway_context
        ctx = {
            "accessKey": "test.fmu",
            "claims": {"nbf": time.time() + 3600},
        }
        with pytest.raises(HTTPException) as exc_info:
            validate_gateway_context(ctx, "test.fmu")
        assert "RESERVATION_NOT_ACTIVE" in exc_info.value.detail

    def test_invalid_exp_claim_rejected(self):
        from fastapi import HTTPException
        from app.auth import validate_gateway_context

        ctx = {
            "accessKey": "test.fmu",
            "claims": {"exp": "not-a-timestamp"},
        }
        with pytest.raises(HTTPException) as exc_info:
            validate_gateway_context(ctx, "test.fmu")
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "RESERVATION_NOT_ACTIVE"

    def test_invalid_nbf_claim_rejected(self):
        from fastapi import HTTPException
        from app.auth import validate_gateway_context

        ctx = {
            "accessKey": "test.fmu",
            "claims": {"nbf": "not-a-timestamp"},
        }
        with pytest.raises(HTTPException) as exc_info:
            validate_gateway_context(ctx, "test.fmu")
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "RESERVATION_NOT_ACTIVE"

    def test_access_key_mismatch_rejected(self):
        from fastapi import HTTPException
        from app.auth import validate_gateway_context
        ctx = {
            "accessKey": "other.fmu",
            "claims": {},
        }
        with pytest.raises(HTTPException) as exc_info:
            validate_gateway_context(ctx, "test.fmu")
        assert exc_info.value.status_code == 403

    def test_valid_context_passes(self):
        import time
        from app.auth import validate_gateway_context
        ctx = {
            "accessKey": "test.fmu",
            "claims": {
                "exp": time.time() + 3600,
                "nbf": time.time() - 60,
            },
        }
        # Should not raise
        validate_gateway_context(ctx, "test.fmu")


# ---------------------------------------------------------------------------
# Helper: create a WS session backed by a mock FMU
# ---------------------------------------------------------------------------

def _mock_fmu_patches(fmu_root: Path):
    """Return a context manager that patches fmpy calls for session creation."""
    dummy_fmu = fmu_root / "Test.fmu"
    if not dummy_fmu.exists():
        dummy_fmu.write_bytes(b"PK\x03\x04dummy")

    md = MagicMock()
    md.modelName = "Test"
    md.guid = "{g}"
    md.fmiVersion = "2.0"
    md.coSimulation = MagicMock()
    md.coSimulation.modelIdentifier = "Test"
    md.modelExchange = None
    md.defaultExperiment = MagicMock()
    md.defaultExperiment.startTime = "0"
    md.defaultExperiment.stopTime = "1"
    md.defaultExperiment.stepSize = "0.1"

    out_var = MagicMock()
    out_var.name = "y"
    out_var.valueReference = 1
    out_var.causality = "output"
    out_var.variability = "continuous"
    out_var.type = "Real"
    out_var.start = None
    out_var.unit = None
    out_var.min = None
    out_var.max = None
    md.modelVariables = [out_var]

    from contextlib import ExitStack

    class _Patches:
        def __enter__(self):
            self._stack = ExitStack()
            self._stack.__enter__()
            self._stack.enter_context(
                patch("app.engine.read_model_description", return_value=md)
            )
            self._stack.enter_context(
                patch("app.engine.fmpy_extract", return_value=str(fmu_root))
            )
            self._stack.enter_context(
                patch("fmpy.read_model_description", return_value=md)
            )
            return md
        def __exit__(self, *exc):
            self._stack.__exit__(*exc)

    return _Patches()


def _ws_create_session(ws, request_id: str = "req-create", gateway_context=None):
    """Send session.create and return the response dict."""
    gateway_context = gateway_context or {
        "mode": "station",
        "accessKey": "Test.fmu",
        "claims": {},
    }
    ws.send_text(json.dumps({
        "type": "session.create",
        "requestId": request_id,
        "gatewayContext": gateway_context,
    }))
    resp = json.loads(ws.receive_text())
    assert resp["type"] == "session.created", f"Unexpected: {resp}"
    assert resp["requestId"] == request_id
    return resp


# ---------------------------------------------------------------------------
# WebSocket – session.attach (reattach to existing session)
# ---------------------------------------------------------------------------

class TestSessionAttach:
    def test_attach_after_create(self, client, _isolate_config):
        """After session.create the same WS can send session.attach."""
        with _mock_fmu_patches(_isolate_config):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "test-secret"},
            ) as ws:
                created = _ws_create_session(ws, request_id="attach-create")
                session_id = created["sessionId"]

                ws.send_text(json.dumps({
                    "type": "session.attach",
                    "requestId": "req-attach",
                }))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "session.attached"
                assert resp["sessionId"] == session_id
                assert resp["requestId"] == "req-attach"
                assert "serverTime" in resp

    def test_attach_after_station_websocket_disconnect_preserves_session(self, client, _isolate_config):
        """A new Station WebSocket can reattach to the existing FMU state."""
        from app import engine

        with _mock_fmu_patches(_isolate_config):
            gateway_context = {
                "mode": "station",
                "accessKey": "Test.fmu",
                "claims": {"sub": "user-1", "reservationKey": "res-1"},
            }
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "test-secret"},
            ) as ws:
                created = _ws_create_session(
                    ws,
                    request_id="reconnect-create",
                    gateway_context=gateway_context,
                )
                session_id = created["sessionId"]

            assert engine.get_session(session_id) is not None

            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "test-secret"},
            ) as ws:
                ws.send_text(json.dumps({
                    "type": "session.attach",
                    "requestId": "reconnect-attach",
                    "sessionId": session_id,
                    "gatewayContext": gateway_context,
                }))
                attached = json.loads(ws.receive_text())
                assert attached["type"] == "session.attached"
                assert attached["sessionId"] == session_id

                ws.send_text(json.dumps({
                    "type": "sim.getState",
                    "requestId": "reconnect-state",
                }))
                state = json.loads(ws.receive_text())
                assert state["type"] == "sim.state"
                assert state["state"] == "loaded"

    def test_attach_after_disconnect_rejects_different_reservation_context(self, client, _isolate_config):
        """Reattach must retain the original reservation binding."""
        from app import engine

        with _mock_fmu_patches(_isolate_config):
            original_context = {
                "mode": "station",
                "accessKey": "Test.fmu",
                "claims": {"sub": "user-1", "reservationKey": "res-1"},
            }
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "test-secret"},
            ) as ws:
                created = _ws_create_session(
                    ws,
                    request_id="mismatch-create",
                    gateway_context=original_context,
                )
                session_id = created["sessionId"]

            mismatched_context = {
                **original_context,
                "claims": {"sub": "user-1", "reservationKey": "res-other"},
            }
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "test-secret"},
            ) as ws:
                ws.send_text(json.dumps({
                    "type": "session.attach",
                    "requestId": "mismatch-attach",
                    "sessionId": session_id,
                    "gatewayContext": mismatched_context,
                }))
                response = json.loads(ws.receive_text())
                assert response["type"] == "error"
                assert response["code"] == "FORBIDDEN"

            assert engine.get_session(session_id) is not None

    def test_detached_session_is_removed_after_attach_grace(self, client, _isolate_config, monkeypatch):
        """A disconnected session must not consume capacity indefinitely."""
        from app import config, engine

        monkeypatch.setattr(config, "FMU_ATTACH_GRACE_SECONDS", 0)
        with _mock_fmu_patches(_isolate_config):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "test-secret"},
            ) as ws:
                created = _ws_create_session(ws, request_id="grace-create")
                session_id = created["sessionId"]

            engine.cleanup_expired_sessions()
            assert engine.get_session(session_id) is None

    def test_attach_without_create_fails(self, client):
        """session.attach requires an active session."""
        with client.websocket_connect(
            "/internal/fmu/sessions",
            headers={"X-Internal-Session-Token": "test-secret"},
        ) as ws:
            ws.send_text(json.dumps({
                "type": "session.attach",
                "requestId": "req-attach-no-sess",
            }))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "error"
            assert resp["requestId"] == "req-attach-no-sess"

    def test_stale_websocket_cleanup_cannot_detach_new_attachment(self, _isolate_config):
        """A late disconnect from the old channel must not break a reattach."""
        from app import engine

        session = engine.FmuSession("sess-owner-race", _isolate_config / "Test.fmu")
        engine._sessions[session.session_id] = session
        old_connection = object()
        new_connection = object()

        session.mark_attached(old_connection)
        session.mark_attached(new_connection)
        engine.detach_session(
            session.session_id,
            grace_seconds=120,
            attachment_owner=old_connection,
        )

        assert session._attached is True
        assert session._attachment_owner is new_connection
        assert session.can_attach() is True

        engine.detach_session(
            session.session_id,
            grace_seconds=120,
            attachment_owner=new_connection,
        )
        assert session._attached is False
        assert session._attachment_owner is None

    def test_full_lifecycle_create_attach_terminate(self, client, _isolate_config):
        """Full lifecycle: create → attach → getState → terminate."""
        with _mock_fmu_patches(_isolate_config):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "test-secret"},
            ) as ws:
                created = _ws_create_session(ws, request_id="lifecycle-create")
                sid = created["sessionId"]

                # Attach
                ws.send_text(json.dumps({"type": "session.attach", "requestId": "a"}))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "session.attached"

                # Get state (should be loaded, not yet initialized)
                ws.send_text(json.dumps({"type": "sim.getState", "requestId": "s"}))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "sim.state"
                assert resp["state"] == "loaded"

                # Terminate
                ws.send_text(json.dumps({"type": "session.terminate", "requestId": "t"}))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "session.closed"
                assert resp["sessionId"] == sid


# ---------------------------------------------------------------------------
# Engine – backpressure and dropped outputs
# ---------------------------------------------------------------------------

class TestSubscriptionBackpressure:
    def test_rate_limiting_drops_samples(self):
        """When sample_subscription is called faster than min_interval,
        intermediate samples are collected but only one payload is emitted
        per interval, with a correct 'dropped' count."""
        import time as _time_mod
        from app.engine import OutputSubscription, FmuSession

        session = FmuSession.__new__(FmuSession)
        session.session_id = "bp-test"
        session.fmu_path = Path("dummy.fmu")
        session._md = MagicMock()
        session._slave = MagicMock()
        session._time = 1.0
        session._initialised = True
        session._terminated = False
        session._step_size = 0.01
        session.seq = 0
        session._pending_samples = []
        session._pending_queue_drops = 0

        out_var = MagicMock()
        out_var.name = "y"
        out_var.valueReference = 1
        out_var.causality = "output"
        out_var.type = "Real"
        session._md.modelVariables = [out_var]
        session._slave.getReal.return_value = [42.0]

        # Subscribe at 10 Hz → min interval 0.1s
        session.subscription = OutputSubscription(
            variables=None,
            period_ms=100,
            max_batch_size=64,
        )
        # Force last_emit to 0 so first call emits
        session.subscription.last_emit_monotonic = 0.0

        # First call should emit (monotonic clock is well past 0)
        result = session.sample_subscription()
        assert result is not None
        assert result["type"] == "sim.outputs"
        assert result["seq"] == 0
        assert result["dropped"] == 0

        # Immediately call again — should be rate-limited (returns None)
        session.subscription.last_emit_monotonic = _time_mod.monotonic()
        result2 = session.sample_subscription()
        assert result2 is None

        # And again
        session.subscription.last_emit_monotonic = _time_mod.monotonic()
        result3 = session.sample_subscription()
        assert result3 is None

        # Simulate time passing beyond the interval
        session.subscription.last_emit_monotonic = _time_mod.monotonic() - 0.2

        # Now should emit with exactly the two rate-limited calls dropped
        result4 = session.sample_subscription()
        assert result4 is not None
        assert result4["seq"] == 1
        assert result4["dropped"] == 2  # exactly the 2 rate-limited calls

    def test_max_batch_size_enforced(self):
        """When pending samples exceed max_batch_size, oldest are dropped."""
        from app.engine import OutputSubscription, FmuSession

        session = FmuSession.__new__(FmuSession)
        session.session_id = "batch-test"
        session.fmu_path = Path("dummy.fmu")
        session._md = MagicMock()
        session._slave = MagicMock()
        session._time = 0.0
        session._initialised = True
        session._terminated = False
        session._step_size = 0.01
        session.seq = 0
        session._pending_samples = []
        session._pending_queue_drops = 0

        out_var = MagicMock()
        out_var.name = "y"
        out_var.valueReference = 1
        out_var.causality = "output"
        out_var.type = "Real"
        session._md.modelVariables = [out_var]
        session._slave.getReal.return_value = [0.0]

        # Subscribe with max_batch_size=3, very fast rate to never rate-limit
        session.subscription = OutputSubscription(
            variables=None,
            period_ms=1,
            max_batch_size=3,
        )
        session.subscription.last_emit_monotonic = 0.0

        # First call emits immediately
        r1 = session.sample_subscription()
        assert r1 is not None

        # Now prevent emission by setting last_emit to now
        import time as _time_mod
        session.subscription.last_emit_monotonic = _time_mod.monotonic()

        # Accumulate 5 samples without emission
        for _ in range(5):
            session.sample_subscription()

        # Pending list should be capped at max_batch_size=3
        assert len(session._pending_samples) <= 3

    def test_subscription_variable_filter(self):
        """Only 'output' variables matching the filter are sampled."""
        from app.engine import OutputSubscription, FmuSession

        session = FmuSession.__new__(FmuSession)
        session.session_id = "filter-test"
        session.fmu_path = Path("dummy.fmu")
        session._md = MagicMock()
        session._slave = MagicMock()
        session._time = 0.5
        session._initialised = True
        session._terminated = False
        session._step_size = 0.01
        session.seq = 0
        session._pending_samples = []
        session._pending_queue_drops = 0

        out_y = MagicMock()
        out_y.name = "y"
        out_y.valueReference = 1
        out_y.causality = "output"
        out_y.type = "Real"

        out_z = MagicMock()
        out_z.name = "z"
        out_z.valueReference = 2
        out_z.causality = "output"
        out_z.type = "Real"

        param_x = MagicMock()
        param_x.name = "x"
        param_x.valueReference = 3
        param_x.causality = "parameter"
        param_x.type = "Real"

        session._md.modelVariables = [out_y, out_z, param_x]
        session._slave.getReal.return_value = [99.0]

        # Subscribe to only "y"
        session.subscription = OutputSubscription(
            variables=["y"],
            period_ms=1,
            max_batch_size=64,
        )
        session.subscription.last_emit_monotonic = 0.0

        result = session.sample_subscription()
        assert result is not None
        # getReal should have been called with [1] (y's ref), not [2] (z) or [3] (x)
        session._slave.getReal.assert_called_with([1])


# ---------------------------------------------------------------------------
# FMU validation and quarantine
# ---------------------------------------------------------------------------

class TestFmuValidation:
    def test_validate_missing_fmu(self, client, auth_headers):
        resp = client.post("/internal/fmu/validate/nonexistent.fmu", headers=auth_headers)
        body = resp.json()
        assert body["valid"] is False
        assert "not found" in body["reason"].lower()

    def test_validate_valid_fmu(self, client, _isolate_config, auth_headers):
        fmu_root = _isolate_config
        fmu_file = fmu_root / "Good.fmu"
        fmu_file.write_bytes(b"PK\x03\x04dummy")

        md = MagicMock()
        md.coSimulation = MagicMock()
        md.guid = "{valid-guid}"

        with patch("fmpy.read_model_description", return_value=md):
            resp = client.post("/internal/fmu/validate/Good.fmu", headers=auth_headers)
        body = resp.json()
        assert body["valid"] is True
        assert body["reason"] == ""

    def test_validate_unparseable_auto_quarantine(self, client, _isolate_config, auth_headers):
        """An unparseable FMU with auto_quarantine=true gets quarantined."""
        fmu_root = _isolate_config
        fmu_file = fmu_root / "Broken.fmu"
        fmu_file.write_bytes(b"not-a-zip")

        resp = client.post(
            "/internal/fmu/validate/Broken.fmu?auto_quarantine=true",
            headers=auth_headers,
        )
        body = resp.json()
        assert body["valid"] is False
        assert "Broken.fmu" == body["accessKey"]

        # Should now be quarantined
        from app import fmu_storage
        assert fmu_storage.is_quarantined("Broken.fmu")

    def test_validate_no_cosim_support(self, client, _isolate_config, auth_headers):
        fmu_root = _isolate_config
        fmu_file = fmu_root / "NoCS.fmu"
        fmu_file.write_bytes(b"PK\x03\x04dummy")

        md = MagicMock()
        md.coSimulation = None
        md.guid = "{noCS}"

        with patch("fmpy.read_model_description", return_value=md):
            resp = client.post("/internal/fmu/validate/NoCS.fmu", headers=auth_headers)
        body = resp.json()
        assert body["valid"] is False
        assert "Co-Simulation" in body["reason"]

    def test_validate_does_not_expose_exception_details(
        self, client, _isolate_config, auth_headers
    ):
        (_isolate_config / "Broken.fmu").write_bytes(b"not-a-zip")
        with patch(
            "fmpy.read_model_description",
            side_effect=RuntimeError("secret path C:\\private\\model.fmu"),
        ):
            resp = client.post(
                "/internal/fmu/validate/Broken.fmu",
                headers=auth_headers,
            )

        assert resp.status_code == 200
        assert resp.json()["reason"] == "Cannot parse model description"
        assert "private" not in resp.text


class TestQuarantine:
    def test_quarantine_and_list(self, client, _isolate_config, auth_headers):
        fmu_root = _isolate_config
        (fmu_root / "Bad.fmu").write_bytes(b"PK")

        # Quarantine
        resp = client.post(
            "/internal/fmu/quarantine/Bad.fmu?reason=manual-test",
            headers=auth_headers,
        )
        assert resp.json()["quarantined"] is True

        # List should include it
        resp = client.get("/internal/fmu/quarantine", headers=auth_headers)
        items = resp.json()["quarantined"]
        keys = [i["accessKey"] for i in items]
        assert "Bad.fmu" in keys

        # Catalog should exclude it
        from app import fmu_storage
        assert "Bad.fmu" not in fmu_storage.list_fmus()
        assert not fmu_storage.fmu_exists("Bad.fmu")

    def test_unquarantine_restores(self, client, _isolate_config, auth_headers):
        fmu_root = _isolate_config
        (fmu_root / "Restore.fmu").write_bytes(b"PK")

        # Quarantine then restore
        client.post("/internal/fmu/quarantine/Restore.fmu?reason=test", headers=auth_headers)

        from app import fmu_storage
        assert fmu_storage.is_quarantined("Restore.fmu")

        resp = client.delete("/internal/fmu/quarantine/Restore.fmu", headers=auth_headers)
        assert resp.json()["restored"] is True
        assert not fmu_storage.is_quarantined("Restore.fmu")

    def test_nested_directory_quarantine_roundtrip(self, client, _isolate_config, auth_headers):
        model_dir = _isolate_config / "provider"
        model_dir.mkdir()
        (model_dir / "demo.fmu").write_bytes(b"PK")

        resp = client.post(
            "/internal/fmu/quarantine/provider?reason=nested",
            headers=auth_headers,
        )
        assert resp.json()["quarantined"] is True

        from app import fmu_storage
        assert not fmu_storage.fmu_exists("provider")

        resp = client.delete(
            "/internal/fmu/quarantine/provider",
            headers=auth_headers,
        )
        assert resp.json()["restored"] is True
        assert fmu_storage.fmu_exists("provider")

    def test_health_includes_quarantine_count(self, client, _isolate_config, auth_headers):
        fmu_root = _isolate_config
        (fmu_root / "Q1.fmu").write_bytes(b"PK")

        # Before quarantine
        resp = client.get("/internal/health")
        assert resp.json()["quarantinedCount"] == 0

        client.post("/internal/fmu/quarantine/Q1.fmu?reason=test", headers=auth_headers)
        resp = client.get("/internal/health")
        assert resp.json()["quarantinedCount"] >= 1
