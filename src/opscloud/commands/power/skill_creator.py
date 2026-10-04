"""``/skill-creator`` — Skill creation workflow (alias for ``/skill:skill-creator``)."""

from __future__ import annotations

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


class SkillCreatorHandler(BaseCommandHandler):
    """Launch the built-in skill-creator skill to scaffold a new skill.

    Convenience alias for ``/skill:skill-creator``.  The rewrite lets
    users discover this command before skill loading completes.
    """

    @property
    def name(self) -> str:
        return "/skill-creator"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        args = ctx.args.strip()

        # Rewrite to /skill:skill-creator <args> and delegate
        rewritten = f"/skill:skill-creator {args}" if args else "/skill:skill-creator"

        app = ctx.app
        if app is not None and hasattr(app, "_handle_skill_command"):
            try:
                await app._handle_skill_command(rewritten)
                return CommandResult(
                    success=True,
                    message=None,
                    mount_as_app_message=False,
                )
            except Exception as exc:
                logger.warning("Error delegating /skill-creator to app: %s", exc)

        # Fallback: try dispatching through the skill invoke handler directly
        from opscloud.commands.power.skill_invoke import SkillInvokeHandler

        handler = SkillInvokeHandler()
        # Create a new context with the rewritten command
        from opscloud.commands._base import CommandContext as Ctx

        new_ctx = Ctx(
            app=ctx.app,
            session=ctx.session,
            agent=ctx.agent,
            settings=ctx.settings,
            raw_command=rewritten,
            args=args,
            thread_id=ctx.thread_id,
            model_spec=ctx.model_spec,
        )
        return await handler.execute(new_ctx)
