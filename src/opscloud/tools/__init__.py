"""Tools catalog and registry for OpsCloud."""

from __future__ import annotations

from opscloud.tools.catalog import register_all_tools
from opscloud.tools.display import (
    format_json_output,
    format_tool_error,
    format_tool_success,
)
from opscloud.tools.fetch_url import fetch_url
from opscloud.tools.goal_tools import get_goal, get_rubric, update_goal
from opscloud.tools.registry import ToolRegistry
from opscloud.tools.thread import get_current_thread_id
from opscloud.tools.web_search import (
    create_web_search_tool,
    is_web_search_tool,
    web_search,
)

__all__ = [
    "ToolRegistry",
    "create_web_search_tool",
    "fetch_url",
    "format_json_output",
    "format_tool_error",
    "format_tool_success",
    "get_current_thread_id",
    "get_goal",
    "get_rubric",
    "is_web_search_tool",
    "register_all_tools",
    "update_goal",
    "web_search",
]

