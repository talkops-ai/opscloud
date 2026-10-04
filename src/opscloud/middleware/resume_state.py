"""Middleware for persisting per-checkpoint resume facts (tokens, model specs)."""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
)

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    PrivateStateAttr,
    TracePolicy,
    omit_payload,
)
from langchain_core.messages import AIMessage

from opscloud.middleware.registry import register_middleware
from opscloud.state.goal_channels import (
    INHERIT_RUBRIC_MODEL,
    GoalProposalKind,
    GoalRubricChannels,
    GoalStatus,
    coerce_goal_proposal_kind,
    coerce_goal_status,
    coerce_model_spec,
)
from opscloud.state.resume_state import ResumeState

if TYPE_CHECKING:
    from langgraph.runtime import Runtime


def _extract_context_tokens(message: AIMessage) -> int | None:
    """Return total context tokens from an AIMessage usage_metadata."""
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return None
    input_toks = usage.get("input_tokens", 0) or 0
    output_toks = usage.get("output_tokens", 0) or 0
    if input_toks or output_toks:
        return input_toks + output_toks
    total = usage.get("total_tokens", 0) or 0
    return total or None


@register_middleware(name="resume_state")
class ResumeStateMiddleware(AgentMiddleware[ResumeState, ContextT]):
    """Persists per-checkpoint resume facts after each model call."""

    trace_policy = TracePolicy(process_inputs=omit_payload)
    state_schema = ResumeState

    def after_model(
        self,
        state: ResumeState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Record context tokens from the latest AIMessage usage metadata."""
        update: dict[str, Any] = {}

        for msg in reversed(state.get("messages") or []):
            if isinstance(msg, AIMessage):
                tokens = _extract_context_tokens(msg)
                if tokens is not None:
                    update["_context_tokens"] = tokens
                break

        return update or None

    async def aafter_model(
        self,
        state: ResumeState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Async variant of after_model."""
        return self.after_model(state, runtime)


__all__ = [
    "INHERIT_RUBRIC_MODEL",
    "GoalProposalKind",
    "GoalRubricChannels",
    "GoalStatus",
    "ResumeState",
    "ResumeStateMiddleware",
    "coerce_goal_proposal_kind",
    "coerce_goal_status",
    "coerce_model_spec",
]
