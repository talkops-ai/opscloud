"""Unit tests for DebugConsoleScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Select

from opscloud.ui.widgets.debug_console import DebugConsoleScreen, SnapshotField, _DebugLogView


class DummyDebugApp(App[None]):
    """Minimal textual app to mount and test DebugConsoleScreen."""

    def __init__(self, screen_instance: DebugConsoleScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_debug_console_screen_mount_and_controls() -> None:
    snapshot = [
        SnapshotField(label="Model", value="claude-3-7-sonnet"),
        SnapshotField(label="Thread", value="test-thread-id", copyable=True),
    ]
    screen = DebugConsoleScreen(snapshot=snapshot)
    app = DummyDebugApp(screen)

    async with app.run_test() as pilot:
        log_view = screen.query_one("#debug-log", _DebugLogView)
        assert log_view is not None

        level_select = screen.query_one("#debug-level-filter", Select)
        assert level_select is not None
        assert level_select.value == "all"

        # Close
        screen.action_close()
        await pilot.pause()
        assert len(app._screen_stack) == 1
