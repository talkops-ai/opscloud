"""Unit tests for AutoModeNoticeScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from opscloud.ui.widgets.auto_mode_notice import AutoModeNoticeScreen


class DummyNoticeApp(App[None]):
    """Minimal textual app to mount and test AutoModeNoticeScreen."""

    def __init__(self, screen_instance: AutoModeNoticeScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_auto_mode_notice_screen() -> None:
    screen = AutoModeNoticeScreen()
    app = DummyNoticeApp(screen)

    async with app.run_test() as pilot:
        # Cancel action dismisses with False
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1
