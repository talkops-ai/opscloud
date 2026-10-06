import asyncio
from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


class PluginsHandler(BaseCommandHandler):
    """Handler for /plugins — open plugin manager or list plugins."""

    @property
    def name(self) -> str:
        return "/plugins"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        # If subcommand arguments were passed, route to CLI plugin executor
        args_text = (ctx.args or "").strip()
        if args_text:
            import shlex
            import argparse
            from opscloud.plugins.commands_cli import setup_plugin_parser, execute_plugin_command

            parser = argparse.ArgumentParser(prog="opscloud", add_help=False)
            subparsers = parser.add_subparsers()
            setup_plugin_parser(subparsers)
            try:
                parsed_args = parser.parse_args(["plugin"] + shlex.split(args_text))
                output = execute_plugin_command(parsed_args)
                return CommandResult(success=True, message=output or "")
            except SystemExit:
                return CommandResult(success=False, message=f"Failed to execute command: /plugins {args_text}")
            except Exception as e:
                return CommandResult(success=False, message=f"Error: {e}")

        # No args passed: try to push PluginManager screen in TUI mode
        if ctx.app and hasattr(ctx.app, "_show_plugin_manager"):
            res = ctx.app._show_plugin_manager()
            if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                await res
            return CommandResult(success=True, message="", mount_as_app_message=False)

        # Fallback: check app._discovered_plugins if set on mock app
        if ctx.app and getattr(ctx.app, "_discovered_plugins", None):
            plugins_list = ctx.app._discovered_plugins or []
            lines = ["**Installed Plugins:**\n"]
            for p in plugins_list:
                name = getattr(p, "name", str(p))
                desc = getattr(p, "description", "")
                status = "[ok]" if getattr(p, "healthy", True) else "[error]"
                lines.append(f"  {status} `{name}`: {desc}")
            return CommandResult(success=True, message="\n".join(lines))

        # Fallback: list discovered marketplace plugins in CLI mode
        from opscloud.plugins import discover_marketplace_plugins, list_available_plugins
        _project_root = getattr(ctx.settings, "project_root", None) if ctx.settings else None
        result = discover_marketplace_plugins(project_root=_project_root)
        plugins = result.plugins
        if not plugins:
            available = list_available_plugins(project_root=_project_root)
            if available:
                lines = ["**Available Plugins:**\n"]
                for p_id, desc, enabled in available:
                    status = "[enabled]" if enabled else "[disabled]"
                    lines.append(f"  {status} `{p_id}`: {desc}")
                return CommandResult(success=True, message="\n".join(lines))
            return CommandResult(success=True, message="No plugins installed or discovered.")

        lines = ["**Installed Plugins:**\n"]
        for p in plugins:
            name = getattr(p, "name", str(p))
            p_id = getattr(p, "plugin_id", name)
            lines.append(f"  • `{p_id}` (v{p.version or '1.0'})")
        return CommandResult(success=True, message="\n".join(lines))




__all__ = ["PluginsHandler"]
