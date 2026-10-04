"""OperationCard widget for tracking long-running DevOps background operations.

Displays operation status, elapsed timer, live log stream, and cancellation controls.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Static


class OperationCard(Widget):
    """Card widget tracking background / long-running operations."""

    DEFAULT_CSS = """
    OperationCard {
        padding: 1;
        margin: 1 0 0 2;
        background: $surface;
        border: solid $warning;
    }
    OperationCard .header {
        color: $warning;
        text-style: bold;
        margin-bottom: 1;
    }
    OperationCard VerticalScroll {
        height: 6;
        background: $background;
        border: solid $panel;
    }
    """

    class CancelRequested(Message):
        """Fired when user clicks cancel button."""

        def __init__(self, operation_id: str) -> None:
            super().__init__()
            self.operation_id = operation_id

    def __init__(self, operation_id: str, name: str, env: str = "non-prod", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.operation_id = operation_id
        self.op_name = name
        self.env = env
        self._start_time = time.time()
        self._status = "running"
        self._logs: list[str] = []

    def compose(self):
        title = Text(f"⚡ Operation: {self.op_name} [{self.env}]", style="bold amber")
        yield Static(title, classes="header", id=f"hdr-{self.operation_id}")

        with VerticalScroll(id=f"log-{self.operation_id}"):
            yield Static("Log output starting...", classes="muted")

        yield Button("Cancel Operation", variant="error", id=f"btn-cancel-{self.operation_id}")

    def on_mount(self) -> None:
        """Start periodic timer tick for elapsed time update."""
        self.set_interval(1.0, self._update_timer)

    def _update_timer(self) -> None:
        if self._status == "running" and self.is_mounted:
            elapsed = int(time.time() - self._start_time)
            mins, secs = divmod(elapsed, 60)
            hdr = self.query_one(f"#hdr-{self.operation_id}", Static)
            title = Text(f"⚡ Operation: {self.op_name} [{self.env}] ⏱️ {mins:02d}:{secs:02d}", style="bold warning")
            hdr.update(title)

    def append_log(self, line: str) -> None:
        """Append log line to streamed viewer."""
        self._logs.append(line)
        if self.is_mounted:
            log_scroll = self.query_one(f"#log-{self.operation_id}", VerticalScroll)
            style = "green" if "OK" in line or "SUCCESS" in line else "red" if "ERROR" in line or "FAIL" in line else "dim"
            log_scroll.mount(Static(Text(line, style=style)))

    def set_complete(self, success: bool = True) -> None:
        """Mark operation complete and notify user."""
        self._status = "success" if success else "error"
        elapsed = int(time.time() - self._start_time)
        if self.is_mounted:
            hdr = self.query_one(f"#hdr-{self.operation_id}", Static)
            icon = "✓" if success else "✗"
            style = "bold green" if success else "bold red"
            hdr.update(Text(f"{icon} Operation: {self.op_name} ({self._status}) ⏱️ {elapsed}s", style=style))
            if self.app is not None:
                msg = f"Operation '{self.op_name}' completed in {elapsed}s"
                severity = "information" if success else "error"
                self.app.notify(msg, severity=severity)



    @on(Button.Pressed)
    def _on_button(self, event: Button.Pressed) -> None:
        if "btn-cancel" in (event.button.id or ""):
            self.post_message(self.CancelRequested(self.operation_id))
