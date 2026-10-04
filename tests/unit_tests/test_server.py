"""Unit tests for ServerProcess, ServerConfig, stream item parsing, and RemoteAgent connection."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opscloud.server import ServerConfig, ServerProcess, find_free_port, generate_langgraph_json
from opscloud.ui.remote_client import (
    RemoteAgent,
    _convert_ai_message,
    _convert_interrupts,
    _convert_tool_message,
    _parse_stream_item,
)


def test_server_config_environment_roundtrip():
    cfg = ServerConfig(
        model="anthropic:claude-3-5-sonnet-latest",
        assistant_id="opscloud",
        system_prompt="Test prompt",
        auto_approve=True,
        interactive=False,
        enable_shell=True,
        cwd="/tmp/workspace",
        approval_mode="smart",
    )
    env = cfg.to_env()
    assert env["OPSCLOUD_SERVER_MODEL"] == "anthropic:claude-3-5-sonnet-latest"
    assert env["OPSCLOUD_SERVER_AUTO_APPROVE"] == "true"
    assert env["OPSCLOUD_SERVER_INTERACTIVE"] == "false"
    assert env["OPSCLOUD_SERVER_APPROVAL_MODE"] == "smart"
    assert env["OPSCLOUD_SERVER_CWD"] == "/tmp/workspace"
    assert env["OPSCLOUD_SERVER_LOG_LEVEL"] == "WARNING"
    assert cfg.server_log_level == "WARNING"

    # Custom log level
    custom_cfg = ServerConfig(server_log_level="DEBUG")
    assert custom_cfg.to_env()["OPSCLOUD_SERVER_LOG_LEVEL"] == "DEBUG"


def test_generate_langgraph_json_scaffolding(tmp_path: Path):
    target = generate_langgraph_json(tmp_path)
    assert target.exists()
    assert target.name == "langgraph.json"

    # Verify checkpointer script and server_graph are scaffolded
    assert (tmp_path / "server_graph.py").exists()
    assert (tmp_path / "checkpointer.py").exists()

    content = target.read_text(encoding="utf-8")
    assert "checkpointer.py:create_checkpointer" in content
    assert "server_graph.py:make_graph" in content


def test_find_free_port():
    port = find_free_port()
    assert isinstance(port, int)
    assert port > 1024


def test_server_process_initialization():
    from opscloud.utils.logger import get_active_log_file

    cfg = ServerConfig()
    proc = ServerProcess(cfg, port=0)
    assert proc.port > 0
    assert proc.url == f"http://127.0.0.1:{proc.port}"
    assert proc.log_file == get_active_log_file()


def test_parse_stream_item_conformance():
    # 1. 3-tuple with namespace and data: (namespace, mode, data)
    chunk3 = (("subagent",), "messages", ({"type": "ai", "content": "hi"}, {}))
    parsed3 = _parse_stream_item(chunk3)
    assert parsed3 == (("subagent",), "messages", ({"type": "ai", "content": "hi"}, {}))

    # 2. 3-tuple with empty namespace
    chunk_root = ((), "updates", {"agent": {"messages": []}})
    parsed_root = _parse_stream_item(chunk_root)
    assert parsed_root == ((), "updates", {"agent": {"messages": []}})

    # 3. 2-tuple: (mode, data)
    chunk2 = ("custom", {"event": "rubric_start"})
    parsed2 = _parse_stream_item(chunk2)
    assert parsed2 == ((), "custom", {"event": "rubric_start"})

    # 4. Invalid item
    assert _parse_stream_item("invalid") is None
    assert _parse_stream_item((1,)) is None


def test_convert_ai_message_and_tools():
    # Streaming chunks with tool_call_chunks
    ai_dict = {
        "type": "ai",
        "content": "Analyzing cluster...",
        "id": "msg-123",
        "tool_call_chunks": [{"name": "run_command", "args": '{"cmd": "az account show"}', "id": "call-1", "index": 0}],
    }
    chunk = _convert_ai_message(ai_dict)
    assert chunk is not None
    assert chunk.content == "Analyzing cluster..."
    assert len(chunk.tool_call_chunks) == 1
    assert chunk.tool_call_chunks[0]["name"] == "run_command"

    # ToolMessage conversion
    tool_dict = {
        "type": "tool",
        "name": "run_command",
        "content": "Subscription: Production",
        "tool_call_id": "call-1",
        "id": "tool-1",
        "status": "success",
    }
    tool_msg = _convert_tool_message(tool_dict)
    assert tool_msg is not None
    assert tool_msg.name == "run_command"
    assert tool_msg.content == "Subscription: Production"
    assert tool_msg.tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_remote_agent_acancel_active_runs():
    agent = RemoteAgent(url="http://127.0.0.1:9999", graph_name="agent")
    mock_graph = MagicMock()
    mock_client = MagicMock()
    mock_runs = AsyncMock()

    # Return one active and one finished run
    mock_runs.list = AsyncMock(return_value=[
        {"run_id": "run-active", "status": "running"},
        {"run_id": "run-done", "status": "success"},
    ])
    mock_runs.cancel = AsyncMock(return_value=None)
    mock_client.runs = mock_runs
    mock_graph._validate_client.return_value = mock_client
    agent._graph = mock_graph

    config = {"configurable": {"thread_id": "thread-abc"}}
    await agent.acancel_active_runs(config)

    # Only the active run must be cancelled with wait=True and action='interrupt'
    mock_runs.cancel.assert_awaited_once_with(
        "thread-abc", "run-active", wait=True, action="interrupt"
    )


def test_server_graph_factory_signature():
    """Verify that make_graph matches LangGraph API 0-parameter factory requirements."""
    from langgraph_api._factory_utils import _classify_factory
    from opscloud.server.server_graph import make_graph

    classification = _classify_factory(make_graph)
    assert classification == {}
