from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app import auth, config, engine, fmu_storage, process_runner
from app.simulation_jobs import (
    SimulationJobManager,
    _job_id,
    _validate_options,
    _validate_parameters,
)
from app.simulation_store import QuotaExceededError, SimulationStore, reservation_scope


CONTEXT = {
    "targetGatewayId": "gateway-a",
    "labId": "lab-a",
    "reservationKey": "reservation-a",
    "pucHash": "puc-a",
}
PARAMETERS = {"gain": 2}
OPTIONS = {"stopTime": 1.0}


@pytest.fixture
def jobs(monkeypatch, tmp_path):
    store = SimulationStore(tmp_path / "simulations.sqlite3")
    store.initialize()
    manager = SimulationJobManager(store)
    monkeypatch.setattr(config, "execution_mode", lambda: "process")
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 4)
    monkeypatch.setattr(config, "MAX_SIMULATION_STEPS", 1000)
    monkeypatch.setattr(config, "MAX_BATCH_CASES", 8)
    monkeypatch.setattr(fmu_storage, "describe", lambda _key: {
        "supportsCoSimulation": True, "defaultStepSize": 0.1,
    })
    monkeypatch.setattr(auth, "validate_gateway_context", lambda _context, _key: None)
    monkeypatch.setattr(fmu_storage, "get_fmu_path", lambda _key: tmp_path / "model.fmu")
    monkeypatch.setattr(engine, "create_session", lambda *_args, **_kwargs: SimpleNamespace(session_id="job-session"))
    monkeypatch.setattr(engine, "remove_session", Mock())
    monkeypatch.setattr(process_runner, "run", Mock(return_value={
        "type": "sim.result", "time": 1.0, "outputs": {"speed": 2.0},
    }))
    return manager, store


def _submit(manager, **overrides):
    arguments = {
        "access_key": "model.fmu",
        "gateway_context": CONTEXT,
        "parameters": PARAMETERS,
        "options": OPTIONS,
        "requested_id": "job-1",
    }
    arguments.update(overrides)
    return manager.submit_single(**arguments)


def test_job_id_generates_default_and_rejects_unsafe_values():
    assert len(_job_id(None)) == 32
    assert _job_id("batch_01-abc") == "batch_01-abc"
    for value in ("../escape", "contains space", "x" * 65):
        with pytest.raises(HTTPException) as error:
            _job_id(value)
        assert error.value.status_code == 400
        assert error.value.detail == "INVALID_SIMULATION_ID"


def test_options_require_cosimulation_and_a_supported_finite_time_range(monkeypatch):
    monkeypatch.setattr(fmu_storage, "describe", lambda _key: {
        "supportsCoSimulation": True, "defaultStepSize": 0.1,
    })
    assert _validate_options("model.fmu", {"stopTime": 1, "stepSize": 0.2}) == 5
    for value in (
        {"backend": "fmpy"}, {"startTime": "nope"}, {"stopTime": 0},
        {"stepSize": 0}, {"stopTime": float("inf")},
    ):
        with pytest.raises(HTTPException):
            _validate_options("model.fmu", value)

    monkeypatch.setattr(fmu_storage, "describe", lambda _key: {"supportsCoSimulation": False})
    with pytest.raises(HTTPException) as error:
        _validate_options("model.fmu", {})
    assert error.value.detail == "FMU_COSIMULATION_REQUIRED"

    monkeypatch.setattr(fmu_storage, "describe", lambda _key: {"supportsCoSimulation": True})
    monkeypatch.setattr(config, "MAX_SIMULATION_STEPS", 2)
    with pytest.raises(HTTPException) as error:
        _validate_options("model.fmu", {"stopTime": 1, "stepSize": 0.1})
    assert error.value.status_code == 413


@pytest.mark.parametrize("parameters", [
    [], {"x": None}, {"x": {"nested": 1}}, {"": 1}, {"x": float("nan")},
    {"x": [1, [2, [3, [4, [5]]]]]}, {"x": "s" * 4097},
    {str(index): index for index in range(33)},
])
def test_parameter_validation_rejects_unsupported_or_unbounded_values(parameters):
    with pytest.raises(HTTPException):
        _validate_parameters(parameters)


def test_parameter_validation_enforces_encoded_size_and_accepts_nested_scalars():
    _validate_parameters({"x": [1, True, "text", [2.5]]})
    with pytest.raises(HTTPException) as error:
        _validate_parameters({"x": ["s" * 4000] * 5})
    assert error.value.status_code == 413


def test_submit_single_executes_and_records_scoped_result(jobs):
    manager, _ = jobs
    scope_key, _ = reservation_scope(CONTEXT)

    async def run():
        response = await _submit(manager)
        result = await manager.wait(response["id"], scope_key)
        return response, result

    response, result = asyncio.run(run())
    assert response["status"] == "queued"
    assert response["kind"] == "single"
    assert result["status"] == "completed"
    assert result["completedCases"] == 1
    assert result["result"] == {"type": "sim.result", "time": 1.0, "outputs": {"speed": 2.0}}
    engine.remove_session.assert_called_once_with("job-session")
    process_runner.run.assert_called_once()


def test_submit_batch_merges_options_and_records_labeled_results(jobs, monkeypatch):
    manager, _ = jobs
    scope_key, _ = reservation_scope(CONTEXT)
    calls = []

    def execute(_path, **kwargs):
        calls.append(kwargs)
        return {"time": kwargs["options"]["stopTime"], "outputs": kwargs["parameters"]}

    monkeypatch.setattr(process_runner, "run", execute)
    scenarios = [
        {"label": "baseline", "parameters": {"gain": 1}, "options": {"stepSize": 0.5}},
        {"parameters": {"gain": 3}, "options": {}},
    ]

    async def run():
        response = await manager.submit_batch(
            access_key="model.fmu", gateway_context=CONTEXT, scenarios=scenarios,
            options=OPTIONS, requested_id="batch-1",
        )
        return response, await manager.wait(response["id"], scope_key)

    response, result = asyncio.run(run())
    assert response["kind"] == "batch"
    assert result["status"] == "completed"
    assert result["result"]["partial"] is False
    assert [case["label"] for case in result["result"]["cases"]] == ["baseline", "case-2"]
    assert calls[0]["options"] == {"stopTime": 1.0, "stepSize": 0.5}
    assert calls[1]["options"] == OPTIONS
    assert result["completedCases"] == 2


def test_batch_validation_rejects_size_and_combined_step_overages(jobs, monkeypatch):
    manager, _ = jobs

    async def too_many():
        return await manager.submit_batch(
            access_key="model.fmu", gateway_context=CONTEXT,
            scenarios=[], options=OPTIONS,
        )

    with pytest.raises(HTTPException) as error:
        asyncio.run(too_many())
    assert error.value.status_code == 422

    monkeypatch.setattr(config, "MAX_SIMULATION_STEPS", 5)

    async def too_dense():
        return await manager.submit_batch(
            access_key="model.fmu", gateway_context=CONTEXT,
            scenarios=[{}, {}, {}], options={"stopTime": 1, "stepSize": 0.2},
        )

    with pytest.raises(HTTPException) as error:
        asyncio.run(too_dense())
    assert error.value.status_code == 413
    assert error.value.detail == "BATCH_STEP_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("in-process", "ASYNC_EXECUTION_REQUIRES_PROCESS_ISOLATION")],
)
def test_async_jobs_require_process_isolation(jobs, monkeypatch, mode, expected):
    manager, store = jobs
    monkeypatch.setattr(config, "execution_mode", lambda: mode)

    with pytest.raises(HTTPException) as error:
        asyncio.run(_submit(manager))
    assert error.value.status_code == 409
    assert error.value.detail == expected
    assert store.list_history(reservation_scope(CONTEXT)[0], limit=10, offset=0)["total"] == 0


def test_submission_maps_quota_duplicate_and_invalid_storage_errors(jobs, monkeypatch):
    manager, store = jobs
    original_create = store.create_job

    for failure, status, detail in (
        (QuotaExceededError(0), 429, "RESERVATION_DAILY_SCENARIO_LIMIT"),
        (sqlite_error(), 409, "SIMULATION_ID_ALREADY_EXISTS"),
        (ValueError("bad serialization"), 422, "INVALID_SIMULATION_REQUEST"),
    ):
        monkeypatch.setattr(store, "create_job", Mock(side_effect=failure))
        with pytest.raises(HTTPException) as error:
            asyncio.run(_submit(manager, requested_id=f"job-{status}"))
        assert error.value.status_code == status
        if status == 429:
            assert error.value.detail["remaining"] == 0
        else:
            assert error.value.detail == detail
    monkeypatch.setattr(store, "create_job", original_create)


def sqlite_error():
    return sqlite3.IntegrityError("duplicate key")


def test_submission_rejects_full_station_capacity(jobs, monkeypatch):
    manager, _ = jobs
    monkeypatch.setattr(config, "MAX_CONCURRENT_SESSIONS", 1)

    async def run():
        manager._tasks["occupied"] = asyncio.create_task(asyncio.sleep(30))
        try:
            await _submit(manager)
        finally:
            manager._tasks["occupied"].cancel()

    with pytest.raises(HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 429
    assert error.value.detail == "STATION_CAPACITY_EXHAUSTED"


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (engine.CapacityExceededError("busy"), "failed", "STATION_CAPACITY_EXHAUSTED"),
        (process_runner.ProcessExecutionError("cancelled", code="FMU_EXECUTION_CANCELLED"), "cancelled", None),
        (process_runner.ProcessExecutionError("timed out", code="FMU_EXECUTION_TIMEOUT"), "failed", "FMU_EXECUTION_TIMEOUT"),
        (ValueError("oversized result"), "failed", "RESULT_SIZE_LIMIT_EXCEEDED"),
        (RuntimeError("worker failed"), "failed", "FMU_EXECUTION_FAILED"),
    ],
)
def test_execution_maps_worker_failures_and_cleans_up(jobs, monkeypatch, failure, status, code):
    manager, _ = jobs
    scope_key, _ = reservation_scope(CONTEXT)
    if isinstance(failure, engine.CapacityExceededError):
        monkeypatch.setattr(engine, "create_session", Mock(side_effect=failure))
    else:
        monkeypatch.setattr(process_runner, "run", Mock(side_effect=failure))

    async def run():
        response = await _submit(manager)
        return await manager.wait(response["id"], scope_key)

    result = asyncio.run(run())
    assert result["status"] == status
    assert result["errorCode"] == code
    if not isinstance(failure, engine.CapacityExceededError):
        engine.remove_session.assert_called_once_with("job-session")


def test_cancel_before_execution_prevents_fmu_session_creation(jobs, monkeypatch):
    manager, _ = jobs
    scope_key, _ = reservation_scope(CONTEXT)
    create_session = Mock(side_effect=AssertionError("cancelled job must not allocate a session"))
    monkeypatch.setattr(engine, "create_session", create_session)

    async def run():
        response = await _submit(manager)
        cancelling = await manager.cancel(response["id"], scope_key)
        result = await manager.wait(response["id"], scope_key)
        return cancelling, result

    cancelling, result = asyncio.run(run())
    assert cancelling["status"] == "cancelling"
    assert result["status"] == "cancelled"
    assert result["result"] is None
    create_session.assert_not_called()


def test_cancel_hides_other_scopes_and_returns_terminal_jobs(jobs):
    manager, _ = jobs
    scope_key, _ = reservation_scope(CONTEXT)
    other_scope, _ = reservation_scope({**CONTEXT, "reservationKey": "other"})

    async def run():
        response = await _submit(manager)
        await manager.wait(response["id"], scope_key)
        return (
            await manager.cancel(response["id"], other_scope),
            await manager.cancel(response["id"], scope_key),
        )

    hidden, completed = asyncio.run(run())
    assert hidden is None
    assert completed["status"] == "completed"


def test_partial_batch_result_overflow_records_safe_failure(jobs, monkeypatch):
    manager, _ = jobs
    scope_key, _ = reservation_scope(CONTEXT)
    monkeypatch.setattr(config, "MAX_STORED_RESULT_BYTES", 100)
    calls = 0

    def execute(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"output": "x" * 200}
        raise process_runner.ProcessExecutionError("later case failed", code="FMU_EXECUTION_FAILED")

    monkeypatch.setattr(process_runner, "run", execute)

    async def run():
        response = await manager.submit_batch(
            access_key="model.fmu", gateway_context=CONTEXT,
            scenarios=[{}, {}], options=OPTIONS, requested_id="partial",
        )
        return await manager.wait(response["id"], scope_key)

    result = asyncio.run(run())
    assert result["status"] == "failed"
    assert result["errorCode"] == "RESULT_SIZE_LIMIT_EXCEEDED"
    assert result["result"] is None


def test_status_result_history_and_shutdown(jobs):
    manager, _ = jobs
    scope_key, _ = reservation_scope(CONTEXT)

    async def run():
        response = await _submit(manager)
        await manager.wait(response["id"], scope_key)
        before_shutdown = (
            manager.status(response["id"], scope_key),
            manager.result(response["id"], scope_key),
            manager.history(scope_key, limit=10, offset=0),
        )
        await manager.shutdown()
        return before_shutdown

    status, result, history = asyncio.run(run())
    assert status["status"] == "completed"
    assert result["result"] is not None
    assert history["total"] == 1
    assert manager.status("missing", scope_key) is None
    assert manager.result("missing", scope_key) is None
