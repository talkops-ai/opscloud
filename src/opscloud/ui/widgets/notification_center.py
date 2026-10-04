"""Notification center panel for OpsCloud TUI.

Lists history of notifications, toasts, and warnings with dismiss and suppress options.
"""

from __future__ import annotations

import time
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Static

from textual.notifications import SeverityLevel
from opscloud.ui.widgets.toast import ToastNotification


class NotificationCenter(Widget):
    """Overlay panel displaying session notifications."""

    DEFAULT_CSS = """
    NotificationCenter {
        layer: overlay;
        dock: right;
        width: 45;
        height: 100%;
        background: $surface;
        border-left: tall $panel;
        padding: 1;
    }
    NotificationCenter .header {
        color: $primary;
        text-style: bold;
        margin-bottom: 1;
    }
    NotificationCenter .item {
        padding: 1;
        margin-bottom: 1;
        background: $background;
        border: solid $panel;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_panel", "Close", show=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._notifications: list[ToastNotification] = []
        self._suppressed: set[SeverityLevel] = set()

    def add_notification(
        self, title: str, message: str, severity: SeverityLevel = "information"
    ) -> ToastNotification | None:
        if severity in self._suppressed:
            return None
        item = ToastNotification(
            id=str(len(self._notifications) + 1),
            title=title,
            message=message,
            severity=severity,
            timestamp=time.time(),
        )
        self._notifications.append(item)
        return item

    def dismiss_notification(self, item_id: str) -> None:
        """Mark notification as dismissed and remove."""
        for item in self._notifications:
            if item.id == item_id:
                item.dismissed = True
        self._notifications = [n for n in self._notifications if not n.dismissed]

    def suppress_severity(self, severity: SeverityLevel) -> None:
        """Suppress notifications of specified severity level."""
        self._suppressed.add(severity)

    def compose(self):
        yield Static("🔔 Notifications & Alerts", classes="header")
        with VerticalScroll():
            if not self._notifications:
                yield Static("No notifications.", classes="muted")
            for item in self._notifications:
                icon = "ℹ️"
                if item.severity == "success":
                    icon = "✅"
                elif item.severity == "warning":
                    icon = "⚠️"
                elif item.severity == "error":
                    icon = "❌"

                content = Text(f"{icon} {item.title or 'Notification'}\n", style="bold")
                content.append(item.message, style="dim")
                yield Static(content, classes="item")

        yield Button("Close (Esc)", variant="primary", id="btn-close")

    @on(Button.Pressed, "#btn-close")
    def action_dismiss_panel(self) -> None:
        self.remove()


__all__ = ["NotificationCenter"]
