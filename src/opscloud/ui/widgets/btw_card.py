"""Dedicated dismissible `/btw` side-question card widget.

Features:
- Renders above the prompt input without polluting the main conversation transcript.
- Shows ``/btw <question>``, ``Thinking...``, live streaming tokens with
  ``Generating... (Enter/Esc to cancel)``, or error state with
  ``Press Enter/Esc to dismiss``.
- Supports ``Tab to expand`` / ``Tab to collapse`` (``... (truncated)``),
  history navigation across side-question items, and ``/copy btw``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from textual.content import Content
from textual.widgets import Static

from opscloud.ui.widgets.modal_base import escape_markup as escape

_COLLAPSED_MAX_LINES = 8
_COLLAPSED_MAX_CHARS = 600


@dataclass
class BtwExchange:
    """State for a single `/btw` side question."""

    question: str
    answer: str = ""
    loading: bool = True
    error: str = ""
    expanded: bool = False


def _truncate_answer(answer: str, *, expanded: bool) -> tuple[str, bool]:
    """Return ``(display_text, is_truncated)`` for *answer*."""
    text = answer.strip()
    if not text:
        return "", False
    lines = text.splitlines()
    needs_truncation = len(lines) > _COLLAPSED_MAX_LINES or len(text) > _COLLAPSED_MAX_CHARS
    if expanded or not needs_truncation:
        return text, needs_truncation
    clipped_lines = lines[:_COLLAPSED_MAX_LINES]
    joined = "\n".join(clipped_lines)
    if len(joined) > _COLLAPSED_MAX_CHARS:
        joined = joined[:_COLLAPSED_MAX_CHARS].rstrip()
    return joined, True


class BtwCardPanel(Static):
    """Dismissible overlay card displaying `/btw` side-question exchanges above the input."""

    DEFAULT_CSS = """
    BtwCardPanel {
        height: auto;
        max-height: 22;
        overflow-y: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        background: $surface;
        border: round $primary 55%;
        color: $text;
        display: none;
    }
    """

    def __init__(self, *, id: str | None = "btw-card-panel") -> None:
        """Initialize an empty hidden `/btw` card panel."""
        super().__init__("", id=id, classes="btw-card-panel")
        self.display = False

    def render_state(
        self,
        items: Sequence[BtwExchange],
        index: int,
    ) -> None:
        """Render the active `/btw` exchange at *index* or hide when dismissed (`index < 0`)."""
        if index < 0 or index >= len(items):
            self.update("")
            self.display = False
            return

        exchange = items[index]
        total = len(items)
        counter = f" [dim]({index + 1}/{total})[/dim]" if total > 1 else ""
        header = f"[bold $primary]/btw[/bold $primary] [bold]{escape(exchange.question)}[/bold]{counter}"

        body_parts: list[str] = [header]

        if exchange.error:
            body_parts.append(f"[bold $error]Error: {escape(exchange.error)}[/bold $error]")
            footer_hints = ["Press Enter/Esc to dismiss"]
            if total > 1:
                footer_hints.insert(0, "←/→ history")
            body_parts.append(f"[dim]{' · '.join(footer_hints)}[/dim]")
        elif exchange.loading:
            display_text, is_truncated = _truncate_answer(exchange.answer, expanded=exchange.expanded)
            if display_text:
                body_parts.append(escape(display_text))
                if not exchange.expanded and is_truncated:
                    body_parts.append("[dim]... (truncated)[/dim]")
            else:
                body_parts.append("[italic dim]Thinking...[/italic dim]")
            footer_hints = ["Generating... (Enter/Esc to cancel)"]
            if display_text:
                footer_hints.insert(0, "Tab to collapse" if exchange.expanded else "Tab to expand")
            body_parts.append(f"[dim]{' · '.join(footer_hints)}[/dim]")
        else:
            display_text, is_truncated = _truncate_answer(exchange.answer, expanded=exchange.expanded)
            if display_text:
                body_parts.append(escape(display_text))
                if not exchange.expanded and is_truncated:
                    body_parts.append("[dim]... (truncated)[/dim]")
            else:
                body_parts.append("[dim](No response)[/dim]")

            footer_hints = [
                "Tab to collapse" if exchange.expanded else "Tab to expand",
                "/copy btw",
                "Enter/Esc to dismiss",
            ]
            if total > 1:
                footer_hints.insert(1, "←/→ history")
            body_parts.append(f"[dim]{' · '.join(footer_hints)}[/dim]")

        markup = "\n".join(body_parts)
        self.update(Content.from_markup(markup))
        self.display = True


__all__ = [
    "BtwCardPanel",
    "BtwExchange",
]
