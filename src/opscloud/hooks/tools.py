"""Native OpsCloud tool vocabulary mapped to compatible wire names."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from opscloud.hooks.models.domain import ToolCallData
    from opscloud.json_types import JsonObject


def _select(args: JsonObject, *fields: str) -> JsonObject:
    """Filter dictionary to only contain specified keys if present.

    Args:
        args: Input argument dictionary.
        *fields: Allowed field names to preserve.

    Returns:
        Filtered dictionary containing only the specified present keys.
    """
    return {field: args[field] for field in fields if field in args}


def _bash_input(args: JsonObject) -> JsonObject:
    """Project execute/bash arguments to wire inputs, converting seconds to milliseconds.

    Args:
        args: Native tool argument dictionary.

    Returns:
        Wire input dictionary with timeout in milliseconds.
    """
    result = _select(args, "command")
    if "timeout" in args:
        timeout = args["timeout"]
        result["timeout"] = timeout * 1000 if isinstance(timeout, int) and not isinstance(timeout, bool) else timeout
    return result


def _write_input(args: JsonObject) -> JsonObject:
    """Project write_file arguments to wire inputs.

    Args:
        args: Native tool argument dictionary.

    Returns:
        Wire input dictionary with file_path and content.
    """
    return _select(args, "file_path", "content")


def _edit_input(args: JsonObject) -> JsonObject:
    """Project edit_file arguments to wire inputs.

    Args:
        args: Native tool argument dictionary.

    Returns:
        Wire input dictionary with replacement details.
    """
    return _select(args, "file_path", "old_string", "new_string", "replace_all")


def _read_input(args: JsonObject) -> JsonObject:
    """Project read_file arguments to wire inputs, converting 0-based offset to 1-based.

    Args:
        args: Native tool argument dictionary.

    Returns:
        Wire input dictionary with 1-based offset.
    """
    result = _select(args, "file_path", "limit")
    if "offset" in args:
        offset = args["offset"]
        result["offset"] = offset + 1 if isinstance(offset, int) and not isinstance(offset, bool) else offset
    return result


def _glob_input(args: JsonObject) -> JsonObject:
    """Project glob arguments to wire inputs.

    Args:
        args: Native tool argument dictionary.

    Returns:
        Wire input dictionary with pattern and path.
    """
    return _select(args, "pattern", "path")


def _grep_input(args: JsonObject) -> JsonObject:
    """Project grep arguments to wire inputs, escaping regex and mapping head limit.

    Args:
        args: Native tool argument dictionary.

    Returns:
        Wire input dictionary for wire Grep tool.
    """
    result = _select(args, "path", "glob", "output_mode")
    if "pattern" in args:
        pattern = args["pattern"]
        result["pattern"] = re.escape(pattern) if isinstance(pattern, str) else pattern
    if "max_count" in args:
        result["head_limit"] = args["max_count"]
    return result


def _ls_input(args: JsonObject) -> JsonObject:
    """Project ls arguments to wire inputs.

    Args:
        args: Native tool argument dictionary.

    Returns:
        Wire input dictionary with path.
    """
    return _select(args, "path")


def _web_search_input(args: JsonObject) -> JsonObject:
    """Project web_search arguments to wire inputs.

    Args:
        args: Native tool argument dictionary.

    Returns:
        Wire input dictionary with query and max_results.
    """
    return _select(args, "query", "max_results")


def _fetch_url_input(args: JsonObject) -> JsonObject:
    """Project fetch_url arguments to wire inputs.

    Args:
        args: Native tool argument dictionary.

    Returns:
        Wire input dictionary with url and timeout.
    """
    return _select(args, "url", "timeout")


_NATIVE_TO_WIRE: dict[str, tuple[str, Callable[[JsonObject], JsonObject]]] = {
    "execute": ("Bash", _bash_input),
    "write_file": ("Write", _write_input),
    "edit_file": ("Edit", _edit_input),
    "read_file": ("Read", _read_input),
    "glob": ("Glob", _glob_input),
    "grep": ("Grep", _grep_input),
    "ls": ("LS", _ls_input),
    "web_search": ("WebSearch", _web_search_input),
    "fetch_url": ("WebFetch", _fetch_url_input),
    "fetch_web_page": ("WebFetch", _fetch_url_input),
}

_MCP_WIRE_RE = re.compile(r"^mcp__.+__.+$")


def format_mcp_wire_name(server: str, tool: str) -> str:
    """Format a compatible MCP wire tool name.

    Args:
        server: MCP server identifier.
        tool: Tool name on the MCP server.

    Returns:
        Standardized wire representation `mcp__{server}__{tool}`.
    """
    return f"mcp__{server}__{tool}"


def to_wire_tool_name(
    name: str,
    *,
    mcp_server: str | None = None,
) -> str:
    """Map a native tool name to the compatible wire tool name.

    Args:
        name: Native tool name or MCP tool name.
        mcp_server: Optional MCP server name if this tool belongs to an MCP server.

    Returns:
        Standardized wire name (e.g. 'Bash', 'mcp__server__tool', or original name).
    """
    if _MCP_WIRE_RE.fullmatch(name) is not None:
        return name
    if mcp_server is not None:
        prefix = f"{mcp_server}_"
        tool = name.removeprefix(prefix) if name.startswith(prefix) else name
        return format_mcp_wire_name(mcp_server, tool)
    adapter = _NATIVE_TO_WIRE.get(name)
    return adapter[0] if adapter is not None else name


def to_wire_tool_input(name: str, args: JsonObject) -> JsonObject:
    """Map native tool arguments to the compatible wire input object.

    Args:
        name: Native tool name.
        args: Input argument dictionary.

    Returns:
        Adapted wire input dictionary.
    """
    adapter = _NATIVE_TO_WIRE.get(name)
    return adapter[1](args) if adapter is not None else dict(args)


def to_wire_call(
    call: ToolCallData,
) -> tuple[str, JsonObject]:
    """Project a native tool call into compatible wire name and input.

    Args:
        call: Native ToolCallData instance.

    Returns:
        A tuple of (wire_tool_name, wire_tool_input).
    """
    name = to_wire_tool_name(call.name, mcp_server=call.mcp_server)
    if call.mcp_server is not None or _MCP_WIRE_RE.fullmatch(call.name) is not None:
        return name, dict(call.args)
    return name, to_wire_tool_input(call.name, call.args)
