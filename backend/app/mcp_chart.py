import hashlib
import os
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .config import ARTIFACT_DIR


class AntVChartClient:
    def __init__(self) -> None:
        self.url = os.getenv("MCP_CHART_URL", "http://localhost:1122/mcp")

    @staticmethod
    def arguments_for(visualization: dict[str, Any]) -> dict[str, Any]:
        tool_name = visualization["tool_name"]
        category_field = "time" if tool_name == "generate_line_chart" else "category"
        data = [
            {
                category_field: str(row[visualization["x_field"]]),
                "value": float(row[visualization["y_field"]]),
            }
            for row in visualization["data"]
            if row.get(visualization["x_field"]) is not None
            and isinstance(row.get(visualization["y_field"]), (int, float))
        ]
        arguments = {
            "data": data,
            "title": visualization["title"],
            "axisXTitle": visualization["x_field"],
            "axisYTitle": visualization["y_field"],
            "width": 1200,
            "height": 640,
        }
        if tool_name in {"generate_column_chart", "generate_bar_chart"}:
            arguments.update({"group": False, "stack": False})
        return arguments

    async def render(self, visualization: dict[str, Any]) -> dict[str, Any]:
        tool_name = visualization["tool_name"]
        arguments = self.arguments_for(visualization)
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
            remote_url = self._extract_url(result.content)
            image_url = await self._cache_asset(remote_url) if remote_url else None
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
