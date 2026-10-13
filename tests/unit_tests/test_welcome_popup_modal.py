"""Unit tests for WelcomeDetailPopup OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from opscloud.ui.widgets.welcome_popup import WelcomeDetailPopup


class DummyWelcomePopupApp(App[None]):
    """Minimal textual app to mount and test WelcomeDetailPopup."""

    def __init__(self, screen_instance: WelcomeDetailPopup) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_welcome_detail_popup_dismiss() -> None:
    items = [("skill1", "First test skill"), ("skill2", "Second test skill")]
    screen = WelcomeDetailPopup("Skills", items)
    app = DummyWelcomePopupApp(screen)

    async with app.run_test() as pilot:
        assert screen.dialog is not None
        assert screen.dialog.border_title is not None
        assert "Skills" in str(screen.dialog.border_title)
        screen.action_dismiss_popup()
        await pilot.pause()
        assert len(app._screen_stack) == 1


@pytest.mark.asyncio
async def test_welcome_detail_popup_full_height() -> None:
    """Verify welcome detail popup scroll fills the available dialog height."""
    items = [(f"skill_{i}", f"Description {i} with details") for i in range(16)]
    screen = WelcomeDetailPopup("Discovered Skills", items)
    app = DummyWelcomePopupApp(screen)

    async with app.run_test(size=(100, 50)) as pilot:
        scroll = screen.query_one("#welcome-detail-scroll")
        dialog = screen.query_one(".ops-modal-dialog")
        # Ensure scroll container fills dialog height and is not capped at 24
        assert scroll.size.height > 24
        assert scroll.size.height == dialog.size.height

