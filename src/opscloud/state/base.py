"""Core Agent State schemas for OpsCloud.

Combines LangGraph's DeepAgentState (DeltaChannel messages) with typed operational
channels: multi-cloud environment tracking, goal & rubric evaluation, cost
metrics, auto-mode execution boundaries, and lifecycle hooks.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired

from deepagents.graph import DeepAgentState
from langchain.agents.middleware.types import (
    OmitFromInput,
    PrivateStateAttr,
)

from opscloud.state.cloud_context import CloudContextState
from opscloud.state.goal_channels import GoalRubricChannels
from opscloud.state.resume_state import ResumeState
from opscloud.state.service_context import ServiceContextState


def _merge_dict(
    existing: dict[str, Any] | None,
    new: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reducer that merges dictionary mappings."""
    if not existing:
        return dict(new or {})
    if not new:
        return dict(existing)
    return {**existing, **new}


class OpsCloudAgentState(
    DeepAgentState,
    ResumeState,
    GoalRubricChannels,
    CloudContextState,
    ServiceContextState,
):
    """Unified composite agent state schema for OpsCloud LangGraph execution.

    Declared with DeltaChannel for messages to keep checkpoint growth O(N) rather
    than O(N²), while providing first-class channels for cloud operations,
    goal management, cost tracking, and execution boundaries.
    """

    # ── Cumulative Session Cost Tracking ──────────────────
    _session_cost_usd: Annotated[
        NotRequired[float],
        PrivateStateAttr,
        operator.add,
    ]
    """Cumulative estimated USD cost across all priceable turns in this thread."""

    _session_cost_breakdown: Annotated[
        NotRequired[dict[str, Any]],
        PrivateStateAttr,
        _merge_dict,
    ]
    """Model-by-model spend breakdown for this thread."""

    _session_cost_transfers: Annotated[
        NotRequired[dict[str, Any]],
        OmitFromInput,
        operator.or_,
    ]
    """Subagent spend handoffs awaiting parent graph aggregation."""

    _session_total_tokens: Annotated[
        NotRequired[int],
        PrivateStateAttr,
        operator.add,
    ]
    """Cumulative total tokens across all model calls in this thread."""

    # ── Local Workspace & Git Context ─────────────────────
    _local_context: Annotated[
        NotRequired[str | None],
        PrivateStateAttr,
    ]
    """Cached local git/workspace status snapshot."""

    _local_context_refreshed_at_cutoff: Annotated[
        NotRequired[int | None],
        PrivateStateAttr,
    ]
    """Message cutoff index of the last summarization refresh."""

    _latest_local_context_fingerprint: Annotated[
        NotRequired[str | None],
        PrivateStateAttr,
    ]
    """Content fingerprint used to deduplicate local context updates."""

    # ── Auto-Mode & HITL Plan Execution ───────────────────
    _auto_decision_plan: Annotated[
        NotRequired[Any],
        PrivateStateAttr,
    ]
    """Active auto-mode decision plan and tool execution permissions."""

    _auto_temp_artifacts: Annotated[
        NotRequired[dict[str, Any]],
        PrivateStateAttr,
        _merge_dict,
    ]
    """Temporary scratch artifacts generated during auto-mode tasks."""

    _auto_classifier_conversation: Annotated[
        NotRequired[Any],
        PrivateStateAttr,
    ]
    """Review conversation history for the auto-mode safety classifier."""

    # ── Server Hooks & Lifecycle Tracking ─────────────────
    _hooks_stop_continuation_count: Annotated[
        NotRequired[int],
        PrivateStateAttr,
    ]
    """Stop-hook continuation count for the active turn."""

    _hooks_pre_tool_outcomes: Annotated[
        NotRequired[dict[str, Any]],
        PrivateStateAttr,
    ]
    """Pre-execution hook verdicts keyed by tool call ID."""

    # ── Jev Dynamic Model & Reasoning Routing ─────────────
    _dynamic_model_route: Annotated[
        NotRequired[dict[str, Any]],
        PrivateStateAttr,
    ]
    """Active Jev dynamic model routing decision for the current turn."""

    model_route: Annotated[
        NotRequired[Any],
        PrivateStateAttr,
    ]
    """Active LangChain ModelRouterMiddleware choice answer for the current turn."""


# Canonical aliases
AgentState = OpsCloudAgentState
BaseAgentState = OpsCloudAgentState

__all__ = [
    "AgentState",
    "BaseAgentState",
    "OpsCloudAgentState",
]
