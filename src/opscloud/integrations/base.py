"""Abstract base classes for external interface and messaging integrations.

To integrate an external interface or runner, subclass ``MessagingIntegration``
and implement the abstract methods. The generic ``GraphStreamBridge`` and
``ThreadStore`` can be reused as-is.

Data flow::

    External Event → IncomingMessage → MessagingIntegration.handle_message()
                                           │
                                           ├─ IdentityMapper.resolve()
                                           ├─ ThreadStore.resolve()
                                           └─ GraphStreamBridge.process_stream()
                                                    │
                                                    └─ StreamSink (interface-specific)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IncomingMessage:
    """Platform-agnostic representation of an incoming user message.

    Each platform adapter (Slack Bolt handler, Teams webhook, etc.)
    converts its native event payload into this structure before handing
    it to :meth:`MessagingIntegration.handle_message`.
    """

    text: str
    """The user's message text, stripped of platform-specific markup."""

    user_id: str
    """Platform-native user identifier (e.g. Slack ``U…``, Teams AAD OID)."""

    thread_id: str
    """Platform-native thread identifier used for reply threading."""

    channel_id: str
    """Platform-native channel / conversation identifier."""

    platform: str
    """Platform key: ``"slack"``, ``"teams"``, ``"discord"``, etc."""

    raw_event: dict[str, Any] = field(default_factory=dict)
    """Full, unmodified platform event payload for advanced use cases."""

    context: dict[str, Any] = field(default_factory=dict)
    """Additional context extracted from the platform
    (e.g. Slack assistant ``context`` object with ``channel_id``,
    ``team_id``, ``enterprise_id``)."""


@dataclass
class InteractionPayload:
    """Platform-agnostic representation of an interactive user action.

    Covers button clicks, menu selections, modal submissions, etc.
    The platform adapter converts its native interactive payload into
    this structure before handing it to
    :meth:`MessagingIntegration.handle_interaction`.
    """

    action_id: str
    """Platform-native action identifier (e.g. ``"approve_deploy"``)."""

    user_id: str
    """Platform-native user identifier of the person who acted."""

    thread_id: str
    """Thread identifier linking this action back to the conversation."""

    channel_id: str
    """Channel / conversation where the interactive element was posted."""

    platform: str
    """Platform key: ``"slack"``, ``"teams"``, ``"discord"``, etc."""

    value: str | None = None
    """Button value, selected option text, or freeform input."""

    raw_payload: dict[str, Any] = field(default_factory=dict)
    """Full, unmodified interactive payload from the platform."""


class MessagingIntegration(ABC):
    """Base class all interface and messaging adapters must implement.

    Subclasses handle four responsibilities:

    1. **Message ingestion** — Convert external events into
       :class:`IncomingMessage` and run them through the LangGraph agent.
    2. **Streaming output** — Pipe agent output tokens to the interface
       in real time.
    3. **HITL rendering** — Render ``interrupt()`` payloads as
       actionable confirmation / approval requests.
    4. **Identity resolution** — Map caller IDs to internal
       identities and cloud RBAC roles.
    """

    @abstractmethod
    async def handle_message(self, message: IncomingMessage) -> None:
        """Process an incoming user message through the LangGraph agent.

        This method should:

        1. Resolve the user's identity and RBAC permissions.
        2. Map the platform thread to a LangGraph ``thread_id``.
        3. Build the graph input state.
        4. Stream graph execution to the platform via a ``StreamSink``.
        """

    @abstractmethod
    async def handle_interaction(self, payload: InteractionPayload) -> None:
        """Handle an interactive action (approve / reject / edit).

        Typically resumes an interrupted LangGraph run with
        ``Command(resume=...)``.
        """

    @abstractmethod
    async def send_text(
        self,
        channel_id: str,
        thread_id: str,
        text: str,
    ) -> None:
        """Send a plain-text message to the platform."""

    @abstractmethod
    async def send_blocks(
        self,
        channel_id: str,
        thread_id: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        """Send rich / structured content to the platform.

        "Blocks" is a generic term — on Slack it maps to Block Kit JSON,
        on Teams to Adaptive Cards, etc.
        """

    @abstractmethod
    async def set_typing_status(
        self,
        channel_id: str,
        thread_id: str,
    ) -> None:
        """Show a typing / thinking indicator on the platform."""
