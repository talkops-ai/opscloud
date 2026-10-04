"""Help command handler for OpsCode."""

from __future__ import annotations

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel


class HelpHandler(BaseCommandHandler):
    """Handler for /help command providing categorized listing and per-command details."""

    @property
    def name(self) -> str:
        return "/help"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        target = ctx.args.strip()
        if target:
            return self._specific_help(ctx, target)
        return self._general_help(ctx)

    def _general_help(self, ctx: CommandContext) -> CommandResult:
        try:
            from opscloud.ui.command_registry import get_slash_commands

            commands = get_slash_commands()
            formatted_cmds = [
                f"{entry.name} {entry.argument_hint}".rstrip()
                for entry in commands
            ]
            formatted_cmds.append("/skill:<name>")
            commands_str = ", ".join(formatted_cmds)
        except Exception:
            commands_str = (
                "/bug, /clear, /compact, /context, /effort [<level>|clear], "
                "/exit, /fast, /force-clear, /help, /model, /notifications, /plan [module_path], "
                "/resume, /scrollbar, /timestamps, /version, /skill:<name>"
            )

        from opscloud.config.settings import newline_shortcut

        nl_shortcut = newline_shortcut()
        help_body = (
            f"Commands: {commands_str}\n\n"
            "Interactive Features:  \n"
            "  Enter           Submit your message  \n"
            f"  {nl_shortcut:<15} Insert newline  \n"
            "  Ctrl+X          Open prompt in external editor  \n"
            "  Ctrl+N          Review pending notifications  \n"
            "  Ctrl+\\          Toggle the debug console  \n"
            "  Shift+Tab       Toggle auto-approve mode  \n"
            "  @filename       Auto-complete files and inject content  \n"
            "  /command        Slash commands (/help, /clear, /quit)  \n"
            "  !command        Run shell commands directly  \n"
            "  !!command       Run shell commands without adding command/output to model context\n\n"
            "Docs: https://opscloud.dev/docs"
        )

        return CommandResult(success=True, message=help_body)

    def _specific_help(self, ctx: CommandContext, target: str) -> CommandResult:
        clean_target = target if target.startswith("/") else f"/{target}"
        router = getattr(ctx.app, "_command_router", None)
        handler = router.get_handler(clean_target) if router else None

        if not handler:
            return CommandResult(success=False, message=f"Unknown command: `{clean_target}`")

        lines = [
            f"**Command:** `{handler.name}`",
            f"**Category:** {handler.category.value.title()}",
            f"**Safety Level:** `{handler.safety_level.value}`",
            f"**Bypass Tier:** `{handler.bypass_tier.value}`",
        ]
        if handler.aliases:
            alias_str = ", ".join(f"`{a}`" for a in handler.aliases)
            lines.append(f"**Aliases:** {alias_str}")

        return CommandResult(success=True, message="\n".join(lines))


__all__ = ["HelpHandler"]
