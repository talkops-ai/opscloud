"""Domain models for hook lifecycle invocations and decisions."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast
from uuid import UUID

from langchain_core.messages import ToolMessage
from pydantic import BaseModel, ConfigDict, Field

from opscloud.json_types import JSON_VALUE_ADAPTER, JsonObject, JsonValue
from opscloud.security.approval_mode import ApprovalMode

if TYPE_CHECKING:
    from langgraph.types import Command


class _DomainModel(BaseModel):
    """Base model for hook domain objects with common serialization."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class HookEvent(StrEnum):
    """Supported hook lifecycle events."""

    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    SESSION_END = "SessionEnd"
    PERMISSION_REQUEST = "PermissionRequest"
    NOTIFICATION = "Notification"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    PRE_COMPACT = "PreCompact"
    STOP = "Stop"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"


class HookOwner(StrEnum):
    """Process responsible for originating an event."""

    CLIENT = "client"
    SERVER = "server"


class SessionStartCause(StrEnum):
    """Reason a session-start event occurred."""

    STARTUP = "startup"
    RESUME = "resume"
    CLEAR = "clear"
    COMPACT = "compact"


class SessionEndCause(StrEnum):
    """Reason a session-end event occurred."""

    CLEAR = "clear"
    RESUME = "resume"
    LOGOUT = "logout"
    PROMPT_INPUT_EXIT = "prompt_input_exit"
    BYPASS_PERMISSIONS_DISABLED = "bypass_permissions_disabled"
    OTHER = "other"


class OpscloudNotificationKind(StrEnum):
    """OpsCloud lifecycle notifications with compatible wire mappings."""

    PERMISSION_REQUIRED = "permission_required"
    AGENT_NEEDS_INPUT = "agent_needs_input"
    AGENT_COMPLETED = "agent_completed"


class CompactTrigger(StrEnum):
    """Reason context compaction was requested."""

    MANUAL = "manual"
    AUTO = "auto"


type EffortLevel = Literal["none", "low", "medium", "high", "xhigh", "max"]


class ToolCallData(_DomainModel):
    """Native tool-call data used by hook lifecycle owners."""

    id: str
    name: str
    args: JsonObject
    mcp_server: str | None = None


class AgentIdentity(_DomainModel):
    """Resolved subagent identity."""

    id: str
    name: str


class OpscloudNotification(_DomainModel):
    """A notification emitted by an OpsCloud lifecycle owner."""

    type: str
    message: str
    title: str | None = None


class BackgroundTaskSnapshot(_DomainModel):
    """Background task state captured for a hook invocation."""

    id: str
    type: str
    status: str
    description: str
    command: str | None = None
    agent_type: str | None = None
    server: str | None = None
    tool: str | None = None
    name: str | None = None


class SessionCronSnapshot(_DomainModel):
    """Scheduled session prompt captured for a hook invocation."""

    id: str
    schedule: str
    recurring: bool
    prompt: str


class HookContext(_DomainModel):
    """Context shared by every domain hook event."""

    thread_id: str
    cwd: Path
    prompt_id: UUID | None = None
    approval_mode: ApprovalMode
    effort: EffortLevel | None = None
    agent: AgentIdentity | None = None
    transcript_revision: str | None = None


class SessionStartEvent(_DomainModel):
    """Domain payload for `SessionStart`."""

    event: Literal[HookEvent.SESSION_START]
    cause: SessionStartCause
    model: str | None = None


class UserPromptSubmitEvent(_DomainModel):
    """Domain payload for `UserPromptSubmit`."""

    event: Literal[HookEvent.USER_PROMPT_SUBMIT]
    prompt: str


class SessionEndEvent(_DomainModel):
    """Domain payload for `SessionEnd`."""

    event: Literal[HookEvent.SESSION_END]
    cause: SessionEndCause


class PermissionRequestEvent(_DomainModel):
    """Domain payload for `PermissionRequest`."""

    event: Literal[HookEvent.PERMISSION_REQUEST]
    call: ToolCallData


class NotificationEvent(_DomainModel):
    """Domain payload for `Notification`."""

    event: Literal[HookEvent.NOTIFICATION]
    notification: OpscloudNotification


class PreToolUseEvent(_DomainModel):
    """Domain payload for `PreToolUse`."""

    event: Literal[HookEvent.PRE_TOOL_USE]
    call: ToolCallData


class PostToolUseEvent(_DomainModel):
    """Domain payload for `PostToolUse`."""

    event: Literal[HookEvent.POST_TOOL_USE]
    call: ToolCallData
    result: JsonValue
    duration_ms: int | None = None

    @classmethod
    def from_tool_result(
        cls,
        result: ToolMessage | Command[Any],
        *,
        call: ToolCallData,
        duration_ms: int | None = None,
    ) -> PostToolUseEvent:
        """Construct a PostToolUseEvent from a tool execution result.

        Args:
            result: Tool execution result message or command.
            call: Associated tool call data.
            duration_ms: Execution duration in milliseconds, if available.

        Returns:
            PostToolUseEvent: Constructed domain event model.
        """
        if isinstance(result, ToolMessage):
            value: object = result.model_dump(mode="json")
        else:
            value = JSON_VALUE_ADAPTER.dump_python(cast(Any, result), mode="json", warnings=False)
        return cls(
            event=HookEvent.POST_TOOL_USE,
            call=call,
            result=JSON_VALUE_ADAPTER.validate_python(value),
            duration_ms=duration_ms,
        )


class PostToolUseFailureEvent(_DomainModel):
    """Domain payload for `PostToolUseFailure`."""

    event: Literal[HookEvent.POST_TOOL_USE_FAILURE]
    call: ToolCallData
    error: str
    is_interrupt: bool = False
    duration_ms: int | None = None


class PreCompactEvent(_DomainModel):
    """Domain payload for `PreCompact`."""

    event: Literal[HookEvent.PRE_COMPACT]
    trigger: CompactTrigger
    custom_instructions: str = ""


class StopEvent(_DomainModel):
    """Domain payload for `Stop`."""

    event: Literal[HookEvent.STOP]
    continuation_count: int
    last_assistant_message: str
    background_tasks: list[BackgroundTaskSnapshot] = Field(default_factory=list)
    session_crons: list[SessionCronSnapshot] = Field(default_factory=list)


class SubagentStartEvent(_DomainModel):
    """Domain payload for `SubagentStart`."""

    event: Literal[HookEvent.SUBAGENT_START]
    agent: AgentIdentity


class SubagentStopEvent(_DomainModel):
    """Domain payload for `SubagentStop`."""

    event: Literal[HookEvent.SUBAGENT_STOP]
    agent: AgentIdentity
    continuation_count: int
    last_assistant_message: str
    transcript_revision: str | None = None
    background_tasks: list[BackgroundTaskSnapshot] = Field(default_factory=list)
    session_crons: list[SessionCronSnapshot] = Field(default_factory=list)


type HookDomainEvent = Annotated[
    SessionStartEvent
    | UserPromptSubmitEvent
    | SessionEndEvent
    | PermissionRequestEvent
    | NotificationEvent
    | PreToolUseEvent
    | PostToolUseEvent
    | PostToolUseFailureEvent
    | PreCompactEvent
    | StopEvent
    | SubagentStartEvent
    | SubagentStopEvent,
    Field(discriminator="event"),
]


class HookInvocation(_DomainModel):
    """A domain hook event with its invocation context."""

    context: HookContext
    event: HookDomainEvent


class HookDiagnostic(_DomainModel):
    """Structured diagnostic produced while processing a hook."""

    code: str
    severity: Literal["debug", "warning", "error"]
    message: str
    handler_id: str | None = None
    field: str | None = None


class PermissionEffect(_DomainModel):
    """Normalized permission result from hook processing."""

    behavior: Literal["allow", "deny", "ask", "none"]
    reason: str | None = None
    interrupt: bool = False


class BaseHookDecision(_DomainModel):
    """Fields common to every event-specific hook decision."""

    continue_processing: bool = True
    stop_reason: str | None = None
    user_notices: list[str] = Field(default_factory=list)
    terminal_sequences: list[str] = Field(default_factory=list)
    diagnostics: list[HookDiagnostic] = Field(default_factory=list)


class SessionStartDecision(BaseHookDecision):
    """Decision returned for `SessionStart`."""

    event: Literal[HookEvent.SESSION_START]
    context: list[str] = Field(default_factory=list)


class UserPromptSubmitDecision(BaseHookDecision):
    """Decision returned for `UserPromptSubmit`."""

    event: Literal[HookEvent.USER_PROMPT_SUBMIT]
    context: list[str] = Field(default_factory=list)
    suppress_original_prompt: bool = False


class SessionEndDecision(BaseHookDecision):
    """Decision returned for `SessionEnd`."""

    event: Literal[HookEvent.SESSION_END]


class PermissionRequestDecision(BaseHookDecision):
    """Decision returned for `PermissionRequest`."""

    event: Literal[HookEvent.PERMISSION_REQUEST]
    permission: PermissionEffect


class NotificationDecision(BaseHookDecision):
    """Decision returned for `Notification`."""

    event: Literal[HookEvent.NOTIFICATION]


class PreToolUseDecision(BaseHookDecision):
    """Decision returned for `PreToolUse`."""

    event: Literal[HookEvent.PRE_TOOL_USE]
    permission: PermissionEffect
    context: list[str] = Field(default_factory=list)


class PostToolUseDecision(BaseHookDecision):
    """Decision returned for `PostToolUse`."""

    event: Literal[HookEvent.POST_TOOL_USE]
    feedback: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)


class PostToolUseFailureDecision(BaseHookDecision):
    """Decision returned for `PostToolUseFailure`."""

    event: Literal[HookEvent.POST_TOOL_USE_FAILURE]
    feedback: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)


class PreCompactDecision(BaseHookDecision):
    """Decision returned for `PreCompact`."""

    event: Literal[HookEvent.PRE_COMPACT]


class StopDecision(BaseHookDecision):
    """Decision returned for `Stop`."""

    event: Literal[HookEvent.STOP]
    continue_loop: bool
    feedback: list[str] = Field(default_factory=list)


class SubagentStartDecision(BaseHookDecision):
    """Decision returned for `SubagentStart`."""

    event: Literal[HookEvent.SUBAGENT_START]
    context: list[str] = Field(default_factory=list)


class SubagentStopDecision(BaseHookDecision):
    """Decision returned for `SubagentStop`."""

    event: Literal[HookEvent.SUBAGENT_STOP]
    context: list[str] = Field(default_factory=list)


type HookDecision = Annotated[
    SessionStartDecision
    | UserPromptSubmitDecision
    | SessionEndDecision
    | PermissionRequestDecision
    | NotificationDecision
    | PreToolUseDecision
    | PostToolUseDecision
    | PostToolUseFailureDecision
    | PreCompactDecision
    | StopDecision
    | SubagentStartDecision
    | SubagentStopDecision,
    Field(discriminator="event"),
]


class HookEffect(_DomainModel):
    """Normalized effect produced by one hook handler."""

    handler_id: str
    decision: HookDecision
