from __future__ import annotations

import sqlite3
import time
from contextlib import closing

import pytest

from app import config
from app.simulation_store import QuotaExceededError, SimulationStore, reservation_scope


CONTEXT = {
    "targetGatewayId": "Gateway-A",
    "labId": "Lab-A",
    "reservationKey": "Reservation-A",
    "pucHash": "ABC123",
}


@pytest.fixture
def store(tmp_path):
    value = SimulationStore(tmp_path / "state" / "simulations.sqlite3")
    value.initialize()
    return value


def add_job(store, job_id, *, context=CONTEXT, status="queued", result=None, created_at=None):
    scope_key, scope = reservation_scope(context)
    store.create_job(
        job_id=job_id,
        scope_key=scope_key,
        scope=scope,
        fmu_filename="model.fmu",
        kind="single",
        total_cases=1,
        parameters={"gain": 2},
        options={"stopTime": 1},
    )
    if status != "queued" or result is not None:
        store.update_job(job_id, status=status, result=result)
    if created_at is not None:
        with closing(sqlite3.connect(store.database_path)) as db:
            db.execute("UPDATE simulation_jobs SET created_at=? WHERE id=?", (created_at, job_id))
            db.commit()
    return scope_key


def test_reservation_scope_normalizes_values_and_supports_claims():
    key, values = reservation_scope({"claims": {
        "targetGatewayId": " Gateway-A ", "labId": " Lab-A ",
        "reservationKey": " Reservation-A ", "pucHash": "ABC123",
    }})

    expected_key, expected_values = reservation_scope(CONTEXT)
    assert key == expected_key
    assert values == expected_values
    assert values == {
        "targetGatewayId": "gateway-a", "labId": "lab-a",
        "reservationKey": "reservation-a", "pucHash": "abc123",
    }


@pytest.mark.parametrize("context", [None, [], {}, {"claims": []}, {"labId": "lab"}])
def test_reservation_scope_rejects_incomplete_or_malformed_context(context):
    with pytest.raises(ValueError):
        reservation_scope(context)


def test_reserve_scenarios_debits_atomically_and_enforces_daily_quota(store, monkeypatch):
    monkeypatch.setattr(config, "MAX_SCENARIOS_PER_RESERVATION_PER_DAY", 3)
    scope_key, _ = reservation_scope(CONTEXT)

    assert store.reserve_scenarios(scope_key, 2) == 2
    with pytest.raises(QuotaExceededError) as error:
        store.reserve_scenarios(scope_key, 2)
    assert error.value.args == (1,)
    assert store.reserve_scenarios("other-scope", 3) == 3
    with pytest.raises(ValueError, match="positive"):
        store.reserve_scenarios(scope_key, 0)


def test_create_job_persists_parameters_and_charges_usage(store):
    scope_key = add_job(store, "created")

    item = store.get_result("created", scope_key)
    assert item["id"] == "created"
    assert item["status"] == "queued"
    assert item["parameters"] == {"gain": 2}
    assert item["options"] == {"stopTime": 1}
    assert item["result"] is None
    assert item["resultAvailable"] is False
    assert item["labId"] == "lab-a"
    assert item["reservationKey"] == "reservation-a"
    assert item["fmuFileName"] == "model.fmu"
    assert store.reserve_scenarios(scope_key, 1) == 2


def test_create_job_rolls_back_quota_and_rejects_non_json_numbers(store, monkeypatch):
    monkeypatch.setattr(config, "MAX_SCENARIOS_PER_RESERVATION_PER_DAY", 1)
    scope_key, scope = reservation_scope(CONTEXT)

    with pytest.raises(QuotaExceededError):
        store.create_job(
            job_id="too-many", scope_key=scope_key, scope=scope, fmu_filename="m.fmu",
            kind="batch", total_cases=2, parameters={}, options={},
        )
    assert store.get_job("too-many", scope_key) is None
    with pytest.raises(ValueError):
        store.create_job(
            job_id="not-json", scope_key=scope_key, scope=scope, fmu_filename="m.fmu",
            kind="single", total_cases=1, parameters={"x": float("nan")}, options={},
        )
    assert store.reserve_scenarios(scope_key, 1) == 1


def test_update_job_tracks_elapsed_time_and_serializes_result(store):
    scope_key = add_job(store, "lifecycle")
    store.update_job("lifecycle", status="running", completed_cases=0)
    running = store.get_job("lifecycle", scope_key)
    assert running["status"] == "running"
    assert running["startedAt"] is not None
    assert running["elapsedSeconds"] >= 0

    result = {"type": "sim.result", "outputs": {"speed": 3.5}}
    store.update_job("lifecycle", status="completed", completed_cases=1, result=result)
    completed = store.get_result("lifecycle", scope_key)
    assert completed["status"] == "completed"
    assert completed["completedCases"] == 1
    assert completed["elapsedSeconds"] >= 0
    assert completed["result"] == result
    assert completed["resultAvailable"] is True


def test_update_job_enforces_result_limit_and_json_safety(store, monkeypatch):
    scope_key = add_job(store, "limited")
    monkeypatch.setattr(config, "MAX_STORED_RESULT_BYTES", 8)
    with pytest.raises(ValueError, match="storage limit"):
        store.update_job("limited", status="completed", result={"long": "value"})
    monkeypatch.setattr(config, "MAX_STORED_RESULT_BYTES", 1024)
    with pytest.raises(ValueError):
        store.update_job("limited", status="completed", result={"notFinite": float("inf")})
    assert store.get_job("limited", scope_key)["status"] == "queued"


def test_mark_cancelling_is_scope_limited_and_only_changes_active_jobs(store):
    scope_key = add_job(store, "cancel-me")
    other_scope, _ = reservation_scope({**CONTEXT, "reservationKey": "other"})
    assert store.mark_cancelling("cancel-me", other_scope) is None
    assert store.mark_cancelling("cancel-me", scope_key)["status"] == "cancelling"
    store.update_job("cancel-me", status="cancelled")
    assert store.mark_cancelling("cancel-me", scope_key)["status"] == "cancelled"


def test_reads_are_scope_filtered_and_history_paginates_newest_first(store):
    now = time.time()
    scope_key = add_job(store, "old", created_at=now - 2)
    add_job(store, "new", created_at=now - 1)
    other_key = add_job(store, "private", context={**CONTEXT, "reservationKey": "other"}, created_at=now)

    assert store.get_job("private", scope_key) is None
    assert store.get_result("private", scope_key) is None
    first = store.list_history(scope_key, limit=1, offset=0)
    second = store.list_history(scope_key, limit=1, offset=1)
    assert first["total"] == 2
    assert [item["id"] for item in first["simulations"]] == ["new"]
    assert [item["id"] for item in second["simulations"]] == ["old"]
    assert store.list_history(other_key, limit=10, offset=0)["total"] == 1


def test_initialize_marks_in_flight_jobs_interrupted_and_prunes_old_history(store, monkeypatch):
    scope_key = add_job(store, "running")
    add_job(store, "expired", status="completed", created_at=1)
    with closing(sqlite3.connect(store.database_path)) as db:
        db.execute(
            "UPDATE simulation_jobs SET status='running',started_at=?,elapsed_seconds=NULL WHERE id='running'",
            (time.time() - 10,),
        )
        db.commit()
    monkeypatch.setattr(config, "HISTORY_RETENTION_DAYS", 1)

    store.initialize()

    interrupted = store.get_job("running", scope_key)
    assert interrupted["status"] == "interrupted"
    assert interrupted["errorCode"] == "EXECUTOR_RESTARTED"
    assert interrupted["elapsedSeconds"] >= 0
    assert store.get_job("expired", scope_key) is None


def test_prune_limits_history_count_and_result_storage_without_deleting_active_jobs(store, monkeypatch):
    monkeypatch.setattr(config, "HISTORY_RETENTION_DAYS", 30)
    monkeypatch.setattr(config, "MAX_STORED_HISTORY_RECORDS", 2)
    monkeypatch.setattr(config, "MAX_TOTAL_HISTORY_BYTES", 18)
    created = [time.time() - offset for offset in (30, 20, 10)]
    scope_key = add_job(store, "oldest", status="completed", result={"v": "a" * 10}, created_at=created[0])
    add_job(store, "middle", status="completed", result={"v": "b" * 10}, created_at=created[1])
    add_job(store, "newest", status="completed", result={"v": "c" * 10}, created_at=created[2])
    add_job(store, "active", status="running", result={"v": "active"}, created_at=created[0])

    store.prune()

    history = store.list_history(scope_key, limit=10, offset=0)
    assert {item["id"] for item in history["simulations"]} == {"active", "middle", "newest"}
    assert store.get_result("middle", scope_key)["result"] is None
    assert store.get_result("newest", scope_key)["result"] == {"v": "c" * 10}
    assert store.get_job("active", scope_key)["status"] == "running"
