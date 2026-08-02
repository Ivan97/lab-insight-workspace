from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import duckdb

from .database import connection, rows_as_dicts
from .sql_guard import SQLGuardError, guard_sql
from .text_to_sql import GeneratedPlan, TextToSQLPlanner


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


def _execute_plan(plan: GeneratedPlan) -> tuple[str, list[dict[str, Any]]]:
    if not plan.sql:
        raise ValueError("The generated plan does not contain SQL")
    guarded_sql = guard_sql(plan.sql)
    with connection() as conn:
        conn.execute(f"EXPLAIN {guarded_sql}")
        rows = rows_as_dicts(conn.execute(guarded_sql))
    if rows:
        columns = set(rows[0])
        if plan.x_field not in columns or plan.y_field not in columns:
            raise ValueError("Chart fields do not match the SQL result aliases")
    return guarded_sql, [
        {key: _normalize_value(value) for key, value in row.items()} for row in rows
    ]


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time.min else value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (float, Decimal)):
        return round(float(value), 2)
    return value


def run_analysis(question: str) -> dict[str, Any]:
    planner = TextToSQLPlanner()
    plan = planner.generate(question)
    if plan.clarification:
        return _empty_analysis(plan.clarification)

    try:
        guarded_sql, rows = _execute_plan(plan)
    except (SQLGuardError, duckdb.Error, ValueError) as exc:
        plan = planner.generate(question, repair_context=str(exc))
        if plan.clarification:
            return _empty_analysis(plan.clarification)
        guarded_sql, rows = _execute_plan(plan)

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
        },
        "warnings": ["Query returned no rows"] if not rows else [],
    }
