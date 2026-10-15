"""Unit tests for the enhanced ``/agent`` command, autocomplete, and AgentSessionMiddleware."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphInterrupt

from opscloud.commands._base import CommandContext
from opscloud.commands.power.agents import (
    AgentInvokeHandler,
    AgentSessionState,
    AgentsHandler,
    activate_agent_session,
    clear_agent_session,
    get_agent_session_state,
    resolve_plugin_agent,
    sync_agent_session_status_label,
)
from opscloud.middleware.agent_session import (
    AGENT_SESSION_NODE_PREFIX,
    AgentSessionMiddleware,
)
from opscloud.state.agent_session_channels import (
    is_agent_session_turn,
)
from opscloud.ui.command_registry import (
    build_agent_commands,
    get_command,
)
from opscloud.ui.textual_adapter import _is_agent_session_namespace


# ── Fakes & Helpers ──────────────────────────────────────────────────────────


class FakeSubAgent:
    """Minimal runnable stand-in for compiled plugin subagent."""

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def invoke(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(payload)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def ainvoke(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.invoke(payload, **kwargs)


def _runtime() -> Any:
    events: list[dict[str, Any]] = []
    return SimpleNamespace(context=None, stream_writer=events.append, events=events)


def _sample_plugin_agents() -> list[dict[str, Any]]:
    return [
        {
            "name": "aws-compute@talkops-devops-plugins",
            "plugin_id": "talkops-devops-plugins",
            "description": "Provisions and operates Amazon EC2 virtual machines.",
            "tools": ["execute", "edit_file", "aws_ec2:reboot_instances"],
            "skills": ["aws-compute:*"],
            "source": "plugin:talkops-devops-plugins",
        },
        {
            "name": "k8s-operator@talkops-devops-plugins",
            "plugin_id": "talkops-devops-plugins",
            "description": "Kubernetes cluster administration and troubleshooting.",
            "tools": ["execute", "k8s:get_pods"],
            "skills": ["k8s-operator:*"],
            "source": "plugin:talkops-devops-plugins",
        },
    ]


# ── 1. Plugin Agent Resolution ───────────────────────────────────────────────


def test_resolve_plugin_agent_exact_and_short_names() -> None:
    agents = cast("Any", _sample_plugin_agents())

    # Exact full name
    match = resolve_plugin_agent("aws-compute@talkops-devops-plugins", agents)
    assert match is not None
    assert match["name"] == "aws-compute@talkops-devops-plugins"

    # Short name before '@'
    match = resolve_plugin_agent("aws-compute", agents)
    assert match is not None
    assert match["name"] == "aws-compute@talkops-devops-plugins"

    # Case-insensitive
    match = resolve_plugin_agent("AWS-COMPUTE", agents)
    assert match is not None
    assert match["name"] == "aws-compute@talkops-devops-plugins"

    # Second agent
    match = resolve_plugin_agent("k8s-operator", agents)
    assert match is not None
    assert match["name"] == "k8s-operator@talkops-devops-plugins"

    # Unknown agent
    assert resolve_plugin_agent("nonexistent-agent", agents) is None
    assert resolve_plugin_agent("", agents) is None


# ── 2. Autocomplete Commands Generation ──────────────────────────────────────


def test_build_agent_commands_matches_workflow_pattern() -> None:
    agents = _sample_plugin_agents()
    entries = build_agent_commands(agents)

    assert len(entries) == 2
    e0 = entries[0]
    assert e0.name == "/agent:aws-compute@talkops-devops-plugins"
    assert e0.display_name == "/agent:aws-compute"
    assert "(agent · talkops-devops-plugins)" in e0.description
    assert "Provisions and operates" in e0.description
    assert e0.argument_hint == "[prompt]"
    assert "aws-compute" in e0.hidden_keywords

    e1 = entries[1]
    assert e1.name == "/agent:k8s-operator@talkops-devops-plugins"
    assert e1.display_name == "/agent:k8s-operator"


# ── 3. Command Descriptors & Resolution ──────────────────────────────────────


def test_command_descriptor_and_prefix_resolution() -> None:
    # Top-level command
    cmd = get_command("/agent")
    assert cmd is not None
    assert cmd.name == "/agent"
    assert "/agents" in cmd.aliases

    # Prefix resolution for /agent:<name>
    prefix_cmd = get_command("/agent:aws-compute")
    assert prefix_cmd is not None
    assert prefix_cmd.name == "/agent:"

    # Prefix resolution with alias /agents:<name>
    prefix_alias = get_command("/agents:k8s-operator")
    assert prefix_alias is not None
    assert prefix_alias.name == "/agent:"


# ── 4. AgentsHandler Execution ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_list_text() -> None:
    handler = AgentsHandler()
    mock_app = MagicMock()
    mock_app.get_discovered_plugin_agents.return_value = _sample_plugin_agents()

    ctx = CommandContext(app=mock_app, raw_command="/agent", args="")
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.message is not None
    assert "**Available Plugin Agents**" in result.message
    assert "aws-compute" in result.message
    assert "k8s-operator" in result.message
    assert "/agent:<name> [prompt]" in result.message


@pytest.mark.asyncio
async def test_agent_status_when_inactive() -> None:
    handler = AgentsHandler()
    mock_app = MagicMock()
    mock_app.get_discovered_plugin_agents.return_value = _sample_plugin_agents()

    ctx = CommandContext(app=mock_app, raw_command="/agent status", args="status")
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.message is not None
    assert "No active plugin agent session" in result.message


@pytest.mark.asyncio
async def test_agent_activation_and_status() -> None:
    handler = AgentsHandler()
    mock_app = MagicMock()
    mock_app._run_agent_session_request = AsyncMock()
    mock_app.get_discovered_plugin_agents.return_value = _sample_plugin_agents()

    # Activate aws-compute
    ctx = CommandContext(app=mock_app, raw_command="/agent aws-compute", args="aws-compute", thread_id="t1")
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.message is not None
    assert "Activated agent persona **aws-compute**" in result.message
    assert mock_app._run_agent_session_request.called
    req = mock_app._run_agent_session_request.call_args[0][0]
    assert req["kind"] == "start"
    assert req["agent_name"] == "aws-compute@talkops-devops-plugins"

    # Status now shows active agent
    status_ctx = CommandContext(app=mock_app, raw_command="/agent status", args="status")
    status_result = await handler.execute(status_ctx)
    assert status_result.message is not None
    assert "**Active Agent Persona:** `aws-compute`" in status_result.message


@pytest.mark.asyncio
async def test_agent_activation_with_initial_prompt() -> None:
    handler = AgentsHandler()
    mock_app = MagicMock()
    mock_app._run_agent_session_request = AsyncMock()
    mock_app.get_discovered_plugin_agents.return_value = _sample_plugin_agents()

    ctx = CommandContext(
        app=mock_app,
        raw_command="/agent aws-compute check running instances",
        args="aws-compute check running instances",
        thread_id="t1",
    )
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.message is not None
    assert "Activated agent persona **aws-compute**" in result.message
    assert "check running instances" in result.message
    req = mock_app._run_agent_session_request.call_args[0][0]
    assert req["prompt"] == "check running instances"


@pytest.mark.asyncio
async def test_agent_clear_and_detach() -> None:
    handler = AgentsHandler()
    mock_app = MagicMock()
    mock_app._run_agent_session_request = AsyncMock()
    mock_app.get_discovered_plugin_agents.return_value = _sample_plugin_agents()

    # First activate
    await handler.execute(CommandContext(app=mock_app, raw_command="/agent aws-compute", args="aws-compute"))

    # Detach
    detach_res = await handler.execute(CommandContext(app=mock_app, raw_command="/agent detach", args="detach"))
    assert detach_res.success is True
    assert detach_res.message is not None
    assert "Detached from `aws-compute`" in detach_res.message
    state = get_agent_session_state(mock_app)
    assert state.is_active is False

    # Attach
    attach_res = await handler.execute(CommandContext(app=mock_app, raw_command="/agent attach", args="attach"))
    assert attach_res.success is True
    assert attach_res.message is not None
    assert "Attached to `aws-compute`" in attach_res.message
    assert state.is_active is True

    # Clear
    clear_res = await handler.execute(CommandContext(app=mock_app, raw_command="/agent clear", args="clear"))
    assert clear_res.success is True
    assert clear_res.message is not None
    assert "Detached from agent **aws-compute**" in clear_res.message
    assert state.agent_id is None
    clear_req = mock_app._run_agent_session_request.call_args[0][0]
    assert clear_req["kind"] == "clear"


@pytest.mark.asyncio
async def test_agent_invoke_handler_prefix_syntax() -> None:
    invoke_handler = AgentInvokeHandler()
    assert invoke_handler.name == "/agent:"
    assert "/agents:" in invoke_handler.aliases

    mock_app = MagicMock()
    mock_app._run_agent_session_request = AsyncMock()
    mock_app.get_discovered_plugin_agents.return_value = _sample_plugin_agents()

    # /agent:k8s-operator get pods in default
    ctx = CommandContext(
        app=mock_app,
        raw_command="/agent:k8s-operator get pods in default",
        args="get pods in default",
        thread_id="t2",
    )
    result = await invoke_handler.execute(ctx)

    assert result.success is True
    assert result.message is not None
    assert "Activated agent persona **k8s-operator**" in result.message
    req = mock_app._run_agent_session_request.call_args[0][0]
    assert req["agent_name"] == "k8s-operator@talkops-devops-plugins"
    assert req["prompt"] == "get pods in default"


# ── 5. AgentSessionMiddleware Execution ──────────────────────────────────────


def test_agent_session_middleware_passthrough_when_no_request() -> None:
    subagent = FakeSubAgent({})
    mw = AgentSessionMiddleware({"test-agent": subagent})

    # Normal turn: no-op
    assert mw.before_agent(cast(Any, {"messages": []}), _runtime()) is None

    # Previous turn cleanup
    assert mw.before_agent(cast(Any, {"messages": [], "_agent_session_turn": True}), _runtime()) == {
        "_agent_session_turn": False
    }


def test_agent_session_middleware_start_without_prompt() -> None:
    subagent = FakeSubAgent({})
    mw = AgentSessionMiddleware({"test-agent": subagent})

    state = {
        "messages": [HumanMessage(content="main chat")],
        "agent_session_request": {"kind": "start", "agent_name": "test-agent"},
    }
    update = mw.before_agent(cast(Any, state), _runtime())

    assert update is not None
    assert update["jump_to"] == "end"
    assert update["_agent_session_turn"] is True
    assert update["_active_agent_id"] == "test-agent"
    assert update["_agent_session_messages"]["reset"] is True
    assert "messages" not in update  # Main transcript untouched
    assert len(subagent.calls) == 0  # No LLM call needed for activation only


def test_agent_session_middleware_start_with_prompt() -> None:
    fake_reply = {"messages": [HumanMessage(content="run audit"), AIMessage(content="audit complete")]}
    subagent = FakeSubAgent(fake_reply)
    mw = AgentSessionMiddleware({"test-agent": subagent})

    state = {
        "messages": [HumanMessage(content="main chat")],
        "agent_session_request": {"kind": "start", "agent_name": "test-agent", "prompt": "run audit"},
    }
    update = mw.before_agent(cast(Any, state), _runtime())

    assert update is not None
    assert update["jump_to"] == "end"
    assert update["_agent_session_turn"] is True
    assert update["_active_agent_id"] == "test-agent"
    assert len(subagent.calls) == 1
    assert subagent.calls[0]["messages"][0].content == "run audit"
    assert [m.content for m in update["_agent_session_messages"]["messages"]] == [
        "run audit",
        "audit complete",
    ]


def test_agent_session_middleware_continue_maintains_isolated_history() -> None:
    fake_reply = {
        "messages": [
            HumanMessage(content="q1"),
            AIMessage(content="a1"),
            HumanMessage(content="q2"),
            AIMessage(content="a2"),
        ]
    }
    subagent = FakeSubAgent(fake_reply)
    mw = AgentSessionMiddleware({"test-agent": subagent})

    prior_history = [HumanMessage(content="q1"), AIMessage(content="a1")]
    state = {
        "messages": [HumanMessage(content="main deep agent prompt")],
        "_active_agent_id": "test-agent",
        "_agent_session_messages": prior_history,
        "agent_session_request": {"kind": "continue", "agent_name": "test-agent", "message": "q2"},
    }
    update = mw.before_agent(cast(Any, state), _runtime())

    assert update is not None
    assert update["jump_to"] == "end"
    assert update["_agent_session_turn"] is True
    assert len(subagent.calls) == 1
    sent_msgs = subagent.calls[0]["messages"]
    assert [m.content for m in sent_msgs] == ["q1", "a1", "q2"]
    assert [m.content for m in update["_agent_session_messages"]] == ["q1", "a1", "q2", "a2"]


def test_agent_session_middleware_clear() -> None:
    subagent = FakeSubAgent({})
    mw = AgentSessionMiddleware({"test-agent": subagent})

    state = {
        "_active_agent_id": "test-agent",
        "_agent_session_messages": [HumanMessage(content="test")],
        "agent_session_request": {"kind": "clear"},
    }
    update = mw.before_agent(cast(Any, state), _runtime())

    assert update is not None
    assert update["_active_agent_id"] is None
    assert update["_agent_session_messages"]["reset"] is True
    assert update["jump_to"] == "end"


def test_agent_session_middleware_bubbles_up_interrupt() -> None:
    subagent = FakeSubAgent(GraphInterrupt(()))
    mw = AgentSessionMiddleware({"test-agent": subagent})

    state = {
        "agent_session_request": {"kind": "start", "agent_name": "test-agent", "prompt": "delete cluster"},
    }
    with pytest.raises(GraphInterrupt):
        mw.before_agent(cast(Any, state), _runtime())


@pytest.mark.asyncio
async def test_agent_session_middleware_async_execution() -> None:
    fake_reply = {"messages": [HumanMessage(content="hello"), AIMessage(content="world")]}
    subagent = FakeSubAgent(fake_reply)
    mw = AgentSessionMiddleware({"test-agent": subagent})

    state = {
        "agent_session_request": {"kind": "start", "agent_name": "test-agent", "prompt": "hello"},
    }
    update = await mw.abefore_agent(cast(Any, state), _runtime())
    assert update is not None
    assert update["_agent_session_turn"] is True
    assert update["_active_agent_id"] == "test-agent"
    assert [m.content for m in update["_agent_session_messages"]["messages"]] == ["hello", "world"]


# ── 6. State Turn Helper & Adapter Namespace Detection ───────────────────────


def test_is_agent_session_turn() -> None:
    assert is_agent_session_turn({"_agent_session_turn": True})
    assert is_agent_session_turn({"agent_session_request": {"kind": "start"}})
    assert not is_agent_session_turn({"messages": []})
    assert not is_agent_session_turn(None)


def test_is_agent_session_namespace() -> None:
    assert _is_agent_session_namespace((f"{AGENT_SESSION_NODE_PREFIX}aws-compute",))
    assert not _is_agent_session_namespace(("GoalCriteriaMiddleware.goal_criteria",))
    assert not _is_agent_session_namespace(())
    assert not _is_agent_session_namespace(("deeper", "node"))


# ── 7. Autocomplete Scoring, Prioritization, and Space Subcommands ───────────


def test_agent_autocomplete_prioritization_and_space() -> None:
    from opscloud.ui.command_registry import get_all_entries
    from opscloud.ui.widgets.autocomplete import SlashCommandController

    agents = _sample_plugin_agents()
    entries = build_agent_commands(agents)
    all_cmds = get_all_entries(entries)

    view = MagicMock()
    ctrl = SlashCommandController(all_cmds, view)

    # 1. Typing /ag prioritizes plugin agents before aliases
    ctrl.on_text_changed("/ag", 3)
    assert len(ctrl._suggestions) >= 2
    top_names = [name for name, _ in ctrl._suggestions[:3]]
    assert "/agent:aws-compute" in top_names
    assert "/agent:k8s-operator" in top_names

    # 2. Typing /agent shows /agent then plugin agents
    ctrl.on_text_changed("/agent", 6)
    assert ctrl._suggestions[0][0] == "/agent"
    agent_names = [name for name, _ in ctrl._suggestions[1:3]]
    assert "/agent:aws-compute" in agent_names
    assert "/agent:k8s-operator" in agent_names

    # 3. Typing /agent  (with space) shows all plugin agents
    ctrl.on_text_changed("/agent ", 7)
    assert len(ctrl._suggestions) == 2
    assert ctrl._suggestions[0][0] == "/agent:aws-compute"
    assert ctrl._suggestions[1][0] == "/agent:k8s-operator"

    # 4. Typing /agent aws (filtering by space)
    ctrl.on_text_changed("/agent aws", 10)
    assert len(ctrl._suggestions) == 1
    assert ctrl._suggestions[0][0] == "/agent:aws-compute"

    # 5. Selected completion applies clean short display_name
    ctrl._selected_index = 0
    ctrl._apply_selected_completion(10)
    view.replace_completion_range.assert_called_with(0, 10, "/agent:aws-compute")


def test_agent_session_middleware_excludes_heavy_channels_and_resets_on_agent_switch() -> None:
    fake_reply = {"messages": [HumanMessage(content="new prompt"), AIMessage(content="new reply")]}
    subagent_b = FakeSubAgent(fake_reply)
    mw = AgentSessionMiddleware({"agent-a": FakeSubAgent({}), "agent-b": subagent_b})

    state = {
        "messages": [HumanMessage(content="main prompt")],
        "_active_agent_id": "agent-a",
        "_agent_session_messages": [HumanMessage(content="old a1"), AIMessage(content="old a2")],
        "skills_metadata": [{"name": "skill-1", "content": "x" * 1000}],
        "_goal_objective": "some goal",
        "agent_session_request": {"kind": "continue", "agent_name": "agent-b", "message": "new prompt"},
    }
    update = mw.before_agent(cast(Any, state), _runtime())
    assert update is not None
    assert update["_active_agent_id"] == "agent-b"
    assert len(subagent_b.calls) == 1
    sent_state = subagent_b.calls[0]
    # Heavy channels must not leak into subagent state
    assert "skills_metadata" not in sent_state
    assert "_goal_objective" not in sent_state
    # Because _active_agent_id ('agent-a') != 'agent-b', prior agent's messages must be discarded
    assert [m.content for m in sent_state["messages"]] == ["new prompt"]
    assert update["_agent_session_messages"]["reset"] is True

    # Passthrough turn when _active_agent_id is None wipes orphaned _agent_session_messages
    orphan_cleanup = mw.before_agent(
        cast(
            Any,
            {
                "messages": [],
                "_active_agent_id": None,
                "_agent_session_messages": [HumanMessage(content="orphaned")],
            },
        ),
        _runtime(),
    )
    assert orphan_cleanup == {"_agent_session_messages": {"reset": True}}


@pytest.mark.asyncio
async def test_close_subagent_mcp_sessions_on_clear_and_agent_switch() -> None:
    from opscloud.mcp.session_manager import MCPSessionManager
    from opscloud.plugins.adapters.mcp import scoped_subagent_mcp_server_name

    finops_id = "aws-finops-agent@talkops-devops-plugins"
    sec_id = "aws-cloud-security-engineer@talkops-devops-plugins"
    finops_srv = scoped_subagent_mcp_server_name(finops_id, "billing")
    sec_srv = scoped_subagent_mcp_server_name(sec_id, "iam")
    root_srv = "aws-mcp"

    mgr = MCPSessionManager({})
    closed: list[str] = []

    class _DummyEntry:
        def __init__(self, name: str) -> None:
            self.name = name

        async def aclose(self) -> None:
            closed.append(self.name)

    mgr._sessions[finops_srv] = cast(Any, _DummyEntry(finops_srv))
    mgr._sessions[sec_srv] = cast(Any, _DummyEntry(sec_srv))
    mgr._sessions[root_srv] = cast(Any, _DummyEntry(root_srv))

    # 1. Agent-specific close matches hashed plugin subagent server name
    count = await mgr.close_subagent_sessions(finops_id)
    assert count == 1
    assert closed == [finops_srv]
    assert finops_srv not in mgr._sessions
    assert sec_srv in mgr._sessions
    assert root_srv in mgr._sessions

    # 2. Unscoped close (used on /agent clear) closes all remaining subagent__* sessions
    count_all = await mgr.close_subagent_sessions(None)
    assert count_all == 1
    assert sec_srv not in mgr._sessions
    assert root_srv in mgr._sessions


