import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .config import ARTIFACT_DIR
from .mcp_config import McpServer, McpTransport, enabled_servers
from .model_client import OpenAICompatibleModel

logger = logging.getLogger("prism.mcp")

DEFAULT_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5


def retry_attempts() -> int:
    """How many times a failing MCP operation is tried in total."""
    raw = os.getenv("MCP_RETRY_ATTEMPTS")
    if raw is None or not raw.strip():
        return DEFAULT_RETRY_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"MCP_RETRY_ATTEMPTS={raw!r} must be an integer") from None
    if value < 1:
        raise ValueError(f"MCP_RETRY_ATTEMPTS={value} must be at least 1")
    return value


async def with_retry(label: str, operation: Callable[[], Awaitable[Any]]) -> Any:
    """Retry a flaky MCP operation.

    Starting a stdio server shells out to npx, which fails transiently often
    enough that one bad attempt should not disable charts for the whole run.
    Backoff is linear because these are process starts, not rate limits.
    """
    attempts = retry_attempts()
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:  # noqa: BLE001 - the last failure is re-raised below.
            last = exc
            logger.warning(
                "%s failed attempt=%s/%s: %s", label, attempt, attempts, exc
            )
            if attempt < attempts:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)
    assert last is not None
    raise last

ChartEventSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class DiscoveredTool:
    """One tool schema, remembering which server can execute it.

    Deliberately holds no session. Schemas are static, so they are cached and
    reused, which lets the model choose a tool before any server is started.
    """

    server: McpServer
    schema: dict[str, Any]

    @property
    def asset_hosts(self) -> tuple[str, ...]:
        return self.server.asset_hosts


async def _open_session(stack: AsyncExitStack, server: McpServer) -> ClientSession:
    """Connect to one server. stdio is verified; http is not yet exercised."""
    if server.transport is McpTransport.STDIO:
        read, write = await stack.enter_async_context(
            stdio_client(
                StdioServerParameters(
                    command=server.command or "",
                    args=list(server.args),
                    env=dict(server.env) or None,
                )
            )
        )
    else:
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(server.url or "", headers=dict(server.headers) or None)
        )
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


_TOOL_CACHE: tuple[dict[str, DiscoveredTool], dict[str, Any]] | None = None
_CACHE_LOCK = asyncio.Lock()


async def discover_tools(
    servers: list[McpServer],
) -> tuple[dict[str, DiscoveredTool], dict[str, Any]]:
    """Connect to each server and collect its tool schemas, retrying transients."""
    tools: dict[str, DiscoveredTool] = {}
    report: dict[str, Any] = {}
    for server in servers:

        async def list_for(server: McpServer = server) -> Any:
            async with AsyncExitStack() as stack:
                session = await _open_session(stack, server)
                return await session.list_tools()

        try:
            discovered = await with_retry(f"mcp {server.name} tools/list", list_for)
            for tool in discovered.tools:
                if tool.name in tools:
                    raise ValueError(
                        f"Tool {tool.name!r} is exposed by both "
                        f"{tools[tool.name].server.name!r} and {server.name!r}. "
                        "Disable one of them in mcp.json."
                    )
                tools[tool.name] = DiscoveredTool(
                    server=server,
                    schema={
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema,
                        },
                    },
                )
            report[server.name] = {"tool_count": len(discovered.tools)}
        except Exception as exc:  # noqa: BLE001 - one bad server must not hide the rest.
            logger.warning("mcp server %s unavailable: %s", server.name, exc)
            report[server.name] = {"error": f"{type(exc).__name__}: {exc}"}
    return tools, report


async def cached_tools(
    servers: list[McpServer], refresh: bool = False
) -> tuple[dict[str, DiscoveredTool], dict[str, Any], bool]:
    """Tool schemas, discovered once per process.

    Starting a stdio server costs seconds, so the schemas are cached and the
    model picks a tool before anything is spawned. Returns whether the answer
    came from cache.
    """
    global _TOOL_CACHE
    async with _CACHE_LOCK:
        if _TOOL_CACHE is not None and not refresh:
            tools, report = _TOOL_CACHE
            return tools, report, True
        tools, report = await discover_tools(servers)
        # A server that failed is not cached as empty: a transient npx failure
        # would otherwise disable charts for the life of the process.
        if tools:
            logger.info("mcp tools discovered count=%s servers=%s", len(tools), list(report))
            _TOOL_CACHE = (tools, report)
        return tools, report, False


async def prime_tool_cache() -> dict[str, Any]:
    """Warm the cache at startup. Never fatal: charts degrade, the app serves."""
    try:
        _, report, _ = await cached_tools(enabled_servers())
        return report
    except Exception as exc:  # noqa: BLE001 - startup must not depend on npx.
        return {"error": f"{type(exc).__name__}: {exc}"}


class McpVisualizationClient:
    """Picks and runs one charting tool from the configured MCP servers."""

    def __init__(self, servers: list[McpServer] | None = None) -> None:
        self.servers = enabled_servers() if servers is None else servers

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
            "name": "MCP · tools/list", "status": "RUNNING",
            "arguments": {"servers": [server.name for server in self.servers]},
        })
        if not self.servers:
            self._emit(event_sink, {
                "type": "tool_result", "tool_call_id": discovery_id,
                "name": "MCP · tools/list", "status": "COMPLETED",
                "arguments": {}, "result": {"tool_count": 0, "servers": {}},
            })
            return {**visualization, "status": "SKIPPED", "asset_url": None}

        selected_call_id: str | None = None
        selected_label = "MCP · tool call"
        try:
            tools, per_server, from_cache = await cached_tools(self.servers)
            self._emit(event_sink, {
                "type": "tool_result", "tool_call_id": discovery_id,
                "name": "MCP · tools/list", "status": "COMPLETED",
                "arguments": {},
                "result": {
                    "tool_count": len(tools),
                    "servers": per_server,
                    "tools": list(tools),
                    "cached": from_cache,
                },
            })
            if not tools:
                raise ValueError("No MCP server offered any tool")

            # Chosen from cached schemas, so a question that needs no chart
            # never starts a server at all.
            selected = await OpenAICompatibleModel().select_visualization_tool(
                {"question": question, "query_result": analysis["table"]},
                [tool.schema for tool in tools.values()],
                reasoning_sink=(
                    lambda delta: self._emit(
                        event_sink, {"type": "reasoning_delta", "delta": delta}
                    )
                ) if thinking_enabled else None,
                thinking_enabled=thinking_enabled,
            )
            if selected is None:
                return {**visualization, "status": "SKIPPED", "asset_url": None}
            chosen = tools.get(selected.name)
            if chosen is None:
                raise ValueError("Visualization agent selected an undiscovered MCP tool")
            selected_call_id = selected.tool_call_id
            selected_label = f"{chosen.server.name} · {selected.name}"
            logger.info("mcp tool selected server=%s tool=%s", chosen.server.name, selected.name)
            self._emit(event_sink, {
                "type": "tool_call", "tool_call_id": selected.tool_call_id,
                "name": selected_label, "status": "RUNNING",
                "arguments": selected.arguments,
            })
            # Only the server that owns the tool is started.
            async def call_selected() -> Any:
                async with AsyncExitStack() as stack:
                    session = await _open_session(stack, chosen.server)
                    return await session.call_tool(selected.name, selected.arguments)

            result = await with_retry(f"mcp {selected_label}", call_selected)
            remote_url = self._extract_url(result.content)
            image_url = (
                await self._cache_asset(remote_url, chosen.asset_hosts) if remote_url else None
            )
            status = "READY" if image_url else "FAILED"
            self._emit(event_sink, {
                "type": "tool_result", "tool_call_id": selected.tool_call_id,
                "name": selected_label,
                "status": "COMPLETED" if image_url else "FAILED",
                "arguments": selected.arguments,
                "result": {
                    "status": status,
                    "asset_url": image_url,
                    "mcp_content": [getattr(item, "text", None) for item in result.content],
                },
            })
            return {
                **visualization,
                "status": status,
                "server_name": chosen.server.name,
                "tool_name": selected.name,
                "title": str(selected.arguments.get("title") or selected.name),
                "asset_url": image_url,
            }
        except Exception as exc:  # noqa: BLE001 - MCP failure must not fail analysis.
            self._emit(event_sink, {
                "type": "tool_result",
                "tool_call_id": selected_call_id or discovery_id,
                "name": selected_label if selected_call_id else "MCP · tools/list",
                "status": "FAILED",
                "result": {"error": f"{type(exc).__name__}: {exc}"},
            })
            return {
                **visualization,
                "status": "FAILED",
                "asset_url": None,
                "error": f"MCP unavailable: {type(exc).__name__}",
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
    async def _cache_asset(url: str, allowed_hosts: tuple[str, ...]) -> str | None:
        """Download a tool's image, but only from hosts that server declared."""
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or (parsed.host or "").lower() not in allowed_hosts:
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
