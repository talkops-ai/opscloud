"""``/rubric`` (``/criteria``) — Direct rubric management.

Grader settings (``model``, ``max-iterations``) are shared with ``/goal``
via the common ``GoalState`` object.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from opscloud.commands.power.goal import GoalState

logger = get_logger(__name__)


def _rubric_usage_text() -> str:
    """Return user-facing usage instructions for rubric commands.

    The command list is wrapped in a markdown fenced code block so that
    ``rich.markdown.Markdown`` preserves line breaks instead of collapsing
    them into a single paragraph.
    """
    return (
        "**Usage:**\n\n"
        "```\n"
        "/rubric set <criteria>\n"
        "/rubric next <criteria>\n"
        "/rubric file <path>\n"
        "/rubric show\n"
        "/rubric clear\n"
        "/rubric model [provider:model|clear]\n"
        "/rubric max-iterations <N|clear>\n"
        "```\n\n"
        "Use `/rubric next` for a one-turn quality gate. Use `/rubric set` "
        "when you want explicit acceptance criteria to persist across turns."
    )


class RubricHandler(BaseCommandHandler):
    """View, set, and manage acceptance criteria (rubric).

    Subcommands:
      ``/rubric``               — show usage + current state
      ``/rubric show``          — display current rubric
      ``/rubric set <criteria>`` — set persistent acceptance criteria
      ``/rubric next <criteria>`` — one-turn quality gate
      ``/rubric file <path>``   — load criteria from a file
      ``/rubric clear``          — remove active rubric
      ``/rubric model [spec]``  — configure grading model
      ``/rubric max-iterations <N>`` — limit grading iterations
    """

    @property
    def name(self) -> str:
        return "/rubric"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/criteria",)

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
        from opscloud.commands.power.goal import get_goal_state

        state = get_goal_state(ctx.app)
        args = ctx.args.strip()

        if not args:
            return self._show_usage(state)

        sub, _, remainder = args.partition(" ")
        sub = sub.lower()
        remainder = remainder.strip()

        # ── show / status ────────────────────────────────
        if sub in {"show", "status"}:
            return self._show_rubric_state(ctx, state)

        # ── set <criteria> ───────────────────────────────
        if sub == "set":
            if not remainder:
                return CommandResult(success=False, message="Usage: /rubric set <criteria>")
            from langchain_core.messages import SystemMessage
            async with ctx.app._goal_state_mutation_boundary():
                state.rubric = remainder
                from opscloud.commands.power.goal import GoalHandler
                GoalHandler._sync_status_rubric(ctx.app, state)
                await ctx.app._persist_goal_rubric_state(notice=SystemMessage(content="Rubric updated."))
            return CommandResult(
                success=True,
                message="Rubric set.",
                notify="Rubric set",
            )

        # ── next <criteria> ──────────────────────────────
        if sub == "next":
            if not remainder:
                return CommandResult(success=False, message="Usage: /rubric next <criteria>")
            state.next_rubric = remainder
            from opscloud.commands.power.goal import GoalHandler
            GoalHandler._sync_status_rubric(ctx.app, state)
            return CommandResult(
                success=True,
                message="Rubric set for next turn.",
                notify="Rubric set for next turn",
            )

        # ── file <path> ──────────────────────────────────
        if sub == "file":
            if not remainder:
                return CommandResult(success=False, message="Usage: /rubric file <path>")
            return await self._set_from_file(ctx, state, remainder)

        # ── clear ────────────────────────────────────────
        if sub == "clear":
            if not state.rubric and not state.next_rubric:
                return CommandResult(
                    success=True,
                    message="No rubric set. Nothing to clear.",
                )
            from langchain_core.messages import SystemMessage
            async with ctx.app._goal_state_mutation_boundary():
                state.rubric = None
                state.next_rubric = None
                from opscloud.commands.power.goal import GoalHandler
                GoalHandler._sync_status_rubric(ctx.app, state)
                await ctx.app._persist_goal_rubric_state(notice=SystemMessage(content="Rubric cleared."))
            return CommandResult(
                success=True,
                message="Rubric cleared.",
                notify="Rubric cleared",
            )

        # ── model [provider:model | clear] ───────────────
        if sub == "model":
            if not remainder or remainder.lower() == "clear":
                state.rubric_model = None
                return CommandResult(
                    success=True,
                    message="Rubric grading model cleared (using default chat model).",
                )
            state.rubric_model = remainder
            return CommandResult(
                success=True,
                message=f"Rubric grading model set to `{remainder}`.",
            )

        # ── max-iterations <N | clear> ───────────────────
        if sub in {"max-iterations", "max_iterations"}:
            if not remainder:
                return CommandResult(
                    success=False,
                    message="Usage: /rubric max-iterations <N|clear>",
                )
            if remainder.lower() == "clear":
                state.rubric_max_iterations = None
                return CommandResult(
                    success=True,
                    message="Rubric max iterations cleared (using SDK default).",
                )
            from opscloud.commands.power.goal import _parse_rubric_max_iterations
            value, error = _parse_rubric_max_iterations(remainder)
            if error is not None:
                return CommandResult(success=False, message=error)
            state.rubric_max_iterations = value
            return CommandResult(
                success=True,
                message=f"Rubric max iterations set to {value}.",
            )

        # Unknown subcommand
        return self._show_usage(state)

    def _show_rubric_state(self, ctx: CommandContext, state: "GoalState") -> CommandResult:
        """Display current rubric state."""
        startup_model = ""
        if hasattr(ctx.app, "model_id") and ctx.app.model_id:
            startup_model = str(ctx.app.model_id)
        elif hasattr(ctx.app, "settings") and ctx.app.settings.model:
            startup_model = str(ctx.app.settings.model)

        lines: list[str] = []
        if state.rubric:
            lines.append(f"Rubric:\n{state.rubric}")
        if state.next_rubric:
            lines.append(f"Next-turn rubric:\n{state.next_rubric}")

        grader_model, grader_iterations = state.grader_display_values(startup_model)

        if not lines:
            if state.rubric_model or state.rubric_max_iterations is not None:
                return CommandResult(
                    success=True,
                    message="\n\n".join([
                        "No rubric set.",
                        f"Rubric grader model: {grader_model}",
                        f"Rubric max iterations: {grader_iterations}",
                    ]),
                )
            return CommandResult(
                success=True,
                message=(
                    "No rubric set.\n\n"
                    "Set one with `/rubric set <criteria>`, or load a file:\n"
                    "  /rubric set tests pass; keep the diff minimal\n"
                    "  /rubric file ./rubric.md"
                ),
            )

        lines.extend([
            f"Rubric grader model: {grader_model}",
            f"Rubric max iterations: {grader_iterations}",
        ])
        return CommandResult(success=True, message="\n\n".join(lines))

    def _show_usage(self, state: "GoalState") -> CommandResult:
        """Show usage help with current state if present."""
        usage = _rubric_usage_text()

        state_parts: list[str] = []
        if state.rubric:
            state_parts.append("Rubric is set.")
        if state.next_rubric:
            state_parts.append("Next-turn rubric is set.")
        if state.rubric_model:
            state_parts.append(f"Rubric grader model: {state.rubric_model}")
        if state.rubric_max_iterations is not None:
            state_parts.append(f"Rubric max iterations: {state.rubric_max_iterations}")
        if state.rubric or state.next_rubric:
            state_parts.append("Use /rubric show to view.")

        if state_parts:
            usage += "\n\nCurrent state:\n" + "\n".join(f"  - {line}" for line in state_parts)

        return CommandResult(success=True, message=usage)

    async def _set_from_file(self, ctx: CommandContext, state: "GoalState", file_path: str) -> CommandResult:
        """Load criteria from a file."""
        path = Path(file_path).expanduser()
        if not path.is_file():
            return CommandResult(
                success=False,
                message=f"File not found: `{file_path}`",
            )

        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            return CommandResult(
                success=False,
                message=f"Could not read file: {exc}",
            )

        if not content:
            return CommandResult(
                success=False,
                message=f"File is empty: `{file_path}`",
            )

        from langchain_core.messages import SystemMessage
        async with ctx.app._goal_state_mutation_boundary():
            state.rubric = content
            from opscloud.commands.power.goal import GoalHandler
            GoalHandler._sync_status_rubric(ctx.app, state)
            await ctx.app._persist_goal_rubric_state(notice=SystemMessage(content=f"Rubric loaded from file {path.name}"))
        return CommandResult(
            success=True,
            message=f"Rubric set from {path.name}.",
            notify=f"Rubric set from {path.name}",
        )
