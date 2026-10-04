"""Unit tests for /reload and /restart slash commands, formatting, and lifecycles."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opscloud.commands._base import CommandContext
from opscloud.commands.power.runtime import ReloadHandler, RestartHandler
from opscloud.mcp.mcp_info import MCPServerInfo, MCPToolInfo
from opscloud.server import ServerConfig, ServerProcess
from opscloud.ui.app import (
    OpsCloudApp,
    _ServerRespawnResult,
    _format_mcp_server_changes,
)
from opscloud.ui.command_registry import (
    COMMANDS,
    BypassTier,
    get_all_entries,
)
from opscloud.ui.widgets.messages import SystemMessage
from opscloud.ui.widgets.autocomplete import CompletionView, SlashCommandController


# ── 1. Autocomplete Order & SlashCommand Metadata Tests ──────────


class MockCompletionView(CompletionView):
    """Stub view to capture autocomplete render calls."""

    def __init__(self) -> None:
        self.suggestions: list[tuple[str, str]] = []
        self.selected_index: int = 0
        self.visible: bool = False

    def render_completion_suggestions(
        self, suggestions: list[tuple[str, str]], selected_index: int
    ) -> None:
        self.suggestions = suggestions
        self.selected_index = selected_index
        self.visible = True

    def clear_completion_suggestions(self) -> None:
        self.suggestions = []
        self.visible = False

    def replace_completion_range(self, start: int, end: int, replacement: str) -> None:
        pass


def test_autocomplete_re_order_and_descriptions():
    """Verify typing `/re` displays remember, reload, and restart in exact order."""
    entries = get_all_entries()
    view = MockCompletionView()
    controller = SlashCommandController(entries, view)

    # User types "/re" with cursor at index 3
    controller.on_text_changed("/re", 3)

    assert len(controller._suggestions) >= 3
    top_three = controller._suggestions[:3]

    expected = [
        ("/remember", "Save useful context to memory or skills"),
        ("/reload", "Reload environment and config"),
        ("/restart", "Restart the agent server"),
    ]
    assert top_three == expected


def test_slash_command_registry_metadata():
    """Verify SlashCommand specifications for /remember, /reload, /restart."""
    by_name = {cmd.name: cmd for cmd in COMMANDS}

    assert "/remember" in by_name
    remember_cmd = by_name["/remember"]
    assert remember_cmd.description == "Save useful context to memory or skills"
    assert remember_cmd.bypass_tier == BypassTier.QUEUED
    assert remember_cmd.argument_hint == "[<text>]"

    assert "/reload" in by_name
    reload_cmd = by_name["/reload"]
    assert reload_cmd.description == "Reload environment and config"
    assert reload_cmd.bypass_tier == BypassTier.QUEUED
    assert "skills" in reload_cmd.hidden_keywords
    assert "themes" in reload_cmd.hidden_keywords

    assert "/restart" in by_name
    restart_cmd = by_name["/restart"]
    assert restart_cmd.description == "Restart the agent server"
    assert restart_cmd.bypass_tier == BypassTier.ALWAYS
    assert "server" in restart_cmd.hidden_keywords


# ── 2. Command Handlers Execution Tests ─────────────────────────


@pytest.mark.asyncio
async def test_reload_handler_delegation_to_app():
    """Verify ReloadHandler schedules reload on OpsCloudApp."""
    mock_app = MagicMock()
    mock_app._schedule_reload = MagicMock()

    ctx = CommandContext(app=mock_app, raw_command="/reload", args="")
    handler = ReloadHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.mount_as_app_message is False
    assert mock_app._schedule_reload.called


@pytest.mark.asyncio
async def test_reload_handler_fallback_without_app():
    """Verify ReloadHandler fallback when app is not provided."""
    mock_settings = MagicMock()
    mock_settings.reload_from_environment.return_value = ["OPENAI_API_KEY changed"]

    ctx = CommandContext(app=None, settings=mock_settings, raw_command="/reload", args="")
    handler = ReloadHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.mount_as_app_message is True
    assert result.message is not None
    assert "Configuration reloaded. Changes:" in result.message
    assert "OPENAI_API_KEY changed" in result.message


@pytest.mark.asyncio
async def test_restart_handler_delegation_to_app():
    """Verify RestartHandler delegates to app._handle_restart_command."""
    mock_app = MagicMock()
    mock_app._handle_restart_command = AsyncMock()

    ctx = CommandContext(app=mock_app, raw_command="/restart", args="")
    handler = RestartHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.mount_as_app_message is False
    mock_app._handle_restart_command.assert_awaited_once_with(command="/restart")


@pytest.mark.asyncio
async def test_restart_handler_fallback_without_app():
    """Verify RestartHandler fallback when app is not provided."""
    ctx = CommandContext(app=None, raw_command="/restart", args="")
    handler = RestartHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.mount_as_app_message is True
    assert result.message == "Restart complete."


# ── 3. _format_mcp_server_changes Tests ──────────────────────────


def test_format_mcp_server_changes_when_current_is_none():
    """Verify formatting when current server list is None."""
    # Without error
    msg = _format_mcp_server_changes(previous=None, current=None)
    assert "MCP server changes couldn't be determined; use /mcp to check." == msg

    # With error
    msg_err = _format_mcp_server_changes(
        previous=None, current=None, error="ConnectionRefusedError"
    )
    assert "(ConnectionRefusedError)" in msg_err


def test_format_mcp_server_changes_no_baseline():
    """Verify formatting when previous is None but current servers are loaded."""
    current = [
        MCPServerInfo(name="srv1", status="ok"),
        MCPServerInfo(name="srv2", status="unauthenticated"),
        MCPServerInfo(name="err_cfg", transport="config"),
    ]
    msg = _format_mcp_server_changes(previous=None, current=current)
    assert "no baseline metadata" in msg
    assert "currently loaded: srv1" in msg
    assert "unavailable: srv2 (unauthenticated)" in msg
    assert "config errors: err_cfg" in msg


def test_format_mcp_server_changes_no_changes():
    """Verify formatting when baseline and current are identical and healthy."""
    servers = [
        MCPServerInfo(name="filesystem", status="ok"),
        MCPServerInfo(name="github", status="ok"),
    ]
    msg = _format_mcp_server_changes(previous=servers, current=servers)
    assert msg == "MCP server changes: no changes detected."


def test_format_mcp_server_changes_stuck_needing_attention():
    """Verify formatting when unchanged but a server still needs attention."""
    servers = [
        MCPServerInfo(name="filesystem", status="ok"),
        MCPServerInfo(name="github", status="unauthenticated"),
    ]
    msg = _format_mcp_server_changes(previous=servers, current=servers)
    assert "MCP server changes: no changes detected (still needs attention: github; use /mcp)." == msg


def test_format_mcp_server_changes_diffs():
    """Verify formatting for added, removed, failing, and reconfigured servers."""
    previous = [
        MCPServerInfo(name="unchanged", status="ok"),
        MCPServerInfo(name="to_remove", status="ok"),
        MCPServerInfo(name="now_failing", status="ok"),
        MCPServerInfo(
            name="tool_changed",
            status="ok",
            tools=(MCPToolInfo(name="t1"),),
        ),
    ]
    current = [
        MCPServerInfo(name="unchanged", status="ok"),
        MCPServerInfo(name="newly_added", status="ok"),
        MCPServerInfo(name="now_failing", status="error"),
        MCPServerInfo(
            name="tool_changed",
            status="ok",
            tools=(MCPToolInfo(name="t1"), MCPToolInfo(name="t2")),
        ),
    ]
    msg = _format_mcp_server_changes(previous=previous, current=current)
    assert "  - Loaded: newly_added" in msg
    assert "  - Removed: to_remove" in msg
    assert "  - Failed to load: now_failing" in msg
    assert "  - Reconfigured: tool_changed" in msg


# ── 4. OpsCloudApp /reload and /restart Lifecycle Tests ─────────


@pytest.mark.asyncio
async def test_run_reload_unlocked_report_matches_expected():
    """Verify /reload outputs the exact structured report matching dcode."""
    app = OpsCloudApp(defer_server_start=True)

    # Mock settings reload (no changes)
    app._settings = MagicMock()
    app._settings.reload_from_environment.return_value = []

    # Mock theme reload
    with patch("opscloud.ui.theme.reload_registry"), patch(
        "opscloud.ui.theme.register_app_themes"
    ):
        # Mock skill discovery (13 plugin skills)
        app.get_discovered_skills = MagicMock(
            return_value=[{"name": f"test_plugin:skill_{i}"} for i in range(13)]
        )

        # Mock plugin discovery (1 plugin, 0 hooks, 0 MCP)
        mock_plugin = MagicMock()
        mock_plugin.manifest = MagicMock()
        mock_plugin.manifest.hooks = ()
        mock_plugin_result = MagicMock(plugins=(mock_plugin,))

        # Mock server restart for plugin MCP
        app._server_proc = MagicMock()
        app._restart_server_manual_result = AsyncMock(
            return_value=_ServerRespawnResult(
                restarted=True,
                mcp_server_info=[],
                mcp_status="fresh",
                mcp_error=None,
            )
        )
        app._mcp_server_info = []

        mounted_messages = []
        app._mount_message = AsyncMock(side_effect=lambda msg: mounted_messages.append(msg))

        with patch(
            "opscloud.plugins.discovery.discover_plugins",
            return_value=mock_plugin_result,
        ), patch(
            "opscloud.plugins.adapters.mcp.plugin_mcp_configs",
            return_value=[],
        ), patch(
            "opscloud.model.config.clear_caches"
        ):
            await app._run_reload_unlocked()

        assert len(mounted_messages) == 1
        msg = mounted_messages[0]
        assert isinstance(msg, SystemMessage)
        content = getattr(msg, "_raw_content", "")

        expected_lines = [
            "Configuration reloaded. No changes detected.",
            "Model config caches cleared.",
            "Theme registry reloaded.",
            "Plugins: 1 plugin · 13 skills · 0 plugin MCP servers · 0 hooks",
            "Agent server restarted for plugin MCP.",
            "MCP server changes: no changes detected.",
        ]
        for line in expected_lines:
            assert line in content, f"Expected line {line!r} not in:\n{content}"


@pytest.mark.asyncio
async def test_run_restart_respawn_flow():
    """Verify _run_restart_respawn success emits 'Restart complete.'."""
    app = OpsCloudApp(defer_server_start=True)

    app._restart_server_manual_result = AsyncMock(
        return_value=_ServerRespawnResult(restarted=True)
    )

    mounted_messages = []
    app._mount_message = AsyncMock(side_effect=lambda msg: mounted_messages.append(msg))

    transient_mock = MagicMock()
    transient_mock.remove = AsyncMock()
    app._mount_transient_app_message = AsyncMock(return_value=transient_mock)

    restarted = await app._run_restart_respawn(propagate_errors=False)

    assert restarted is True
    assert any(
        isinstance(m, SystemMessage) and getattr(m, "_raw_content", "") == "Restart complete."
        for m in mounted_messages
    )


# ── 5. ServerProcess restart() Tests ─────────────────────────────


@pytest.mark.asyncio
async def test_server_process_restart():
    """Verify ServerProcess.restart() calls _stop_process and start."""
    cfg = ServerConfig()
    proc = ServerProcess(cfg, port=9999)

    proc._stop_process = MagicMock()
    proc.start = MagicMock()

    await proc.restart(timeout=5.0)

    assert proc._stop_process.called
    assert proc.start.called
