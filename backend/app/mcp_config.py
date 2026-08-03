"""MCP servers declared in a config file rather than in code.

The file uses the same `mcpServers` shape as Claude Desktop, Cursor and the
VS Code MCP extension, so an entry can be pasted between them. Adding a second
tool server is then an edit to mcp.json instead of a change to the
visualization client.

The file is committed, so secrets must not be written into it. Values in `env`,
`args`, `url` and `headers` may reference environment variables as ${VAR}, which
are substituted at load time.
"""

import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import ROOT_DIR

DEFAULT_CONFIG_PATH = ROOT_DIR / "mcp.json"
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class McpConfigError(ValueError):
    """Raised when the MCP config file cannot be trusted.

    Malformed configuration fails loudly instead of silently disabling a tool
    server, which would look like a model that simply chose not to draw a chart.
    """


@dataclass(frozen=True)
class McpServer:
    name: str
    transport: McpTransport
    enabled: bool = True
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # Hosts this server may hand back images from. Empty means the server can
    # only return inline content, never a URL we will fetch.
    asset_hosts: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        """Health-endpoint view. Never includes header values, which may hold tokens."""
        if self.transport is McpTransport.STDIO:
            detail: dict[str, Any] = {"command": [self.command, *self.args]}
        else:
            detail = {"url": self.url}
        return {
            "name": self.name,
            "transport": self.transport.value,
            "enabled": self.enabled,
            "asset_hosts": list(self.asset_hosts),
            **detail,
        }


def config_path() -> Path:
    override = os.getenv("MCP_CONFIG_PATH")
    return Path(override) if override else DEFAULT_CONFIG_PATH


def _expand(value: str) -> str:
    return _PLACEHOLDER.sub(lambda match: os.getenv(match.group(1), ""), value)


def _string_map(raw: Any, name: str, key: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or any(not isinstance(v, str) for v in raw.values()):
        raise McpConfigError(f"MCP server {name!r}: {key} must be a map of strings")
    return {str(k): _expand(v) for k, v in raw.items()}


def _asset_hosts(raw: Any, name: str) -> tuple[str, ...]:
    """Hosts whose images may be downloaded and cached.

    This is an SSRF boundary, so it is declared per server rather than inferred
    from whatever URL a tool happens to return.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise McpConfigError(f"MCP server {name!r}: assetHosts must be a list of hostnames")
    return tuple(_expand(item).strip().lower() for item in raw if item.strip())


def _parse_server(name: str, raw: Any) -> McpServer:
    if not isinstance(raw, dict):
        raise McpConfigError(f"MCP server {name!r} must be an object")

    declared = raw.get("transport")
    if declared is None:
        # Infer from the shape so a pasted stdio entry works unchanged.
        declared = McpTransport.HTTP.value if raw.get("url") else McpTransport.STDIO.value
    try:
        transport = McpTransport(str(declared).strip().lower())
    except ValueError:
        allowed = ", ".join(member.value for member in McpTransport)
        raise McpConfigError(
            f"MCP server {name!r}: transport={declared!r} is not supported. Use one of: {allowed}"
        ) from None

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise McpConfigError(f"MCP server {name!r}: enabled must be true or false")

    if transport is McpTransport.STDIO:
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise McpConfigError(f"MCP server {name!r}: stdio transport requires a command")
        args = raw.get("args", [])
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise McpConfigError(f"MCP server {name!r}: args must be a list of strings")
        return McpServer(
            name=name,
            transport=transport,
            enabled=enabled,
            command=_expand(command),
            args=[_expand(item) for item in args],
            env=_string_map(raw.get("env"), name, "env"),
            asset_hosts=_asset_hosts(raw.get("assetHosts"), name),
        )

    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise McpConfigError(f"MCP server {name!r}: http transport requires a url")
    return McpServer(
        name=name,
        transport=transport,
        enabled=enabled,
        url=_expand(url),
        headers=_string_map(raw.get("headers"), name, "headers"),
        asset_hosts=_asset_hosts(raw.get("assetHosts"), name),
    )


def load_servers(path: Path | None = None) -> list[McpServer]:
    """All servers declared in the config file, enabled or not."""
    target = path or config_path()
    if not target.is_file():
        # No file means no tool servers. The health endpoint reports the path so
        # this stays visible rather than looking like a model that declined.
        return []
    try:
        document = json.loads(target.read_text())
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise McpConfigError(f"{target} must contain a JSON object")
    servers = document.get("mcpServers")
    if servers is None:
        raise McpConfigError(f"{target} must contain an 'mcpServers' object")
    if not isinstance(servers, dict):
        raise McpConfigError(f"{target}: 'mcpServers' must be an object keyed by server name")
    return [_parse_server(name, raw) for name, raw in servers.items()]


def enabled_servers(path: Path | None = None) -> list[McpServer]:
    return [server for server in load_servers(path) if server.enabled]
