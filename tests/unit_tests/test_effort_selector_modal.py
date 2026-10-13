"""Unit tests for EffortSelectorScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from opscloud.ui.widgets.effort_selector import EffortSelectorScreen


class DummyEffortApp(App[None]):
    """Minimal textual app to mount and test EffortSelectorScreen."""

    def __init__(self, screen_instance: EffortSelectorScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_effort_selector_screen_mount_and_options() -> None:
    efforts = ("low", "medium", "high")
    screen = EffortSelectorScreen(
        model_spec="claude-3-7-sonnet",
        efforts=efforts,
        current_effort="medium",
        default_effort="high",
    )
    app = DummyEffortApp(screen)

    async with app.run_test() as pilot:
        opt_list = screen.query_one("#effort-options", OptionList)
        assert opt_list is not None
        assert opt_list.has_focus
        assert opt_list.option_count == 3
        # "medium" is index 1
        assert opt_list.highlighted == 1

        # Test navigation down
        screen.action_cursor_down()
        await pilot.pause()
        assert opt_list.highlighted == 2

        # Test cancel
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1
