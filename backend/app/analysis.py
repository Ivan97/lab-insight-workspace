from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import duckdb

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
        "kpis": [],
        "table": {"columns": [], "rows": [], "row_count": 0, "truncated": False},
        "insights": [],
        "sql": None,
        "visualization": {
            "status": "SKIPPED",
            "tool_name": "generate_column_chart",
            "title": "No chart generated",
            "rationale": "A data question is required before querying the analytical view.",
            "x_field": "",
            "y_field": "",
            "data": [],
        },
        "warnings": [],
    }


def _execute_plan(
    plan: GeneratedPlan,
    event_sink: AnalysisEventSink | None = None,
    attempt: int = 1,
) -> tuple[str, list[dict[str, Any]]]:
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
            conn.execute(f"EXPLAIN {guarded_sql}")
            rows = rows_as_dicts(conn.execute(guarded_sql))
    except Exception as exc:
        _emit(event_sink, {
            "type": "tool_result",
            "tool_call_id": query_id,
            "name": "DuckDB · execute_query",
            "status": "FAILED",
            "arguments": {"sql": guarded_sql},
            "result": {"error": str(exc)},
        })
        raise
    if rows:
        columns = set(rows[0])
        if plan.x_field not in columns or plan.y_field not in columns:
            error = "Chart fields do not match the SQL result aliases"
            _emit(event_sink, {
                "type": "tool_result",
                "tool_call_id": query_id,
                "name": "DuckDB · execute_query",
                "status": "FAILED",
                "arguments": {"sql": guarded_sql},
                "result": {"error": error, "columns": list(rows[0])},
            })
            raise ValueError(error)
    json_rows = [{key: _to_json_value(value) for key, value in row.items()} for row in rows]
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


def _to_json_value(value: Any) -> Any:
    """Convert database-native objects for transport without applying display policy."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def run_analysis(
    question: str, event_sink: AnalysisEventSink | None = None
) -> dict[str, Any]:
    planner = TextToSQLPlanner()
    reasoning_sink = lambda chunk: _emit(
        event_sink, {"type": "reasoning_delta", "delta": chunk}
    )
    plan = planner.generate(question, reasoning_sink=reasoning_sink)
    if plan.clarification:
        return _empty_analysis(plan.clarification)

    try:
        guarded_sql, rows = _execute_plan(plan, event_sink, attempt=1)
    except (SQLGuardError, duckdb.Error, ValueError) as exc:
        plan = planner.generate(
            question, repair_context=str(exc), reasoning_sink=reasoning_sink
        )
        if plan.clarification:
            return _empty_analysis(plan.clarification)
        guarded_sql, rows = _execute_plan(plan, event_sink, attempt=2)

    with connection() as conn:
        total_tests, total_cost, pass_rate, turnaround = conn.execute(
            """
            SELECT count(*), round(sum(cost_usd), 2),
                   round(100.0 * sum(CASE WHEN result = 'PASS' THEN 1 ELSE 0 END) / count(*), 1),
                   round(avg(turnaround_days), 1)
            FROM fact_test_results
            """
        ).fetchone()

    return {
        "answer": "The query completed successfully. Base the response only on its returned rows.",
        "requires_clarification": False,
        "kpis": [
            {"key": "tests", "label": "Tests", "value": total_tests, "format": "INTEGER"},
            {
                "key": "cost",
                "label": "Total cost",
                "value": total_cost,
                "format": "CURRENCY_USD",
            },
            {
                "key": "pass_rate",
                "label": "Pass rate",
                "value": pass_rate,
                "format": "PERCENT",
            },
            {
                "key": "turnaround",
                "label": "Avg. turnaround",
                "value": turnaround,
                "format": "DAYS",
            },
        ],
        "table": {
            "columns": list(rows[0]) if rows else [],
            "formats": plan.formats,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) == 200,
        },
        "insights": [],
        "sql": guarded_sql,
        "visualization": {
            "status": "PENDING" if rows else "SKIPPED",
            "tool_name": plan.tool_name,
            "title": plan.title,
            "rationale": plan.rationale,
            "x_field": plan.x_field,
            "y_field": plan.y_field,
            "data": rows,
            "formats": plan.formats,
        },
        "warnings": ["Query returned no rows"] if not rows else [],
    }
