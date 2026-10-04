"""``/goal`` — Set, show, clear, pause, resume goals with rubric auto-generation.

Drives:
  1. LLM-based rubric generation from user objectives
  2. User review via a dedicated ``GoalReviewScreen`` modal
  3. System prompt injection of accepted rubric
  4. Goal status tracking in the status bar

Grader settings (``model``, ``max-iterations``) are shared with ``/rubric``
via the same ``GoalState``. Both ``/goal model`` and ``/rubric model``
point to the same underlying fields so goal-first users can tune grading
without discovering ``/rubric``.
"""

from __future__ import annotations

from typing import Literal

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

GoalStatus = Literal["active", "blocked", "complete", "paused"]


class GoalState:
    """Mutable goal state attached to the app instance.

    Centralises all goal/rubric fields to avoid scattering state across
    ``app.*`` attributes.  Grader settings (``rubric_model``,
    ``rubric_max_iterations``) are intentionally left untouched by
    ``clear()`` — they survive ``/goal clear`` and ``/rubric clear``
    (matching reference: app.py L9854-L9857).
    """

    def __init__(self) -> None:
        # ── Goal fields ──────────────────────────────────
        self.objective: str | None = None
        self.status: GoalStatus | None = None
        self.rubric: str | None = None
        self.pending_rubric: str | None = None
        self.pending_objective: str | None = None
        self.pending_kind: str | None = None      # "create" | "amend"
        self.status_note: str | None = None

        # ── One-shot rubric (next turn only) ─────────────
        self.next_rubric: str | None = None

        # ── Grader settings (shared with /rubric) ────────
        self.rubric_model: str | None = None
        self.rubric_max_iterations: int | None = None

    def clear(self) -> None:
        """Clear goal and rubric state.  Grader settings survive."""
        self.objective = None
        self.status = None
        self.rubric = None
        self.pending_rubric = None
        self.pending_objective = None
        self.pending_kind = None
        self.status_note = None
        self.next_rubric = None
        # NOTE: rubric_model and rubric_max_iterations are NOT cleared.

    @property
    def is_active(self) -> bool:
        return self.status in {"active", "blocked"}

    @property
    def is_actionable(self) -> bool:
        return self.objective is not None and self.status in {"active", "blocked"}

    def grader_display_values(self, startup_model: str = "") -> tuple[str, str]:
        """Return display strings for the shared grader model and iteration cap.

        """
        if self.rubric_model:
            model = self.rubric_model
        elif startup_model:
            model = f"startup chat model ({startup_model})"
        else:
            model = "startup chat model"

        iterations = (
            str(self.rubric_max_iterations)
            if self.rubric_max_iterations is not None
            else "3 (SDK default)"
        )
        return model, iterations

    def to_dict(self) -> dict[str, str | None]:
        return {
            "objective": self.objective,
            "status": self.status,
            "rubric": self.rubric,
            "note": self.status_note,
        }


def get_goal_state(app: object) -> GoalState:
    """Get or create the GoalState attached to an app instance."""
    existing = getattr(app, "_goal_state", None)
    if not isinstance(existing, GoalState):
        app._goal_state = GoalState()  # type: ignore[attr-defined]
    return app._goal_state  # type: ignore[attr-defined]


def _is_grader_alias_arg(arg: str) -> bool:
    """Whether a ``/goal`` grader-alias argument is a grader value, not prose.

    Grader arguments (``clear``, a model spec like ``openai:gpt-5.1``, or an
    iteration count) are always a single token, so a multi-word argument is
    a plain-language objective that merely starts with ``model`` /
    ``max-iterations``.  Such objectives must fall through to the objective
    workflow instead of being hijacked as a grader command.

    """
    return len(arg.split()) <= 1


def _goal_usage_text() -> str:
    """Return user-facing usage instructions for goal commands.


    The command list is wrapped in a markdown fenced code block so that
    ``rich.markdown.Markdown`` preserves line breaks instead of collapsing
    them into a single paragraph.
    """
    return (
        "**Usage:**\n\n"
        "```\n"
        "/goal <objective>\n"
        "/goal amend <feedback>\n"
        "/goal new <objective>\n"
        "/goal pause\n"
        "/goal resume\n"
        "/goal show\n"
        "/goal clear\n"
        "/goal model [provider:model|clear]\n"
        "/goal max-iterations <N|clear>\n"
        "```\n\n"
        "Use `/goal` when you have a plain-language objective; opscloud will "
        "draft a checklist and ask before applying it. Once accepted, the "
        "goal stays active for this thread until paused, completed, blocked, "
        "or cleared. Follow-up prompts or `/goal <feedback>` continue amending "
        "and working toward that goal."
    )


class GoalHandler(BaseCommandHandler):
    """Set, inspect, and manage goals with LLM-driven rubric generation.

    Subcommands:
      ``/goal <objective>``              — draft acceptance criteria for a new goal
      ``/goal show`` / ``/goal status``  — display current goal state
      ``/goal clear``                    — clear the active goal and rubric
      ``/goal pause``                    — pause goal without losing state
      ``/goal resume``                   — resume a paused goal
      ``/goal amend <feedback>``         — revise criteria based on feedback
      ``/goal new <objective>``          — start a new goal, replacing any active goal
      ``/goal model [provider:model|clear]`` — set/clear grading model
      ``/goal max-iterations <N|clear>`` — set/clear max grading iterations

    ``model`` and ``max-iterations`` are grader-setting aliases shared with
    ``/rubric``.  They intercept ahead of all other subcommands, but only
    when the argument is a single token (a model spec, iteration count, or
    ``clear``); a multi-word objective that merely starts with ``model`` or
    ``max-iterations`` still falls through to the objective workflow.
    """

    @property
    def name(self) -> str:
        return "/goal"

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
        state = get_goal_state(ctx.app)
        remainder = ctx.args.strip()
        subcommand = remainder.lower()

        # ── Grader aliases (intercept first, single-token only) ──
        grader_sub, _, grader_arg = remainder.partition(" ")
        grader_sub = grader_sub.lower()
        grader_arg = grader_arg.strip()

        if grader_sub == "model" and _is_grader_alias_arg(grader_arg):
            return self._dispatch_grader_model(state, grader_arg)

        if grader_sub in {"max-iterations", "max_iterations"} and _is_grader_alias_arg(grader_arg):
            return self._dispatch_grader_max_iterations(state, grader_arg)

        # ── show / status / no args ──────────────────────
        if not remainder or subcommand in {"show", "status"}:
            return self._show_goal_state(state)

        # ── amend <feedback> ─────────────────────────────
        # model/max-iterations), not the full remainder, because it always
        # carries feedback text.
        if grader_sub == "amend":
            if not grader_arg:
                return CommandResult(success=False, message="Usage: /goal amend <feedback>")
            if not state.objective or state.status == "complete":
                return CommandResult(
                    success=False,
                    message="No active goal to amend. Use `/goal <objective>` to create one.",
                )
            return await self._amend(ctx, state, grader_arg)

        # ── pause ────────────────────────────────────────
        if subcommand == "pause":
            return await self._pause_goal(ctx, state)

        # ── resume ───────────────────────────────────────
        if subcommand == "resume":
            return await self._resume_goal(ctx, state)

        # ── accept / edit (informational) ────────────────
        if subcommand in {"accept", "edit"}:
            return CommandResult(
                success=True,
                message="Goal proposals are reviewed in the review prompt. "
                "Use `/goal <objective>` to draft criteria.",
            )

        # ── clear ────────────────────────────────────────
        if subcommand == "clear":
            from langchain_core.messages import SystemMessage
            async with ctx.app._goal_state_mutation_boundary():
                state.clear()
                self._sync_status_rubric(ctx.app, state)
                await ctx.app._persist_goal_rubric_state(notice=SystemMessage(content="Goal cleared."))
            return CommandResult(
                success=True,
                message="Goal cleared.",
                notify="Goal cleared",
                mount_as_app_message=False,
            )

        # ── explicit new / replace subcommand when goal is active ───
        if state.is_actionable and grader_sub in {"new", "replace"}:
            if not grader_arg:
                return CommandResult(
                    success=False,
                    message=f"Usage: /goal {grader_sub} <objective>",
                )
            return await self._set_objective(ctx, state, grader_arg)

        # ── conversational amendment when goal is actionable ──
        # If an active or blocked goal already exists and the user submits follow-up
        # guidance without clearing first (e.g. "/goal lets skip...", "/goal also add...",
        # or "/goal remove check"), route to amendment so the existing objective and
        # criteria are preserved and updated rather than wiped.
        if state.is_actionable:
            return await self._amend(ctx, state, remainder)

        # ── new objective ────────────────────────────────
        # If it didn't match any subcommand logic above, treat the ENTIRE
        # original string as the objective.
        return await self._set_objective(ctx, state, remainder)

    # ── Subcommand implementations ───────────────────────

    def _show_goal_state(self, state: GoalState) -> CommandResult:
        """Render active or pending goal state.


        Uses ``**bold**`` labels and ``\\n\\n`` paragraph breaks so that
        ``rich.markdown.Markdown`` renders each section clearly.
        """
        lines: list[str] = []

        if state.objective:
            status = state.status or "active"
            lines.append(f"**Goal:** {state.objective}")
            lines.append(f"**Status:** {status}")

        if state.status_note:
            lines.append(f"**Status note:** {state.status_note}")

        if state.rubric:
            lines.append(f"**Criteria:**\n\n{state.rubric}")

        # Show pending proposals
        if state.pending_objective and state.pending_rubric:
            kind_label = (
                "pending amendment review"
                if state.pending_kind == "amend"
                else "pending review"
            )
            lines.append(f"**Goal:** {state.pending_objective}")
            lines.append(f"**Status:** {kind_label}")
            lines.append(f"**Criteria:**\n\n{state.pending_rubric}")
            lines.append(
                "Review the proposal in the review prompt, or run "
                "`/goal clear` to cancel it."
            )

        if lines:
            # Append grader config
            grader_model, grader_iterations = state.grader_display_values()
            lines.append(f"**Grader:** {grader_model} · max iterations: {grader_iterations}")

            if state.objective and state.status == "active":
                lines.append(
                    "Goal is active for this thread until paused, completed, blocked, "
                    "or cleared. Follow-up prompts will continue working toward this goal."
                )
            elif state.objective and state.status == "paused":
                lines.append(
                    "Goal is paused. It remains saved, but it will not drive work or "
                    "grading until resumed."
                )

            return CommandResult(success=True, message="\n\n".join(lines))

        # No goal set — show full usage
        return CommandResult(
            success=True,
            message="No goal set.\n\n" + _goal_usage_text(),
        )

    async def _pause_goal(self, ctx: CommandContext, state: GoalState) -> CommandResult:
        """Persist a paused goal without clearing its objective or criteria.

        """
        if not state.objective or state.status == "complete":
            return CommandResult(
                success=False,
                message="No active goal to pause. Use `/goal <objective>` to create one.",
            )
        if state.status == "paused":
            return CommandResult(success=True, message="Goal is already paused.")
        if state.status == "blocked":
            return CommandResult(
                success=True,
                message="Goal is blocked and already waiting for user input. Reply to "
                "continue, or clear it with `/goal clear`.",
            )
        
        from langchain_core.messages import SystemMessage
        async with ctx.app._goal_state_mutation_boundary():
            state.status = "paused"
            self._sync_status_rubric(ctx.app, state)
            await ctx.app._persist_goal_rubric_state(notice=SystemMessage(content="Goal paused. Use `/goal resume` to continue it."))
            
        return CommandResult(
            success=True,
            message="Goal paused. Use `/goal resume` to continue it.",
            mount_as_app_message=False,
        )

    async def _resume_goal(self, ctx: CommandContext, state: GoalState) -> CommandResult:
        """Resume a paused goal from its persisted conversation state.

        """
        if not state.objective or state.status == "complete":
            return CommandResult(
                success=False,
                message="No paused goal to resume. Use `/goal <objective>` to create one.",
            )
        if state.status != "paused":
            return CommandResult(
                success=False,
                message="Goal is not paused. Use `/goal show` to inspect its current state.",
            )
            
        from langchain_core.messages import SystemMessage
        async with ctx.app._goal_state_mutation_boundary():
            state.status = "active"
            self._sync_status_rubric(ctx.app, state)
            await ctx.app._persist_goal_rubric_state(notice=SystemMessage(content="Goal resumed."))
            
        return CommandResult(success=True, message="Goal resumed.", mount_as_app_message=False)

    def _dispatch_grader_model(self, state: GoalState, arg: str) -> CommandResult:
        """Route a grader-model argument to the shared setter.

        Shared by ``/rubric model`` and ``/goal model``.
        """
        if not arg:
            return CommandResult(
                success=True,
                message=f"Grader model: {state.rubric_model or 'current chat model (default)'}\n\n"
                "Usage: /goal model <provider:model|clear>",
            )
        if arg.lower() == "clear":
            state.rubric_model = None
            return CommandResult(
                success=True,
                message="Grader model cleared (using default chat model).",
            )
        state.rubric_model = arg
        return CommandResult(
            success=True,
            message=f"Grader model set to `{arg}`.",
        )

    def _dispatch_grader_max_iterations(self, state: GoalState, arg: str) -> CommandResult:
        """Route a grader ``max-iterations`` argument to the shared setter.

        Shared by ``/rubric max-iterations`` and ``/goal max-iterations``.
        """
        if not arg:
            return CommandResult(
                success=False,
                message="Usage: /goal max-iterations <N|clear>",
            )
        if arg.lower() == "clear":
            state.rubric_max_iterations = None
            return CommandResult(
                success=True,
                message="Grader max iterations cleared (using SDK default).",
            )
        value, error = _parse_rubric_max_iterations(arg)
        if error is not None:
            return CommandResult(success=False, message=error)
        state.rubric_max_iterations = value
        return CommandResult(
            success=True,
            message=f"Grader max iterations set to {value}.",
        )

    async def _set_objective(
        self, ctx: CommandContext, state: GoalState, objective: str
    ) -> CommandResult:
        """Request goal criteria generation."""
        import uuid
        request = {
            "kind": "create",
            "request_id": str(uuid.uuid4()),
            "objective": objective,
        }
        await ctx.app._run_goal_criteria_request(request)
        return CommandResult(
            success=True,
            message=None,
            mount_as_app_message=False,
        )

    async def _amend(
        self, ctx: CommandContext, state: GoalState, feedback: str
    ) -> CommandResult:
        """Request goal criteria amendment."""
        import uuid
        request = {
            "kind": "amend",
            "request_id": str(uuid.uuid4()),
            "objective": state.objective or "",
            "criteria": state.rubric or "",
            "feedback": feedback,
        }
        await ctx.app._run_goal_criteria_request(request)
        return CommandResult(
            success=True,
            message=None,
            mount_as_app_message=False,
        )

    @staticmethod
    def _sync_status_rubric(app: object, state: GoalState) -> None:
        """Reflect active rubric and goal state in the status bar.

        """
        if app is None:
            return

        status_bar = getattr(app, "_status_bar", None)
        if status_bar is None:
            return

        set_label = getattr(status_bar, "set_rubric_label", None)
        if set_label is None:
            return

        try:
            if state.objective and state.status == "complete":
                set_label("✓ Goal complete")
            elif state.objective and state.status == "blocked":
                set_label("⚠ Goal blocked")
            elif state.objective and state.status == "paused":
                set_label("⏸ Goal paused")
            elif state.next_rubric:
                set_label("✓ Rubric: next turn")
            elif state.rubric:
                set_label("✓ Rubric set")
            else:
                set_label("")
        except Exception:
            pass  # Status bar may not be mounted yet


# ── Shared helpers ───────────────────────────────────────


def _generate_rubric(
    objective: str,
    *,
    model_spec: str | None = None,
    feedback: str | None = None,
    previous_criteria: str | None = None,
) -> str:
    """Invoke the rubric generator backend."""
    from opscloud.rubrics.generator import generate_rubric

    return generate_rubric(
        objective,
        model_spec=model_spec,
        feedback=feedback,
        previous_criteria=previous_criteria,
    )


def _parse_rubric_max_iterations(raw: str) -> tuple[int | None, str | None]:
    """Parse a grader ``max-iterations`` argument.


    Returns:
        Tuple of ``(value, error)``.  ``error`` is ``None`` on success.
    """
    raw = raw.strip()
    if raw.lower() == "clear":
        return None, None
    try:
        value = int(raw)
        if value < 1:
            return None, f"max-iterations must be a positive integer, got {value}."
        return value, None
    except ValueError:
        return None, f"Invalid max-iterations value: `{raw}`. Use a positive integer or `clear`."


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
