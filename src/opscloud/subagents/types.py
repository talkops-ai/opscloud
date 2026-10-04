"""Type definitions for subagent metadata."""

from typing import Any, NotRequired, TypedDict


class SubagentMetadata(TypedDict):
    """Metadata for a custom subagent loaded from filesystem."""

    name: str
    description: str
    system_prompt: str
    model: NotRequired[str | None]
    skills: NotRequired[list[str] | None]
    tools: NotRequired[list[str] | None]
    capabilities: NotRequired[list[dict[str, Any]] | None]
    mcp_config: NotRequired[dict[str, Any] | None]
    mcp_files: NotRequired[list[str] | None]
    middleware: NotRequired[list[Any] | None]
    permission_tier: NotRequired[str | None]
    is_plugin: NotRequired[bool]
    source: str
    path: str
