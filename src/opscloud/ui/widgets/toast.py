"""Toast notification helper for OpsCloud TUI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from textual.notifications import SeverityLevel

if TYPE_CHECKING:
    from textual.app import App


@dataclass
class ToastNotification:
    id: str
    title: str
    message: str
    severity: SeverityLevel = "information"
    timestamp: float = 0.0
    dismissed: bool = False


def show_toast(
    app: App[Any],
    message: str,
    title: str = "",
    severity: SeverityLevel = "information",
) -> None:
    """Trigger a Textual notification styled with severity."""
    app.notify(message=message, title=title, severity=severity)
