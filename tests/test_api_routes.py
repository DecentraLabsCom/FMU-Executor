from __future__ import annotations

import json
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app import config, engine, fmu_storage, main, process_runner, session_registry
from tests.fmu_fixture import write_test_fmu


class FakeSlot:
    session_id = "route-session"
    _time = 0.0
    _finished_reason = None

    def __init__(self):
        self.calls = []

    def load(self): self.calls.append(("load",))
    def initialize(self, **kwargs): self.calls.append(("initialize", kwargs))
    def run_until(self, target, step_size=None): return {"time": target}
    def get_outputs(self): return {"outputs": {"result": 12}}
    def run_until_streaming(self, target, step_size=None, input_schedule=None):
        self._time = target
        return iter([{"type": "sim.outputs", "simTime": 0.5}])
    def terminate(self): self.calls.append(("terminate",))


@pytest.fixture
def route_client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "internal_token", lambda: "route-test-token")
    monkeypatch.setattr(config, "FMU_ROOT", tmp_path / "fmus")
    monkeypatch.setattr(config, "TEMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(main._simulation_store, "database_path", tmp_path / "state" / "simulations.sqlite3")
    config.FMU_ROOT.mkdir()
    config.TEMP_DIR.mkdir()
    return TestClient(main.app), {"X-Internal-Session-Token": "route-test-token"}


def install_fake_fmu(monkeypatch, tmp_path):
    path = tmp_path / "demo.fmu"
    write_test_fmu(path)
    monkeypatch.setattr(fmu_storage, "fmu_exists", lambda key: key == "demo.fmu")
    monkeypatch.setattr(fmu_storage, "get_fmu_path", lambda key: path)
    monkeypatch.setattr(
        fmu_storage, "inspect_fmu",
        lambda key, **kwargs: fmu_storage.inspect_fmu_path(path, access_key=key, include_hash=kwargs.get("include_hash", True)),
    )
    return path


def test_companion_asset_requires_the_internal_token_and_access_key(route_client):
    client, headers = route_client

    unauthorized = client.get(
        "/internal/fmu/companion-asset",
        headers={"X-FMU-Access-Key": "demo.fmu"},
    )
    missing_key = client.get("/internal/fmu/companion-asset", headers=headers)

    assert unauthorized.status_code == 401
    assert missing_key.status_code == 422


def test_companion_asset_returns_the_sibling_glb(route_client):
    client, headers = route_client
    fmu_path = config.FMU_ROOT / "provider" / "ExampleCell.fmu"
    write_test_fmu(fmu_path)
    scene_path = fmu_path.with_suffix(".glb")
    scene_path.write_bytes(b"glTF demo scene")

    response = client.get(
        "/internal/fmu/companion-asset?assetType=glb",
        headers={**headers, "X-FMU-Access-Key": "provider/ExampleCell.fmu"},
    )

    assert response.status_code == 200
    assert response.content == b"glTF demo scene"
    assert response.headers["content-type"] == "model/gltf-binary"
    assert response.headers["cache-control"] == "private, no-store"


def test_companion_asset_returns_the_sibling_threejs_app(route_client):
    client, headers = route_client
    fmu_path = config.FMU_ROOT / "SmartWarehouseCell.fmu"
    write_test_fmu(fmu_path)
    app_path = fmu_path.with_suffix(".webapp.zip")
    app_path.write_bytes(b"static app archive")

    response = client.get(
        "/internal/fmu/companion-asset?assetType=threejs-app",
        headers={**headers, "X-FMU-Access-Key": "SmartWarehouseCell.fmu"},
    )

    assert response.status_code == 200
    assert response.content == b"static app archive"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "private, no-store"


def _write_fmu_with_ref_assets(path, related_files):
    path.parent.mkdir(parents=True, exist_ok=True)
    root_attrs = (
        'xmlns:fmi-ls="http://fmi-standard.org/fmi-ls-manifest" '
        'fmi-ls:fmi-ls-name="org.fmi-standard.fmi-ls-ref" '
        'fmi-ls:fmi-ls-version="1.0.0-beta.1"'
    )
    related_xml = "".join(
        f'<Related type="{media_type}" source="{source}" role="other" />'
        for media_type, source in related_files
    )
    manifest = f"<fmiReferences {root_attrs}>{related_xml}</fmiReferences>".encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "extra/org.fmi-standard.fmi-ls-ref/fmi-ls-manifest.xml",
            manifest,
        )
        archive.writestr("modelDescription.xml", b"<fmiModelDescription />")
        archive.writestr("visualizations/cell.glb", b"ref-glb")
        archive.writestr("visualizations/cell.webapp.zip", b"ref-app")


def test_companion_asset_auto_prefers_ref_webapp_over_ref_glb_and_legacy_files(route_client):
    client, headers = route_client
    fmu_path = config.FMU_ROOT / "provider" / "ExampleCell.fmu"
    _write_fmu_with_ref_assets(
        fmu_path,
        [
            ("model/gltf-binary", "../../visualizations/cell.glb"),
            ("application/zip", "../../visualizations/cell.webapp.zip"),
        ],
    )
    fmu_path.with_suffix(".glb").write_bytes(b"legacy-glb")
    fmu_path.with_suffix(".webapp.zip").write_bytes(b"legacy-app")

    response = client.get(
        "/internal/fmu/companion-asset?assetType=auto",
        headers={**headers, "X-FMU-Access-Key": "provider/ExampleCell.fmu"},
    )

    assert response.status_code == 200
    assert response.content == b"ref-app"
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-digital-twin-asset-type"] == "threejs-app"
    assert response.headers["x-digital-twin-asset-source"] == "fmi-ls-ref"


def test_companion_asset_auto_uses_legacy_app_then_glb_when_ref_is_absent(route_client):
    client, headers = route_client
    fmu_path = config.FMU_ROOT / "ExampleCell.fmu"
    with zipfile.ZipFile(fmu_path, "w") as archive:
        archive.writestr("modelDescription.xml", b"<fmiModelDescription />")
    fmu_path.with_suffix(".glb").write_bytes(b"legacy-glb")
    fmu_path.with_suffix(".webapp.zip").write_bytes(b"legacy-app")

    response = client.get(
        "/internal/fmu/companion-asset?assetType=auto",
        headers={**headers, "X-FMU-Access-Key": "ExampleCell.fmu"},
    )

    assert response.status_code == 200
    assert response.content == b"legacy-app"
    assert response.headers["x-digital-twin-asset-type"] == "threejs-app"
    assert response.headers["x-digital-twin-asset-source"] == "legacy-sibling"


def test_companion_asset_auto_falls_back_to_legacy_glb(route_client):
    client, headers = route_client
    fmu_path = config.FMU_ROOT / "ExampleCell.fmu"
    _write_fmu_with_ref_assets(
        fmu_path,
        [("application/zip", "https://example.test/app.webapp.zip")],
    )
    fmu_path.with_suffix(".glb").write_bytes(b"legacy-glb")

    response = client.get(
        "/internal/fmu/companion-asset?assetType=auto",
        headers={**headers, "X-FMU-Access-Key": "ExampleCell.fmu"},
    )

    assert response.status_code == 200
    assert response.content == b"legacy-glb"
    assert response.headers["x-digital-twin-asset-type"] == "glb"
    assert response.headers["x-digital-twin-asset-source"] == "legacy-sibling"


def test_companion_asset_auto_prefers_sibling_webapp_to_ref_glb(route_client):
    client, headers = route_client
    fmu_path = config.FMU_ROOT / "ExampleCell.fmu"
    _write_fmu_with_ref_assets(
        fmu_path,
        [("model/gltf-binary", "../../visualizations/cell.glb")],
    )
    app_path = fmu_path.with_suffix(".webapp.zip")
    app_path.write_bytes(b"legacy-app")

    response = client.get(
        "/internal/fmu/companion-asset?assetType=auto",
        headers={**headers, "X-FMU-Access-Key": "ExampleCell.fmu"},
    )

    assert response.status_code == 200
    assert response.content == b"legacy-app"
    assert response.headers["x-digital-twin-asset-type"] == "threejs-app"
    assert response.headers["x-digital-twin-asset-source"] == "legacy-sibling"


def test_companion_asset_returns_not_found_when_fmu_or_asset_is_missing(route_client):
    client, headers = route_client
    request_headers = {**headers, "X-FMU-Access-Key": "ExampleCell.fmu"}

    assert client.get("/internal/fmu/companion-asset?assetType=glb", headers=request_headers).status_code == 404

    write_test_fmu(config.FMU_ROOT / "ExampleCell.fmu")
    assert client.get("/internal/fmu/companion-asset?assetType=glb", headers=request_headers).status_code == 404


def test_companion_asset_rejects_unsafe_keys_and_paths_outside_fmu_root(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    invalid_key = client.get(
        "/internal/fmu/companion-asset?assetType=glb",
        headers={**headers, "X-FMU-Access-Key": "../outside.fmu"},
    )
    outside_fmu = write_test_fmu(tmp_path / "outside.fmu")
    outside_fmu.with_suffix(".glb").write_bytes(b"outside scene")
    monkeypatch.setattr(fmu_storage, "get_fmu_path", lambda _key: outside_fmu)
    outside_path = client.get(
        "/internal/fmu/companion-asset?assetType=glb",
        headers={**headers, "X-FMU-Access-Key": "demo.fmu"},
    )

    assert invalid_key.status_code == 400
    assert outside_path.status_code == 404


def test_companion_asset_enforces_the_response_size_limit(route_client, monkeypatch):
    client, headers = route_client
    fmu_path = config.FMU_ROOT / "ExampleCell.fmu"
    write_test_fmu(fmu_path)
    fmu_path.with_suffix(".glb").write_bytes(b"scene-too-large")
    monkeypatch.setattr(main, "MAX_COMPANION_ASSET_BYTES", 8)

    response = client.get(
        "/internal/fmu/companion-asset?assetType=glb",
        headers={**headers, "X-FMU-Access-Key": "ExampleCell.fmu"},
    )

    assert response.status_code == 413


@pytest.mark.parametrize(("backend", "status"), [("unknown", 400), ("omsimulator", 501)])
def test_simulation_routes_reject_unavailable_backends_before_allocating(route_client, monkeypatch, tmp_path, backend, status):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    create = Mock(side_effect=AssertionError("session should not be allocated"))
    monkeypatch.setattr(session_registry, "create_session", create)

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
    capacity = object()
    released = Mock()
    monkeypatch.setattr(session_registry, "reserve_capacity", lambda: capacity)
    monkeypatch.setattr(session_registry, "release_capacity", released)
    process_run = Mock(return_value={"type": "sim.result", "time": 1.5, "outputs": {"x": 2}})
    monkeypatch.setattr(process_runner, "run", process_run)
    monkeypatch.setattr(config, "execution_mode", lambda: "process")

    response = client.post(
        "/internal/fmu/simulations/run",
        headers=headers,
        json={"accessKey": "demo.fmu", "parameters": {"x": 2}, "options": {"stopTime": 1.5}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "sim.result" and payload["time"] == 1.5
    assert payload["evidence"]["options"]["stopTime"] == 1.5
    process_run.assert_called_once()
    assert process_run.call_args.args[0].is_file()
    assert process_run.call_args.args[0] != fmu_path
    assert process_run.call_args.kwargs["parameters"] == {"x": 2}
    assert process_run.call_args.kwargs["resource_policy"]["maxSteps"] > 0
    released.assert_called_once_with(capacity)


@pytest.mark.parametrize(("error", "expected_status"), [(process_runner.ProcessExecutionError("failed"), 500), (process_runner.ProcessExecutionError("timed out", code="FMU_EXECUTION_TIMEOUT", retryable=True), 504)])
def test_run_route_maps_worker_errors_and_still_releases_capacity(route_client, monkeypatch, tmp_path, error, expected_status):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    capacity = object()
    released = Mock()
    monkeypatch.setattr(session_registry, "reserve_capacity", lambda: capacity)
    monkeypatch.setattr(session_registry, "release_capacity", released)
    monkeypatch.setattr(process_runner, "run", Mock(side_effect=error))
    monkeypatch.setattr(config, "execution_mode", lambda: "process")

    response = client.post("/internal/fmu/simulations/run", headers=headers, json={"accessKey": "demo.fmu"})

    assert response.status_code == expected_status
    released.assert_called_once_with(capacity)


def test_run_route_rejects_exhausted_capacity(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    monkeypatch.setattr(session_registry, "reserve_capacity", Mock(side_effect=session_registry.CapacityExceededError("full")))

    response = client.post("/internal/fmu/simulations/run", headers=headers, json={"accessKey": "demo.fmu"})

    assert response.status_code == 429
    assert response.json()["detail"] == "STATION_CAPACITY_EXHAUSTED"


def test_run_route_executes_in_process_mode(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    slot = FakeSlot()
    monkeypatch.setattr(engine, "FmuSession", lambda *_args, **_kwargs: slot)
    capacity = object()
    released = Mock()
    monkeypatch.setattr(session_registry, "reserve_capacity", lambda: capacity)
    monkeypatch.setattr(session_registry, "release_capacity", released)
    monkeypatch.setattr(config, "execution_mode", lambda: "in-process")

    response = client.post(
        "/internal/fmu/simulations/run",
        headers=headers,
        json={"accessKey": "demo.fmu", "parameters": {"input": 4}, "options": {"startTime": 0.2, "stopTime": 2, "stepSize": 0.1}},
    )

    assert response.status_code == 200
    assert response.json()["type"] == "sim.result"
    assert response.json()["outputs"] == {"result": 12}
    assert response.json()["evidence"]["options"]["earlyReturnAllowed"] is False
    assert slot.calls[:2] == [("load",), ("initialize", {"start_time": 0.2, "stop_time": 2.0, "step_size": 0.1, "parameters": {"input": 4}, "early_return_allowed": False})]
    assert slot.calls[-1] == ("terminate",)
    released.assert_called_once_with(capacity)


def test_stream_route_yields_ndjson_and_releases_capacity(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    capacity = object()
    released = Mock()
    monkeypatch.setattr(session_registry, "reserve_capacity", lambda: capacity)
    monkeypatch.setattr(session_registry, "release_capacity", released)
    monkeypatch.setattr(process_runner, "stream", lambda *args, **kwargs: iter([{"type": "sim.outputs", "simTime": 0.5}, {"type": "sim.done", "time": 1}]))
    monkeypatch.setattr(config, "execution_mode", lambda: "process")

    response = client.post("/internal/fmu/simulations/stream", headers=headers, json={"accessKey": "demo.fmu"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    payloads = [json.loads(line) for line in response.text.splitlines()]
    assert payloads[0] == {"type": "sim.outputs", "simTime": 0.5}
    assert payloads[1]["type"] == "sim.done"
    assert payloads[1]["evidence"]["fmuSha256"] == payloads[1]["fmuSha256"]
    released.assert_called_once_with(capacity)


def test_stream_route_reports_capacity_as_ndjson(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    monkeypatch.setattr(session_registry, "reserve_capacity", Mock(side_effect=session_registry.CapacityExceededError("full")))

    response = client.post("/internal/fmu/simulations/stream", headers=headers, json={"accessKey": "demo.fmu"})

    assert response.status_code == 429
    assert response.json()["detail"] == "STATION_CAPACITY_EXHAUSTED"


def test_stream_route_in_process_and_exception_paths_are_closed(route_client, monkeypatch, tmp_path):
    client, headers = route_client
    install_fake_fmu(monkeypatch, tmp_path)
    slot = FakeSlot()
    capacity = object()
    released = Mock()
    monkeypatch.setattr(session_registry, "reserve_capacity", lambda: capacity)
    monkeypatch.setattr(engine, "FmuSession", lambda *_args, **_kwargs: slot)
    monkeypatch.setattr(session_registry, "release_capacity", released)
    monkeypatch.setattr(config, "execution_mode", lambda: "in-process")

    response = client.post("/internal/fmu/simulations/stream", headers=headers, json={"accessKey": "demo.fmu", "options": {"stopTime": 2}})

    payloads = [json.loads(line) for line in response.text.splitlines()]
    assert payloads[0] == {"type": "sim.outputs", "simTime": 0.5}
    assert payloads[1]["type"] == "sim.done" and payloads[1]["time"] == 2.0
    released.assert_called_once_with(capacity)

    released.reset_mock()
    monkeypatch.setattr(config, "execution_mode", lambda: "process")
    monkeypatch.setattr(process_runner, "stream", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret detail")))
    response = client.post("/internal/fmu/simulations/stream", headers=headers, json={"accessKey": "demo.fmu"})
    assert json.loads(response.text) == {"type": "error", "message": "FMU simulation failed"}
    released.assert_called_once_with(capacity)


def test_quarantine_endpoints_validate_and_delegate(route_client, monkeypatch):
    client, headers = route_client
    inspect = Mock(return_value={
        "accessKey": "demo.fmu", "archiveValid": False,
        "modelDescriptionValid": False, "stationCompatible": False,
        "issues": ["FMU is not a readable ZIP archive"],
    })
    quarantine = Mock()
    unquarantine = Mock(return_value=True)
    monkeypatch.setattr(fmu_storage, "inspect_fmu", inspect)
    monkeypatch.setattr(fmu_storage, "quarantine", quarantine)
    monkeypatch.setattr(fmu_storage, "unquarantine", unquarantine)
    monkeypatch.setattr(fmu_storage, "list_quarantined", lambda: [{"accessKey": "demo.fmu"}])

    response = client.post("/internal/fmu/validate/demo.fmu?auto_quarantine=true", headers=headers)
    assert response.status_code == 200 and response.json()["valid"] is False
    quarantine.assert_called_once_with("demo.fmu", "FMU is not a readable ZIP archive")

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
    monkeypatch.setattr(session_registry, "cleanup_expired_sessions", cleanup)
    monkeypatch.setattr(session_registry, "terminate_all", terminate)

    with TestClient(main.app):
        pass

    assert cleanup.call_count >= 1
    terminate.assert_called_once_with()
