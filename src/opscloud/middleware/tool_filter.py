"""Tool filtering proxy middleware for restricting subagent tool access."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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

ALWAYS_ALLOWED_SYSTEM_TOOLS: frozenset[str] = frozenset({
    "compact_conversation",
    "ask_user",
})


def _mcp_server_from_tool(tool: object | None) -> str | None:
    """Extract MCP server name from tool metadata."""
    if tool is None:
        return None
    metadata = getattr(tool, "metadata", None)
    if isinstance(metadata, Mapping):
        for key in ("_mcp_server", "mcp_server", "mcp_server_name", "server_name"):
            val = metadata.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _mcp_original_name_from_tool(tool: object | None) -> str | None:
    """Extract bare original MCP tool name from tool metadata."""
    if tool is None:
        return None
    metadata = getattr(tool, "metadata", None)
    if isinstance(metadata, Mapping):
        for key in ("_mcp_original_name", "mcp_original_name", "original_name"):
            val = metadata.get(key)
            if isinstance(val, str) and val:
                return val
    orig = getattr(tool, "original_name", None)
    return str(orig) if orig else None


def _parse_mcp_pattern(
    pattern: str,
    known_servers: set[str],
    subagent_name: str | None = None,
) -> tuple[str | None, str | None]:
    """Parse (server_name, tool_pattern) from an MCP tool pattern.

    Supports:
    - mcp__plugin_<subagent>_<server>__<tool>
    - mcp__plugin__<subagent>__<server>__<tool>
    - mcp__<server>__<tool>
    - <server>:<tool>
    - <server>_*
    """
    if pattern.startswith("mcp__"):
        content = pattern[5:]
        parts = content.split("__")
        if len(parts) == 2:
            server_part, tool_pat = parts[0], parts[1]
            if server_part.startswith("plugin_"):
                rest = server_part[7:]
                if subagent_name and rest.startswith(f"{subagent_name}_"):
                    return rest[len(subagent_name) + 1 :], tool_pat
                for srv in sorted(known_servers, key=len, reverse=True):
                    if rest.endswith(f"_{srv}"):
                        return srv, tool_pat
                if "_" in rest:
                    return rest.rsplit("_", 1)[1], tool_pat
                return rest, tool_pat
            return server_part, tool_pat
        elif len(parts) >= 3:
            tool_pat = parts[-1]
            if parts[0] == "plugin":
                server = parts[-2]
                return server, tool_pat
            return parts[0], tool_pat
    elif ":" in pattern:
        srv, tool_pat = pattern.split(":", 1)
        return srv, tool_pat
    elif pattern.endswith("_*"):
        srv = pattern[:-2]
        if srv in known_servers:
            return srv, "*"
    return None, None


def _expand_tool_patterns(
    patterns: Sequence[str],
    known_servers: set[str] | None = None,
    subagent_name: str | None = None,
) -> tuple[str, ...]:
    """Expand tool pattern list with aliases, wire projections, and provider-safe variations."""
    expanded: list[str] = []
    seen: set[str] = set()

    def _add(p: str | None) -> None:
        if p and p not in seen:
            seen.add(p)
            expanded.append(p)

    known = set(known_servers or ())

    for pattern in patterns:
        _add(pattern)
        clean = pattern.strip().lower()
        if clean in TOOL_ALIAS_MAP:
            for alias in TOOL_ALIAS_MAP[clean]:
                _add(alias)

        server, tool_pat = _parse_mcp_pattern(pattern, known, subagent_name)
        if server:
            known.add(server)
            _add(f"mcp__{server}__{tool_pat}")
            _add(f"{server}:{tool_pat}")
            _add(f"{server}_{tool_pat}")
            if subagent_name:
                _add(f"mcp__plugin_{subagent_name}_{server}__{tool_pat}")
                _add(f"mcp__plugin__{subagent_name}__{server}__{tool_pat}")
            if tool_pat == "*":
                _add(server)
                _add(f"{server}_*")
                _add(f"{server}:*")
                _add(f"mcp__{server}__*")
            else:
                _add(tool_pat)

    return tuple(expanded)


_GLOB_METACHARS = frozenset("*?[")


def _entry_matches_tool(
    pattern: str,
    cand: str,
    base_name: str,
    server_name: str | None = None,
) -> bool:
    """Check if an allowed pattern matches a tool call under any valid naming projection."""
    if server_name:
        if pattern in {
            f"mcp__{server_name}__*",
            f"{server_name}:*",
            f"{server_name}_*",
            server_name,
        }:
            return True
        if fnmatch.fnmatch(server_name, pattern) or fnmatch.fnmatch(server_name.lower(), pattern.lower()):
            return True

    is_glob = any(ch in _GLOB_METACHARS for ch in pattern)
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
        subagent_name: str | None = None,
        mcp_tools: Sequence[Any] | None = None,
        known_mcp_servers: Sequence[str] | set[str] | None = None,
    ) -> None:
        super().__init__()
        self._subagent_name = subagent_name
        self._known_mcp_servers: set[str] = set(known_mcp_servers or ())
        self._tool_to_server: dict[str, str] = {}
        self._tool_to_orig_name: dict[str, str] = {}

        if mcp_tools:
            for tool in mcp_tools:
                name = getattr(tool, "name", None)
                if isinstance(name, str) and name:
                    srv = _mcp_server_from_tool(tool)
                    if srv:
                        self._tool_to_server[name] = srv
                        self._known_mcp_servers.add(srv)
                    orig = _mcp_original_name_from_tool(tool)
                    if orig:
                        self._tool_to_orig_name[name] = orig

        patterns: list[str] = []

        if allowed_patterns is not None:
            for item in allowed_patterns:
                if isinstance(item, dict):
                    server = item.get("mcp_server")
                    if server and item.get("allow_all"):
                        patterns.append(f"mcp__{server}__*")
                        patterns.append(f"{server}:*")
                        patterns.append(f"{server}_*")
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
                        patterns.append(f"{server}_*")
                    if "tools" in cap and isinstance(cap["tools"], (list, tuple)):
                        patterns.extend(str(t) for t in cap["tools"])
                elif isinstance(cap, str):
                    patterns.append(cap)

        self._allowed_patterns: tuple[str, ...] = (
            _expand_tool_patterns(patterns, self._known_mcp_servers, self._subagent_name)
            if patterns
            else ()
        )

    def is_tool_allowed(self, tool_name: str, tool_obj: Any = None) -> bool:
        """Check if a tool name matches any allowed patterns."""
        if tool_name in ALWAYS_ALLOWED_SYSTEM_TOOLS:
            return True
        if not self._allowed_patterns:
            return True

        server_name: str | None = None
        base_name: str | None = None

        if tool_obj is not None:
            server_name = _mcp_server_from_tool(tool_obj)
            base_name = _mcp_original_name_from_tool(tool_obj)
            if server_name:
                self._known_mcp_servers.add(server_name)
                self._tool_to_server[tool_name] = server_name

        if not server_name and tool_name in self._tool_to_server:
            server_name = self._tool_to_server[tool_name]
            base_name = self._tool_to_orig_name.get(tool_name)

        if not server_name:
            if tool_name.startswith("mcp__"):
                parts = tool_name[5:].split("__")
                if len(parts) == 2:
                    server_name, base_name = parts[0], parts[1]
                elif len(parts) >= 3:
                    base_name = parts[-1]
                    server_part = parts[-2]
                    for srv in sorted(self._known_mcp_servers, key=len, reverse=True):
                        if parts[1].endswith(f"_{srv}") or parts[1].endswith(f"__{srv}") or parts[1] == srv:
                            server_name = srv
                            break
                    if not server_name:
                        server_name = server_part
            elif ":" in tool_name:
                server_name, base_name = tool_name.split(":", 1)
            else:
                for srv in sorted(self._known_mcp_servers, key=len, reverse=True):
                    if tool_name.startswith(f"{srv}_"):
                        server_name = srv
                        base_name = tool_name[len(srv) + 1 :]
                        break

        if not base_name:
            base_name = tool_name

        candidates: list[str] = [tool_name, base_name]
        if server_name:
            candidates.extend([
                f"{server_name}_{base_name}",
                f"{server_name}:{base_name}",
                f"mcp__{server_name}__{base_name}",
                server_name,
            ])
            if self._subagent_name:
                candidates.extend([
                    f"mcp__plugin_{self._subagent_name}_{server_name}__{base_name}",
                    f"mcp__plugin__{self._subagent_name}__{server_name}__{base_name}",
                ])

        from opscloud.hooks.tools import to_wire_tool_name

        wire_name = to_wire_tool_name(tool_name, mcp_server=server_name)
        if wire_name not in candidates:
            candidates.append(wire_name)

        for cand in candidates:
            for pattern in self._allowed_patterns:
                if _entry_matches_tool(pattern, cand, base_name, server_name):
                    return True

        return False

    def _validate_tool_call(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = (
            request.tool_call.get("name", "")
            if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict)
            else ""
        )
        tool_obj = getattr(request, "tool", None)
        if self.is_tool_allowed(tool_name, tool_obj=tool_obj):
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
    "ALWAYS_ALLOWED_SYSTEM_TOOLS",
    "TOOL_ALIAS_MAP",
    "ToolFilterMiddleware",
]
