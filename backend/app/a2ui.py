import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from .agent import AnalysisRun, stream_agent
from .cancellation import AnalysisCancelled, CancellationToken
from .config import CATALOG_ID
from .conversation import recent_messages
from .database import connection, json_dumps, utcnow
from .mcp_chart import McpVisualizationClient

logger = logging.getLogger("prism.stream")

COMPONENTS = [
    {"id": "root", "component": "Column", "children": ["eventStream", "analysis"]},
    {
        "id": "eventStream",
        "component": "AgentEventStream",
        "events": {"path": "/events"},
    },
    {
        "id": "analysis",
        "component": "AnalysisResult",
        "analysis": {"path": "/analysis"},
        "artifacts": {"path": "/artifacts"},
    },
]


class A2UIValidationError(ValueError):
    pass


def replay_envelopes(message_id: str, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild a completed answer from its stored snapshot.

    Restoring history must never re-run the analysis, so a reloaded page
    replays these envelopes instead of opening a new stream.
    """
    surface_id = f"message:{message_id}"
    envelopes = [
        {
            "version": "v0.9.1",
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": CATALOG_ID,
                "theme": {"primaryColor": "#007AFF"},
                "sendDataModel": False,
            },
        },
        {
            "version": "v0.9.1",
            "updateComponents": {"surfaceId": surface_id, "components": COMPONENTS},
        },
        {
            "version": "v0.9.1",
            "updateDataModel": {"surfaceId": surface_id, "path": "/", "value": snapshot},
        },
    ]
    for envelope in envelopes:
        validate_envelope(envelope)
    return envelopes


class A2UIStream:
    def __init__(
        self,
        conversation_id: str,
        question: str,
        message_id: str | None = None,
        resume_after: int = 0,
        reasoning_enabled: bool = False,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.question = question
        self.message_id = message_id or str(uuid.uuid4())
        self.surface_id = f"message:{self.message_id}"
        self.sequence = 0
        self.resume_after = resume_after
        self.reasoning_enabled = reasoning_enabled
        self.cancellation_token = cancellation_token or CancellationToken()
        self.tool_sequence = 0
        self.tool_sequences: dict[str, int] = {}
        self.model: dict[str, Any] = {
            "events": [],
            "reasoning": {"segments": [], "status": "IDLE"},
            "content": {"markdown": ""},
            "toolGroups": {},
            "artifacts": [],
            "analysis": {},
            "status": "STREAMING",
        }

    async def events(self) -> AsyncIterator[str]:
        self._create_message()
        # Loaded after the current turn exists so it can be excluded by timestamp.
        history = recent_messages(self.conversation_id, self.message_id)
        yield self._sse(
            self._envelope(
                "createSurface",
                {
                    "surfaceId": self.surface_id,
                    "catalogId": CATALOG_ID,
                    "theme": {"primaryColor": "#007AFF"},
                    "sendDataModel": False,
                },
            )
        )
        yield self._sse(
            self._envelope(
                "updateComponents", {"surfaceId": self.surface_id, "components": COMPONENTS}
            )
        )
        yield self._sse(self._update("/", self.model))

        run = AnalysisRun()
        markdown = ""
        try:
            async for event in stream_agent(
                self.question,
                run,
                cancellation_token=self.cancellation_token,
                thinking_enabled=self.reasoning_enabled,
                history=history,
            ):
                kind = event["type"]
                if kind == "reasoning_delta":
                    if self.reasoning_enabled:
                        yield self._sse(self._update_reasoning_delta(event["delta"], "RUNNING"))
                    continue
                if kind == "content_delta":
                    markdown += event["delta"]
                    self.model["content"]["markdown"] = markdown
                    yield self._sse(self._update_content_delta(event["delta"]))
                    continue
                tool_call_id = event["tool_call_id"]
                yield self._sse(
                    self._update_tool(
                        tool_call_id,
                        event["name"],
                        event["status"],
                        self._tool_sequence(tool_call_id),
                        arguments=event.get("arguments"),
                        result=event.get("result"),
                    )
                )
        except AnalysisCancelled:
            logger.info("stream cancelled message=%s", self.message_id)
            yield self._sse(self._update("/status", "CANCELLED"))
            return
        except Exception:
            logger.exception("stream failed message=%s", self.message_id)
            async for event in self._failure_events(
                "无法完成真实数据查询。请检查模型配置或换一种数据问题后重试。"
            ):
                yield event
            return
        if self._is_cancelled():
            yield self._sse(self._update("/status", "CANCELLED"))
            return
        analysis = run.as_analysis()
        if analysis["visualization"]["status"] == "PENDING":
            chart_client = McpVisualizationClient()
            chart_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            def receive_chart_event(event: dict[str, Any]) -> None:
                if event["type"] == "reasoning_delta" and not self.reasoning_enabled:
                    return
                chart_queue.put_nowait(event)

            chart_task = asyncio.create_task(
                chart_client.render(
                    self.question,
                    analysis,
                    receive_chart_event,
                    self.reasoning_enabled,
                )
            )
            try:
                while not chart_task.done() or not chart_queue.empty():
                    try:
                        event = await asyncio.wait_for(chart_queue.get(), timeout=0.05)
                    except TimeoutError:
                        continue
                    if event["type"] == "reasoning_delta":
                        yield self._sse(
                            self._update_reasoning_delta(event["delta"], "RUNNING")
                        )
                        continue
                    tool_call_id = event["tool_call_id"]
                    yield self._sse(
                        self._update_tool(
                            tool_call_id,
                            event["name"],
                            event["status"],
                            self._tool_sequence(tool_call_id),
                            arguments=event.get("arguments"),
                            result=event.get("result"),
                        )
                    )
                visualization = await chart_task
            finally:
                if not chart_task.done():
                    chart_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await chart_task
            if self._is_cancelled():
                yield self._sse(self._update("/status", "CANCELLED"))
                return
            analysis["visualization"] = visualization

        self.model["analysis"] = analysis
        yield self._sse(self._update("/analysis", analysis))
        if self.reasoning_enabled:
            self.model["reasoning"]["status"] = "COMPLETED"
            yield self._sse(self._complete_reasoning_events("COMPLETED"))
        self.model["status"] = "COMPLETED"
        yield self._sse(self._update("/status", "COMPLETED"))
        self._complete_message()

    def _update_reasoning(self, text: str, status: str) -> dict[str, Any]:
        segment = {
            "id": str(uuid.uuid4()), "text": text, "createdAt": datetime.now(UTC).isoformat()
        }
        self.model["reasoning"]["segments"].append(segment)
        self.model["reasoning"]["status"] = status
        events = self.model["events"]
        if events and events[-1]["type"] == "reasoning" and events[-1]["status"] == status:
            events[-1]["segments"].append(segment)
        else:
            events.append({
                "id": str(uuid.uuid4()), "type": "reasoning",
                "segments": [segment], "status": status,
            })
        return self._update("/events", events)

    def _update_reasoning_delta(self, delta: str, status: str) -> dict[str, Any]:
        events = self.model["events"]
        if events and events[-1]["type"] == "reasoning" and events[-1]["status"] == status:
            segment = events[-1]["segments"][-1]
            segment["text"] += delta
        else:
            segment = {
                "id": str(uuid.uuid4()),
                "text": delta,
                "createdAt": datetime.now(UTC).isoformat(),
            }
            events.append({
                "id": str(uuid.uuid4()), "type": "reasoning",
                "segments": [segment], "status": status,
            })
            self.model["reasoning"]["segments"].append(segment)
        self.model["reasoning"]["status"] = status
        return self._update("/events", events)

    def _update_tool(
        self,
        key: str,
        name: str,
        status: str,
        sequence: int,
        summary: str | None = None,
        arguments: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._complete_active_reasoning()
        group = self.model["toolGroups"].setdefault(
            "analysis", {"groupId": "analysis", "calls": []}
        )
        existing = next((item for item in group["calls"] if item["toolCallId"] == key), None)
        payload = {
            "toolCallId": key,
            "displayName": name,
            "status": status,
            "sequence": sequence,
            "summary": summary,
            "arguments": arguments,
            "result": result,
        }
        if existing:
            existing.update(payload)
        else:
            group["calls"].append(payload)
        events = self.model["events"]
        event_call = None
        for event in events:
            if event["type"] == "tool_group":
                event_call = next(
                    (call for call in event["calls"] if call["toolCallId"] == key), None
                )
                if event_call:
                    break
        if event_call:
            event_call.update(payload)
        elif events and events[-1]["type"] == "tool_group":
            events[-1]["calls"].append(payload.copy())
        else:
            events.append({
                "id": str(uuid.uuid4()), "type": "tool_group", "calls": [payload.copy()]
            })
        return self._update("/events", events)

    def _update_content(self, markdown: str) -> dict[str, Any]:
        self._complete_active_reasoning()
        events = self.model["events"]
        events.append({"id": str(uuid.uuid4()), "type": "content", "markdown": markdown})
        return self._update("/events", events)

    def _update_content_delta(self, chunk: str) -> dict[str, Any]:
        self._complete_active_reasoning()
        events = self.model["events"]
        if events and events[-1]["type"] == "content":
            events[-1]["markdown"] += chunk
        else:
            events.append({"id": str(uuid.uuid4()), "type": "content", "markdown": chunk})
        return self._update("/events", events)

    def _complete_reasoning_events(self, status: str) -> dict[str, Any]:
        for event in self.model["events"]:
            if event["type"] == "reasoning" and event["status"] == "RUNNING":
                event["status"] = status
        return self._update("/events", self.model["events"])

    def _complete_active_reasoning(self) -> None:
        events = self.model["events"]
        if events and events[-1]["type"] == "reasoning" and events[-1]["status"] == "RUNNING":
            events[-1]["status"] = "COMPLETED"

    def _tool_sequence(self, tool_call_id: str) -> int:
        if tool_call_id not in self.tool_sequences:
            self.tool_sequence += 1
            self.tool_sequences[tool_call_id] = self.tool_sequence
        return self.tool_sequences[tool_call_id]

    def _update(self, path: str, value: Any) -> dict[str, Any]:
        return self._envelope(
            "updateDataModel", {"surfaceId": self.surface_id, "path": path, "value": value}
        )

    def _envelope(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        envelope = {"version": "v0.9.1", kind: payload}
        validate_envelope(envelope)
        return envelope

    def _sse(self, envelope: dict[str, Any]) -> str:
        self.sequence += 1
        if self.sequence <= self.resume_after:
            return ""
        event_id = f"{self.message_id}:{self.sequence}"
        with connection() as conn:
            conn.execute(
                "INSERT INTO a2ui_events VALUES (?, ?, ?, ?, ?)",
                [event_id, self.message_id, self.sequence, json_dumps(envelope), utcnow()],
            )
        return f"id: {event_id}\nevent: a2ui\ndata: {json_dumps(envelope)}\n\n"

    def _create_message(self) -> None:
        with connection() as conn:
            if conn.execute(
                "SELECT 1 FROM messages WHERE message_id = ?", [self.message_id]
            ).fetchone():
                return
            now = utcnow()
            first_turn = not conn.execute(
                "SELECT 1 FROM messages WHERE conversation_id = ? LIMIT 1",
                [self.conversation_id],
            ).fetchone()
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, 'USER', ?, 'COMPLETED', NULL, ?, ?)",
                [str(uuid.uuid4()), self.conversation_id, self.question, now, now],
            )
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, 'ASSISTANT', '', 'STREAMING', NULL, ?, NULL)",
                [self.message_id, self.conversation_id, now],
            )
            # The sidebar reads titles from the database, so the first question
            # has to name the conversation there rather than only in the client.
            if first_turn:
                conn.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE conversation_id = ?",
                    [self.question[:60], now, self.conversation_id],
                )
            else:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                    [now, self.conversation_id],
                )

    def _complete_message(self) -> None:
        with connection() as conn:
            conn.execute(
                "UPDATE messages SET content = ?, status = 'COMPLETED', a2ui_surface_snapshot = ?, completed_at = ? WHERE message_id = ?",
                [
                    self.model["content"]["markdown"],
                    json_dumps(self.model),
                    utcnow(),
                    self.message_id,
                ],
            )

    async def _failure_events(self, message: str) -> AsyncIterator[str]:
        self.model["content"]["markdown"] = message
        yield self._sse(self._update_content(message))
        if self.reasoning_enabled:
            self.model["reasoning"]["status"] = "COMPLETED"
            yield self._sse(self._complete_reasoning_events("COMPLETED"))
        self.model["status"] = "FAILED"
        yield self._sse(self._update("/status", "FAILED"))
        with connection() as conn:
            conn.execute(
                "UPDATE messages SET content = ?, status = 'FAILED', a2ui_surface_snapshot = ?, completed_at = ? WHERE message_id = ?",
                [message, json_dumps(self.model), utcnow(), self.message_id],
            )

    def _is_cancelled(self) -> bool:
        if self.cancellation_token.cancelled:
            return True
        with connection() as conn:
            row = conn.execute(
                "SELECT status FROM messages WHERE message_id = ?", [self.message_id]
            ).fetchone()
        return bool(row and row[0] == "CANCELLED")


def validate_envelope(envelope: dict[str, Any]) -> None:
    if envelope.get("version") != "v0.9.1":
        raise A2UIValidationError("A2UI version must be v0.9.1")
    kinds = [
        key
        for key in ("createSurface", "updateComponents", "updateDataModel", "deleteSurface")
        if key in envelope
    ]
    if len(kinds) != 1:
        raise A2UIValidationError("Envelope must contain exactly one A2UI message type")
    payload = envelope[kinds[0]]
    if not isinstance(payload, dict) or not payload.get("surfaceId"):
        raise A2UIValidationError("A2UI payload requires surfaceId")
    if kinds[0] == "createSurface" and payload.get("catalogId") != CATALOG_ID:
        raise A2UIValidationError("Unsupported catalogId")
    if kinds[0] == "updateComponents":
        ids = [component.get("id") for component in payload.get("components", [])]
        if "root" not in ids or len(ids) != len(set(ids)):
            raise A2UIValidationError("Component tree requires unique IDs and root")
