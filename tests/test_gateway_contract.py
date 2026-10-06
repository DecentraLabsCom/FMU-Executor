"""Gateway ↔ Station integration contract tests.

These tests validate that the Station executor's API responses match the exact
shapes the Gateway's StationFmuBackend and StationRealtimeWsProxyManager expect.

The tests run against the Station's FastAPI TestClient — no real Gateway or
network is involved.  This ensures that any future change on either side that
would break the contract is caught immediately.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures (same isolation as test_executor.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path):
    fmu_root = tmp_path / "fmu-data"
    fmu_root.mkdir()
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()

    with patch.dict(os.environ, {
        "FMU_ROOT": str(fmu_root),
        "FMU_EXECUTOR_TEMP": str(temp_dir),
        "FMU_INTERNAL_TOKEN": "station-shared-secret",
        "FMU_MAX_SESSIONS": "4",
        "FMU_LOG_LEVEL": "WARNING",
    }):
        import importlib
        from app import config as cfg
        importlib.reload(cfg)
        try:
            yield fmu_root
        finally:
            from app.engine import terminate_all
            terminate_all()
            from app.fmu_storage import _quarantine_cache
            _quarantine_cache.clear()


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


@pytest.fixture
def headers():
    return {"X-Internal-Session-Token": "station-shared-secret"}


def _make_mock_md():
    md = MagicMock()
    md.modelName = "BouncingBall"
    md.guid = "{bb-guid-1234}"
    md.fmiVersion = "2.0"
    md.coSimulation = MagicMock()
    md.coSimulation.modelIdentifier = "BouncingBall"
    md.modelExchange = None
    md.defaultExperiment = MagicMock()
    md.defaultExperiment.startTime = "0.0"
    md.defaultExperiment.stopTime = "5.0"
    md.defaultExperiment.stepSize = "0.01"

    out_h = MagicMock()
    out_h.name = "h"
    out_h.valueReference = 1
    out_h.causality = "output"
    out_h.variability = "continuous"
    out_h.type = "Real"
    out_h.start = "10.0"
    out_h.unit = "m"
    out_h.min = None
    out_h.max = None

    param_g = MagicMock()
    param_g.name = "g"
    param_g.valueReference = 2
    param_g.causality = "parameter"
    param_g.variability = "fixed"
    param_g.type = "Real"
    param_g.start = "9.81"
    param_g.unit = "m/s2"
    param_g.min = None
    param_g.max = None

    md.modelVariables = [out_h, param_g]
    return md


def _provision_fmu(fmu_root: Path, name: str = "BouncingBall.fmu"):
    fmu = fmu_root / name
    fmu.write_bytes(b"PK\x03\x04dummy-fmu-archive")
    return fmu


# ===================================================================
# 1. Health endpoint contract
# ===================================================================

class TestHealthContract:
    """Gateway StationFmuBackend.health() checks:
    - ``payload.get("status").upper() == "UP"``
    - ``int(payload.get("fmuCount") or 0)``
    """

    def test_health_status_is_up(self, client):
        resp = client.get("/internal/health")
        assert resp.status_code == 200
        body = resp.json()
        assert str(body.get("status") or "").upper() == "UP", \
            f"Gateway expects status='UP', got {body.get('status')!r}"

    def test_health_fmu_count_is_int(self, client):
        body = client.get("/internal/health").json()
        assert isinstance(body.get("fmuCount"), int)

    def test_health_includes_quarantine_count(self, client):
        body = client.get("/internal/health").json()
        assert "quarantinedCount" in body


# ===================================================================
# 2. Catalog endpoint contract
# ===================================================================

class TestCatalogContract:
    """Gateway StationFmuBackend.list_authorized_fmu() checks:
    - 200 response has ``fmus`` as a list
    - Each entry is a dict with at least ``filename``, ``path``
    - ``source`` defaults to ``"station"`` if missing
    """

    def test_catalog_returns_fmus_list(self, client, headers, _isolate_config):
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("fmpy.read_model_description", return_value=md):
            resp = client.get("/internal/fmu/catalog", headers={**headers, "X-FMU-Access-Key": "BouncingBall.fmu"})
        assert resp.status_code == 200
        body = resp.json()
        fmus = body.get("fmus")
        assert isinstance(fmus, list), f"Gateway expects 'fmus' list, got {type(fmus)}"
        assert len(fmus) >= 1

    def test_catalog_entry_has_filename_and_path(self, client, headers, _isolate_config):
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("fmpy.read_model_description", return_value=md):
            body = client.get("/internal/fmu/catalog", headers={**headers, "X-FMU-Access-Key": "BouncingBall.fmu"}).json()
        entry = body["fmus"][0]
        assert "filename" in entry, "Gateway reads entry['filename']"
        assert "path" in entry, "Gateway reads entry['path']"

    def test_catalog_entry_has_source_station(self, client, headers, _isolate_config):
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("fmpy.read_model_description", return_value=md):
            body = client.get("/internal/fmu/catalog", headers={**headers, "X-FMU-Access-Key": "BouncingBall.fmu"}).json()
        entry = body["fmus"][0]
        assert entry.get("source") == "station"

    def test_catalog_404_when_missing(self, client, headers):
        resp = client.get("/internal/fmu/catalog", headers={**headers, "X-FMU-Access-Key": "nonexistent.fmu"})
        assert resp.status_code == 404


# ===================================================================
# 3. Describe endpoint contract
# ===================================================================

class TestDescribeContract:
    """Gateway StationFmuBackend._normalize_model_metadata() requires:
    - ``modelVariables`` as a list of dicts
    - Each variable has ``name``, ``type``, ``causality``, ``variability``, ``valueReference``
    - Top-level: ``modelName``, ``guid``, ``fmiVersion``,
      ``supportsCoSimulation``, ``simulationKind``, ``simulationType``
    - Optional: ``defaultStartTime``, ``defaultStopTime``, ``defaultStepSize``
    """

    def test_describe_required_fields(self, client, headers, _isolate_config):
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("fmpy.read_model_description", return_value=md):
            resp = client.get("/internal/fmu/describe", headers={**headers, "X-FMU-Access-Key": "BouncingBall.fmu"})
        body = resp.json()
        for field in ("modelName", "guid", "fmiVersion", "supportsCoSimulation",
                       "simulationKind", "simulationType", "modelVariables"):
            assert field in body, f"Missing required field {field!r}"

    def test_describe_variables_shape(self, client, headers, _isolate_config):
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("fmpy.read_model_description", return_value=md):
            body = client.get("/internal/fmu/describe", headers={**headers, "X-FMU-Access-Key": "BouncingBall.fmu"}).json()

        variables = body["modelVariables"]
        assert isinstance(variables, list)
        assert len(variables) >= 1

        for var in variables:
            assert isinstance(var, dict)
            assert "name" in var
            assert "valueReference" in var
            assert isinstance(var["valueReference"], int)
            assert "causality" in var
            assert "variability" in var

    def test_describe_optional_default_experiment(self, client, headers, _isolate_config):
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("fmpy.read_model_description", return_value=md):
            body = client.get("/internal/fmu/describe", headers={**headers, "X-FMU-Access-Key": "BouncingBall.fmu"}).json()
        assert isinstance(body.get("defaultStartTime"), (int, float))
        assert isinstance(body.get("defaultStopTime"), (int, float))
        assert isinstance(body.get("defaultStepSize"), (int, float))


# ===================================================================
# 4. Auth header contract
# ===================================================================

class TestAuthHeaderContract:
    """Gateway sends X-Internal-Session-Token header on all REST and WS requests."""

    def test_rest_requires_token(self, client):
        resp = client.get("/internal/fmu/describe", headers={"X-FMU-Access-Key": "any.fmu"})
        assert resp.status_code == 401

    def test_rest_rejects_wrong_token(self, client):
        resp = client.get("/internal/fmu/describe",
                          headers={"X-Internal-Session-Token": "wrong", "X-FMU-Access-Key": "any.fmu"})
        assert resp.status_code == 401

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


# ===================================================================
# 5. WS session.create / session.created contract
# ===================================================================

class TestWsSessionCreateContract:
    """Gateway proxy reads:
    - ``type == "session.created"``
    - ``sessionId`` (non-empty string)
    - ``expiresAt`` (epoch or None)
    from the Station response to ``session.create``.
    It sends ``gatewayContext`` with ``mode``, ``accessKey``, ``claims``.
    """

    def test_session_created_response_shape(self, client, _isolate_config):
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("app.engine.read_model_description", return_value=md), \
             patch("app.engine.fmpy_extract", return_value=str(_isolate_config)), \
             patch("fmpy.read_model_description", return_value=md):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "station-shared-secret"},
            ) as ws:
                ws.send_text(json.dumps({
                    "type": "session.create",
                    "requestId": "gw-req-1",
                    "gatewayContext": {
                        "mode": "station",
                        "accessKey": "BouncingBall.fmu",
                        "claims": {
                            "sub": "user-1",
                            "labId": "lab-1",
                            "accessKey": "BouncingBall.fmu",
                            "reservationKey": "res-1",
                            "exp": int(time.time()) + 3600,
                        },
                        "labId": "lab-1",
                        "reservationKey": "res-1",
                    },
                }))
                resp = json.loads(ws.receive_text())

                assert resp["type"] == "session.created"
                assert resp["requestId"] == "gw-req-1"

                sid = resp.get("sessionId")
                assert isinstance(sid, str) and len(sid) > 0, "sessionId must be non-empty string"

                assert "serverTime" in resp
                assert "expiresAt" in resp  # may be None
                assert "capabilities" in resp


# ===================================================================
# 6. WS session.attach / session.attached contract
# ===================================================================

class TestWsSessionAttachContract:
    """Gateway proxy reads:
    - ``type == "session.attached"``
    - ``sessionId`` (non-empty string)
    """

    def test_session_attached_response_shape(self, client, _isolate_config):
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("app.engine.read_model_description", return_value=md), \
             patch("app.engine.fmpy_extract", return_value=str(_isolate_config)), \
             patch("fmpy.read_model_description", return_value=md):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "station-shared-secret"},
            ) as ws:
                # Create
                ws.send_text(json.dumps({
                    "type": "session.create",
                    "requestId": "c1",
                    "gatewayContext": {
                        "mode": "station",
                        "accessKey": "BouncingBall.fmu",
                        "claims": {},
                    },
                }))
                created = json.loads(ws.receive_text())
                assert created["type"] == "session.created"

                # Attach
                ws.send_text(json.dumps({
                    "type": "session.attach",
                    "requestId": "a1",
                }))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "session.attached"
                assert resp["requestId"] == "a1"
                assert isinstance(resp.get("sessionId"), str) and len(resp["sessionId"]) > 0
                assert "serverTime" in resp


# ===================================================================
# 7. WS session.closed contract (on terminate)
# ===================================================================

class TestWsSessionClosedContract:
    """Gateway proxy cleans up internal sessions when it sees ``type == "session.closed"``.
    The Station must return ``session.closed`` (not ``session.terminated``)
    so the Gateway can clean up its ``_sessions`` dict.
    """

    def test_terminate_returns_session_closed(self, client, _isolate_config):
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("app.engine.read_model_description", return_value=md), \
             patch("app.engine.fmpy_extract", return_value=str(_isolate_config)), \
             patch("fmpy.read_model_description", return_value=md):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "station-shared-secret"},
            ) as ws:
                ws.send_text(json.dumps({
                    "type": "session.create",
                    "requestId": "c1",
                    "gatewayContext": {
                        "mode": "station",
                        "accessKey": "BouncingBall.fmu",
                        "claims": {},
                    },
                }))
                created = json.loads(ws.receive_text())
                sid = created["sessionId"]

                ws.send_text(json.dumps({
                    "type": "session.terminate",
                    "requestId": "t1",
                }))
                resp = json.loads(ws.receive_text())

                assert resp["type"] == "session.closed", \
                    f"Gateway expects 'session.closed', got {resp['type']!r}"
                assert resp["sessionId"] == sid
                assert resp["requestId"] == "t1"


# ===================================================================
# 8. WS error payload contract
# ===================================================================

class TestWsErrorContract:
    """Gateway's error_payload() always includes:
    - ``type``: ``"error"``
    - ``code``: short code string
    - ``message``: human readable
    - ``retryable``: bool
    Station errors must include all four fields.
    """

    def test_error_has_required_fields(self, client):
        with client.websocket_connect(
            "/internal/fmu/sessions",
            headers={"X-Internal-Session-Token": "station-shared-secret"},
        ) as ws:
            # Send invalid command to trigger an error
            ws.send_text(json.dumps({
                "type": "sim.step",
                "requestId": "err-test",
            }))
            resp = json.loads(ws.receive_text())

            assert resp["type"] == "error"
            assert "code" in resp, "Error must have 'code'"
            assert "message" in resp, "Error must have 'message'"
            assert "retryable" in resp, "Error must have 'retryable'"
            assert isinstance(resp["retryable"], bool)
            assert resp["requestId"] == "err-test"

    def test_error_code_is_short_code(self, client):
        """code should be a short identifier, not a full sentence."""
        with client.websocket_connect(
            "/internal/fmu/sessions",
            headers={"X-Internal-Session-Token": "station-shared-secret"},
        ) as ws:
            ws.send_text(json.dumps({
                "type": "session.create",
                "requestId": "err-no-ctx",
            }))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "error"
            code = resp["code"]
            # Code should not contain sentences — it should be a short token
            assert " – " not in code, f"code contains long detail: {code!r}"
            assert " - " not in code and len(code) < 30, f"code too long: {code!r}"

    def test_fmu_not_found_error(self, client):
        with client.websocket_connect(
            "/internal/fmu/sessions",
            headers={"X-Internal-Session-Token": "station-shared-secret"},
        ) as ws:
            ws.send_text(json.dumps({
                "type": "session.create",
                "requestId": "err-404",
                "gatewayContext": {
                    "mode": "station",
                    "accessKey": "nonexistent.fmu",
                    "claims": {},
                },
            }))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "error"
            assert resp["code"] == "FMU_NOT_FOUND"
            assert "message" in resp
            assert isinstance(resp["retryable"], bool)


# ===================================================================
# 9. WS ping/pong contract
# ===================================================================

class TestWsPingPongContract:
    """Gateway proxy forwards ping messages through. Station should respond
    with ``session.pong`` and a ``serverTime`` field."""

    def test_ping_pong(self, client, _isolate_config):
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("app.engine.read_model_description", return_value=md), \
             patch("app.engine.fmpy_extract", return_value=str(_isolate_config)), \
             patch("fmpy.read_model_description", return_value=md):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "station-shared-secret"},
            ) as ws:
                # Create session first (ping requires a session in some paths)
                ws.send_text(json.dumps({
                    "type": "session.create",
                    "requestId": "c1",
                    "gatewayContext": {
                        "mode": "station",
                        "accessKey": "BouncingBall.fmu",
                        "claims": {},
                    },
                }))
                json.loads(ws.receive_text())  # consume created

                ws.send_text(json.dumps({
                    "type": "session.ping",
                    "requestId": "p1",
                }))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "session.pong"
                assert resp["requestId"] == "p1"
                assert "serverTime" in resp


# ===================================================================
# 10. WS sim.outputs (subscription) pass-through contract
# ===================================================================

class TestWsSimOutputsContract:
    """Gateway proxy passes synchronous and subscribed ``sim.outputs`` messages.
    Subscription events must include: ``type``, ``sessionId``, ``seq``,
    ``dropped``, ``simTime``, and ``values``.
    """

    def test_outputs_shape_from_get_outputs(self, client, _isolate_config):
        """Test via sim.getOutputs (synchronous) — validates the per-message shape."""
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        mock_fmu = MagicMock()
        mock_fmu.getReal.return_value = [3.14]

        with patch("app.engine.read_model_description", return_value=md), \
             patch("app.engine.fmpy_extract", return_value=str(_isolate_config)), \
             patch("fmpy.read_model_description", return_value=md), \
             patch("app.engine.fmpy_instantiate_fmu", return_value=mock_fmu):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "station-shared-secret"},
            ) as ws:
                # Create
                ws.send_text(json.dumps({
                    "type": "session.create",
                    "requestId": "c1",
                    "gatewayContext": {
                        "mode": "station",
                        "accessKey": "BouncingBall.fmu",
                        "claims": {},
                    },
                }))
                json.loads(ws.receive_text())

                # Initialize
                ws.send_text(json.dumps({
                    "type": "sim.initialize",
                    "requestId": "i1",
                    "options": {"startTime": 0, "stopTime": 5, "stepSize": 0.01},
                }))
                init_resp = json.loads(ws.receive_text())
                assert init_resp["type"] == "sim.state"
                assert init_resp["state"] == "initialized"

                # Get outputs
                ws.send_text(json.dumps({
                    "type": "sim.getOutputs",
                    "requestId": "o1",
                }))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "sim.outputs"
                assert "simTime" in resp
                assert "values" in resp
                assert isinstance(resp["values"], dict)

    def test_get_outputs_reports_missing_model_description(self, client, _isolate_config):
        """A selected-output request must report an unloaded model explicitly."""
        from app import engine

        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        with patch("app.engine.read_model_description", return_value=md), \
             patch("app.engine.fmpy_extract", return_value=str(_isolate_config)), \
             patch("fmpy.read_model_description", return_value=md):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "station-shared-secret"},
            ) as ws:
                ws.send_text(json.dumps({
                    "type": "session.create",
                    "requestId": "missing-model-create",
                    "gatewayContext": {
                        "mode": "station",
                        "accessKey": "BouncingBall.fmu",
                        "claims": {},
                    },
                }))
                created = json.loads(ws.receive_text())
                assert created["type"] == "session.created"
                session = engine.get_session(created["sessionId"])
                assert session is not None
                session._md = None

                ws.send_text(json.dumps({
                    "type": "sim.getOutputs",
                    "requestId": "missing-model-outputs",
                    "variables": ["x"],
                }))
                response = json.loads(ws.receive_text())

                assert response["type"] == "error"
                assert response["code"] == "MODEL_DESCRIPTION_NOT_LOADED"

    def test_subscription_outputs_shape(self, client, _isolate_config):
        """Subscription events expose the fields consumed by the Gateway proxy."""
        _provision_fmu(_isolate_config)
        md = _make_mock_md()
        mock_fmu = MagicMock()
        mock_fmu.getReal.return_value = [3.14]

        with patch("app.engine.read_model_description", return_value=md), \
             patch("app.engine.fmpy_extract", return_value=str(_isolate_config)), \
             patch("fmpy.read_model_description", return_value=md), \
             patch("app.engine.fmpy_instantiate_fmu", return_value=mock_fmu):
            with client.websocket_connect(
                "/internal/fmu/sessions",
                headers={"X-Internal-Session-Token": "station-shared-secret"},
            ) as ws:
                ws.send_text(json.dumps({
                    "type": "session.create",
                    "requestId": "c1",
                    "gatewayContext": {
                        "mode": "station",
                        "accessKey": "BouncingBall.fmu",
                        "claims": {},
                    },
                }))
                created = json.loads(ws.receive_text())
                session_id = created["sessionId"]

                ws.send_text(json.dumps({
                    "type": "sim.initialize",
                    "requestId": "i1",
                    "options": {"startTime": 0, "stopTime": 5, "stepSize": 0.01},
                }))
                init_resp = json.loads(ws.receive_text())
                assert init_resp["type"] == "sim.state"
                assert init_resp["state"] == "initialized"

                ws.send_text(json.dumps({
                    "type": "sim.subscribeOutputs",
                    "requestId": "s1",
                    "periodMs": 100,
                }))

                subscribed = None
                output = None
                while subscribed is None:
                    candidate = json.loads(ws.receive_text())
                    if candidate["type"] == "sim.subscribed":
                        subscribed = candidate
                    else:
                        assert candidate["type"] == "sim.outputs"
                        output = candidate

                assert subscribed["requestId"] == "s1"
                assert subscribed["sessionId"] == session_id

                if output is None:
                    output = json.loads(ws.receive_text())

                assert output["type"] == "sim.outputs"
                assert output["sessionId"] == session_id
                assert isinstance(output["seq"], int)
                assert isinstance(output["dropped"], int)
                assert isinstance(output["simTime"], (int, float))
                assert isinstance(output["values"], dict)


# ===================================================================
# 11. Run / Stream simulation contract (REST)
# ===================================================================

class TestRunSimulationContract:
    """Gateway's StationFmuBackend._json_payload_for_station() sends:
    ``{accessKey, claims, parameters, options, labId?, reservationKey?}``

    Station must accept this shape and return a valid simulation result.
    """

    def test_run_404_when_fmu_missing(self, client, headers):
        resp = client.post(
            "/internal/fmu/simulations/run",
            headers=headers,
            json={
                "accessKey": "nonexistent.fmu",
                "claims": {},
                "parameters": {},
                "options": {},
            },
        )
        assert resp.status_code == 404

    def test_stream_404_when_fmu_missing(self, client, headers):
        resp = client.post(
            "/internal/fmu/simulations/stream",
            headers=headers,
            json={
                "accessKey": "nonexistent.fmu",
                "claims": {},
                "parameters": {},
                "options": {},
            },
        )
        assert resp.status_code == 404

    def test_health_exposes_effective_execution_capacity(self, client, headers):
        resp = client.get("/internal/health", headers=headers)

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["maxConcurrentExecutions"] == payload["maxSessions"]
        assert payload["availableCapacity"] == payload["maxConcurrentExecutions"] - payload["activeExecutions"]

    def test_capacity_endpoint_exposes_effective_execution_capacity(self, client, headers):
        resp = client.get("/internal/fmu/capacity", headers=headers)

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["capacity"] == payload["maxConcurrentExecutions"]
        assert payload["available"] == payload["capacity"] - payload["active"]
