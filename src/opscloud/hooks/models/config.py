"""Validated hook configuration models for declarative Hooks v2 specifications."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opscloud.hooks.models.domain import HookEvent


class _ConfigModel(BaseModel):
    """Base model for hook configuration objects."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class CommandHandlerSpec(_ConfigModel):
    """Configuration for a command hook handler.

    Currently supports `type: "command"`. Commands can run shell expressions
    or direct executable arguments via `argv`.
    """

    type: Literal["command"]
    command: str
    argv: list[str] | None = None
    timeout: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    status_message: str | None = Field(default=None, alias="statusMessage")
    async_: bool | None = Field(default=None, alias="async")

    @field_validator("argv", mode="after")
    @classmethod
    def _normalize_argv(cls, value: list[str] | None) -> list[str] | None:
        """Validate that argv is a non-empty list of strings if provided.

        Args:
            value: Provided argv list or None.

        Returns:
            Validated list of string arguments.

        Raises:
            ValueError: If argv is empty, contains non-strings, or has an empty executable.
        """
        if value is None:
            return None
        if not value or not all(isinstance(part, str) for part in value):
            msg = "argv must be a non-empty list of strings when provided."
            raise ValueError(msg)
        if not value[0].strip():
            msg = "argv[0] must be a non-empty executable path."
            raise ValueError(msg)
        return value


HandlerSpec = CommandHandlerSpec


class MatcherGroup(_ConfigModel):
    """A matcher pattern and its ordered hook handlers.

    Attributes:
        matcher: Tool name pattern (e.g. 'Bash', 'aws_*', 'Write') or regex.
        hooks: List of handler specifications to execute on match.
    """

    matcher: str | None = None
    hooks: list[HandlerSpec]


class HooksConfig(_ConfigModel):
    """Top-level configuration grouped by hook lifecycle event.

    Attributes:
        hooks: Mapping from lifecycle HookEvent to list of MatcherGroups.
    """

    hooks: dict[HookEvent, list[MatcherGroup]]
