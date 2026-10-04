"""Lightweight modal popup for displaying full skill / subagent lists.

Pushed by the WelcomeBanner when the user clicks a "…more" span.
Dismissed on Escape, Enter, or backdrop click.
"""

from __future__ import annotations

from typing import Any, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static


class WelcomeDetailPopup(ModalScreen[None]):
    """Scrollable popup listing names + descriptions for skills or subagents."""

    BINDINGS = [
        Binding("escape", "dismiss_popup", "Close", show=False),
        Binding("enter", "dismiss_popup", "Close", show=False),
    ]

    DEFAULT_CSS = """
    WelcomeDetailPopup {
        align: center middle;
        background: rgba(0, 0, 0, 0.70);
    }

    WelcomeDetailPopup #welcome-detail-outer {
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }

    WelcomeDetailPopup #welcome-detail-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
        height: auto;
    }

    WelcomeDetailPopup #welcome-detail-scroll {
        height: auto;
        max-height: 24;
        scrollbar-size: 1 1;
    }

    WelcomeDetailPopup .welcome-detail-row {
        height: auto;
        margin-bottom: 1;
    }

    WelcomeDetailPopup #welcome-detail-hint {
        height: auto;
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        title: str,
        items: Sequence[tuple[str, str]],
        **kwargs: Any,
    ) -> None:
        """
        Args:
            title: Popup heading, e.g. "Skills" or "Subagents".
            items: Sequence of (name, description) pairs.
        """
        super().__init__(**kwargs)
        self._title = title
        self._items = list(items)

    def compose(self) -> ComposeResult:
        with Vertical(id="welcome-detail-outer"):
            yield Static(self._title, id="welcome-detail-title")
            with VerticalScroll(id="welcome-detail-scroll"):
                for name, desc in self._items:
                    text = Text()
                    text.append(f"  {name}", style="bold cyan")
                    if desc:
                        text.append(f"  —  {desc}", style="dim")
                    yield Static(text, classes="welcome-detail-row")
            hint = Text()
            hint.append("\n  Press ", style="dim")
            hint.append("Esc", style="bold")
            hint.append(" or ", style="dim")
            hint.append("Enter", style="bold")
            hint.append(" to close", style="dim")
            yield Static(hint, id="welcome-detail-hint")

    def action_dismiss_popup(self) -> None:
        self.dismiss(None)
