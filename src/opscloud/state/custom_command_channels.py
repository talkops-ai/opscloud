"""LangGraph state channels for custom command execution.

Tracks active command permissions (`command_permissions`), turn-scoped
`disallowed-tools`, session-scoped invoked skills/commands, dynamically
activated path-conditional commands, and forked subagent turn recursion guards.
"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired
from typing_extensions import TypedDict

from langchain.agents.middleware.types import PrivateStateAttr


def _merge_invoked_commands(
    existing: dict[str, dict[str, Any]] | None,
    new: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Reducer that merges invoked custom command records across turns."""
    if not existing:
        return dict(new or {})
    if not new:
        return dict(existing)
    return {**existing, **new}


class CustomCommandChannels(TypedDict):
    """TypedDict mixin for custom command state in `OpsCloudAgentState`."""

    custom_command_request: NotRequired[dict[str, Any] | None]
    """Optional turn-level custom command execution request payload."""

    _active_command_permissions: Annotated[
        NotRequired[dict[str, Any] | None],
        PrivateStateAttr,
    ]
    """Active command permission layer (`allowed_tools`, turn-scoped `disallowed_tools`, `model`, `effort`)."""

    _invoked_custom_commands: Annotated[
        NotRequired[dict[str, dict[str, Any]]],
        PrivateStateAttr,
        _merge_invoked_commands,
    ]
    """Session-scoped registry of invoked custom commands/skills."""

    _dynamic_conditional_commands: Annotated[
        NotRequired[list[str]],
        PrivateStateAttr,
    ]
    """List of conditional `paths:` command names dynamically activated by file access."""

    _forked_command_turn: Annotated[
        NotRequired[bool],
        PrivateStateAttr,
    ]
    """True when running inside a `context: fork` subagent turn to block recursive spawning."""
