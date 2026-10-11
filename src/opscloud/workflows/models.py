"""Data models for isolated skill workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired

from typing_extensions import TypedDict


@dataclass(frozen=True)
class WorkflowSpec:
    """A skill-defined procedure that can be triggered as an isolated workflow."""

    name: str
    """Fully qualified skill name (``plugin@market:skill`` for plugin skills)."""

    short_name: str
    """Terminal skill name used in ``/workflow:<short_name>``."""

    description: str
    path: str
    """Absolute path to ``SKILL.md``."""

    source: str
    plugin_id: str | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for graph state."""
        return {
            "name": self.name,
            "short_name": self.short_name,
            "description": self.description,
            "path": self.path,
            "source": self.source,
            "plugin_id": self.plugin_id,
        }


class WorkflowRequest(TypedDict):
    """One typed operation submitted through the ``workflow_request`` channel."""

    kind: Literal["start", "continue", "cancel"]
    request_id: str
    workflow: NotRequired[dict[str, Any]]
    """Serialized ``WorkflowSpec`` (required for ``start``)."""

    args: NotRequired[str]
    """Operator parameters for ``start`` (free text, ``key=value``, flags)."""

    message: NotRequired[str]
    """Operator reply routed to the workflow agent (required for ``continue``)."""

    replace: NotRequired[bool]
    """``start`` only: discard an open run instead of rejecting the request."""


__all__ = [
    "WorkflowRequest",
    "WorkflowSpec",
]
