"""Unit tests for smooth scrolling and layout-thrash prevention."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.geometry import Offset

from opscloud.ui.theme import get_css_variable_defaults
from opscloud.ui.widgets.messages import (
    MessageList,
    ToolCallMessage,
    UserMessage,
    compute_wheel_speed,
)
from opscloud.ui.widgets.status import StatusBar
from opscloud.ui.widgets.subagent_panel import SubagentPanel


class _ThemedTestApp(App[None]):
    """Minimal test App providing OpsCloud custom CSS variables."""

    def get_css_variables(self) -> dict[str, str]:
        return {**super().get_css_variables(), **get_css_variable_defaults()}


def test_compute_wheel_speed_sublinear_curve() -> None:
    """Verify sub-linear WheelSpeed curve min(12, 1 + isqrt(streak)) and viewport clamp."""
    # Streak 0 -> 1 line
    assert compute_wheel_speed(0) == 1
    # Streak 1..3 -> 2 lines
    assert compute_wheel_speed(1) == 2
    assert compute_wheel_speed(3) == 2
    # Streak 4..8 -> 3 lines
    assert compute_wheel_speed(4) == 3
    assert compute_wheel_speed(8) == 3
    # Streak 9 -> 4 lines
    assert compute_wheel_speed(9) == 4
    # Streak 16 -> 5 lines
    assert compute_wheel_speed(16) == 5
    # Cap at 12 lines for large streaks
    assert compute_wheel_speed(121) == 12
    assert compute_wheel_speed(500) == 12

    # Clamp to max(1, viewport_height // 3) when viewport_height > 0
    assert compute_wheel_speed(100, viewport_height=15) == 5
    assert compute_wheel_speed(100, viewport_height=2) == 1


def test_wheel_streak_window_and_direction_reset() -> None:
    """Verify wheel streak increments within 150ms and resets on timeout or reversal."""
    ml = MessageList()

    # First tick at t=10.0 -> streak 0 -> speed 1
    assert ml._next_wheel_speed(-1, now=10.000) == 1
    assert ml._wheel_streak == 0

    # Rapid ticks within 150ms in same direction (-1) build streak
    assert ml._next_wheel_speed(-1, now=10.050) == 2  # streak 1
    assert ml._next_wheel_speed(-1, now=10.100) == 2  # streak 2
    assert ml._next_wheel_speed(-1, now=10.140) == 2  # streak 3
    assert ml._next_wheel_speed(-1, now=10.200) == 3  # streak 4

    # Direction reversal (+1) resets streak to 0 -> speed 1
    assert ml._next_wheel_speed(1, now=10.220) == 1
    assert ml._wheel_streak == 0

    # Pause >= 150ms resets streak to 0 -> speed 1
    assert ml._next_wheel_speed(1, now=10.260) == 2  # streak 1
    assert ml._next_wheel_speed(1, now=10.500) == 1  # >150ms gap -> streak 0


@pytest.mark.asyncio
async def test_scroll_coalescer_and_direction_reversal() -> None:
    """Verify 16ms frame-budget scroll coalescing and direction reversal reset."""

    class ScrollHostApp(_ThemedTestApp):
        def compose(self) -> ComposeResult:
            yield MessageList(id="messages")

    app = ScrollHostApp()
    async with app.run_test(size=(100, 20)) as pilot:
        messages = app.query_one("#messages", MessageList)
        for i in range(40):
            messages.add_user_message(f"Message line {i}\nExtra line {i}")
        await pilot.pause()

        assert messages.max_scroll_y > 20
        messages.scroll_to(y=messages.max_scroll_y, animate=False, immediate=True)
        await pilot.pause()
        start_y = messages.scroll_y
        assert start_y == messages.max_scroll_y

        # 1. Leading edge flushes immediately when >= 16ms since last flush
        messages._last_scroll_flush_time = 0.0
        messages._queue_wheel_scroll(-3, now=100.0)
        assert messages._pending_scroll_delta == 0
        assert messages.scroll_y == start_y - 3
        assert messages._auto_scroll_locked is False

        # 2. Rapid wheel events within the same 16ms window accumulate in _pending_scroll_delta
        messages._last_scroll_flush_time = 100.0
        messages._queue_wheel_scroll(-2, now=100.004)
        messages._queue_wheel_scroll(-4, now=100.008)
        assert messages._pending_scroll_delta == -6

        # 3. Reversing direction mid-frame resets pending delta to the new direction
        messages._queue_wheel_scroll(2, now=100.010)
        assert messages._pending_scroll_delta == 2

        # Flush applies the coalesced delta in a single step
        messages._flush_pending_scroll()
        assert messages._pending_scroll_delta == 0
        assert messages.scroll_y == start_y - 1


@pytest.mark.asyncio
async def test_user_scroll_up_locks_out_streaming_yank_and_relocks_at_bottom() -> None:
    """Verify scrolling up prevents streaming updates from yanking viewport to bottom."""

    class ScrollHostApp(_ThemedTestApp):
        def compose(self) -> ComposeResult:
            yield MessageList(id="messages")

    app = ScrollHostApp()
    async with app.run_test(size=(100, 20)) as pilot:
        messages = app.query_one("#messages", MessageList)
        for i in range(35):
            messages.add_user_message(f"Turn {i}\nBody {i}")
        await pilot.pause()

        # Initially locked at bottom
        messages.scroll_to(y=messages.max_scroll_y, animate=False, immediate=True)
        await pilot.pause()
        assert messages._auto_scroll_locked is True

        # User scrolls up with mouse wheel
        up_event = events.MouseScrollUp(
            widget=messages,
            x=10,
            y=10,
            delta_x=0,
            delta_y=-1,
            button=0,
            shift=False,
            meta=False,
            ctrl=False,
        )
        messages._on_mouse_scroll_up(up_event)
        messages._flush_pending_scroll()
        await pilot.pause()

        assert messages._auto_scroll_locked is False
        user_scroll_y = messages.scroll_y
        assert user_scroll_y < messages.max_scroll_y

        # Streaming tokens and tool calls arrive while user is scrolled up
        messages.append_thinking_token("Thinking about step 1...", 1.0)
        messages.add_tool_call("execute", "call-1", {"command": "ls -la"}, live=True)
        messages.update_tool_result("call-1", "total 0", success=True)
        messages.append_assistant_token("Here is the result")
        if messages._current_assistant is not None:
            await messages._current_assistant._flush_pending_append()
        await pilot.pause()

        # Viewport must remain at user_scroll_y, NOT yanked to bottom!
        assert messages._auto_scroll_locked is False
        assert messages.scroll_y == user_scroll_y

        # User scrolls to bottom via End action -> re-locks auto-scroll
        messages.action_scroll_end()
        await pilot.pause()
        assert messages._auto_scroll_locked is True
        assert messages.scroll_y == messages.max_scroll_y


@pytest.mark.asyncio
async def test_spinners_do_not_trigger_full_tree_layout_reflow() -> None:
    """Verify 10Hz spinners on ToolCallMessage, StatusBar, and SubagentPanel use layout=False."""

    class SpinnerHostApp(_ThemedTestApp):
        def compose(self) -> ComposeResult:
            yield MessageList(id="messages")
            yield SubagentPanel(id="subagent-panel")
            yield StatusBar(id="status-bar")

    app = SpinnerHostApp()
    async with app.run_test(size=(100, 24)) as pilot:
        messages = app.query_one("#messages", MessageList)
        status_bar = app.query_one("#status-bar", StatusBar)
        subagent_panel = app.query_one("#subagent-panel", SubagentPanel)

        # 1. Grouped (hidden) running ToolCallMessage does not update header or dirty layout
        messages.add_tool_call("read_file", "call-grouped", {"file_path": "a.py"}, live=True)
        grouped_tool = messages._tool_calls["call-grouped"]
        assert grouped_tool.display is False

        # 2. Visible (ungrouped) running ToolCallMessage updates header with layout=False
        messages.add_tool_call(
            "write_todos", "call-visible", {"todos": []}, live=True
        )
        visible_tool = messages._tool_calls["call-visible"]
        assert visible_tool.display is True

        status_bar.set_connection("connecting")
        status_bar.set_busy("Working")
        subagent_panel._handle_start(
            "sub-1", "eval-1", {"description": "Task 1", "subagent_type": "research"}
        )
        subagent_panel._refresh()
        await pilot.pause()

        # Clear any mount-time layout queue
        app.screen._layout_widgets.clear()

        # Tick all 10Hz spinners
        grouped_tool._update_running_animation()
        visible_tool._update_running_animation()
        status_bar._tick_spinner()
        subagent_panel._tick()

        # None of the spinner frame advances should schedule a layout reflow!
        assert len(app.screen._layout_widgets) == 0


@pytest.mark.asyncio
async def test_hover_recalculation_suppressed_during_active_scroll() -> None:
    """Verify App._update_mouse_over is suppressed while MessageList is actively scrolling."""

    class HoverScrollApp(_ThemedTestApp):
        def compose(self) -> ComposeResult:
            yield MessageList(id="messages")

    app = HoverScrollApp()
    async with app.run_test(size=(100, 20)) as pilot:
        messages = app.query_one("#messages", MessageList)
        for i in range(25):
            messages.add_user_message(f"Message {i}")
        await pilot.pause()

        app.mouse_over = messages
        app.mouse_position = Offset(10, 5)

        # Mark user scrolling -> hover suppression window active
        messages._mark_user_scrolling()
        spy = MagicMock()
        monkeypatch_orig = app.call_after_refresh
        app.call_after_refresh = spy  # type: ignore[method-assign]
        try:
            app._update_mouse_over(app.screen)
            assert spy.call_count == 0
        finally:
            app.call_after_refresh = monkeypatch_orig  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_scroll_reflows_welcome_banner_and_markdown_children_in_compositor() -> None:
    """Verify scrolling down and back up triggers Compositor.reflow_visible for all children.

    Regression test for UI corruption where overriding ``watch_scroll_y`` with
    ``self.refresh()`` bypassed ``Widget._refresh_scroll()`` ->
    ``Screen._refresh_layout(scroll=True)`` -> ``Compositor.reflow_visible()``,
    leaving ``WelcomeBanner`` (``#welcome-card``, ``_InfoPanel``) and Markdown
    blocks with stale off-screen coordinates when scrolling back up.
    """
    from opscloud.ui.widgets.messages import AssistantMessage
    from opscloud.ui.widgets.welcome import WelcomeBanner, _InfoPanel, _LeftPanel

    class WelcomeScrollApp(_ThemedTestApp):
        def compose(self) -> ComposeResult:
            with MessageList(id="messages"):
                yield WelcomeBanner(id="welcome")

    app = WelcomeScrollApp()
    async with app.run_test(size=(100, 24)) as pilot:
        messages = app.query_one("#messages", MessageList)
        welcome = app.query_one("#welcome", WelcomeBanner)
        welcome_card = welcome.query_one("#welcome-card")
        left_panel = welcome.query_one(_LeftPanel)
        info_panel = welcome.query_one(_InfoPanel)

        for i in range(15):
            messages.add_user_message(f"Question {i}")
            assistant = AssistantMessage(
                f"## Section {i}\n\n- Bullet item {i}A\n- Bullet item {i}B\n\n```bash\necho {i}\n```"
            )
            messages._mount_child(assistant)
        await pilot.pause(0.2)

        assert messages.max_scroll_y > 20

        # Scroll to bottom: WelcomeBanner and its sub-panels should scroll out of view
        messages.scroll_to(y=messages.max_scroll_y, animate=False, immediate=True)
        await pilot.pause()
        visible_at_bottom = app.screen._compositor.visible_widgets
        assert welcome_card not in visible_at_bottom
        assert info_panel not in visible_at_bottom

        # Scroll back to top: Compositor must immediately reflow all WelcomeBanner panels back into view
        messages.scroll_to(y=0, animate=False, immediate=True)
        await pilot.pause()
        visible_at_top = app.screen._compositor.visible_widgets
        assert welcome in visible_at_top
        assert welcome_card in visible_at_top
        assert left_panel in visible_at_top
        assert info_panel in visible_at_top
        assert welcome_card.region.y >= 0
        assert left_panel.region.width > 0
        assert info_panel.region.width > 0

