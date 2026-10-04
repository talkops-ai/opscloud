"""MCPContextMiddleware — inject MCP server inventory into the system prompt.

When included in a subagent's middleware list, it appends an ``**MCP Servers**``
block to the system prompt before every model call, informing the LLM which
MCP servers are available, their status, and what tools they expose.

Uses preloaded ``MCPServerInfo`` metadata — no live session connections needed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)

from opscloud.middleware.registry import register_middleware
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_TOOL_NAME_DISPLAY_LIMIT = 10
_MCP_ERROR_DETAIL_LIMIT = 200


def _sanitize_error_detail(error: str | None) -> str:
    """Truncate and sanitize error text for prompt injection."""
    if not error:
        return "unknown error"
    sanitized = error[:_MCP_ERROR_DETAIL_LIMIT]
    sanitized = "".join(c for c in sanitized if c.isprintable() or c in ("\n", "\t"))
    return sanitized or "unknown error"


def _build_mcp_context_from_infos(
    servers: Sequence[Any],
) -> str:
    """Format MCP server/tool inventory for the system prompt."""
    if not servers:
        return ""

    total_tools = sum(len(getattr(s, "tools", ())) for s in servers)
    lines = [f"**MCP Servers** ({len(servers)} servers, {total_tools} tools):"]

    for server in servers:
        name = getattr(server, "name", str(server))
        transport = getattr(server, "transport", "stdio")
        tools = getattr(server, "tools", ())
        status = getattr(server, "status", "ok")
        error = getattr(server, "error", None)

        if not tools:
            if status == "error":
                detail = _sanitize_error_detail(error)
                lines.append(
                    f"- **{name}** ({transport}): "
                    f"UNAVAILABLE — <error>{detail}</error>. "
                    "Treat this integration as unavailable; only mention this if the "
                    "user's task specifically requires operations with this server."
                )
            elif status == "disabled":
                lines.append(f"- **{name}** ({transport}): (disabled by user)")
            else:
                lines.append(f"- **{name}** ({transport}): (no tools registered)")
            continue

        tool_names = [getattr(t, "name", str(t)) for t in tools]
        if len(tool_names) > _TOOL_NAME_DISPLAY_LIMIT:
            shown = ", ".join(tool_names[:_TOOL_NAME_DISPLAY_LIMIT])
            remaining = len(tool_names) - _TOOL_NAME_DISPLAY_LIMIT
            lines.append(f"- **{name}** ({transport}): {shown}, and {remaining} more")
        else:
            lines.append(f"- **{name}** ({transport}): {', '.join(tool_names)}")

    return "\n".join(lines)


def _build_mcp_context_from_config(
    mcp_config: dict[str, Any],
) -> str:
    """Fallback: format MCP server inventory from raw config dict."""
    servers = mcp_config.get("mcpServers") or mcp_config
    if not isinstance(servers, dict) or not servers:
        return ""

    lines = ["**MCP Servers**:"]
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        transport = cfg.get("type") or cfg.get("transport") or "stdio"
        lines.append(f"- **{name}** ({transport}): configured")

    return "\n".join(lines) if len(lines) > 1 else ""


@register_middleware(name="mcp_context")
class MCPContextMiddleware(AgentMiddleware):
    """Inject MCP server inventory into the system prompt.

    Accepts either preloaded ``MCPServerInfo`` metadata (preferred) or a raw
    config dict (fallback). Does NOT require live session connections.
    """

    def __init__(
        self,
        mcp_server_info: Sequence[Any] | None = None,
        mcp_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize MCPContextMiddleware with server info or raw config."""
        super().__init__()
        self._server_info = list(mcp_server_info) if mcp_server_info else []
        self._config = mcp_config or {}

    def _build_mcp_context(self) -> str:
        """Build MCP context string — prefers MCPServerInfo, falls back to config."""
        if self._server_info:
            return _build_mcp_context_from_infos(self._server_info)
        if self._config:
            return _build_mcp_context_from_config(self._config)
        return ""

    def _get_modified_request(self, request: ModelRequest) -> ModelRequest:
        mcp_context = self._build_mcp_context()
        if not mcp_context:
            return request
        new_system_msg = append_to_system_message(request.system_message, mcp_context)
        return request.override(system_message=new_system_msg)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Wrap synchronous model call to inject MCP server inventory into system message."""
        modified_request = self._get_modified_request(request)
        return handler(modified_request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Wrap asynchronous model call to inject MCP server inventory into system message."""
        modified_request = self._get_modified_request(request)
        return await handler(modified_request)
