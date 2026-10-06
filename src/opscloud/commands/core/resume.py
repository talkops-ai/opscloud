"""Resume / thread management command handler for OpsCode."""

from __future__ import annotations

from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


class ResumeHandler(BaseCommandHandler):
    """Handler for /resume and /threads commands to browse or resume sessions."""

    @property
    def name(self) -> str:
        return "/threads"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/resume",)

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.IMMEDIATE_UI

    async def execute(self, ctx: CommandContext) -> CommandResult:
        args = ctx.args.strip()

        if not args:
            if ctx.app is not None and hasattr(ctx.app, "_show_thread_selector"):
                await ctx.app._show_thread_selector()
                return CommandResult(success=True, mount_as_app_message=False)
            installed = getattr(ctx.app, "_installed_screens", {}) if ctx.app else {}
            if "ThreadSelector" in installed:
                return CommandResult(
                    success=True,
                    push_screen="ThreadSelector",
                    mount_as_app_message=False,
                )
            return CommandResult(
                success=True,
                message="Opened Thread Selector.",
                mount_as_app_message=False,
            )

        if args == "-r":
            target_id: str | None = None
            if ctx.session is not None and hasattr(ctx.session, "get_recent_threads"):
                threads = await ctx.session.get_recent_threads(limit=1)
                if threads:
                    target_id = getattr(threads[0], "thread_id", str(threads[0]))
            elif ctx.app is not None and hasattr(ctx.app, "get_recent_threads"):
                threads = await ctx.app.get_recent_threads(limit=1)
                if threads:
                    target_id = getattr(threads[0], "thread_id", str(threads[0]))

            if not target_id:
                return CommandResult(success=False, message="No previous threads to resume.")
            return await self._resume_thread(ctx, target_id)

        if args.startswith("-r "):
            target_id = args[3:].strip()
            if not target_id:
                return CommandResult(success=False, message="Usage: /resume [-r [ID]]")
            return await self._resume_thread(ctx, target_id)

        return CommandResult(success=False, message="Usage: /resume [-r [ID]]")

    async def _resume_thread(self, ctx: CommandContext, thread_id: str) -> CommandResult:
        if ctx.session is not None and hasattr(ctx.session, "thread_exists"):
            exists = await ctx.session.thread_exists(thread_id)
            if not exists:
                return CommandResult(success=False, message=f"Thread not found: {thread_id}")

        if ctx.app is not None:
            if hasattr(ctx.app, "resume_thread"):
                await ctx.app.resume_thread(thread_id)
            elif hasattr(ctx.app, "_switch_thread"):
                await ctx.app._switch_thread(thread_id)

        return CommandResult(
            success=True,
            message=f"**Resumed Thread:** `{thread_id}`",
        )


__all__ = ["ResumeHandler"]
