import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from .analysis import run_analysis
from .config import CATALOG_ID
from .database import connection, json_dumps, utcnow
from .mcp_chart import AntVChartClient
from .model_client import OpenAICompatibleModel

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


class A2UIStream:
    def __init__(
        self,
        conversation_id: str,
        question: str,
        message_id: str | None = None,
        resume_after: int = 0,
        reasoning_enabled: bool = True,
    ) -> None:
        self.conversation_id = conversation_id
        self.question = question
        self.message_id = message_id or str(uuid.uuid4())
        self.surface_id = f"message:{self.message_id}"
        self.sequence = 0
        self.resume_after = resume_after
        self.reasoning_enabled = reasoning_enabled
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

        event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def receive_analysis_event(event: dict[str, Any]) -> None:
            if event["type"] == "reasoning_delta" and not self.reasoning_enabled:
                return
            loop.call_soon_threadsafe(event_queue.put_nowait, event)

        analysis_task = asyncio.create_task(
            asyncio.to_thread(run_analysis, self.question, receive_analysis_event)
        )
        try:
            while not analysis_task.done() or not event_queue.empty():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.05)
                except TimeoutError:
                    continue
                if event["type"] == "reasoning_delta":
                    yield self._sse(
                        self._update_reasoning_delta(event["delta"], "RUNNING")
                    )
                    continue
                if event["type"] in {"tool_call", "tool_result"}:
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
            analysis = await analysis_task
        except Exception:  # noqa: BLE001 - stream failures must become visible UI state.
            async for event in self._failure_events(
                "无法完成真实数据查询。请检查模型配置或换一种数据问题后重试。"
            ):
                yield event
            return
        if self._is_cancelled():
            yield self._sse(self._update("/status", "CANCELLED"))
            return
        if analysis["visualization"]["status"] == "PENDING":
            chart_client = AntVChartClient()
            chart_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            def receive_chart_event(event: dict[str, Any]) -> None:
                if event["type"] == "reasoning_delta" and not self.reasoning_enabled:
                    return
                chart_queue.put_nowait(event)

            chart_task = asyncio.create_task(
                chart_client.render(self.question, analysis, receive_chart_event)
            )
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
            if self._is_cancelled():
                yield self._sse(self._update("/status", "CANCELLED"))
                return
            analysis["visualization"] = visualization

        markdown = ""
        if analysis["requires_clarification"]:
            markdown = analysis["answer"]
            self.model["content"]["markdown"] = markdown
            yield self._sse(self._update_content(markdown))
        else:
            try:
                async for model_event in OpenAICompatibleModel().stream_answer(
                    self.question, analysis
                ):
                    if self._is_cancelled():
                        yield self._sse(self._update("/status", "CANCELLED"))
                        return
                    if model_event["type"] == "reasoning_delta":
                        if self.reasoning_enabled:
                            yield self._sse(
                                self._update_reasoning_delta(
                                    model_event["delta"], "RUNNING"
                                )
                            )
                        continue
                    chunk = model_event["delta"]
                    markdown += chunk
                    self.model["content"]["markdown"] = markdown
                    yield self._sse(self._update_content_delta(chunk))
                    await asyncio.sleep(0.04)
            except Exception as exc:  # noqa: BLE001 - expose failure instead of a fake fallback.
                async for event in self._failure_events(
                    f"数据查询已完成，但真实模型回答失败（{type(exc).__name__}）。请检查模型服务后重试。"
                ):
                    yield event
                return

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
        events = self.model["events"]
        events.append({"id": str(uuid.uuid4()), "type": "content", "markdown": markdown})
        return self._update("/events", events)

    def _update_content_delta(self, chunk: str) -> dict[str, Any]:
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
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, 'USER', ?, 'COMPLETED', NULL, ?, ?)",
                [str(uuid.uuid4()), self.conversation_id, self.question, now, now],
            )
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, 'ASSISTANT', '', 'STREAMING', NULL, ?, NULL)",
                [self.message_id, self.conversation_id, now],
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
