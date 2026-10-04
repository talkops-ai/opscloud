"""Display helpers for rendering tool outputs with syntax highlighting."""

from __future__ import annotations

import json
from typing import Any


def format_tool_error(error_msg: str) -> str:
    """Format an error message with ANSI red color."""
    return f"\033[91mError: {error_msg}\033[0m"


def format_tool_success(msg: str) -> str:
    """Format a success message with ANSI green color."""
    return f"\033[92mSuccess: {msg}\033[0m"


def format_json_output(data: Any) -> str:
    """Format dictionary/JSON output as a pretty printed string."""
    try:
        return json.dumps(data, indent=2)
    except Exception:
        return str(data)
