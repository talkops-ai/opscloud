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
        subcmd = (ctx.args or "").strip().lower()
        if subcmd == "btw":
            return self._execute_copy_btw(ctx)

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

        if self._copy_text(ctx.app, content):
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

    def _execute_copy_btw(self, ctx: CommandContext) -> CommandResult:
        """Copy the visible or latest `/btw` answer (`copyBtwAnswer` parity)."""
        app = ctx.app
        answer: str | None = None
        err_msg: str | None = None

        if app is not None and hasattr(app, "get_btw_copy_status"):
            answer, err_msg = app.get_btw_copy_status()
        else:
            err_msg = "No /btw response to copy."

        if err_msg or not answer:
            msg = err_msg or "No /btw response to copy."
            return CommandResult(
                success=False,
                message=msg,
                notify=msg,
                notify_severity="warning",
            )

        if self._copy_text(app, answer):
            return CommandResult(
                success=True,
                message="Copied /btw response to clipboard.",
                notify="Copied /btw response to clipboard.",
                notify_severity="information",
            )

        return CommandResult(
            success=False,
            message="Clipboard support unavailable.",
            notify="Clipboard support unavailable.",
            notify_severity="error",
        )

    @staticmethod
    def _copy_text(app: object | None, content: str) -> bool:
        copied = False
        try:
            import pyperclip
            pyperclip.copy(content)
            copied = True
        except Exception:
            if app is not None and hasattr(app, "copy_to_clipboard"):
                try:
                    app.copy_to_clipboard(content)  # type: ignore[attr-defined]
                    copied = True
                except Exception:
                    pass
        return copied


__all__ = ["CopyHandler"]
