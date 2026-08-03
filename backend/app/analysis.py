from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import duckdb

from .cancellation import CancellationToken
from .database import connection, rows_as_dicts
from .sql_guard import SQLGuardError, guard_sql
from .text_to_sql import GeneratedPlan, TextToSQLPlanner

AnalysisEventSink = Callable[[dict[str, Any]], None]


def _emit(event_sink: AnalysisEventSink | None, event: dict[str, Any]) -> None:
    if event_sink:
        event_sink(event)


def _empty_analysis(answer: str) -> dict[str, Any]:
    return {
        "answer": answer,
        "requires_clarification": True,
        "table": {"columns": [], "rows": [], "row_count": 0, "truncated": False},
        "sql": None,
        "visualization": {
            "status": "SKIPPED",
            "data": [],
        },
        "warnings": [],
    }


def _execute_plan(
    plan: GeneratedPlan,
    event_sink: AnalysisEventSink | None = None,
    attempt: int = 1,
    cancellation_token: CancellationToken | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    if cancellation_token:
        cancellation_token.raise_if_cancelled()
    if not plan.sql:
        raise ValueError("The generated plan does not contain SQL")
    guard_id = f"sql_guard:{attempt}"
    _emit(event_sink, {
        "type": "tool_call",
        "tool_call_id": guard_id,
        "name": "SQLGlot · validate_sql",
        "status": "RUNNING",
        "arguments": {"sql": plan.sql},
    })
    try:
        guarded_sql = guard_sql(plan.sql)
    except Exception as exc:
        _emit(event_sink, {
            "type": "tool_result",
            "tool_call_id": guard_id,
            "name": "SQLGlot · validate_sql",
            "status": "FAILED",
            "arguments": {"sql": plan.sql},
            "result": {"error": str(exc)},
        })
        raise
    _emit(event_sink, {
        "type": "tool_result",
        "tool_call_id": guard_id,
        "name": "SQLGlot · validate_sql",
        "status": "COMPLETED",
        "arguments": {"sql": plan.sql},
        "result": {"validated_sql": guarded_sql, "read_only": True},
    })

    query_id = f"duckdb_query:{attempt}"
    _emit(event_sink, {
        "type": "tool_call",
        "tool_call_id": query_id,
        "name": "DuckDB · execute_query",
        "status": "RUNNING",
        "arguments": {"sql": guarded_sql},
    })
    try:
        with connection() as conn:
            remove_interrupt = (
                cancellation_token.add_callback(conn.interrupt)
                if cancellation_token
                else lambda: None
            )
            try:
                conn.execute(f"EXPLAIN {guarded_sql}")
                rows = rows_as_dicts(conn.execute(guarded_sql))
            finally:
                remove_interrupt()
    except Exception as exc:
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        _emit(event_sink, {
            "type": "tool_result",
            "tool_call_id": query_id,
            "name": "DuckDB · execute_query",
            "status": "FAILED",
            "arguments": {"sql": guarded_sql},
            "result": {"error": str(exc)},
        })
        raise
    json_rows = [{key: to_json_value(value) for key, value in row.items()} for row in rows]
    _emit(event_sink, {
        "type": "tool_result",
        "tool_call_id": query_id,
        "name": "DuckDB · execute_query",
        "status": "COMPLETED",
        "arguments": {"sql": guarded_sql},
        "result": {
            "row_count": len(json_rows),
            "columns": list(json_rows[0]) if json_rows else [],
            "rows": json_rows,
        },
    })
    return guarded_sql, json_rows


def to_json_value(value: Any) -> Any:
    """Convert database-native objects for transport without applying display policy."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def run_analysis(
    question: str,
    event_sink: AnalysisEventSink | None = None,
    cancellation_token: CancellationToken | None = None,
    thinking_enabled: bool = True,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    planner = TextToSQLPlanner()
    def reasoning_sink(chunk: str) -> None:
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        _emit(event_sink, {"type": "reasoning_delta", "delta": chunk})

    if cancellation_token:
        cancellation_token.raise_if_cancelled()
    plan = planner.generate(
        question,
        reasoning_sink=reasoning_sink if thinking_enabled else None,
        thinking_enabled=thinking_enabled,
        history=history,
    )
    if plan.clarification:
        return _empty_analysis(plan.clarification)

    try:
        guarded_sql, rows = _execute_plan(
            plan, event_sink, attempt=1, cancellation_token=cancellation_token
        )
    except (SQLGuardError, duckdb.Error, ValueError) as exc:
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        plan = planner.generate(
            question,
            repair_context=str(exc),
            reasoning_sink=reasoning_sink if thinking_enabled else None,
            thinking_enabled=thinking_enabled,
            history=history,
        )
        if plan.clarification:
            return _empty_analysis(plan.clarification)
        guarded_sql, rows = _execute_plan(
            plan, event_sink, attempt=2, cancellation_token=cancellation_token
        )

    return {
        "answer": "The query completed successfully. Base the response only on its returned rows.",
        "requires_clarification": False,
        "table": {
            "columns": list(rows[0]) if rows else [],
            "formats": plan.formats,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) == 200,
        },
        "sql": guarded_sql,
        "visualization": {
            "status": "PENDING" if rows else "SKIPPED",
            "data": rows,
            "formats": plan.formats,
        },
        "warnings": ["Query returned no rows"] if not rows else [],
    }
