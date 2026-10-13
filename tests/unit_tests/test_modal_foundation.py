"""Unit tests for OpsModalScreen and modal foundation components.

Tests the unified modal dialog container, PillTabs, ModalSearchInput,
and modal option row formatting.
"""

from __future__ import annotations

import importlib
from typing import ClassVar

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Static

# `opscloud.config.settings` is shadowed by a Settings instance on the package.
settings = importlib.import_module("opscloud.config.settings")
from opscloud.ui.theme import get_css_variable_defaults, get_theme_colors, register_app_themes
from opscloud.ui.widgets.modal_base import (
    ModalDialog,
    ModalOptionRow,
    ModalSearchInput,
    OpsModalScreen,
    PillTab,
    PillTabs,
    format_modal_hints,
    format_modal_option_row,
)


class _ThemedModalApp(App):
    """App carrying OpsCloud theme variables for testing modal screens."""

    def __init__(self, screen_to_mount: OpsModalScreen | None = None) -> None:
        super().__init__()
        register_app_themes(self)
        self.theme = "opscloud-dark"
        self._screen_to_mount = screen_to_mount

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return get_css_variable_defaults(colors=get_theme_colors(self))

    def compose(self) -> ComposeResult:
        yield Static("Background content", id="bg")

    async def on_mount(self) -> None:
        if self._screen_to_mount is not None:
            await self.push_screen(self._screen_to_mount)


class _SampleModal(OpsModalScreen[str | None]):
    """Sample modal screen for testing foundation features."""

    def compose_modal(self) -> ComposeResult:
        yield PillTabs(
            [
                ("discover", "Discover"),
                ("installed", "Installed", 4),
                ("marketplaces", "Marketplaces", 2),
                ("errors", "Errors", 0),
            ],
            id="modal-tabs",
        )
        yield ModalSearchInput(placeholder="Search plugins...", id="modal-search")
        yield OptionList(id="modal-options")


# ── Tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ops_modal_screen_mounting_and_dialog():
    """OpsModalScreen correctly wraps compose_modal() inside ModalDialog."""
    modal = _SampleModal()
    app = _ThemedModalApp(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert modal.dialog is not None
        assert isinstance(modal.dialog, ModalDialog)
        assert modal.query_one(ModalDialog) is modal.dialog

        # Inner components exist inside the dialog
        assert modal.query_one(PillTabs) is not None
        assert modal.query_one(ModalSearchInput) is not None
        assert modal.query_one(OptionList) is not None


@pytest.mark.asyncio
async def test_modal_title_and_subtitle_formatting():
    """set_modal_title and set_modal_subtitle update dialog border chrome."""
    modal = _SampleModal()
    app = _ThemedModalApp(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        modal.set_modal_title("Plugins", 26)
        assert modal.dialog is not None
        assert str(modal.dialog.border_title).strip() == "Plugins (26)"

        modal.set_modal_title("Settings")
        assert str(modal.dialog.border_title).strip() == "Settings"

        modal.set_modal_subtitle([("↑↓", "navigate"), ("↵", "select"), ("esc", "close")])
        subtitle_plain = str(modal.dialog.border_subtitle)
        assert "navigate" in subtitle_plain
        assert "select" in subtitle_plain
        assert "close" in subtitle_plain


@pytest.mark.asyncio
async def test_escape_key_dismisses_modal():
    """Pressing escape cleanly dismisses OpsModalScreen with None."""
    modal = _SampleModal()
    app = _ThemedModalApp(modal)
    dismiss_result = "not_dismissed"

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        def on_dismiss(result):
            nonlocal dismiss_result
            dismiss_result = result

        # Replace dismiss to capture return value
        orig_dismiss = modal.dismiss
        setattr(modal, "dismiss", lambda res=None: (on_dismiss(res), orig_dismiss(res)))

        await pilot.press("escape")
        await pilot.pause()
        assert dismiss_result is None


@pytest.mark.asyncio
async def test_pill_tabs_switching_and_counts():
    """PillTabs handles active selection, counting, and next/previous navigation."""
    tabs = PillTabs(
        [
            ("discover", "Discover"),
            ("installed", "Installed", 4),
            ("errors", "Errors", 0),
        ],
        id="tabs",
    )

    class TabsApp(App):
        def compose(self) -> ComposeResult:
            yield tabs

    app = TabsApp()
    async with app.run_test(size=(80, 10)) as pilot:
        await pilot.pause()

        # Initial active tab
        assert tabs.active_tab == "discover"
        assert tabs.tabs[0].has_class("pill-tab--active")
        assert not tabs.tabs[1].has_class("pill-tab--active")

        # Activate second tab
        tabs.activate_tab("installed")
        await pilot.pause()
        assert tabs.active_tab == "installed"
        assert tabs.tabs[1].has_class("pill-tab--active")
        assert not tabs.tabs[0].has_class("pill-tab--active")

        # Update count badge
        tabs.set_tab_count("installed", 7)
        assert tabs.tabs[1].count == 7

        # Next tab wrap-around
        tabs.next_tab()
        await pilot.pause()
        assert tabs.active_tab == "errors"

        tabs.next_tab()
        await pilot.pause()
        assert tabs.active_tab == "discover"

        # Previous tab wrap-around
        tabs.previous_tab()
        await pilot.pause()
        assert tabs.active_tab == "errors"


@pytest.mark.asyncio
async def test_modal_search_input_navigation_to_list():
    """Pressing down arrow or enter in ModalSearchInput focuses the target OptionList."""
    modal = _SampleModal()
    app = _ThemedModalApp(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        search = modal.query_one(ModalSearchInput)
        opt_list = modal.query_one(OptionList)

        search.focus()
        await pilot.pause()
        assert search.input_widget.has_focus

        # Press down arrow -> should transfer focus to OptionList
        await pilot.press("down")
        await pilot.pause()
        assert opt_list.has_focus


@pytest.mark.asyncio
async def test_tab_keyboard_shortcuts_switch_tabs():
    """Tab and shift+tab cycle through PillTabs from the modal screen."""
    modal = _SampleModal()
    app = _ThemedModalApp(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        tabs = modal.query_one(PillTabs)
        opt_list = modal.query_one(OptionList)
        opt_list.focus()
        await pilot.pause()

        assert tabs.active_tab == "discover"

        # Next tab
        modal.action_next_tab()
        await pilot.pause()
        assert tabs.active_tab == "installed"

        # Previous tab
        modal.action_previous_tab()
        await pilot.pause()
        assert tabs.active_tab == "discover"


@pytest.mark.asyncio
async def test_slash_keyboard_shortcut_focuses_search():
    """Pressing '/' from outside an input focuses the ModalSearchInput."""
    modal = _SampleModal()
    app = _ThemedModalApp(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        opt_list = modal.query_one(OptionList)
        opt_list.focus()
        await pilot.pause()
        assert opt_list.has_focus

        modal.action_focus_search()
        await pilot.pause()
        search = modal.query_one(ModalSearchInput)
        assert search.input_widget.has_focus


def test_format_modal_option_row():
    """format_modal_option_row renders two-line structured row with badges and meta."""
    row = format_modal_option_row(
        label="aws-agents",
        description="The agents for aws and cloud DevOps engineering.",
        badges=[("plugin", "$mode-command"), ("agent-toolkit", "$secondary")],
        meta="v1.4.0",
        width=80,
        is_selected=True,
    )
    plain = row.plain

    # Line 1 contains selection bar, label, badges, and meta
    lines = plain.split("\n")
    assert len(lines) == 2
    assert "▌" in lines[0] or ">" in lines[0]
    assert "aws-agents" in lines[0]
    assert "[plugin]" in lines[0]
    assert "[agent-toolkit]" in lines[0]
    assert "v1.4.0" in lines[0]

    # Line 2 contains indented description
    assert lines[1].startswith("  ")
    assert "The agents for aws and cloud DevOps" in lines[1]


def test_format_modal_option_row_truncation():
    """Long descriptions are truncated with an ellipsis within the specified width."""
    long_desc = "A" * 150
    row = format_modal_option_row(
        label="my-tool",
        description=long_desc,
        width=40,
        is_selected=False,
    )
    lines = row.plain.split("\n")
    assert len(lines) == 2
    assert lines[1].endswith("…") or lines[1].endswith("...")
    assert len(lines[1]) <= 40


def test_format_modal_option_row_dataclass():
    """ModalOptionRow dataclass renders identical Content."""
    helper = ModalOptionRow(
        label="test-plugin",
        description="A helpful description.",
        badges=["tag1"],
        meta="v2.0",
        is_selected=False,
    )
    rendered = helper.render(width=70)
    assert "test-plugin" in rendered.plain
    assert "[tag1]" in rendered.plain
    assert "v2.0" in rendered.plain


def test_format_modal_hints():
    """format_modal_hints joins keycaps with bullet separator and respects max_width."""
    hints = [("↑↓", "navigate"), ("↵", "select"), ("esc", "close")]
    content = format_modal_hints(hints, max_width=100)
    plain = content.plain
    assert "↑↓" in plain
    assert "navigate" in plain
    assert "close" in plain

    # Tight max_width cuts trailing hints
    tight = format_modal_hints(hints, max_width=15)
    assert "navigate" in tight.plain
    assert "close" not in tight.plain


def test_ascii_mode_glyphs(monkeypatch):
    """In ASCII mode, selection bar and search glyph fallback cleanly."""
    monkeypatch.setattr(settings, "_glyphs_cache", settings.ASCII_GLYPHS)

    row = format_modal_option_row(
        label="tool",
        description="desc",
        is_selected=True,
    )
    assert ">" in row.plain


def test_format_modal_option_row_with_status():
    """format_modal_option_row correctly displays status tag and metadata."""
    row = format_modal_option_row(
        label="my-plugin",
        description="Handles deployments.",
        status=("✓ Enabled", "bold $success"),
        meta="v1.0.0",
        width=80,
    )
    assert "✓ Enabled" in row.plain
    assert "v1.0.0" in row.plain


def test_format_modal_option_row_single_line():
    """format_modal_option_row without description generates a single-line Content."""
    row = format_modal_option_row(
        label="single-line-item",
        meta="meta-info",
        width=60,
    )
    lines = row.plain.split("\n")
    assert len(lines) == 1
    assert "single-line-item" in lines[0]
    assert "meta-info" in lines[0]


@pytest.mark.asyncio
async def test_modal_responsive_sizing():
    """ModalDialog respects responsive width and max-width at different terminal sizes."""
    modal = _SampleModal()
    app = _ThemedModalApp(modal)
    # At 80 columns width
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert modal.dialog is not None
        # Max-width is 95% of 80 = 76
        assert modal.dialog.outer_size.width <= 76

    # At 160 columns width
    modal2 = _SampleModal()
    app2 = _ThemedModalApp(modal2)
    async with app2.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert modal2.dialog is not None
        # Fixed width target is 88 outer cells
        assert modal2.dialog.outer_size.width == 88


@pytest.mark.asyncio
async def test_check_action_gating():
    """check_action prevents next_tab/previous_tab when search input contains text."""
    modal = _SampleModal()
    app = _ThemedModalApp(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        search = modal.query_one(ModalSearchInput)
        search.focus()
        await pilot.pause()

        # Empty search allows tab switching
        search.value = ""
        assert modal.check_action("next_tab", ()) is True

        # Non-empty search gates tab switching so arrows move text cursor
        search.value = "filtering text"
        assert modal.check_action("next_tab", ()) is False
        assert modal.check_action("focus_search", ()) is False


@pytest.mark.asyncio
async def test_pill_tab_click_event():
    """Clicking a PillTab widget directly activates it."""
    modal = _SampleModal()
    app = _ThemedModalApp(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        tabs = modal.query_one(PillTabs)
        installed_tab = tabs.tabs[1]
        assert tabs.active_tab == "discover"

        # Simulate click
        from textual.events import Click
        from unittest.mock import MagicMock
        click = MagicMock(spec=Click)
        installed_tab.on_click(click)

        await pilot.pause()
        assert tabs.active_tab == "installed"


class _CompactModal(OpsModalScreen[None]):
    DIALOG_CLASSES: ClassVar[str] = "-compact"

    def compose_modal(self) -> ComposeResult:
        yield Static("Compact content")


class _WorkspaceModal(OpsModalScreen[None]):
    DIALOG_CLASSES: ClassVar[str] = "-workspace"

    def compose_modal(self) -> ComposeResult:
        yield Static("Workspace content")


@pytest.mark.asyncio
async def test_dialog_classes_composition():
    """OpsModalScreen composes DIALOG_CLASSES onto ModalDialog."""
    # Default screen
    default_modal = _SampleModal()
    app = _ThemedModalApp(default_modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert default_modal.dialog is not None
        assert default_modal.dialog.has_class("ops-modal-dialog")
        assert not default_modal.dialog.has_class("-compact")
        assert not default_modal.dialog.has_class("-workspace")

    # Compact variant
    compact_modal = _CompactModal()
    app2 = _ThemedModalApp(compact_modal)
    async with app2.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert compact_modal.dialog is not None
        assert compact_modal.dialog.has_class("ops-modal-dialog")
        assert compact_modal.dialog.has_class("-compact")

    # Workspace variant
    workspace_modal = _WorkspaceModal()
    app3 = _ThemedModalApp(workspace_modal)
    async with app3.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert workspace_modal.dialog is not None
        assert workspace_modal.dialog.has_class("ops-modal-dialog")
        assert workspace_modal.dialog.has_class("-workspace")


@pytest.mark.asyncio
async def test_ctrl_tab_cycles_pill_tabs():
    """ctrl+tab and ctrl+shift+tab cycle through PillTabs."""
    modal = _SampleModal()
    app = _ThemedModalApp(modal)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tabs = modal.query_one(PillTabs)
        assert tabs.active_tab == "discover"

        # ctrl+tab switches to next tab
        await pilot.press("ctrl+tab")
        await pilot.pause()
        assert tabs.active_tab == "installed"

        # ctrl+shift+tab switches back to previous tab
        await pilot.press("ctrl+shift+tab")
        await pilot.pause()
        assert tabs.active_tab == "discover"


@pytest.mark.asyncio
async def test_compact_modal_hugs_content_in_tall_terminal():
    """-compact modal hugs small content in tall terminal and stays below 20 rows."""
    modal = _CompactModal()
    app = _ThemedModalApp(modal)

    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        assert modal.dialog is not None
        assert modal.dialog.has_class("-compact")
        assert modal.dialog.region.height < 20


@pytest.mark.asyncio
async def test_workspace_modal_fills_bounded_viewport():
    """-workspace modal fills bounded viewport (~85vh) in tall terminal."""
    modal = _WorkspaceModal()
    app = _ThemedModalApp(modal)

    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        assert modal.dialog is not None
        assert modal.dialog.has_class("-workspace")
        assert 45 <= modal.dialog.region.height <= 54


def test_all_modal_subclasses_declare_dialog_classes():
    """Every concrete OpsModalScreen subclass must declare DIALOG_CLASSES explicitly."""
    from opscloud.ui.widgets.agent_selector import AgentSelectorScreen
    from opscloud.ui.widgets.approval import ApprovalModalScreen
    from opscloud.ui.widgets.auth import AuthPromptScreen
    from opscloud.ui.widgets.auth_manager import AuthManagerScreen
    from opscloud.ui.widgets.auto_mode_notice import AutoModeNoticeScreen
    from opscloud.ui.widgets.cloud_selector import CloudProfileSelectorScreen
    from opscloud.ui.widgets.config_manager import ConfigManagerScreen, _InlineEditScreen
    from opscloud.ui.widgets.debug_console import DebugConsoleScreen
    from opscloud.ui.widgets.effort_selector import EffortSelectorScreen
    from opscloud.ui.widgets.install_confirm import InstallProviderConfirmScreen
    from opscloud.ui.widgets.mcp_viewer import (
        MCPServerErrorScreen,
        MCPToolDetailScreen,
        MCPViewerScreen,
    )
    from opscloud.ui.widgets.model_selector import ModelSelectorScreen
    from opscloud.ui.widgets.notification_settings import NotificationSettingsScreen
    from opscloud.ui.widgets.permissions_manager import PermissionsManagerScreen, _AddRuleScreen
    from opscloud.ui.widgets.plugin_manager import PluginManagerScreen
    from opscloud.ui.widgets.pool_selector import PoolSelectorScreen
    from opscloud.ui.widgets.skills_viewer import SkillDetailScreen, SkillsViewerScreen
    from opscloud.ui.widgets.theme_selector import ThemeSelectorScreen
    from opscloud.ui.widgets.thread_selector import DeleteThreadConfirmScreen, ThreadSelectorScreen
    from opscloud.ui.widgets.welcome_popup import WelcomeDetailPopup

    expected_classes: dict[type[OpsModalScreen], str] = {
        # Content-hugging defaults
        CloudProfileSelectorScreen: "",
        ModelSelectorScreen: "",
        AgentSelectorScreen: "",
        # Compact variants
        ThemeSelectorScreen: "-compact",
        EffortSelectorScreen: "-compact",
        NotificationSettingsScreen: "-compact",
        InstallProviderConfirmScreen: "-compact",
        AutoModeNoticeScreen: "-compact",
        AuthPromptScreen: "-compact",
        ApprovalModalScreen: "-compact",
        WelcomeDetailPopup: "-compact",
        SkillDetailScreen: "-compact",
        MCPToolDetailScreen: "-compact",
        MCPServerErrorScreen: "-compact",
        _InlineEditScreen: "-compact",
        _AddRuleScreen: "-compact",
        DeleteThreadConfirmScreen: "-compact",
        # Workspace variants
        ThreadSelectorScreen: "-workspace",
        PluginManagerScreen: "-workspace",
        SkillsViewerScreen: "-workspace",
        MCPViewerScreen: "-workspace",
        ConfigManagerScreen: "-workspace",
        PoolSelectorScreen: "-workspace",
        AuthManagerScreen: "-workspace",
        PermissionsManagerScreen: "-workspace",
        DebugConsoleScreen: "-workspace",
    }

    for screen_cls, expected in expected_classes.items():
        assert hasattr(screen_cls, "DIALOG_CLASSES"), f"{screen_cls.__name__} missing DIALOG_CLASSES"
        # Must be explicitly defined on the class itself, not inherited from OpsModalScreen
        assert "DIALOG_CLASSES" in screen_cls.__dict__, (
            f"{screen_cls.__name__} must declare DIALOG_CLASSES directly, not inherit it"
        )
        assert screen_cls.DIALOG_CLASSES == expected, (
            f"{screen_cls.__name__} expected DIALOG_CLASSES={expected!r}, got {screen_cls.DIALOG_CLASSES!r}"
        )




