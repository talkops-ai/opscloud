"""UI state and phase derivation for the OpsCloud TUI.

Inspired by Antigravity CLI's ``store/uiphase.go`` (``DeriveUIState``,
``derivePhase``, ``deriveHints``), this module provides a single deterministic
source of truth for the active UI lifecycle phase and its scoped keyboard hints,
replacing scattered boolean flag checks across widgets and app handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class UIPhase(StrEnum):
    """Mutually-exclusive high-level lifecycle phase of the TUI."""

    WORKSPACE_TRUST = "workspace_trust"
    """Startup blocked awaiting workspace trust or settings error resolution."""

    BOOTING = "booting"
    """Initial server connection or startup sequence in progress."""

    RELOADING = "reloading"
    """Environment/plugin/MCP reload or server restart in progress."""

    AWAITING_APPROVAL = "awaiting_approval"
    """Inline HITL tool approval prompt is active and owns keyboard input."""

    AWAITING_ASK_USER = "awaiting_ask_user"
    """Inline ask_user question prompt is active and owns keyboard input."""

    AWAITING_GOAL_REVIEW = "awaiting_goal_review"
    """Inline goal acceptance-criteria review prompt is active."""

    SHELL_RUNNING = "shell_running"
    """Interactive `!` or `!!` shell subprocess is currently executing."""

    AGENT_RUNNING = "agent_running"
    """Agent turn stream is actively running."""

    IDLE = "idle"
    """Ready for normal prompt, slash command, or shell input."""


_PHASE_HINTS: dict[UIPhase, tuple[tuple[str, str], ...]] = {
    UIPhase.WORKSPACE_TRUST: (
        ("enter", "confirm"),
        ("esc", "quit"),
    ),
    UIPhase.BOOTING: (
        ("ctrl+c", "quit"),
    ),
    UIPhase.RELOADING: (
        ("ctrl+c", "cancel"),
    ),
    UIPhase.AWAITING_APPROVAL: (
        ("y", "approve"),
        ("s", "smart mode"),
        ("n", "reject"),
        ("tab", "reason"),
    ),
    UIPhase.AWAITING_ASK_USER: (
        ("↑↓", "select"),
        ("enter", "confirm"),
        ("tab", "next question"),
        ("esc", "cancel"),
    ),
    UIPhase.AWAITING_GOAL_REVIEW: (
        ("y", "accept"),
        ("e", "edit"),
        ("r", "reject"),
        ("n", "cancel"),
    ),
    UIPhase.SHELL_RUNNING: (
        ("esc", "kill command"),
        ("ctrl+c", "interrupt"),
    ),
    UIPhase.AGENT_RUNNING: (
        ("esc", "interrupt"),
        ("ctrl+o", "toggle output"),
        ("shift+tab", "approval mode"),
    ),
    UIPhase.IDLE: (
        ("/", "commands"),
        ("!", "bash"),
        ("@", "files"),
        ("shift+tab", "approval mode"),
    ),
}


@dataclass(frozen=True)
class UIState:
    """Immutable snapshot of derived UI state for the current frame."""

    phase: UIPhase
    input_mode: str = "normal"
    btw_open: bool = False
    queued_count: int = 0
    hints: tuple[tuple[str, str], ...] = ()

    @property
    def has_inline_prompt(self) -> bool:
        """Whether an inline prompt (approval, ask_user, goal_review) owns focus."""
        return self.phase in {
            UIPhase.AWAITING_APPROVAL,
            UIPhase.AWAITING_ASK_USER,
            UIPhase.AWAITING_GOAL_REVIEW,
        }

    @property
    def is_busy(self) -> bool:
        """Whether an agent turn or shell command is actively running."""
        return self.phase in {
            UIPhase.AGENT_RUNNING,
            UIPhase.SHELL_RUNNING,
            UIPhase.AWAITING_APPROVAL,
            UIPhase.AWAITING_ASK_USER,
        }


def derive_ui_phase(app: Any) -> UIPhase:
    """Compute the deterministic `UIPhase` from the current application state."""
    if getattr(app, "_check_workspace_trust", False) and not getattr(
        app, "_workspace_trusted", True
    ):
        return UIPhase.WORKSPACE_TRUST

    if getattr(app, "_reloading", False) or getattr(app, "_reconnecting", False):
        return UIPhase.RELOADING

    if getattr(app, "_pending_approval_widget", None) is not None:
        return UIPhase.AWAITING_APPROVAL

    if getattr(app, "_pending_ask_user_widget", None) is not None:
        return UIPhase.AWAITING_ASK_USER

    if getattr(app, "_pending_goal_review_widget", None) is not None:
        return UIPhase.AWAITING_GOAL_REVIEW

    if getattr(app, "_shell_running", False):
        return UIPhase.SHELL_RUNNING

    if getattr(app, "_agent_running", False):
        return UIPhase.AGENT_RUNNING

    if getattr(app, "_connecting", False) or getattr(
        app, "_startup_sequence_running", False
    ):
        return UIPhase.BOOTING

    return UIPhase.IDLE


def derive_ui_hints(
    phase: UIPhase,
    input_mode: str = "normal",
) -> tuple[tuple[str, str], ...]:
    """Return scoped keybinding hints for the active `UIPhase` and input mode."""
    if phase is UIPhase.IDLE and input_mode != "normal":
        from opscloud.ui.widgets.chat_input import input_hints

        return tuple(input_hints(input_mode))
    return _PHASE_HINTS.get(phase, _PHASE_HINTS[UIPhase.IDLE])


def derive_ui_state(app: Any) -> UIState:
    """Derive a complete immutable `UIState` snapshot from the application."""
    phase = derive_ui_phase(app)
    chat_input = getattr(app, "_chat_input", None)
    input_mode = getattr(chat_input, "mode", "normal") if chat_input is not None else "normal"
    btw_open = bool(app.is_btw_open()) if hasattr(app, "is_btw_open") else False
    pending_msgs = getattr(app, "_pending_messages", None)
    queued_count = len(pending_msgs) if pending_msgs is not None else 0
    hints = derive_ui_hints(phase, input_mode=input_mode)
    return UIState(
        phase=phase,
        input_mode=input_mode,
        btw_open=btw_open,
        queued_count=queued_count,
        hints=hints,
    )


__all__ = [
    "UIPhase",
    "UIState",
    "derive_ui_hints",
    "derive_ui_phase",
    "derive_ui_state",
]
