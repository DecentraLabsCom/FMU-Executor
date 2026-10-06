from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import config, engine, fmu_storage, main, process_runner


class FakeSlot:
    session_id = "route-session"

    def __init__(self):
        self.calls = []

    def load(self): self.calls.append(("load",))
    def initialize(self, **kwargs): self.calls.append(("initialize", kwargs))
    def run_until(self, target, step_size=None): return {"time": target}
    def get_outputs(self): return {"outputs": {"result": 12}}
    def run_until_streaming(self, target, step_size=None): return iter([{"type": "sim.outputs", "simTime": 0.5}])


@pytest.fixture
def route_client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "internal_token", lambda: "route-test-token")
    monkeypatch.setattr(config, "FMU_ROOT", tmp_path / "fmus")
    monkeypatch.setattr(config, "TEMP_DIR", tmp_path / "tmp")
    config.FMU_ROOT.mkdir()
    config.TEMP_DIR.mkdir()
    return TestClient(main.app), {"X-Internal-Session-Token": "route-test-token"}


def install_fake_fmu(monkeypatch, tmp_path):
    path = tmp_path / "demo.fmu"
    path.write_bytes(b"PK\x03\x04test")
    monkeypatch.setattr(fmu_storage, "fmu_exists", lambda key: key == "demo.fmu")
    monkeypatch.setattr(fmu_storage, "get_fmu_path", lambda key: path)
    return path


@pytest.mark.parametrize(("backend", "status"), [("unknown", 400), ("omsimulator", 501)])
def test_simulation_routes_reject_unavailable_backends_before_allocating(route_client, monkeypatch, tmp_path, backend, status):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    create = Mock(side_effect=AssertionError("session should not be allocated"))
    monkeypatch.setattr(engine, "create_session", create)

    response = client.post(
        "/internal/fmu/simulations/run",
        headers=headers,
        json={"accessKey": "demo.fmu", "options": {"backend": backend}},
    )

    assert response.status_code == status
    create.assert_not_called()


def test_run_route_returns_process_result_and_releases_capacity(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    fmu_path = install_fake_fmu(monkeypatch, tmp_path)
    slot = FakeSlot()
    removed = Mock()
    monkeypatch.setattr(engine, "create_session", lambda _path: slot)
    monkeypatch.setattr(engine, "remove_session", removed)
    process_run = Mock(return_value={"type": "sim.result", "time": 1.5, "outputs": {"x": 2}})
    monkeypatch.setattr(process_runner, "run", process_run)
    monkeypatch.setattr(config, "execution_mode", lambda: "process")

    response = client.post(
        "/internal/fmu/simulations/run",
        headers=headers,
        json={"accessKey": "demo.fmu", "parameters": {"x": 2}, "options": {"stopTime": 1.5}},
    )

    assert response.status_code == 200
    assert response.json() == {"type": "sim.result", "time": 1.5, "outputs": {"x": 2}}
    process_run.assert_called_once_with(fmu_path, access_key="demo.fmu", parameters={"x": 2}, options={"stopTime": 1.5})
    removed.assert_called_once_with(slot.session_id)


@pytest.mark.parametrize(("error", "expected_status"), [(process_runner.ProcessExecutionError("failed"), 500), (process_runner.ProcessExecutionError("timed out", code="FMU_EXECUTION_TIMEOUT", retryable=True), 504)])
def test_run_route_maps_worker_errors_and_still_releases_capacity(route_client, monkeypatch, tmp_path, error, expected_status):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    slot = FakeSlot()
    removed = Mock()
    monkeypatch.setattr(engine, "create_session", lambda _path: slot)
    monkeypatch.setattr(engine, "remove_session", removed)
    monkeypatch.setattr(process_runner, "run", Mock(side_effect=error))
    monkeypatch.setattr(config, "execution_mode", lambda: "process")

    response = client.post("/internal/fmu/simulations/run", headers=headers, json={"accessKey": "demo.fmu"})

    assert response.status_code == expected_status
    removed.assert_called_once_with(slot.session_id)


def test_run_route_rejects_exhausted_capacity(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    monkeypatch.setattr(engine, "create_session", Mock(side_effect=engine.CapacityExceededError("full")))

    response = client.post("/internal/fmu/simulations/run", headers=headers, json={"accessKey": "demo.fmu"})

    assert response.status_code == 429
    assert response.json()["detail"] == "STATION_CAPACITY_EXHAUSTED"


def test_run_route_executes_in_process_mode(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    slot = FakeSlot()
    removed = Mock()
    monkeypatch.setattr(engine, "create_session", lambda _path: slot)
    monkeypatch.setattr(engine, "remove_session", removed)
    monkeypatch.setattr(config, "execution_mode", lambda: "in-process")

    response = client.post(
        "/internal/fmu/simulations/run",
        headers=headers,
        json={"accessKey": "demo.fmu", "parameters": {"input": 4}, "options": {"startTime": 0.2, "stopTime": 2, "stepSize": 0.1}},
    )

    assert response.status_code == 200
    assert response.json() == {"type": "sim.result", "time": 2, "state": "terminated", "outputs": {"result": 12}}
    assert slot.calls == [("load",), ("initialize", {"start_time": 0.2, "stop_time": 2.0, "step_size": 0.1, "parameters": {"input": 4}})]
    removed.assert_called_once_with(slot.session_id)


def test_stream_route_yields_ndjson_and_releases_capacity(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    slot = FakeSlot()
    removed = Mock()
    monkeypatch.setattr(engine, "create_session", lambda _path: slot)
    monkeypatch.setattr(engine, "remove_session", removed)
    monkeypatch.setattr(process_runner, "stream", lambda *args, **kwargs: iter([{"type": "sim.outputs", "simTime": 0.5}, {"type": "sim.done", "time": 1}]))
    monkeypatch.setattr(config, "execution_mode", lambda: "process")

    response = client.post("/internal/fmu/simulations/stream", headers=headers, json={"accessKey": "demo.fmu"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert [json.loads(line) for line in response.text.splitlines()] == [
        {"type": "sim.outputs", "simTime": 0.5}, {"type": "sim.done", "time": 1}
    ]
    removed.assert_called_once_with(slot.session_id)


def test_stream_route_reports_capacity_as_ndjson(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    monkeypatch.setattr(engine, "create_session", Mock(side_effect=engine.CapacityExceededError("full")))

    response = client.post("/internal/fmu/simulations/stream", headers=headers, json={"accessKey": "demo.fmu"})

    assert response.status_code == 200
    assert json.loads(response.text) == {
        "type": "error", "code": "STATION_CAPACITY_EXHAUSTED",
        "message": "Station execution capacity is exhausted", "retryable": True,
    }


def test_stream_route_in_process_and_exception_paths_are_closed(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    slot = FakeSlot()
    removed = Mock()
    monkeypatch.setattr(engine, "create_session", lambda _path: slot)
    monkeypatch.setattr(engine, "remove_session", removed)
    monkeypatch.setattr(config, "execution_mode", lambda: "in-process")

    response = client.post("/internal/fmu/simulations/stream", headers=headers, json={"accessKey": "demo.fmu", "options": {"stopTime": 2}})

    assert [json.loads(line) for line in response.text.splitlines()] == [
        {"type": "sim.outputs", "simTime": 0.5}, {"type": "sim.done", "time": 2.0}
    ]
    removed.assert_called_once_with(slot.session_id)

    removed.reset_mock()
    monkeypatch.setattr(config, "execution_mode", lambda: "process")
    monkeypatch.setattr(process_runner, "stream", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret detail")))
    response = client.post("/internal/fmu/simulations/stream", headers=headers, json={"accessKey": "demo.fmu"})
    assert json.loads(response.text) == {"type": "error", "message": "FMU simulation failed"}
    removed.assert_called_once_with(slot.session_id)


def test_quarantine_endpoints_validate_and_delegate(route_client, monkeypatch):
    client, headers = route_client
    validate = Mock(return_value=(False, "corrupt archive"))
    quarantine = Mock()
    unquarantine = Mock(return_value=True)
    monkeypatch.setattr(fmu_storage, "validate_fmu", validate)
    monkeypatch.setattr(fmu_storage, "quarantine", quarantine)
    monkeypatch.setattr(fmu_storage, "unquarantine", unquarantine)
    monkeypatch.setattr(fmu_storage, "list_quarantined", lambda: [{"accessKey": "demo.fmu"}])

    response = client.post("/internal/fmu/validate/demo.fmu?auto_quarantine=true", headers=headers)
    assert response.status_code == 200 and response.json()["valid"] is False
    quarantine.assert_called_once_with("demo.fmu", "corrupt archive")

    response = client.post("/internal/fmu/quarantine/demo.fmu?reason=operator", headers=headers)
    assert response.status_code == 200 and response.json()["quarantined"] is True
    quarantine.assert_called_with("demo.fmu", "operator")

    response = client.delete("/internal/fmu/quarantine/demo.fmu", headers=headers)
    assert response.status_code == 200 and response.json()["restored"] is True
    response = client.get("/internal/fmu/quarantine", headers=headers)
    assert response.json() == {"quarantined": [{"accessKey": "demo.fmu"}]}
    assert client.post("/internal/fmu/validate/%2E%2E%2Foutside.fmu", headers=headers).status_code == 400


def test_backend_and_startup_shutdown_routes_cover_lifecycle(route_client, monkeypatch):
    client, headers = route_client
    monkeypatch.setattr(config, "execution_mode", lambda: "in-process")
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 3)
    backend = client.get("/internal/fmu/backends", headers=headers)
    assert backend.status_code == 200 and backend.json()["executionMode"] == "in-process"
    assert backend.json()["backends"]["fmpy"]["available"] is True
    monkeypatch.setattr(config, "log_level", lambda: "not-a-log-level")
    cleanup = Mock()
    terminate = Mock()
    monkeypatch.setattr(engine, "cleanup_expired_sessions", cleanup)
    monkeypatch.setattr(engine, "terminate_all", terminate)

    with TestClient(main.app):
        pass

    assert cleanup.call_count >= 1
    terminate.assert_called_once_with()
