"""Bottom input dock container (`InputDock`) for the OpsCloud TUI.

Inspired by Antigravity CLI's ``InputModel`` (``model/input.go``) and
``ConversationModel`` dock area, `InputDock` encapsulates the bottom container
holding:
  - `SubagentPanel` (`#subagent-panel`)
  - `GoalStatusPanel` (`#goal-status-panel`)
  - `BtwCardPanel` (`#btw-card-panel`)
  - `ChatInput` (`#input-area`)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.containers import Container
from textual.css.query import NoMatches

from opscloud.ui.widgets.btw_card import BtwCardPanel
from opscloud.ui.widgets.chat_input import ChatInput
from opscloud.ui.widgets.goal_status import GoalStatusPanel
from opscloud.ui.widgets.subagent_panel import SubagentPanel

if TYPE_CHECKING:
    from textual.app import ComposeResult


class InputDock(Container):
    """Bottom-dock multiplexer and status/card host above the prompt input."""

    def __init__(self, *, id: str = "bottom-container", **kwargs: Any) -> None:
        super().__init__(id=id, **kwargs)

    def compose(self) -> ComposeResult:
        yield SubagentPanel(id="subagent-panel")
        yield GoalStatusPanel(id="goal-status-panel")
        yield BtwCardPanel(id="btw-card-panel")
        yield ChatInput(id="input-area")

    @property
    def chat_input(self) -> ChatInput | None:
        """Return the `ChatInput` widget if mounted."""
        try:
            return self.query_one("#input-area", ChatInput)
        except NoMatches:
            return None

    @property
    def subagent_panel(self) -> SubagentPanel | None:
        """Return the `SubagentPanel` widget if mounted."""
        try:
            return self.query_one("#subagent-panel", SubagentPanel)
        except NoMatches:
            return None

    @property
    def goal_status_panel(self) -> GoalStatusPanel | None:
        """Return the `GoalStatusPanel` widget if mounted."""
        try:
            return self.query_one("#goal-status-panel", GoalStatusPanel)
        except NoMatches:
            return None

    @property
    def btw_card_panel(self) -> BtwCardPanel | None:
        """Return the `BtwCardPanel` widget if mounted."""
        try:
            return self.query_one("#btw-card-panel", BtwCardPanel)
        except NoMatches:
            return None

    def focus_input(self) -> None:
        """Focus the active `ChatInput` widget."""
        ci = self.chat_input
        if ci is not None:
            ci.focus_input()

    def dismiss_popup_or_mode(self) -> bool:
        """Dismiss active autocomplete popup or exit slash/shell mode on Escape."""
        ci = self.chat_input
        if ci is None:
            return False
        if hasattr(ci, "dismiss_completion") and ci.dismiss_completion():
            return True
        if hasattr(ci, "exit_mode") and ci.exit_mode():
            return True
        return False


__all__ = ["InputDock"]
