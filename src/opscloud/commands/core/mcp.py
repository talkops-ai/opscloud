"""MCP server management command handler for OpsCloud."""

from __future__ import annotations

from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


class McpHandler(BaseCommandHandler):
    """Handler for /mcp — manage MCP server connections."""

    @property
    def name(self) -> str:
        return "/mcp"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        args = ctx.args.strip()

        # No args → mount MCPViewer widget
        if not args:
            return self._open_viewer(ctx)

        parts = args.split(maxsplit=1)
        sub = parts[0].lower()

        if sub == "status":
            return await self._status(ctx)
        if sub == "login" and len(parts) > 1:
            return await self._login(ctx, parts[1].strip())
        if sub == "reconnect":
            force = "--force" in args
            return await self._reconnect(ctx, force=force)
        return CommandResult(
            success=False,
            message="Usage: /mcp [status|login <server>|reconnect [--force]]",
        )

    def _open_viewer(self, ctx: CommandContext) -> CommandResult:
        """Push the MCP viewer modal screen."""
        if ctx.app is None:
            return CommandResult(success=False, message="App context not available.")
        try:
            from opscloud.ui.widgets.mcp_viewer import MCPViewerScreen
            servers = ctx.app.get_mcp_servers() if hasattr(ctx.app, "get_mcp_servers") else []
            ctx.app.push_screen(MCPViewerScreen(server_info=servers))
            return CommandResult(success=True, message="", mount_as_app_message=False)
        except Exception as e:
            return CommandResult(success=False, message=f"Failed to open MCP viewer: {e}")

    async def _status(self, ctx: CommandContext) -> CommandResult:
        """Show inline MCP server status."""
        if ctx.app is None or not hasattr(ctx.app, "get_mcp_servers"):
            return CommandResult(success=True, message="MCP server status not available.")

        servers = ctx.app.get_mcp_servers()
        if not servers:
            return CommandResult(success=True, message="No MCP servers configured.")

        lines = ["🔌 **MCP Servers:**\n"]
        for srv in servers:
            connected = getattr(srv, "connected", False)
            name = getattr(srv, "name", "unknown")
            tool_count = getattr(srv, "tool_count", 0)
            icon = "🟢" if connected else "🔴"
            lines.append(f"  {icon} `{name}`: {tool_count} tools")
        return CommandResult(success=True, message="\n".join(lines))

    async def _login(self, ctx: CommandContext, server_name: str) -> CommandResult:
        """Trigger OAuth/auth flow for a named MCP server."""
        if ctx.app is None:
            return CommandResult(success=False, message="App context not available.")

        # Delegate to app's MCP login flow if available
        if hasattr(ctx.app, "_start_mcp_login"):
            ctx.app._start_mcp_login(server_name)
            return CommandResult(
                success=True,
                message=f"🔐 Starting auth flow for MCP server `{server_name}`...",
            )
        return CommandResult(
            success=False,
            message=f"MCP login not available for `{server_name}`.",
        )

    async def _reconnect(self, ctx: CommandContext, *, force: bool) -> CommandResult:
        """Reconnect failed/pending MCP servers."""
        if ctx.app is None:
            return CommandResult(success=False, message="App context not available.")

        if hasattr(ctx.app, "reconnect_mcp_servers"):
            count = await ctx.app.reconnect_mcp_servers(force=force)
            return CommandResult(
                success=True,
                message=f"🔄 Reconnected {count} MCP server(s).",
            )
        if hasattr(ctx.app, "_handle_mcp_reconnect_command"):
            await ctx.app._handle_mcp_reconnect_command(force=force)
            return CommandResult(success=True, message="🔄 MCP reconnect triggered.")

        return CommandResult(success=False, message="MCP reconnect not available.")


__all__ = ["McpHandler"]
