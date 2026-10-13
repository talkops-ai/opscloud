"""Unit tests for Step 8: markup safety and dynamic value rendering."""

from __future__ import annotations

from typing import Any
import pytest
from rich.markup import escape
from unittest.mock import patch

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import OptionList, Static

from opscloud.config.cloud_profiles import AWSProfileInfo
from opscloud.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from opscloud.ui.theme import register_app_themes
from opscloud.ui.widgets.cloud_selector import (
    CloudProfileOption,
    CloudProfileSelectorScreen,
)
from opscloud.ui.widgets.mcp_viewer import (
    MCPServerErrorScreen,
    MCPServerHeaderItem,
    MCPToolDetailScreen,
    MCPToolItem,
    MCPViewerScreen,
)
from opscloud.ui.widgets.modal_base import format_modal_option_row
from opscloud.ui.widgets.model_selector import (
    ModelOption,
    ModelSelectorScreen,
)
from opscloud.ui.widgets.skills_viewer import (
    SkillDetailScreen,
    SkillsViewerScreen,
)


class _TestHarnessApp(App[None]):
    """Test app harness for mounting modal screens."""

    def __init__(self, screen_instance: Any) -> None:
        super().__init__()
        self._test_screen = screen_instance
        register_app_themes(self)
        self.theme = "opscloud-dark"

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


def _get_plain(obj: Any) -> str:
    """Helper to extract plain string from a widget, Content, or RenderResult."""
    if hasattr(obj, "plain"):
        return str(getattr(obj, "plain"))
    r = obj.render() if hasattr(obj, "render") and callable(getattr(obj, "render")) else obj
    if hasattr(r, "plain"):
        return str(getattr(r, "plain"))
    return str(r)


MALICIOUS_VALUE = "prod[s]-role[/s][bold]literal[/bold]"


# ── 1. format_modal_option_row Safety ─────────────────────────────────────────


def test_format_modal_option_row_with_markup_injection() -> None:
    """Verify format_modal_option_row renders brackets literally without parsing tags."""
    content = format_modal_option_row(
        label=MALICIOUS_VALUE,
        description="desc with [dim]fake[/dim] tags and [bracket]",
        badges=[("BADGE[1]", "dim")],
        meta="v[1.0.0]",
        status="[active]",
        width=80,
    )
    plain = content.plain
    # All brackets must be preserved literally in plain text
    assert MALICIOUS_VALUE in plain
    assert "[dim]fake[/dim]" in plain
    assert "[BADGE[1]]" in plain
    assert "v[1.0.0]" in plain
    assert "[active]" in plain


# ── 2. CloudProfileSelectorScreen Safety ──────────────────────────────────────


@pytest.mark.asyncio
async def test_cloud_selector_markup_safety() -> None:
    """Verify cloud profiles with bracketed names/regions render safely in list and footer."""
    prof = AWSProfileInfo(
        name=MALICIOUS_VALUE,
        region="us-[east]-1",
        account_id="1234[56]789",
        role_arn="arn:aws:iam::123:role/test[role]",
    )

    screen = CloudProfileSelectorScreen()
    screen._load_profile_data = lambda: [prof]

    app = _TestHarnessApp(screen)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()

        # Check row rendering
        opt = screen.query_one(CloudProfileOption)
        rendered_plain = str(opt.render())
        assert MALICIOUS_VALUE in rendered_plain

        # Check footer rendering
        footer = screen.query_one("#cloud-detail-footer", Static)
        footer_plain = _get_plain(footer)

        assert MALICIOUS_VALUE in footer_plain
        assert "us-[east]-1" in footer_plain
        assert "1234[56]789" in footer_plain
        assert "arn:aws:iam::123:role/test[role]" in footer_plain


# ── 3. ModelSelectorScreen Safety ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_selector_markup_safety() -> None:
    """Verify model specs with brackets render safely in option rows and footer."""
    malicious_model = (
        MALICIOUS_VALUE,
        f"Display {MALICIOUS_VALUE}",
        "custom[provider]",
    )

    with patch(
        "opscloud.ui.widgets.model_selector.get_available_models_list",
        return_value=[malicious_model],
    ):
        screen = ModelSelectorScreen()
        screen._recommended_only = False

        app = _TestHarnessApp(screen)
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()

            opts = screen.query(ModelOption)
            assert any(MALICIOUS_VALUE in str(opt.render()) for opt in opts)

            # Move selection to the custom model row to check footer
            screen._set_selected_index(1)
            await pilot.pause()

            footer = screen.query_one("#model-detail-footer", Static)
            footer_plain = _get_plain(footer)
            assert MALICIOUS_VALUE in footer_plain
            assert "custom[provider]" in footer_plain


# ── 4. MCPViewerScreen Safety ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_viewer_markup_safety() -> None:
    """Verify MCP servers and tools with brackets render safely without crashing."""
    tool = MCPToolInfo(
        name=f"tool__{MALICIOUS_VALUE}",
        description=f"Description containing {MALICIOUS_VALUE}",
        input_schema={
            "type": "object",
            "properties": {
                f"param_{MALICIOUS_VALUE}": {
                    "type": "string",
                    "description": f"Prop description with {MALICIOUS_VALUE}",
                }
            },
        },
        original_name=f"orig__{MALICIOUS_VALUE}",
    )
    server = MCPServerInfo(
        name=f"srv__{MALICIOUS_VALUE}",
        transport="stdio",
        status="ok",
        tools=(tool,),
    )

    screen = MCPViewerScreen(server_info=[server])
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()

        # Check server header
        header = screen.query_one(MCPServerHeaderItem)
        header_text = str(header.render())
        assert MALICIOUS_VALUE in header_text

        # Check tool item
        tool_item = screen.query_one(MCPToolItem)
        tool_text = str(tool_item.render())
        assert MALICIOUS_VALUE in tool_text

        # Select tool and check detail body
        screen._move_to(1)
        await pilot.pause()

        detail_body = screen.query_one("#mcp-detail-body", Vertical)
        detail_plain = " ".join(_get_plain(w) for w in detail_body.children)
        assert MALICIOUS_VALUE in detail_plain


@pytest.mark.asyncio
async def test_mcp_tool_detail_and_error_screens_markup_safety() -> None:
    """Verify MCPToolDetailScreen and MCPServerErrorScreen render malicious brackets safely."""
    tool = MCPToolInfo(
        name=f"tool_{MALICIOUS_VALUE}",
        description=f"Desc with {MALICIOUS_VALUE}",
        input_schema={"test[key]": MALICIOUS_VALUE},
    )
    tool_screen = MCPToolDetailScreen(tool_info=tool, server_name=f"server_{MALICIOUS_VALUE}")
    app1 = _TestHarnessApp(tool_screen)
    async with app1.run_test(size=(100, 25)) as pilot:
        await pilot.pause()
        content = tool_screen.query_one("#mcp-tool-detail-content")
        plain = " ".join(_get_plain(w) for w in content.children)
        assert MALICIOUS_VALUE in plain

    error_server = MCPServerInfo(
        name=f"srv_{MALICIOUS_VALUE}",
        transport="stdio",
        status="error",
        error=f"Failure traceback: {MALICIOUS_VALUE}\nInvalid syntax at [line 42]",
    )
    err_screen = MCPServerErrorScreen(server=error_server)
    app2 = _TestHarnessApp(err_screen)
    async with app2.run_test(size=(100, 25)) as pilot:
        await pilot.pause()
        err_text = err_screen.query_one(".mcp-error-text", Static)
        plain = _get_plain(err_text)
        assert MALICIOUS_VALUE in plain
        assert "[line 42]" in plain


# ── 5. SkillsViewerScreen Safety ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skills_viewer_markup_safety() -> None:
    """Verify skills with brackets render safely in list, details, and SkillDetailScreen."""
    mock_skill = {
        "name": f"skill-{MALICIOUS_VALUE}",
        "description": f"Skill description with {MALICIOUS_VALUE} and [bold]tags[/bold]",
        "source": "plugin",
        "path": f"/mock/path/{MALICIOUS_VALUE}/SKILL.md",
        "tools": [f"tool_{MALICIOUS_VALUE}"],
        "tags": [f"tag_{MALICIOUS_VALUE}"],
    }

    screen = SkillsViewerScreen(skills=[mock_skill])
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()

        # Verify OptionList contains escaped label
        opt_list = screen.query_one("#skills-options", OptionList)
        assert opt_list.option_count == 1
        opt = opt_list.get_option_at_index(0)
        assert MALICIOUS_VALUE in _get_plain(opt.prompt)

        # Verify detail container
        detail_container = screen.query_one("#skills-detail-container")
        detail_plain = " ".join(_get_plain(w) for w in detail_container.children)
        assert MALICIOUS_VALUE in detail_plain


@pytest.mark.asyncio
async def test_skill_detail_screen_markup_safety() -> None:
    """Verify SkillDetailScreen mounts and displays raw markdown with brackets without error."""
    raw_markdown = f"# Header {MALICIOUS_VALUE}\n\n- Item with [link](url)\n- Strikethrough [s]text[/s]\n- Code block `test`"
    meta = {
        "name": MALICIOUS_VALUE,
        "source": "built-in",
        "path": f"/mock/{MALICIOUS_VALUE}/SKILL.md",
        "tools": ["t1"],
    }
    screen = SkillDetailScreen(
        skill_name=MALICIOUS_VALUE,
        content=raw_markdown,
        skill_meta=meta,
    )
    app = _TestHarnessApp(screen)

    async with app.run_test(size=(100, 25)) as pilot:
        await pilot.pause()
        body = screen.query_one(".skill-detail-body", VerticalScroll)
        body_static = body.query_one(Static)
        plain = _get_plain(body_static)
        assert MALICIOUS_VALUE in plain
        assert "[link](url)" in plain
        assert "[s]text[/s]" in plain
