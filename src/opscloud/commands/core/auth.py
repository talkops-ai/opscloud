"""Authentication command handlers for OpsCode."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import os

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


class LoginHandler(BaseCommandHandler):
    """Handler for /login — open auth manager to manage API keys."""

    @property
    def name(self) -> str:
        return "/login"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/auth", "/connect")

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.IMMEDIATE_UI

    async def execute(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is None:
            return CommandResult(success=False, message="App context not available.")

        from opscloud.ui.widgets.auth_manager import AuthManagerScreen
        initial_provider = ctx.args.strip() or None
        screen = AuthManagerScreen(initial_provider=initial_provider)

        def _on_close(_result) -> None:
            # Refocus chat input after auth manager closes
            if hasattr(ctx.app, "_chat_input") and ctx.app._chat_input:
                ctx.app._chat_input.focus_input()
            if hasattr(ctx.app, "maybe_start_deferred_server"):
                import asyncio
                asyncio.create_task(ctx.app.maybe_start_deferred_server())

        ctx.app.push_screen(screen, _on_close)
        return CommandResult(success=True, message="", mount_as_app_message=False)


class LogoutHandler(BaseCommandHandler):
    """Handler for /logout — revoke stored credentials."""

    @property
    def name(self) -> str:
        return "/logout"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        from opscloud.model.config import revoke_provider_credentials

        provider_arg = ctx.args.strip() or None
        cleared = revoke_provider_credentials(provider_arg, settings=ctx.settings)

        if cleared:
            names = ", ".join(cleared)
            return CommandResult(
                success=True,
                message=f"Logged out. Credentials revoked and removed from disk for: `{names}`",
            )
        return CommandResult(
            success=True,
            message="No active credentials found to revoke.",
        )


__all__ = ["LoginHandler", "LogoutHandler"]
