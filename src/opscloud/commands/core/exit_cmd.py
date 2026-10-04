"""Exit command handler for OpsCode."""

from __future__ import annotations

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel


class ExitHandler(BaseCommandHandler):
    """Handler for /exit, /quit, and /q commands."""

    @property
    def name(self) -> str:
        return "/exit"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/quit", "/q")

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.ALWAYS

    async def execute(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None:
            if hasattr(ctx.app, "prepare_exit"):
                ctx.app.prepare_exit()
            if hasattr(ctx.app, "exit"):
                ctx.app.exit()
        return CommandResult(success=True, mount_as_app_message=False)


__all__ = ["ExitHandler"]
