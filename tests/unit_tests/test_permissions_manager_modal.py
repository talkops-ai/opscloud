"""Unit tests for PermissionsManagerScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from opscloud.ui.permission_store import PermissionStore
from opscloud.ui.widgets.permissions_manager import PermissionsManagerScreen, _AddRuleScreen


class DummyPermApp(App[None]):
    """Minimal textual app to mount and test PermissionsManagerScreen."""

    def __init__(self, screen_instance: PermissionsManagerScreen | _AddRuleScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_permissions_manager_screen_mount_and_tabs() -> None:
    store = PermissionStore()
    screen = PermissionsManagerScreen(store=store)
    app = DummyPermApp(screen)

    async with app.run_test() as pilot:
        opt_list = screen.query_one("#perm-options", OptionList)
        assert opt_list is not None
        assert opt_list.has_focus
        assert opt_list.option_count > 0

        # Switch tab next
        screen.action_next_tab()
        await pilot.pause()
        assert screen._active_tab == "recently_denied"

        # Switch tab prev
        screen.action_prev_tab()
        await pilot.pause()
        assert screen._active_tab == "permissions"

        # Cycle mode
        initial_mode = store.mode
        screen.action_cycle_mode()
        await pilot.pause()
        assert store.mode != initial_mode

        # Close
        screen.action_close()
        await pilot.pause()
        assert len(app._screen_stack) == 1


@pytest.mark.asyncio
async def test_add_rule_screen() -> None:
    screen = _AddRuleScreen(category="allow")
    app = DummyPermApp(screen)

    async with app.run_test() as pilot:
        inp = screen.query_one("#add-rule-input")
        assert inp.has_focus

        # Cancel
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1
