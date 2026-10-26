"""Reservation-scoped execution history and daily simulation budgets."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
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

    def __init__(
        self,
        code: str,
        remaining: int,
        *,
        retry_after_ms: int | None = None,
        max_batch_size: int | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.remaining = remaining
        self.retry_after_ms = retry_after_ms
        self.max_batch_size = max_batch_size


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
                    created_at REAL NOT NULL,
                    payload_bytes INTEGER NOT NULL DEFAULT 0,
                    usage_day TEXT NOT NULL DEFAULT '',
                    reservation_id TEXT
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
            existing_input_event_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(input_update_events)").fetchall()
            }
            for column, definition in (
                ("payload_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("usage_day", "TEXT NOT NULL DEFAULT ''"),
                ("reservation_id", "TEXT"),
            ):
                if column not in existing_input_event_columns:
                    db.execute(f"ALTER TABLE input_update_events ADD COLUMN {column} {definition}")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_input_update_events_reservation "
                "ON input_update_events(reservation_id)"
            )
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

    def reserve_input_update(
        self,
        scope_key: str,
        access_key: str,
        payload_bytes: int,
        *,
        maximum_window_count: int | None = None,
        window_seconds: int | None = None,
    ) -> dict[str, int]:
        """Debit input update budgets independently from simulation scenarios."""
        return self._reserve_input_updates(
            scope_key,
            access_key,
            [payload_bytes],
            reserve_for_schedule=False,
            maximum_window_count=maximum_window_count,
            window_seconds=window_seconds,
        )

    def reserve_input_updates(
        self,
        scope_key: str,
        access_key: str,
        payload_sizes: list[int],
        *,
        maximum_window_count: int | None = None,
        window_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Reserve quota for timed writes until an advance reports its applied prefix."""
        return self._reserve_input_updates(
            scope_key,
            access_key,
            payload_sizes,
            reserve_for_schedule=True,
            maximum_window_count=maximum_window_count,
            window_seconds=window_seconds,
        )

    def _reserve_input_updates(
        self,
        scope_key: str,
        access_key: str,
        payload_sizes: list[int],
        *,
        reserve_for_schedule: bool,
        maximum_window_count: int | None,
        window_seconds: int | None,
    ) -> dict[str, Any]:
        if not isinstance(payload_sizes, list) or not payload_sizes:
            raise ValueError("At least one input update is required")
        if any(type(size) is not int or size < 0 for size in payload_sizes):
            raise ValueError("Input payload sizes must be non-negative integers")
        update_count = len(payload_sizes)
        effective_window_count = (
            config.MAX_INPUT_UPDATES_PER_WINDOW
            if maximum_window_count is None
            else maximum_window_count
        )
        effective_window_seconds = (
            config.INPUT_UPDATE_WINDOW_SECONDS
            if window_seconds is None
            else window_seconds
        )
        if type(effective_window_count) is not int or not 1 <= effective_window_count <= config.MAX_INPUT_UPDATES_PER_WINDOW:
            raise ValueError("Maximum input updates per window is outside the platform range")
        if type(effective_window_seconds) is not int or effective_window_seconds != config.INPUT_UPDATE_WINDOW_SECONDS:
            raise ValueError("Input update window must match the platform window")
        if reserve_for_schedule and update_count > effective_window_count:
            raise InputUpdateQuotaExceededError(
                "INPUT_UPDATE_BATCH_LIMIT",
                effective_window_count,
                max_batch_size=effective_window_count,
            )
        for payload_bytes in payload_sizes:
            if payload_bytes > config.MAX_INPUT_UPDATE_BYTES:
                raise InputUpdateQuotaExceededError("INPUT_UPDATE_TOO_LARGE", config.MAX_INPUT_UPDATE_BYTES)
        payload_bytes_total = sum(payload_sizes)
        model_key = hashlib.sha256(access_key.encode("utf-8")).hexdigest()
        usage_day = datetime.now(timezone.utc).date().isoformat()
        now = time.time()
        window_start = now - effective_window_seconds
        reservation_id = uuid.uuid4().hex if reserve_for_schedule else None
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
            if recent + update_count > effective_window_count:
                events_to_expire = recent + update_count - effective_window_count
                earliest_required_expiry = db.execute(
                    "SELECT created_at FROM input_update_events "
                    "WHERE scope_key=? AND model_key=? AND created_at>=? "
                    "ORDER BY created_at LIMIT 1 OFFSET ?",
                    (scope_key, model_key, window_start, events_to_expire - 1),
                ).fetchone()
                retry_after_ms = max(
                    1,
                    math.ceil((float(earliest_required_expiry["created_at"]) + effective_window_seconds - now) * 1000),
                ) if earliest_required_expiry else None
                raise InputUpdateQuotaExceededError(
                    "INPUT_UPDATE_RATE_LIMIT",
                    max(0, effective_window_count - recent),
                    retry_after_ms=retry_after_ms,
                )
            row = db.execute(
                "SELECT updates,payload_bytes FROM input_update_usage "
                "WHERE scope_key=? AND model_key=? AND usage_day=?",
                (scope_key, model_key, usage_day),
            ).fetchone()
            used_updates = int(row["updates"]) if row else 0
            used_bytes = int(row["payload_bytes"]) if row else 0
            if used_updates + update_count > config.MAX_INPUT_UPDATES_PER_DAY:
                raise InputUpdateQuotaExceededError(
                    "INPUT_UPDATE_DAILY_LIMIT", max(0, config.MAX_INPUT_UPDATES_PER_DAY - used_updates)
                )
            if used_bytes + payload_bytes_total > config.MAX_INPUT_UPDATE_BYTES_PER_DAY:
                raise InputUpdateQuotaExceededError(
                    "INPUT_UPDATE_DAILY_BYTES_LIMIT",
                    max(0, config.MAX_INPUT_UPDATE_BYTES_PER_DAY - used_bytes),
                )
            db.execute(
                "INSERT INTO input_update_usage(scope_key,model_key,usage_day,updates,payload_bytes) "
                "VALUES(?,?,?,?,?) ON CONFLICT(scope_key,model_key,usage_day) DO UPDATE SET "
                "updates=excluded.updates,payload_bytes=excluded.payload_bytes",
                (scope_key, model_key, usage_day, used_updates + update_count, used_bytes + payload_bytes_total),
            )
            db.executemany(
                "INSERT INTO input_update_events "
                "(scope_key,model_key,created_at,payload_bytes,usage_day,reservation_id) VALUES(?,?,?,?,?,?)",
                [(scope_key, model_key, now, size, usage_day, reservation_id) for size in payload_sizes],
            )
            result: dict[str, int | str] = {
                "updatesRemaining": config.MAX_INPUT_UPDATES_PER_DAY - used_updates - update_count,
                "bytesRemaining": config.MAX_INPUT_UPDATE_BYTES_PER_DAY - used_bytes - payload_bytes_total,
            }
            if reservation_id:
                result["reservationId"] = reservation_id
            return result

    def settle_input_updates(
        self,
        scope_key: str,
        access_key: str,
        reservation_id: str,
        *,
        applied_count: int,
    ) -> None:
        """Keep quota for applied scheduled writes and release the unconsumed suffix."""
        if type(applied_count) is not int or applied_count < 0:
            raise ValueError("Applied input update count must be a non-negative integer")
        model_key = hashlib.sha256(access_key.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT rowid,payload_bytes,usage_day FROM input_update_events "
                "WHERE scope_key=? AND model_key=? AND reservation_id=? ORDER BY rowid",
                (scope_key, model_key, reservation_id),
            ).fetchall()
            if not rows or applied_count > len(rows):
                raise ValueError("Scheduled input reservation is missing or has an invalid applied count")
            unused_rows = rows[applied_count:]
            if unused_rows:
                unused_bytes = sum(int(row["payload_bytes"]) for row in unused_rows)
                usage_day = rows[0]["usage_day"]
                usage = db.execute(
                    "SELECT updates,payload_bytes FROM input_update_usage "
                    "WHERE scope_key=? AND model_key=? AND usage_day=?",
                    (scope_key, model_key, usage_day),
                ).fetchone()
                if usage is None or usage["updates"] < len(unused_rows) or usage["payload_bytes"] < unused_bytes:
                    raise RuntimeError("Scheduled input quota reservation is inconsistent")
                next_updates = int(usage["updates"]) - len(unused_rows)
                next_bytes = int(usage["payload_bytes"]) - unused_bytes
                if next_updates == 0 and next_bytes == 0:
                    db.execute(
                        "DELETE FROM input_update_usage WHERE scope_key=? AND model_key=? AND usage_day=?",
                        (scope_key, model_key, usage_day),
                    )
                else:
                    db.execute(
                        "UPDATE input_update_usage SET updates=?,payload_bytes=? "
                        "WHERE scope_key=? AND model_key=? AND usage_day=?",
                        (next_updates, next_bytes, scope_key, model_key, usage_day),
                    )
                db.executemany("DELETE FROM input_update_events WHERE rowid=?", [(row["rowid"],) for row in unused_rows])
            applied_rows = rows[:applied_count]
            if applied_rows:
                db.executemany(
                    "UPDATE input_update_events SET reservation_id=NULL WHERE rowid=?",
                    [(row["rowid"],) for row in applied_rows],
                )

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
