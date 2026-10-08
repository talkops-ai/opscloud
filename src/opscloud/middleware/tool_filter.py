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
    known_servers: set[str] | None = None,
    subagent_name: str | None = None,
) -> tuple[str | None, str | None]:
    """Parse (server_name, tool_pattern) from an MCP tool pattern generically.

    Supports arbitrary namespaces and patterns:
    - mcp__<server>__<tool>
    - mcp__<namespace>_<server>__<tool>
    - mcp__<namespace>__<server>__<tool>
    - <server>:<tool>
    - <server>_*
    """
    known = known_servers or set()
    pat = pattern.strip()

    if pat.startswith("mcp__"):
        rest = pat[5:]
        if "__" in rest:
            qualifier, tool_pat = rest.rsplit("__", 1)
            # 1. Direct match with known servers
            if qualifier in known:
                return qualifier, tool_pat
            # 2. Check if qualifier ends with a known server (e.g. prefix_server or prefix__server)
            for srv in sorted(known, key=len, reverse=True):
                if qualifier.endswith(f"_{srv}") or qualifier.endswith(f"__{srv}"):
                    return srv, tool_pat
            # 3. Subagent name check if provided
            if subagent_name and (qualifier == subagent_name or qualifier.endswith(f"_{subagent_name}")):
                return subagent_name, tool_pat
            # 4. Fallback: extract last token
            if "__" in qualifier:
                return qualifier.rsplit("__", 1)[1], tool_pat
            if "_" in qualifier:
                return qualifier.rsplit("_", 1)[1], tool_pat
            return qualifier, tool_pat
    elif ":" in pat:
        srv, tool_pat = pat.split(":", 1)
        for known_srv in sorted(known, key=len, reverse=True):
            if srv == known_srv or srv.endswith(f"_{known_srv}") or srv.endswith(f"__{known_srv}"):
                return known_srv, tool_pat
        return srv, tool_pat
    elif pat.endswith("_*"):
        srv = pat[:-2]
        for known_srv in sorted(known, key=len, reverse=True):
            if srv == known_srv or srv.endswith(f"_{known_srv}"):
                return known_srv, "*"
        return srv, "*"

    return None, None


def _expand_tool_patterns(
    patterns: Sequence[str],
    known_servers: set[str] | None = None,
    subagent_name: str | None = None,
) -> tuple[str, ...]:
    """Expand tool pattern list with aliases, wire projections, and standard MCP variations."""
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
    entry: str,
    candidates: Sequence[str] | set[str],
    server_name: str | None = None,
    original_name: str | None = None,
) -> bool:
    """Return True if a single filter entry matches any tool candidate.

    Aligned directly with dcode's _entry_matches_tool candidate matching:
    checks candidates via case-insensitive fnmatchcase when glob metachars are present,
    or case-insensitive equality otherwise. Also checks structured MCP qualifiers.
    """
    entry_clean = entry.strip()
    entry_lower = entry_clean.lower()
    has_glob = any(ch in _GLOB_METACHARS for ch in entry_clean)

    # 1. Match against generated candidates
    for cand in candidates:
        cand_lower = cand.lower()
        if has_glob:
            if fnmatch.fnmatchcase(cand_lower, entry_lower):
                return True
        else:
            if cand_lower == entry_lower:
                return True

    # 2. Semantic matching for server-qualified patterns (e.g. "billing:*", "billing", "mcp__..._billing__*")
    if server_name:
        srv_lower = server_name.lower()
        if entry_lower in {srv_lower, f"{srv_lower}:*", f"{srv_lower}_*", f"mcp__{srv_lower}__*"}:
            return True

        # Parse qualifier and tool pattern from entry
        tool_pat: str | None = None
        qualifier: str | None = None
        if entry_clean.startswith("mcp__") and "__" in entry_clean[5:]:
            qualifier, tool_pat = entry_clean[5:].rsplit("__", 1)
        elif ":" in entry_clean:
            qualifier, tool_pat = entry_clean.split(":", 1)

        if qualifier is not None and tool_pat is not None:
            qual_lower = qualifier.lower()
            if (
                qual_lower == srv_lower
                or qual_lower.endswith(f"_{srv_lower}")
                or qual_lower.endswith(f"__{srv_lower}")
            ):
                if tool_pat == "*":
                    return True
                if original_name:
                    orig_lower = original_name.lower()
                    pat_lower = tool_pat.lower()
                    if any(ch in _GLOB_METACHARS for ch in tool_pat):
                        return fnmatch.fnmatchcase(orig_lower, pat_lower)
                    return orig_lower == pat_lower

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
                        patterns.append(f"{server}:*")
                    if "tools" in cap and isinstance(cap["tools"], (list, tuple)):
                        patterns.extend(str(t) for t in cap["tools"])
                elif isinstance(cap, str):
                    patterns.append(cap)

        self._allowed_patterns: tuple[str, ...] = (
            _expand_tool_patterns(patterns, self._known_mcp_servers, self._subagent_name)
            if patterns
            else ()
        )

    def _get_tool_candidates(
        self,
        tool_name: str,
        server_name: str | None = None,
        original_name: str | None = None,
    ) -> list[str]:
        """Generate dcode-aligned candidate names for a tool."""
        candidates: list[str] = [tool_name]
        if original_name and original_name != tool_name:
            candidates.append(original_name)

        if server_name:
            candidates.append(server_name)
            if original_name:
                candidates.extend([
                    f"{server_name}_{original_name}",
                    f"{server_name}:{original_name}",
                    f"mcp__{server_name}__{original_name}",
                ])

        # Aliases for native tools
        clean = tool_name.lower()
        if clean in TOOL_ALIAS_MAP:
            candidates.extend(TOOL_ALIAS_MAP[clean])
        for alias_key, alias_group in TOOL_ALIAS_MAP.items():
            if tool_name in alias_group or clean in alias_group:
                candidates.append(alias_key)
                candidates.extend(alias_group)

        from opscloud.hooks.tools import to_wire_tool_name

        wire_name = to_wire_tool_name(tool_name, mcp_server=server_name)
        if wire_name and wire_name not in candidates:
            candidates.append(wire_name)

        return list(dict.fromkeys(candidates))

    def is_tool_allowed(self, tool_name: str, tool_obj: Any = None) -> bool:
        """Check if a tool name matches any allowed patterns."""
        if tool_name in ALWAYS_ALLOWED_SYSTEM_TOOLS:
            return True
        if not self._allowed_patterns:
            return True

        server_name: str | None = None
        original_name: str | None = None

        if tool_obj is not None:
            server_name = _mcp_server_from_tool(tool_obj)
            original_name = _mcp_original_name_from_tool(tool_obj)
            if server_name:
                self._known_mcp_servers.add(server_name)
                self._tool_to_server[tool_name] = server_name

        if not server_name and tool_name in self._tool_to_server:
            server_name = self._tool_to_server[tool_name]
            original_name = original_name or self._tool_to_orig_name.get(tool_name)

        if not server_name:
            if tool_name.startswith("mcp__"):
                parts = tool_name[5:].split("__")
                if len(parts) >= 2:
                    original_name = original_name or parts[-1]
                    qualifier = parts[0] if len(parts) == 2 else parts[-2]
                    for srv in sorted(self._known_mcp_servers, key=len, reverse=True):
                        if qualifier == srv or qualifier.endswith(f"_{srv}") or qualifier.endswith(f"__{srv}"):
                            server_name = srv
                            break
                    if not server_name:
                        server_name = qualifier
            elif ":" in tool_name:
                srv, orig = tool_name.split(":", 1)
                original_name = original_name or orig
                for known_srv in sorted(self._known_mcp_servers, key=len, reverse=True):
                    if srv == known_srv or srv.endswith(f"_{known_srv}"):
                        server_name = known_srv
                        break
                if not server_name:
                    server_name = srv
            else:
                for srv in sorted(self._known_mcp_servers, key=len, reverse=True):
                    if tool_name.startswith(f"{srv}_"):
                        server_name = srv
                        original_name = original_name or tool_name[len(srv) + 1 :]
                        break

        if not original_name:
            original_name = tool_name

        candidates = self._get_tool_candidates(tool_name, server_name, original_name)

        for pattern in self._allowed_patterns:
            if _entry_matches_tool(pattern, candidates, server_name=server_name, original_name=original_name):
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
