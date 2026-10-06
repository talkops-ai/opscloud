"""Cross-cutting safety guards for command execution."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from typing import TYPE_CHECKING

from opscloud.commands._types import SafetyLevel

if TYPE_CHECKING:
    from opscloud.commands._base import BaseCommandHandler, CommandContext

logger = get_logger(__name__)


async def require_confirmation(handler: BaseCommandHandler, ctx: CommandContext) -> bool:
    """Prompt user for Human-In-The-Loop (HITL) confirmation on high-risk commands.

    Uses the existing ApprovalModalScreen from opscloud.ui.approval.
    Returns True if approved, False if cancelled or denied.
    """
    if handler.safety_level not in (SafetyLevel.HIGH_RISK, SafetyLevel.DESTRUCTIVE):
        return True

    severity = "[HIGH RISK]" if handler.safety_level == SafetyLevel.HIGH_RISK else "[DESTRUCTIVE]"
    description = f"{severity}: {handler.name}\n{ctx.raw_command}"

    # If app supports screen pushing (Textual app in TUI mode)
    if ctx.app is not None and hasattr(ctx.app, "push_screen_wait"):
        try:
            from opscloud.ui.widgets.approval import ApprovalModalScreen

            screen = ApprovalModalScreen(
                tool_name=handler.name,
                call_id=f"cmd-{handler.name}",
                args={"raw_command": ctx.raw_command, "description": description},
            )
            result = await ctx.app.push_screen_wait(screen)
            if hasattr(result, "approved") and isinstance(result.approved, bool):
                return result.approved
            if isinstance(result, bool):
                return result
            return False
        except Exception:
            logger.exception("Failed to prompt confirmation dialog for %s — denying execution", handler.name)
            return False

    logger.warning("No TUI runtime available to prompt HITL for %s — denying execution", handler.name)
    return False


__all__ = ["require_confirmation"]
