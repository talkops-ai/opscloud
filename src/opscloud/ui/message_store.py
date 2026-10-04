"""In-memory message store for the OpsCloud TUI.

Keeps an ordered, thread-isolated history of all conversation messages so they
can be replayed when switching threads or resuming sessions.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opscloud.ui.widgets.messages import MessageList


class MessageType(Enum):
    """Type/Role of a conversation message."""

    USER = auto()
    ASSISTANT = auto()
    TOOL = auto()
    SYSTEM = auto()
    ERROR = auto()
    SKILL = auto()
    DIFF = auto()
    QUEUED_USER = auto()
    TOOL_GROUP_SUMMARY = auto()


class ToolStatus(Enum):
    """Lifecycle status of a tool call message."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class MessageData:
    """Rich message record stored in history."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    msg_type: MessageType = MessageType.USER
    content: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_status: ToolStatus | None = None
    tool_call_id: str | None = None
    parent_id: str | None = None  # For subagent child messages


class MessageStore:
    """Thread-aware in-memory message store.

    Keyed by thread ID to maintain full isolation across conversations.
    """

    def __init__(self) -> None:
        self._threads: dict[str, list[MessageData]] = {}

    def append(
        self,
        msg_type: MessageType,
        content: str,
        thread_id: str = "default",
        tool_status: ToolStatus | None = None,
        tool_call_id: str | None = None,
        parent_id: str | None = None,
        **metadata: Any,
    ) -> MessageData:
        """Add a message to the specified thread's store."""
        msg = MessageData(
            msg_type=msg_type,
            content=content,
            metadata=metadata,
            tool_status=tool_status,
            tool_call_id=tool_call_id,
            parent_id=parent_id,
        )
        if thread_id not in self._threads:
            self._threads[thread_id] = []
        self._threads[thread_id].append(msg)
        return msg

    def get_thread(self, thread_id: str = "default") -> list[MessageData]:
        """Return all messages for thread_id (newest last)."""
        return list(self._threads.get(thread_id, []))

    def update_tool_status(
        self,
        call_id: str,
        status: ToolStatus,
        result: str | None = None,
        thread_id: str = "default",
    ) -> bool:
        """Update the tool status and optional result for a tool call message."""
        messages = self._threads.get(thread_id, [])
        for msg in reversed(messages):
            if msg.tool_call_id == call_id:
                msg.tool_status = status
                if result is not None:
                    msg.content = result
                return True
        return False

    def clear(self, thread_id: str | None = None) -> None:
        """Remove messages for thread_id (or all threads if None)."""
        if thread_id is None:
            self._threads.clear()
        elif thread_id in self._threads:
            self._threads[thread_id].clear()

    def replay_to_widgets(
        self, messages_widget: MessageList, thread_id: str = "default"
    ) -> None:
        """Replay stored messages for a thread into a MessageList widget."""
        messages_widget.clear()
        for msg in self.get_thread(thread_id):
            if msg.msg_type == MessageType.USER:
                messages_widget.add_user_message(msg.content)
            elif msg.msg_type == MessageType.ASSISTANT:
                messages_widget.start_assistant_message()
                messages_widget.append_assistant_token(msg.content)
                messages_widget.finish_assistant_message()
            elif msg.msg_type == MessageType.TOOL:
                messages_widget.add_tool_call(
                    name=msg.metadata.get("name", "tool"),
                    call_id=msg.tool_call_id or "",
                    args=msg.metadata.get("args", {}),
                )
                if msg.content:
                    messages_widget.update_tool_result(
                        call_id=msg.tool_call_id or "",
                        result=msg.content,
                    )
            elif msg.msg_type == MessageType.SYSTEM:
                messages_widget.add_system_message(msg.content)
            elif msg.msg_type == MessageType.ERROR:
                messages_widget.add_error_message(msg.content)
            elif msg.msg_type == MessageType.SKILL:
                messages_widget.add_skill_message(
                    name=msg.metadata.get("name", "skill"),
                    content=msg.content,
                )

    def __len__(self) -> int:
        return sum(len(msgs) for msgs in self._threads.values())
