"""Loading widget with animated spinner for agent activity."""

from __future__ import annotations

from time import time
from typing import TYPE_CHECKING, Any

from textual.containers import Horizontal
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.await_remove import AwaitRemove
    from textual.timer import Timer

BRAILLE_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def format_duration(seconds: int) -> str:
    """Format duration in seconds to human-readable string (e.g. 5s, 2m 10s)."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    rem_seconds = seconds % 60
    if rem_seconds == 0:
        return f"{minutes}m"
    return f"{minutes}m {rem_seconds}s"


class Spinner:
    """Animated spinner using braille charset frames."""

    def __init__(self) -> None:
        self._position = 0

    @property
    def frames(self) -> tuple[str, ...]:
        return BRAILLE_SPINNER_FRAMES

    def next_frame(self) -> str:
        frames = self.frames
        frame = frames[self._position]
        self._position = (self._position + 1) % len(frames)
        return frame

    def current_frame(self) -> str:
        return self.frames[self._position]


class LoadingWidget(Static):
    """Animated loading indicator with status text and elapsed time.

    Displays: <spinner> Thinking... (3s, esc to interrupt)
    """

    DEFAULT_CSS = """
    LoadingWidget {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    LoadingWidget .loading-container {
        height: auto;
        width: 100%;
    }

    LoadingWidget .loading-spinner {
        width: auto;
        color: $primary;
    }

    LoadingWidget .loading-status {
        width: auto;
        color: $primary;
    }

    LoadingWidget .loading-hint {
        width: auto;
        color: $text-muted;
        margin-left: 1;
    }
    """

    def __init__(self, status: str = "Thinking", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._status = status
        self._spinner = Spinner()
        self._start_time: float | None = None
        self._spinner_widget: Static | None = None
        self._status_widget: Static | None = None
        self._hint_widget: Static | None = None
        self._animation_timer: Timer | None = None
        self._paused = False
        self._paused_elapsed: float = 0.0

    def compose(self) -> ComposeResult:
        with Horizontal(classes="loading-container"):
            self._spinner_widget = Static(
                self._spinner.current_frame(), classes="loading-spinner"
            )
            yield self._spinner_widget

            self._status_widget = Static(
                f" {self._status}... ", classes="loading-status"
            )
            yield self._status_widget

            self._hint_widget = Static("(0s, esc to interrupt)", classes="loading-hint")
            yield self._hint_widget

    def on_mount(self) -> None:
        if self._start_time is None:
            self._start_time = time()
        self._animation_timer = self.set_interval(0.1, self._update_animation)

    def on_unmount(self) -> None:
        self._stop_timer()

    def remove(self) -> AwaitRemove:
        self._stop_timer()
        return super().remove()

    def _stop_timer(self) -> None:
        if self._animation_timer is not None:
            self._animation_timer.stop()
            self._animation_timer = None

    def _update_animation(self) -> None:
        if self._paused:
            return

        if self._spinner_widget:
            frame = self._spinner.next_frame()
            self._spinner_widget.update(frame)

        if self._hint_widget and self._start_time is not None:
            elapsed = int(time() - self._start_time)
            self._hint_widget.update(f"({format_duration(elapsed)}, esc to interrupt)")

    def set_status(self, status: str) -> None:
        self._status = status
        if self._status_widget:
            self._status_widget.update(f" {self._status}... ")

    def pause(self, status: str = "Awaiting decision") -> None:
        self._paused = True
        if self._start_time is not None:
            self._paused_elapsed = time() - self._start_time
        self._status = status
        if self._status_widget:
            self._status_widget.update(f" {status}... ")
        if self._hint_widget:
            self._hint_widget.update(
                f"(paused at {format_duration(int(self._paused_elapsed))})"
            )
        if self._spinner_widget:
            self._spinner_widget.update("⏸")

    def resume(self) -> None:
        if not self._paused:
            return
        self._start_time = time() - self._paused_elapsed
        self._paused = False
        self._status = "Thinking"
        if self._status_widget:
            self._status_widget.update(f" {self._status}... ")

    def stop(self) -> None:
        self._stop_timer()
