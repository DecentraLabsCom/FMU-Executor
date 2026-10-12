"""Reservation-scoped execution history and daily simulation budgets."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config


_ACTIVE_STATUSES = ("queued", "running", "cancelling")
_TERMINAL_STATUSES = ("completed", "cancelled", "failed", "expired", "interrupted")


class QuotaExceededError(RuntimeError):
    """A reservation has exhausted its daily simulation budget."""


class InputUpdateQuotaExceededError(RuntimeError):
    """Input update rate or size budget has been exhausted."""

    def __init__(self, code: str, remaining: int):
        super().__init__(code)
        self.code = code
        self.remaining = remaining


def reservation_scope(context: dict[str, Any] | None) -> tuple[str, dict[str, str]]:
    """Return a stable key for one Gateway/lab/reservation authorization scope."""
    if not isinstance(context, dict):
        raise ValueError("Missing gatewayContext")
    claims = context.get("claims") or {}
    if not isinstance(claims, dict):
        raise ValueError("gatewayContext claims must be an object")
    values = {
        "targetGatewayId": str(context.get("targetGatewayId") or claims.get("targetGatewayId") or "").strip().lower(),
        "labId": str(context.get("labId") or claims.get("labId") or "").strip().lower(),
        "reservationKey": str(context.get("reservationKey") or claims.get("reservationKey") or "").strip().lower(),
        "pucHash": str(context.get("pucHash") or claims.get("pucHash") or "").strip().lower(),
    }
    if not values["labId"] or not values["reservationKey"]:
        raise ValueError("Reservation scope is incomplete")
    packed = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(packed).hexdigest(), values


class SimulationStore:
    """Small local store; every read is filtered by reservation scope."""

    def __init__(self, database_path: Path | None = None):
        self.database_path = database_path or config.STATE_DIR / "simulations.sqlite3"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.database_path.parent, 0o700)
        connection = sqlite3.connect(str(self.database_path), timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=10000")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            if os.name != "nt" and self.database_path.exists():
                os.chmod(self.database_path, 0o600)

    def initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS simulation_jobs (
                    id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    lab_id TEXT NOT NULL,
                    reservation_key TEXT NOT NULL,
                    fmu_filename TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    elapsed_seconds REAL,
                    completed_cases INTEGER NOT NULL DEFAULT 0,
                    total_cases INTEGER NOT NULL DEFAULT 1,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    options_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    error_code TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    fmu_sha256 TEXT,
                    authorization_expires_at REAL,
                    reservation_ends_at REAL,
                    finish_reason TEXT,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    result_sha256 TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_simulation_jobs_scope_created
                    ON simulation_jobs(scope_key, created_at DESC);
                CREATE TABLE IF NOT EXISTS simulation_usage (
                    scope_key TEXT NOT NULL,
                    usage_day TEXT NOT NULL,
                    scenarios INTEGER NOT NULL,
                    PRIMARY KEY(scope_key, usage_day)
                );
                CREATE TABLE IF NOT EXISTS simulation_model_usage (
                    scope_key TEXT NOT NULL,
                    model_sha256 TEXT NOT NULL,
                    usage_day TEXT NOT NULL,
                    scenarios INTEGER NOT NULL,
                    PRIMARY KEY(scope_key, model_sha256, usage_day)
                );
                CREATE TABLE IF NOT EXISTS input_update_usage (
                    scope_key TEXT NOT NULL,
                    model_key TEXT NOT NULL,
                    usage_day TEXT NOT NULL,
                    updates INTEGER NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    PRIMARY KEY(scope_key, model_key, usage_day)
                );
                CREATE TABLE IF NOT EXISTS input_update_events (
                    scope_key TEXT NOT NULL,
                    model_key TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_input_update_events_window
                    ON input_update_events(scope_key, model_key, created_at);
                """
            )
            existing_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(simulation_jobs)").fetchall()
            }
            for column, definition in (
                ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("fmu_sha256", "TEXT"),
                ("authorization_expires_at", "REAL"),
                ("reservation_ends_at", "REAL"),
                ("finish_reason", "TEXT"),
                ("warnings_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("result_sha256", "TEXT"),
            ):
                if column not in existing_columns:
                    db.execute(f"ALTER TABLE simulation_jobs ADD COLUMN {column} {definition}")
            # Preserve previously debited reservation-wide usage in a wildcard
            # bucket while new executions are counted per immutable model hash.
            db.execute(
                "INSERT OR IGNORE INTO simulation_model_usage(scope_key,model_sha256,usage_day,scenarios) "
                "SELECT scope_key,'*',usage_day,scenarios FROM simulation_usage"
            )
            # Native FMU workers cannot be resumed after a process restart.
            now = time.time()
            db.execute(
                "UPDATE simulation_jobs SET status='interrupted',updated_at=?,"
                "elapsed_seconds=CASE WHEN started_at IS NULL THEN elapsed_seconds ELSE MAX(0,?-started_at) END,"
                "error_code='EXECUTOR_RESTARTED' WHERE status IN ('queued','running','cancelling')",
                (now, now),
            )
        self.prune()

    def reserve_scenarios(
        self,
        scope_key: str,
        count: int,
        *,
        model_sha256: str = "*",
        maximum: int | None = None,
    ) -> int:
        """Atomically debit a reservation's daily budget for one model hash."""
        if count < 1:
            raise ValueError("Scenario count must be positive")
        usage_day = datetime.now(timezone.utc).date().isoformat()
        maximum = maximum or config.MAX_SCENARIOS_PER_RESERVATION_PER_DAY
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT scenarios FROM simulation_model_usage WHERE scope_key=? AND model_sha256=? AND usage_day=?",
                (scope_key, model_sha256, usage_day),
            ).fetchone()
            used = int(row["scenarios"]) if row else 0
            if model_sha256 != "*":
                legacy = db.execute(
                    "SELECT scenarios FROM simulation_model_usage WHERE scope_key=? AND model_sha256='*' AND usage_day=?",
                    (scope_key, usage_day),
                ).fetchone()
                used += int(legacy["scenarios"]) if legacy else 0
            if used + count > maximum:
                raise QuotaExceededError(maximum - used)
            db.execute(
                "INSERT INTO simulation_model_usage(scope_key,model_sha256,usage_day,scenarios) VALUES(?,?,?,?) "
                "ON CONFLICT(scope_key,model_sha256,usage_day) DO UPDATE SET scenarios=simulation_model_usage.scenarios+excluded.scenarios",
                (scope_key, model_sha256, usage_day, count),
            )
            return used + count

    def reserve_input_update(self, scope_key: str, access_key: str, payload_bytes: int) -> dict[str, int]:
        """Debit input update budgets independently from simulation scenarios."""
        if payload_bytes < 0:
            raise ValueError("Input payload size must not be negative")
        if payload_bytes > config.MAX_INPUT_UPDATE_BYTES:
            raise InputUpdateQuotaExceededError("INPUT_UPDATE_TOO_LARGE", config.MAX_INPUT_UPDATE_BYTES)
        model_key = hashlib.sha256(access_key.encode("utf-8")).hexdigest()
        usage_day = datetime.now(timezone.utc).date().isoformat()
        now = time.time()
        window_start = now - config.INPUT_UPDATE_WINDOW_SECONDS
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM input_update_events WHERE created_at<?", (now - 3600,))
            db.execute(
                "DELETE FROM input_update_events WHERE scope_key=? AND model_key=? AND created_at<?",
                (scope_key, model_key, window_start),
            )
            recent = int(db.execute(
                "SELECT COUNT(*) AS count FROM input_update_events "
                "WHERE scope_key=? AND model_key=? AND created_at>=?",
                (scope_key, model_key, window_start),
            ).fetchone()["count"])
            if recent >= config.MAX_INPUT_UPDATES_PER_WINDOW:
                raise InputUpdateQuotaExceededError(
                    "INPUT_UPDATE_RATE_LIMIT", max(0, config.MAX_INPUT_UPDATES_PER_WINDOW - recent)
                )
            row = db.execute(
                "SELECT updates,payload_bytes FROM input_update_usage "
                "WHERE scope_key=? AND model_key=? AND usage_day=?",
                (scope_key, model_key, usage_day),
            ).fetchone()
            used_updates = int(row["updates"]) if row else 0
            used_bytes = int(row["payload_bytes"]) if row else 0
            if used_updates + 1 > config.MAX_INPUT_UPDATES_PER_DAY:
                raise InputUpdateQuotaExceededError(
                    "INPUT_UPDATE_DAILY_LIMIT", max(0, config.MAX_INPUT_UPDATES_PER_DAY - used_updates)
                )
            if used_bytes + payload_bytes > config.MAX_INPUT_UPDATE_BYTES_PER_DAY:
                raise InputUpdateQuotaExceededError(
                    "INPUT_UPDATE_DAILY_BYTES_LIMIT",
                    max(0, config.MAX_INPUT_UPDATE_BYTES_PER_DAY - used_bytes),
                )
            db.execute(
                "INSERT INTO input_update_usage(scope_key,model_key,usage_day,updates,payload_bytes) "
                "VALUES(?,?,?,?,?) ON CONFLICT(scope_key,model_key,usage_day) DO UPDATE SET "
                "updates=excluded.updates,payload_bytes=excluded.payload_bytes",
                (scope_key, model_key, usage_day, used_updates + 1, used_bytes + payload_bytes),
            )
            db.execute(
                "INSERT INTO input_update_events(scope_key,model_key,created_at) VALUES(?,?,?)",
                (scope_key, model_key, now),
            )
            return {
                "updatesRemaining": config.MAX_INPUT_UPDATES_PER_DAY - used_updates - 1,
                "bytesRemaining": config.MAX_INPUT_UPDATE_BYTES_PER_DAY - used_bytes - payload_bytes,
            }

    def create_job(
        self,
        *,
        job_id: str,
        scope_key: str,
        scope: dict[str, str],
        fmu_filename: str,
        kind: str,
        total_cases: int,
        parameters: dict[str, Any],
        options: dict[str, Any],
        metadata: dict[str, Any],
        authorization_expires_at: float,
        reservation_ends_at: float | None,
        maximum_scenarios: int,
    ) -> None:
        """Create the audit row and charge its scenarios in one transaction."""
        self.prune()
        usage_day = datetime.now(timezone.utc).date().isoformat()
        now = time.time()
        parameters_json = json.dumps(parameters, separators=(",", ":"), allow_nan=False)
        options_json = json.dumps(options, separators=(",", ":"), allow_nan=False)
        metadata_json = json.dumps(metadata, separators=(",", ":"), allow_nan=False)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            model_sha256 = str(metadata.get("fmuSha256") or "*")
            row = db.execute(
                "SELECT scenarios FROM simulation_model_usage WHERE scope_key=? AND model_sha256=? AND usage_day=?",
                (scope_key, model_sha256, usage_day),
            ).fetchone()
            used = int(row["scenarios"]) if row else 0
            legacy = db.execute(
                "SELECT scenarios FROM simulation_model_usage WHERE scope_key=? AND model_sha256='*' AND usage_day=?",
                (scope_key, usage_day),
            ).fetchone()
            used += int(legacy["scenarios"]) if legacy and model_sha256 != "*" else 0
            maximum = maximum_scenarios
            if used + total_cases > maximum:
                raise QuotaExceededError(maximum - used)
            db.execute(
                "INSERT INTO simulation_jobs "
                "(id,scope_key,lab_id,reservation_key,fmu_filename,kind,status,created_at,updated_at,total_cases,parameters_json,options_json,metadata_json,fmu_sha256,authorization_expires_at,reservation_ends_at) "
                "VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?)",
                (
                    job_id, scope_key, scope["labId"], scope["reservationKey"], fmu_filename,
                    kind, now, now, total_cases, parameters_json, options_json, metadata_json,
                    metadata.get("fmuSha256"), authorization_expires_at, reservation_ends_at,
                ),
            )
            db.execute(
                "INSERT INTO simulation_model_usage(scope_key,model_sha256,usage_day,scenarios) VALUES(?,?,?,?) "
                "ON CONFLICT(scope_key,model_sha256,usage_day) DO UPDATE SET scenarios=simulation_model_usage.scenarios+excluded.scenarios",
                (scope_key, model_sha256, usage_day, total_cases),
            )

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        completed_cases: int | None = None,
        result: Any = None,
        error_code: str | None = None,
        finish_reason: str | None = None,
        warnings: list[str] | None = None,
        result_sha256: str | None = None,
    ) -> None:
        now = time.time()
        fields = ["status=?", "updated_at=?", "error_code=?"]
        values: list[Any] = [status, now, error_code]
        if finish_reason is not None:
            fields.append("finish_reason=?")
            values.append(finish_reason)
        if warnings is not None:
            fields.append("warnings_json=?")
            values.append(json.dumps(warnings, separators=(",", ":"), allow_nan=False))
        if result_sha256 is not None:
            fields.append("result_sha256=?")
            values.append(result_sha256)
        if status == "running":
            fields.append("started_at=COALESCE(started_at,?)")
            values.append(now)
        if status in _TERMINAL_STATUSES:
            fields.append("elapsed_seconds=CASE WHEN started_at IS NULL THEN elapsed_seconds ELSE MAX(0,?-started_at) END")
            values.append(now)
        if completed_cases is not None:
            fields.append("completed_cases=?")
            values.append(completed_cases)
        if result is not None:
            encoded = json.dumps(result, separators=(",", ":"), allow_nan=False)
            if len(encoded.encode("utf-8")) > config.MAX_STORED_RESULT_BYTES:
                raise ValueError("Simulation result exceeds the configured storage limit")
            fields.append("result_json=?")
            values.append(encoded)
        values.append(job_id)
        with self._connect() as db:
            db.execute(f"UPDATE simulation_jobs SET {', '.join(fields)} WHERE id=?", values)
        self.prune()

    def mark_cancelling(self, job_id: str, scope_key: str) -> dict[str, Any] | None:
        with self._connect() as db:
            db.execute(
                "UPDATE simulation_jobs SET status='cancelling',updated_at=? "
                "WHERE id=? AND scope_key=? AND status IN ('queued','running')",
                (time.time(), job_id, scope_key),
            )
        return self.get_job(job_id, scope_key)

    def get_job(self, job_id: str, scope_key: str) -> dict[str, Any] | None:
        self.prune()
        with self._connect() as db:
            row = db.execute(
                "SELECT id,lab_id,reservation_key,fmu_filename,kind,status,created_at,updated_at,started_at,"
                "elapsed_seconds,completed_cases,total_cases,error_code,result_json,metadata_json,"
                "fmu_sha256,authorization_expires_at,reservation_ends_at,finish_reason,warnings_json,result_sha256 "
                "FROM simulation_jobs WHERE id=? AND scope_key=?",
                (job_id, scope_key),
            ).fetchone()
        return self._serialize(row) if row else None

    def get_result(self, job_id: str, scope_key: str) -> dict[str, Any] | None:
        self.prune()
        with self._connect() as db:
            row = db.execute(
                "SELECT id,lab_id,reservation_key,fmu_filename,kind,status,created_at,updated_at,started_at,"
                "elapsed_seconds,completed_cases,total_cases,error_code,parameters_json,options_json,result_json,"
                "metadata_json,fmu_sha256,authorization_expires_at,reservation_ends_at,finish_reason,warnings_json,result_sha256 "
                "FROM simulation_jobs WHERE id=? AND scope_key=?",
                (job_id, scope_key),
            ).fetchone()
        if not row:
            return None
        item = self._serialize(row)
        item["parameters"] = json.loads(row["parameters_json"])
        item["options"] = json.loads(row["options_json"])
        item["result"] = json.loads(row["result_json"]) if row["result_json"] else None
        item["evidence"] = json.loads(row["metadata_json"])
        if item["evidence"]:
            item["evidence"]["finishReason"] = row["finish_reason"] or "unknown"
            item["evidence"]["warnings"] = json.loads(row["warnings_json"] or "[]")
            item["evidence"]["resultSha256"] = row["result_sha256"]
        return item

    def list_history(self, scope_key: str, *, limit: int, offset: int) -> dict[str, Any]:
        self.prune()
        with self._connect() as db:
            total = int(db.execute(
                "SELECT COUNT(*) AS total FROM simulation_jobs WHERE scope_key=?",
                (scope_key,),
            ).fetchone()["total"])
            rows = db.execute(
                "SELECT id,lab_id,reservation_key,fmu_filename,kind,status,created_at,updated_at,started_at,"
                "elapsed_seconds,completed_cases,total_cases,error_code,result_json,metadata_json,"
                "fmu_sha256,authorization_expires_at,reservation_ends_at,finish_reason,warnings_json,result_sha256 "
                "FROM simulation_jobs WHERE scope_key=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (scope_key, limit, offset),
            ).fetchall()
        return {
            "simulations": [self._serialize(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def prune(self) -> None:
        cutoff = time.time() - config.HISTORY_RETENTION_DAYS * 86400
        with self._connect() as db:
            db.execute(
                "DELETE FROM simulation_jobs WHERE created_at<? AND status NOT IN ('queued','running','cancelling')",
                (cutoff,),
            )
            db.execute("DELETE FROM simulation_usage WHERE usage_day<date('now','-2 day')")
            db.execute("DELETE FROM simulation_model_usage WHERE usage_day<date('now','-2 day')")
            db.execute("DELETE FROM input_update_usage WHERE usage_day<date('now','-2 day')")
            db.execute("DELETE FROM input_update_events WHERE created_at<?", (time.time() - 3600,))
            db.execute(
                "DELETE FROM simulation_jobs WHERE id IN ("
                "SELECT id FROM simulation_jobs WHERE status NOT IN ('queued','running','cancelling') "
                "ORDER BY created_at DESC LIMIT -1 OFFSET ?) ",
                (config.MAX_STORED_HISTORY_RECORDS,),
            )
            retained = db.execute(
                "SELECT id,LENGTH(CAST(result_json AS BLOB)) AS result_bytes "
                "FROM simulation_jobs WHERE result_json IS NOT NULL "
                "AND status NOT IN ('queued','running','cancelling') ORDER BY created_at DESC"
            ).fetchall()
            total_bytes = sum(int(row["result_bytes"] or 0) for row in retained)
            for row in reversed(retained):
                if total_bytes <= config.MAX_TOTAL_HISTORY_BYTES:
                    break
                db.execute("UPDATE simulation_jobs SET result_json=NULL WHERE id=?", (row["id"],))
                total_bytes -= int(row["result_bytes"] or 0)

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        item = {key: row[key] for key in row.keys() if key != "result_json"}
        metadata_json = item.pop("metadata_json", "{}")
        warnings_json = item.pop("warnings_json", "[]")
        fmu_sha256 = item.pop("fmu_sha256", None)
        authorization_expires_at = item.pop("authorization_expires_at", None)
        reservation_ends_at = item.pop("reservation_ends_at", None)
        finish_reason = item.pop("finish_reason", None)
        result_sha256 = item.pop("result_sha256", None)
        item["createdAt"] = item.pop("created_at")
        item["updatedAt"] = item.pop("updated_at")
        started_at = item.pop("started_at")
        elapsed_seconds = item.pop("elapsed_seconds")
        if elapsed_seconds is None and started_at is not None and item["status"] in _ACTIVE_STATUSES:
            elapsed_seconds = max(0.0, time.time() - started_at)
        item["startedAt"] = started_at
        item["elapsedSeconds"] = elapsed_seconds
        item["labId"] = item.pop("lab_id")
        item["reservationKey"] = item.pop("reservation_key")
        item["fmuFileName"] = item.pop("fmu_filename")
        item["completedCases"] = item.pop("completed_cases")
        item["totalCases"] = item.pop("total_cases")
        item["errorCode"] = item.pop("error_code")
        item["resultAvailable"] = row["result_json"] is not None
        item["fmuSha256"] = fmu_sha256
        item["authorizationExpiresAt"] = authorization_expires_at
        item["reservationEndsAt"] = reservation_ends_at
        item["finishReason"] = finish_reason
        item["warnings"] = json.loads(warnings_json or "[]")
        item["resultSha256"] = result_sha256
        item["evidence"] = json.loads(metadata_json or "{}")
        item["evidence"]["finishReason"] = finish_reason or "unknown"
        item["evidence"]["warnings"] = item["warnings"]
        item["evidence"]["resultSha256"] = result_sha256
        return item
