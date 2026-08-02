import hashlib
from collections.abc import Callable
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .config import ARTIFACT_DIR
from .model_client import OpenAICompatibleModel

ChartEventSink = Callable[[dict[str, Any]], None]


class AntVChartClient:
    def __init__(self) -> None:
        self.server = StdioServerParameters(
            command="npx",
            args=["-y", "@antv/mcp-server-chart"],
        )

    async def render(
        self,
        question: str,
        analysis: dict[str, Any],
        event_sink: ChartEventSink | None = None,
        thinking_enabled: bool = True,
    ) -> dict[str, Any]:
        visualization = analysis["visualization"]
        discovery_id = "mcp:tools/list"
        self._emit(event_sink, {
            "type": "tool_call", "tool_call_id": discovery_id,
            "name": "AntV MCP · tools/list", "status": "RUNNING",
            "arguments": {},
        })
        selected_call_id: str | None = None
        try:
            async with (
                stdio_client(self.server) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                discovered = await session.list_tools()
                tools_by_name = {tool.name: tool for tool in discovered.tools}
                self._emit(event_sink, {
                    "type": "tool_result", "tool_call_id": discovery_id,
                    "name": "AntV MCP · tools/list", "status": "COMPLETED",
                    "arguments": {},
                    "result": {
                        "tool_count": len(discovered.tools),
                        "tools": list(tools_by_name),
                    },
                })
                model_tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema,
                        },
                    }
                    for tool in discovered.tools
                ]
                selected = await OpenAICompatibleModel().select_visualization_tool(
                    {
                        "question": question,
                        "query_result": analysis["table"],
                    },
                    model_tools,
                    reasoning_sink=(
                        lambda delta: self._emit(
                            event_sink, {"type": "reasoning_delta", "delta": delta}
                        )
                    ) if thinking_enabled else None,
                    thinking_enabled=thinking_enabled,
                )
                if selected is None:
                    return {**visualization, "status": "SKIPPED", "asset_url": None}
                if selected.name not in tools_by_name:
                    raise ValueError("Visualization agent selected an undiscovered MCP tool")
                selected_call_id = selected.tool_call_id
                self._emit(event_sink, {
                    "type": "tool_call", "tool_call_id": selected.tool_call_id,
                    "name": f"AntV MCP · {selected.name}", "status": "RUNNING",
                    "arguments": selected.arguments,
                })
                result = await session.call_tool(selected.name, selected.arguments)
            remote_url = self._extract_url(result.content)
            image_url = await self._cache_asset(remote_url) if remote_url else None
            status = "READY" if image_url else "FAILED"
            result_payload = {
                "status": status,
                "asset_url": image_url,
                "mcp_content": [getattr(item, "text", None) for item in result.content],
            }
            self._emit(event_sink, {
                "type": "tool_result", "tool_call_id": selected.tool_call_id,
                "name": f"AntV MCP · {selected.name}",
                "status": "COMPLETED" if image_url else "FAILED",
                "arguments": selected.arguments, "result": result_payload,
            })
            return {
                **visualization,
                "status": status,
                "tool_name": selected.name,
                "title": str(selected.arguments.get("title") or selected.name),
                "rationale": "",
                "asset_url": image_url,
            }
        except Exception as exc:  # noqa: BLE001 - MCP failure must not fail analysis.
            failed_id = selected_call_id or discovery_id
            failed_name = (
                "AntV MCP · tool call" if selected_call_id else "AntV MCP · tools/list"
            )
            self._emit(event_sink, {
                "type": "tool_result", "tool_call_id": failed_id,
                "name": failed_name, "status": "FAILED",
                "result": {"error": f"{type(exc).__name__}: {exc}"},
            })
            return {
                **visualization,
                "status": "FAILED",
                "asset_url": None,
                "error": f"AntV MCP unavailable: {type(exc).__name__}",
            }

    @staticmethod
    def _emit(event_sink: ChartEventSink | None, event: dict[str, Any]) -> None:
        if event_sink:
            event_sink(event)

    @staticmethod
    def _extract_url(content: list[Any]) -> str | None:
        for item in content:
            text = getattr(item, "text", None)
            if not text:
                continue
            for token in text.replace('"', " ").split():
                if token.startswith(("https://", "http://")):
                    return token.rstrip(".,}")
        return None

    @staticmethod
    async def _cache_asset(url: str) -> str | None:
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or parsed.host != "mdn.alipayobjects.com":
            return None
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, trust_env=False) as client:
            response = await client.get(url)
            response.raise_for_status()
        content = response.content
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif content.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            suffix = None
        if not suffix or len(content) > 5_000_000:
            return None
        asset_id = hashlib.sha256(content).hexdigest()[:24]
        target = ARTIFACT_DIR / f"{asset_id}{suffix}"
        target.write_bytes(content)
        return f"/api/v1/assets/{target.name}"
