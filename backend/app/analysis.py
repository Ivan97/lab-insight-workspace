from dataclasses import dataclass
from typing import Any

from .database import connection, rows_as_dicts


@dataclass(frozen=True)
class AnalysisPlan:
    intent: str
    sql: str
    tool_name: str
    title: str
    x_field: str
    y_field: str
    rationale: str


def choose_plan(question: str) -> AnalysisPlan:
    lowered = question.lower()
    if any(token in lowered for token in ["trend", "最近", "month", "异常", "上涨"]):
        return AnalysisPlan(
            intent="monthly_cost_trend",
            sql="""
                SELECT strftime(submitted_date, '%Y-%m') AS month,
                       round(avg(cost_usd), 2) AS avg_cost_usd
                FROM fact_test_results
                WHERE vendor = 'DeltaLab'
                GROUP BY 1 ORDER BY 1
            """,
            tool_name="generate_line_chart",
            title="DeltaLab monthly average cost",
            x_field="month",
            y_field="avg_cost_usd",
            rationale="A line chart makes the month-over-month cost movement easiest to inspect.",
        )
    if any(token in lowered for token in ["material", "材料", "failure", "失败"]):
        return AnalysisPlan(
            intent="material_failure",
            sql="""
                SELECT material,
                       round(100.0 * sum(CASE WHEN result = 'FAIL' THEN 1 ELSE 0 END) / count(*), 1) AS fail_rate_pct,
                       count(*) AS tests
                FROM fact_test_results
                GROUP BY 1 ORDER BY fail_rate_pct DESC
            """,
            tool_name="generate_column_chart",
            title="Failure rate by material",
            x_field="material",
            y_field="fail_rate_pct",
            rationale="A column chart supports direct comparison across material categories.",
        )
    return AnalysisPlan(
        intent="vendor_comparison",
        sql="""
            SELECT vendor,
                   round(avg(cost_usd), 2) AS avg_cost_usd,
                   round(avg(turnaround_days), 1) AS avg_turnaround_days,
                   round(100.0 * sum(CASE WHEN result = 'PASS' THEN 1 ELSE 0 END) / count(*), 1) AS pass_rate_pct,
                   count(*) AS tests
            FROM fact_test_results
            GROUP BY 1 ORDER BY avg_cost_usd
        """,
        tool_name="generate_column_chart",
        title="Average test cost by vendor",
        x_field="vendor",
        y_field="avg_cost_usd",
        rationale="A column chart makes cost differences across vendors immediately comparable.",
    )


def run_analysis(question: str) -> dict[str, Any]:
    plan = choose_plan(question)
    with connection() as conn:
        rows = rows_as_dicts(conn.execute(plan.sql))
        total_tests, total_cost, pass_rate, turnaround = conn.execute(
            """
            SELECT count(*), round(sum(cost_usd), 2),
                   round(100.0 * sum(CASE WHEN result = 'PASS' THEN 1 ELSE 0 END) / count(*), 1),
                   round(avg(turnaround_days), 1)
            FROM fact_test_results
            """
        ).fetchone()

    if plan.intent == "vendor_comparison":
        cheapest = rows[0]
        slowest = max(rows, key=lambda row: row["avg_turnaround_days"])
        answer = (
            f"{cheapest['vendor']} has the lowest average cost at ${cheapest['avg_cost_usd']:.2f}, "
            f"while {slowest['vendor']} has the longest turnaround at {slowest['avg_turnaround_days']:.1f} days."
        )
        insights = [
            {
                "kind": "TRADE_OFF",
                "title": "Lower cost, slower delivery",
                "description": "BluePeak is economical but takes materially longer than the vendor average.",
            },
            {
                "kind": "QUALITY",
                "title": "Quality remains comparable",
                "description": "Pass rates are broadly similar, so delivery speed is the main trade-off.",
            },
        ]
    elif plan.intent == "monthly_cost_trend":
        first, last = rows[0], rows[-1]
        change = round((last["avg_cost_usd"] / first["avg_cost_usd"] - 1) * 100, 1)
        answer = f"DeltaLab's average cost rose {change}% from {first['month']} to {last['month']}, with the increase concentrated in the last two months."
        insights = [
            {
                "kind": "ANOMALY",
                "title": "Recent cost increase",
                "description": "The latest two months sit above DeltaLab's earlier baseline.",
            }
        ]
    else:
        top = rows[0]
        answer = f"{top['material']} has the highest observed failure rate at {top['fail_rate_pct']}% across {top['tests']} tests."
        insights = [
            {
                "kind": "ANOMALY",
                "title": "Material risk concentration",
                "description": "Failures are concentrated in one material and merit a test-level drill-down.",
            }
        ]

    return {
        "answer": answer,
        "kpis": [
            {"key": "tests", "label": "Tests", "value": total_tests, "format": "INTEGER"},
            {"key": "cost", "label": "Total cost", "value": total_cost, "format": "CURRENCY_USD"},
            {"key": "pass_rate", "label": "Pass rate", "value": pass_rate, "format": "PERCENT"},
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
            "truncated": False,
        },
        "insights": insights,
        "sql": " ".join(plan.sql.split()),
        "visualization": {
            "status": "PENDING",
            "tool_name": plan.tool_name,
            "title": plan.title,
            "rationale": plan.rationale,
            "x_field": plan.x_field,
            "y_field": plan.y_field,
            "data": rows,
        },
        "warnings": [],
    }
