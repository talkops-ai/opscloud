"""Bridge between LangGraph graph execution and terminal/external stream sinks.

Uses ``stream_events(version="v3")`` / Pregel streams with model-agnostic
event translation and thinking/reasoning extraction across all major LLM
providers (Gemini, Anthropic, OpenAI, DeepSeek, and inline XML).

Output rendering is handled by the :class:`StreamSink` protocol.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import (
    AIMessageChunk,
    ToolMessage,
)

from opscloud.middleware.goal_state_notice import is_conversation_control_message
from opscloud.utils.logger import AgentLogger

logger = AgentLogger("StreamBridge")


# ---------------------------------------------------------------------------
# Tool Name Formatting
# ---------------------------------------------------------------------------


def _humanize_tool_name(raw_name: str) -> str:
    """Convert ``snake_case`` tool name to *Title Case* display name."""
    if not raw_name:
        return "Tool"
    name = raw_name
    for prefix in (
        "transfer_to_",
        "aws_",
        "terraform_",
        "tofu_",
        "cloud_",
        "kubernetes_",
        "kubectl_",
        "k8s_",
        "helm_",
    ):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name.replace("_", " ").strip().title() or raw_name.title()


# ---------------------------------------------------------------------------
# Model-Agnostic Thinking & Text Extraction
# ---------------------------------------------------------------------------


def _extract_thinking_value(val: Any) -> list[str]:
    """Recursively extract thinking text snippets from strings, lists, or dicts."""
    parts: list[str] = []
    if not val:
        return parts
    if isinstance(val, str):
        parts.append(val)
    elif isinstance(val, dict):
        for k in ("thinking", "thought", "thoughts", "reasoning", "reasoning_content", "text", "summary", "content"):
            sub = val.get(k)
            if isinstance(sub, str) and sub:
                parts.append(sub)
            elif isinstance(sub, (list, dict)):
                parts.extend(_extract_thinking_value(sub))
    elif isinstance(val, list):
        for item in val:
            parts.extend(_extract_thinking_value(item))
    return parts


def _extract_text_and_thinking(
    content: Any,
    additional_kwargs: dict[str, Any] | None = None,
    response_metadata: dict[str, Any] | None = None,
    msg_obj: Any = None,
) -> tuple[str, str]:
    """Extract regular response text and internal thinking/reasoning text.

    Supports:
    - DeepSeek: ``reasoning_content`` attribute
    - Anthropic: ``thinking`` attribute or content blocks
    - Gemini: ``thoughts`` / ``thinking`` in metadata or parts
    - OpenAI: ``additional_kwargs["reasoning"]`` or ``additional_kwargs["thinking"]``
    - Inline XML: ``<thinking>...</thinking>`` or ``<thought>...</thought>``

    Returns:
        tuple[str, str]: (text, thinking_text)
    """
    text_parts: list[str] = []
    thinking_parts: list[str] = []

    # 1. Direct reasoning attributes on message object
    if msg_obj is not None:
        direct_thinking = (
            getattr(msg_obj, "reasoning_content", None)
            or getattr(msg_obj, "thinking", None)
            or getattr(msg_obj, "thoughts", None)
            or getattr(msg_obj, "reasoning", None)
        )
        if direct_thinking:
            thinking_parts.extend(_extract_thinking_value(direct_thinking))

    # 2. Additional kwargs and response metadata
    combined: dict[str, Any] = {}
    if additional_kwargs and isinstance(additional_kwargs, dict):
        combined.update(additional_kwargs)
    if response_metadata and isinstance(response_metadata, dict):
        combined.update(response_metadata)

    thinking = (
        combined.get("thinking")
        or combined.get("thoughts")
        or combined.get("reasoning_content")
        or combined.get("thought")
        or combined.get("reasoning")
    )
    if thinking:
        thinking_parts.extend(_extract_thinking_value(thinking))

    # 3. Content blocks & string parsing
    if isinstance(content, str):
        if "<thinking>" in content or "<thought>" in content:
            raw = content
            for tag_open, tag_close in [
                ("<thinking>", "</thinking>"),
                ("<thought>", "</thought>"),
            ]:
                while tag_open in raw:
                    start = raw.find(tag_open)
                    end = raw.find(tag_close, start)
                    if end != -1:
                        think_chunk = raw[start + len(tag_open) : end]
                        thinking_parts.append(think_chunk)
                        raw = raw[:start] + raw[end + len(tag_close) :]
                    else:
                        think_chunk = raw[start + len(tag_open) :]
                        thinking_parts.append(think_chunk)
                        raw = raw[:start]
            if raw:
                text_parts.append(raw)
        elif content:
            text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type", "")
                think_val = (
                    block.get("thinking")
                    or block.get("thoughts")
                    or block.get("reasoning")
                    or block.get("reasoning_content")
                )
                if think_val:
                    thinking_parts.extend(_extract_thinking_value(think_val))
                elif block_type in ("thinking", "thought", "reasoning"):
                    text_val = block.get("text") or block.get("thinking") or block.get("thought") or block.get("summary")
                    if text_val:
                        thinking_parts.extend(_extract_thinking_value(text_val))
                elif isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
            elif hasattr(block, "text") and isinstance(block.text, str):
                text_parts.append(block.text)
    elif content is not None:
        text_parts.append(str(content))

    return "".join(text_parts), "".join(thinking_parts)


def _extract_text(
    content: Any,
    additional_kwargs: dict[str, Any] | None = None,
    response_metadata: dict[str, Any] | None = None,
    msg_obj: Any = None,
) -> str:
    """Extract string text with optional formatted thinking prefix."""
    text, thinking = _extract_text_and_thinking(content, additional_kwargs, response_metadata, msg_obj)
    if thinking:
        return f"> *Thinking:* {thinking}\n\n{text}"
    return text


# ---------------------------------------------------------------------------
# StreamSink protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StreamSink(Protocol):
    """Protocol for platform-specific stream output handlers."""

    async def on_stream_start(
        self,
        channel_id: str,
        thread_id: str,
    ) -> None:
        """Called once when a new streaming response begins."""
        ...

    async def on_token(self, token: str) -> None:
        """Called for each LLM output token."""
        ...

    async def on_thinking(self, text: str) -> None:
        """Called for LLM thinking/reasoning blocks."""
        ...

    async def on_tool_call_started(
        self,
        name: str,
        call_id: str,
        args: dict[str, Any],
    ) -> None:
        """Called when a tool execution begins."""
        ...

    async def on_tool_call_completed(
        self,
        name: str,
        call_id: str,
        result: str,
    ) -> None:
        """Called when a tool execution finishes."""
        ...

    async def on_interrupt(self, interrupt_value: dict[str, Any]) -> None:
        """Called when the graph hits an interrupt (HITL / Ask User)."""
        ...

    async def on_subagent_event(self, agent_name: str, status: str) -> None:
        """Called on subagent start/stop/progress."""
        ...

    async def on_rubric_event(self, data: dict[str, Any]) -> None:
        """Called on rubric evaluation start/progress/complete."""
        ...

    async def on_auto_mode_event(self, data: dict[str, Any]) -> None:
        """Called on auto-mode HITL classification events."""
        ...

    async def on_stream_end(self) -> None:
        """Called when the graph run completes."""
        ...

    async def on_error(self, error: Exception) -> None:
        """Called when execution raises an exception."""
        ...

    async def on_pre_interrupt(self) -> None:
        """Called just before on_interrupt."""
        ...


# ---------------------------------------------------------------------------
# GraphStreamBridge
# ---------------------------------------------------------------------------


class GraphStreamBridge:
    """Model-agnostic stream bridge between LangGraph runtime and StreamSink."""

    def __init__(self, sink: StreamSink) -> None:
        """Initialize GraphStreamBridge with the target stream sink.

        Args:
            sink: Output stream sink to receive parsed events.
        """
        self.sink = sink
        self._buffered_text: list[str] = []
        self._buffered_thinking: list[str] = []

    @property
    def buffered_text(self) -> str:
        """Return all text emitted so far."""
        return "".join(self._buffered_text)

    @property
    def buffered_thinking(self) -> str:
        """Return all thinking emitted so far."""
        return "".join(self._buffered_thinking)

    async def process_stream(
        self,
        stream: AsyncIterator[tuple[str, Any]],
        channel_id: str = "default",
        thread_id: str = "default",
    ) -> str:
        """Consume LangGraph stream events and dispatch to StreamSink.

        Handles stream modes:
        - ``messages``: (AIMessageChunk, metadata)
        - ``updates``: state updates, interrupts
        - ``custom``: subagents, rubrics, auto-mode events

        Returns:
            The complete accumulated response text.
        """
        await self.sink.on_stream_start(channel_id, thread_id)
        self._buffered_text.clear()
        self._buffered_thinking.clear()

        try:
            async for mode, payload in stream:
                if mode == "messages":
                    await self._handle_message_chunk(payload)
                elif mode == "updates":
                    await self._handle_updates(payload)
                elif mode == "custom":
                    await self._handle_custom(payload)
            await self.sink.on_stream_end()
        except Exception as exc:
            logger.error(f"Error during stream processing: {exc}", exc_info=True)
            await self.sink.on_error(exc)
            raise

        return self.buffered_text

    async def _handle_message_chunk(self, chunk_tuple: Any) -> None:
        """Process an AIMessageChunk or ToolMessage from the stream."""
        msg = chunk_tuple[0] if isinstance(chunk_tuple, tuple) and len(chunk_tuple) >= 1 else chunk_tuple

        if is_conversation_control_message(msg):
            return

        if isinstance(msg, AIMessageChunk):
            # Extract thinking and text
            text, thinking = _extract_text_and_thinking(
                msg.content,
                additional_kwargs=getattr(msg, "additional_kwargs", None),
                response_metadata=getattr(msg, "response_metadata", None),
                msg_obj=msg,
            )
            if thinking:
                self._buffered_thinking.append(thinking)
                await self.sink.on_thinking(thinking)
            if text:
                self._buffered_text.append(text)
                await self.sink.on_token(text)

            # Tool call chunks
            if hasattr(msg, "tool_call_chunks") and msg.tool_call_chunks:
                for tc in msg.tool_call_chunks:
                    call_id = tc.get("id") or ""
                    name = tc.get("name") or ""
                    raw_args = tc.get("args")
                    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
                    if name:
                        await self.sink.on_tool_call_started(name, call_id, args)

        elif isinstance(msg, ToolMessage):
            call_id = getattr(msg, "tool_call_id", "") or ""
            name = getattr(msg, "name", "") or "tool"
            result = str(getattr(msg, "content", ""))
            await self.sink.on_tool_call_completed(name, call_id, result)

    async def _handle_updates(self, updates: Any) -> None:
        """Handle state updates and __interrupt__ events."""
        if not isinstance(updates, dict):
            return

        # Check for interrupts
        interrupt_val = updates.get("__interrupt__")
        if interrupt_val:
            await self.sink.on_pre_interrupt()
            if isinstance(interrupt_val, (list, tuple)) and interrupt_val:
                raw_int = interrupt_val[0]
                val = getattr(raw_int, "value", raw_int)
                if isinstance(val, dict):
                    await self.sink.on_interrupt(val)
                else:
                    await self.sink.on_interrupt({"raw": val})
            elif isinstance(interrupt_val, dict):
                await self.sink.on_interrupt(interrupt_val)

    async def _handle_custom(self, custom: Any) -> None:
        """Handle custom events (subagents, rubrics, auto-mode)."""
        if not isinstance(custom, dict):
            return

        event_type = custom.get("type", "")
        if event_type == "subagent":
            name = custom.get("name", "subagent")
            status = custom.get("status", "running")
            await self.sink.on_subagent_event(name, status)
        elif event_type.startswith("rubric_"):
            await self.sink.on_rubric_event(custom)
        elif event_type == "auto_mode":
            await self.sink.on_auto_mode_event(custom)
