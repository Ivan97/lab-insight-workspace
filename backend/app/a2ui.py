import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from .analysis import run_analysis
from .config import CATALOG_ID
from .database import connection, json_dumps, utcnow
from .mcp_chart import AntVChartClient

COMPONENTS = [
    {"id": "root", "component": "Column", "children": ["reasoning", "tools", "answer", "analysis"]},
    {
        "id": "reasoning",
        "component": "ReasoningPanel",
        "segments": {"path": "/reasoning/segments"},
        "status": {"path": "/reasoning/status"},
    },
    {"id": "tools", "component": "ToolCallGroup", "groups": {"path": "/toolGroups"}},
    {"id": "answer", "component": "RichMarkdown", "markdown": {"path": "/content/markdown"}},
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
    ) -> None:
        self.conversation_id = conversation_id
        self.question = question
        self.message_id = message_id or str(uuid.uuid4())
        self.surface_id = f"message:{self.message_id}"
        self.sequence = 0
        self.resume_after = resume_after
        self.model: dict[str, Any] = {
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

        yield self._sse(self._update_reasoning("正在理解问题并选择可信的数据范围。", "RUNNING"))
        await asyncio.sleep(0.12)
        yield self._sse(self._update_tool("query", "Query DuckDB", "RUNNING", 1))
        yield self._sse(
            self._update_reasoning("已锁定统一分析视图，正在执行只读聚合查询。", "RUNNING")
        )
        analysis = await asyncio.to_thread(run_analysis, self.question)
        yield self._sse(
            self._update_tool(
                "query", "Query DuckDB", "COMPLETED", 1, "Returned trusted aggregate rows"
            )
        )

        chart_tool = analysis["visualization"]["tool_name"]
        yield self._sse(self._update_tool("chart", f"AntV · {chart_tool}", "RUNNING", 2))
        yield self._sse(
            self._update_reasoning(
                "查询结果已通过检查，正在由 Visualization Agent 选择并调用图表工具。", "RUNNING"
            )
        )
        visualization = await AntVChartClient().render(analysis["visualization"])
        analysis["visualization"] = visualization
        yield self._sse(
            self._update_tool(
                "chart",
                f"AntV · {chart_tool}",
                "COMPLETED" if visualization["status"] == "READY" else "FAILED",
                2,
                visualization.get("error") or "Chart artifact generated",
            )
        )

        yield self._sse(
            self._update_reasoning("正在把事实、口径和限制整理成可审阅的结论。", "RUNNING")
        )
        words = analysis["answer"].split()
        for end in range(4, len(words) + 4, 4):
            markdown = " ".join(words[:end])
            self.model["content"]["markdown"] = markdown
            yield self._sse(self._update("/content/markdown", markdown))
            await asyncio.sleep(0.04)

        self.model["analysis"] = analysis
        yield self._sse(self._update("/analysis", analysis))
        self.model["reasoning"]["status"] = "COMPLETED"
        yield self._sse(self._update("/reasoning/status", "COMPLETED"))
        self.model["status"] = "COMPLETED"
        yield self._sse(self._update("/status", "COMPLETED"))
        self._complete_message()

    def _update_reasoning(self, text: str, status: str) -> dict[str, Any]:
        self.model["reasoning"]["segments"].append(
            {"id": str(uuid.uuid4()), "text": text, "createdAt": datetime.now(UTC).isoformat()}
        )
        self.model["reasoning"]["status"] = status
        return self._update("/reasoning", self.model["reasoning"])

    def _update_tool(
        self, key: str, name: str, status: str, sequence: int, summary: str | None = None
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
        }
        if existing:
            existing.update(payload)
        else:
            group["calls"].append(payload)
        return self._update("/toolGroups/analysis", group)

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
