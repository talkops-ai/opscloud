"""Base command handler — the contract every command implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opscloud.commands._types import BypassTier, CommandCategory, NotifySeverity, SafetyLevel

if TYPE_CHECKING:
    from textual.app import App

    from opscloud.config.settings import Settings
    from opscloud.state.session import SessionManager


@dataclass(frozen=True)
class CommandContext:
    """Immutable context injected into every command handler.

    Handlers MUST NOT reach into app internals directly.
    Everything required by a command handler is passed in this context bag.
    """

    app: Any  # TUI App / runtime reference for UI callbacks and screens
    session: Any = None  # Thread & checkpoint session manager reference
    agent: Any = None  # LangGraph compiled agent graph reference
    settings: Any = None  # Global application settings reference
    raw_command: str = ""  # Full raw command text e.g. "/effort high"
    args: str = ""  # Command arguments portion e.g. "high"
    thread_id: str | None = None  # Active thread ID
    model_spec: str | None = None  # Active model specification "provider:model"


@dataclass(init=False)
class CommandResult:
    """Return value from handler execution detailing result and TUI hints."""

    success: bool
    message: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    # TUI action hints evaluated by CommandRouter / app
    mount_as_app_message: bool = True
    push_screen: str | None = None
    notify: str | None = None
    notify_severity: NotifySeverity = "information"  # "information", "warning", "error"

    def __init__(
        self,
        success: bool = True,
        message: str | None = None,
        data: dict[str, Any] | None = None,
        mount_as_app_message: bool = True,
        push_screen: str | None = None,
        notify: str | None = None,
        notify_severity: NotifySeverity = "information",
    ) -> None:
        self.success = success
        self.message = message
        self.data = data if data is not None else {}
        self.mount_as_app_message = mount_as_app_message
        self.push_screen = push_screen
        self.notify = notify
        self.notify_severity = notify_severity


class BaseCommandHandler(ABC):
    """Every slash command handler implements this abstract base class interface."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical command name including slash prefix, e.g. '/effort'."""
        ...

    @property
    def aliases(self) -> tuple[str, ...]:
        """Alternative command names, e.g. ('/q',) for '/quit'."""
        return ()

    @property
    @abstractmethod
    def category(self) -> CommandCategory:
        """Taxonomy layer: core, power, devops, automation."""
        ...

    @property
    @abstractmethod
    def safety_level(self) -> SafetyLevel:
        """Risk classification for safety guards."""
        ...

    @property
    def bypass_tier(self) -> BypassTier:
        """Queue-bypass classification."""
        return BypassTier.QUEUED

    def validate(self, ctx: CommandContext) -> str | None:
        """Pre-execution validation. Returns error message string if invalid, else None."""
        return None

    @abstractmethod
    async def execute(self, ctx: CommandContext) -> CommandResult:
        """Execute the command logic. Must return a CommandResult."""
        ...


__all__ = ["BaseCommandHandler", "CommandContext", "CommandResult"]
