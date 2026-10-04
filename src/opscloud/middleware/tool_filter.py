"""Tool filtering proxy middleware for restricting subagent tool access."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import fnmatch
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from opscloud.middleware.registry import register_middleware
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


TOOL_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "read": ("read_file", "view_file", "read_url_content"),
    "read_file": ("read_file", "view_file", "read_url_content"),
    "write": ("write_to_file", "write_file"),
    "write_file": ("write_to_file", "write_file"),
    "edit": ("replace_file_content", "multi_replace_file_content", "edit_file"),
    "edit_file": ("replace_file_content", "multi_replace_file_content", "edit_file"),
    "grep": ("grep_search", "grep"),
    "grep_search": ("grep_search", "grep"),
    "search": ("grep_search", "grep", "file_search"),
    "glob": ("glob", "file_search", "dir_list"),
    "ls": ("ls", "list_dir", "dir_list"),
    "list_dir": ("ls", "list_dir", "dir_list"),
    "bash": ("run_command", "execute"),
    "execute": ("run_command", "execute"),
}


def _expand_tool_patterns(patterns: Sequence[str]) -> tuple[str, ...]:
    expanded: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        if pattern not in seen:
            seen.add(pattern)
            expanded.append(pattern)
        clean = pattern.strip().lower()
        if clean in TOOL_ALIAS_MAP:
            for alias in TOOL_ALIAS_MAP[clean]:
                if alias not in seen:
                    seen.add(alias)
                    expanded.append(alias)
    return tuple(expanded)


_GLOB_METACHARS = frozenset("*?[")


def _entry_matches_tool(pattern: str, tool_name: str, base_name: str, server_name: str | None = None) -> bool:
    """Check if an allowed pattern matches a tool call under any valid naming projection."""
    if server_name:
        if pattern in {f"mcp__{server_name}__*", f"{server_name}:*", f"{server_name}_*", server_name}:
            return True
        if fnmatch.fnmatch(server_name, pattern) or fnmatch.fnmatch(server_name.lower(), pattern.lower()):
            return True

    candidates: list[str] = [tool_name, base_name]
    if server_name:
        candidates.append(f"mcp__{server_name}__{base_name}")
        candidates.append(f"{server_name}:{base_name}")
        candidates.append(f"{server_name}_{base_name}")

    is_glob = any(ch in _GLOB_METACHARS for ch in pattern)
    for cand in candidates:
        if is_glob:
            if fnmatch.fnmatch(cand, pattern) or fnmatch.fnmatch(cand.lower(), pattern.lower()):
                return True
        else:
            if cand == pattern or cand.lower() == pattern.lower():
                return True

    return False


@register_middleware(name="tool_filter")
class ToolFilterMiddleware(AgentMiddleware[Any, Any]):
    """Filters tool calls against an allowlist of allowed tool patterns (fnmatch format)."""

    def __init__(
        self,
        allowed_patterns: Sequence[str | dict[str, Any]] | None = None,
        capabilities: Sequence[dict[str, Any] | str] | None = None,
    ) -> None:
        super().__init__()
        patterns: list[str] = []

        if allowed_patterns is not None:
            for item in allowed_patterns:
                if isinstance(item, dict):
                    server = item.get("mcp_server")
                    if server and item.get("allow_all"):
                        patterns.append(f"mcp__{server}__*")
                        patterns.append(f"{server}:*")
                    if "tools" in item and isinstance(item["tools"], (list, tuple)):
                        patterns.extend(str(t) for t in item["tools"])
                elif isinstance(item, str):
                    patterns.append(item)

        if capabilities:
            for cap in capabilities:
                if isinstance(cap, dict):
                    server = cap.get("mcp_server")
                    if server and cap.get("allow_all"):
                        patterns.append(f"mcp__{server}__*")
                        patterns.append(f"{server}:*")
                    if "tools" in cap and isinstance(cap["tools"], (list, tuple)):
                        patterns.extend(str(t) for t in cap["tools"])
                elif isinstance(cap, str):
                    patterns.append(cap)

        self._allowed_patterns: tuple[str, ...] = _expand_tool_patterns(patterns) if patterns else ()

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if a tool name matches any allowed patterns."""
        if not self._allowed_patterns:
            return True

        server_name: str | None = None
        if tool_name.startswith("mcp__"):
            parts = tool_name[5:].split("__", 1)
            server_name = parts[0]
            base_name = parts[1] if len(parts) > 1 else tool_name
        elif ":" in tool_name:
            server_name, base_name = tool_name.split(":", 1)
        else:
            base_name = tool_name

        for pattern in self._allowed_patterns:
            if _entry_matches_tool(pattern, tool_name, base_name, server_name):
                return True
        return False

    def _validate_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = (
            request.tool_call.get("name", "")
            if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict)
            else ""
        )
        if self.is_tool_allowed(tool_name):
            return None

        allowed_str = ", ".join(self._allowed_patterns)

        logger.warning(
            "Tool call %r blocked for subagent (not in allowed list: %s)",
            tool_name,
            allowed_str,
        )
        return ToolMessage(
            content=(
                f"Tool call rejected: tool `{tool_name}` is restricted for this subagent. "
                f"Allowed tool patterns: [{allowed_str}]."
            ),
            name=tool_name,
            tool_call_id=(
                request.tool_call.get("id", "")
                if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict)
                else ""
            ),
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        err = self._validate_tool_call(request)
        if err is not None:
            return err
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        err = self._validate_tool_call(request)
        if err is not None:
            return err
        return await handler(request)


__all__ = [
    "TOOL_ALIAS_MAP",
    "ToolFilterMiddleware",
]
