"""Canonical workflow state channels for OpsCloud.

``WorkflowChannels`` declares the checkpointed, schema-private channels used by
``WorkflowMiddleware`` to run a skill as an *isolated* workflow.  The workflow's
own conversation lives in ``_workflow_messages`` (never in ``messages``), so the
main deep agent never sees workflow instructions, tool calls, or outputs.
"""

from __future__ import annotations

from typing import (
    Annotated,
    Any,
    Literal,
    NotRequired,
    Sequence,
    cast,
    get_args,
)

from langchain.agents.middleware.types import (
    AgentState,
    OmitFromOutput,
    PrivateStateAttr,
)
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

WorkflowStatus = Literal[
    "running",
    "blocked",
    "complete",
    "cancelled",
    "failed",
]
"""Lifecycle status of a workflow run.

``running`` means the agent is executing or waiting for the operator's reply
in prose; ``blocked`` means it explicitly reported it cannot continue.
"""

WorkflowRequestKind = Literal["start", "continue", "cancel"]
"""Typed operations the TUI can submit through ``workflow_request``."""

_WORKFLOW_STATUS_VALUES: frozenset[str] = frozenset(get_args(WorkflowStatus))
ACTIVE_WORKFLOW_STATUSES: frozenset[str] = frozenset({"running", "blocked"})
"""Statuses for which a run is still open (one per thread) and accepts replies."""

TERMINAL_WORKFLOW_STATUSES: frozenset[str] = frozenset({"complete", "cancelled", "failed"})

WORKFLOW_PRIVATE_KEYS: tuple[str, ...] = (
    "_workflow_run_id",
    "_workflow_name",
    "_workflow_skill_path",
    "_workflow_args",
    "_workflow_status",
    "_workflow_status_note",
    "_workflow_artifacts",
)
"""Scalar workflow channels (excludes the message reducer channel)."""


def _replace_or_reset_messages(
    existing: Sequence[AnyMessage] | None,
    new: Sequence[AnyMessage] | dict[str, Any] | None,
) -> list[AnyMessage]:
    """Reducer for ``_workflow_messages``.

    Behaves like ``add_messages`` but accepts ``None`` / ``{"reset": True}`` to
    wipe the isolated transcript, and ``{"reset": True, "messages": [...]}`` to
    replace it atomically (used when a fresh run starts).
    """
    if new is None:
        return []
    if isinstance(new, dict):
        if new.get("reset"):
            replacement = new.get("messages") or []
            return cast("list[AnyMessage]", list(add_messages(cast(Any, []), cast(Any, list(replacement))))) if replacement else []
        return list(existing or [])
    return cast("list[AnyMessage]", list(add_messages(cast(Any, existing or []), cast(Any, new))))


class WorkflowChannels(AgentState):
    """Canonical workflow channels shared by every schema touching them."""

    workflow_request: NotRequired[Annotated[dict[str, Any] | None, OmitFromOutput]]
    """Public graph input carrying one typed workflow operation."""

    _workflow_turn: Annotated[NotRequired[bool], PrivateStateAttr]
    """True while the current invocation is a workflow turn.

    Main-graph ``after_agent`` hooks (rubric grading, Stop hooks) must no-op
    on workflow turns because the main transcript did not change.
    """

    _workflow_run_id: Annotated[NotRequired[str | None], PrivateStateAttr]
    _workflow_name: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Fully qualified skill name, e.g. ``equity-research@cffs:earnings-preview``."""

    _workflow_skill_path: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Absolute path to the workflow's ``SKILL.md``."""

    _workflow_args: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Operator parameters the run was triggered with."""

    _workflow_status: Annotated[NotRequired[WorkflowStatus | None], PrivateStateAttr]
    _workflow_status_note: Annotated[NotRequired[str | None], PrivateStateAttr]
    _workflow_artifacts: Annotated[NotRequired[list[str] | None], PrivateStateAttr]

    _workflow_messages: Annotated[
        NotRequired[list[AnyMessage]],
        PrivateStateAttr,
        _replace_or_reset_messages,
    ]
    """Isolated workflow-agent transcript. Never merged into ``messages``."""


def coerce_workflow_status(value: object) -> WorkflowStatus | None:
    """Narrow a persisted workflow status to a known value."""
    if isinstance(value, str) and value in _WORKFLOW_STATUS_VALUES:
        return cast("WorkflowStatus", value)
    return None


def is_workflow_turn(state: object) -> bool:
    """Return whether graph ``state`` belongs to an isolated workflow turn."""
    getter = getattr(state, "get", None)
    if not callable(getter):
        return False
    return bool(getter("_workflow_turn")) or bool(getter("workflow_request"))


__all__ = [
    "ACTIVE_WORKFLOW_STATUSES",
    "TERMINAL_WORKFLOW_STATUSES",
    "WORKFLOW_PRIVATE_KEYS",
    "WorkflowChannels",
    "WorkflowRequestKind",
    "WorkflowStatus",
    "coerce_workflow_status",
    "is_workflow_turn",
]
