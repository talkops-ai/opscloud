"""Data structures for MCP server and tool metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MCPServerStatus = Literal["ok", "unauthenticated", "error", "disabled", "disconnected"]


@dataclass(frozen=True)
class MCPToolInfo:
    """Lightweight metadata about a single tool exposed by an MCP server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert MCPToolInfo to a JSON-serializable dictionary.

        Returns:
            dict[str, Any]: Dictionary containing tool name, description, and input schema.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema or {},
        }


@dataclass(frozen=True)
class MCPServerInfo:
    """Metadata about a configured MCP server, including probed tools."""

    name: str
    transport: str = "stdio"
    status: MCPServerStatus = "ok"
    error: str | None = None
    tools: tuple[MCPToolInfo, ...] = ()
    enabled: bool = True
    source: str = "project"
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    disabled_tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()

    @property
    def is_disabled(self) -> bool:
        """Check whether the server is disabled.

        Returns:
            bool: True if server is disabled.
        """
        return not self.enabled

    @property
    def tool_count(self) -> int:
        """Count of tools exposed by this server.

        Returns:
            int: Number of exposed tools.
        """
        return len(self.tools)

    @property
    def display_target(self) -> str:
        """Human-readable target URL or command line.

        Returns:
            str: URL for network servers or joined command string for stdio.
        """
        if self.transport in ("http", "sse", "streamable_http"):
            return self.url or ""
        if self.command:
            parts = [self.command, *self.args]
            return " ".join(parts)
        return ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize server metadata to dictionary format.

        Returns:
            dict[str, Any]: JSON-compatible dictionary of server properties.
        """
        res: dict[str, Any] = {
            "name": self.name,
            "transport": self.transport,
            "status": self.status,
            "error": self.error,
            "url": self.url,
            "command": self.command,
            "args": list(self.args),
            "tool_count": self.tool_count,
            "tools": [t.to_dict() for t in self.tools],
            "enabled": self.enabled,
        }
        return res

    def needs_attention(self) -> bool:
        """Return whether this server requires user action."""
        return self.status in {"unauthenticated", "error", "disconnected"}
