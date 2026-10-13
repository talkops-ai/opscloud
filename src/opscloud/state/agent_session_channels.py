"""Canonical agent session state channels for OpsCloud.

``AgentSessionChannels`` declares the checkpointed, schema-private channels used by
``AgentSessionMiddleware`` to run a plugin subagent as an *activated* session agent.
The subagent's own conversation lives in ``_agent_session_messages`` (never in
``messages``), so the main deep agent never sees internal subagent tool calls or outputs.
"""

from __future__ import annotations

from typing import (
    Annotated,
    Any,
    Literal,
    NotRequired,
)

from langchain.agents.middleware.types import (
    AgentState,
    OmitFromOutput,
    PrivateStateAttr,
)
from langchain_core.messages import AnyMessage

from opscloud.state.workflow_channels import _replace_or_reset_messages

AgentSessionRequestKind = Literal["start", "continue", "clear"]
"""Typed operations the TUI can submit through ``agent_session_request``."""

AGENT_SESSION_PRIVATE_KEYS: tuple[str, ...] = (
    "_active_agent_id",
    "_active_agent_plugin_id",
    "_active_agent_description",
    "_agent_session_turn",
)


class AgentSessionChannels(AgentState):
    """Canonical agent session channels shared by every schema touching them."""

    agent_session_request: NotRequired[Annotated[dict[str, Any] | None, OmitFromOutput]]
    """Public graph input carrying one typed agent session operation (start, continue, clear)."""

    _agent_session_turn: Annotated[NotRequired[bool], PrivateStateAttr]
    """True while the current invocation is an agent session turn.

    Main-graph ``after_agent`` hooks (rubric grading, Stop hooks) must no-op
    on agent session turns because the main transcript did not change.
    """

    _active_agent_id: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Fully-qualified active plugin agent name, e.g. ``aws-compute@talkops-devops-plugins``."""

    _active_agent_plugin_id: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Plugin ID that provided the active agent."""

    _active_agent_description: Annotated[NotRequired[str | None], PrivateStateAttr]
    """Description of the active agent persona."""

    _agent_session_messages: Annotated[
        NotRequired[list[AnyMessage]],
        PrivateStateAttr,
        _replace_or_reset_messages,
    ]
    """Isolated agent session transcript. Never merged into ``messages``."""


def is_agent_session_turn(state: object) -> bool:
    """Return whether graph ``state`` belongs to an isolated agent session turn."""
    getter = getattr(state, "get", None)
    if not callable(getter):
        return False
    return bool(getter("_agent_session_turn")) or bool(getter("agent_session_request"))


__all__ = [
    "AGENT_SESSION_PRIVATE_KEYS",
    "AgentSessionChannels",
    "AgentSessionRequestKind",
    "is_agent_session_turn",
]
