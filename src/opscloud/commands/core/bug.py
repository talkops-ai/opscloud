"""Bug / feedback command handler for OpsCode."""

from __future__ import annotations

import webbrowser

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel


class BugHandler(BaseCommandHandler):
    """Handler for /bug and /feedback commands."""

    @property
    def name(self) -> str:
        return "/bug"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/feedback",)

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
        url = "https://github.com/talkops-ai/opscloud/issues/new"
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return CommandResult(
            success=True,
            message=f"🐛 **Bug Report / Feedback:** Submit an issue at:\n{url}",
        )


__all__ = ["BugHandler"]
