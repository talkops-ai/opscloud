"""Loading widget with animated spinner for agent activity."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, Any

from textual.content import Content
from textual.widgets import Static

if TYPE_CHECKING:
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
    """Animated one-line activity indicator shown while the agent works.

    Displays: ``⠹ Thinking · 12s · esc to interrupt``

    * A soft highlight sweeps across the status label (shimmer) so the line
      feels alive without being loud.
    * ``"Subagent: aws-security"``-style statuses render as
      ``Subagent · aws-security``.
    * Elapsed time counts from ``started_at`` (the turn start) when given,
      so it keeps running across tool calls instead of resetting.
    * Paused (awaiting an approval) switches to amber, the same
      "needs attention" colour used elsewhere.
    * Hidden while a live ``ThinkingMessage`` is streaming, since that block
      already shows the spinner, label and elapsed time.
    """

    DEFAULT_CSS = """
    LoadingWidget {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
    }
    """

    _TICK_SECONDS = 0.1
    _SHIMMER_WIDTH = 3
    _SHIMMER_GAP = 4

    def __init__(
        self,
        status: str = "Thinking",
        *,
        started_at: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._status = status
        self._spinner = Spinner()
        self._start_time: float | None = started_at
        self._animation_timer: Timer | None = None
        self._paused = False
        self._paused_elapsed: float = 0.0
        self._shimmer_pos = 0

    @property
    def status(self) -> str:
        """Current status label (e.g. ``"Thinking"``)."""
        return self._status

    @property
    def paused(self) -> bool:
        """Whether the indicator is paused awaiting a decision."""
        return self._paused

    def on_mount(self) -> None:
        if self._start_time is None:
            self._start_time = monotonic()
        self._animation_timer = self.set_interval(self._TICK_SECONDS, self._update_animation)

    def on_unmount(self) -> None:
        self._stop_timer()

    def remove(self) -> AwaitRemove:
        self._stop_timer()
        return super().remove()

    def _stop_timer(self) -> None:
        if self._animation_timer is not None:
            self._animation_timer.stop()
            self._animation_timer = None

    def _thinking_block_is_live(self) -> bool:
        current = getattr(self.parent, "_current_thinking", None)
        return bool(current is not None and getattr(current, "is_live", False))

    def _update_animation(self) -> None:
        hide = self._thinking_block_is_live() and not self._paused
        if self.display == hide:
            self.display = not hide
        if self._paused or hide:
            return
        self._spinner.next_frame()
        self._shimmer_pos += 1
        self.refresh()

    def _elapsed_seconds(self) -> int:
        if self._paused:
            return int(self._paused_elapsed)
        if self._start_time is None:
            return 0
        return max(0, int(monotonic() - self._start_time))

    def _shimmer(self, label: str) -> list[tuple[str, str]]:
        """Style each character of ``label`` relative to the sweeping highlight."""
        cycle = len(label) + self._SHIMMER_GAP
        center = self._shimmer_pos % cycle
        half = self._SHIMMER_WIDTH // 2
        parts: list[tuple[str, str]] = []
        for i, ch in enumerate(label):
            distance = abs(i - center)
            if distance == 0:
                style = "bold $foreground"
            elif distance <= half:
                style = "bold $primary"
            else:
                style = "$primary"
            parts.append((ch, style))
        return parts

    def render(self) -> Content:
        sep = "\u00b7"  # ·
        elapsed = format_duration(self._elapsed_seconds())

        if self._paused:
            return Content.assemble(
                ("\u23f8 ", "$warning"),  # ⏸
                (self._status, "bold $warning"),
                (f" {sep} paused at {elapsed}", "dim"),
            )

        label, _, detail = self._status.partition(": ")
        parts: list[tuple[str, str]] = [(f"{self._spinner.current_frame()} ", "$primary")]
        parts.extend(self._shimmer(label))
        if detail:
            parts.append((f" {sep} {detail}", "$primary"))
        parts.append((f" {sep} {elapsed} {sep} ", "dim"))
        parts.append(("esc", "dim bold"))
        parts.append((" to interrupt", "dim"))
        return Content.assemble(*parts)

    def set_status(self, status: str) -> None:
        self._status = status
        self.refresh()

    def pause(self, status: str = "Awaiting decision") -> None:
        if not self._paused and self._start_time is not None:
            self._paused_elapsed = monotonic() - self._start_time
        self._paused = True
        self._status = status
        self.display = True
        self.refresh()

    def resume(self) -> None:
        if not self._paused:
            return
        self._start_time = monotonic() - self._paused_elapsed
        self._paused = False
        self._status = "Thinking"
        self.refresh()

    def stop(self) -> None:
        self._stop_timer()
