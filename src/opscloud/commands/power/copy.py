"""Copy command handler for OpsCode."""

from __future__ import annotations

from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


class CopyHandler(BaseCommandHandler):
    """Handler for /copy command to copy latest assistant response to clipboard."""

    @property
    def name(self) -> str:
        return "/copy"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        content: str | None = None
        streaming_pending = False

        if ctx.app is not None:
            if hasattr(ctx.app, "get_latest_assistant_message"):
                content = ctx.app.get_latest_assistant_message()
            elif hasattr(ctx.app, "query"):
                try:
                    from opscloud.ui.widgets.messages import AssistantMessage
                    msgs = list(ctx.app.query(AssistantMessage))
                    for latest in reversed(msgs):
                        msg_text = getattr(latest, "content_text", None) or getattr(latest, "content", None) or "".join(getattr(latest, "_fragments", []))
                        if not msg_text or not msg_text.strip():
                            continue
                        if getattr(latest, "is_streaming", False):
                            streaming_pending = True
                            continue
                        content = msg_text
                        break
                except Exception as exc:
                    logger.debug("Failed querying AssistantMessage: %s", exc)

        if not content:
            msg = (
                "Latest assistant message is still streaming; try again in a moment."
                if streaming_pending
                else "No assistant message content available to copy."
            )
            return CommandResult(
                success=False,
                message=msg,
                notify=msg,
                notify_severity="warning",
            )

        copied = False
        try:
            import pyperclip
            pyperclip.copy(content)
            copied = True
        except Exception:
            if ctx.app is not None and hasattr(ctx.app, "copy_to_clipboard"):
                try:
                    ctx.app.copy_to_clipboard(content)
                    copied = True
                except Exception:
                    pass

        if copied:
            return CommandResult(
                success=True,
                message="Copied latest assistant message to clipboard.",
                notify="Copied latest assistant message to clipboard.",
                notify_severity="information",
            )

        return CommandResult(
            success=False,
            message="Clipboard support unavailable.",
            notify="Clipboard support unavailable.",
            notify_severity="error",
        )


__all__ = ["CopyHandler"]
