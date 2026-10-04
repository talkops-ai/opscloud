"""Unit tests for MCPToolMiddleware argument normalization and authentication error handling."""

from unittest.mock import MagicMock
import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from opscloud.middleware.mcp_middleware import (
    MCPToolMiddleware,
    normalize_mcp_arguments,
)


def test_normalize_mcp_arguments_drops_optional_empty_strings():
    schema = {
        "required": ["cluster_name"],
        "properties": {
            "cluster_name": {"type": "string"},
            "namespace": {"type": "string"},
            "region": {"type": "string"},
            "count": {"type": "integer"},
        },
    }
    raw_args = {
        "cluster_name": "prod-eks",
        "namespace": "",       # optional empty string -> should be dropped
        "region": "us-west-2",
        "count": 0,            # 0 should NOT be dropped
    }

    cleaned = normalize_mcp_arguments(raw_args, schema)
    assert cleaned == {
        "cluster_name": "prod-eks",
        "region": "us-west-2",
        "count": 0,
    }
    assert "namespace" not in cleaned


def test_normalize_mcp_arguments_preserves_required_empty_strings():
    schema = {
        "required": ["mandatory_field"],
        "properties": {
            "mandatory_field": {"type": "string"},
            "optional_field": {"type": "string"},
        },
    }
    raw_args = {
        "mandatory_field": "",
        "optional_field": "",
    }

    cleaned = normalize_mcp_arguments(raw_args, schema)
    assert cleaned == {"mandatory_field": ""}


def test_normalize_mcp_arguments_non_dict_passthrough():
    assert normalize_mcp_arguments("not-a-dict") == "not-a-dict"
    assert normalize_mcp_arguments([1, 2, 3]) == [1, 2, 3]


def test_is_mcp_call_detection():
    mw = MCPToolMiddleware()

    req_colon = MagicMock(spec=ToolCallRequest)
    req_colon.tool_call = {"name": "aws:list_s3_buckets", "args": {}}
    assert mw._is_mcp_call(req_colon) is True

    req_mcp_prefix = MagicMock(spec=ToolCallRequest)
    req_mcp_prefix.tool_call = {"name": "mcp__github_list_prs", "args": {}}
    assert mw._is_mcp_call(req_mcp_prefix) is True

    req_native = MagicMock(spec=ToolCallRequest)
    req_native.tool_call = {"name": "execute_command", "args": {}}
    assert mw._is_mcp_call(req_native) is False


def test_wrap_tool_call_normalizes_mcp_arguments():
    mw = MCPToolMiddleware()

    tool_mock = MagicMock()
    args_schema_mock = MagicMock()
    args_schema_mock.schema.return_value = {
        "required": ["repo"],
        "properties": {
            "repo": {"type": "string"},
            "branch": {"type": "string"},
        },
    }
    tool_mock.args_schema = args_schema_mock

    request = ToolCallRequest(
        tool=tool_mock,
        tool_call={"name": "git:checkout", "args": {"repo": "opscloud", "branch": ""}, "id": "call_123"},
        state={},
        runtime=None,
    )

    def dummy_handler(req):
        return req.tool_call["args"]

    result = mw.wrap_tool_call(request, dummy_handler)
    assert result == {"repo": "opscloud"}


def test_wrap_tool_call_handles_auth_error():
    mw = MCPToolMiddleware()

    tool_mock = MagicMock()
    request = ToolCallRequest(
        tool=tool_mock,
        tool_call={"name": "aws:describe_instances", "args": {}, "id": "call_auth_fail"},
        state={},
        runtime=None,
    )

    def failing_handler(req):
        raise RuntimeError("AWS STS Error: The security token included in the request is expired")

    res = mw.wrap_tool_call(request, failing_handler)
    assert isinstance(res, ToolMessage)
    assert res.status == "error"
    assert res.tool_call_id == "call_auth_fail"
    assert "authentication" in res.content
    assert "expired" in res.content


def test_wrap_tool_call_reraises_non_auth_error():
    mw = MCPToolMiddleware()

    tool_mock = MagicMock()
    request = ToolCallRequest(
        tool=tool_mock,
        tool_call={"name": "aws:describe_instances", "args": {}, "id": "call_fail"},
        state={},
        runtime=None,
    )

    def failing_handler(req):
        raise KeyError("Invalid key lookup")

    with pytest.raises(KeyError):
        mw.wrap_tool_call(request, failing_handler)


@pytest.mark.asyncio
async def test_awrap_tool_call_handles_auth_error():
    mw = MCPToolMiddleware()

    tool_mock = MagicMock()
    request = ToolCallRequest(
        tool=tool_mock,
        tool_call={"name": "k8s:get_pods", "args": {}, "id": "call_k8s_auth"},
        state={},
        runtime=None,
    )

    async def async_failing_handler(req):
        raise PermissionError("Access denied: unauthorized token for cluster api")

    res = await mw.awrap_tool_call(request, async_failing_handler)
    assert isinstance(res, ToolMessage)
    assert res.status == "error"
    assert res.tool_call_id == "call_k8s_auth"
    assert "authentication" in res.content
