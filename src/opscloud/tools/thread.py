"""Runtime thread identification tool for OpsCloud."""

from __future__ import annotations

from langchain_core.tools import tool
from langgraph.config import get_config

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


@tool
def get_current_thread_id() -> str:
    """Get the current OpsCloud thread ID for LangSmith or MCP tooling.

    Returns:
        The current `configurable.thread_id`, or an explanatory message if missing.
    """
    try:
        from langgraph.config import get_config

        thread_id = get_config().get("configurable", {}).get("thread_id")
    except Exception:
        thread_id = None

    if isinstance(thread_id, str) and thread_id:
        return thread_id
    return "No current thread ID is available."



