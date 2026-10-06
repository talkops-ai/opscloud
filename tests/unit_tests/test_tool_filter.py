"""Unit tests for ToolFilterMiddleware and tool pattern matching."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from opscloud.middleware.tool_filter import (
    ToolFilterMiddleware,
    _expand_tool_patterns,
    _parse_mcp_pattern,
    _entry_matches_tool,
)


def test_parse_mcp_pattern():
    known = {"aws-mcp", "billing", "awspricing"}
    subagent = "aws-finops-agent"

    # Plugin-prefixed patterns
    srv, tool = _parse_mcp_pattern("mcp__plugin_aws-finops-agent_aws-mcp__*", known, subagent)
    assert srv == "aws-mcp"
    assert tool == "*"

    srv, tool = _parse_mcp_pattern("mcp__plugin_aws-finops-agent_billing__cost-explorer", known, subagent)
    assert srv == "billing"
    assert tool == "cost-explorer"

    srv, tool = _parse_mcp_pattern("mcp__plugin__aws-finops-agent__billing__*", known, subagent)
    assert srv == "billing"
    assert tool == "*"

    # Unscoped MCP pattern
    srv, tool = _parse_mcp_pattern("mcp__billing__*", known, subagent)
    assert srv == "billing"
    assert tool == "*"

    srv, tool = _parse_mcp_pattern("mcp__awspricing__get-products", known, subagent)
    assert srv == "awspricing"
    assert tool == "get-products"

    # Colon and underscore patterns
    srv, tool = _parse_mcp_pattern("billing:*", known, subagent)
    assert srv == "billing"
    assert tool == "*"

    srv, tool = _parse_mcp_pattern("aws-mcp_*", known, subagent)
    assert srv == "aws-mcp"
    assert tool == "*"


def test_expand_tool_patterns():
    patterns = [
        "Read",
        "Bash",
        "mcp__plugin_aws-finops-agent_aws-mcp__*",
        "mcp__plugin_aws-finops-agent_billing__*",
    ]
    known = {"aws-mcp", "billing"}
    expanded = _expand_tool_patterns(patterns, known_servers=known, subagent_name="aws-finops-agent")

    # Checks aliases
    assert "read_file" in expanded
    assert "view_file" in expanded
    assert "run_command" in expanded
    assert "execute" in expanded

    # Checks MCP expansions
    assert "aws-mcp_*" in expanded
    assert "aws-mcp:*" in expanded
    assert "mcp__aws-mcp__*" in expanded
    assert "aws-mcp" in expanded

    assert "billing_*" in expanded
    assert "billing:*" in expanded
    assert "mcp__billing__*" in expanded
    assert "billing" in expanded


def test_tool_filter_middleware_allows_subagent_mcp_tools():
    allowed = [
        "Read",
        "Grep",
        "Glob",
        "Bash",
        "Write",
        "Skill",
        "TodoWrite",
        "WebFetch",
        "mcp__plugin_aws-finops-agent_aws-mcp__*",
        "mcp__plugin_aws-finops-agent_billing__*",
        "mcp__plugin_aws-finops-agent_awspricing__*",
    ]
    known = ["aws-mcp", "billing", "awspricing"]
    mw = ToolFilterMiddleware(
        allowed_patterns=allowed,
        subagent_name="aws-finops-agent",
        known_mcp_servers=known,
    )

    # AWS MCP tools (provider format with underscore and colon)
    assert mw.is_tool_allowed("aws-mcp_aws___run_script") is True
    assert mw.is_tool_allowed("aws-mcp:aws___run_script") is True
    assert mw.is_tool_allowed("mcp__aws-mcp__aws___run_script") is True
    assert mw.is_tool_allowed("mcp__plugin_aws-finops-agent_aws-mcp__aws___run_script") is True

    # Billing tools
    assert mw.is_tool_allowed("billing_cost-explorer") is True
    assert mw.is_tool_allowed("billing:cost-explorer") is True
    assert mw.is_tool_allowed("mcp__billing__cost-explorer") is True

    # AWS pricing tools
    assert mw.is_tool_allowed("awspricing_get-products") is True
    assert mw.is_tool_allowed("awspricing:get-products") is True

    # Standard tools
    assert mw.is_tool_allowed("run_command") is True
    assert mw.is_tool_allowed("execute") is True
    assert mw.is_tool_allowed("read_file") is True
    assert mw.is_tool_allowed("view_file") is True
    assert mw.is_tool_allowed("write_to_file") is True
    assert mw.is_tool_allowed("grep_search") is True

    # Unallowed tools blocked
    assert mw.is_tool_allowed("slack_post_message") is False
    assert mw.is_tool_allowed("database_drop_all") is False


def test_tool_filter_middleware_with_tool_objects():
    mock_tool = MagicMock()
    mock_tool.name = "billing_cost-explorer"
    mock_tool.metadata = {
        "_mcp_server": "billing",
        "_mcp_original_name": "cost-explorer",
    }

    allowed = ["mcp__plugin_aws-finops-agent_billing__*"]
    mw = ToolFilterMiddleware(
        allowed_patterns=allowed,
        subagent_name="aws-finops-agent",
        mcp_tools=[mock_tool],
    )

    assert mw.is_tool_allowed("billing_cost-explorer") is True
    assert mw.is_tool_allowed("billing_cost-explorer", tool_obj=mock_tool) is True


def test_tool_filter_middleware_validates_request_and_rejects_unallowed():
    allowed = ["read_file", "billing_cost-explorer"]
    mw = ToolFilterMiddleware(allowed_patterns=allowed)

    # Allowed tool call
    req_allowed = ToolCallRequest(
        tool_call={"name": "billing_cost-explorer", "args": {}, "id": "call_1"},
        tool=None,
        state=None,
        runtime=MagicMock(),
    )
    result = mw._validate_tool_call(req_allowed)
    assert result is None

    # Blocked tool call
    req_blocked = ToolCallRequest(
        tool_call={"name": "aws-mcp_aws___run_script", "args": {}, "id": "call_2"},
        tool=None,
        state=None,
        runtime=MagicMock(),
    )
    result = mw._validate_tool_call(req_blocked)
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "tool `aws-mcp_aws___run_script` is restricted" in result.content
