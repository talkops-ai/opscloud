"""Unit tests for OpsCloudApp instantiation and configuration."""

import pytest
from opscloud.approval_mode import ApprovalMode
from opscloud.ui.app import OpsCloudApp
from opscloud.ui.remote_client import RemoteAgent


def test_opscloud_app_init_defaults():
    app = OpsCloudApp(defer_server_start=True)
    assert app._assistant_id == "opscloud"
    assert app._client is None
    assert app._server_proc is None
    assert app._approval_mode == ApprovalMode.MANUAL
    assert app._auto_approve is False


def test_opscloud_app_init_with_server_url_and_thread():
    url = "http://127.0.0.1:9999"
    thread_id = "test-thread-1234"
    app = OpsCloudApp(
        server_url=url,
        thread_id=thread_id,
        approval_mode="auto",
        model="anthropic:claude-3-5-sonnet-latest",
    )
    assert app._client is not None
    assert isinstance(app._client, RemoteAgent)
    assert app._client._url == url
    assert app._resume_thread == thread_id
    assert app._approval_mode == ApprovalMode.AUTO
    assert app._server_startup_deferred is False


def test_opscloud_app_init_smart_approval_mode():
    app = OpsCloudApp(
        approval_mode="smart",
        defer_server_start=True,
    )
    assert app._approval_mode == ApprovalMode.SMART
    assert app._auto_approve is False


def test_opscloud_app_init_with_server_process():
    fake_proc = object()
    app = OpsCloudApp(
        server=fake_proc,
        defer_server_start=True,
    )
    assert app._server_proc is fake_proc


def test_opscloud_app_deferred_startup_when_no_model(monkeypatch):
    from opscloud.exceptions import NoCredentialsConfiguredError
    monkeypatch.setattr("opscloud.model.factory._get_default_model_spec", lambda: (_ for _ in ()).throw(NoCredentialsConfiguredError("No model configured")))
    app = OpsCloudApp(model=None, defer_server_start=False, client=None)
    assert app._server_startup_deferred is True
    assert app._model == ""


def test_format_rubric_event_removes_hourglass_emoji():
    from opscloud.ui.textual_adapter import _format_rubric_event

    # Start event without iteration label
    res1 = _format_rubric_event({"type": "rubric_evaluation_start", "iteration": 0, "show_iteration": False})
    assert res1 == "Checking acceptance criteria…"
    assert "⌛" not in res1

    # Start event with iteration label
    res2 = _format_rubric_event({"type": "rubric_evaluation_start", "iteration": 1, "show_iteration": True})
    assert res2 == "Checking acceptance criteria (iteration 2)…"
    assert "⌛" not in res2

    # End event satisfied
    res3 = _format_rubric_event({"type": "rubric_evaluation_end", "result": "satisfied"})
    assert "Acceptance criteria satisfied" in res3


@pytest.mark.asyncio
async def test_sync_goal_state_from_checkpoint_marks_goal_complete():
    from unittest.mock import AsyncMock
    from opscloud.commands.power.goal import GoalHandler, get_goal_state

    app = OpsCloudApp(defer_server_start=True)
    goal_state = get_goal_state(app)
    goal_state.objective = "Create nginx.conf"
    goal_state.rubric = "1. Listens on port 80"
    goal_state.status = "active"

    # Mock thread state values returned from checkpointer with rubric satisfied
    mock_state_values = {
        "_goal_objective": "Create nginx.conf",
        "_goal_status": "active",
        "_goal_rubric": "1. Listens on port 80",
        "_rubric_status": "satisfied",
        "_pending_goal_completion_note": "Created nginx.conf listening on 80",
    }
    app._get_thread_state_values = AsyncMock(return_value=mock_state_values)
    app._persist_goal_rubric_state = AsyncMock(return_value=True)
    app._mount_message = AsyncMock()

    # Create dummy status bar
    dummy_status_bar = AsyncMock()
    rubric_labels = []
    dummy_status_bar.set_rubric_label = lambda lbl: rubric_labels.append(lbl)
    app._status_bar = dummy_status_bar

    # Run checkpoint sync
    reconciled = await app._sync_goal_state_from_checkpoint(force=True)
    assert reconciled is True

    # Verify goal completed and persisted
    assert goal_state.status == "complete"
    assert goal_state.status_note == "Created nginx.conf listening on 80"
    app._persist_goal_rubric_state.assert_awaited_once()
    app._mount_message.assert_awaited_once()

    # Verify status bar label
    assert "✓ Goal complete" in rubric_labels


def test_welcome_banner_ascii_logo():
    from opscloud.ui.widgets.welcome import WelcomeBanner, _LeftPanel, _build_logo_text

    banner = WelcomeBanner()
    # Heading was removed per user request
    assert banner.border_title is None

    logo = _build_logo_text()
    plain = logo.plain
    # Verify logo is multi-line ASCII art (5 lines of art)
    lines = [line for line in plain.splitlines() if line.strip()]
    assert len(lines) == 5
    for line in lines:
        assert len(line) <= 50

    # Test left panel rendering
    left = _LeftPanel()
    rendered = left.render()
    assert rendered is not None
    # Verify Version, Project, and Status text are removed; tagline is present
    rendered_plain = "".join(r.plain for r in rendered.renderables)
    assert "by talkops.ai" in rendered_plain
    assert "Version" not in rendered_plain
    assert "Project" not in rendered_plain
    assert "Status" not in rendered_plain

    # Verify status transitions
    left.status = "connected"
    assert left.status == "connected"
    left.status = "error"
    assert left.status == "error"

    # Verify animation steps
    left.beam_x = -6
    left._advance_beam()
    assert left.beam_x == -4
    left.beam_x = 64
    left._advance_beam()
    assert left.beam_x == -6  # Wraps around

    # Test banner set_connected and set_error
    banner.set_connected()
    banner.set_error()

    # Test animated logo color beam modes
    for st in ("connected", "starting", "error"):
        anim_logo = _build_logo_text(beam_x=20, status=st)
        assert len(anim_logo.plain.splitlines()) == 5


def test_app_get_context_tokens_cumulative():
    app = OpsCloudApp(defer_server_start=True)
    app._cumulative_session_tokens = 552400
    app._context_tokens = 552400
    assert app.get_context_tokens() == 552400


def test_textual_adapter_session_cost_event_updates_cumulative_tokens():
    from unittest.mock import MagicMock
    from opscloud.ui.textual_adapter import TextualAdapter

    app = OpsCloudApp(defer_server_start=True)
    status_bar_mock = MagicMock()
    adapter = TextualAdapter(
        client=MagicMock(),
        assistant_id="opscloud",
        app=app,
        status_bar=status_bar_mock,
    )

    data = {
        "type": "session_cost",
        "total": 0.2503,
        "total_tokens": 552400,
        "pricing_ok": True,
    }
    adapter._handle_custom_stream_event(data, is_main_agent=True)

    assert app._session_cost_usd == 0.2503
    assert app._cumulative_session_tokens == 552400
    status_bar_mock.set_cost.assert_called_once_with(0.2503)
    status_bar_mock.set_tokens.assert_called_once_with(552400, context_tokens=0)


@pytest.mark.asyncio
async def test_load_thread_history_accumulates_cumulative_tokens():
    from unittest.mock import AsyncMock, MagicMock
    from langchain_core.messages import AIMessage, HumanMessage

    app = OpsCloudApp(defer_server_start=True)
    app._agent_thread_id = "test-thread-cumulative"

    raw_msgs = [
        HumanMessage(content="First turn"),
        AIMessage(
            content="Answer 1",
            usage_metadata={"input_tokens": 10000, "output_tokens": 2000, "total_tokens": 12000},
        ),
        HumanMessage(content="Second turn"),
        AIMessage(
            content="Answer 2",
            usage_metadata={"input_tokens": 15000, "output_tokens": 3000, "total_tokens": 18000},
        ),
    ]

    app._get_thread_state_values = AsyncMock(return_value={
        "messages": raw_msgs,
        "_session_cost_usd": 0.15,
        "_session_total_tokens": 30000,
        "_context_tokens": 18000,
    })
    app._restore_goal_rubric_state = MagicMock()
    app._regroup_completed_tools = AsyncMock()

    # Create dummy messages container
    dummy_container = MagicMock()
    dummy_container.children = []
    dummy_container._tool_calls = {}

    from textual.css.query import NoMatches

    def mock_query_one(selector, *args, **kwargs):
        if selector == "#messages":
            return dummy_container
        raise NoMatches(selector)

    app.query_one = mock_query_one

    await app._load_thread_history("test-thread-cumulative")

    # Cumulative tokens: 30000, Active context tokens: 18000
    assert app._context_tokens == 18000
    assert app._cumulative_session_tokens == 30000
    assert app._session_cost_usd >= 0.15


def test_finalize_turn_success_does_not_double_count():
    from unittest.mock import MagicMock
    from opscloud.ui.textual_adapter import TextualAdapter, _TurnStreamState

    app = OpsCloudApp(defer_server_start=True)
    app._cumulative_session_tokens = 38700
    status_bar_mock = MagicMock()
    adapter = TextualAdapter(
        client=MagicMock(),
        assistant_id="opscloud",
        app=app,
        status_bar=status_bar_mock,
    )

    stream_state = _TurnStreamState(
        start_session_tokens=38700,
        turn_context_tokens=13860,
    )

    # 1. session_cost event arrives with total thread tokens (52560)
    data = {
        "type": "session_cost",
        "total": 0.0426,
        "total_tokens": 52560,
        "pricing_ok": True,
    }
    adapter._handle_custom_stream_event(data, is_main_agent=True, stream_state=stream_state)
    assert stream_state.received_session_tokens is True
    assert app._cumulative_session_tokens == 52560

    # 2. finalize turn success sets cumulative tokens (52560) and active context tokens (13860) on status bar and app
    adapter._finalize_turn_success(0.0, 13860, stream_state=stream_state)
    assert app._cumulative_session_tokens == 52560
    assert app._context_tokens == 13860
    status_bar_mock.set_tokens.assert_called_with(52560, context_tokens=13860)


def test_finalize_turn_success_fallback_without_session_cost():
    from unittest.mock import MagicMock
    from opscloud.ui.textual_adapter import TextualAdapter, _TurnStreamState

    app = OpsCloudApp(defer_server_start=True)
    app._cumulative_session_tokens = 38700
    status_bar_mock = MagicMock()
    adapter = TextualAdapter(
        client=MagicMock(),
        assistant_id="opscloud",
        app=app,
        status_bar=status_bar_mock,
    )

    stream_state = _TurnStreamState(
        start_session_tokens=38700,
        turn_context_tokens=13860,
        received_session_tokens=False,
    )

    # If no session_cost event arrived, fallback adds to start_session_tokens
    adapter._finalize_turn_success(0.0, 13860, stream_state=stream_state)
    assert app._cumulative_session_tokens == 38700 + 13860
    assert app._context_tokens == 13860
    status_bar_mock.set_tokens.assert_called_with(38700 + 13860, context_tokens=13860)


def test_status_bar_context_window_rendering():
    from unittest.mock import MagicMock
    from opscloud.ui.widgets.status import StatusBar

    bar = StatusBar()
    mock_display = MagicMock()
    bar.query_one = MagicMock(return_value=mock_display)

    # 1. Context limit 1,000,000, cumulative tokens 37,658, context tokens 12,932, cost $0.04
    bar.set_context_limit(1_000_000)
    bar.set_cost(0.04)
    bar.set_tokens(37_658, context_tokens=12_932)

    # Verify update called with Text object containing Context: 1% • 37.7K tokens • $0.04
    mock_display.update.assert_called()
    rendered_text = mock_display.update.call_args[0][0]
    plain = rendered_text.plain if hasattr(rendered_text, "plain") else str(rendered_text)
    assert "Context: 1%" in plain
    assert "37.7K tokens" in plain
    assert "$0.04" in plain


@pytest.mark.asyncio
async def test_deprecated_cost_and_tokens_command():
    from opscloud.commands.core.cost import CostHandler
    from opscloud.commands._base import CommandContext

    handler = CostHandler()
    ctx = CommandContext(app=None, raw_command="/cost", args="")
    result = await handler.execute(ctx)

    assert result.success is True
    assert "deprecated" in result.message.lower()
    assert "status bar" in result.message.lower()


def test_interrupt_and_quit_bindings():
    """Verify priority bindings for escape and ctrl+c."""
    bindings = {b.key: b for b in OpsCloudApp.BINDINGS}
    assert "escape" in bindings
    assert bindings["escape"].action == "interrupt"
    assert bindings["escape"].priority is True

    assert "ctrl+c" in bindings
    assert bindings["ctrl+c"].action == "quit_or_interrupt"
    assert bindings["ctrl+c"].priority is True


def test_user_message_set_cancelled_and_raw_text():
    """Verify UserMessage tracks raw text and can be dimmed via set_cancelled."""
    from opscloud.ui.widgets.messages import UserMessage

    msg = UserMessage("hello world")
    assert msg.raw_text == "hello world"
    assert "-cancelled" not in msg.classes
    msg.set_cancelled()
    assert "-cancelled" in msg.classes


def test_action_interrupt_cancels_agent_worker():
    """Verify action_interrupt cancels agent worker, adapter, and dims message."""
    from unittest.mock import MagicMock
    from opscloud.ui.widgets.messages import UserMessage

    app = OpsCloudApp(defer_server_start=True)
    app._agent_running = True
    app._agent_thread_id = "test-thread-interrupt"

    mock_worker = MagicMock()
    app._agent_worker = mock_worker

    mock_adapter = MagicMock()
    app._adapter = mock_adapter

    user_msg = UserMessage("test prompt to interrupt")
    app._active_user_message = user_msg
    app._active_turn_visible_output_started = False

    mock_chat_input = MagicMock()
    mock_chat_input.value = ""
    mock_chat_input.dismiss_completion.return_value = False
    mock_chat_input.exit_mode.return_value = False
    app._chat_input = mock_chat_input

    app.action_interrupt()

    # Worker was cancelled
    mock_worker.cancel.assert_called_once()
    # Adapter cancel was called with thread_id
    mock_adapter.cancel.assert_called_once_with(thread_id="test-thread-interrupt")
    # User message was dimmed
    assert "-cancelled" in user_msg.classes
    # Prompt was restored to chat input
    mock_chat_input.set_value_at_end.assert_called_once_with("test prompt to interrupt")


def test_action_interrupt_preserves_prompt_if_output_appeared():
    """Verify prompt is NOT restored if visible model output already appeared."""
    from unittest.mock import MagicMock
    from opscloud.ui.widgets.messages import UserMessage

    app = OpsCloudApp(defer_server_start=True)
    app._agent_running = True
    app._agent_thread_id = "test-thread-interrupt"
    app._agent_worker = MagicMock()
    app._adapter = MagicMock()

    user_msg = UserMessage("test prompt with output")
    app._active_user_message = user_msg
    app._active_turn_visible_output_started = True

    mock_chat_input = MagicMock()
    mock_chat_input.value = ""
    mock_chat_input.dismiss_completion.return_value = False
    mock_chat_input.exit_mode.return_value = False
    app._chat_input = mock_chat_input

    app.action_interrupt()

    # set_value_at_end was NOT called because output already appeared
    mock_chat_input.set_value_at_end.assert_not_called()
    assert "-cancelled" in user_msg.classes


def test_action_interrupt_rejects_pending_approval_first():
    """Verify approval widget is rejected before agent worker is cancelled."""
    from unittest.mock import MagicMock

    app = OpsCloudApp(defer_server_start=True)
    app._agent_running = True
    mock_worker = MagicMock()
    app._agent_worker = mock_worker

    mock_approval = MagicMock()
    app._pending_approval_widget = mock_approval

    app.action_interrupt()

    # Approval was rejected
    mock_approval.action_select_reject.assert_called_once()
    # Agent worker was NOT cancelled yet because approval handled the interrupt
    mock_worker.cancel.assert_not_called()


def test_action_interrupt_pops_queued_message():
    """Verify queued message is popped instead of cancelling active agent worker."""
    from collections import deque
    from unittest.mock import MagicMock
    from opscloud.ui.app import QueuedMessage

    app = OpsCloudApp(defer_server_start=True)
    app._agent_running = True
    mock_worker = MagicMock()
    app._agent_worker = mock_worker

    app._pending_messages = deque([QueuedMessage(text="queued prompt 1"), QueuedMessage(text="queued prompt 2")])
    mock_widget = MagicMock()
    app._queued_widgets = deque([MagicMock(), mock_widget])

    mock_chat_input = MagicMock()
    mock_chat_input.value = ""
    mock_chat_input.dismiss_completion.return_value = False
    mock_chat_input.exit_mode.return_value = False
    app._chat_input = mock_chat_input

    app.action_interrupt()

    # Last queued message was popped
    assert len(app._pending_messages) == 1
    assert app._pending_messages[0].text == "queued prompt 1"
    mock_widget.remove.assert_called_once()
    mock_chat_input.set_value_at_end.assert_called_once_with("queued prompt 2")
    # Agent worker was NOT cancelled
    mock_worker.cancel.assert_not_called()

