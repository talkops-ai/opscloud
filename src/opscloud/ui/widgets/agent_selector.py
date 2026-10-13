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

from opscloud.config.settings import get_glyphs
from opscloud.ui.widgets.modal_base import OpsModalScreen, format_modal_option_row

logger = get_logger(__name__)


class AgentSelectorScreen(OpsModalScreen[str | None]):
    """Modal dialog for switching between available agents.

    Displays agents found in ``~/.opscloud/`` in an ``OptionList``.
    Returns the selected agent name on Enter, or ``None`` on Esc.
    ``Ctrl+S`` toggles the highlighted agent as the persisted default.
    """

    DIALOG_CLASSES: ClassVar[str] = ""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("tab", "cursor_down", "Next", show=False, priority=True),
        Binding("shift+tab", "cursor_up", "Previous", show=False, priority=True),
        Binding("ctrl+s", "set_default", "Set default", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    AgentSelectorScreen .agent-selector-title {
        display: none;
    }

    AgentSelectorScreen .agent-selector-subtitle {
        height: auto;
        color: $text-muted;
        text-align: center;
        margin-bottom: 1;
    }

    AgentSelectorScreen OptionList {
        height: auto;
        max-height: 16;
        background: $background;
        border: solid $primary 20%;
    }

    AgentSelectorScreen OptionList > .option-list--option-highlighted {
        background: $primary 18%;
        color: $text;
        text-style: bold;
    }

    AgentSelectorScreen .agent-selector-help {
        display: none;
    }

    AgentSelectorScreen .agent-selector-empty {
        display: block;
        height: auto;
        color: $text-muted;
        text-align: center;
        padding: 1;
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

    def compose_modal(self):
        yield Static("Select Agent", classes="agent-selector-title")
        if self._agent_names:
            yield Static(
                "Select an agent persona to activate in this session.",
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
                "No agents found in ~/.opscloud/ or plugins.\n"
                "Run opscloud with -a <name> to create one.",
                classes="agent-selector-empty",
            )
            help_text = "Esc close"
        yield Static(help_text, classes="agent-selector-help", id="agent-help")

    def on_mount(self) -> None:
        super().on_mount()
        glyphs = get_glyphs()
        if self._agent_names:
            self.set_modal_title("Select Agent", counter=f"{len(self._agent_names)} available")
            self.set_modal_subtitle([
                (f"{glyphs.arrow_up}{glyphs.arrow_down}", "navigate"),
                ("enter", "select"),
                ("ctrl+s", "set default"),
                ("esc", "cancel"),
            ])
            try:
                self.query_one("#agent-options", OptionList).focus()
            except NoMatches:
                pass
            self.call_after_refresh(self._reformat_options)
        else:
            self.set_modal_title("Select Agent")
            self.set_modal_subtitle([("esc", "close")])

    def on_resize(self) -> None:
        self.call_after_refresh(self._reformat_options)

    def _row_width(self) -> int:
        try:
            ol = self.query_one("#agent-options", OptionList)
            region_w = ol.scrollable_content_region.width
            if region_w > 0:
                return region_w
        except Exception:
            pass
        if self.dialog is not None and self.dialog.region.width > 0:
            return max(32, self.dialog.region.width - 6)
        return 72

    def _reformat_options(self) -> None:
        try:
            ol = self.query_one("#agent-options", OptionList)
        except NoMatches:
            return
        if not self._agent_names:
            return
        highlighted = ol.highlighted if ol.highlighted is not None else 0
        new_options = self._build_options()
        ol.clear_options()
        ol.add_options(new_options)
        if 0 <= highlighted < len(self._agent_names):
            ol.highlighted = highlighted

    def _build_options(self, width: int | None = None) -> list[Option]:
        """Build option entries with badges for current and default status."""
        options: list[Option] = []
        w = width if width is not None else self._row_width()
        for idx, name in enumerate(self._agent_names):
            badges: list[tuple[str, str]] = []
            if name == self._current_agent:
                badges.append(("ACTIVE", "bold $success"))
            if name == self._default_agent:
                badges.append(("DEFAULT", "bold $primary"))

            is_selected = (idx == self._current_index())
            content = format_modal_option_row(
                label=name,
                badges=badges if badges else None,
                width=w,
                is_selected=is_selected,
            )
            options.append(Option(content, id=name))
        return options

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
