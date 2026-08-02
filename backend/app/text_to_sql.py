import json
import os
import re
from dataclasses import dataclass, field
from typing import ClassVar

import httpx

from .config import ROOT_DIR  # noqa: F401 - importing config loads local runtime settings.
from .semantic import semantic_prompt_context

SYSTEM_PROMPT = """You are the Text-to-SQL planner for an internal laboratory analytics product.
Return one JSON object and no prose.

Available DuckDB tables:
fact_test_results(
  test_result_id VARCHAR, vendor VARCHAR, project VARCHAR, material VARCHAR,
  test_name VARCHAR, submitted_date DATE, completed_date DATE, status VARCHAR,
  result VARCHAR values PASS/FAIL, cost_usd DOUBLE, turnaround_days INTEGER
)
dim_vendor_contracts(
  vendor VARCHAR, contract_tier VARCHAR, region VARCHAR, contracted_cost_usd DOUBLE,
  sla_days INTEGER, quality_target_pct DOUBLE stored as 0-1, effective_date DATE
)
dim_project_budgets(
  project VARCHAR, owner VARCHAR, priority VARCHAR, approved_budget_usd DOUBLE,
  start_date DATE, end_date DATE
)
dim_material_standards(
  material VARCHAR, material_family VARCHAR, target_failure_pct DOUBLE stored on a 0-100 scale,
  max_avg_cost_usd DOUBLE, risk_tier VARCHAR
)

The current published semantic view schema and relationships are appended to this prompt at runtime.

Rules:
- Answer only questions supported by this schema.
- Produce exactly one read-only SELECT statement against one or more available tables.
- Prefer vw_laboratory_analysis for questions that combine test results with contract, SLA,
  quality-target, owner, priority, project-budget, or material-standard fields. Its joins are
  user-reviewed and published.
- Do not add another join when querying vw_laboratory_analysis.
- Never use external readers, DDL, DML, PRAGMA, ATTACH, COPY, or unapproved tables.
- Prefer aggregate queries. Use DuckDB syntax and a maximum of 200 result rows.
- SQL aliases must be simple snake_case names.
- Treat result shape and presentation as part of the SQL contract:
  - Counts stay INTEGER and use aliases ending in _count when practical.
  - Currency values use aliases ending in _usd and must be explicitly ROUND(..., 2).
  - Other calculated decimals and averages must be explicitly ROUND(..., 2).
  - Rates must be returned on a 0-100 percentage scale, explicitly ROUND(..., 2), and use aliases ending in _pct. Never return a 0-1 fraction for display.
  - Dates must be returned as text with strftime(date_expression, '%Y-%m-%d'). Do not expose 00:00:00 for date-only questions.
  - Ordered month buckets must be returned as text with strftime(date_expression, '%Y-%m').
  - Only return a timestamp when the user explicitly asks for time-of-day detail; then use '%Y-%m-%d %H:%M:%S'.
- Return a formats entry for every selected alias. Allowed format values are TEXT, INTEGER,
  DECIMAL_2, CURRENCY_USD, PERCENT_2, DATE, MONTH, and DATETIME.
- Select one chart tool from generate_column_chart, generate_bar_chart, generate_line_chart.
- x_field and y_field must exactly match selected SQL aliases.
- Use a line chart for ordered time, a bar chart for long category labels, otherwise a column chart.
- If the message is not an analytical data question, set clarification to a short helpful question and sql to null.

Required JSON shape:
{
  "intent": "short_snake_case",
  "sql": "SELECT ..." or null,
  "title": "short chart title",
  "tool_name": "generate_column_chart",
  "x_field": "selected_alias",
  "y_field": "selected_numeric_alias",
  "formats": {"selected_alias": "TEXT", "selected_numeric_alias": "DECIMAL_2"},
  "rationale": "one sentence",
  "clarification": null or "short clarification"
}
"""


class ModelConfigurationError(RuntimeError):
    pass


class ModelRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeneratedPlan:
    intent: str
    sql: str | None
    title: str
    tool_name: str
    x_field: str
    y_field: str
    rationale: str
    clarification: str | None = None
    formats: dict[str, str] = field(default_factory=dict)


class TextToSQLPlanner:
    allowed_tools: ClassVar[set[str]] = {
        "generate_column_chart",
        "generate_bar_chart",
        "generate_line_chart",
    }

    def __init__(self) -> None:
        self.base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")

    def generate(self, question: str, repair_context: str | None = None) -> GeneratedPlan:
        if not self.base_url or not self.api_key or not self.model:
            raise ModelConfigurationError(
                "LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL."
            )
        user_prompt = f"User question: {question}"
        if repair_context:
            user_prompt += f"\nThe previous SQL was rejected. Repair it using this error: {repair_context}"
        payload = {
            "model": self.model,
            "stream": False,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{semantic_prompt_context()}"},
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            with httpx.Client(timeout=35, trust_env=False) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            plan = self._parse_plan(content)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelRequestError(f"Text-to-SQL model request failed: {type(exc).__name__}") from exc
        if plan.tool_name not in self.allowed_tools:
            raise ModelRequestError("Text-to-SQL model selected an unsupported chart tool")
        return plan

    @staticmethod
    def _parse_plan(content: str) -> GeneratedPlan:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        payload = json.loads(cleaned)
        required = {"intent", "title", "tool_name", "x_field", "y_field", "formats", "rationale"}
        if not required.issubset(payload):
            raise ValueError("Planner response is missing required fields")
        sql = payload.get("sql")
        clarification = payload.get("clarification")
        if not sql and not clarification:
            raise ValueError("Planner response requires SQL or clarification")
        formats = payload.get("formats")
        allowed_formats = {
            "TEXT", "INTEGER", "DECIMAL_2", "CURRENCY_USD", "PERCENT_2", "DATE", "MONTH", "DATETIME"
        }
        if not isinstance(formats, dict) or any(value not in allowed_formats for value in formats.values()):
            raise ValueError("Planner response contains invalid result formats")
        if sql and (str(payload["x_field"]) not in formats or str(payload["y_field"]) not in formats):
            raise ValueError("Planner response is missing chart field formats")
        return GeneratedPlan(
            intent=str(payload["intent"]),
            sql=str(sql) if sql else None,
            title=str(payload["title"]),
            tool_name=str(payload["tool_name"]),
            x_field=str(payload["x_field"]),
            y_field=str(payload["y_field"]),
            rationale=str(payload["rationale"]),
            clarification=str(clarification) if clarification else None,
            formats={str(key): str(value) for key, value in formats.items()},
        )
