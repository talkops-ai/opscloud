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


def test_generic_open_source_mcp_matching_and_filtering():
    """Verify arbitrary third-party open-source plugins work with various user-defined pattern styles."""
    known_servers = ["postgres", "github", "datadog"]
    mock_postgres_tool = MagicMock()
    mock_postgres_tool.name = "postgres_query"
    mock_postgres_tool.metadata = {
        "_mcp_server": "postgres",
        "_mcp_original_name": "query",
    }

    mock_github_tool = MagicMock()
    mock_github_tool.name = "github_create_issue"
    mock_github_tool.metadata = {
        "_mcp_server": "github",
        "_mcp_original_name": "create_issue",
    }

    mock_datadog_tool = MagicMock()
    mock_datadog_tool.name = "datadog_get_metrics"
    mock_datadog_tool.metadata = {
        "_mcp_server": "datadog",
        "_mcp_original_name": "get_metrics",
    }

    # Style 1: Server colon wildcard (postgres:*) and server underscore wildcard (github_*)
    mw1 = ToolFilterMiddleware(
        allowed_patterns=["postgres:*", "github_*", "Read", "Bash"],
        subagent_name="custom-devops-subagent",
        known_mcp_servers=known_servers,
        mcp_tools=[mock_postgres_tool, mock_github_tool, mock_datadog_tool],
    )
    assert mw1.is_tool_allowed("postgres_query", tool_obj=mock_postgres_tool) is True
    assert mw1.is_tool_allowed("postgres:query") is True
    assert mw1.is_tool_allowed("mcp__postgres__query") is True
    assert mw1.is_tool_allowed("github_create_issue", tool_obj=mock_github_tool) is True
    assert mw1.is_tool_allowed("read_file") is True
    assert mw1.is_tool_allowed("execute") is True
    # datadog not in allowed_patterns
    assert mw1.is_tool_allowed("datadog_get_metrics", tool_obj=mock_datadog_tool) is False

    # Style 2: Bare server name ("postgres")
    mw2 = ToolFilterMiddleware(
        allowed_patterns=["postgres"],
        subagent_name="custom-devops-subagent",
        known_mcp_servers=known_servers,
        mcp_tools=[mock_postgres_tool, mock_github_tool],
    )
    assert mw2.is_tool_allowed("postgres_query", tool_obj=mock_postgres_tool) is True
    assert mw2.is_tool_allowed("github_create_issue", tool_obj=mock_github_tool) is False

    # Style 3: Arbitrary third-party namespaced pattern (e.g. from open-source marketplace package)
    mw3 = ToolFilterMiddleware(
        allowed_patterns=["mcp__acme-corp_custom-agent_postgres__*"],
        subagent_name="custom-agent",
        known_mcp_servers=["postgres"],
        mcp_tools=[mock_postgres_tool],
    )
    assert mw3.is_tool_allowed("postgres_query", tool_obj=mock_postgres_tool) is True
    assert mw3.is_tool_allowed("mcp__postgres__query") is True


def test_tool_filter_wrap_model_call():
    """Verify wrap_model_call prunes disallowed tools before calling model."""
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.tools import tool

    @tool("read_file")
    def mock_read(path: str) -> str:
        """Read file."""
        return "content"

    @tool("bash")
    def mock_bash(cmd: str) -> str:
        """Execute command."""
        return "out"

    mw = ToolFilterMiddleware(allowed_patterns=["read"])
    fake_llm = FakeListChatModel(responses=["ok"])
    req = ModelRequest(
        model=fake_llm,
        messages=[],
        tools=[mock_read, mock_bash],
    )

    captured_tools = []

    def mock_handler(r):
        captured_tools.extend(r.tools)
        return fake_llm.invoke([])

    mw.wrap_model_call(req, mock_handler)
    assert len(captured_tools) == 1
    assert captured_tools[0].name == "read_file"


@pytest.mark.asyncio
async def test_tool_filter_awrap_model_call():
    """Verify awrap_model_call asynchronously prunes disallowed tools before calling model."""
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langchain_core.tools import tool

    @tool("read_file")
    def mock_read(path: str) -> str:
        """Read file."""
        return "content"

    @tool("bash")
    def mock_bash(cmd: str) -> str:
        """Execute command."""
        return "out"

    mw = ToolFilterMiddleware(allowed_patterns=["read"])
    fake_llm = FakeListChatModel(responses=["ok"])
    req = ModelRequest(
        model=fake_llm,
        messages=[],
        tools=[mock_read, mock_bash],
    )

    captured_tools = []

    async def mock_handler(r):
        captured_tools.extend(r.tools)
        return await fake_llm.ainvoke([])

    await mw.awrap_model_call(req, mock_handler)
    assert len(captured_tools) == 1
    assert captured_tools[0].name == "read_file"


def test_subagent_scoped_mcp_tools_matching_and_model_pruning():
    """Verify subagent-scoped MCP tools with hashed names and plugin patterns are allowed and not pruned."""
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    mock_presigned_tool = MagicMock()
    mock_presigned_tool.name = "subagent__aws-cloud-securit_aws___get_presigned_url_d5d22cc1a56b"
    mock_presigned_tool.metadata = {
        "_opscloud_mcp": True,
        "_mcp_server": "subagent__aws-cloud-security-engineer_talkops-devops-plugi_ca4dbdbf__aws-mcp",
        "_mcp_original_name": "aws___get_presigned_url",
    }

    mock_iam_tool = MagicMock()
    mock_iam_tool.name = "subagent__aws-cloud-security-engineer_ta_list_users_77000c74ca84"
    mock_iam_tool.metadata = {
        "_opscloud_mcp": True,
        "_mcp_server": "subagent__aws-cloud-security-engineer_talkops-devops-plugi_ca4dbdbf__iam",
        "_mcp_original_name": "list_users",
    }

    mock_unrelated_tool = MagicMock()
    mock_unrelated_tool.name = "subagent__other-subagent_other__unrelated_tool"
    mock_unrelated_tool.metadata = {
        "_opscloud_mcp": True,
        "_mcp_server": "subagent__other-subagent__other-mcp",
        "_mcp_original_name": "unrelated_tool",
    }

    allowed_patterns = [
        "Read",
        "Bash",
        "mcp__plugin_aws-cloud-security-engineer_aws-mcp__*",
        "mcp__plugin_aws-cloud-security-engineer_iam__*",
    ]
    known_servers = [
        "subagent__aws-cloud-security-engineer_talkops-devops-plugi_ca4dbdbf__aws-mcp",
        "subagent__aws-cloud-security-engineer_talkops-devops-plugi_ca4dbdbf__iam",
    ]

    mw = ToolFilterMiddleware(
        allowed_patterns=allowed_patterns,
        subagent_name="aws-cloud-security-engineer@talkops-devops-plugins",
        known_mcp_servers=known_servers,
        mcp_tools=[mock_presigned_tool, mock_iam_tool, mock_unrelated_tool],
    )

    # Both plugin MCP tools should be allowed
    assert mw.is_tool_allowed(mock_presigned_tool.name, tool_obj=mock_presigned_tool) is True
    assert mw.is_tool_allowed(mock_iam_tool.name, tool_obj=mock_iam_tool) is True
    assert mw.is_tool_allowed(mock_unrelated_tool.name, tool_obj=mock_unrelated_tool) is False

    # Also test via wrap_model_call
    fake_llm = FakeListChatModel(responses=["ok"])
    req = ModelRequest(
        model=fake_llm,
        messages=[],
        tools=[mock_presigned_tool, mock_iam_tool, mock_unrelated_tool],
    )

    captured = []
    def handler(r):
        captured.extend(r.tools)
        return fake_llm.invoke([])

    mw.wrap_model_call(req, handler)
    assert len(captured) == 2
    assert captured[0].name == mock_presigned_tool.name
    assert captured[1].name == mock_iam_tool.name


def test_subagent_scoped_mcp_tools_with_server_colon_and_bare_name_styles():
    """Verify subagent MCP tools match across colon patterns, bare server names, and bare method names."""
    mock_presigned_tool = MagicMock()
    mock_presigned_tool.name = "subagent__aws-cloud-securit_aws___get_presigned_url_d5d22cc1a56b"
    mock_presigned_tool.metadata = {
        "_opscloud_mcp": True,
        "_mcp_server": "subagent__aws-cloud-security-engineer_talkops-devops-plugi_ca4dbdbf__aws-mcp",
        "_mcp_base_server": "aws-mcp",
        "_mcp_original_name": "aws___get_presigned_url",
        "_mcp_subagent_owner": "aws-cloud-security-engineer",
    }

    mock_iam_tool = MagicMock()
    mock_iam_tool.name = "subagent__aws-cloud-security-engineer_ta_list_users_77000c74ca84"
    mock_iam_tool.metadata = {
        "_opscloud_mcp": True,
        "_mcp_server": "subagent__aws-cloud-security-engineer_talkops-devops-plugi_ca4dbdbf__iam",
        "_mcp_base_server": "iam",
        "_mcp_original_name": "list_users",
        "_mcp_subagent_owner": "aws-cloud-security-engineer",
    }

    # Style 1: Colon wildcard ("aws-mcp:*", "iam:*")
    mw1 = ToolFilterMiddleware(
        allowed_patterns=["aws-mcp:*", "iam:*"],
        subagent_name="aws-cloud-security-engineer",
        mcp_tools=[mock_presigned_tool, mock_iam_tool],
    )
    assert mw1.is_tool_allowed(mock_presigned_tool.name, tool_obj=mock_presigned_tool) is True
    assert mw1.is_tool_allowed(mock_iam_tool.name, tool_obj=mock_iam_tool) is True

    # Style 2: Bare server names ("aws-mcp", "iam")
    mw2 = ToolFilterMiddleware(
        allowed_patterns=["aws-mcp", "iam"],
        subagent_name="aws-cloud-security-engineer",
        mcp_tools=[mock_presigned_tool, mock_iam_tool],
    )
    assert mw2.is_tool_allowed(mock_presigned_tool.name, tool_obj=mock_presigned_tool) is True
    assert mw2.is_tool_allowed(mock_iam_tool.name, tool_obj=mock_iam_tool) is True

    # Style 3: Bare method names without prefix ("get_presigned_url", "list_users")
    mw3 = ToolFilterMiddleware(
        allowed_patterns=["get_presigned_url", "list_users"],
        subagent_name="aws-cloud-security-engineer",
        mcp_tools=[mock_presigned_tool, mock_iam_tool],
    )
    assert mw3.is_tool_allowed(mock_presigned_tool.name, tool_obj=mock_presigned_tool) is True
    assert mw3.is_tool_allowed(mock_iam_tool.name, tool_obj=mock_iam_tool) is True

    # Style 4: Wildcard glob on method names ("list_*")
    mw4 = ToolFilterMiddleware(
        allowed_patterns=["list_*"],
        subagent_name="aws-cloud-security-engineer",
        mcp_tools=[mock_presigned_tool, mock_iam_tool],
    )
    assert mw4.is_tool_allowed(mock_presigned_tool.name, tool_obj=mock_presigned_tool) is False
    assert mw4.is_tool_allowed(mock_iam_tool.name, tool_obj=mock_iam_tool) is True

    # Style 5: Wildcard "*"
    mw5 = ToolFilterMiddleware(
        allowed_patterns=["*"],
        subagent_name="aws-cloud-security-engineer",
        mcp_tools=[mock_presigned_tool, mock_iam_tool],
    )
    assert mw5.is_tool_allowed(mock_presigned_tool.name, tool_obj=mock_presigned_tool) is True
    assert mw5.is_tool_allowed(mock_iam_tool.name, tool_obj=mock_iam_tool) is True


def test_tool_filter_fail_loud_diagnostic_logging(caplog):
    """Verify ToolFilterMiddleware logs error when all MCP tools in request are pruned."""
    import logging
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    mock_presigned_tool = MagicMock()
    mock_presigned_tool.name = "subagent__aws-cloud-securit_aws___get_presigned_url_d5d22cc1a56b"
    mock_presigned_tool.metadata = {
        "_opscloud_mcp": True,
        "_mcp_server": "subagent__aws-cloud-security-engineer_talkops-devops-plugi_ca4dbdbf__aws-mcp",
        "_mcp_base_server": "aws-mcp",
        "_mcp_original_name": "aws___get_presigned_url",
    }

    # Only allow "read_file", effectively pruning the MCP tool
    mw = ToolFilterMiddleware(
        allowed_patterns=["read_file"],
        subagent_name="aws-cloud-security-engineer",
        mcp_tools=[mock_presigned_tool],
    )

    fake_llm = FakeListChatModel(responses=["ok"])
    req = ModelRequest(
        model=fake_llm,
        messages=[],
        tools=[mock_presigned_tool],
    )

    with caplog.at_level(logging.ERROR):
        mw.wrap_model_call(req, lambda r: fake_llm.invoke([]))

    assert any("pruned ALL 1 MCP tools from model call!" in rec.message for rec in caplog.records)

