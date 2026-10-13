"""Unit tests for Step 7: responsive workspace panes and dynamic row widths."""

from __future__ import annotations

from typing import Any
import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList

from opscloud.config.settings import get_glyphs
from opscloud.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from opscloud.ui.permission_store import PermissionStore
from opscloud.ui.theme import register_app_themes
from opscloud.ui.widgets.auth_manager import AuthManagerScreen
from opscloud.ui.widgets.config_manager import ConfigManagerScreen
from opscloud.ui.widgets.effort_selector import EffortSelectorScreen
from opscloud.ui.widgets.mcp_viewer import (
    MCPToolDetailScreen,
    MCPViewerScreen,
)
from opscloud.ui.widgets.modal_base import (
    OpsModalScreen,
    truncate_cells,
)
from opscloud.ui.widgets.permissions_manager import PermissionsManagerScreen
from opscloud.ui.widgets.skills_viewer import (
    SkillDetailScreen,
    SkillsViewerScreen,
)
from opscloud.ui.widgets.theme_selector import ThemeSelectorScreen


class _TestHarnessApp(App[None]):
    """Test harness for mounting and resizing modal screens."""

    def __init__(self, screen_instance: Any) -> None:
        super().__init__()
        self._test_screen = screen_instance
        register_app_themes(self)
        self.theme = "opscloud-dark"

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


def _sample_skills() -> list[dict[str, Any]]:
    return [
        {
            "name": "aws-cost-analysis",
            "description": "Analyze AWS billing data, cost anomalies, and underutilized resources.",
            "source": "built-in",
            "path": "/mock/skills/aws-cost-analysis/SKILL.md",
            "tools": ["get_cost_and_usage"],
            "tags": ["aws", "cost"],
        },
        {
            "name": "k8s-pod-troubleshoot",
            "description": "Inspect failing Kubernetes pods, events, and container logs.",
            "source": "plugin",
            "path": "/mock/plugins/k8s/skills/k8s-pod-troubleshoot/SKILL.md",
            "tools": ["kubectl_get_pods"],
            "tags": ["k8s", "debug"],
        },
    ]


def _sample_mcp_servers() -> list[MCPServerInfo]:
    return [
        MCPServerInfo(
            name="aws_mcp",
            transport="stdio",
            status="ok",
            tools=(
                MCPToolInfo(
                    name="aws_ecs_list_tasks",
                    description="List ECS tasks in the specified cluster.",
                    input_schema={
                        "type": "object",
                        "properties": {"cluster": {"type": "string"}},
                    },
                    original_name="aws___ecs_list_tasks",
                ),
            ),
        )
    ]


# ── 1. Cell-aware Truncation Tests ────────────────────────────────────────────


def test_truncate_cells_ascii() -> None:
    """Verify standard ASCII truncation at cell boundary with default suffix."""
    text = "Hello, world! This is a long string."
    assert truncate_cells(text, 50) == text
    truncated = truncate_cells(text, 12)
    assert cell_len(truncated) <= 12
    assert truncated.endswith(get_glyphs().ellipsis)


def test_truncate_cells_wide_characters() -> None:
    """Verify wide character (CJK/emoji) cell-length measurement prevents overflow."""
    # Each Japanese character is 2 cells wide
    cjk_text = "日本語テスト文字列です"
    # cjk_text has 11 chars = 22 cells
    assert cell_len(cjk_text) == 22

    truncated = truncate_cells(cjk_text, 10)
    assert cell_len(truncated) <= 10
    assert truncated.endswith(get_glyphs().ellipsis)

    # With width exactly fitting without partial character split
    truncated_even = truncate_cells(cjk_text, 7)
    assert cell_len(truncated_even) <= 7


def test_truncate_cells_edge_cases() -> None:
    """Verify empty text, zero/negative width, and custom suffixes."""
    assert truncate_cells("", 10) == ""
    assert truncate_cells("hello", 0) == ""
    assert truncate_cells("hello", -5) == ""
    assert truncate_cells("abcdef", 5, suffix="..") == "abc.."


# ── 2. Modal Base Option Content Width ────────────────────────────────────────


@pytest.mark.asyncio
async def test_option_content_width_fallback() -> None:
    """Verify _option_content_width returns fallback when list or dialog is not mounted."""
    screen: OpsModalScreen[None] = OpsModalScreen()
    assert screen._option_content_width("#nonexistent", fallback=42) == 42


# ── 3. SkillsViewerScreen Responsiveness ──────────────────────────────────────


@pytest.mark.asyncio
async def test_skills_viewer_wide_layout() -> None:
    """Wide terminal (>=90 cols) mounts SkillsViewerScreen without -narrow class."""
    screen = SkillsViewerScreen(skills=_sample_skills())
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert screen.has_class("-narrow") is False
        detail = screen.query_one(".skills-detail-pane")
        assert detail is not None


@pytest.mark.asyncio
async def test_skills_viewer_narrow_layout() -> None:
    """Narrow terminal (<90 cols) adds -narrow class to SkillsViewerScreen."""
    screen = SkillsViewerScreen(skills=_sample_skills())
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(75, 30)) as pilot:
        await pilot.pause()
        assert screen.has_class("-narrow") is True


@pytest.mark.asyncio
async def test_skills_viewer_enter_opens_detail() -> None:
    """Pressing Enter or selecting an option pushes SkillDetailScreen in both modes."""
    screen = SkillsViewerScreen(skills=_sample_skills())
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(80, 25)) as pilot:
        await pilot.pause()
        screen.action_select_skill()
        await pilot.pause()

        active_screen = app.screen_stack[-1]
        assert isinstance(active_screen, SkillDetailScreen)
        assert active_screen.dialog is not None
        assert "SKILL.md" in str(active_screen.dialog.border_title)


# ── 4. MCPViewerScreen Responsiveness ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_viewer_wide_and_narrow_layout() -> None:
    """MCPViewerScreen toggles -narrow class based on terminal width."""
    screen = MCPViewerScreen(server_info=_sample_mcp_servers())
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        assert screen.has_class("-narrow") is False

        # Resize to narrow
        await pilot.resize_terminal(70, 30)
        await pilot.pause()
        assert screen.has_class("-narrow") is True

        # Resize back to wide
        await pilot.resize_terminal(105, 30)
        await pilot.pause()
        assert screen.has_class("-narrow") is False


@pytest.mark.asyncio
async def test_mcp_viewer_slash_focus_filter_and_check_action() -> None:
    """MCPViewerScreen action_focus_filter focuses search input, suppressed while typing."""
    screen = MCPViewerScreen(server_info=_sample_mcp_servers())
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        filter_input = screen.query_one("#mcp-filter", Input)

        # Trigger slash action
        screen.action_focus_filter()
        assert filter_input.has_focus is True

        # check_action suppresses focus_filter and focus_search when typing in an Input
        assert screen.check_action("focus_filter", ()) is False
        assert screen.check_action("focus_search", ()) is False


@pytest.mark.asyncio
async def test_mcp_viewer_enter_pushes_tool_detail() -> None:
    """Pressing Enter on an MCP tool item pushes MCPToolDetailScreen."""
    screen = MCPViewerScreen(server_info=_sample_mcp_servers())
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(80, 25)) as pilot:
        await pilot.pause()
        # Move selection to the tool row (index 1, since index 0 is server header)
        screen._move_to(1)
        screen.action_select_item()
        await pilot.pause()

        active_screen = app.screen_stack[-1]
        assert isinstance(active_screen, MCPToolDetailScreen)
        assert active_screen.dialog is not None
        assert "MCP Tool" in str(active_screen.dialog.border_title)


# ── 5. Dynamic Row Widths in Managers ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_theme_selector_row_formatting_responsive() -> None:
    """ThemeSelectorScreen formats rows with responsive width."""
    screen = ThemeSelectorScreen(
        current_theme="opscloud-dark",
        terminal_default="opscloud-dark",
    )
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        opt_list = screen.query_one("#theme-options", OptionList)
        assert opt_list.option_count > 0

        # Narrow resize triggers populate with narrower option row width
        await pilot.resize_terminal(60, 25)
        await pilot.pause()
        assert screen._last_row_width is not None
        assert screen._last_row_width <= 58


@pytest.mark.asyncio
async def test_effort_selector_row_formatting_responsive() -> None:
    """EffortSelectorScreen formats rows with responsive width."""
    screen = EffortSelectorScreen(
        model_spec="claude-3-7-sonnet",
        efforts=("low", "medium", "high"),
        current_effort="medium",
        default_effort="high",
    )
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(90, 25)) as pilot:
        await pilot.pause()
        opt_list = screen.query_one("#effort-options", OptionList)
        assert opt_list.option_count == 3

        await pilot.resize_terminal(55, 20)
        await pilot.pause()
        assert screen._last_row_width is not None


@pytest.mark.asyncio
async def test_config_manager_row_formatting_responsive() -> None:
    """ConfigManagerScreen formats rows with responsive width."""
    screen = ConfigManagerScreen()
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        opt_list = screen.query_one("#config-options", OptionList)
        assert opt_list.option_count > 0

        await pilot.resize_terminal(70, 25)
        await pilot.pause()
        assert screen._last_row_width is not None


@pytest.mark.asyncio
async def test_auth_manager_row_formatting_responsive() -> None:
    """AuthManagerScreen formats rows with responsive width."""
    screen = AuthManagerScreen()
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(105, 30)) as pilot:
        await pilot.pause()
        opt_list = screen.query_one("#auth-mgr-options", OptionList)
        assert opt_list.option_count > 0

        await pilot.resize_terminal(65, 25)
        await pilot.pause()
        assert screen._last_row_width is not None


@pytest.mark.asyncio
async def test_permissions_manager_row_formatting_responsive() -> None:
    """PermissionsManagerScreen formats rows with responsive width."""
    store = PermissionStore()
    screen = PermissionsManagerScreen(store=store)
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(115, 30)) as pilot:
        await pilot.pause()
        opt_list = screen.query_one("#perm-options", OptionList)
        assert opt_list.option_count > 0

        await pilot.resize_terminal(70, 25)
        await pilot.pause()
        assert screen._last_row_width is not None
