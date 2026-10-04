"""Headless MCP guard middleware — rejects mutating MCP calls when no approval UI exists."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import AgentState, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command

from opscloud.middleware.registry import register_middleware
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def mcp_tool_is_coherently_read_only(tool: object) -> bool:
    """Return whether an MCP tool has coherent read-only annotations."""
    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, Mapping):
        return False
    hint_names = (
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
    )
    if any(
        name in metadata and metadata[name] is not None and not isinstance(metadata[name], bool) for name in hint_names
    ):
        return False
    return metadata.get("readOnlyHint") is True and metadata.get("destructiveHint") is not True


def gated_mcp_tool_names(mcp_tools: Sequence[BaseTool]) -> set[str]:
    """Return MCP tool names that require manual approval (not coherently read-only)."""
    return {tool.name for tool in mcp_tools if not mcp_tool_is_coherently_read_only(tool)}


@register_middleware(name="headless_mcp_guard")
class HeadlessMCPGuardMiddleware(HumanInTheLoopMiddleware[AgentState[Any], Any, Any]):
    """Reject dynamically gated MCP calls when running headlessly without an approval UI."""

    def __init__(self, tool_names: Sequence[str] | set[str] | None = None) -> None:
        """Initialize HeadlessMCPGuardMiddleware.

        Args:
            tool_names: Collection of mutating tool names that require human approval.
        """
        super().__init__({})
        self._tool_names = frozenset(tool_names or ())

    def _rejection(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = request.tool_call.get("name", "")
        if tool_name not in self._tool_names:
            return None

        tool_id = request.tool_call.get("id", "")
        logger.warning("Headless MCP guard blocked mutating tool call: %r", tool_name)
        return ToolMessage(
            content=(
                "This MCP action requires approval, but the current headless runtime "
                "has no approval UI. Provide human approval or choose a read-only MCP action."
            ),
            name=tool_name,
            tool_call_id=tool_id,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Wrap synchronous tool call and block guarded tools if running headlessly."""
        return self._rejection(request) or handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Wrap asynchronous tool call and block guarded tools if running headlessly."""
        rejection = self._rejection(request)
        return rejection if rejection is not None else await handler(request)
