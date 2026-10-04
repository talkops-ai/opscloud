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
        if not ctx.args or not ctx.args.strip():
            return "Usage: /btw <question>\n\nAsk an ephemeral side question without interrupting your main task."
        return None

    async def execute(self, ctx: CommandContext) -> CommandResult:
        question = ctx.args.strip()

        # Build ephemeral wrapper that instructs the agent to answer
        # without modifying its current task state.
        ephemeral_prompt = (
            "[ASIDE — ephemeral side question, do NOT alter your current task or goal state]\n\n"
            f"{question}\n\n"
            "[/ASIDE — return to your main task after answering]"
        )

        app = ctx.app
        if app is not None and hasattr(app, "_send_ephemeral_message"):
            try:
                await app._send_ephemeral_message(ephemeral_prompt)
                return CommandResult(
                    success=True,
                    message=None,
                    mount_as_app_message=False,
                )
            except Exception:
                pass

        # Fallback: treat as a regular message with aside wrapper
        if app is not None and hasattr(app, "send_agent_message"):
            try:
                await app.send_agent_message(
                    ephemeral_prompt,
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
            message=f"💬 **Aside:** {question}\n\n_(Agent not connected — aside queued for next turn.)_",
        )
