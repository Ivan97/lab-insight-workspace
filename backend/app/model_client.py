import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .config import ROOT_DIR  # noqa: F401 - importing config loads local runtime settings.
from .text_to_sql import ModelConfigurationError, ModelRequestError

ANSWER_SYSTEM_PROMPT = """You are a careful internal data analyst.
Answer only from the supplied query result and never invent causes or numbers.
Write concise Markdown: lead with the direct answer, then at most two evidence bullets.

Presentation contract:
- Preserve the values and units returned by SQL; do not recompute metrics.
- Render date-only values as YYYY-MM-DD and month values as YYYY-MM. Never append 00:00:00.
- Render currency in USD with a $ sign, thousands separators, and two decimal places.
- Render percentages with a % sign and at most two decimal places. The SQL result is already on a 0-100 scale; never multiply it by 100 again.
- Render other decimal measures with at most two decimal places and avoid floating-point noise.
- Render counts as integers.
- Use the supplied table.formats metadata as the authoritative formatting contract.
"""

VISUALIZATION_SYSTEM_PROMPT = """You are a visualization agent.
You receive a user question, SQL result rows, display formats, and dynamically discovered MCP tools.
Call exactly one tool only when a chart materially improves the answer; otherwise return text and no tool call.
Choose from the supplied tools without assuming any fixed chart taxonomy.
Construct arguments strictly from the selected tool's JSON Schema.
Preserve the supplied data values exactly; do not invent, aggregate, or recompute values.
Use a concise title and readable labels when the selected schema supports them.
"""


@dataclass(frozen=True)
class ModelToolCall:
    tool_call_id: str
    name: str
    arguments: dict[str, Any]


class OpenAICompatibleModel:
    def __init__(self) -> None:
        self.base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "deepseek-reasoner")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    async def stream_answer(
        self, question: str, analysis: dict[str, Any]
    ) -> AsyncIterator[dict[str, str]]:
        if not self.configured:
            raise ModelConfigurationError(
                "LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL."
            )
        prompt = f"Question: {question}\nAnalysis JSON: {json.dumps(analysis, ensure_ascii=False, default=str)}"
        payload = {
            "model": self.model,
            "stream": True,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/chat/completions"
        try:
            async with (
                httpx.AsyncClient(timeout=35, trust_env=False) as client,
                client.stream("POST", url, json=payload, headers=headers) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    delta = json.loads(data)["choices"][0].get("delta", {})
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        yield {"type": "reasoning_delta", "delta": str(reasoning)}
                    content = delta.get("content")
                    if content:
                        yield {"type": "content_delta", "delta": str(content)}
        except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ModelRequestError(
                f"Answer model request failed: {type(exc).__name__}"
            ) from exc

    async def select_visualization_tool(
        self,
        context: dict[str, Any],
        tools: list[dict[str, Any]],
        reasoning_sink: Callable[[str], None] | None = None,
    ) -> ModelToolCall | None:
        if not self.configured:
            raise ModelConfigurationError(
                "LLM is not configured. Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL."
            )
        payload = {
            "model": self.model,
            "stream": True,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": VISUALIZATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False, default=str),
                },
            ],
            "tools": tools,
            "tool_choice": "auto",
        }
        calls: dict[int, dict[str, str]] = {}
        try:
            async with (
                httpx.AsyncClient(timeout=45, trust_env=False) as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                ) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    delta = json.loads(data)["choices"][0].get("delta", {})
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning and reasoning_sink:
                        reasoning_sink(str(reasoning))
                    for tool_delta in delta.get("tool_calls") or []:
                        index = int(tool_delta.get("index", 0))
                        current = calls.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        current["id"] += str(tool_delta.get("id") or "")
                        function = tool_delta.get("function") or {}
                        current["name"] += str(function.get("name") or "")
                        current["arguments"] += str(function.get("arguments") or "")
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelRequestError(
                f"Visualization tool selection failed: {type(exc).__name__}"
            ) from exc
        if not calls:
            return None
        if len(calls) != 1:
            raise ModelRequestError("Visualization agent must select at most one tool")
        selected = calls[min(calls)]
        arguments = json.loads(selected["arguments"] or "{}")
        if not isinstance(arguments, dict) or not selected["name"]:
            raise ModelRequestError("Visualization agent returned an invalid tool call")
        return ModelToolCall(
            tool_call_id=selected["id"] or f"model:{selected['name']}",
            name=selected["name"],
            arguments=arguments,
        )
