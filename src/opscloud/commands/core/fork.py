"""Fork / branch conversation command handler for OpsCloud.

Implements the ``/fork`` (alias ``/branch``) command to clone the current
conversation thread at a turn boundary and switch the active session to the
newly branched thread.
"""

from __future__ import annotations

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.middleware.goal_state_notice import build_fork_notice_message
from opscloud.state.session import SessionManager
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


class ForkHandler(BaseCommandHandler):
    """Handler for ``/fork`` (alias ``/branch``) to branch the current conversation."""

    @property
    def name(self) -> str:
        return "/fork"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/branch",)

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.IMMEDIATE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        # 1. Precondition: active conversation exists
        source_thread_id = (
            ctx.thread_id
            or (getattr(ctx.app, "_agent_thread_id", None) if ctx.app is not None else None)
            or (getattr(ctx.session, "thread_id", None) if ctx.session is not None else None)
        )
        if not isinstance(source_thread_id, str) or not source_thread_id.strip():
            return CommandResult(
                success=False,
                message="No active conversation to fork.",
            )
        source_thread_id = source_thread_id.strip()

        # 2. Precondition: must fork at a turn boundary
        if ctx.app is not None and (
            getattr(ctx.app, "_agent_running", False) is True
            or getattr(ctx.app, "_shell_running", False) is True
        ):
            return CommandResult(
                success=False,
                message=(
                    "Cannot fork conversation while a turn is in progress. "
                    "Wait for the current turn to finish first."
                ),
            )

        # 3. Precondition: cannot fork a subagent conversation
        if ctx.app is not None:
            agent_session = getattr(ctx.app, "_agent_session_state", None)
            if agent_session is not None and getattr(agent_session, "is_active", False) is True:
                return CommandResult(
                    success=False,
                    message="Cannot fork a subagent conversation; fork the parent conversation instead.",
                )

        # 4. Perform thread fork and switch active session
        previous_thread_id = source_thread_id
        new_thread_id: str | None = None

        if ctx.app is not None and callable(getattr(ctx.app, "fork_thread", None)):
            fork_res = await ctx.app.fork_thread(source_thread_id=source_thread_id)
            if isinstance(fork_res, tuple) and len(fork_res) == 2:
                previous_thread_id, new_thread_id = str(fork_res[0]), str(fork_res[1])
            elif isinstance(fork_res, str) and fork_res:
                new_thread_id = fork_res
        elif ctx.agent is not None and callable(getattr(ctx.agent, "afork_thread", None)):
            new_thread_id = await ctx.agent.afork_thread(source_thread_id)
        else:
            new_thread_id = await SessionManager.fork_thread(source_thread_id)
            if ctx.agent is not None and callable(getattr(ctx.agent, "aupdate_state", None)):
                notice_msg = build_fork_notice_message(
                    source_thread_id=source_thread_id,
                    new_thread_id=new_thread_id,
                )
                try:
                    await ctx.agent.aupdate_state(
                        {"configurable": {"thread_id": new_thread_id}},
                        {"messages": [notice_msg]},
                    )
                except Exception:
                    logger.debug("Could not append <fork_notice> via agent.aupdate_state", exc_info=True)

        if not new_thread_id:
            return CommandResult(
                success=False,
                message="Failed to fork conversation.",
            )

        if ctx.session is not None:
            try:
                ctx.session.previous_thread_id = previous_thread_id
                ctx.session.thread_id = new_thread_id
            except Exception:
                pass

        return CommandResult(
            success=True,
            message=(
                f"Forked conversation (`{previous_thread_id}` → `{new_thread_id}`).\n"
                f"To go back, use: `/resume {previous_thread_id}`"
            ),
            data={
                "previous_thread_id": previous_thread_id,
                "new_thread_id": new_thread_id,
            },
        )


__all__ = ["ForkHandler"]
