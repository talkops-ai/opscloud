"""Unit tests for `/btw` side-question isolation and UI card."""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from opscloud.agent.config import CLIContextSchema
from opscloud.commands._base import CommandContext
from opscloud.commands._types import BypassTier
from opscloud.commands.power.btw import BtwHandler
from opscloud.commands.power.copy import CopyHandler
from opscloud.middleware.btw import (
    BTW_PROMPT_PREFIX,
    BTW_TIMEOUT_SECONDS,
    BtwSideQuestionMiddleware,
    format_btw_prompt,
    is_btw_turn,
    is_ephemeral_message,
)
from opscloud.ui.command_registry import IMMEDIATE_CMDS, get_command
from opscloud.ui.remote_client import RemoteAgent
from opscloud.ui.widgets.btw_card import BtwCardPanel, BtwExchange, _truncate_answer


def test_btw_prompt_prefix_and_timeout() -> None:
    """Verify the side-question prompt prefix and 60s timeout constant."""
    assert len(BTW_PROMPT_PREFIX.encode("utf-8")) == 419
    assert BTW_PROMPT_PREFIX.startswith("This is a side question from the user.")
    assert BTW_PROMPT_PREFIX.endswith("Here is the question: ")
    assert BTW_TIMEOUT_SECONDS == 60.0

    wrapped = format_btw_prompt("What does S3 versioning do?")
    assert wrapped == f"{BTW_PROMPT_PREFIX}What does S3 versioning do?"
    # Idempotent if already wrapped
    assert format_btw_prompt(wrapped) == wrapped


def test_is_ephemeral_message_and_is_btw_turn() -> None:
    """Verify ephemeral message detection and `/btw` turn detection across state and runtime."""
    normal_msg = HumanMessage(content="Deploy ECS cluster")
    prefixed_msg = HumanMessage(content=format_btw_prompt("What region are we in?"))
    flagged_msg = HumanMessage(content="Quick question", additional_kwargs={"ephemeral": True})

    assert is_ephemeral_message(normal_msg) is False
    assert is_ephemeral_message(prefixed_msg) is True
    assert is_ephemeral_message(flagged_msg) is True

    # State flag
    assert is_btw_turn({"_btw_side_question": True, "messages": [normal_msg]}) is True
    assert is_btw_turn({"_btw_side_question": False, "messages": [normal_msg]}) is False

    # Runtime CLIContextSchema flag
    mock_runtime = MagicMock()
    mock_runtime.context = CLIContextSchema.from_payload({"_btw_side_question": True})
    mock_runtime.config = {}
    assert is_btw_turn({"messages": [normal_msg]}, mock_runtime) is True

    # Message fallback
    assert is_btw_turn({"messages": [prefixed_msg]}) is True


@pytest.mark.asyncio
async def test_btw_middleware_strips_tools_and_jumps_to_end() -> None:
    """Verify BtwSideQuestionMiddleware strips tools in wrap_model_call and jumps to end in after_model."""
    mw = BtwSideQuestionMiddleware()

    mock_req = MagicMock()
    mock_req.state = {"_btw_side_question": True}
    mock_req.runtime = None
    overridden_req = MagicMock()
    mock_req.override = MagicMock(return_value=overridden_req)

    # Sync wrap_model_call
    sync_handler = MagicMock(return_value="sync_resp")
    res = mw.wrap_model_call(mock_req, sync_handler)
    assert res == "sync_resp"
    mock_req.override.assert_called_once_with(tools=[])
    sync_handler.assert_called_once_with(overridden_req)

    # Async awrap_model_call
    mock_req.override.reset_mock()
    async_handler = AsyncMock(return_value="async_resp")
    res_async = await mw.awrap_model_call(mock_req, async_handler)
    assert res_async == "async_resp"
    mock_req.override.assert_called_once_with(tools=[])
    async_handler.assert_awaited_once_with(overridden_req)

    # Normal turn does not strip tools
    normal_req = MagicMock()
    normal_req.state = {"_btw_side_question": False, "messages": []}
    normal_req.runtime = None
    await mw.awrap_model_call(normal_req, async_handler)
    normal_req.override.assert_not_called()

    # after_model & aafter_model
    mock_runtime = MagicMock()
    mock_runtime.context = None
    mock_runtime.config = {}
    assert mw.after_model({"_btw_side_question": True, "messages": []}, mock_runtime) == {
        "_btw_side_question": True,
        "jump_to": "end",
    }
    assert await mw.aafter_model({"_btw_side_question": True, "messages": []}, mock_runtime) == {
        "_btw_side_question": True,
        "jump_to": "end",
    }
    assert mw.after_model({"_btw_side_question": False, "messages": []}, mock_runtime) is None
    assert await mw.aafter_model({"_btw_side_question": False, "messages": []}, mock_runtime) is None


@pytest.mark.asyncio
async def test_btw_middleware_in_langchain_create_agent_graph() -> None:
    """Verify create_agent with BtwSideQuestionMiddleware strips tools and skips rubric/stop hooks."""
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from opscloud.middleware.reliable_rubric import ReliableRubricMiddleware
    from opscloud.state.base import OpsCloudAgentState

    bound_tools_seen: list[int] = []

    class InspectingChatModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "inspecting-fake"

        def _generate(
            self,
            messages: Any,
            stop: Any = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> ChatResult:
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="Single-turn side answer."))]
            )

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            bound_tools_seen.append(len(tools))
            return self

    @tool
    def dummy_shell(cmd: str) -> str:
        """Run a shell command."""
        return cmd

    model = InspectingChatModel()
    rubric_mw = ReliableRubricMiddleware(
        model=model,
        max_iterations=2,
    )

    mw_stack: list[Any] = [BtwSideQuestionMiddleware(), rubric_mw]
    graph: Any = create_agent(
        model=model,
        tools=[dummy_shell],
        middleware=mw_stack,
        state_schema=OpsCloudAgentState,
        context_schema=CLIContextSchema,
    )

    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content=format_btw_prompt("What is IAM?"))],
            "rubric": "- Must create S3 bucket",
        },
        context=CLIContextSchema(_btw_side_question=True),
    )

    # Verify the model was bound with 0 tools on the /btw turn and rubric evaluation was skipped
    assert bound_tools_seen == []
    assert result["messages"][-1].content == "Single-turn side answer."


@pytest.mark.asyncio
async def test_remote_agent_astream_side_question_forks_and_deletes_temp_thread() -> None:
    """Verify RemoteAgent.astream_side_question forks parent thread via threads.copy and cleans up in finally."""
    agent = RemoteAgent(url="http://localhost:2024", graph_name="agent")
    agent._active_run_id = "main-run-123"

    mock_client = MagicMock()
    mock_client.threads.copy = AsyncMock(return_value={"thread_id": "temp-btw-thread-999"})
    mock_client.threads.delete = AsyncMock()
    mock_client.runs.cancel = AsyncMock()

    async def fake_astream(run_input: dict[str, Any], **kwargs: Any):
        assert kwargs["config"]["configurable"]["thread_id"] == "temp-btw-thread-999"
        assert kwargs["context"]["_btw_side_question"] is True
        assert run_input["_btw_side_question"] is True
        msg = run_input["messages"][-1]
        assert msg.content == format_btw_prompt("Why is the pod pending?")

        on_run_created = kwargs.get("on_run_created")
        if callable(on_run_created):
            on_run_created({"run_id": "side-run-456"})

        yield (
            (),
            "messages",
            (
                {"type": "ai", "content": "Because PVC is unbound."},
                {"run_id": "side-run-456"},
            ),
        )

    mock_graph = MagicMock()
    mock_graph._validate_client = MagicMock(return_value=mock_client)
    mock_graph.astream = fake_astream

    with patch.object(agent, "_get_graph", return_value=mock_graph):
        chunks = []
        async for chunk in agent.astream_side_question(
            "Why is the pod pending?",
            parent_thread_id="parent-thread-111",
            context={"provider": "google_genai"},
        ):
            chunks.append(chunk)

    # Verify parent run state was never mutated
    assert agent._active_run_id == "main-run-123"
    assert chunks == ["Because PVC is unbound."]

    # Verify forked thread was created via copy and deleted in finally
    mock_client.threads.copy.assert_awaited_once()
    mock_client.threads.delete.assert_awaited_once_with("temp-btw-thread-999")
    mock_client.runs.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_btw_command_handler_and_copy_btw() -> None:
    """Verify /btw and /copy btw command handlers."""
    handler = BtwHandler()
    assert handler.name == "/btw"
    assert "/aside" in handler.aliases
    assert handler.bypass_tier == BypassTier.IMMEDIATE
    assert "/btw" in IMMEDIATE_CMDS
    assert "/aside" in IMMEDIATE_CMDS
    btw_cmd = get_command("/btw")
    assert btw_cmd is not None
    assert btw_cmd.description == "Ask a side question without interrupting the current task"

    # 1. Empty /btw with no history -> usage message
    mock_app = MagicMock()
    mock_app.show_btw_history = MagicMock(return_value=False)
    mock_app.is_btw_active = MagicMock(return_value=False)
    mock_app._send_ephemeral_message = AsyncMock()

    res_empty = await handler.execute(CommandContext(app=mock_app, raw_command="/btw", args=""))
    assert res_empty.success is False
    assert "Usage: /btw <question>" in (res_empty.message or "")

    # 2. Empty /btw with existing history -> reopens history card silently
    mock_app.show_btw_history.return_value = True
    res_hist = await handler.execute(CommandContext(app=mock_app, raw_command="/btw", args=""))
    assert res_hist.success is True
    assert res_hist.message is None
    mock_app.show_btw_history.assert_called()

    # 3. Concurrent /btw while one is already in flight -> rejected
    mock_app.is_btw_active.return_value = True
    res_busy = await handler.execute(
        CommandContext(app=mock_app, raw_command="/btw another question", args="another question")
    )
    assert res_busy.success is False
    assert (
        res_busy.message
        == "A /btw question is already in progress. Please wait for it to finish."
    )
    mock_app._send_ephemeral_message.assert_not_awaited()

    # 4. Valid /btw question -> dispatches _send_ephemeral_message
    mock_app.is_btw_active.return_value = False
    res_ok = await handler.execute(
        CommandContext(app=mock_app, raw_command="/btw what is VPC peering?", args="what is VPC peering?")
    )
    assert res_ok.success is True
    mock_app._send_ephemeral_message.assert_awaited_once_with("what is VPC peering?")

    # 5. /copy btw
    copy_handler = CopyHandler()
    mock_app.get_btw_copy_status = MagicMock(return_value=(None, "No /btw response to copy."))
    res_copy_none = await copy_handler.execute(
        CommandContext(app=mock_app, raw_command="/copy btw", args="btw")
    )
    assert res_copy_none.success is False
    assert res_copy_none.message == "No /btw response to copy."

    mock_app.get_btw_copy_status.return_value = (
        None,
        "Wait for the /btw response to finish before copying.",
    )
    res_copy_loading = await copy_handler.execute(
        CommandContext(app=mock_app, raw_command="/copy btw", args="btw")
    )
    assert res_copy_loading.success is False
    assert res_copy_loading.message == "Wait for the /btw response to finish before copying."

    mock_app.get_btw_copy_status.return_value = ("VPC peering connects two VPCs.", None)
    with patch.object(CopyHandler, "_copy_text", return_value=True):
        res_copy_ok = await copy_handler.execute(
            CommandContext(app=mock_app, raw_command="/copy btw", args="btw")
        )
    assert res_copy_ok.success is True
    assert "Copied /btw response to clipboard" in (res_copy_ok.message or "")


def test_btw_card_panel_render_and_truncation() -> None:
    """Verify BtwCardPanel rendering for loading, completed, error, and multi-item states."""
    panel = BtwCardPanel()
    assert panel.display is False

    ex1 = BtwExchange(question="What is an ALB?", loading=True)
    panel.render_state([ex1], 0)
    assert panel.display is True

    ex1.answer = "Application Load Balancer."
    ex1.loading = False
    ex2 = BtwExchange(question="What is an NLB?", answer="Network Load Balancer.", loading=False)
    panel.render_state([ex1, ex2], 1)
    assert panel.display is True

    # Dismissing hides the panel
    panel.render_state([ex1, ex2], -1)
    assert panel.display is False

    # Verify truncation helper
    long_answer = "\n".join(f"Line {i}" for i in range(15))
    clipped, is_truncated = _truncate_answer(long_answer, expanded=False)
    assert is_truncated is True
    assert len(clipped.splitlines()) == 8

    full, is_truncated_exp = _truncate_answer(long_answer, expanded=True)
    assert is_truncated_exp is True
    assert full == long_answer


def test_opscloud_app_btw_store_and_bypass_queue() -> None:
    """Verify OpsCloudApp /btw queue bypass, history navigation, expand toggle, and copy status."""
    from opscloud.ui.app import OpsCloudApp

    app = OpsCloudApp(defer_server_start=True)
    app._refresh_btw_card = MagicMock()  # type: ignore[method-assign]

    # /btw and /aside must bypass the busy queue even when _agent_running is True
    app._agent_running = True
    assert app._can_bypass_queue("/btw what is S3?") is True
    assert app._can_bypass_queue("/aside explain") is True
    assert app._can_bypass_queue("regular prompt") is False

    # Initially empty
    assert app.is_btw_open() is False
    assert app.is_btw_active() is False
    assert app.show_btw_history() is False
    assert app.get_btw_copy_status() == (None, "No /btw response to copy.")

    # Add two exchanges
    ex1 = BtwExchange(question="Q1", answer="A1", loading=False)
    ex2 = BtwExchange(question="Q2", answer="", loading=True)
    app._btw_items.extend([ex1, ex2])
    app._btw_index = 1

    assert app.is_btw_open() is True
    assert app.is_btw_active() is True
    assert app.get_btw_copy_status() == (
        None,
        "The /btw answer is still being generated.",
    )

    # Complete ex2
    ex2.answer = "A2"
    ex2.loading = False
    assert app.is_btw_active() is False
    assert app.get_btw_copy_status() == ("A2", None)

    # Navigate history
    app.navigate_btw(-1)
    assert app._btw_index == 0
    assert app.get_btw_copy_status() == ("A1", None)
    app.navigate_btw(1)
    assert app._btw_index == 1

    # Toggle expand
    assert ex2.expanded is False
    app.toggle_btw_expanded()
    assert ex2.expanded is True

    # Dismiss and reopen history
    app.dismiss_btw_response()
    assert app.is_btw_open() is False
    # Even when dismissed, /copy btw copies the latest completed exchange
    assert app.get_btw_copy_status() == ("A2", None)
    assert app.show_btw_history() is True
    assert app.is_btw_open() is True
    assert app._btw_index == 1

    # Clear all on /clear
    app.clear_active_btw()
    assert app._btw_items == []
    assert app._btw_index == -1

