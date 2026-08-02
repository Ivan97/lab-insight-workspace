import os
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class AntVChartClient:
    def __init__(self) -> None:
        self.url = os.getenv("MCP_CHART_URL", "http://127.0.0.1:1122/mcp")

    async def render(self, visualization: dict[str, Any]) -> dict[str, Any]:
        tool_name = visualization["tool_name"]
        arguments = {
            "data": visualization["data"],
            "xField": visualization["x_field"],
            "yField": visualization["y_field"],
            "title": visualization["title"],
            "width": 1200,
            "height": 640,
        }
        try:
            async with (
                httpx.AsyncClient(trust_env=False) as http_client,
                streamable_http_client(self.url, http_client=http_client) as (
                    read_stream,
                    write_stream,
                    _,
                ),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
            image_url = self._extract_url(result.content)
            return {
                **visualization,
                "status": "READY" if image_url else "SKIPPED",
                "asset_url": image_url,
            }
        except Exception as exc:  # noqa: BLE001 - MCP failure must not fail analysis.
            return {
                **visualization,
                "status": "SKIPPED",
                "asset_url": None,
                "error": f"AntV MCP unavailable: {type(exc).__name__}",
            }

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
