"""Stream bridge between LangGraph SDK client and Textual widgets.

The ``TextualAdapter`` consumes streaming events from ``client.runs.stream()``
and routes them directly to Textual widgets via widget mutation.  Only
lifecycle events (interrupts, subagents, stream end/error) use the Textual
message bus.  High-frequency token and tool updates bypass ``post_message``
to avoid flooding the event queue.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
from opscloud.utils.logger import get_logger
import math
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, Callable
import uuid

from textual.message import Message

from opscloud.utils.session_stats import SessionStats
from opscloud.ui.widgets._tool_stream import (
    ToolCallBuffer,
    ToolCallBufferKey,
    tool_call_buffer_key,
)

from opscloud.config.settings import get_glyphs

if TYPE_CHECKING:
    from opscloud.ui.widgets.messages import MessageList
    from opscloud.ui.widgets.status import StatusBar

logger = get_logger(__name__)


@dataclass
class _TurnStreamState:
    thinking_start_t: float | None = None
    turn_context_tokens: int = 0
    start_session_tokens: int = 0
    received_session_tokens: bool = False
    assistant_message_by_namespace: dict[tuple[str, ...], Any] = field(default_factory=dict)
    pending_text_by_namespace: dict[tuple[str, ...], str] = field(default_factory=dict)
    tool_call_buffers: dict[ToolCallBufferKey, ToolCallBuffer] = field(default_factory=dict)
    displayed_tool_ids: set[str] = field(default_factory=set)
    recorded_message_usage: dict[str, tuple[int, int]] = field(default_factory=dict)


def _read_mentioned_file(file_path: Path, max_embed_bytes: int = 256 * 1024) -> str:
    """Read a mentioned file for inline embedding."""
    file_size = file_path.stat().st_size
    if file_size > max_embed_bytes:
        size_kb = file_size // 1024
        return (
            f"\n### {file_path.name}\n"
            f"Path: `{file_path}`\n"
            f"Size: {size_kb}KB (too large to embed, use read_file tool to view)"
        )
    content = file_path.read_text(encoding="utf-8")
    return f"\n### {file_path.name}\nPath: `{file_path}`\n```text\n{content}\n```"


def _is_renderable_subagent_event(data: Any, *, is_main_agent: bool) -> bool:
    return isinstance(data, dict) and data.get("type") == "subagent"


def _is_auto_mode_classifier_chunk(metadata: dict[str, Any] | None) -> bool:
    """Check if a message chunk is internal Auto mode classifier output.

    The Auto mode authorization classifier is invoked with
    `config={"metadata": {"lc_source": "auto_mode_classifier"}}`
    (see `AutoModeHITLMiddleware` in `opscloud.middleware.auto_mode_hitl`), which
    LangChain's callback system merges into the stream metadata dict.

    Args:
        metadata: The metadata dict from the stream chunk.

    Returns:
        Whether the chunk should be hidden from the conversation transcript.
    """
    if metadata is None:
        return False
    return metadata.get("lc_source") == "auto_mode_classifier"


def _format_rubric_event(data: dict[str, Any]) -> str | None:
    """Format a concise rubric custom-stream event for the transcript."""
    glyphs = get_glyphs()
    event_type = data.get("type")
    if event_type == "rubric_evaluation_start":
        iteration = data.get("iteration", 0)
        show_iteration = data.get("show_iteration") is True
        label = (
            f" (iteration {iteration + 1})"
            if show_iteration and isinstance(iteration, int)
            else ""
        )
        return f"Checking acceptance criteria{label}{glyphs.ellipsis}"
    if event_type != "rubric_evaluation_end":
        return None

    result = data.get("result")
    if result is None:
        return None
    if result == "satisfied":
        return f"{glyphs.checkmark} Acceptance criteria satisfied"
    if result == "needs_revision":
        return f"{glyphs.retry} Acceptance criteria not yet satisfied"
    if result == "max_iterations_reached":
        return f"{glyphs.warning} Acceptance criteria not yet satisfied (iteration limit reached)"
    if result == "failed":
        return f"{glyphs.warning} Rubric is invalid or cannot be evaluated"
    if result == "grader_error":
        return f"{glyphs.warning} Acceptance criteria check failed"
    return f"{glyphs.warning} Acceptance criteria check ended"


def _format_rubric_details(data: dict[str, Any], *, goal_active: bool = False) -> str:
    """Format complete grader details without serializing or truncating payloads."""
    result = data.get("result")
    if result in {None, "satisfied"}:
        return ""

    sections: list[str] = []
    explanation = str(data.get("explanation") or "").strip()
    if explanation:
        sections.append(f"Explanation\n{explanation}")

    criteria = data.get("criteria")
    failing: list[tuple[str, str]] = []
    if isinstance(criteria, list):
        for criterion in criteria:
            if isinstance(criterion, dict) and criterion.get("passed") is False:
                name = str(criterion.get("name") or "Unnamed criterion").strip()
                gap = str(criterion.get("gap") or "").strip()
                failing.append((name, gap))
    if failing:
        lines = ["Unmet criteria"]
        for name, gap in failing:
            lines.append(f"- {name}" + (f"\n  {gap}" if gap else ""))
        sections.append("\n".join(lines))

    if result == "max_iterations_reached" and goal_active:
        next_step = (
            "The goal remains active. Continue with another prompt to resume or "
            "retry, use `/goal <objective>` to amend it, or `/goal clear` to clear it."
        )
    elif result in {"needs_revision", "max_iterations_reached"}:
        next_step = "Address every unmet criterion, then retry the check."
    elif result == "failed":
        next_step = "Review or replace the rubric before grading again."
    elif result == "grader_error":
        next_step = "Retry the check, or choose a different grader model."
    else:
        next_step = "Review the grader details before continuing."
    sections.append(f"Next step\n{next_step}")
    return "\n\n".join(sections)


def _format_thinking_tags(text: str) -> str:
    """Format inline <thinking> or <thought> XML tags into blockquotes."""
    if not text:
        return text
    text = text.replace("<thinking>", "> *Thinking:*\n> ").replace("</thinking>", "\n\n")
    text = text.replace("<thought>", "> *Thinking:*\n> ").replace("</thought>", "\n\n")
    return text


def _format_thinking_quote(text: str) -> str:
    """Format a thinking string cleanly into blockquote format."""
    if not text:
        return ""
    clean = text.strip()
    if not clean:
        return ""
    return f"> *Thinking:* {clean}\n\n"


def _extract_text_and_thinking(
    content: Any,
    additional_kwargs: dict[str, Any] | None = None,
    response_metadata: dict[str, Any] | None = None,
    msg_obj: Any = None,
) -> tuple[str, str]:
    """Extract regular response text and internal thinking/reasoning text from message.

    Returns:
        (text, thinking_text)
    """
    text_parts: list[str] = []
    thinking_parts: list[str] = []

    # 1. Direct reasoning attributes on message object (DeepSeek / Anthropic / Gemini / LangChain)
    if msg_obj is not None:
        direct_thinking = (
            getattr(msg_obj, "reasoning_content", None)
            or getattr(msg_obj, "thinking", None)
            or getattr(msg_obj, "thoughts", None)
        )
        if direct_thinking and isinstance(direct_thinking, str):
            thinking_parts.append(direct_thinking)

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
    )
    if thinking:
        if isinstance(thinking, str):
            thinking_parts.append(thinking)
        elif isinstance(thinking, dict) and isinstance(thinking.get("text"), str):
            thinking_parts.append(thinking["text"])
        elif isinstance(thinking, list):
            for t_item in thinking:
                if isinstance(t_item, str):
                    thinking_parts.append(t_item)
                elif isinstance(t_item, dict) and isinstance(t_item.get("text"), str):
                    thinking_parts.append(t_item["text"])

    # 3. Content blocks & string parsing
    if isinstance(content, str):
        if "<thinking>" in content or "<thought>" in content:
            raw = content
            for tag_open, tag_close in [("<thinking>", "</thinking>"), ("<thought>", "</thought>")]:
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
                    if isinstance(think_val, str):
                        thinking_parts.append(think_val)
                    elif isinstance(think_val, dict) and isinstance(think_val.get("text"), str):
                        thinking_parts.append(think_val["text"])
                elif block_type in ("thinking", "thought", "reasoning"):
                    text_val = block.get("text") or block.get("thinking") or block.get("thought")
                    if text_val and isinstance(text_val, str):
                        thinking_parts.append(text_val)
                elif isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
            elif hasattr(block, "text") and isinstance(getattr(block, "text"), str):
                text_parts.append(getattr(block, "text"))
    elif content is not None:
        text_parts.append(str(content))

    return "".join(text_parts), "".join(thinking_parts)


def _extract_text(
    content: Any,
    additional_kwargs: dict[str, Any] | None = None,
    response_metadata: dict[str, Any] | None = None,
    msg_obj: Any = None,
) -> str:
    """Extract string text from a message content string, list, or block structure (including thinking/reasoning blocks)."""
    text, thinking = _extract_text_and_thinking(content, additional_kwargs, response_metadata, msg_obj)
    if thinking:
        return f"> *Thinking:* {thinking}\n\n{text}"
    return text






class TextualAdapter:
    """Bridge between LangGraph SDK stream events and Textual widgets.

    Parses the event stream from ``client.runs.stream()`` and posts custom
    Textual events for thread-safe UI rendering and lifecycle tracking.
    """

    # ── Custom Textual Events ────────────────────────────
    # Only events that require app-level handling (lifecycle, approvals,
    # subagents) use the Textual message bus. High-frequency token and
    # tool-call updates bypass post_message entirely and mutate widgets
    # directly from the stream loop to avoid flooding the event queue.

    class InterruptRaised(Message):
        def __init__(
            self,
            tool_name: str,
            call_id: str,
            args: dict[str, Any],
            interrupt_type: str = "tool_approval",
        ) -> None:
            super().__init__()
            self.tool_name = tool_name
            self.call_id = call_id
            self.args = args
            self.interrupt_type = interrupt_type

    class SubagentSpawned(Message):
        def __init__(self, agent_name: str, task: str) -> None:
            super().__init__()
            self.agent_name = agent_name
            self.task = task

    class SubagentUpdate(Message):
        def __init__(self, agent_name: str, token: str, status: str = "") -> None:
            super().__init__()
            self.agent_name = agent_name
            self.token = token
            self.status = status

    class TokenStreamed(Message):
        """Fired when a new content token is streamed from the model."""
        def __init__(self, token: str) -> None:
            super().__init__()
            self.token = token

    class ToolCallStarted(Message):
        """Fired when a tool call begins execution."""
        def __init__(self, name: str, call_id: str = "") -> None:
            super().__init__()
            self.name = name
            self.call_id = call_id

    class ToolCallCompleted(Message):
        """Fired when a tool call finishes."""
        def __init__(self, name: str, call_id: str = "", result: str = "") -> None:
            super().__init__()
            self.name = name
            self.call_id = call_id
            self.result = result

    class StreamFinished(Message):
        def __init__(self, stats: SessionStats) -> None:
            super().__init__()
            self.stats = stats

    class StreamError(Message):
        def __init__(self, error: str) -> None:
            super().__init__()
            self.error = error

    def __init__(
        self,
        *,
        client: Any,
        assistant_id: str,
        messages_widget: MessageList | None = None,
        status_bar: StatusBar | None = None,
        auto_approve: bool = False,
        set_spinner: Any = None,
        on_subagent_event: Callable[[dict[str, Any]], None] | None = None,
        app: Any = None,
        prompt_manager: Any = None,
        request_approval: Callable[..., Any] | None = None,
        request_ask_user: Callable[..., Any] | None = None,
    ) -> None:
        self._client = client
        self._assistant_id = assistant_id
        self._messages: MessageList | None = messages_widget
        self._status_bar: StatusBar | None = status_bar
        self._auto_approve = auto_approve
        self._set_spinner = set_spinner
        self._on_subagent_event = on_subagent_event
        self._app = app
        self._prompt_manager = prompt_manager
        self._request_approval = request_approval
        self._request_ask_user = request_ask_user
        self._stats = SessionStats()
        self._cancel_event = asyncio.Event()
        self._active_tools_map: dict[str, str] = {}
        self._approval_events: dict[str, asyncio.Event] = {}
        self._approval_responses: dict[str, bool] = {}
        self._turn_number = 0

    @property
    def _effective_request_approval(self) -> Callable[..., Any] | None:
        if self._request_approval is not None:
            return self._request_approval
        if self._app and hasattr(self._app, "_request_approval"):
            return self._app._request_approval
        return None

    @property
    def _effective_request_ask_user(self) -> Callable[..., Any] | None:
        if self._request_ask_user is not None:
            return self._request_ask_user
        if self._app and hasattr(self._app, "_request_ask_user"):
            return self._app._request_ask_user
        return None

    @property
    def stats(self) -> SessionStats:
        return self._stats

    @property
    def connected(self) -> bool:
        """Return True when a client is bound and ready for streaming."""
        return self._client is not None

    def set_client(self, client: Any) -> None:
        """Bind or replace the LangGraph SDK client (deferred server startup).

        Called by ``OpsCloudApp`` when the background server worker posts
        ``ServerReady``.  After this call, ``stream_turn`` can execute.
        """
        self._client = client

    # ── Subroutines for stream_turn ───────────────────────

    def _prepare_stream_input(
        self,
        prompt: str,
        graph_input: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str | None]:
        """Build the graph input payload and turn ID.

        Returns:
            tuple[input_payload_dict, turn_id]
        """
        from langchain_core.messages import HumanMessage
        from opscloud.middleware.auto_mode import USER_PROMPT_METADATA_KEY, user_prompt_metadata

        if graph_input is not None:
            return graph_input, None

        from opscloud.input import parse_file_mentions

        prompt_text, mentioned_files = parse_file_mentions(prompt)
        max_embed_bytes = 256 * 1024

        if mentioned_files:
            context_parts = [prompt_text, "\n\n## Referenced Files\n"]
            for file_path in mentioned_files:
                try:
                    part = _read_mentioned_file(file_path, max_embed_bytes)
                    context_parts.append(part)
                except Exception as e:
                    context_parts.append(f"\n### {file_path.name}\n[Error reading file: {e}]")
            final_content = "\n".join(context_parts)
        else:
            final_content = prompt_text

        self._turn_number += 1
        turn_id = str(uuid.uuid4())

        human_msg = HumanMessage(
            content=final_content,
            additional_kwargs={
                USER_PROMPT_METADATA_KEY: user_prompt_metadata(
                    literal_user_text=prompt,
                    referenced_paths=[str(p) for p in mentioned_files],
                    turn_id=turn_id,
                )
            },
        )
        input_msg: dict[str, Any] = {
            "messages": [human_msg],
            "goal_criteria_request": None,
        }

        if self._app is not None:
            from opscloud.commands.power.goal import get_goal_state, GoalHandler

            goal_state = get_goal_state(self._app)
            if goal_state.is_actionable and goal_state.objective:
                input_msg["_goal_objective"] = goal_state.objective
                input_msg["_goal_rubric"] = goal_state.rubric
                input_msg["_goal_status"] = goal_state.status
                if goal_state.rubric:
                    input_msg["rubric"] = goal_state.rubric
            elif goal_state.next_rubric:
                input_msg["rubric"] = goal_state.next_rubric
                goal_state.next_rubric = None  # One-turn quality gate
                GoalHandler._sync_status_rubric(self._app, goal_state)
            elif goal_state.rubric:
                input_msg["rubric"] = goal_state.rubric

        return input_msg, turn_id

    async def _prepare_stream_config(
        self,
        thread_id: str,
        turn_id: str | None,
        context: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build stream configuration and enriched context dict.

        Returns:
            tuple[config_dict, enriched_context_dict]
        """
        from opscloud.approval_mode import ApprovalMode, awrite_approval_mode, coerce_approval_mode
        from opscloud.config.metadata import build_stream_config

        raw_mode = (
            getattr(self._app, "_approval_mode", ApprovalMode.MANUAL)
            if self._app is not None
            else ApprovalMode.MANUAL
        )
        selected_mode = raw_mode if isinstance(raw_mode, ApprovalMode) else coerce_approval_mode(raw_mode)

        agent_obj = (
            self._client
            or (getattr(self._app, "_client", None) if self._app is not None else None)
            or (getattr(self._app, "_agent", None) if self._app is not None else None)
        )
        live_key: str | None = None
        if agent_obj is not None:
            try:
                live_key = await awrite_approval_mode(agent_obj, thread_id, mode=selected_mode)
            except Exception:
                logger.warning("Failed to persist approval mode to store", exc_info=True)

        auto_approve_active = False

        eff = None
        if self._app is not None:
            eff = getattr(self._app, "_reasoning_effort", None) or getattr(
                getattr(self._app, "settings", None), "reasoning_effort", None
            )

        from pathlib import Path
        app_cwd = getattr(self._app, "_cwd", None) if self._app else None
        target_cwd = app_cwd or str(Path.cwd().resolve())

        config = await asyncio.to_thread(
            build_stream_config,
            thread_id,
            getattr(self, "_assistant_id", "agent"),
            turn_id=turn_id,
            turn_number=self._turn_number,
            approval_mode=selected_mode.value,
            approval_mode_key=live_key,
            auto_approve=auto_approve_active,
            cwd=target_cwd,
            reasoning_effort=eff,
        )

        if context is None or not isinstance(context, dict):
            context = {}
        enriched_context = context
        enriched_context["thread_id"] = thread_id
        if turn_id is not None:
            enriched_context["turn_id"] = turn_id
        enriched_context["approval_mode"] = selected_mode.value
        enriched_context["auto_approve"] = auto_approve_active
        if live_key is not None:
            enriched_context["approval_mode_key"] = live_key
        else:
            enriched_context.pop("approval_mode_key", None)

        enriched_context["cwd"] = target_cwd
        enriched_context["working_dir"] = target_cwd
        if "workspace" not in enriched_context or not isinstance(enriched_context["workspace"], dict):
            enriched_context["workspace"] = {"cwd": target_cwd}

        logger.debug("TUI: stream_turn start, thread_id: %s, context: %s", thread_id, enriched_context)
        return config, enriched_context

    def _handle_custom_stream_event(
        self,
        data: Any,
        is_main_agent: bool,
        stream_state: _TurnStreamState | None = None,
    ) -> None:
        """Handle mode == 'custom' stream event payloads."""
        if isinstance(data, dict) and data.get("type") == "session_cost":
            if stream_state is not None:
                stream_state.received_session_tokens = True
            total_usd = data.get("total")
            if isinstance(total_usd, (int, float)) and math.isfinite(total_usd) and total_usd >= 0:
                cost_val = float(total_usd)
                self._stats.total_cost_usd = cost_val
                if self._app:
                    setattr(self._app, "_session_cost_usd", cost_val)
                if self._status_bar is not None:
                    self._status_bar.set_cost(cost_val)
            total_tokens = data.get("total_tokens")
            if isinstance(total_tokens, int) and total_tokens >= 0:
                if self._app:
                    setattr(self._app, "_cumulative_session_tokens", total_tokens)
                if self._status_bar is not None:
                    ctx_toks = getattr(self._app, "_context_tokens", 0) if self._app else 0
                    self._status_bar.set_tokens(total_tokens, context_tokens=ctx_toks)
            return

        if isinstance(data, dict) and data.get("type") == "subagent_progress":
            action = str(data.get("action") or "Working...")
            sub_name = data.get("subagent_name") or data.get("id")
            sub_str = str(sub_name) if sub_name else None
            panel = getattr(self._app, "_get_subagent_panel", lambda: None)() if self._app else None
            if panel is not None:
                panel.set_subagent_activity(sub_str, action)
            has_attached_panel = panel is not None and getattr(panel, "is_attached", False)
            if self._set_spinner:
                if has_attached_panel:
                    try:
                        import asyncio
                        loop = asyncio.get_running_loop()
                        loop.create_task(self._set_spinner(None))
                    except RuntimeError:
                        pass
                else:
                    display_name = f"Subagent [{sub_str}]" if sub_str else "Subagent"
                    try:
                        import asyncio
                        loop = asyncio.get_running_loop()
                        loop.create_task(self._set_spinner(f"{display_name}: {action}"))
                    except RuntimeError:
                        pass
            return

        if isinstance(data, dict) and data.get("type") == "model_routed":
            provider = str(data.get("provider") or "")
            model_name = str(data.get("model") or "")
            effort = str(data.get("effort") or "")
            is_sub = data.get("is_subagent") or not is_main_agent
            if is_sub:
                panel = getattr(self._app, "_get_subagent_panel", lambda: None)() if self._app else None
                if panel is not None:
                    panel.set_subagent_model(None, model_name)
            else:
                if self._status_bar is not None and model_name:
                    self._status_bar.set_model(
                        provider=provider,
                        model=model_name,
                        effort=effort,
                    )
                if self._app is not None and model_name:
                    self._app._model = f"{provider}:{model_name}" if provider else model_name
                    if effort:
                        self._app._reasoning_effort = effort
            return

        if self._on_subagent_event is not None and _is_renderable_subagent_event(data, is_main_agent=is_main_agent):
            try:
                self._on_subagent_event(data)
            except Exception:
                logger.exception("subagent panel event handler failed")
        elif isinstance(data, dict):
            rubric_msg = data
            formatted_event = _format_rubric_event(rubric_msg)
            if formatted_event is not None and is_main_agent and self._messages is not None:
                details = (
                    _format_rubric_details(rubric_msg, goal_active=True)
                    if rubric_msg.get("type") == "rubric_evaluation_end"
                    else ""
                )
                from opscloud.ui.widgets.messages import RubricResultMessage, SystemMessage, ErrorMessage

                if rubric_msg.get("type") == "rubric_evaluation_end":
                    if self._app is not None and hasattr(self._app, "_handle_rubric_evaluation_end"):
                        try:
                            self._app._handle_rubric_evaluation_end(rubric_msg)
                        except Exception:
                            pass
                    if rubric_msg.get("result") == "grader_error":
                        widget = ErrorMessage(
                            "Acceptance-criteria grading failed because of a grader or "
                            "infrastructure error. The goal remains active, and its "
                            "completion request is still pending; it will be re-graded on "
                            "your next turn."
                        )
                    elif details:
                        widget = RubricResultMessage(formatted_event, details)
                    else:
                        widget = SystemMessage(formatted_event)
                else:
                    widget = SystemMessage(formatted_event)
                self._messages.mount(widget)

    def _handle_updates_stream_event(
        self,
        data: Any,
        pending_interrupts: dict[str, Any],
    ) -> None:
        """Handle mode == 'updates' stream event payloads and record interrupts."""
        if not isinstance(data, dict):
            return
        interrupts = data.get("__interrupt__")
        if interrupts:
            for interrupt_obj in interrupts:
                interrupt_id = getattr(interrupt_obj, "id", None)
                interrupt_val = getattr(interrupt_obj, "value", None)
                if interrupt_val and interrupt_id:
                    logger.debug("TUI: Handle interrupt %s: %s", interrupt_id, interrupt_val)
                    pending_interrupts[interrupt_id] = interrupt_val

    async def _handle_messages_stream_event(
        self,
        data: Any,
        ns_key: tuple[str, ...],
        is_main_agent: bool,
        stream_state: _TurnStreamState,
    ) -> None:
        """Handle mode == 'messages' stream event payloads."""
        from langchain_core.messages import HumanMessage, ToolMessage

        msg_obj, meta = data if isinstance(data, tuple) else (data, {})

        # 1. Update token usage stats
        usage_meta = (
            getattr(msg_obj, "usage_metadata", None)
            or (getattr(msg_obj, "response_metadata", None) or {}).get("usage")
            or (getattr(msg_obj, "response_metadata", None) or {}).get("token_usage")
            or (meta or {}).get("usage")
        )
        if isinstance(usage_meta, dict):
            inp_toks = int(usage_meta.get("input_tokens") or usage_meta.get("prompt_tokens") or 0)
            out_toks = int(usage_meta.get("output_tokens") or usage_meta.get("completion_tokens") or 0)
            msg_id = getattr(msg_obj, "id", None)
            if not msg_id or not isinstance(msg_id, str):
                msg_id = f"{ns_key}:{id(msg_obj)}"

            prev_inp, prev_out = stream_state.recorded_message_usage.get(msg_id, (0, 0))
            delta_inp = max(0, inp_toks - prev_inp)
            delta_out = max(0, out_toks - prev_out)

            if delta_inp > 0 or delta_out > 0:
                stream_state.recorded_message_usage[msg_id] = (max(prev_inp, inp_toks), max(prev_out, out_toks))
                self._stats.input_tokens += delta_inp
                self._stats.output_tokens += delta_out

                # Track active conversation context size
                if is_main_agent and inp_toks > 0:
                    stream_state.turn_context_tokens = max(stream_state.turn_context_tokens, inp_toks + out_toks)
                elif stream_state.turn_context_tokens == 0:
                    stream_state.turn_context_tokens = inp_toks + out_toks

                if self._app:
                    setattr(self._app, "_context_tokens", stream_state.turn_context_tokens)

                total_delta = delta_inp + delta_out
                if self._app:
                    current_cum = getattr(self._app, "_cumulative_session_tokens", 0) or 0
                    if not stream_state.received_session_tokens:
                        new_cum = current_cum + total_delta
                        setattr(self._app, "_cumulative_session_tokens", new_cum)

                # Estimate cost for this delta so the status bar updates live
                from opscloud.middleware.cost_tracking import estimate_cost

                model_name = ""
                provider = ""
                model_spec = getattr(self._app, "_model", "") if self._app else ""
                if model_spec:
                    if ":" in model_spec:
                        provider, model_name = model_spec.split(":", 1)
                    else:
                        model_name = model_spec

                resp_meta = getattr(msg_obj, "response_metadata", {}) or {}
                if isinstance(resp_meta, dict):
                    model_name = resp_meta.get("model_name") or resp_meta.get("model") or model_name

                cost_delta = estimate_cost(
                    {"input_tokens": delta_inp, "output_tokens": delta_out},
                    model_name,
                    provider,
                )
                if cost_delta is not None and cost_delta > 0:
                    self._stats.total_cost_usd += cost_delta
                    if not stream_state.received_session_tokens and self._app:
                        cur_cost = getattr(self._app, "_session_cost_usd", 0.0) or 0.0
                        new_cost = cur_cost + cost_delta
                        setattr(self._app, "_session_cost_usd", new_cost)
                        if self._status_bar is not None:
                            self._status_bar.set_cost(new_cost)

                if self._status_bar is not None:
                    cumulative = getattr(self._app, "_cumulative_session_tokens", 0) if self._app else 0
                    display_tokens = cumulative or stream_state.turn_context_tokens
                    self._status_bar.set_tokens(display_tokens, context_tokens=stream_state.turn_context_tokens)

        # The Auto mode authorization classifier is a nested model
        # call. Its structured JSON is internal policy machinery,
        # not assistant output for the conversation transcript.
        if _is_auto_mode_classifier_chunk(meta):
            return

        # 2. Tool result update
        if isinstance(msg_obj, ToolMessage) or msg_obj.__class__.__name__ == "ToolMessage":
            call_id = getattr(msg_obj, "tool_call_id", "") or ""
            raw_name = getattr(msg_obj, "name", "") or ""
            tool_name = (
                raw_name
                if (raw_name and raw_name != "None")
                else self._active_tools_map.get(call_id, "tool")
            )
            content = str(getattr(msg_obj, "content", ""))
            if self._messages is not None and is_main_agent:
                self._messages.update_tool_result(call_id=call_id, result=content, name=tool_name)

            if not is_main_agent:
                panel = getattr(self._app, "_get_subagent_panel", lambda: None)() if self._app else None
                if panel is not None:
                    panel.set_subagent_activity(None, "Thinking")
                has_panel = panel is not None and getattr(panel, "is_attached", False)
                if self._set_spinner and not has_panel:
                    try:
                        await self._set_spinner("Subagent: Thinking")
                    except Exception:
                        pass
            elif self._set_spinner:
                try:
                    await self._set_spinner("Thinking")
                except Exception:
                    pass

        # 3. Human message flush
        elif isinstance(msg_obj, HumanMessage):
            pending_text = stream_state.pending_text_by_namespace.get(ns_key, "")
            if pending_text and self._messages is not None and is_main_agent:
                self._messages.finish_assistant_message()
            stream_state.pending_text_by_namespace[ns_key] = ""
            stream_state.assistant_message_by_namespace.pop(ns_key, None)

        # 4. AIMessage / AIMessageChunk
        else:
            text, thinking = _extract_text_and_thinking(
                getattr(msg_obj, "content", ""),
                getattr(msg_obj, "additional_kwargs", None),
                getattr(msg_obj, "response_metadata", None),
                msg_obj=msg_obj,
            )

            if thinking:
                if stream_state.thinking_start_t is None:
                    stream_state.thinking_start_t = time.time()
                thinking_dur = time.time() - stream_state.thinking_start_t
                if self._messages is not None and is_main_agent:
                    self._messages.append_thinking_token(thinking, duration_seconds=thinking_dur)
                if not is_main_agent:
                    panel = getattr(self._app, "_get_subagent_panel", lambda: None)() if self._app else None
                    if panel is not None:
                        panel.set_subagent_activity(None, "Thinking")
                    has_panel = panel is not None and getattr(panel, "is_attached", False)
                    if self._set_spinner and not has_panel:
                        try:
                            await self._set_spinner("Subagent: Thinking")
                        except Exception:
                            pass
                elif self._set_spinner:
                    try:
                        await self._set_spinner("Thinking")
                    except Exception:
                        pass

            if text:
                pending_text = stream_state.pending_text_by_namespace.get(ns_key, "")
                pending_text += text
                stream_state.pending_text_by_namespace[ns_key] = pending_text
                if ns_key not in stream_state.assistant_message_by_namespace:
                    stream_state.assistant_message_by_namespace[ns_key] = True
                    if self._set_spinner:
                        try:
                            await self._set_spinner(None)
                        except Exception:
                            pass

                if self._messages is not None and is_main_agent:
                    self._messages.append_assistant_token(text)

            # Process tool call chunks
            tool_chunks = getattr(msg_obj, "tool_call_chunks", []) or []
            if not tool_chunks and hasattr(msg_obj, "content_blocks"):
                tool_chunks = [
                    b
                    for b in msg_obj.content_blocks
                    if isinstance(b, dict) and b.get("type") in {"tool_call_chunk", "tool_call"}
                ]
            for block in tool_chunks:
                chunk_name = block.get("name")
                chunk_args = block.get("args")
                chunk_id = block.get("id")
                chunk_index = block.get("index")

                buffer_key = tool_call_buffer_key(chunk_index, chunk_id, len(stream_state.tool_call_buffers))
                buffer = stream_state.tool_call_buffers.setdefault(buffer_key, ToolCallBuffer())
                buffer.ingest(name=chunk_name, tool_id=chunk_id, args=chunk_args)

                buffer_name = buffer.name
                buffer_id = buffer.tool_id
                if buffer_name is None:
                    continue

                parsed_args = buffer.parse_args()
                if parsed_args is None:
                    continue

                if self._messages is not None and is_main_agent:
                    self._messages.finish_assistant_message()
                stream_state.pending_text_by_namespace.pop(ns_key, None)
                stream_state.assistant_message_by_namespace.pop(ns_key, None)

                if buffer_id is not None and buffer_id not in stream_state.displayed_tool_ids:
                    stream_state.displayed_tool_ids.add(buffer_id)
                    if not is_main_agent:
                        panel = getattr(self._app, "_get_subagent_panel", lambda: None)() if self._app else None
                        if panel is not None:
                            panel.set_subagent_activity(None, buffer_name)
                        has_panel = panel is not None and getattr(panel, "is_attached", False)
                        if self._set_spinner and not has_panel:
                            try:
                                await self._set_spinner(f"Subagent: {buffer_name}")
                            except Exception:
                                pass
                    else:
                        if self._set_spinner:
                            try:
                                await self._set_spinner("Thinking")
                            except Exception:
                                pass

                        if self._messages is not None and is_main_agent:
                            self._messages.add_tool_call(name=buffer_name, call_id=buffer_id, args=parsed_args)
                            self._active_tools_map[buffer_id] = buffer_name

    async def _resolve_pending_interrupts(
        self,
        pending_interrupts: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve pending HITL and ask_user interrupts and return the Command resume dict."""
        resume_payload: dict[str, Any] = {}
        req_approval_fn = self._effective_request_approval
        req_ask_user_fn = self._effective_request_ask_user

        for int_id, int_val in list(pending_interrupts.items()):
            if not isinstance(int_val, dict):
                continue

            # ── 1. Handle `ask_user` Interactive Question Prompts ───────────
            if int_val.get("type") == "ask_user" or "questions" in int_val:
                questions = int_val.get("questions", [])

                if req_ask_user_fn is not None:
                    try:
                        if self._set_spinner:
                            try:
                                await self._set_spinner(None)
                            except Exception:
                                pass

                        res = req_ask_user_fn(questions)
                        future = await res if inspect.isawaitable(res) else res
                        widget_result = (
                            await future
                            if (inspect.isawaitable(future) or isinstance(future, asyncio.Future))
                            else future
                        )

                        if isinstance(widget_result, dict):
                            result_type = widget_result.get("type")
                            if result_type == "answered":
                                answers = widget_result.get("answers", [])
                                resume_payload[int_id] = {
                                    "status": "answered",
                                    "answers": answers if isinstance(answers, list) else [str(answers)],
                                }
                            elif result_type == "cancelled":
                                resume_payload[int_id] = {
                                    "status": "cancelled",
                                    "answers": ["(cancelled)" for _ in questions],
                                }
                            else:
                                resume_payload[int_id] = {
                                    "status": "error",
                                    "error": str(widget_result.get("error", "invalid widget response")),
                                    "answers": [],
                                }
                        else:
                            resume_payload[int_id] = {
                                "status": "error",
                                "error": "ask_user future returned invalid non-dict result",
                                "answers": [],
                            }
                    except Exception as exc:
                        logger.exception("Failed to resolve ask_user prompt: %s", exc)
                        resume_payload[int_id] = {
                            "status": "error",
                            "error": f"Failed to display ask_user prompt: {exc}",
                            "answers": [],
                        }
                else:
                    # Headless or non-interactive fallback
                    resume_payload[int_id] = {
                        "status": "cancelled",
                        "error": "ask_user prompt not supported in non-interactive UI adapter",
                        "answers": ["(cancelled)" for _ in questions],
                    }
                continue

            # ── 2. Handle Tool Action Approvals (HITL) ───────────────────────
            action_requests = int_val.get("action_requests", [])
            if not action_requests:
                continue

            if self._auto_approve:
                decisions = [{"type": "approve"} for _ in action_requests]
                resume_payload[int_id] = {"decisions": decisions}
                continue

            if req_approval_fn is not None:
                res = req_approval_fn(action_requests, self._assistant_id)
                future = await res if inspect.isawaitable(res) else res
                decision = (
                    await future
                    if (inspect.isawaitable(future) or isinstance(future, asyncio.Future))
                    else future
                )

                if (
                    isinstance(decision, dict)
                    and decision.get("type") in {"smart_approve_all", "auto_approve_all"}
                ):
                    callback = getattr(self._app, "_on_smart_approve_enabled", None) or getattr(
                        self._app, "_on_auto_approve_enabled", None
                    )
                    if callback is not None:
                        callback_result = callback()
                        enabled = (
                            await callback_result
                            if inspect.isawaitable(callback_result)
                            else callback_result
                        )
                        if enabled is None:
                            enabled = True
                        if enabled is False:
                            continue

                if isinstance(decision, dict):
                    decision_type = decision.get("type")
                    if decision_type in {"approve", "auto_approve_all", "smart_approve_all"}:
                        decisions = [{"type": "approve"} for _ in action_requests]
                    elif decision_type == "switch_manual":
                        decisions = [{"type": "reject"} for _ in action_requests]
                    elif decision_type == "reject":
                        reject_msg = decision.get("message")
                        rej_dict: dict[str, Any] = {"type": "reject"}
                        if reject_msg:
                            rej_dict["message"] = reject_msg
                        decisions = [rej_dict for _ in action_requests]
                    else:
                        decisions = [{"type": "approve"} for _ in action_requests]
                else:
                    decisions = [{"type": "approve"} for _ in action_requests]
                resume_payload[int_id] = {"decisions": decisions}
            else:
                # Backwards-compatible fallback
                call_ids = [f"{int_id}_{i}" for i in range(len(action_requests))]
                for i, req in enumerate(action_requests):
                    if self._app:
                        self._app.post_message(
                            self.InterruptRaised(
                                tool_name=req.get("action") or req.get("name", "unknown"),
                                call_id=call_ids[i],
                                args=req.get("args", {}),
                            )
                        )
                if call_ids:
                    approved_results = await asyncio.gather(*(self._await_approval(cid) for cid in call_ids))
                    decisions = [{"type": "approve" if approved else "reject"} for approved in approved_results]
                else:
                    decisions = []
                resume_payload[int_id] = {"decisions": decisions}

        return resume_payload

    def _finalize_turn_success(
        self,
        start_t: float,
        turn_context_tokens: int,
        stream_state: _TurnStreamState | None = None,
    ) -> None:
        """Update session stats and post StreamFinished message."""
        self._stats.request_count += 1
        if stream_state and not stream_state.received_session_tokens and self._app and turn_context_tokens > 0:
            new_cumulative = stream_state.start_session_tokens + turn_context_tokens
            setattr(self._app, "_cumulative_session_tokens", new_cumulative)
        if self._app and turn_context_tokens > 0:
            setattr(self._app, "_context_tokens", turn_context_tokens)
        if self._status_bar is not None:
            cumulative = getattr(self._app, "_cumulative_session_tokens", 0) if self._app else 0
            display_tokens = cumulative or turn_context_tokens or getattr(self._app, "_context_tokens", 0)
            if display_tokens > 0:
                self._status_bar.set_tokens(display_tokens, context_tokens=turn_context_tokens)
        if self._app:
            self._app.post_message(self.StreamFinished(self._stats))

    def _finalize_turn_error(self, error: Exception) -> None:
        """Handle turn exception: finish assistant message, mount error, and post StreamError."""
        logger.exception("TUI: stream_turn Exception: %s", error)
        if self._messages is not None:
            self._messages.finish_assistant_message()
            from opscloud.ui.widgets.messages import ErrorMessage

            self._messages.mount(ErrorMessage(f"Agent execution failed: {error}"))
        if self._app:
            self._app.post_message(self.StreamError(str(error)))

    async def _finalize_turn_cleanup(self, start_t: float, turn_context_tokens: int) -> None:
        """Final cleanup logic executed in the finally block."""
        self._stats.wall_time_seconds += time.time() - start_t
        # Dismiss the spinner and clear the status bar on turn end.
        if self._set_spinner:
            try:
                await self._set_spinner(None)
            except Exception:
                pass
        if self._status_bar is not None:
            self._status_bar.set_status("")
        logger.debug("TUI: stream_turn finally complete")

    # ── Public Stream API ─────────────────────────────────

    async def stream_turn(
        self,
        prompt: str,
        *,
        thread_id: str,
        context: dict[str, Any] | None = None,
        graph_input: dict[str, Any] | None = None,
    ) -> None:
        """Run one agent turn, routing stream events to Textual widgets."""
        from langgraph.types import Command

        # 1. Prepare input and configuration
        input_msg, turn_id = self._prepare_stream_input(prompt, graph_input)
        config, enriched_context = await self._prepare_stream_config(thread_id, turn_id, context)

        start_t = time.time()
        self._stats.input_tokens += max(1, len(prompt) // 4)
        if self._status_bar is not None:
            self._status_bar.show_pending_tokens()

        stream_state = _TurnStreamState()
        stream_state.start_session_tokens = (
            getattr(self._app, "_cumulative_session_tokens", 0) if self._app else 0
        )
        stream_input: Any = input_msg
        self._current_thread_id = thread_id

        try:
            while True:
                pending_interrupts: dict[str, Any] = {}

                if self._set_spinner and not getattr(self, "_current_tool_messages", {}):
                    await self._set_spinner("Thinking")

                async for chunk in self._client.astream(
                    stream_input,
                    stream_mode=["messages", "updates", "custom"],
                    subgraphs=True,
                    config=config,
                    context=enriched_context,
                ):
                    if self._cancel_event.is_set():
                        logger.debug("TUI: cancel event is set, breaking stream loop")
                        break

                    if not isinstance(chunk, tuple) or len(chunk) != 3:
                        continue

                    namespace, current_stream_mode, data = chunk
                    ns_key = tuple(namespace) if namespace else ()
                    is_main_agent = ns_key == ()

                    if current_stream_mode == "custom":
                        self._handle_custom_stream_event(data, is_main_agent, stream_state=stream_state)
                    elif current_stream_mode == "updates":
                        self._handle_updates_stream_event(data, pending_interrupts)
                    elif current_stream_mode == "messages":
                        await self._handle_messages_stream_event(
                            data, ns_key, is_main_agent, stream_state
                        )

                if self._cancel_event.is_set():
                    break

                if self._messages is not None:
                    self._messages.finish_assistant_message()
                    regroup_fn = getattr(self._messages, "regroup_completed_tools", None)
                    if callable(regroup_fn):
                        res = regroup_fn()
                        if inspect.isawaitable(res):
                            await res

                if pending_interrupts:
                    resume_payload = await self._resolve_pending_interrupts(pending_interrupts)
                    stream_input = Command(resume=resume_payload)
                    continue
                else:
                    break

            self._finalize_turn_success(
                start_t,
                stream_state.turn_context_tokens,
                stream_state=stream_state,
            )

        except asyncio.CancelledError:
            logger.debug("TUI: stream_turn CancelledError")
            if self._messages is not None:
                self._messages.finish_assistant_message()
            raise
        except Exception as e:
            self._finalize_turn_error(e)
            raise
        finally:
            self._current_thread_id = None
            await self._finalize_turn_cleanup(start_t, stream_state.turn_context_tokens)


    def cancel(self) -> None:
        """Cancel the current streaming turn and notify the server."""
        self._cancel_event.set()
        if self._client and hasattr(self._client, "acancel_active_runs"):
            tid = getattr(self, "_current_thread_id", None)
            if tid:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._client.acancel_active_runs({"configurable": {"thread_id": tid}})
                    )
                except RuntimeError:
                    pass

    def submit_approval(self, call_id: str, approved: bool) -> None:
        """Submit HITL approval response from UI thread."""
        self._approval_responses[call_id] = approved
        event = self._approval_events.setdefault(call_id, asyncio.Event())
        event.set()

    async def _await_approval(self, call_id: str) -> bool:
        """Pause stream loop until approval event resolves."""
        if call_id in self._approval_responses:
            return self._approval_responses.pop(call_id)
        evt = self._approval_events.setdefault(call_id, asyncio.Event())
        await evt.wait()
        self._approval_events.pop(call_id, None)
        return self._approval_responses.pop(call_id, False)
