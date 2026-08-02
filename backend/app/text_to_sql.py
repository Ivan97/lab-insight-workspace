import json
import os
import re
from dataclasses import dataclass
from typing import ClassVar

import httpx

from .config import ROOT_DIR  # noqa: F401 - importing config loads local runtime settings.

SYSTEM_PROMPT = """You are the Text-to-SQL planner for an internal laboratory analytics product.
Return one JSON object and no prose.

Available DuckDB table:
fact_test_results(
  test_result_id VARCHAR, vendor VARCHAR, project VARCHAR, material VARCHAR,
  test_name VARCHAR, submitted_date DATE, completed_date DATE, status VARCHAR,
  result VARCHAR values PASS/FAIL, cost_usd DOUBLE, turnaround_days INTEGER
)

Rules:
- Answer only questions supported by this schema.
- Produce exactly one read-only SELECT statement against fact_test_results.
- Never use external readers, DDL, DML, PRAGMA, ATTACH, COPY, or unapproved tables.
- Prefer aggregate queries. Use DuckDB syntax and a maximum of 200 result rows.
- SQL aliases must be simple snake_case names.
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
                {"role": "system", "content": SYSTEM_PROMPT},
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
        required = {"intent", "title", "tool_name", "x_field", "y_field", "rationale"}
        if not required.issubset(payload):
            raise ValueError("Planner response is missing required fields")
        sql = payload.get("sql")
        clarification = payload.get("clarification")
        if not sql and not clarification:
            raise ValueError("Planner response requires SQL or clarification")
        return GeneratedPlan(
            intent=str(payload["intent"]),
            sql=str(sql) if sql else None,
            title=str(payload["title"]),
            tool_name=str(payload["tool_name"]),
            x_field=str(payload["x_field"]),
            y_field=str(payload["y_field"]),
            rationale=str(payload["rationale"]),
            clarification=str(clarification) if clarification else None,
        )
