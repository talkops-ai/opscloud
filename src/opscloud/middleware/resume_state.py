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


def _extract_runtime_thread_and_model(
    state: ResumeState,
    runtime: Runtime[ContextT],
) -> tuple[str | None, str, str | None]:
    """Return ``(thread_id, checkpoint_ns, model_spec)`` from LangGraph runtime and state."""
    execution_info = getattr(runtime, "execution_info", None)
    thread_id = getattr(execution_info, "thread_id", None)
    checkpoint_ns = getattr(execution_info, "checkpoint_ns", None) or ""

    ctx = getattr(runtime, "context", None)
    model_spec: str | None = None
    if isinstance(ctx, dict):
        if not thread_id:
            tid = ctx.get("thread_id")
            if isinstance(tid, str) and tid:
                thread_id = tid
        raw_model = ctx.get("model")
        if isinstance(raw_model, str) and raw_model:
            model_spec = raw_model

    cfg = getattr(runtime, "config", None)
    if isinstance(cfg, dict):
        configurable = cfg.get("configurable")
        if isinstance(configurable, dict):
            if not thread_id:
                tid = configurable.get("thread_id")
                if isinstance(tid, str) and tid:
                    thread_id = tid
            if not checkpoint_ns:
                c_ns = configurable.get("checkpoint_ns")
                if isinstance(c_ns, str):
                    checkpoint_ns = c_ns

    if not model_spec:
        state_spec = state.get("_model_spec")
        if isinstance(state_spec, str) and state_spec:
            model_spec = state_spec

    return (
        thread_id if isinstance(thread_id, str) and thread_id else None,
        checkpoint_ns,
        model_spec,
    )


@register_middleware(name="resume_state")
class ResumeStateMiddleware(AgentMiddleware[ResumeState, ContextT]):
    """Persists per-checkpoint resume facts and schedules first-turn thread title generation."""

    trace_policy = TracePolicy(process_inputs=omit_payload)
    state_schema = ResumeState

    async def abefore_agent(
        self,
        state: ResumeState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Schedule non-blocking conversation title generation on the first turn of a main thread.

        Mirrors ``(*CascadeManager).maybeGenerateConversationTitle`` (0x101d812a0):
        - Only runs on the first turn (no prior ``AIMessage`` in state).
        - Only runs on root conversation trajectories (``checkpoint_ns == ""``).
        - Skips non-conversational background requests (e.g. ``goal_criteria_request``).
        """
        if state.get("goal_criteria_request"):
            return None

        messages = list(state.get("messages") or [])
        if not messages or any(isinstance(m, AIMessage) for m in messages):
            return None

        thread_id, checkpoint_ns, model_spec = _extract_runtime_thread_and_model(state, runtime)
        if not thread_id or checkpoint_ns:
            return None

        from opscloud.state.conversation_title import schedule_conversation_title_generation
        from opscloud.state.session import _initial_prompt_from_messages

        initial_prompt = _initial_prompt_from_messages(messages)
        if initial_prompt:
            schedule_conversation_title_generation(
                thread_id,
                initial_prompt,
                model_spec=model_spec,
            )
        return None

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
