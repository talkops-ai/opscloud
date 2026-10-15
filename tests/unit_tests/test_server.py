"""Unit tests for ServerProcess, ServerConfig, stream item parsing, and RemoteAgent connection."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opscloud.server import ServerConfig, ServerProcess, find_free_port, generate_langgraph_json
from opscloud.ui.remote_client import (
    RemoteAgent,
    _convert_ai_message,
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

    checkpointer_content = (tmp_path / "checkpointer.py").read_text(encoding="utf-8")
    assert "OpsCloudCheckpointer" in checkpointer_content


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
    mock_runs.get = AsyncMock(return_value={"status": "interrupted"})
    mock_client.runs = mock_runs
    mock_graph._validate_client.return_value = mock_client
    agent._graph = mock_graph
    agent._active_run_id = "run-active"

    config = {"configurable": {"thread_id": "thread-abc"}}
    res = await agent.acancel_active_runs(config)

    # Only the active run must be cancelled with wait=True and action='interrupt'
    mock_runs.cancel.assert_awaited_once_with(
        "thread-abc", "run-active", wait=True, action="interrupt"
    )
    assert res.success is True
    assert res.cancelled_run_ids == ("run-active",)
    assert res.terminal_statuses["run-active"] == "interrupted"
    assert agent._active_run_id is None


@pytest.mark.asyncio
async def test_remote_agent_acancel_active_runs_keeps_run_id_on_failure():
    """Verify active_run_id is retained if terminal status cannot be confirmed (DV-09)."""
    agent = RemoteAgent(url="http://127.0.0.1:9999", graph_name="agent")
    mock_graph = MagicMock()
    mock_client = MagicMock()
    mock_runs = AsyncMock()

    mock_runs.list = AsyncMock(return_value=[{"run_id": "run-active", "status": "running"}])
    mock_runs.cancel = AsyncMock(side_effect=TimeoutError("cancellation timeout"))
    mock_runs.get = AsyncMock(return_value={"status": "running"})
    mock_client.runs = mock_runs
    mock_graph._validate_client.return_value = mock_client
    agent._graph = mock_graph
    agent._active_run_id = "run-active"

    config = {"configurable": {"thread_id": "thread-abc"}}
    res = await agent.acancel_active_runs(config)

    assert res.success is False
    assert "run-active" in res.failed_run_ids
    # Active run ID MUST be preserved when terminal state is not confirmed!
    assert agent._active_run_id == "run-active"


def test_server_graph_import_performs_no_runtime_construction(monkeypatch):
    """LangGraph import must not instantiate models, MCP clients, or graphs."""
    import importlib
    import sys

    monkeypatch.setenv("OPSCLOUD_SERVER_PROCESS", "1")
    sys.modules.pop("opscloud.server.server_graph", None)
    with patch(
        "opscloud.model.factory.create_model",
        side_effect=AssertionError("runtime construction happened during import"),
    ) as create_model:
        importlib.import_module("opscloud.server.server_graph")

    create_model.assert_not_called()


@pytest.mark.asyncio
async def test_async_agent_factory_prepares_mcp_before_sync_compilation(tmp_path):
    """Server construction keeps MCP I/O async and compiles Deep Agents once in a worker."""
    from opscloud.agent.factory import create_opscloud_agent_async

    project_context = MagicMock(user_cwd=tmp_path, project_root=tmp_path)
    prepared = {"agent": ({}, [], [])}
    graph = MagicMock()
    backend = MagicMock()
    manager = MagicMock()
    manager.register_servers = MagicMock()

    with (
        patch("opscloud.agent.factory.list_subagents", return_value=[]),
        patch("opscloud.plugins.adapters.mcp.prepare_subagents_mcp_async", new_callable=AsyncMock, return_value=prepared) as prepare_subagents,
        patch("opscloud.mcp.discovery.discover_mcp_configs", return_value={"disabled": {"enabled": False}}),
        patch("opscloud.mcp.preload.preload_mcp_metadata", new_callable=AsyncMock, return_value=[]) as preload,
        patch("opscloud.mcp.preload.get_cached_mcp_server_infos", return_value=[]),
        patch("opscloud.mcp.session_manager.MCPSessionManager.get_instance", return_value=manager),
        patch("opscloud.mcp.session_manager.build_mcp_tools_from_server_infos", return_value=[]),
        patch("opscloud.agent.factory.create_opscloud_agent", return_value=(graph, backend)) as compile_agent,
    ):
        result = await create_opscloud_agent_async(
            model=MagicMock(),
            cwd=tmp_path,
            project_context=project_context,
        )

    assert result == (graph, backend)
    prepare_subagents.assert_awaited_once()
    preload.assert_awaited_once()
    assert compile_agent.call_args.kwargs["_prepared_subagent_mcp"] == prepared


def test_server_graph_factory_signature():
    """Verify that make_graph matches LangGraph API 0-parameter factory requirements."""
    from langgraph_api._factory_utils import _classify_factory
    from opscloud.server.server_graph import make_graph

    classification = _classify_factory(make_graph)
    assert classification == {}


def test_wait_for_graph_ready_single_request():
    """Verify wait_for_graph_ready sends a single request with full remaining timeout."""
    from unittest.mock import MagicMock, patch

    cfg = ServerConfig()
    proc = ServerProcess(cfg, port=8123)
    proc.process = MagicMock()
    proc.process.poll.return_value = None

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.get", return_value=mock_resp) as mock_get:
        proc.wait_for_graph_ready("agent", timeout=15.0)

        # Must be called exactly once
        assert mock_get.call_count == 1
        call_url, call_kwargs = mock_get.call_args[0][0], mock_get.call_args[1]
        assert call_url == f"{proc.url}/assistants/agent/graph"
        # Timeout must be approximately 15.0s, not 2.0s
        assert 14.0 <= call_kwargs["timeout"] <= 15.0


def test_wait_for_graph_ready_fallback():
    """Verify wait_for_graph_ready falls back from agent to opscloud on 404."""
    from unittest.mock import MagicMock, patch

    cfg = ServerConfig()
    proc = ServerProcess(cfg, port=8123)
    proc.process = MagicMock()
    proc.process.poll.return_value = None

    resp_404 = MagicMock(status_code=404)
    resp_200 = MagicMock(status_code=200)

    with patch("httpx.get", side_effect=[resp_404, resp_200]) as mock_get:
        proc.wait_for_graph_ready("agent", timeout=15.0)

        assert mock_get.call_count == 2
        assert mock_get.call_args_list[0][0][0] == f"{proc.url}/assistants/agent/graph"
        assert mock_get.call_args_list[1][0][0] == f"{proc.url}/assistants/opscloud/graph"


def test_server_prepare_launch_environment_sanitization(monkeypatch):
    """Verify dangerous carrier env vars are denied and langgraph flags configured."""
    monkeypatch.setenv("LD_PRELOAD", "/lib/malicious.so")
    monkeypatch.setenv("PYTHONEXECUTABLE", "/custom/bin/python")

    cfg = ServerConfig()
    proc = ServerProcess(cfg, port=8123)
    env, cmd = proc._prepare_launch()

    assert "LD_PRELOAD" not in env
    assert "PYTHONEXECUTABLE" not in env
    assert env["LANGGRAPH_AUTH_TYPE"] == "noop"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["LANGGRAPH_ALLOW_BLOCKING"] == "true"
    assert "--allow-blocking" in cmd
    assert "--server-log-level" in cmd


@pytest.mark.asyncio
async def test_server_process_astart_success():
    """Verify ServerProcess.astart() runs asynchronously and waits for health."""
    cfg = ServerConfig()
    proc = ServerProcess(cfg, port=8123)

    proc._prepare_launch = MagicMock(return_value=({}, ["dummy_cmd"]))
    proc._spawn_process = MagicMock()
    proc.await_until_healthy = AsyncMock()
    proc.await_for_graph_ready = AsyncMock()

    url = await proc.astart(timeout=10.0)
    assert url == proc.url
    proc.await_until_healthy.assert_awaited_once_with(timeout=10.0)
    proc.await_for_graph_ready.assert_awaited_once_with(timeout=10.0)


def test_server_process_multi_workspace_isolation(tmp_path: Path):
    """Verify concurrent instances in different workspaces get isolated runtime dirs and ports."""
    from opscloud._constants import SERVER_RUNTIME_DIR

    cfg = ServerConfig()
    proc1 = ServerProcess(cfg, port=8123)
    proc2 = ServerProcess(cfg, port=8124)

    assert proc1.port != proc2.port
    assert proc1.runtime_dir.name.startswith("inst_8123_")
    assert proc2.runtime_dir.name.startswith("inst_8124_")
    assert proc1.runtime_dir != proc2.runtime_dir
    assert proc1.runtime_dir.parent == SERVER_RUNTIME_DIR

    # Even two instances targeting the exact same port get isolated runtime dirs (IA-06)
    proc1_same = ServerProcess(cfg, port=8123)
    assert proc1_same.runtime_dir != proc1.runtime_dir
    assert proc1_same.runtime_dir.name.startswith("inst_8123_")

    # Custom runtime_dir override still respected and never deleted on stop (IA-09)
    custom_dir = tmp_path / "custom_server"
    custom_dir.mkdir(parents=True, exist_ok=True)
    proc_custom = ServerProcess(cfg, port=8125, runtime_dir=custom_dir)
    assert proc_custom.runtime_dir == custom_dir
    assert proc_custom._owns_runtime_dir is False
    assert proc1._owns_runtime_dir is True

    # Stopping proc_custom must NEVER delete the caller-owned directory
    proc_custom.stop()
    assert custom_dir.is_dir()


@pytest.mark.asyncio
async def test_server_process_astart_cancellation_calls_stop():
    """Verify cancellation during astart() triggers stop() and cleans up the process (DV-04)."""
    cfg = ServerConfig()
    proc = ServerProcess(cfg, port=8123)

    proc._prepare_launch = MagicMock(return_value=({}, ["dummy_cmd"]))
    proc._spawn_process = MagicMock()
    proc.await_until_healthy = AsyncMock(side_effect=asyncio.CancelledError())
    proc.stop = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await proc.astart(timeout=5.0)

    proc.stop.assert_called_once()


def test_validate_server_ownership_never_signals_ambiguous_pids(tmp_path: Path):
    """Verify _validate_server_ownership requires matching server.json and rejects ambiguous PIDs (DV-03)."""
    from opscloud.server.server import _validate_server_ownership
    import json
    import os

    # 1. No server.json exists -> MUST return False even if PID is alive
    current_pid = os.getpid()
    empty_dir = tmp_path / "empty_runtime"
    empty_dir.mkdir()
    assert _validate_server_ownership(empty_dir, current_pid) is False

    # 2. server.json exists but PID differs -> False
    mismatch_dir = tmp_path / "mismatch_runtime"
    mismatch_dir.mkdir()
    (mismatch_dir / "server.json").write_text(json.dumps({"pid": current_pid + 99999, "runtime_dir": str(mismatch_dir)}))
    assert _validate_server_ownership(mismatch_dir, current_pid) is False

    # 3. server.json exists with PID matching but runtime_dir differs -> False
    other_dir = tmp_path / "other_runtime"
    other_dir.mkdir()
    bad_dir_runtime = tmp_path / "bad_dir_runtime"
    bad_dir_runtime.mkdir()
    (bad_dir_runtime / "server.json").write_text(json.dumps({"pid": current_pid, "runtime_dir": str(other_dir)}))
    assert _validate_server_ownership(bad_dir_runtime, current_pid) is False


def test_server_process_stop_escalates_to_killpg_when_descendant_ignores_sigterm():
    """Verify _stop_process verifies process-group disappearance and escalates to SIGKILL for descendants ignoring SIGTERM (DV-06)."""
    import os
    import signal
    import subprocess
    import sys
    import time

    if os.name == "nt":
        pytest.skip("Process groups via killpg are POSIX-only")

    cfg = ServerConfig()
    proc = ServerProcess(cfg, port=8199)

    # Spawn a process group leader that launches a child descendant ignoring SIGTERM
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", script], start_new_session=True, text=True)
    pgid = os.getpgid(p.pid)
    proc.process = p
    proc._pgid = pgid

    # Give child descendant time to initialize and register SIG_IGN
    time.sleep(0.3)
    assert os.killpg(pgid, 0) is None  # Group is alive

    # Stop should signal SIGTERM, detect group alive, escalate to SIGKILL, and wait for disappearance
    proc.stop()

    # The entire process group MUST now be dead
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_concurrent_same_port_and_free_port_toctou_resilience():
    """Verify concurrent same-port instances receive isolated runtime directories and free-port selection works (DV-11)."""
    import socket
    from opscloud.server import ServerConfig, ServerProcess, find_free_port

    # 1. find_free_port returns open port
    p1 = find_free_port()
    assert isinstance(p1, int) and p1 > 1024

    # 2. Hold a port bound to simulate race/occupation
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        occupied_port = s.getsockname()[1]
        p2 = find_free_port()
        # Free port returned must not be the occupied port
        assert p2 != occupied_port

    # 3. Concurrent same-port instances receive strictly isolated directories
    cfg = ServerConfig()
    procs = [ServerProcess(cfg, port=8999) for _ in range(5)]
    runtime_dirs = {p.runtime_dir for p in procs}
    instance_ids = {p.instance_id for p in procs}

    # All 5 instances have isolated UUID paths and will never collide on server.pid
    assert len(runtime_dirs) == 5
    assert len(instance_ids) == 5
    for p in procs:
        assert str(p.port) in str(p.runtime_dir)
        p.stop()


@pytest.mark.asyncio
async def test_in_process_server_handle_and_agent_lifecycle(tmp_path: Path):
    """Verify start_server_and_get_agent uses InProcessServerHandle by default without spawning a subprocess."""
    from langchain_core.messages import AIMessageChunk
    from opscloud.cli.server_manager import InProcessServerHandle, start_server_and_get_agent
    from opscloud.server.server_graph import ServerRuntime
    from opscloud.ui.remote_client import InProcessAgent

    fake_graph = MagicMock()

    async def _fake_astream(input_data, **kwargs):
        del input_data, kwargs
        yield ((), "messages", (AIMessageChunk(content="Hello in-process!"), {"run_id": "run-inproc-1"}))

    fake_graph.astream = _fake_astream
    fake_graph.aget_state = AsyncMock(return_value=MagicMock(values={"messages": []}))
    fake_graph.aupdate_state = AsyncMock(return_value=None)

    fake_runtime = ServerRuntime(agent=fake_graph, backend=MagicMock())

    with (
        patch(
            "opscloud.server.server_graph._build_default_runtime_async",
            new_callable=AsyncMock,
            return_value=fake_runtime,
        ) as mock_build_runtime,
        patch(
            "opscloud.state.session.get_db_path",
            return_value=tmp_path / "sessions.db",
        ),
    ):
        client, handle = await start_server_and_get_agent(cwd=tmp_path)

        assert isinstance(handle, InProcessServerHandle)
        assert handle.is_in_process is True
        assert handle.process is None
        assert isinstance(client, InProcessAgent)
        assert client.is_in_process is True
        mock_build_runtime.assert_awaited_once()

        chunks = []
        async for ns, mode, data in client.astream(
            {"messages": [{"role": "user", "content": "hi"}]},
            config={
                "configurable": {
                    "thread_id": "11111111-2222-3333-4444-555555555555",
                    "approval_mode": "auto",
                }
            },
            context={
                "working_dir": str(tmp_path),
                "cwd": str(tmp_path),
                "workspace": {"cwd": str(tmp_path)},
                "interactive": True,
                "custom_field": "ok",
            },
        ):
            chunks.append((ns, mode, data))

        assert len(chunks) == 1
        assert chunks[0][1] == "messages"
        assert chunks[0][2][0].content == "Hello in-process!"

        # Also verify LangGraph's _coerce_context directly with CLIContextSchema and dict containing working_dir
        from langgraph.pregel.main import _coerce_context
        from langgraph.types import Interrupt
        from opscloud.agent.config import CLIContextSchema
        from opscloud.ui.remote_client import _convert_interrupts
        from opscloud.ui.textual_adapter import TextualAdapter

        coerced = _coerce_context(
            CLIContextSchema,
            {"working_dir": "/tmp/work", "interactive": True, "unknown_key": 123},
        )
        assert isinstance(coerced, CLIContextSchema)
        assert coerced.cwd == "/tmp/work"
        assert coerced.working_dir == "/tmp/work"
        assert coerced.get("unknown_key") == 123

        # Verify in-process Pregel tuple of Interrupts is flattened so HITL prompts are never skipped
        tuple_interrupts = (
            Interrupt(
                value={"action_requests": [{"name": "execute", "args": {"command": "date -u"}}]},
                id="int-tuple-1",
            ),
        )
        converted_ints = _convert_interrupts(tuple_interrupts)
        assert len(converted_ints) == 1
        assert isinstance(converted_ints[0], Interrupt)
        assert converted_ints[0].id == "int-tuple-1"

        from typing import Any

        adapter = TextualAdapter.__new__(TextualAdapter)
        pending: dict[str, Any] = {}
        adapter._handle_updates_stream_event({"__interrupt__": converted_ints}, pending)
        assert "int-tuple-1" in pending
        assert pending["int-tuple-1"]["action_requests"][0]["name"] == "execute"

        await handle.astop()

