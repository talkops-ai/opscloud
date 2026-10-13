"""Unit tests for InstallProviderConfirmScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from opscloud.ui.widgets.install_confirm import InstallProviderConfirmScreen


class DummyInstallApp(App[None]):
    """Minimal textual app to mount and test InstallProviderConfirmScreen."""

    def __init__(self, screen_instance: InstallProviderConfirmScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_install_confirm_screen() -> None:
    screen = InstallProviderConfirmScreen(provider="anthropic", extra="anthropic")
    app = DummyInstallApp(screen)

    async with app.run_test() as pilot:
        # Cancel action dismisses with False
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1
