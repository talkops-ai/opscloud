"""Unit tests for Phase 1 & Phase 2 UI modularization:
- Scoped keybindings on `ApprovalMenu` (decoupled from `OpsCloudApp.BINDINGS`)
- `UIPhase` and `UIState` deterministic derivation (`opscloud.ui.state.ui_phase`)
- `PromptController` inline prompt lifecycle, interrupt routing, and focus guard
- `InputDock` bottom-container composition and typed widget accessors
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Key

from opscloud.ui.app import OpsCloudApp
from opscloud.ui.controllers.prompt_controller import PromptController
from opscloud.ui.state.ui_phase import (
    UIPhase,
    derive_ui_hints,
    derive_ui_phase,
    derive_ui_state,
)
from opscloud.ui.widgets.approval import ApprovalMenu
from opscloud.ui.widgets.input_dock import InputDock


def test_opscloud_app_bindings_contain_only_global_actions() -> None:
    """Verify widget-specific approval bindings and action_approval_* proxies are removed from OpsCloudApp."""
    actions = [
        b.action for b in OpsCloudApp.BINDINGS if isinstance(b, Binding)
    ]
    assert not any(a.startswith("approval_") for a in actions)
    assert not hasattr(OpsCloudApp, "action_approval_yes")
    assert not hasattr(OpsCloudApp, "action_approval_no")
    assert not hasattr(OpsCloudApp, "action_approval_up")
    assert not hasattr(OpsCloudApp, "action_approval_down")


def test_derive_ui_phase_priority_and_states() -> None:
    """Verify deterministic UIPhase transitions across app lifecycle states."""
    app = OpsCloudApp(defer_server_start=True, check_workspace_trust=False)
    assert app.ui_phase is UIPhase.IDLE
    assert app.ui_state.phase is UIPhase.IDLE
    assert app.ui_state.has_inline_prompt is False
    assert app.ui_state.is_busy is False

    # Booting
    app._connecting = True
    assert derive_ui_phase(app) is UIPhase.BOOTING
    app._connecting = False

    # Agent running
    app._agent_running = True
    assert derive_ui_phase(app) is UIPhase.AGENT_RUNNING
    assert derive_ui_state(app).is_busy is True

    # Shell running takes precedence over agent running
    app._shell_running = True
    assert derive_ui_phase(app) is UIPhase.SHELL_RUNNING
    app._shell_running = False

    # Inline prompts take precedence over agent_running
    app._pending_goal_review_widget = object()
    assert derive_ui_phase(app) is UIPhase.AWAITING_GOAL_REVIEW
    assert derive_ui_state(app).has_inline_prompt is True

    app._pending_ask_user_widget = MagicMock()
    assert derive_ui_phase(app) is UIPhase.AWAITING_ASK_USER

    app._pending_approval_widget = MagicMock()
    assert derive_ui_phase(app) is UIPhase.AWAITING_APPROVAL

    # Reloading takes precedence over inline prompts
    app._reloading = True
    assert derive_ui_phase(app) is UIPhase.RELOADING
    app._reloading = False

    # Workspace trust gate has highest priority
    app._check_workspace_trust = True
    app._workspace_trusted = False
    assert derive_ui_phase(app) is UIPhase.WORKSPACE_TRUST


def test_derive_ui_hints_for_phases_and_modes() -> None:
    """Verify scoped keyboard hints are returned for each UIPhase and input mode."""
    approval_hints = dict(derive_ui_hints(UIPhase.AWAITING_APPROVAL))
    assert "y" in approval_hints
    assert "n" in approval_hints

    idle_normal_hints = dict(derive_ui_hints(UIPhase.IDLE, input_mode="normal"))
    assert "/" in idle_normal_hints
    assert "!" in idle_normal_hints

    idle_cmd_hints = dict(derive_ui_hints(UIPhase.IDLE, input_mode="command"))
    assert "tab" in idle_cmd_hints


def test_approval_menu_handle_scoped_key() -> None:
    """Verify ApprovalMenu.handle_scoped_key dispatches navigation and decision keys locally."""
    menu = ApprovalMenu(
        [{"name": "execute", "call_id": "call_1", "args": {"command": "ls -la"}}]
    )
    assert menu._selected == 0

    assert menu.handle_scoped_key("down") is True
    assert menu._selected == 1

    assert menu.handle_scoped_key("up") is True
    assert menu._selected == 0

    # When reason input is active, printable keys return False so Input receives them
    menu._reason_input_active = True
    assert menu.handle_scoped_key("y") is False
    # Escape exits reason input mode and is consumed
    assert menu.handle_scoped_key("escape") is True


def test_prompt_controller_focus_guard_respects_active_prompt() -> None:
    """Verify focus_chat_input_after_refresh does not steal focus when an inline prompt is mounted."""
    app = OpsCloudApp(defer_server_start=True, check_workspace_trust=False)
    mock_chat_input = MagicMock()
    app._chat_input = mock_chat_input

    mock_approval = MagicMock()
    mock_approval.is_mounted = True
    app._pending_approval_widget = mock_approval

    scheduled: list[object] = []
    app.call_after_refresh = lambda fn, *a, **kw: scheduled.append(fn)  # type: ignore[method-assign]

    app._prompts.focus_chat_input_after_refresh()
    assert mock_approval.focus in scheduled
    assert mock_chat_input.focus_input not in scheduled


def test_prompt_controller_route_key_and_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify PromptController routes keys and interrupts to the active inline prompt."""
    app = OpsCloudApp(defer_server_start=True, check_workspace_trust=False)
    controller: PromptController = app._prompts

    menu = ApprovalMenu(
        [{"name": "execute", "call_id": "call_1", "args": {"command": "echo hi"}}]
    )
    monkeypatch.setattr(ApprovalMenu, "is_mounted", property(lambda self: True))
    controller.pending_approval_widget = menu

    event = Key(key="j", character="j")
    assert controller.route_key(event) is True
    assert menu._selected == 1

    # Interrupt rejects active approval
    menu.action_select_reject = lambda: setattr(menu, "_rejected_test", True) or None  # type: ignore[method-assign]
    assert controller.interrupt_active_prompt() is True
    assert getattr(menu, "_rejected_test", False) is True


@pytest.mark.asyncio
async def test_input_dock_composes_bottom_container_children() -> None:
    """Verify InputDock mounts SubagentPanel, GoalStatusPanel, BtwCardPanel, and ChatInput."""
    from opscloud.ui.theme import get_css_variable_defaults

    class _DockApp(App[None]):
        def get_theme_variable_defaults(self) -> dict[str, str]:
            return get_css_variable_defaults()

        def compose(self) -> ComposeResult:
            yield InputDock(id="bottom-container")

    app = _DockApp()
    async with app.run_test(size=(100, 24)):
        dock = app.query_one("#bottom-container", InputDock)
        assert dock.chat_input is not None
        assert dock.subagent_panel is not None
        assert dock.goal_status_panel is not None
        assert dock.btw_card_panel is not None


def test_domain_controllers_initialized_and_wired_on_app() -> None:
    """Verify Phase 3 & Phase 4 domain controllers are instantiated and bound on OpsCloudApp."""
    from opscloud.ui.controllers import (
        BtwController,
        GoalController,
        ModalController,
        PromptController,
        ServerLifecycleController,
        ShellController,
        ThreadController,
    )

    app = OpsCloudApp(defer_server_start=True, check_workspace_trust=False)
    assert isinstance(app._prompts, PromptController)
    assert isinstance(app._btw, BtwController)
    assert isinstance(app._shell, ShellController)
    assert isinstance(app._goals, GoalController)
    assert isinstance(app._threads, ThreadController)
    assert isinstance(app._server_lifecycle, ServerLifecycleController)
    assert isinstance(app._modals, ModalController)


def test_btw_and_shell_controllers_manage_state() -> None:
    """Verify BtwController and ShellController isolate side-question and shell buffering state."""
    from opscloud.ui.widgets.btw_card import BtwExchange

    app = OpsCloudApp(defer_server_start=True, check_workspace_trust=False)

    # BtwController
    assert app._btw.is_btw_open() is False
    app._btw_items.append(
        BtwExchange(question="what is k8s?", answer="container orchestrator", loading=False)
    )
    app._btw_index = 0
    assert app._btw.is_btw_open() is True
    answer, err = app._btw.get_btw_copy_status()
    assert answer == "container orchestrator"
    assert err is None
    app.dismiss_btw_response()
    assert app._btw.is_btw_open() is False
    assert app.show_btw_history() is True
    assert app._btw.is_btw_open() is True

    # ShellController
    assert len(app._pending_shell_messages) == 0
    app._shell.buffer_shell_for_model_context("echo hello", "hello", 0)
    assert len(app._pending_shell_messages) == 1
    assert "echo hello" in app._pending_shell_messages[0].content


def test_goal_and_modal_controllers_sync_state() -> None:
    """Verify GoalController rubric payload extraction and ModalController state helpers."""
    app = OpsCloudApp(defer_server_start=True, check_workspace_trust=False)

    payload = app._goals.goal_rubric_payload_from_state(
        {
            "_goal_objective": "Deploy staging cluster",
            "_goal_rubric": "1. Cluster healthy",
            "_goal_status": "active",
        }
    )
    assert payload["goal_objective"] == "Deploy staging cluster"
    assert payload["goal_rubric"] == "1. Cluster healthy"
    assert payload["goal_status"] == "active"

    app._goals.restore_goal_rubric_state(payload)
    assert app._active_goal == "Deploy staging cluster"
    assert app._active_rubric == "1. Cluster healthy"
    assert app._goal_status == "active"

    from opscloud.commands.power.agents import get_agent_session_state

    ag_state = get_agent_session_state(app)
    ag_state.agent_id = "devops-reviewer"
    app._modals.reset_ephemeral_mode_states()
    assert ag_state.agent_id is None


def test_phase5_modular_message_widgets_and_barrel_exports() -> None:
    """Verify Phase 5 modular message widgets match the messages.py barrel re-exports."""
    from opscloud.ui.widgets import (
        chat_messages,
        message_list,
        messages,
        status_messages,
        tool_call_message,
        tool_group,
    )

    assert messages.ToolCallMessage is tool_call_message.ToolCallMessage
    assert messages.FormattedOutput is tool_call_message.FormattedOutput
    assert messages.ToolGroupSummary is tool_group.ToolGroupSummary
    assert messages.summarize_tool_group is tool_group.summarize_tool_group
    assert messages.UserMessage is chat_messages.UserMessage
    assert messages.QueuedUserMessage is chat_messages.QueuedUserMessage
    assert messages.AssistantMessage is chat_messages.AssistantMessage
    assert messages.DiffMessage is chat_messages.DiffMessage
    assert messages.ThinkingMessage is chat_messages.ThinkingMessage
    assert messages.SkillMessage is status_messages.SkillMessage
    assert messages.ErrorMessage is status_messages.ErrorMessage
    assert messages.SystemMessage is status_messages.SystemMessage
    assert messages.CommandOutputMessage is status_messages.CommandOutputMessage
    assert messages.AppMessage is status_messages.AppMessage
    assert messages.TurnSummary is status_messages.TurnSummary
    assert messages.RubricResultMessage is status_messages.RubricResultMessage
    assert messages.MessageList is message_list.MessageList
    assert messages.compute_wheel_speed is message_list.compute_wheel_speed
