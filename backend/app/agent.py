"""The analysis agent.

The pipeline used to run plan -> guard -> execute in a fixed order, which is
what made the SQL guard safe: nothing reached DuckDB without passing it. An
agent chooses its own tool calls, so that ordering no longer holds and the
guard moves inside the query tool. There is no code path from the model to the
database that skips it.

Each run gets its own AnalysisRun. Tool results are recorded there so the A2UI
analysis panel can still be assembled from the last successful query, which an
agent returns through tool output rather than a return value.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.tools import tool

from .analysis import to_json_value
from .cancellation import CancellationToken
from .database import connection, rows_as_dicts
from .model_runtime import answer_text, chat_model, reasoning_text
from .semantic import semantic_prompt_context
from .skills import skill_middleware
from .sql_guard import guard_sql

logger = logging.getLogger("prism.agent")

AgentEventSink = Callable[[dict[str, Any]], None]

# An agent can loop. Each iteration is a real model call and possibly a real
# query, so the loop is bounded rather than trusted.
MAX_ITERATIONS = 8

SYSTEM_PROMPT = """You are the analyst for an internal laboratory analytics product.

Answer questions by querying the database with the execute_query tool, then
explaining the rows it returns. Never state a figure you did not get back from
a query.

Query rules:
- One read-only SELECT per call, DuckDB syntax, at most 200 rows.
- Prefer vw_laboratory_analysis when a question combines test results with
  contract, SLA, quality-target, owner, priority, budget or material-standard
  fields. Its joins are user-reviewed and published. Do not add another join to it.
- Aliases must be simple snake_case.
- Counts stay INTEGER, with aliases ending in _count where practical.
- Currency uses aliases ending in _usd and explicit ROUND(..., 2).
- Other decimals and averages use explicit ROUND(..., 2).
- Rates come back on a 0-100 scale, explicit ROUND(..., 2), aliases ending in _pct.
  Never return a 0-1 fraction for display.
- Dates come back as text via strftime(expr, '%Y-%m-%d'), month buckets as '%Y-%m'.
- Do not query the same thing twice. If a query answers the question, explain it.

Answer rules:
- Lead with the direct answer, then at most two evidence bullets.
- Preserve the values and units the query returned; do not recompute them.
- Render currency with $ and two decimals, percentages with % and at most two
  decimals, counts as integers.
- Earlier turns are supplied for context. Use them to resolve follow-ups, but
  take every figure from the current query.
- If the question is not answerable from this schema, say so and ask for what
  you need instead of guessing.
"""


@dataclass
class AnalysisRun:
    """What the A2UI analysis panel needs, collected as tools run."""

    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    answered: bool = False

    def as_analysis(self) -> dict[str, Any]:
        has_rows = bool(self.rows)
        return {
            "answer": "",
            "requires_clarification": self.sql is None,
            "table": {
                "columns": self.columns,
                "rows": self.rows,
                "row_count": len(self.rows),
                "truncated": len(self.rows) == 200,
            },
            "sql": self.sql,
            "visualization": {
                "status": "PENDING" if has_rows else "SKIPPED",
                "data": self.rows,
            },
            "warnings": self.warnings,
        }


def build_query_tool(
    run: AnalysisRun,
    event_sink: AgentEventSink | None,
    cancellation_token: CancellationToken | None,
):
    """The only path from the model to DuckDB, so the guard lives here."""
    attempts = {"n": 0}

    @tool
    def execute_query(sql: str) -> str:
        """Run one read-only SELECT against the laboratory database.

        Returns the resulting rows as JSON. Rejects anything that is not a
        read-only query against an approved table.
        """
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        attempts["n"] += 1
        guard_id = f"sql_guard:{attempts['n']}"
        query_id = f"duckdb_query:{attempts['n']}"

        def emit(event: dict[str, Any]) -> None:
            if event_sink:
                event_sink(event)

        emit({"type": "tool_call", "tool_call_id": guard_id,
              "name": "SQLGlot · validate_sql", "status": "RUNNING",
              "arguments": {"sql": sql}})
        try:
            guarded = guard_sql(sql)
        except Exception as exc:  # noqa: BLE001 - any rejection is fed back for repair.
            emit({"type": "tool_result", "tool_call_id": guard_id,
                  "name": "SQLGlot · validate_sql", "status": "FAILED",
                  "arguments": {"sql": sql}, "result": {"error": str(exc)}})
            # Returned rather than raised: the agent can repair and retry,
            # which is what the old two-attempt repair loop did by hand.
            logger.warning("sql rejected attempt=%s reason=%s sql=%r", attempts["n"], exc, sql)
            return f"REJECTED: {exc}"
        emit({"type": "tool_result", "tool_call_id": guard_id,
              "name": "SQLGlot · validate_sql", "status": "COMPLETED",
              "arguments": {"sql": sql},
              "result": {"validated_sql": guarded, "read_only": True}})

        emit({"type": "tool_call", "tool_call_id": query_id,
              "name": "DuckDB · execute_query", "status": "RUNNING",
              "arguments": {"sql": guarded}})
        try:
            with connection() as conn:
                remove_interrupt = (
                    cancellation_token.add_callback(conn.interrupt)
                    if cancellation_token
                    else lambda: None
                )
                try:
                    conn.execute(f"EXPLAIN {guarded}")
                    rows = rows_as_dicts(conn.execute(guarded))
                finally:
                    remove_interrupt()
        except Exception as exc:  # noqa: BLE001 - the agent sees the error and retries.
            if cancellation_token:
                cancellation_token.raise_if_cancelled()
            emit({"type": "tool_result", "tool_call_id": query_id,
                  "name": "DuckDB · execute_query", "status": "FAILED",
                  "arguments": {"sql": guarded}, "result": {"error": str(exc)}})
            logger.error("query failed attempt=%s error=%s sql=%r", attempts["n"], exc, guarded)
            return f"QUERY FAILED: {exc}"

        json_rows = [
            {key: to_json_value(value) for key, value in row.items()} for row in rows
        ]
        emit({"type": "tool_result", "tool_call_id": query_id,
              "name": "DuckDB · execute_query", "status": "COMPLETED",
              "arguments": {"sql": guarded},
              "result": {"row_count": len(json_rows),
                         "columns": list(json_rows[0]) if json_rows else [],
                         "rows": json_rows}})
        logger.info("query ok attempt=%s rows=%s", attempts["n"], len(json_rows))
        run.sql = guarded
        run.rows = json_rows
        run.columns = list(json_rows[0]) if json_rows else []
        run.warnings = [] if json_rows else ["Query returned no rows"]
        return json.dumps(json_rows, ensure_ascii=False, default=str)

    return execute_query


def build_agent(run: AnalysisRun, event_sink: AgentEventSink | None,
                cancellation_token: CancellationToken | None, thinking_enabled: bool):
    """Assemble the agent for one question."""
    return create_agent(
        # Always an explicit model instance: the string shorthand builds its own
        # HTTP client, which trusts proxy environment variables.
        model=chat_model(thinking_enabled),
        tools=[build_query_tool(run, event_sink, cancellation_token)],
        middleware=skill_middleware(),
        system_prompt=f"{SYSTEM_PROMPT}\n\n{semantic_prompt_context()}",
    )


def _tool_events(message: BaseMessage) -> list[dict[str, Any]]:
    """Surface skill and filesystem tool calls, which the query tool emits itself."""
    events: list[dict[str, Any]] = []
    for call in getattr(message, "tool_calls", None) or []:
        if call["name"] == "execute_query":
            continue
        events.append({
            "type": "tool_call", "tool_call_id": call.get("id") or call["name"],
            "name": f"Skill · {call['name']}", "status": "RUNNING",
            "arguments": call.get("args") or {},
        })
    return events


async def stream_agent(
    question: str,
    run: AnalysisRun,
    event_sink: AgentEventSink | None = None,
    cancellation_token: CancellationToken | None = None,
    thinking_enabled: bool = True,
    history: list[dict[str, str]] | None = None,
):
    """Run the agent, yielding the same event shapes the A2UI stream already renders."""
    # The query tool runs to completion between model chunks, so buffering its
    # events and draining between iterations keeps them in the right order
    # without marshalling across threads.
    buffered: list[dict[str, Any]] = []

    def sink(event: dict[str, Any]) -> None:
        buffered.append(event)
        if event_sink:
            event_sink(event)

    logger.info(
        "agent start thinking=%s history_turns=%s question=%r",
        thinking_enabled, len(history or []), question[:120],
    )
    agent = build_agent(run, sink, cancellation_token, thinking_enabled)
    messages = [*(history or []), {"role": "user", "content": question}]
    seen_tool_calls: set[str] = set()

    async for mode, chunk in agent.astream(
        {"messages": messages},
        stream_mode=["messages", "updates"],
        config={"recursion_limit": MAX_ITERATIONS * 2},
    ):
        while buffered:
            yield buffered.pop(0)
        if cancellation_token:
            cancellation_token.raise_if_cancelled()
        if mode == "messages":
            message = chunk[0] if isinstance(chunk, tuple) else chunk
            if not isinstance(message, AIMessageChunk):
                continue
            reasoning = reasoning_text(message)
            if reasoning:
                yield {"type": "reasoning_delta", "delta": reasoning}
            content = answer_text(message)
            if content:
                run.answered = True
                yield {"type": "content_delta", "delta": content}
            continue
        for payload in (chunk or {}).values():
            for message in (payload or {}).get("messages", []) or []:
                for event in _tool_events(message):
                    if event["tool_call_id"] in seen_tool_calls:
                        continue
                    seen_tool_calls.add(event["tool_call_id"])
                    yield event
                if message.__class__.__name__ == "ToolMessage":
                    call_id = getattr(message, "tool_call_id", "")
                    if call_id in seen_tool_calls:
                        yield {
                            "type": "tool_result", "tool_call_id": call_id,
                            "name": f"Skill · {getattr(message, 'name', 'tool')}",
                            "status": "COMPLETED",
                            "result": {"output": str(message.content)[:2000]},
                        }
    while buffered:
        yield buffered.pop(0)
    logger.info(
        "agent done queries=%s rows=%s answered=%s",
        1 if run.sql else 0, len(run.rows), run.answered,
    )
