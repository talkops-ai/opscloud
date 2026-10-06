"""Unit tests for MCPViewerScreen, /mcp command handler, and app metadata lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from textual.app import App, ComposeResult

from opscloud.commands._base import CommandContext
from opscloud.commands.core.mcp import McpHandler
from opscloud.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from opscloud.mcp.preload import (
    clear_cached_mcp_server_infos,
    set_cached_mcp_server_info,
)
from opscloud.ui.widgets.mcp_viewer import (
    MCPServerHeaderItem,
    MCPToolDetailScreen,
    MCPToolItem,
    MCPViewerScreen,
    clean_server_name,
    clean_tool_name,
)


class DummyViewerApp(App):
    """Minimal textual app to mount and test MCPViewerScreen."""

    def __init__(self, screen_instance: MCPViewerScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_mcp_viewer_empty_state_mounts_scroll_and_help() -> None:
    """Verify that when server_info is empty, the scroll list and help text are mounted properly."""
    screen = MCPViewerScreen(server_info=[])
    app = DummyViewerApp(screen)

    async with app.run_test() as pilot:
        # Title is mounted
        title = screen.query_one(".mcp-viewer-title")
        assert title is not None
        assert "MCP Servers" in str(title.render())

        # Filter input should NOT be mounted when no servers exist
        filter_inputs = screen.query("#mcp-filter")
        assert len(filter_inputs) == 0

        # Scroll container MUST be mounted (not skipped by if self._server_info)
        scroll = screen.query_one(".mcp-list")
        assert scroll is not None

        # Empty state message must be mounted in scroll
        empty_msg = screen.query_one(".mcp-empty")
        assert empty_msg is not None
        assert "No MCP servers configured" in str(empty_msg.render())

        # Help footer MUST be mounted
        help_text = screen.query_one(".mcp-viewer-help")
        assert help_text is not None
        rendered_help = str(help_text.render())
        assert "navigate" in rendered_help
        assert "Esc close" in rendered_help


@pytest.mark.asyncio
async def test_mcp_viewer_populated_state_mounts_filter_and_tools() -> None:
    """Verify that when server_info has servers, filter, headers, and tools are mounted."""
    server = MCPServerInfo(
        name="plugin__aws-compute_aws-mcp",
        transport="stdio",
        status="ok",
        tools=(
            MCPToolInfo(
                name="plugin__aws-compute_aws-mcp_get_tasks_9badd4f791c6",
                description="List ECS tasks",
                input_schema={"type": "object", "properties": {"cluster": {"type": "string"}}},
                original_name="aws___get_tasks",
            ),
        ),
    )
    screen = MCPViewerScreen(server_info=[server])
    app = DummyViewerApp(screen)

    async with app.run_test() as pilot:
        title = screen.query_one(".mcp-viewer-title")
        assert "1 server, 1 tool" in str(title.render())

        # Filter input is present
        filter_input = screen.query_one("#mcp-filter")
        assert filter_input is not None

        # Server header item is present
        header = screen.query_one(MCPServerHeaderItem)
        assert header is not None
        assert "plugin__aws-compute_aws-mcp" in str(header.render())

        # Tool item is present
        tool_item = screen.query_one(MCPToolItem)
        assert tool_item is not None
        assert tool_item.tool_description == "List ECS tasks"
        assert "plugin__aws-compute_aws-mcp_get_tasks" in str(tool_item.render())

        # Test expand / collapse
        tool_item.toggle_expand()
        assert tool_item._expanded is True
        expanded_text = str(tool_item.render())
        assert "Parameters:" in expanded_text
        assert "cluster: string" in expanded_text


@pytest.mark.asyncio
async def test_mcp_viewer_refresh_server_info_in_place() -> None:
    """Verify refresh_server_info dynamically updates the viewer when background probe finishes."""
    screen = MCPViewerScreen(server_info=[], connecting=True)
    app = DummyViewerApp(screen)

    async with app.run_test() as pilot:
        # Initially shows loading placeholder
        empty_msg = screen.query_one(".mcp-empty")
        assert "Loading MCP tools..." in str(empty_msg.render())

        # Now simulate background preload finishing
        fresh_server = MCPServerInfo(
            name="test-server",
            transport="http",
            status="ok",
            tools=(
                MCPToolInfo(name="test_tool", description="Test Description"),
            ),
        )
        await screen.refresh_server_info([fresh_server])
        await pilot.pause()

        # Should now have server header and tool item
        header = screen.query_one(MCPServerHeaderItem)
        assert header is not None
        assert "test-server" in str(header.render())

        tool_item = screen.query_one(MCPToolItem)
        assert tool_item is not None
        assert "test_tool" in str(tool_item.render())


@pytest.mark.asyncio
async def test_mcp_command_handler_open_viewer_and_cached_fallback() -> None:
    """Verify /mcp command uses cached servers if app._mcp_server_info is empty."""
    clear_cached_mcp_server_infos()
    cached_server = MCPServerInfo(
        name="cached-server",
        transport="stdio",
        status="ok",
        tools=(MCPToolInfo(name="cached_tool", description="Desc"),),
    )
    set_cached_mcp_server_info(cached_server)

    mock_app = MagicMock()
    mock_app._mcp_server_info = []
    mock_app.get_mcp_servers = MagicMock(return_value=[])
    mock_app.push_screen = MagicMock()

    ctx = CommandContext(args="", app=mock_app)
    handler = McpHandler()

    res = await handler.execute(ctx)
    assert res.success is True
    assert mock_app.push_screen.called

    # Screen pushed should have the cached servers
    pushed_screen = mock_app.push_screen.call_args[0][0]
    assert isinstance(pushed_screen, MCPViewerScreen)
    assert len(pushed_screen._server_info) == 1
    assert pushed_screen._server_info[0].name == "cached-server"
    # App's internal server info should also be updated
    assert mock_app._mcp_server_info == [cached_server]


@pytest.mark.asyncio
async def test_mcp_command_status() -> None:
    """Verify /mcp status command renders server status and tool count."""
    mock_app = MagicMock()
    mock_server = MCPServerInfo(
        name="prod-aws",
        transport="stdio",
        status="ok",
        tools=(
            MCPToolInfo(name="ec2_list", description="List"),
            MCPToolInfo(name="s3_list", description="List"),
        ),
    )
    mock_app.get_mcp_servers = MagicMock(return_value=[mock_server])

    ctx = CommandContext(args="status", app=mock_app)
    handler = McpHandler()

    res = await handler.execute(ctx)
    assert res.success is True
    assert "prod-aws" in res.message
    assert "2 tools" in res.message


@pytest.mark.asyncio
async def test_opscloud_app_preload_metadata_background() -> None:
    """Verify OpsCloudApp._preload_mcp_metadata_background loads servers and refreshes viewer."""
    from opscloud.ui.app import OpsCloudApp

    app = OpsCloudApp(defer_server_start=True, mcp_preload_kwargs={"no_mcp": False})
    assert app._mcp_server_info == []

    mock_infos = [
        MCPServerInfo(
            name="bg-server",
            transport="stdio",
            status="ok",
            tools=(MCPToolInfo(name="bg_tool", description=""),),
        )
    ]

    with patch("opscloud.mcp.preload.preload_mcp_server_info", AsyncMock(return_value=mock_infos)):
        await app._preload_mcp_metadata_background()
        assert app._mcp_server_info == mock_infos
        assert app.get_mcp_servers() == mock_infos


def test_clean_server_name() -> None:
    """Test cleaning of raw scoped and unscoped server names."""
    # Plugin scoped with hash
    display, scope = clean_server_name(
        "plugin__aws-compute_talkops-devops-plugins_35da5a75__aws-mcp"
    )
    assert display == "aws-mcp"
    assert scope == "aws-compute"

    # Subagent scoped
    display, scope = clean_server_name("subagent__helm-operator__talkops-helm-mcp")
    assert display == "talkops-helm-mcp"
    assert scope == "helm-operator"

    # Plain server name
    display, scope = clean_server_name("github")
    assert display == "github"
    assert scope is None


def test_clean_tool_name() -> None:
    """Test cleaning of raw and namespaced tool identifiers."""
    # Original name with triple underscore namespace
    assert clean_tool_name("plugin__aws_get_tasks_12345678", "aws___get_tasks") == "get_tasks"

    # Original name without namespace
    assert clean_tool_name("test_tool", "get_tasks") == "get_tasks"

    # Scoped fallback without original name
    assert clean_tool_name("plugin__aws-compute_talkops_aws___get_presigned_url_87eb84793a4d") == "get_presigned_url"

    # Double underscore fallback
    assert clean_tool_name("aws__run_script_f0b5243c096e") == "run_script"


@pytest.mark.asyncio
async def test_mcp_viewer_two_pane_and_live_details() -> None:
    """Verify two-pane layout mounts list pane, detail pane, and updates details live."""
    server = MCPServerInfo(
        name="plugin__aws-compute_talkops-devops-plugins_35da5a75__aws-mcp",
        transport="stdio",
        status="ok",
        tools=(
            MCPToolInfo(
                name="plugin__aws-compute_talkops_aws___get_tasks_9badd4f791c6",
                description="Poll status of long-running tasks.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_ids": {"type": "array", "description": "Up to 3 task IDs"},
                        "poll_iteration": {"type": "integer"},
                    },
                    "required": ["task_ids"],
                },
                original_name="aws___get_tasks",
            ),
        ),
    )
    screen = MCPViewerScreen(server_info=[server])
    app = DummyViewerApp(screen)

    async with app.run_test() as pilot:
        # Verify two-pane layout containers are present
        assert screen.query_one(".mcp-body") is not None
        assert screen.query_one(".mcp-list-pane") is not None
        assert screen.query_one(".mcp-detail-pane") is not None
        assert screen.query_one(".mcp-detail-title") is not None

        detail_body = screen.query_one("#mcp-detail-body")
        assert detail_body is not None

        # Initially index 0 is server header
        rendered_server_details = " ".join(str(c.render()) for c in detail_body.children)
        assert "aws-mcp" in rendered_server_details
        assert "Connected" in rendered_server_details

        # Move selection to tool (index 1)
        screen._move_to(1)
        await pilot.pause()

        rendered_tool_details = " ".join(str(c.render()) for c in detail_body.children)
        assert "get_tasks" in rendered_tool_details
        assert "Poll status of long-running tasks." in rendered_tool_details
        assert "task_ids" in rendered_tool_details
        assert "required" in rendered_tool_details


@pytest.mark.asyncio
async def test_mcp_tool_detail_screen_content() -> None:
    """Verify MCPToolDetailScreen displays comprehensive tool and JSON schema info."""
    tool_info = MCPToolInfo(
        name="test_tool_full",
        description="Detailed test description",
        input_schema={"type": "object", "properties": {"target": {"type": "string"}}},
        original_name="test_tool",
    )
    modal = MCPToolDetailScreen(tool_info=tool_info, server_name="test-server")
    app = DummyViewerApp(modal)

    async with app.run_test() as pilot:
        content = modal.query_one("#mcp-tool-detail-content")
        assert content is not None
        rendered = " ".join(str(c.render()) for c in content.children)
        assert "test_tool" in rendered
        assert "Detailed test description" in rendered
        assert "test-server" in rendered
        assert '"target"' in rendered

