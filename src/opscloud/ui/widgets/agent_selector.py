"""Interactive agent selector screen for the ``/agents`` command.

Modal dialog for selecting agents discovered from ``~/.opscloud/``, supports
↑/↓/Tab navigation, Enter to select, Ctrl+S to toggle default, and Esc to cancel.

On selection returns the agent name string.  The caller
(``OpsCloudApp._show_agent_selector``) handles the actual agent switch.
"""

from __future__ import annotations

import asyncio
import contextlib
from opscloud.utils.logger import get_logger
from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from opscloud.agent.config import (
    clear_default_agent,
    save_default_agent,
)

logger = get_logger(__name__)


class AgentSelectorScreen(ModalScreen[str | None]):
    """Modal dialog for switching between available agents.

    Displays agents found in ``~/.opscloud/`` in an ``OptionList``.
    Returns the selected agent name on Enter, or ``None`` on Esc.
    ``Ctrl+S`` toggles the highlighted agent as the persisted default.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("tab", "cursor_down", "Next", show=False, priority=True),
        Binding("shift+tab", "cursor_up", "Previous", show=False, priority=True),
        Binding("ctrl+s", "set_default", "Set default", show=False, priority=True),
    ]

    CSS = """
    AgentSelectorScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    AgentSelectorScreen > Vertical {
        width: 60;
        max-width: 90%;
        height: auto;
        max-height: 90vh;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    AgentSelectorScreen .agent-selector-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    AgentSelectorScreen .agent-selector-subtitle {
        height: auto;
        color: $foreground;
        text-align: center;
        margin-bottom: 1;
    }

    AgentSelectorScreen OptionList {
        height: auto;
        max-height: 16;
        background: $background;
    }

    AgentSelectorScreen .agent-selector-help {
        height: auto;
        color: $foreground;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(
        self,
        current_agent: str | None,
        agent_names: list[str],
        *,
        default_agent: str | None = None,
    ) -> None:
        """Initialize the AgentSelectorScreen.

        Args:
            current_agent: Name of the currently active agent.
            agent_names: Sorted list of available agent names.
            default_agent: Persisted default agent name, or None.
        """
        super().__init__()
        self._current_agent = current_agent
        self._agent_names = agent_names
        self._default_agent = default_agent

    def compose(self):
        with Vertical():
            yield Static("Select Agent", classes="agent-selector-title")
            if self._agent_names:
                yield Static(
                    "Switching restarts the agent and starts a new thread.",
                    classes="agent-selector-subtitle",
                )
                option_list = OptionList(
                    *self._build_options(),
                    id="agent-options",
                )
                option_list.highlighted = self._current_index()
                yield option_list
                help_text = (
                    "↑/↓ or Tab switch • Enter select\n"
                    "Ctrl+S set default • Esc cancel"
                )
            else:
                yield Static(
                    "No agents found in ~/.opscloud/.\n"
                    "Run opscloud with -a <name> to create one.",
                    classes="agent-selector-help",
                )
                help_text = "Esc close"
            yield Static(help_text, classes="agent-selector-help", id="agent-help")

    def _build_options(self) -> list[Option]:
        """Build option entries with (current) / (default) suffixes."""
        return [Option(self._format_label(name), id=name) for name in self._agent_names]

    def _format_label(self, name: str) -> Content:
        """Render an agent's label with (current) / (default) markers."""
        is_current = name == self._current_agent
        is_default = name == self._default_agent
        if is_current and is_default:
            return Content.from_markup(
                "$name [dim](current,[/dim] [bold]default[/bold][dim])[/dim]",
                name=name,
            )
        if is_current:
            return Content.from_markup("$name [dim](current)[/dim]", name=name)
        if is_default:
            return Content.from_markup(
                "$name [dim]([/dim][bold]default[/bold][dim])[/dim]", name=name
            )
        return Content.from_markup("$name", name=name)

    def _current_index(self) -> int:
        """Return the index of the current agent, or 0."""
        if self._current_agent is None:
            return 0
        try:
            return self._agent_names.index(self._current_agent)
        except ValueError:
            return 0

    # ── Mount ────────────────────────────────────────────

    def on_mount(self) -> None:
        """Focus the option list on mount."""
        try:
            self.query_one("#agent-options", OptionList).focus()
        except NoMatches:
            pass

    # ── Events ───────────────────────────────────────────

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Dismiss with the selected agent name."""
        name = event.option.id
        self.dismiss(name)

    def action_cancel(self) -> None:
        """Cancel without switching agents."""
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        """Move the option list cursor down (Tab)."""
        ol = self._option_list()
        if ol is not None:
            ol.action_cursor_down()

    def action_cursor_up(self) -> None:
        """Move the option list cursor up (Shift+Tab)."""
        ol = self._option_list()
        if ol is not None:
            ol.action_cursor_up()

    # ── Set Default (Ctrl+S) ─────────────────────────────

    def action_set_default(self) -> None:
        """Toggle highlighted agent as the persisted default."""
        ol = self._option_list()
        if ol is None or ol.highlighted is None:
            return
        if not (0 <= ol.highlighted < len(self._agent_names)):
            return

        highlighted = ol.highlighted
        name = self._agent_names[highlighted]

        try:
            help_widget = self.query_one("#agent-help", Static)
        except NoMatches:
            return

        if name == self._default_agent:
            # Clear default
            if not clear_default_agent():
                help_widget.update("Failed to clear default agent")
                self.set_timer(3.0, self._restore_help_text)
                return
            new_default = None
            success_msg = f"Cleared default agent (was {name})"
        else:
            # Set default
            if not save_default_agent(name):
                help_widget.update("Failed to save default agent")
                self.set_timer(3.0, self._restore_help_text)
                return
            new_default = name
            success_msg = f"Set {name} as default agent"

        # Rebuild options with new default marker
        if not self._refresh_options(ol, highlighted, new_default):
            help_widget.update("Failed to refresh agent list")
            self.set_timer(3.0, self._restore_help_text)
            return

        self._default_agent = new_default
        help_widget.update(success_msg)
        self.set_timer(3.0, self._restore_help_text)

    def _refresh_options(
        self,
        option_list: OptionList,
        highlighted: int,
        new_default: str | None,
    ) -> bool:
        """Rebuild option labels to track the new (default) state."""
        previous_default = self._default_agent
        self._default_agent = new_default
        try:
            new_options = self._build_options()
        except Exception:
            logger.exception("Failed to build new agent picker options")
            self._default_agent = previous_default
            return False

        try:
            option_list.clear_options()
            option_list.add_options(new_options)
        except Exception:
            logger.exception("Failed to mount rebuilt agent picker options")
            self._default_agent = previous_default
            with contextlib.suppress(Exception):
                option_list.clear_options()
                option_list.add_options(self._build_options())
            return False

        if 0 <= highlighted < len(self._agent_names):
            option_list.highlighted = highlighted
        self._default_agent = previous_default
        return True

    def _restore_help_text(self) -> None:
        """Restore the default help text after a transient message."""
        try:
            help_widget = self.query_one("#agent-help", Static)
        except NoMatches:
            return
        help_widget.update(
            "↑/↓ or Tab switch • Enter select\n"
            "Ctrl+S set default • Esc cancel"
        )

    def _option_list(self) -> OptionList | None:
        """Return the agent OptionList, or None if empty."""
        try:
            return self.query_one("#agent-options", OptionList)
        except NoMatches:
            return None


__all__ = ["AgentSelectorScreen"]
