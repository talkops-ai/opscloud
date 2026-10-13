"""Lightweight modal popup for displaying full skill / subagent lists.

Pushed by the WelcomeBanner when the user clicks a "…more" span.
Dismissed on Escape, Enter, or backdrop click.
"""

from __future__ import annotations

from typing import Any, ClassVar, Sequence

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.containers import VerticalScroll
from textual.widgets import Static

from opscloud.ui.widgets.modal_base import OpsModalScreen


class WelcomeDetailPopup(OpsModalScreen[None]):
    """Scrollable popup listing names + descriptions for skills or subagents."""

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS = [
        Binding("escape", "dismiss_popup", "Close", show=False),
        Binding("enter", "dismiss_popup", "Close", show=False),
    ]

    DEFAULT_CSS = """
    WelcomeDetailPopup #welcome-detail-title {
        display: none;
    }

    WelcomeDetailPopup #welcome-detail-scroll {
        height: 1fr;
        scrollbar-gutter: stable;
    }

    WelcomeDetailPopup .welcome-detail-row {
        height: auto;
        margin-bottom: 1;
    }

    WelcomeDetailPopup #welcome-detail-hint {
        display: none;
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

    def compose_modal(self) -> ComposeResult:
        yield Static(self._title, id="welcome-detail-title")
        with VerticalScroll(id="welcome-detail-scroll"):
            for name, desc in self._items:
                desc_part = f"  [dim]—  {escape(desc)}[/dim]" if desc else ""
                yield Static(
                    Content.from_markup(f"  [bold $primary]{escape(name)}[/bold $primary]{desc_part}"),
                    classes="welcome-detail-row",
                )
        yield Static("", id="welcome-detail-hint")

    def on_mount(self) -> None:
        super().on_mount()
        self.set_modal_title(self._title, counter=f"{len(self._items)} items")
        self.set_modal_subtitle([
            ("enter", "close"),
            ("esc", "close"),
        ])

    def action_dismiss_popup(self) -> None:
        self.dismiss(None)
