"""Notification settings modal screen for `/notifications` command."""

from __future__ import annotations

import asyncio
from opscloud.utils.logger import get_logger
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import VerticalGroup
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult

logger = get_logger(__name__)

WARNING_TOGGLES: list[tuple[str, str]] = [
    ("ripgrep", "Warn when ripgrep is not installed"),
    ("tavily", "Warn when TAVILY_API_KEY is not set (web search)"),
    ("docker", "Warn when Docker engine is not running"),
    ("kubectl", "Warn when kubectl CLI is not found"),
    ("terraform", "Warn when Terraform / OpenTofu is not installed"),
    ("ansible", "Warn when Ansible is not installed"),
]


class NotificationSettingsScreen(ModalScreen[None]):
    """Modal dialog for managing startup warning preferences.

    Toggling a checkbox enables or suppresses startup warning notices.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("up", "app.focus_previous", "Previous", show=False, priority=True),
        Binding("down", "app.focus_next", "Next", show=False, priority=True),
        Binding("tab", "app.focus_next", "Next", show=False, priority=True),
        Binding("shift+tab", "app.focus_previous", "Previous", show=False, priority=True),
    ]

    CSS = """
    NotificationSettingsScreen {
        align: center middle;
        background: $background 60%;
    }

    NotificationSettingsScreen > VerticalGroup {
        width: 65;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    NotificationSettingsScreen .ns-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    NotificationSettingsScreen .ns-help {
        height: 1;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }

    NotificationSettingsScreen Checkbox {
        margin: 0;
        border: none;
    }
    """

    def __init__(self, suppressed: set[str] | None = None) -> None:
        super().__init__()
        self._suppressed: set[str] = suppressed or set()

    def compose(self) -> ComposeResult:
        with VerticalGroup():
            yield Static("Notification Settings", classes="ns-title")
            for key, label in WARNING_TOGGLES:
                yield Checkbox(
                    label,
                    value=key not in self._suppressed,
                    id=f"ns-{key}",
                )
            yield Static("↑/↓ or Tab navigate • Space toggle • Esc close", classes="ns-help")

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        checkbox_id = event.checkbox.id
        if not checkbox_id or not checkbox_id.startswith("ns-"):
            return
        key = checkbox_id.removeprefix("ns-")
        enabled = event.value

        from opscloud.model.config import suppress_warning, unsuppress_warning

        if enabled:
            self._suppressed.discard(key)
            unsuppress_warning(key)
        else:
            self._suppressed.add(key)
            suppress_warning(key)

        if hasattr(self.app, "_suppressed_warnings"):
            setattr(self.app, "_suppressed_warnings", self._suppressed)

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["NotificationSettingsScreen", "WARNING_TOGGLES"]
