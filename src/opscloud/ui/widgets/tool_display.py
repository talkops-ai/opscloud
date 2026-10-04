"""Formatting utilities for tool call display in the OpsCloud TUI.

Provides generic, extensible tool header and result summary formatting.
Supports runtime tool registrations and automatic snake_case to TitleCase
fallbacks for custom MCP and plugin tools.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

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


def register_tool_display_name(tool_name: str, display_name: str) -> None:
    """Register or override a custom display label for a tool."""
    _CUSTOM_DISPLAY_NAMES[tool_name.lower()] = display_name


def register_tool_summary_formatter(tool_name: str, formatter: Callable[[str], str]) -> None:
    """Register a custom result summary formatter for a tool."""
    _CUSTOM_SUMMARY_FORMATTERS[tool_name.lower()] = formatter


def get_tool_display_name(tool_name: str) -> str:
    """Resolve the display label for any tool name.

    Uses explicit override if present, otherwise transforms snake_case / kebab-case
    into TitleCase automatically (e.g. ``deploy_helm_chart`` -> ``DeployHelmChart``).
    """
    if not tool_name or tool_name == "None":
        return "Tool"

    raw_lower = tool_name.lower()
    if raw_lower in _CUSTOM_DISPLAY_NAMES:
        return _CUSTOM_DISPLAY_NAMES[raw_lower]

    # Generic rule: convert snake_case or kebab-case to TitleCase
    normalized = tool_name.replace("-", "_")
    parts = [part for part in normalized.split("_") if part]
    if parts:
        return "".join(part.capitalize() for part in parts)
    return tool_name.capitalize()


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
    args = tool_args or {}
    raw_name = tool_name if (tool_name and tool_name != "None") else "tool"
    display_name = get_tool_display_name(raw_name)

    # 1. Dedicated formatters for core agent tools
    if raw_name in {"task"}:
        sub_name = args.get("subagent_type") or args.get("agent_name") or args.get("subagent") or "subagent"
        return f"{prefix} {display_name} [{sub_name}]"

    elif raw_name == "ask_user":
        qs = args.get("questions")
        q_count = len(qs) if isinstance(qs, list) else 1
        return f"{prefix} {display_name}({q_count} question{'s' if q_count > 1 else ''})"

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
    query_val = args.get("query") or args.get("pattern")
    url_val = args.get("url") or args.get("uri")
    scope_val = args.get("search_path") or args.get("directory") or args.get("path")

    if path_val is not None and not query_val:
        path_str = abbreviate_path(str(path_val))
        return f"{prefix} {display_name}({path_str})"

    elif query_val is not None:
        q_str = truncate_value(str(query_val), max_length=50)
        scope = f" in {abbreviate_path(str(scope_val))}" if scope_val else ""
        return f'{prefix} {display_name}("{q_str}"{scope})'

    elif cmd_val is not None:
        cmd_str = truncate_value(str(cmd_val), max_length=60)
        return f'{prefix} {display_name}("{cmd_str}")'

    elif url_val is not None:
        u_str = truncate_value(str(url_val), max_length=60)
        return f'{prefix} {display_name}("{u_str}")'

    # 3. Fallback for any tool with arguments
    if args:
        first_key, first_val = next(iter(args.items()))
        if isinstance(first_val, str):
            val_str = truncate_value(first_val, max_length=40)
            return f'{prefix} {display_name}("{val_str}")'
        elif isinstance(first_val, list):
            return f"{prefix} {display_name}({len(first_val)} items)"
        elif isinstance(first_val, dict):
            return f"{prefix} {display_name}({len(first_val)} keys)"
        val_str = truncate_value(str(first_val), max_length=40)
        return f"{prefix} {display_name}({val_str})"

    return f"{prefix} {display_name}()"


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
        return f"⎿ Read {line_count} lines"
    elif raw_lower in {"ls", "list_dir", "glob"}:
        if result.startswith("[") and result.endswith("]"):
            try:
                items = json.loads(result.replace("'", '"'))
                if isinstance(items, list):
                    return f"⎿ {len(items)} items found"
            except Exception:
                pass
        return f"⎿ {len(lines)} items found"
    elif raw_lower in {"grep", "search"}:
        return f"⎿ {len(lines)} matches found"
    elif raw_lower in {"execute", "exec", "bash"}:
        return f"⎿ Command output ({line_count} lines)"
    elif raw_lower in {"edit_file", "write_file", "edit", "write"}:
        return f"⎿ Wrote {line_count} lines"

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


__all__ = [
    "MAX_ARG_LENGTH",
    "abbreviate_path",
    "format_tool_display",
    "format_tool_result_summary",
    "get_tool_display_name",
    "register_tool_display_name",
    "register_tool_summary_formatter",
    "truncate_value",
]
