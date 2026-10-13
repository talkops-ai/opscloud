"""Unit tests for NotificationSettingsScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Checkbox

from opscloud.ui.widgets.notification_settings import NotificationSettingsScreen, WARNING_TOGGLES


class DummyNotifApp(App[None]):
    """Minimal textual app to mount and test NotificationSettingsScreen."""

    def __init__(self, screen_instance: NotificationSettingsScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_notification_settings_screen_mount_and_toggle() -> None:
    suppressed = {"docker"}
    screen = NotificationSettingsScreen(suppressed=suppressed)
    app = DummyNotifApp(screen)

    async with app.run_test() as pilot:
        boxes = list(screen.query(Checkbox))
        assert len(boxes) == len(WARNING_TOGGLES)

        docker_box = screen.query_one("#ns-docker", Checkbox)
        assert docker_box.value is False

        ripgrep_box = screen.query_one("#ns-ripgrep", Checkbox)
        assert ripgrep_box.value is True

        # Toggle ripgrep
        ripgrep_box.value = False
        await pilot.pause()
        assert "ripgrep" in screen._suppressed

        # Close
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1
