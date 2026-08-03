import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessageChunk

from .config import ROOT_DIR  # noqa: F401 - importing config loads local runtime settings.
from .conversation import with_history
from .model_runtime import (
    ModelConfigurationError,
    answer_text,
    chat_model,
    reasoning_text,
)
from .text_to_sql import ModelRequestError

ANSWER_SYSTEM_PROMPT = """You are a careful internal data analyst.
Answer only from the supplied query result and never invent causes or numbers.
Write concise Markdown: lead with the direct answer, then at most two evidence bullets.

Earlier turns of this conversation are supplied before the current question. Use them
only to understand what is being asked and to avoid repeating context the user already
has. Every figure you state must come from the current query result, never from an
earlier turn.

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
    async def stream_answer(
        self,
        question: str,
        analysis: dict[str, Any],
        thinking_enabled: bool = True,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[dict[str, str]]:
        prompt = f"Question: {question}\nAnalysis JSON: {json.dumps(analysis, ensure_ascii=False, default=str)}"
        messages = with_history(ANSWER_SYSTEM_PROMPT, history, prompt)
        try:
            model = chat_model(thinking_enabled, temperature=0.1)
            async for chunk in model.astream(messages):
                # Reasoning and answer text are separate content blocks, so the
                # A2UI stream keeps them in distinct events without parsing.
                reasoning = reasoning_text(chunk)
                if reasoning:
                    yield {"type": "reasoning_delta", "delta": reasoning}
                content = answer_text(chunk)
                if content:
                    yield {"type": "content_delta", "delta": content}
        except ModelConfigurationError:
            raise
        except Exception as exc:
            raise ModelRequestError(
                f"Answer model request failed: {type(exc).__name__}"
            ) from exc

    async def select_visualization_tool(
        self,
        context: dict[str, Any],
        tools: list[dict[str, Any]],
        reasoning_sink: Callable[[str], None] | None = None,
        thinking_enabled: bool = True,
    ) -> ModelToolCall | None:
        messages = [
            {"role": "system", "content": VISUALIZATION_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)},
        ]
        aggregate: AIMessageChunk | None = None
        try:
            # tool_choice stays "auto": DeepSeek rejects a forced choice while
            # thinking is enabled, and the agent must be free to select nothing.
            model = chat_model(thinking_enabled, temperature=0).bind_tools(
                tools, tool_choice="auto"
            )
            async for chunk in model.astream(messages):
                reasoning = reasoning_text(chunk)
                if reasoning and reasoning_sink:
                    reasoning_sink(reasoning)
                # LangChain merges streamed tool-call fragments for us.
                aggregate = chunk if aggregate is None else aggregate + chunk
        except ModelConfigurationError:
            raise
        except Exception as exc:
            raise ModelRequestError(
                f"Visualization tool selection failed: {type(exc).__name__}"
            ) from exc
        calls = list(aggregate.tool_calls) if aggregate else []
        if not calls:
            return None
        if len(calls) != 1:
            raise ModelRequestError("Visualization agent must select at most one tool")
        selected = calls[0]
        arguments = selected.get("args")
        if not isinstance(arguments, dict) or not selected.get("name"):
            raise ModelRequestError("Visualization agent returned an invalid tool call")
        return ModelToolCall(
            tool_call_id=selected.get("id") or f"model:{selected['name']}",
            name=str(selected["name"]),
            arguments=arguments,
        )
