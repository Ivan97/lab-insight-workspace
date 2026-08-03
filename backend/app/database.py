import atexit
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import duckdb

from .config import DATABASE_PATH


class _FairLock:
    """Serialize DuckDB connections without starving queued request threads."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._next_ticket = 0
        self._serving_ticket = 0

    def __enter__(self) -> None:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            while ticket != self._serving_ticket:
                self._condition.wait()

    def __exit__(self, *_: object) -> None:
        with self._condition:
            self._serving_ticket += 1
            self._condition.notify_all()


_lock = _FairLock()
_database_connection: duckdb.DuckDBPyConnection | None = None


def _get_connection() -> duckdb.DuckDBPyConnection:
    global _database_connection
    if _database_connection is None:
        _database_connection = duckdb.connect(str(DATABASE_PATH))
    return _database_connection


def _close_connection() -> None:
    global _database_connection
    with _lock:
        if _database_connection is not None:
            _database_connection.close()
            _database_connection = None


atexit.register(_close_connection)


@contextmanager
def connection() -> Iterator[duckdb.DuckDBPyConnection]:
    with _lock:
        yield _get_connection()


def init_schema() -> None:
    with connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ingestion_batches (
                batch_id VARCHAR PRIMARY KEY,
                source_type VARCHAR NOT NULL,
                source_name VARCHAR NOT NULL,
                vendor_hint VARCHAR,
                status VARCHAR NOT NULL,
                record_count INTEGER NOT NULL,
                current_stage VARCHAR,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS field_mappings (
                batch_id VARCHAR PRIMARY KEY,
                version INTEGER NOT NULL,
                payload JSON NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingestion_payloads (
                batch_id VARCHAR PRIMARY KEY,
                profile JSON NOT NULL,
                preview JSON NOT NULL
            );
            CREATE TABLE IF NOT EXISTS join_rules (
                rule_id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                left_table VARCHAR NOT NULL,
                left_field VARCHAR NOT NULL,
                right_table VARCHAR NOT NULL,
                right_field VARCHAR NOT NULL,
                join_type VARCHAR NOT NULL,
                relationship VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                view_name VARCHAR NOT NULL,
                matched_pct DOUBLE NOT NULL,
                right_key_unique BOOLEAN NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fact_test_results (
                test_result_id VARCHAR PRIMARY KEY,
                vendor VARCHAR NOT NULL,
                project VARCHAR NOT NULL,
                material VARCHAR NOT NULL,
                test_name VARCHAR NOT NULL,
                submitted_date DATE NOT NULL,
                completed_date DATE,
                status VARCHAR NOT NULL,
                result VARCHAR,
                cost_usd DOUBLE NOT NULL,
                turnaround_days INTEGER
            );
            CREATE TABLE IF NOT EXISTS dim_vendor_contracts (
                vendor VARCHAR PRIMARY KEY,
                contract_tier VARCHAR NOT NULL,
                region VARCHAR NOT NULL,
                contracted_cost_usd DOUBLE NOT NULL,
                sla_days INTEGER NOT NULL,
                quality_target_pct DOUBLE NOT NULL,
                effective_date DATE NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dim_project_budgets (
                project VARCHAR PRIMARY KEY,
                owner VARCHAR NOT NULL,
                priority VARCHAR NOT NULL,
                approved_budget_usd DOUBLE NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dim_material_standards (
                material VARCHAR PRIMARY KEY,
                material_family VARCHAR NOT NULL,
                target_failure_pct DOUBLE NOT NULL,
                max_avg_cost_usd DOUBLE NOT NULL,
                risk_tier VARCHAR NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id VARCHAR PRIMARY KEY,
                title VARCHAR NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                message_id VARCHAR PRIMARY KEY,
                conversation_id VARCHAR NOT NULL,
                role VARCHAR NOT NULL,
                content VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                a2ui_surface_snapshot JSON,
                created_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS a2ui_events (
                event_id VARCHAR PRIMARY KEY,
                message_id VARCHAR NOT NULL,
                sequence INTEGER NOT NULL,
                envelope JSON NOT NULL,
                created_at TIMESTAMP NOT NULL,
                UNIQUE(message_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS stream_requests (
                idempotency_key VARCHAR PRIMARY KEY,
                conversation_id VARCHAR NOT NULL,
                message_id VARCHAR NOT NULL,
                question VARCHAR NOT NULL,
                created_at TIMESTAMP NOT NULL
            );
            """
        )
        # quality_score was a hardcoded constant that never described the source.
        # Drop it from databases created before it was removed.
        conn.execute("ALTER TABLE ingestion_batches DROP COLUMN IF EXISTS quality_score")


def rows_as_dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
