"""Goal tools exposed to the agent for persisted goals and notice management."""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NotRequired,
    cast,
    override,
)

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    TracePolicy,
    omit_payload,
)
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import Field

from opscloud.middleware.goal_state_notice import (
    build_goal_state_notice,
    goal_notice_size_error,
    goal_state_fingerprint,
    has_goal_or_rubric_state,
    is_oversized_goal_state_message,
    latest_goal_state_message_index,
    latest_goal_state_notice,
    latest_human_is_unsaved_goal_continuation,
    log_malformed_summarization_event,
    superseded_goal_state_placeholder,
    validated_summarization_cutoff,
)
from opscloud.middleware.registry import register_middleware
from opscloud.state.goal_channels import (
    GoalRubricChannels,
    coerce_goal_status,
)
from opscloud.state.goal_state_limits import (
    GOAL_STATUS_NOTE_CHAR_LIMIT,
    GoalStateSizeError,
    validate_goal_status_note,
)
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from langgraph.runtime import Runtime

logger = get_logger(__name__)

GOAL_TOOL_NAMES = frozenset({"update_goal"})
"""Tool names used by behavioral absence gates and middleware contract tests."""


def _goal_state_notice_for(
    state: dict[str, Any],
    messages: Sequence[object],
    *,
    cutoff: int = 0,
) -> HumanMessage | None:
    """Build a notice when effective history lacks current goal/rubric state.

    Args:
        state: Authoritative middleware state.
        messages: Messages visible at the next model boundary.
        cutoff: Summarization cutoff index that `messages` is measured against.

    Returns:
        Current notice to append, or `None` when history is already authoritative.
    """
    if latest_human_is_unsaved_goal_continuation(messages):
        return None
    latest = latest_goal_state_notice(messages)
    latest_candidate = latest_goal_state_message_index(messages)
    fingerprint = goal_state_fingerprint(state)
    if (
        latest is not None
        and latest[0] == latest_candidate
        and latest[1]["state_fingerprint"] == fingerprint
        and latest[0] >= cutoff
    ):
        return None
    if latest_candidate is None and not has_goal_or_rubric_state(state):
        return None
    return build_goal_state_notice(state)


class GoalToolState(GoalRubricChannels):
    """State fields used by goal tools.

    Inherits the shared `_goal_*`/`_sticky_rubric` channels from `GoalRubricChannels`.
    Adds only the public `rubric` graph input.
    """

    rubric: NotRequired[str | None]
    """Public `RubricMiddleware` graph input (intentionally non-private)."""


def _update_goal_command(
    *,
    status: Literal["complete", "blocked"],
    note: str,
    tool_call_id: str,
    state: dict[str, Any],
) -> Command[Any]:
    """Build the constrained `update_goal` command."""
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

    exc = goal_notice_size_error(state)
    if exc is not None:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            f"Saved goal/rubric state is too large to use, so its "
                            f"status cannot be updated. Ask the user to clear and "
                            f"recreate the goal. Validation detail: {exc}"
                        ),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    goal_status = coerce_goal_status(state.get("_goal_status")) or "active"
    if goal_status in {"paused", "complete"}:
        if goal_status == "paused":
            message = (
                "The goal is paused. The user must run `/goal resume` before its "
                "status can be updated."
            )
        else:
            message = "The goal is already complete and cannot be updated."
        return Command(
            update={
                "messages": [ToolMessage(content=message, tool_call_id=tool_call_id)]
            }
        )

    clean_note = note.strip()
    if not clean_note:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            f"Provide a note with evidence before marking the "
                            f"goal {status}."
                        ),
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
                "messages": [ToolMessage(content=str(exc), tool_call_id=tool_call_id)]
            }
        )

    if status == "complete":
        return Command(
            update={
                "_pending_goal_completion_note": clean_note,
                "messages": [
                    ToolMessage(
                        content=(
                            "Goal completion requested. It will be recorded if "
                            "the accepted rubric is satisfied."
                        ),
                        tool_call_id=tool_call_id,
                    )
                ],
            }
        )

    update = {
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


@register_middleware(name="goal_tools")
class GoalToolsMiddleware(AgentMiddleware[GoalToolState, ContextT]):
    """Expose the constrained `update_goal` tool and maintain the goal-state notice.

    The model reads goal awareness from the injected goal-state notice rather
    than a read tool: `before_model` persists a fresh notice into checkpointed
    history when the latest one no longer matches authoritative state (or has
    scrolled below the summarization cutoff), and `wrap_model_call` re-pins the
    notice into the request when the persisted one is out of view.
    """

    trace_policy = TracePolicy(process_inputs=omit_payload)
    state_schema = GoalToolState

    def __init__(self) -> None:
        """Initialize goal tools."""
        super().__init__()

        @tool
        def update_goal(
            status: Annotated[
                Literal["complete", "blocked"],
                Field(
                    description=(
                        "`complete` to attach completion evidence, or `blocked` "
                        "when you are stuck and need the user."
                    )
                ),
            ],
            note: Annotated[
                str,
                Field(
                    max_length=GOAL_STATUS_NOTE_CHAR_LIMIT,
                    description=(
                        "Evidence the criteria are satisfied, or the specific "
                        "blocker. Required when calling this tool."
                    ),
                ),
            ],
            tool_call_id: Annotated[str, InjectedToolCallId],
            state: Annotated[dict[str, Any], InjectedState],
        ) -> Command[Any]:
            """Update a goal only when the latest state notice says it is actionable.

            Read the current objective and any acceptance criteria from the latest
            goal/rubric state notice in context, or — right after a goal whose save
            failed — from the objective and criteria quoted in the accompanying
            goal continuation message. There is no read tool for them. Use
            `blocked` when you cannot proceed without user input. Goals complete
            automatically after a satisfied goal-backed grading turn, so
            `complete` is optional and only stages its evidence for that result.
            Do not create, pause, resume, clear, or replace goals — those are
            user-controlled.

            Returns:
                Command that updates goal status and returns a tool message.
            """
            return _update_goal_command(
                status=status,
                note=note,
                tool_call_id=tool_call_id,
                state=state,
            )

        self.tools = [update_goal]

    @staticmethod
    def _notice_update(state: AgentState[Any]) -> dict[str, Any] | None:
        """Compute the checkpointed notice update for a `before_model` boundary."""
        values = cast("dict[str, Any]", state)
        raw_messages = values.get("messages", [])
        messages = list(raw_messages) if isinstance(raw_messages, list) else []
        event = values.get("_summarization_event")
        cutoff = validated_summarization_cutoff(
            event,
            message_count=len(messages),
        )
        malformed_event = event is not None and cutoff is None
        notice = _goal_state_notice_for(
            values,
            messages,
            cutoff=len(messages) if malformed_event else (cutoff or 0),
        )
        update: dict[str, Any] = {}
        if malformed_event:
            log_malformed_summarization_event(event, len(messages))
            update["_summarization_event"] = None
        if notice is not None:
            update["messages"] = [notice]
        exc = goal_notice_size_error(values)
        if exc is not None and values.get("rubric") is not None:
            logger.warning(
                "Goal/rubric state exceeds the notice budget; clearing the "
                "per-invocation rubric so this turn is not graded: %s",
                exc,
            )
            update["rubric"] = None
        return update or None

    @override
    def before_model(
        self,
        state: AgentState[Any],
        runtime: Runtime[ContextT] | None = None,
    ) -> dict[str, Any] | None:
        """Persist a current goal-state notice into checkpointed history."""
        del runtime
        return self._notice_update(state)

    @override
    async def abefore_model(
        self,
        state: AgentState[Any],
        runtime: Runtime[ContextT] | None = None,
    ) -> dict[str, Any] | None:
        """Persist a current goal-state notice at an async model boundary."""
        del runtime
        return self._notice_update(state)

    @staticmethod
    def _request_with_goal_notice(
        request: ModelRequest[ContextT],
    ) -> ModelRequest[ContextT]:
        """Ensure a current goal-state notice is visible in a model request."""
        values = cast("dict[str, Any]", request.state)
        event = values.get("_summarization_event")
        cutoff = validated_summarization_cutoff(
            event,
            message_count=len(request.messages),
        )
        malformed_event = event is not None and cutoff is None
        if malformed_event:
            log_malformed_summarization_event(event, len(request.messages))
            values = {**values, "_summarization_event": None}
            request = request.override(state=cast("AgentState[Any]", values))
        notice = _goal_state_notice_for(
            values,
            request.messages,
            cutoff=len(request.messages) if malformed_event else (cutoff or 0),
        )
        latest = latest_goal_state_notice(request.messages)
        preserved_index = latest[0] if notice is None and latest is not None else None
        messages = [
            (
                superseded_goal_state_placeholder(message)
                if index != preserved_index and is_oversized_goal_state_message(message)
                else message
            )
            for index, message in enumerate(request.messages)
        ]
        if notice is not None:
            messages.append(notice)
        if notice is None and all(
            projected is original
            for projected, original in zip(messages, request.messages, strict=True)
        ):
            return request
        return request.override(messages=messages)

    @override
    def wrap_model_call[ResponseT](
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Re-pin the goal-state notice into each model request when needed."""
        return handler(self._request_with_goal_notice(request))

    @override
    async def awrap_model_call[ResponseT](
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        """Re-pin the goal-state notice into each async model request when needed."""
        return await handler(self._request_with_goal_notice(request))


__all__ = [
    "GOAL_TOOL_NAMES",
    "GoalToolState",
    "GoalToolsMiddleware",
    "_goal_state_notice_for",
    "_update_goal_command",
]
