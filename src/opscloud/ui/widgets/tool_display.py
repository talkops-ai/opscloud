"""Formatting utilities for tool call display in the OpsCloud TUI.

Provides generic, extensible tool header and result summary formatting.
Supports runtime tool registrations and automatic snake_case to TitleCase
fallbacks for custom MCP and plugin tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, NamedTuple

MAX_ARG_LENGTH = 80

# ── Dynamic Registry System ───────────────────────────────

_CUSTOM_DISPLAY_NAMES: dict[str, str] = {
    "read_file": "Read",
    "view_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "delete": "Delete",
    "execute": "Execute",
    "exec": "Execute",
    "bash": "Bash",
    "ls": "Ls",
    "list_dir": "Ls",
    "grep": "Grep",
    "glob": "Glob",
    "web_search": "Search",
    "fetch_url": "Fetch",
    "task": "task",
    "ask_user": "Ask",
}

_CUSTOM_SUMMARY_FORMATTERS: dict[str, Callable[[str], str]] = {}

import re


def parse_scoped_tool_name(tool_name: str) -> tuple[str, str | None]:
    """Parse scoped tool name into (base_tool_name, scope_label).

    Handles:
    - plugin scoped: plugin__aws-compute_talkops-devops_aws__run_script_f0b5243c096e
      -> ('run_script', 'aws-compute')
    - mcp scoped: mcp__github__create_issue -> ('create_issue', 'github')
    - subagent scoped: subagent__helm-operator__install -> ('install', 'helm-operator')
    - hash suffixed: run_script_f0b5243c096e -> ('run_script', None)
    - standard: read_file -> ('read_file', None)
    """
    if not tool_name or tool_name == "None":
        return ("tool", None)

    # 1. MCP Subagent scoped: mcp__plugin__<subagent>__<server>__<tool>
    sub_mcp_m = re.match(
        r"^mcp__plugin__([^_]+(?:_[^_]+)*?)__([^_]+(?:_[^_]+)*?)__(.+?)(?:_[0-9a-f]{8,16})?$",
        tool_name,
    )
    if sub_mcp_m:
        scope = sub_mcp_m.group(1).split("_")[0]
        base = sub_mcp_m.group(3)
        return (base, scope)

    # 2. Standard MCP scoped: mcp__<server>__<tool>
    mcp_m = re.match(r"^mcp__([^_]+(?:_[^_]+)*?)__(.+?)(?:_[0-9a-f]{8,16})?$", tool_name)
    if mcp_m:
        server = mcp_m.group(1).split("_")[0]
        base = mcp_m.group(2)
        return (base, server)

    # 3. Plugin scoped: plugin__<namespace>__<tool>(_<hex8+>)?
    plug_m = re.match(r"^plugin__([a-zA-Z0-9_-]+?)__(.+?)(?:_[0-9a-f]{8,16})?$", tool_name)
    if plug_m:
        ns = plug_m.group(1)
        plugin_name = ns.split("_")[0]
        base = plug_m.group(2)
        return (base, plugin_name)

    # 4. Subagent scoped: subagent__<subagent>__<tool>(_<hex8+>)?
    sub_m = re.match(r"^subagent__([a-zA-Z0-9_-]+?)__(.+?)(?:_[0-9a-f]{8,16})?$", tool_name)
    if sub_m:
        scope = sub_m.group(1)
        base = sub_m.group(2)
        return (base, scope)

    # 5. Trailing hash fallback: <tool>_<hex8+>
    hash_m = re.match(r"^(.+?)_[0-9a-f]{8,16}$", tool_name)
    if hash_m:
        return (hash_m.group(1), None)

    return (tool_name, None)


def register_tool_display_name(tool_name: str, display_name: str) -> None:
    """Register or override a custom display label for a tool."""
    _CUSTOM_DISPLAY_NAMES[tool_name.lower()] = display_name


def register_tool_summary_formatter(tool_name: str, formatter: Callable[[str], str]) -> None:
    """Register a custom result summary formatter for a tool."""
    _CUSTOM_SUMMARY_FORMATTERS[tool_name.lower()] = formatter


def get_tool_display_name(tool_name: str) -> str:
    """Resolve the display label for any tool name.

    Uses explicit override if present, otherwise transforms snake_case / kebab-case
    into TitleCase automatically. Preserves and formats scoped identifiers
    (e.g. `plugin__aws-compute...__run_script...` -> `RunScript [aws-compute]`).
    """
    if not tool_name or tool_name == "None":
        return "Tool"

    base_name, scope = parse_scoped_tool_name(tool_name)
    display = _base_display_name(base_name)

    if scope:
        return f"{display} [{scope}]"
    return display


def _base_display_name(base_name: str) -> str:
    """Display label for an unscoped tool name (override or TitleCase)."""
    raw_lower = base_name.lower()
    if raw_lower in _CUSTOM_DISPLAY_NAMES:
        return _CUSTOM_DISPLAY_NAMES[raw_lower]
    # Convert snake_case or kebab-case to TitleCase
    normalized = base_name.replace("-", "_")
    parts = [part for part in normalized.split("_") if part]
    if parts:
        return "".join(part.capitalize() for part in parts)
    return base_name.capitalize()


# ── String & Path Formatting Helpers ───────────────────────


def truncate_value(value: str, max_length: int = MAX_ARG_LENGTH) -> str:
    """Truncate a string value if it exceeds max_length with an ellipsis."""
    if len(value) > max_length:
        return value[:max_length] + "…"
    return value


def abbreviate_path(path_str: str, max_length: int = 60) -> str:
    """Abbreviate a file path intelligently (showing relative path or filename)."""
    if not path_str:
        return ""
    try:
        path = Path(path_str)
        if len(path.parts) == 1:
            return path_str
        try:
            rel_path = path.relative_to(Path.cwd())
            rel_str = str(rel_path)
            if len(rel_str) < len(path_str) and len(rel_str) <= max_length:
                return rel_str
        except (ValueError, OSError):
            pass
        if len(path_str) <= max_length:
            return path_str
        return path.name
    except Exception:
        return truncate_value(path_str, max_length)


# ── Smart Tool Display Formatter ───────────────────────────


class ToolDisplayParts(NamedTuple):
    """Structured pieces of a tool-call header, for styled rendering.

    Attributes:
        name: Display label without the scope tag (e.g. ``"Execute"``).
        scope: Plugin / MCP server / subagent scope, if the tool is scoped.
        detail: Human-readable argument summary, without the legacy
            parentheses or quotes (e.g. ``kubectl get pods``).
        kind: Which heuristic produced ``detail``: ``task``, ``ask``,
            ``path``, ``query``, ``command``, ``code``, ``url``, ``text``,
            ``count``, ``value`` or ``none``.
    """

    name: str
    scope: str | None
    detail: str
    kind: str


_QUOTED_KINDS = frozenset({"command", "code", "url", "text"})
"""Kinds whose ``detail`` the legacy header string wraps in quotes."""


def format_tool_parts(
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
) -> ToolDisplayParts:
    """Split a tool call into display name, scope and argument summary.

    Uses the same heuristics as `format_tool_display` (which is built on top
    of this), so the two never disagree about what a call is doing.

    Examples:
        execute(command="kubectl get pods") -> ("Execute", None, "kubectl get pods", "command")
        read_file(file_path="README.md") -> ("Read", None, "README.md", "path")
    """
    args = tool_args or {}
    raw_name = tool_name if (tool_name and tool_name != "None") else "tool"
    base_name, scope = parse_scoped_tool_name(raw_name)
    name = _base_display_name(base_name)

    def parts(detail: str, kind: str) -> ToolDisplayParts:
        return ToolDisplayParts(name, scope, detail, kind)

    # 1. Dedicated formatters for core agent tools
    if base_name in {"task"}:
        sub_name = args.get("subagent_type") or args.get("agent_name") or args.get("subagent") or "subagent"
        return parts(str(sub_name), "task")

    if base_name == "ask_user":
        qs = args.get("questions")
        q_count = len(qs) if isinstance(qs, list) else 1
        return parts(f"{q_count} question{'s' if q_count > 1 else ''}", "ask")

    # 2. Generic Parameter Inspection Heuristics (Extensible for ANY tool)
    path_val = (
        args.get("file_path")
        or args.get("path")
        or args.get("target_file")
        or args.get("directory_path")
        or args.get("dir")
        or args.get("file")
    )
    cmd_val = args.get("command") or args.get("cmd")
    code_val = args.get("code") or args.get("script")
    query_val = args.get("query") or args.get("pattern")
    url_val = args.get("url") or args.get("uri")
    scope_val = args.get("search_path") or args.get("directory") or args.get("path")

    if path_val is not None and not query_val:
        return parts(abbreviate_path(str(path_val)), "path")

    if query_val is not None:
        q_str = truncate_value(str(query_val).splitlines()[0] if str(query_val) else "", max_length=50)
        scope_str = f" in {abbreviate_path(str(scope_val))}" if scope_val else ""
        return parts(f'"{q_str}"{scope_str}', "query")

    if cmd_val is not None:
        first_line = str(cmd_val).strip().splitlines()[0] if str(cmd_val).strip() else ""
        return parts(truncate_value(first_line, max_length=60), "command")

    if code_val is not None:
        first_line = str(code_val).strip().splitlines()[0] if str(code_val).strip() else ""
        return parts(truncate_value(first_line, max_length=40), "code")

    if url_val is not None:
        return parts(truncate_value(str(url_val), max_length=60), "url")

    # 3. Fallback for any tool with arguments
    if args:
        _first_key, first_val = next(iter(args.items()))
        if isinstance(first_val, str):
            first_line = first_val.strip().splitlines()[0] if first_val.strip() else ""
            return parts(truncate_value(first_line, max_length=40), "text")
        if isinstance(first_val, list):
            return parts(f"{len(first_val)} items", "count")
        if isinstance(first_val, dict):
            return parts(f"{len(first_val)} keys", "count")
        return parts(truncate_value(str(first_val), max_length=40), "value")

    return parts("", "none")


def format_tool_display(
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
    prefix: str = "●",
) -> str:
    """Format any tool call into a clean Claude Code style header string.

    Automatically handles known tools as well as arbitrary future tools via
    generic parameter inspection heuristics.

    Examples:
        read_file(file_path="README.md") -> "● Read(README.md)"
        deploy_helm_chart(chart="nginx") -> '● DeployHelmChart("nginx")'
    """
    p = format_tool_parts(tool_name, tool_args)
    display_name = f"{p.name} [{p.scope}]" if p.scope else p.name
    if p.kind == "task":
        return f"{prefix} {display_name} [{p.detail}]"
    if p.kind in _QUOTED_KINDS:
        return f'{prefix} {display_name}("{p.detail}")'
    return f"{prefix} {display_name}({p.detail})"


# ── Smart Tool Result Summary Formatter ────────────────────


def format_tool_result_summary(tool_name: str, result: str) -> str:
    """Build a clean Claude Code style tree summary (e.g. '⎿ Read 600 lines').

    Extensible via custom summary formatters and generic JSON/multiline heuristics.
    """
    if not result:
        return "⎿ Done"

    raw_lower = tool_name.lower() if tool_name else ""

    # Check registered custom formatter
    if raw_lower in _CUSTOM_SUMMARY_FORMATTERS:
        try:
            return _CUSTOM_SUMMARY_FORMATTERS[raw_lower](result)
        except Exception:
            pass

    lines = [line for line in result.splitlines() if line.strip()]
    line_count = len(result.splitlines())

    # Core tool specific formatters
    if raw_lower in {"read_file", "view_file", "read"}:
        return f"⎿ Read {_plural(line_count, 'line')}"
    elif raw_lower in {"ls", "list_dir", "glob"}:
        if result.startswith("[") and result.endswith("]"):
            try:
                items = json.loads(result.replace("'", '"'))
                if isinstance(items, list):
                    return f"⎿ {_plural(len(items), 'item')} found"
            except Exception:
                pass
        return f"⎿ {_plural(len(lines), 'item')} found"
    elif raw_lower in {"grep", "search"}:
        return f"⎿ {_plural(len(lines), 'match', 'matches')} found"
    elif raw_lower in {"execute", "exec", "bash"}:
        return f"⎿ Command output ({_plural(line_count, 'line')})"
    elif raw_lower in {"edit_file", "write_file", "edit", "write"}:
        return f"⎿ Wrote {_plural(line_count, 'line')}"

    # Generic Fallback Heuristics for ANY future tool output
    trimmed = result.strip()
    if (trimmed.startswith("[") and trimmed.endswith("]")) or (
        trimmed.startswith("{") and trimmed.endswith("}")
    ):
        try:
            parsed = json.loads(trimmed)
            if isinstance(parsed, list):
                return f"⎿ {len(parsed)} items returned"
            elif isinstance(parsed, dict):
                return f"⎿ {len(parsed)} fields returned"
        except Exception:
            pass

    if line_count > 1:
        return f"⎿ {line_count} lines returned"

    return f"⎿ {truncate_value(trimmed, max_length=50)}"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """``1 match`` / ``3 matches``."""
    word = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


__all__ = [
    "MAX_ARG_LENGTH",
    "ToolDisplayParts",
    "abbreviate_path",
    "format_tool_display",
    "format_tool_parts",
    "format_tool_result_summary",
    "get_tool_display_name",
    "parse_scoped_tool_name",
    "register_tool_display_name",
    "register_tool_summary_formatter",
    "truncate_value",
]
