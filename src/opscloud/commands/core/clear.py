"""Clear and force-clear command handlers for OpsCode."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import uuid

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


class ClearHandler(BaseCommandHandler):
    """Handler for /clear command to reset session and TUI view."""

    @property
    def name(self) -> str:
        return "/clear"

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
        if ctx.app is not None:
            if hasattr(ctx.app, "_pending_messages"):
                ctx.app._pending_messages.clear()
            if hasattr(ctx.app, "_queued_widgets"):
                ctx.app._queued_widgets.clear()
            if hasattr(ctx.app, "_sync_status_queued"):
                ctx.app._sync_status_queued()

            try:
                from opscloud.ui.widgets.messages import MessageList
                messages = ctx.app.query_one("#messages", MessageList)
                messages.clear()
            except Exception:
                logger.debug("No MessageList widget found to clear")

            new_thread_id = str(uuid.uuid4())
            if hasattr(ctx.app, "_session_state") and ctx.app._session_state and hasattr(ctx.app._session_state, "reset_thread"):
                try:
                    new_thread_id = ctx.app._session_state.reset_thread()
                except Exception:
                    pass
            ctx.app._agent_thread_id = new_thread_id
            if hasattr(ctx.app, "_restore_goal_rubric_state"):
                ctx.app._restore_goal_rubric_state({})

            try:
                from opscloud.ui.widgets.status import StatusBar
                status_bar = ctx.app.query_one("#status-bar", StatusBar)
                status_bar.set_status("Ready")
            except Exception:
                pass

            msg = f"Started new thread: `{new_thread_id}`"
            previous_id = getattr(ctx.session, "previous_thread_id", None) if ctx.session else None
            if previous_id:
                msg += f"\nPrevious thread: `{previous_id}` (Resume with `/resume -r`)"

            return CommandResult(success=True, message=msg)

        return CommandResult(success=True, message="Conversation cleared.")


class ForceClearHandler(BaseCommandHandler):
    """Handler for /force-clear command to interrupt work and reset session."""

    @property
    def name(self) -> str:
        return "/force-clear"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.ALWAYS

    async def execute(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None and hasattr(ctx.app, "_force_interrupt_active_work"):
            try:
                ctx.app._force_interrupt_active_work()
            except Exception as exc:
                logger.warning("Error during force_interrupt_active_work: %s", exc)

        clear_handler = ClearHandler()
        return await clear_handler.execute(ctx)


__all__ = ["ClearHandler", "ForceClearHandler"]
