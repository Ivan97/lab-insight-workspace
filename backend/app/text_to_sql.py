import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from .config import ROOT_DIR  # noqa: F401 - importing config loads local runtime settings.
from .conversation import with_history
from .model_runtime import apply_thinking_mode, reasoning_from_delta
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
- Earlier turns of this conversation are supplied before the current question.
  Use them to resolve follow-ups that depend on context ("now only Ceramic-C",
  "same thing by month", "sort it the other way"). Restate the resolved intent in
  the SQL rather than assuming the reader remembers the earlier turn.
- If the message asks about the conversation itself rather than the data, answer it
  directly in clarification using those earlier turns, and set sql to null.
- If the message is not an analytical data question, set clarification to a short helpful question and sql to null.
Required JSON shape:
{
  "intent": "short_snake_case",
  "sql": "SELECT ..." or null,
  "formats": {"selected_alias": "TEXT", "selected_numeric_alias": "DECIMAL_2"},
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
    clarification: str | None = None
    formats: dict[str, str] = field(default_factory=dict)


class TextToSQLPlanner:
    def __init__(self) -> None:
        self.base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "deepseek-v4-flash")

    def generate(
        self,
        question: str,
        repair_context: str | None = None,
        reasoning_sink: Callable[[str], None] | None = None,
        thinking_enabled: bool = True,
        history: list[dict[str, str]] | None = None,
    ) -> GeneratedPlan:
        if not self.base_url or not self.api_key or not self.model:
            raise ModelConfigurationError(
                "LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL."
            )
        user_prompt = f"User question: {question}"
        if repair_context:
            user_prompt += f"\nThe previous SQL was rejected. Repair it using this error: {repair_context}"
        payload = apply_thinking_mode({
            "stream": True,
            "temperature": 0,
            "messages": with_history(
                f"{SYSTEM_PROMPT}\n\n{semantic_prompt_context()}", history, user_prompt
            ),
        }, thinking_enabled)
        try:
            with httpx.Client(timeout=35, trust_env=False) as client, client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            ) as response:
                response.raise_for_status()
                content_parts: list[str] = []
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    delta = json.loads(data)["choices"][0].get("delta", {})
                    reasoning = reasoning_from_delta(delta)
                    if reasoning and reasoning_sink:
                        reasoning_sink(str(reasoning))
                    content = delta.get("content")
                    if content:
                        content_parts.append(str(content))
            content = "".join(content_parts)
            if not content:
                raise ValueError("Planner model returned no content")
            plan = self._parse_plan(content)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelRequestError(f"Text-to-SQL model request failed: {type(exc).__name__}") from exc
        return plan

    @staticmethod
    def _parse_plan(content: str) -> GeneratedPlan:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        payload = json.loads(cleaned)
        required = {"intent", "formats"}
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
        return GeneratedPlan(
            intent=str(payload["intent"]),
            sql=str(sql) if sql else None,
            clarification=str(clarification) if clarification else None,
            formats={str(key): str(value) for key, value in formats.items()},
        )
