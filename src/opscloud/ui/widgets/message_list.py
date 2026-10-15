"""Scrollable message transcript viewport (`MessageList`) and wheel-scroll acceleration.

Extracted from `opscloud.ui.widgets.messages` (Phase 5 UI modularization).
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any, ClassVar

from textual import events
from textual.containers import VerticalScroll

from opscloud.ui.widgets.chat_messages import (
    AssistantMessage,
    DiffMessage,
    QueuedUserMessage,
    ThinkingMessage,
    UserMessage,
)
from opscloud.ui.widgets.status_messages import (
    ErrorMessage,
    SkillMessage,
    SystemMessage,
)
from opscloud.ui.widgets.tool_call_message import (
    ToolCallMessage,
    _TOOL_GROUP_EXCLUSIONS,
)
from opscloud.ui.widgets.tool_group import ToolGroupSummary
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from textual.widget import Widget

logger = get_logger(__name__)


def compute_wheel_speed(streak: int, viewport_height: int = 0) -> int:
    """Compute sub-linear scroll lines per wheel event.

    Uses ``min(12, 1 + isqrt(streak))`` clamped to ``max(1, viewport_height // 3)``
    when ``viewport_height > 0``.
    """
    speed = min(12, 1 + math.isqrt(max(0, streak)))
    if viewport_height > 0:
        speed = min(speed, max(1, viewport_height // 3))
    return max(1, speed)


class MessageList(VerticalScroll):
    """Scrollable list of conversation messages with auto-scroll lock controls."""

    DEFAULT_CSS = """
    MessageList {
        height: 1fr;
    }
    """

    _WHEEL_STREAK_WINDOW_S: ClassVar[float] = 0.150
    """Max interval (150ms) between same-direction wheel events to build streak."""

    _SCROLL_COALESCE_INTERVAL_S: ClassVar[float] = 0.016
    """16ms (60fps) frame budget for coalescing burst wheel scroll deltas."""

    _HOVER_SUPPRESS_WINDOW_S: ClassVar[float] = 0.120
    """Suppress post-refresh hover hit-testing while actively scrolling."""

    _STICKY_BOTTOM_THRESHOLD: ClassVar[float] = 2.0
    """Distance from max_scroll_y within which downward scroll re-locks auto-scroll."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._current_assistant: AssistantMessage | None = None
        self._current_thinking: ThinkingMessage | None = None
        self._accumulated_thinking: str = ""
        self._tool_calls: dict[str, ToolCallMessage] = {}
        self._tool_call_total: int = 0
        """Monotonic count of tool-call widgets created (per-turn deltas)."""
        self._active_tool_group: ToolGroupSummary | None = None
        self._auto_scroll_locked = True
        self._scroll_end_pending = False
        self._last_wheel_time: float = 0.0
        self._last_wheel_dir: int = 0
        self._wheel_streak: int = 0
        self._pending_scroll_delta: int = 0
        self._last_scroll_flush_time: float = 0.0
        self._scroll_flush_timer: Any = None
        self._hover_settle_timer: Any = None

    def on_unmount(self) -> None:
        """Stop any pending scroll coalescer or hover settle timers."""
        self._stop_scroll_timers()

    def _stop_scroll_timers(self) -> None:
        if self._scroll_flush_timer is not None:
            self._scroll_flush_timer.stop()
            self._scroll_flush_timer = None
        if self._hover_settle_timer is not None:
            self._hover_settle_timer.stop()
            self._hover_settle_timer = None

    def _mount_child(self, widget: Widget, before: Widget | None = None) -> None:
        """Mount child widget, ensuring it mounts before the bottom loading widget if active."""
        if before is not None:
            self.mount(widget, before=before)
            return
        loading = (
            getattr(self.app, "_loading_widget", None)
            if self.is_attached and self.app
            else None
        )
        if (
            loading is not None
            and getattr(loading, "is_attached", False)
            and loading.parent is self
            and widget is not loading
        ):
            self.mount(widget, before=loading)
        else:
            self.mount(widget)

    def on_scroll_up(self) -> None:
        """Suspend auto-scroll when user manually scrolls up."""
        self._auto_scroll_locked = False
        if self._anchored and not self._anchor_released:
            self.release_anchor()

    def _mark_user_scrolling(self) -> None:
        """Suppress post-refresh hover hit-testing while a scroll gesture is active."""
        if not self.is_attached or self.app is None:
            return
        setattr(
            self.app,
            "_opscloud_scroll_suppress_hover_until",
            time.monotonic() + self._HOVER_SUPPRESS_WINDOW_S,
        )
        if self._hover_settle_timer is not None:
            self._hover_settle_timer.stop()
        self._hover_settle_timer = self.set_timer(
            self._HOVER_SUPPRESS_WINDOW_S, self._on_scroll_hover_settled
        )

    def _on_scroll_hover_settled(self) -> None:
        """Restore hover hit-testing once the scroll gesture settles."""
        self._hover_settle_timer = None
        if not self.is_attached or self.app is None:
            return
        setattr(self.app, "_opscloud_scroll_suppress_hover_until", 0.0)
        try:
            self.app._update_mouse_over(self.screen)
        except Exception:
            pass

    def _next_wheel_speed(self, direction: int, now: float | None = None) -> int:
        """Compute accelerated wheel scroll lines for ``direction`` (-1 up, +1 down)."""
        ts = time.monotonic() if now is None else now
        if (
            self._last_wheel_dir == direction
            and (ts - self._last_wheel_time) < self._WHEEL_STREAK_WINDOW_S
        ):
            self._wheel_streak += 1
        else:
            self._wheel_streak = 0
        self._last_wheel_time = ts
        self._last_wheel_dir = direction
        vh = self.scrollable_content_region.height if self.is_attached else 0
        return compute_wheel_speed(self._wheel_streak, vh)

    def _queue_wheel_scroll(self, delta: int, now: float | None = None) -> None:
        """Coalesce wheel deltas within a 16ms frame budget."""
        if delta == 0:
            return
        ts = time.monotonic() if now is None else now
        if delta < 0:
            self.on_scroll_up()
        self._mark_user_scrolling()
        if (self._pending_scroll_delta > 0 and delta < 0) or (
            self._pending_scroll_delta < 0 and delta > 0
        ):
            self._pending_scroll_delta = delta
        else:
            self._pending_scroll_delta += delta

        if not self.is_attached:
            self._flush_pending_scroll()
            return

        if (
            self._scroll_flush_timer is None
            and (ts - self._last_scroll_flush_time) >= self._SCROLL_COALESCE_INTERVAL_S
        ):
            self._flush_pending_scroll()
        elif self._scroll_flush_timer is None:
            self._scroll_flush_timer = self.set_timer(
                self._SCROLL_COALESCE_INTERVAL_S, self._flush_pending_scroll
            )

    def _flush_pending_scroll(self) -> None:
        """Flush accumulated wheel delta in a single immediate compositor scroll."""
        if self._scroll_flush_timer is not None:
            self._scroll_flush_timer.stop()
            self._scroll_flush_timer = None
        delta = self._pending_scroll_delta
        self._pending_scroll_delta = 0
        if delta == 0:
            return
        self._last_scroll_flush_time = time.monotonic()
        target_y = max(
            0.0, min(float(self.max_scroll_y), float(self.scroll_y) + delta)
        )
        self.scroll_to(y=target_y, animate=False, immediate=True)
        if delta > 0 and (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = True
        elif delta < 0 and (self.max_scroll_y - self.scroll_y) > self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = False

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        """Handle mouse/trackpad scroll up with WheelSpeed + 16ms coalescing."""
        if not self.allow_vertical_scroll:
            return
        self.on_scroll_up()
        if self.scroll_y > 0 or self._pending_scroll_delta > 0:
            speed = self._next_wheel_speed(-1)
            self._queue_wheel_scroll(-speed)
            event.stop()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        """Handle mouse/trackpad scroll down with WheelSpeed + 16ms coalescing."""
        if not self.allow_vertical_scroll:
            return
        if self.scroll_y < self.max_scroll_y or self._pending_scroll_delta < 0:
            speed = self._next_wheel_speed(1)
            self._queue_wheel_scroll(speed)
            event.stop()
        else:
            self._auto_scroll_locked = True

    def action_scroll_up(self) -> None:
        """Scroll up one line immediately and unlock auto-scroll."""
        if not self.allow_vertical_scroll:
            return
        self.on_scroll_up()
        self._mark_user_scrolling()
        self.scroll_up(animate=False, immediate=True)

    def action_scroll_down(self) -> None:
        """Scroll down one line immediately and re-lock if at bottom."""
        if not self.allow_vertical_scroll:
            return
        self._mark_user_scrolling()
        self.scroll_down(animate=False, immediate=True)
        if (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = True

    def action_page_up(self) -> None:
        """Scroll one page up without animation jitter and unlock auto-scroll."""
        if not self.allow_vertical_scroll:
            return
        self.on_scroll_up()
        self._mark_user_scrolling()
        self.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        """Scroll one page down without animation jitter and re-lock at bottom."""
        if not self.allow_vertical_scroll:
            return
        self._mark_user_scrolling()
        self.scroll_page_down(animate=False)
        if (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = True

    def action_scroll_home(self) -> None:
        """Jump to top of transcript and unlock auto-scroll."""
        if not self._allow_scroll:
            return
        self.on_scroll_up()
        self._mark_user_scrolling()
        self.scroll_home(animate=False, immediate=True, x_axis=False)

    def action_scroll_end(self) -> None:
        """Jump to bottom of transcript and re-lock auto-scroll."""
        if not self._allow_scroll:
            return
        self._auto_scroll_locked = True
        self._mark_user_scrolling()
        self.scroll_end(animate=False, immediate=True, x_axis=False)

    def _on_scroll_up(self, event: Any) -> None:
        """Handle scrollbar track page-up click."""
        if self.allow_vertical_scroll:
            self.on_scroll_up()
            self._mark_user_scrolling()
            self.scroll_page_up(animate=False)
            event.stop()

    def _on_scroll_down(self, event: Any) -> None:
        """Handle scrollbar track page-down click."""
        if self.allow_vertical_scroll:
            self._mark_user_scrolling()
            self.scroll_page_down(animate=False)
            if (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
                self._auto_scroll_locked = True
            event.stop()

    def _on_scroll_to(self, message: Any) -> None:
        """Handle scrollbar thumb drag without 0.1s animation lag."""
        if self._allow_scroll:
            self._mark_user_scrolling()
            if message.y is not None:
                if message.y < self.scroll_y:
                    self.on_scroll_up()
                elif (self.max_scroll_y - message.y) <= self._STICKY_BOTTOM_THRESHOLD:
                    self._auto_scroll_locked = True
            self.scroll_to(message.x, message.y, animate=False, immediate=True)
            message.stop()

    async def mount_inline_prompt(self, widget: Widget) -> None:
        """Mount an inline prompt widget into the message transcript area."""
        loading = (
            getattr(self.app, "_loading_widget", None)
            if self.is_attached and self.app
            else None
        )
        if (
            loading is not None
            and getattr(loading, "is_attached", False)
            and loading.parent is self
            and widget is not loading
        ):
            await self.mount(widget, before=loading)
        else:
            await self.mount(widget)
        if self._auto_scroll_locked:
            self._scroll_to_end()

    def add_thinking_message(
        self, content: str = "", duration_seconds: float = 0.0, *, live: bool = False
    ) -> ThinkingMessage:
        """Add a thinking message widget to the conversation."""
        self.close_active_tool_group()
        msg = ThinkingMessage(
            content=content, duration_seconds=duration_seconds, live=live
        )
        self._mount_child(msg)
        if self._auto_scroll_locked:
            self._scroll_to_end()
        return msg

    def append_thinking_token(
        self, token: str, duration_seconds: float = 0.0
    ) -> None:
        """Append a token to the current (live) thinking message."""
        if self._current_thinking is None:
            self._current_thinking = self.add_thinking_message(
                "", duration_seconds, live=True
            )
            self._accumulated_thinking = ""
        self._accumulated_thinking += token
        self._current_thinking.update_thinking(
            self._accumulated_thinking, duration_seconds
        )
        if self._auto_scroll_locked:
            self._scroll_to_end()

    def _end_thinking(self) -> None:
        """Collapse the live thinking block, if any."""
        current = self._current_thinking
        self._current_thinking = None
        if current is not None:
            try:
                current.finish()
            except Exception:
                logger.exception("Failed to finish thinking block")

    def close_active_tool_group(self) -> None:
        """Close the active inline tool group so subsequent tool calls form a new group."""
        group = self._active_tool_group
        self._active_tool_group = None
        if group is not None and getattr(group, "is_attached", False):
            try:
                group.close()
            except Exception:
                logger.exception("Failed to close active tool group")

    def reveal_pending_tool_calls(self) -> None:
        """Surface grouped tool calls before asking for approval."""
        group = self._active_tool_group
        self.close_active_tool_group()
        if group is None or not getattr(group, "is_attached", False):
            return
        try:
            group.reveal_pending()
        except Exception:
            logger.exception("Failed to reveal pending tool calls")

    async def regroup_completed_tools(self) -> None:
        """Fold runs of completed tool calls into collapsible group summaries."""
        self.close_active_tool_group()

        run_tools: list[ToolCallMessage] = []
        run_collapsible: list[Widget] = []
        run_anchor: Widget | None = None
        folded_runs_count = 0

        async def flush() -> None:
            nonlocal run_tools, run_collapsible, run_anchor, folded_runs_count
            if (
                run_tools
                and run_anchor is not None
                and getattr(run_anchor, "is_attached", False)
            ):
                summary = ToolGroupSummary(
                    tools=list(run_tools),
                    collapsible=list(run_collapsible),
                    live=False,
                )
                for widget in run_collapsible:
                    widget.add_class("-grouped")
                try:
                    await self.mount(summary, before=run_anchor)
                    folded_runs_count += 1
                except Exception:
                    logger.warning("Failed to mount tool group summary", exc_info=True)
            run_tools = []
            run_collapsible = []
            run_anchor = None

        from contextlib import nullcontext

        batch_ctx = (
            self.app.batch_update()
            if (self.is_attached and self.app is not None)
            else nullcontext()
        )
        with batch_ctx:
            for child in list(self.children):
                if isinstance(child, ToolGroupSummary):
                    await flush()
                    if not child._finalized:
                        child.close()
                    continue
                if isinstance(child, ToolCallMessage):
                    groupable = (
                        child.tool_name not in _TOOL_GROUP_EXCLUSIONS
                        and child.is_success
                        and not child.has_class("-grouped")
                    )
                    if not groupable:
                        await flush()
                        continue
                    if run_anchor is None:
                        run_anchor = child
                    run_tools.append(child)
                    run_collapsible.append(child)
                    continue
                await flush()
            await flush()

        logger.debug(
            "TUI: MessageList.regroup_completed_tools folded %d runs",
            folded_runs_count,
        )

    def add_user_message(self, text: str) -> None:
        """Add a user message and reset auto-scroll."""
        self._auto_scroll_locked = True
        self.close_active_tool_group()
        self._mount_child(UserMessage(text))
        self._scroll_to_end()

    def add_queued_user_message(self, text: str) -> QueuedUserMessage:
        """Add a queued user message."""
        msg = QueuedUserMessage(text)
        self._mount_child(msg)
        self._scroll_to_end()
        return msg

    def remove_queued_user_messages(self) -> None:
        """Remove any QueuedUserMessage widgets currently in the list."""
        for msg in self.query(QueuedUserMessage):
            msg.remove()

    def start_assistant_message(self) -> None:
        """Start a streaming assistant message."""
        self.close_active_tool_group()
        msg = AssistantMessage()
        self._current_assistant = msg
        self._mount_child(msg)
        self._scroll_to_end()

    def append_assistant_token(self, token: str) -> None:
        """Append a token to current assistant message."""
        self._end_thinking()
        if self._current_assistant is None:
            self.start_assistant_message()
        if self._current_assistant is not None:
            self._current_assistant.append_token(token)

    def update_assistant_message(self, text: str) -> None:
        """Replace current assistant message text with full state."""
        self._end_thinking()
        if self._current_assistant is None:
            self.start_assistant_message()
        if self._current_assistant is not None:
            self._current_assistant.update_text(text)
            if self._auto_scroll_locked:
                self._scroll_to_end()

    def finish_assistant_message(self) -> None:
        """Finalize assistant message."""
        self._end_thinking()
        if self._current_assistant:
            self._current_assistant.finish()
            self._current_assistant = None
            self._scroll_to_end()

    def add_tool_call(
        self, name: str, call_id: str, args: dict[str, Any], live: bool = True
    ) -> None:
        """Add a tool-call widget."""
        self._end_thinking()
        msg = ToolCallMessage(name, args)
        self._tool_call_total += 1
        if live:
            msg.set_running()
        self._tool_calls[call_id] = msg
        from contextlib import nullcontext

        batch_ctx = (
            self.app.batch_update()
            if (self.is_attached and self.app is not None)
            else nullcontext()
        )
        with batch_ctx:
            if live and name not in _TOOL_GROUP_EXCLUSIONS:
                if self._active_tool_group is None or not getattr(
                    self._active_tool_group, "is_attached", False
                ):
                    self._active_tool_group = ToolGroupSummary(live=True)
                    self._mount_child(self._active_tool_group)
                msg.add_class("-grouped")
                msg.display = False
                self._active_tool_group.add_member(msg)
            self._mount_child(msg)
        self._scroll_to_end()

    def update_tool_result(
        self,
        call_id: str,
        result: str,
        success: bool = True,
        name: str | None = None,
        live: bool = True,
    ) -> None:
        """Update a tool-call widget with its result."""
        if call_id and call_id in self._tool_calls:
            msg = self._tool_calls[call_id]
            if success:
                msg.set_success(result)
            else:
                msg.set_error(result)
            for child in list(self.children):
                if isinstance(child, ToolGroupSummary) and msg in child._tools:
                    child.sync_members()
        else:
            msg_name = name if (name and name != "None") else "tool"
            msg = ToolCallMessage(msg_name, {})
            self._tool_call_total += 1
            if call_id:
                self._tool_calls[call_id] = msg
            from contextlib import nullcontext

            batch_ctx = (
                self.app.batch_update()
                if (self.is_attached and self.app is not None)
                else nullcontext()
            )
            with batch_ctx:
                if live and msg_name not in _TOOL_GROUP_EXCLUSIONS:
                    if self._active_tool_group is None or not getattr(
                        self._active_tool_group, "is_attached", False
                    ):
                        self._active_tool_group = ToolGroupSummary(live=True)
                        self._mount_child(self._active_tool_group)
                    msg.add_class("-grouped")
                    msg.display = False
                    self._active_tool_group.add_member(msg)
                self._mount_child(msg)
                if success:
                    msg.set_success(result)
                else:
                    msg.set_error(result)
                if (
                    self._active_tool_group is not None
                    and msg in self._active_tool_group._tools
                ):
                    self._active_tool_group.sync_members()
        self._scroll_to_end()

    def add_skill_message(self, name: str, content: str) -> None:
        """Add a skill loaded card."""
        self._mount_child(SkillMessage(skill_name=name, body=content))
        self._scroll_to_end()

    def add_diff_message(self, patch: str, file_path: str = "") -> None:
        """Add an inline diff message."""
        self._mount_child(DiffMessage(patch, file_path))
        self._scroll_to_end()

    def add_error_message(self, text: str) -> None:
        """Add an error message."""
        self._mount_child(ErrorMessage(text))
        self._scroll_to_end()

    def add_system_message(self, text: str) -> None:
        """Add a system/info message."""
        self._mount_child(SystemMessage(text))
        self._scroll_to_end()

    def clear(self) -> None:
        """Clear all child widgets."""
        self._stop_scroll_timers()
        self._pending_scroll_delta = 0
        self._scroll_end_pending = False
        self._auto_scroll_locked = True
        self.remove_children()
        self._current_assistant = None
        self._current_thinking = None
        self._tool_calls.clear()
        self._active_tool_group = None

    def set_timestamps_visible(self, visible: bool) -> None:
        """Show or hide timestamp footers across all mounted messages."""
        self._timestamps_visible = visible
        for child in self.children:
            fn = getattr(child, "set_timestamp_visible", None)
            if callable(fn):
                fn(visible)

    def _scroll_to_end(self) -> None:
        """Scroll to the end if auto-scroll is locked, coalesced per refresh frame."""
        if not self._auto_scroll_locked:
            return
        if not self.is_attached:
            self.scroll_end(animate=False, x_axis=False)
            return
        if self._scroll_end_pending:
            return
        self._scroll_end_pending = True

        def _do_scroll_end() -> None:
            self._scroll_end_pending = False
            if self._auto_scroll_locked and self.is_attached:
                self.scroll_end(animate=False, immediate=True, x_axis=False)

        self.call_after_refresh(_do_scroll_end)
