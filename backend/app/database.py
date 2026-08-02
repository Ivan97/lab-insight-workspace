import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import duckdb

from .config import DATABASE_PATH

_lock = threading.RLock()


@contextmanager
def connection() -> Iterator[duckdb.DuckDBPyConnection]:
    with _lock:
        conn = duckdb.connect(str(DATABASE_PATH))
        try:
            yield conn
        finally:
            conn.close()


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
                quality_score DOUBLE NOT NULL,
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


def rows_as_dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
