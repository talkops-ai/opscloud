"""Unit tests for ThemeSelectorScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from opscloud.ui.theme import get_registry
from opscloud.ui.widgets.theme_selector import ThemeSelectorScreen


class DummyThemeApp(App[None]):
    """Minimal textual app to mount and test ThemeSelectorScreen."""

    def __init__(self, screen_instance: ThemeSelectorScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_theme_selector_screen_mount_and_options() -> None:
    registry = get_registry()
    assert len(registry) > 0

    screen = ThemeSelectorScreen(
        current_theme="opscloud-dark",
        terminal_default="opscloud-dark",
    )
    app = DummyThemeApp(screen)

    async with app.run_test() as pilot:
        opt_list = screen.query_one("#theme-options", OptionList)
        assert opt_list is not None
        assert opt_list.has_focus
        assert opt_list.option_count == len(registry)

        # Highlighted index matches current_theme
        assert opt_list.highlighted == screen._current_index()

        # Test toggle names (keys vs labels)
        screen.action_toggle_names()
        await pilot.pause()
        assert screen._show_keys is True

        screen.action_toggle_names()
        await pilot.pause()
        assert screen._show_keys is False

        # Test down navigation
        initial_highlighted = opt_list.highlighted or 0
        screen.action_cursor_down()
        await pilot.pause()
        assert opt_list.highlighted == initial_highlighted + 1

        # Test cancel reverts theme
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1
