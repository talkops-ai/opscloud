"""Goal management tools exposed to the agent for persisted goals.

These tools let the model inspect and update the goal lifecycle.
They operate on checkpointed ``PrivateStateAttr`` channels shared between the
``GoalToolsMiddleware`` (which registers them) and ``ResumeState`` (which
declares the channels).

The tools are intentionally constrained:

- ``get_goal`` / ``get_rubric`` are read-only projections.
- ``update_goal`` can only mark a goal ``complete`` or ``blocked``; creation,
  pausing, resuming, and clearing are user-controlled actions.
- Completion is **staged** via ``_pending_goal_completion_note`` rather than
  committed directly, so ``RubricMiddleware`` can verify acceptance criteria
  before the status change is recorded.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NotRequired,
    TypedDict,
)

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import Field

from opscloud.state.goal_channels import (
    GoalRubricChannels,
    GoalStatus,
    coerce_goal_status,
)
from opscloud.state.goal_state_limits import (
    GOAL_STATUS_NOTE_CHAR_LIMIT,
    GoalStateSizeError,
    validate_goal_status_note,
)

if TYPE_CHECKING:
    pass

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

GOAL_TOOL_NAMES = frozenset({"get_goal", "get_rubric", "update_goal"})
"""Tool names used by behavioral absence gates and middleware contract tests."""


# ---------------------------------------------------------------------------
# State projection used by tools
# ---------------------------------------------------------------------------


class GoalToolState(GoalRubricChannels):
    """State fields used by goal tools.

    Inherits the shared ``_goal_*``/``_sticky_rubric`` channels (with their
    ``PrivateStateAttr`` markers) from ``GoalRubricChannels``, so the goal tools
    and ``ResumeState`` cannot drift apart. Adds only the public ``rubric``
    graph input, which is intentionally non-private — it is the
    ``RubricMiddleware`` input.
    """

    rubric: NotRequired[str | None]
    """Public ``RubricMiddleware`` graph input (intentionally non-private).

    Distinct from the TUI-owned ``_sticky_rubric``: this is the per-invocation
    rubric passed in via the graph schema, not checkpointed state.
    """


# ---------------------------------------------------------------------------
# Snapshot types
# ---------------------------------------------------------------------------


class RubricSnapshot(TypedDict):
    """Read-only rubric view returned by the ``get_rubric`` tool to the model.

    ``active`` is always ``criteria is not None``; the two never disagree.
    """

    active: bool
    """Whether acceptance criteria are currently available."""

    criteria: str | None
    """Current acceptance criteria, or ``None`` when no rubric is set."""

    grading_status: str | None
    """Latest ``RubricMiddleware`` grading status for the in-progress or
    just-completed graded turn, or ``None``."""


class GoalSnapshot(TypedDict):
    """Read-only goal view returned by the ``get_goal`` tool to the model.

    A fixed-shape projection of goal state. Both construction branches in
    ``_goal_snapshot`` must populate every key, so the type checker catches a
    drift between them.
    """

    active: bool
    """Whether the goal is actionable (should drive work).

    Derived from ``status``: ``active`` and ``blocked`` goals are actionable,
    while ``paused`` and ``complete`` goals are not.
    """

    objective: str | None
    """Active goal objective, or ``None`` when no goal is set."""

    status: GoalStatus | None
    """Lifecycle status, or ``None`` when no goal is set."""

    criteria: str | None
    """Persisted goal criteria, or shared rubric criteria when no goal rubric
    exists."""

    note: str | None
    """Persisted completion evidence or blocker note for the goal."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_state_text(state: dict[str, Any], key: str) -> str | None:
    """Return a non-empty stripped string from ``state[key]``, or ``None``.

    Used by snapshot builders to normalize empty strings and whitespace-only
    values to ``None`` so the model sees a clean signal.
    """
    value = state.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


# ---------------------------------------------------------------------------
# Snapshot builders
# ---------------------------------------------------------------------------


def _rubric_snapshot(state: dict[str, Any]) -> RubricSnapshot:
    """Build the ``get_rubric`` response from graph state.

    Criteria resolve in precedence order: public ``rubric`` input, else an
    actionable goal rubric, else a standalone sticky rubric.

    Args:
        state: Current graph state injected by LangGraph.

    Returns:
        Rubric snapshot visible to the model.
    """
    criteria = _clean_state_text(state, "rubric")
    goal_rubric = _clean_state_text(state, "_goal_rubric")
    sticky_rubric = _clean_state_text(state, "_sticky_rubric")
    objective = _clean_state_text(state, "_goal_objective")
    status = coerce_goal_status(state.get("_goal_status")) or "active"
    goal_is_actionable = objective is not None and status in {"active", "blocked"}
    sticky_is_goal_rubric = objective is not None and sticky_rubric == goal_rubric

    # Prefer the public `rubric` graph input when present; otherwise surface
    # actionable goal criteria or a standalone sticky rubric.
    if criteria is None:
        if goal_is_actionable and goal_rubric is not None:
            criteria = goal_rubric
        elif sticky_rubric is not None and not sticky_is_goal_rubric:
            criteria = sticky_rubric

    grading_status = _clean_state_text(state, "_rubric_status")
    return {
        "active": criteria is not None,
        "criteria": criteria,
        "grading_status": grading_status,
    }


def _goal_snapshot(state: dict[str, Any]) -> GoalSnapshot:
    """Build the ``get_goal`` response from graph state.

    Args:
        state: Current graph state injected by LangGraph.

    Returns:
        Goal snapshot visible to the model.
    """
    objective = _clean_state_text(state, "_goal_objective")
    rubric = _rubric_snapshot(state)
    if objective is None:
        return {
            "active": False,
            "objective": None,
            "status": None,
            "criteria": rubric["criteria"],
            "note": None,
        }
    status: GoalStatus = coerce_goal_status(state.get("_goal_status")) or "active"
    criteria = _clean_state_text(state, "_goal_rubric") or rubric["criteria"]
    note = _clean_state_text(state, "_goal_status_note")
    return {
        "active": status in {"active", "blocked"},
        "objective": objective,
        "status": status,
        "criteria": criteria,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Update command builder
# ---------------------------------------------------------------------------


def _update_goal_command(
    *,
    status: Literal["complete", "blocked"],
    note: str,
    tool_call_id: str,
    state: dict[str, Any],
) -> Command[Any]:
    """Build the constrained ``update_goal`` command.

    Args:
        status: Goal status the model is reporting (``complete`` or ``blocked``).
        note: Evidence the goal is complete, or the specific blocker.
        tool_call_id: Tool call ID for the returned ``ToolMessage``.
        state: Current graph state injected by LangGraph.

    Returns:
        Command updating goal metadata and returning a tool response.
    """
    objective = state.get("_goal_objective")
    if not isinstance(objective, str) or not objective:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="No active goal is set.",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    goal_status = coerce_goal_status(state.get("_goal_status")) or "active"
    if goal_status in {"paused", "complete"}:
        if goal_status == "paused":
            message = "The goal is paused. The user must run `/goal resume` before its status can be updated."
        else:
            message = "The goal is already complete and cannot be updated."
        return Command(update={"messages": [ToolMessage(content=message, tool_call_id=tool_call_id)]})
    clean_note = note.strip()
    if not clean_note:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(f"Provide a note with evidence before marking the goal {status}."),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    try:
        validate_goal_status_note(clean_note)
    except GoalStateSizeError as exc:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=str(exc),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    if status == "complete":
        # Stage completion evidence; resolved after rubric grader's verdict.
        return Command(
            update={
                "_pending_goal_completion_note": clean_note,
                "messages": [
                    ToolMessage(
                        content=("Goal completion requested. It will be recorded if the accepted rubric is satisfied."),
                        tool_call_id=tool_call_id,
                    )
                ],
            }
        )
    # Blocked — commit immediately and clear any pending completion staging.
    update: dict[str, Any] = {
        "_goal_status": status,
        "_goal_status_note": clean_note,
        "_pending_goal_completion_note": None,
    }
    return Command(
        update={
            **update,
            "messages": [
                ToolMessage(
                    content=f"Goal marked {status}. {clean_note}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


# ---------------------------------------------------------------------------
# Tool definitions (registered by GoalToolsMiddleware.__init__)
# ---------------------------------------------------------------------------


@tool
def get_rubric(
    state: Annotated[dict[str, Any], InjectedState],
) -> RubricSnapshot:
    """Read criteria when the latest state notice says a rubric is active.

    Use this only when the latest goal/rubric state notice reports an active
    rubric. Use ``get_goal`` when a goal is actionable; this tool only reports
    whether criteria are active, the current criteria, and the latest grading
    status.

    Returns:
        Rubric snapshot with ``active``, ``criteria``, and ``grading_status``
        keys.
    """
    return _rubric_snapshot(state)


@tool
def get_goal(
    state: Annotated[dict[str, Any], InjectedState],
) -> GoalSnapshot:
    """Read a goal when the latest state notice says it is actionable.

    Use this only when the latest goal/rubric state notice reports an
    actionable goal. It returns the objective, criteria, lifecycle status,
    and any prior note from authoritative checkpoint state.

    Returns:
        Goal snapshot with ``active``, ``objective``, ``status``, ``criteria``,
        and ``note`` keys.
    """
    return _goal_snapshot(state)


@tool
def update_goal(
    status: Annotated[
        Literal["complete", "blocked"],
        Field(
            description=("`complete` to attach completion evidence, or `blocked` when you are stuck and need user input.")
        ),
    ],
    note: Annotated[
        str,
        Field(
            description=(
                f"Evidence the criteria are satisfied, or the specific blocker. "
                f"Required when calling this tool (max {GOAL_STATUS_NOTE_CHAR_LIMIT} characters)."
            )
        ),
    ],
    tool_call_id: Annotated[str, InjectedToolCallId],
    state: Annotated[dict[str, Any], InjectedState],
) -> Command[Any]:
    """Update a goal only when the latest state notice says it is actionable.

    Use ``blocked`` when you cannot proceed without user input. Goals complete
    automatically after a satisfied goal-backed grading turn, so ``complete``
    is optional and stages its evidence for that evaluation.

    Returns:
        Command that updates goal status and returns a tool message.
    """
    return _update_goal_command(
        status=status,
        note=note,
        tool_call_id=tool_call_id,
        state=state,
    )


_active_criteria_agent: Any | None = None
_active_fallback_agent: Any | None = None


def set_active_criteria_agent(agent: Any | None, fallback: Any | None = None) -> None:
    """Register the active criteria agent and fallback agent for criteria generation."""
    global _active_criteria_agent, _active_fallback_agent
    _active_criteria_agent = agent
    _active_fallback_agent = fallback


def get_active_criteria_agent() -> tuple[Any | None, Any | None]:
    """Get the currently registered criteria agent and fallback agent."""
    return _active_criteria_agent, _active_fallback_agent


__all__ = [
    "GOAL_TOOL_NAMES",
    "GoalSnapshot",
    "GoalToolState",
    "RubricSnapshot",
    "get_active_criteria_agent",
    "get_goal",
    "get_rubric",
    "set_active_criteria_agent",
    "update_goal",
]
