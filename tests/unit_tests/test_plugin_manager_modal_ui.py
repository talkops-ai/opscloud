"""Unit tests for PluginManagerScreen modal UI, pill tabs, and border styling."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock, patch
import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static
from textual.widgets.option_list import Option

from opscloud.ui.widgets.modal_base import PillTabs
from opscloud.ui.widgets.plugin_manager import (
    PluginManagerScreen,
    _ManagerState,
    _MarketplaceRow,
    _PluginRow,
)


class DummyModalApp(App):
    """Test app to mount and interact with PluginManagerScreen."""

    def __init__(self, screen_instance: PluginManagerScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


def _mock_state() -> _ManagerState:
    plugins = (
        _PluginRow(
            plugin_id="aws-core@talkops",
            description="Core AWS skills and tools.",
            enabled=True,
            version="1.0.0",
            author="TalkOps",
            display_name="AWS Core",
            skill_count=5,
        ),
        _PluginRow(
            plugin_id="aws-sre@talkops",
            description="Site reliability operations for AWS.",
            enabled=False,
            version="0.9.0",
            author="TalkOps",
            display_name="AWS SRE",
            skill_count=2,
        ),
    )
    installed = (
        _PluginRow(
            plugin_id="aws-core@talkops",
            description="Core AWS skills and tools.",
            enabled=True,
            version="1.0.0",
            author="TalkOps",
            display_name="AWS Core",
            skill_count=5,
        ),
    )
    marketplaces = (
        _MarketplaceRow(
            name="talkops",
            source="local",
            plugin_count=2,
            installed_count=1,
        ),
    )
    errors = ("Plugin xyz failed to load manifest.",)
    return _ManagerState(
        available_plugins=plugins,
        installed_plugins=installed,
        marketplaces=marketplaces,
        errors=errors,
    )


@pytest.mark.asyncio
async def test_plugin_manager_modal_chrome_and_counters() -> None:
    """Verify dialog border title has counter and subtitle has keycap hints."""
    screen = PluginManagerScreen()
    mock_state = _mock_state()

    with patch("opscloud.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = DummyModalApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()

            # Check border title
            assert screen.dialog is not None
            assert screen.dialog.border_title is not None
            assert "Plugins" in str(screen.dialog.border_title)
            assert "2 available" in str(screen.dialog.border_title)

            # Check border subtitle has keycaps
            subtitle_text = str(screen.dialog.border_subtitle)
            assert "navigate" in subtitle_text
            assert "details" in subtitle_text
            assert "search" in subtitle_text
            assert "close" in subtitle_text

            # Check tab counts
            tabs = screen.query_one(PillTabs)
            assert tabs.tabs[0].tab_id == "discover"
            assert tabs.tabs[1].tab_id == "installed"
            assert tabs.tabs[1].count == 1
            assert tabs.tabs[2].tab_id == "marketplaces"
            assert tabs.tabs[2].count == 1
            assert tabs.tabs[3].tab_id == "errors"
            assert tabs.tabs[3].count == 1


@pytest.mark.asyncio
async def test_plugin_manager_tab_navigation_and_subtitles() -> None:
    """Verify keyboard tab navigation switches views and updates chrome."""
    screen = PluginManagerScreen()
    mock_state = _mock_state()

    with patch("opscloud.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = DummyModalApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()

            # Switch to installed tab
            screen.action_next_tab()
            await pilot.pause()
            assert screen._tab == "installed"
            assert screen.dialog is not None
            assert screen.dialog.border_title is not None
            assert "1 installed" in str(screen.dialog.border_title)

            # Switch to marketplaces tab
            screen.action_next_tab()
            await pilot.pause()
            assert screen._tab == "marketplaces"
            assert screen.dialog is not None
            assert screen.dialog.border_title is not None
            assert "1 marketplaces" in str(screen.dialog.border_title)

            # Search is not active in marketplaces, subtitle hints update
            assert screen.dialog.border_subtitle is not None
            subtitle_text = str(screen.dialog.border_subtitle)
            assert "search" not in subtitle_text

            # Switch back with previous_tab
            screen.action_previous_tab()
            await pilot.pause()
            assert screen._tab == "installed"


@pytest.mark.asyncio
async def test_plugin_manager_details_mode_and_escape_back() -> None:
    """Verify selecting a plugin opens details and Escape returns to list mode."""
    screen = PluginManagerScreen()
    mock_state = _mock_state()

    with patch("opscloud.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = DummyModalApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()

            # Select first available plugin (detail:aws-core@talkops)
            from textual.widgets._option_list import Option

            class DummyOptionSelected:
                option = Option("AWS Core", id="detail:aws-core@talkops")

            await screen.on_option_list_option_selected(cast(Any, DummyOptionSelected()))
            await pilot.pause()

            # Should be in plugin_details mode
            assert screen._mode == "plugin_details"
            assert screen.dialog is not None
            assert screen.dialog.border_title is not None
            assert "AWS Core" in str(screen.dialog.border_title)
            assert "back" in str(screen.dialog.border_subtitle)

            # Press escape to return to list mode
            screen.action_cancel()
            await pilot.pause()
            assert screen._mode == "list"
            assert screen._selected_plugin is None
            assert screen.dialog is not None
            assert screen.dialog.border_title is not None
            assert "Plugins" in str(screen.dialog.border_title)


@pytest.mark.asyncio
async def test_plugin_manager_close_reload_prompt_flow() -> None:
    """Verify that when mutated, closing enters reload prompt and enter dismisses with 'reload'."""
    dismiss_result = None

    class TrackingApp(App):
        def compose(self) -> ComposeResult:
            yield from ()

        async def on_mount(self) -> None:
            nonlocal dismiss_result

            def on_close(res: Any) -> None:
                nonlocal dismiss_result
                dismiss_result = res

            await self.push_screen(screen, on_close)

    screen = PluginManagerScreen()
    mock_state = _mock_state()

    with patch("opscloud.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = TrackingApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # Mark screen as mutated (e.g. plugin installed)
            screen._mutated = True

            # Trigger close (action_cancel)
            screen.action_cancel()
            await pilot.pause()

            # Should transition to reload prompt
            assert screen._close_phase == "reload_prompt"
            assert screen.dialog is not None
            assert "Reload plugins?" in str(screen.dialog.border_title)

            # Pressing Enter triggers reload
            screen.action_reload_plugins()
            await pilot.pause()

            assert dismiss_result == "reload"


@pytest.mark.asyncio
async def test_plugin_manager_check_reload_required_detects_changes() -> None:
    """Verify that check_reload_required callback detecting changes prompts for reload."""
    dismiss_result = None

    async def mock_check() -> bool:
        return True

    screen = PluginManagerScreen(check_reload_required=mock_check)
    mock_state = _mock_state()

    class TrackingApp(App):
        def compose(self) -> ComposeResult:
            yield from ()

        async def on_mount(self) -> None:
            nonlocal dismiss_result

            def on_close(res: Any) -> None:
                nonlocal dismiss_result
                dismiss_result = res

            await self.push_screen(screen, on_close)

    with patch("opscloud.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = TrackingApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            # Cancel while browsing
            screen.action_cancel()
            await pilot.pause()

            assert screen._close_phase == "reload_prompt"

            # Pressing Esc again chooses "later"
            screen.action_cancel()
            await pilot.pause()

            assert dismiss_result == "later"


@pytest.mark.asyncio
async def test_refresh_all_marketplaces_shows_intermediate_status_and_completes() -> None:
    """Verify selecting 'refresh-all-marketplaces' immediately shows intermediate feedback and then completes."""
    screen = PluginManagerScreen()
    mock_state = _mock_state()

    def fake_refresh(**kw: Any) -> dict[str, Any]:
        return {}

    class DummyEvent:
        option = Option("Refresh", id="refresh-all-marketplaces")

    with patch("opscloud.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state), \
         patch("opscloud.plugins.discovery.refresh_all_marketplaces", side_effect=fake_refresh):
        app = DummyModalApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen._select_tab("marketplaces")
            await pilot.pause()

            ev1: Any = DummyEvent()
            await screen.on_option_list_option_selected(ev1)
            # Intermediate state must be immediate:
            assert screen._is_refreshing_marketplaces is True
            assert screen._status == "Refreshing all marketplaces..."
            status_widget = screen.query_one("#plugin-manager-status", Static)
            assert str(status_widget.content) == "Refreshing all marketplaces..."

            # After background worker finishes:
            await pilot.pause()
            assert screen._is_refreshing_marketplaces is False
            assert screen._status == "All marketplaces refreshed."
            assert str(status_widget.content) == "All marketplaces refreshed."


@pytest.mark.asyncio
async def test_refresh_single_marketplace_shows_intermediate_status_and_completes() -> None:
    """Verify selecting single marketplace refresh shows intermediate feedback and completes."""
    screen = PluginManagerScreen()
    mock_state = _mock_state()

    mock_mp = MagicMock()
    mock_mp.plugins = []

    class DummyEvent:
        option = Option("Refresh", id="action:refresh-marketplace")

    with patch("opscloud.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state), \
         patch("opscloud.plugins.discovery.refresh_marketplace", return_value=mock_mp):
        app = DummyModalApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen._select_tab("marketplaces")
            await pilot.pause()

            screen._selected_marketplace = mock_state.marketplaces[0]
            screen._mode = "marketplace_details"
            screen._refresh_view()
            await pilot.pause()

            ev2: Any = DummyEvent()
            await screen.on_option_list_option_selected(ev2)
            # Intermediate state must be immediate:
            assert screen._is_refreshing_marketplaces is True
            assert "Refreshing marketplace talkops..." in str(screen._status)
            status_widget = screen.query_one("#plugin-manager-status", Static)
            assert "Refreshing marketplace talkops..." in str(status_widget.content)

            # After background worker finishes:
            await pilot.pause()
            assert screen._is_refreshing_marketplaces is False
            assert "Marketplace refreshed" in str(screen._status)
            assert "Marketplace refreshed" in str(status_widget.content)
