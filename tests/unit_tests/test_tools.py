"""Unit tests for tool catalog and goal tools."""

import json
import pytest
from opscloud.tools.registry import ToolRegistry
from opscloud.tools.catalog import register_all_tools
from opscloud.tools.goal_tools import _goal_snapshot, _rubric_snapshot


def test_tool_registry():
    register_all_tools()
    registry = ToolRegistry.get_instance()
    tool = registry.build_tool("get_current_thread_id")
    assert tool.name == "get_current_thread_id"


def test_goal_and_rubric_snapshots():
    state = {
        "_goal_objective": "Configure AWS CloudFront CDN",
        "_goal_status": "active",
        "_goal_rubric": "- Distribution is deployed\n- Custom domain linked",
    }
    goal = _goal_snapshot(state)
    assert goal["active"] is True
    assert goal["objective"] == "Configure AWS CloudFront CDN"
    assert goal["status"] == "active"

    rubric = _rubric_snapshot(state)
    assert rubric["active"] is True
    assert "Distribution is deployed" in rubric["criteria"]


@pytest.mark.asyncio
async def test_goal_tools_middleware_notice_generation_and_deduplication():
    from opscloud.middleware.goal_tools import GoalToolsMiddleware

    middleware = GoalToolsMiddleware()
    # No goal -> no notice
    assert await middleware.abefore_model({"messages": []}, runtime=None) is None

    # Active goal -> generates notice via build_goal_state_notice(mapping)
    state = {
        "_goal_objective": "Configure AWS CloudFront CDN",
        "_goal_status": "active",
        "messages": [],
    }
    update = await middleware.abefore_model(state, runtime=None)
    assert update is not None
    assert len(update["messages"]) == 1
    notice = update["messages"][0]
    assert "Goal/rubric state changed" in notice.content

    # State with notice present -> deduplicates
    state["messages"] = [notice]
    assert await middleware.abefore_model(state, runtime=None) is None


def test_get_current_thread_id(monkeypatch):
    """Test get_current_thread_id with explicit config, get_config, and empty state."""
    from opscloud.tools.thread import get_current_thread_id

    # 1. With config kwarg via invoke
    res = get_current_thread_id.invoke({}, config={"configurable": {"thread_id": "thread-123"}})
    assert res == "thread-123"


    # 2. With langgraph.config.get_config
    monkeypatch.setattr(
        "langgraph.config.get_config",
        lambda: {"configurable": {"thread_id": "thread-via-langgraph"}},
    )
    res_lg = get_current_thread_id.invoke({})
    assert res_lg == "thread-via-langgraph"

    # 3. When missing
    monkeypatch.setattr("langgraph.config.get_config", lambda: {})
    res_missing = get_current_thread_id.invoke({})
    assert res_missing == "No current thread ID is available."


def test_web_search_marker_and_factory(monkeypatch):
    """Test web_search marker, create_web_search_tool, and is_web_search_tool."""
    from opscloud.tools.web_search import (
        create_web_search_tool,
        is_web_search_tool,
        web_search,
    )

    # Standard tool is recognized
    assert is_web_search_tool(web_search) is True

    # Factory creates a marked tool
    bound_tool = create_web_search_tool("tvly-test-key-12345")
    assert bound_tool.name == "web_search"
    assert is_web_search_tool(bound_tool) is True

    # Other tools are not recognized
    from opscloud.tools.fetch_url import fetch_url

    assert is_web_search_tool(fetch_url) is False

    # Calling web_search without Tavily key returns helpful error dictionary
    import sys

    ws_mod = sys.modules["opscloud.tools.web_search"]
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(ws_mod, "_get_tavily_client", lambda: None)
    err_res = web_search.invoke({"query": "kubernetes pod status"})
    assert isinstance(err_res, dict)
    assert "error" in err_res
    assert "Tavily API key is not configured" in err_res["error"]




def test_fetch_url_validation_and_user_agent(monkeypatch):
    """Test fetch_url SSRF guard and user-agent header."""
    from opscloud.tools.fetch_url import fetch_url

    # SSRF guard blocks loopback
    res_loopback = fetch_url.invoke({"url": "http://127.0.0.1:8080/secret"})
    assert "error" in res_loopback
    assert res_loopback.get("category") == "validation"

    # Disallowed scheme
    res_ftp = fetch_url.invoke({"url": "ftp://files.example.com/data"})
    assert "error" in res_ftp
    assert res_ftp.get("category") == "validation"

