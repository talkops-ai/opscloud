"""Unit tests for ConfigManagerScreen and _InlineEditScreen modal migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Input, OptionList

from opscloud.ui.widgets.config_manager import ConfigManagerScreen, _InlineEditScreen


class DummyConfigApp(App[None]):
    """Minimal textual app to mount and test ConfigManagerScreen."""

    def __init__(self, screen_instance: ConfigManagerScreen | _InlineEditScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_config_manager_screen_mount_and_search() -> None:
    screen = ConfigManagerScreen()
    app = DummyConfigApp(screen)

    async with app.run_test() as pilot:
        search = screen.query_one("#config-search", Input)
        opt_list = screen.query_one("#config-options", OptionList)
        assert opt_list is not None
        assert opt_list.has_focus
        assert opt_list.option_count > 0

        # Type search filter
        search.value = "theme"
        await pilot.pause()
        # OptionList filtered
        assert opt_list.option_count >= 1

        # Clear search filter
        search.value = ""
        await pilot.pause()
        assert opt_list.option_count > 5

        # Close
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1


@pytest.mark.asyncio
async def test_inline_edit_screen() -> None:
    screen = _InlineEditScreen(label="Test Option", current_value="current_val")
    app = DummyConfigApp(screen)

    async with app.run_test() as pilot:
        inp = screen.query_one("#edit-input", Input)
        assert inp.has_focus
        assert inp.value == "current_val"

        # Cancel
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1
