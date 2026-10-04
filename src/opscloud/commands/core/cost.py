"""Cost / token usage command handler for OpsCode (deprecated)."""

from __future__ import annotations

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel


class CostHandler(BaseCommandHandler):
    """Handler for deprecated /cost and /tokens commands."""

    @property
    def name(self) -> str:
        return "/cost"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/tokens",)

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
        return CommandResult(
            success=True,
            message=(
                "The /cost and /tokens commands have been deprecated.\n"
                "Active context window usage (percentage and token count) and estimated "
                "cumulative cost are now continuously displayed on the status bar "
                "(e.g. 'Context: 7% / Tokens: 13.9K • $0.04')."
            ),
        )


__all__ = ["CostHandler"]
