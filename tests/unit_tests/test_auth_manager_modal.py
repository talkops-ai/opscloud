"""Unit tests for AuthManagerScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from opscloud.ui.widgets.auth import AuthPromptScreen
from opscloud.ui.widgets.auth_manager import AuthManagerScreen, _KNOWN_PROVIDERS


class DummyAuthApp(App[None]):
    """Minimal textual app to mount and test AuthManagerScreen."""

    def __init__(self, screen_instance: AuthManagerScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_auth_manager_screen_mount_and_options() -> None:
    screen = AuthManagerScreen(initial_provider="openai")
    app = DummyAuthApp(screen)

    async with app.run_test() as pilot:
        opt_list = screen.query_one("#auth-mgr-options", OptionList)
        assert opt_list is not None
        assert opt_list.has_focus
        assert opt_list.option_count == len(_KNOWN_PROVIDERS)

        # Highlighted index matches initial_provider
        openai_idx = _KNOWN_PROVIDERS.index("openai")
        assert opt_list.highlighted == openai_idx

        # Close
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1


class DummyAuthPromptApp(App[None]):
    """Minimal textual app to mount and test AuthPromptScreen."""

    def __init__(self, screen_instance: AuthPromptScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_auth_prompt_screen_mount_and_cancel() -> None:
    from opscloud.ui.widgets.auth import AuthPromptScreen

    screen = AuthPromptScreen(provider="anthropic")
    app = DummyAuthPromptApp(screen)

    async with app.run_test() as pilot:
        assert screen.dialog is not None
        assert screen.dialog.border_title is not None
        assert "Anthropic" in str(screen.dialog.border_title)
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1
