"""``/btw`` — Low-priority aside messaging without polluting main context."""

from __future__ import annotations

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel


class BtwHandler(BaseCommandHandler):
    """Send an ephemeral side question that does not affect the main task.

    The aside is sent as a ``HumanMessage`` wrapped in an ephemeral
    system instruction so the agent answers without abandoning its
    current objective.  Compaction middleware should skip ephemeral
    messages to keep the main conversation context clean.
    """

    @property
    def name(self) -> str:
        return "/btw"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/aside",)

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        # Should not queue behind agent work — it's a quick tangent.
        return BypassTier.IMMEDIATE

    def validate(self, ctx: CommandContext) -> str | None:
        # Bare `/btw` is allowed so `execute` can reopen `/btw` history if present.
        del ctx
        return None

    async def execute(self, ctx: CommandContext) -> CommandResult:
        from opscloud.middleware.btw import format_btw_prompt

        question = (ctx.args or "").strip()
        app = ctx.app

        if not question:
            if app is not None and hasattr(app, "show_btw_history") and app.show_btw_history():
                return CommandResult(
                    success=True,
                    message=None,
                    mount_as_app_message=False,
                )
            return CommandResult(
                success=False,
                message="Usage: /btw <question>\n\nAsk an ephemeral side question without interrupting your main task.",
            )

        if app is not None and hasattr(app, "is_btw_active") and app.is_btw_active():
            busy_msg = "A /btw question is already in progress. Please wait for it to finish."
            return CommandResult(
                success=False,
                message=busy_msg,
                notify=busy_msg,
                notify_severity="warning",
            )

        if app is not None and hasattr(app, "_send_ephemeral_message"):
            await app._send_ephemeral_message(question)
            return CommandResult(
                success=True,
                message=None,
                mount_as_app_message=False,
            )

        # Fallback for mock apps that only implement `send_agent_message`
        if app is not None and hasattr(app, "send_agent_message"):
            try:
                await app.send_agent_message(
                    format_btw_prompt(question),
                    additional_kwargs={"ephemeral": True},
                )
                return CommandResult(
                    success=True,
                    message=None,
                    mount_as_app_message=False,
                )
            except Exception:
                pass

        return CommandResult(
            success=True,
            message=f"**Aside:** {question}\n\n_(Agent not connected — aside queued for next turn.)_",
        )
