"""Thread history, fork/resume, compaction, and selector controller extracted from `OpsCloudApp`."""

from __future__ import annotations

import math
import re
import uuid
from typing import TYPE_CHECKING, Any

from textual.css.query import NoMatches

from opscloud.ui.widgets.messages import (
    AssistantMessage,
    MessageList,
    SkillMessage,
    SystemMessage,
    ToolCallMessage,
    UserMessage,
)
from opscloud.utils.logger import bind_logging_to_thread, get_logger

if TYPE_CHECKING:
    from opscloud.ui.app import OpsCloudApp

logger = get_logger(__name__)


class ThreadController:
    """Manages thread history rehydration, resume, fork, compaction, and selector modal."""

    def __init__(self, app: OpsCloudApp) -> None:
        self._app = app

    async def load_thread_history(self, thread_id: str) -> None:
        """Fetch and mount stored message history for a thread."""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from opscloud.middleware.goal_state_notice import is_internal_message

        app = self._app
        app._reset_thread_usage(0.0, 0)
        state_values = await app._get_thread_state_values(thread_id)
        raw_messages = state_values.get("messages", [])
        payload = app._goal_rubric_payload_from_state(state_values, raw_messages)
        app._restore_goal_rubric_state(payload)
        if not raw_messages:
            return

        if any(isinstance(m, dict) for m in raw_messages):
            from langchain_core.messages.utils import convert_to_messages
            raw_messages = convert_to_messages(raw_messages)

        pending_tools: dict[str, dict[str, Any]] = {}

        try:
            messages_container = app.query_one("#messages", MessageList)
        except NoMatches:
            return

        restored_goal_prompt = False

        for idx, msg in enumerate(raw_messages):
            msg_type = getattr(msg, "type", type(msg).__name__.lower().replace("message", ""))
            content_raw = getattr(msg, "content", None)
            is_internal = is_internal_message(msg)
            msg_name = getattr(msg, "name", None) or ""

            logger.info(
                "TUI: _load_thread_history msg[%d/%d] type=%s cls=%s name=%s is_internal=%s content=%r",
                idx,
                len(raw_messages),
                msg_type,
                type(msg).__name__,
                msg_name,
                is_internal,
                str(content_raw)[:80] if content_raw else "",
            )

            # Gate 1: skip named sub-graph messages
            if msg_name in {"rubric_grader", "goal_criteria"}:
                continue

            # Gate 2: skip LangChain system messages
            if msg_type == "system":
                continue

            # Normalise content → plain string
            if isinstance(content_raw, list):
                parts = []
                for block in content_raw:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "".join(parts).strip()
            elif content_raw is not None:
                text = str(content_raw).strip()
            else:
                text = ""

            if isinstance(msg, HumanMessage) or msg_type == "human":
                if text.startswith("[SYSTEM]") or is_internal:
                    if not restored_goal_prompt and idx <= 3:
                        goal_obj = payload.get("goal_objective") or state_values.get("_goal_objective")
                        if goal_obj and isinstance(goal_obj, str) and goal_obj.strip():
                            text = f"/goal {goal_obj.strip()}"
                            restored_goal_prompt = True
                            logger.info("TUI: Restored goal objective prompt for msg[%d]: %r", idx, text)
                        else:
                            logger.info("TUI: Skipping internal human msg[%d] (no goal objective found)", idx)
                            continue
                    else:
                        logger.info("TUI: Skipping internal human msg[%d]", idx)
                        continue
                elif is_internal:
                    continue

                if not text:
                    continue

                if text.startswith("This is grader iteration "):
                    continue

                if text.startswith("System Prompt"):
                    continue

                # ── Extract user intent from goal <operation> XML ─────
                if text.startswith("<operation>"):
                    if text.startswith("<operation>draft</operation>"):
                        match = re.search(r"<goal>\s*(.*?)\s*</goal>", text, re.DOTALL)
                        if match:
                            text = f"/goal {match.group(1).strip()}"
                        else:
                            continue
                    elif text.startswith("<operation>amend</operation>"):
                        match = re.search(r"<user_feedback>\s*(.*?)\s*</user_feedback>", text, re.DOTALL)
                        if match:
                            text = f"/goal amend {match.group(1).strip()}"
                        else:
                            text = "/goal amend"
                    else:
                        continue

                # ── Rehydrate skill invocations as SkillMessage ────────
                add_kwargs = getattr(msg, "additional_kwargs", None) or {}
                skill_meta = add_kwargs.get("__skill") if isinstance(add_kwargs, dict) else None
                if isinstance(skill_meta, dict):
                    s_name = str(skill_meta.get("name") or skill_meta.get("slash_command", "").lstrip("/"))
                    s_desc = str(skill_meta.get("description") or "")
                    s_source = str(skill_meta.get("source") or "built-in")
                    s_args = str(skill_meta.get("args") or "")
                    s_body = str(skill_meta.get("body") or "")
                    if not s_body:
                        scope_match = re.search(
                            r"<SKILL>.*?Here are its contents:\n(.*?)</SKILL>",
                            text,
                            re.DOTALL,
                        )
                        s_body = scope_match.group(1) if scope_match else text
                    await app._mount_message(
                        SkillMessage(
                            skill_name=s_name,
                            description=s_desc,
                            source=s_source,
                            body=s_body,
                            args=s_args,
                        )
                    )
                    slash_cmd = str(skill_meta.get("slash_command") or f"/{s_name}").strip()
                    hist_entry = f"{slash_cmd} {s_args}".strip() if s_args else slash_cmd
                    if app._chat_input is not None and hasattr(app._chat_input, "add_history_entry"):
                        app._chat_input.add_history_entry(hist_entry, persist=False)
                    continue

                skill_env_match = re.search(
                    r"<SKILL>The user has explicitly invoked the \(([^)]+)\) skill\..*?Here are its contents:\n(.*?)</SKILL>",
                    text,
                    re.DOTALL,
                )
                if skill_env_match:
                    s_name = skill_env_match.group(1).strip()
                    s_body = skill_env_match.group(2)
                    req_match = re.search(r"\*\*User request:\*\*\s*(.+)$", text, re.DOTALL)
                    s_args = req_match.group(1).strip() if req_match else ""
                    await app._mount_message(
                        SkillMessage(
                            skill_name=s_name,
                            body=s_body,
                            args=s_args,
                        )
                    )
                    hist_entry = f"/{s_name} {s_args}".strip() if s_args else f"/{s_name}"
                    if app._chat_input is not None and hasattr(app._chat_input, "add_history_entry"):
                        app._chat_input.add_history_entry(hist_entry, persist=False)
                    continue

                await app._mount_message(UserMessage(text))
                if app._chat_input is not None and hasattr(app._chat_input, "add_history_entry"):
                    app._chat_input.add_history_entry(text, persist=False)

            elif isinstance(msg, AIMessage) or msg_type == "ai":
                from opscloud.ui.textual_adapter import _extract_text_and_thinking
                ai_text, thinking = _extract_text_and_thinking(
                    getattr(msg, "content", ""),
                    getattr(msg, "additional_kwargs", None),
                    getattr(msg, "response_metadata", None),
                    msg_obj=msg,
                )
                if ai_text or thinking:
                    if thinking:
                        messages_container.add_thinking_message(thinking)
                    if ai_text:
                        assistant_msg = AssistantMessage(ai_text)
                        await app._mount_message(assistant_msg)

                for tc in getattr(msg, "tool_calls", []) or []:
                    if not isinstance(tc, dict):
                        logger.debug(
                            "Skipping malformed tool_call entry (type=%s) "
                            "during history load for thread %s",
                            type(tc).__name__, thread_id,
                        )
                        continue
                    tc_id = tc.get("id", "")
                    name = tc.get("name") or "tool"
                    args = tc.get("args") or {}
                    if not isinstance(args, dict):
                        args = {}
                    messages_container.add_tool_call(
                        name=name, call_id=tc_id, args=args, live=False
                    )
                    if tc_id:
                        pending_tools[tc_id] = {"name": name}

            elif isinstance(msg, ToolMessage) or msg_type == "tool":
                tc_id = getattr(msg, "tool_call_id", "") or ""
                status = getattr(msg, "status", "success")
                tool_content = text or ""
                success = status != "error"

                if tc_id and tc_id in pending_tools:
                    pending_tools.pop(tc_id)
                    messages_container.update_tool_result(
                        call_id=tc_id,
                        result=tool_content,
                        success=success,
                        live=False,
                    )
                else:
                    tool_name = getattr(msg, "name", None) or "tool"
                    messages_container.update_tool_result(
                        call_id=tc_id or "",
                        result=tool_content,
                        success=success,
                        name=tool_name,
                        live=False,
                    )
            else:
                logger.debug(
                    "Skipping unsupported message type %s during history load",
                    type(msg).__name__,
                )

        # Mark unmatched tool calls as rejected
        for tc_id in pending_tools:
            if tc_id in messages_container._tool_calls:
                messages_container._tool_calls[tc_id].set_rejected()

        # Restore context tokens and cumulative estimated cost from message history
        restored_cost = 0.0
        restored_tokens = 0
        model_spec = getattr(app, "_model", "") or ""
        if ":" in model_spec:
            provider_part, model_part = model_spec.split(":", 1)
        else:
            model_part = model_spec
            provider_part = getattr(getattr(app, "_settings", None), "model_provider", "") or ""

        from opscloud.utils.cost_estimation import estimate_cost
        for msg in raw_messages:
            usage_meta = (
                getattr(msg, "usage_metadata", None)
                or (getattr(msg, "response_metadata", None) or {}).get("usage")
                or (getattr(msg, "response_metadata", None) or {}).get("token_usage")
            )
            if isinstance(usage_meta, dict):
                inp = usage_meta.get("input_tokens", 0) or 0
                out = usage_meta.get("output_tokens", 0) or 0
                if inp or out:
                    restored_tokens += inp + out
                if model_part:
                    m_name = (getattr(msg, "response_metadata", {}) or {}).get("model_name") or model_part
                    c = estimate_cost(m_name, provider_part, inp, out)
                    if c is not None and c > 0:
                        restored_cost += c

        checkpoint_cost = state_values.get("_session_cost_usd")
        if isinstance(checkpoint_cost, (int, float)) and math.isfinite(checkpoint_cost) and checkpoint_cost > 0:
            restored_cost = max(restored_cost, float(checkpoint_cost))

        checkpoint_tokens = state_values.get("_session_total_tokens")
        if isinstance(checkpoint_tokens, int) and checkpoint_tokens > 0:
            restored_tokens = max(restored_tokens, checkpoint_tokens)

        context_tokens = state_values.get("_context_tokens")
        if isinstance(context_tokens, int) and context_tokens > 0:
            restored_context_tokens = context_tokens
        else:
            last_ai_tokens = 0
            for msg in reversed(raw_messages):
                if getattr(msg, "type", "") == "ai" or msg.__class__.__name__ == "AIMessage":
                    u = getattr(msg, "usage_metadata", None) or {}
                    inp = u.get("input_tokens", 0) or 0
                    out = u.get("output_tokens", 0) or 0
                    if inp or out:
                        last_ai_tokens = inp + out
                        break
            restored_context_tokens = last_ai_tokens or restored_tokens

        app._reset_thread_usage(
            cost_usd=restored_cost,
            context_tokens=restored_context_tokens,
            cumulative_tokens=restored_tokens,
        )

        await app._regroup_completed_tools()

        logger.info(
            "TUI: _load_thread_history completed for thread %s (raw_messages=%d, mounted_widgets=%d, tokens=%d, cost=$%.4f)",
            thread_id,
            len(raw_messages),
            len(messages_container.children),
            app._context_tokens,
            app._session_cost_usd,
        )

        try:
            messages_container._auto_scroll_locked = True
            messages_container._scroll_to_end()
        except Exception:
            pass

    def close_active_tool_group(self) -> None:
        """Finalize the open tool group into its collapsed past-tense form."""
        try:
            messages = self._app.query_one("#messages", MessageList)
            messages.close_active_tool_group()
        except Exception:
            logger.exception("Failed to close active tool group")

    async def regroup_completed_tools(self) -> None:
        """Fold runs of completed tool calls into collapsible group summaries."""
        try:
            messages = self._app.query_one("#messages", MessageList)
            await messages.regroup_completed_tools()
        except Exception:
            logger.exception("Failed to regroup completed tools")

    async def resume_thread(self, thread_id: str, *, is_new: bool = False) -> None:
        """Resume conversation from specified checkpoint thread ID and restore history."""
        app = self._app
        app._agent_thread_id = thread_id
        bind_logging_to_thread(thread_id)
        session_state = getattr(app, "_session_state", None)
        if session_state:
            session_state.thread_id = thread_id
        app._pending_messages.clear()
        app._queued_widgets.clear()
        app._reset_ephemeral_mode_states()
        try:
            messages = app.query_one("#messages", MessageList)
            messages.clear()
        except NoMatches:
            pass

        if is_new:
            app._restore_goal_rubric_state({})
            app._reset_thread_usage(0.0, 0)
        else:
            await app._load_thread_history(thread_id)
            if hasattr(app, "_sync_goal_state_from_checkpoint"):
                await app._sync_goal_state_from_checkpoint(force=True)
            if hasattr(app, "_sync_agent_session_state_from_checkpoint"):
                await app._sync_agent_session_state_from_checkpoint()

        label = "Created new thread" if is_new else "Resumed thread"
        await app._mount_message(
            SystemMessage(f"{label}: `{thread_id}`")
        )

    async def fork_thread(self, *, source_thread_id: str | None = None) -> tuple[str, str]:
        """Fork the active conversation thread into a new branch and switch to it in-place."""
        from opscloud.middleware.goal_state_notice import build_fork_notice_message
        from opscloud.state.session import SessionManager

        app = self._app
        prev_id = (source_thread_id or app._agent_thread_id or "").strip()
        if not prev_id:
            raise ValueError("No active conversation to fork.")

        client = getattr(app, "_client", None) or getattr(app, "_agent", None)
        if client is not None and callable(getattr(client, "afork_thread", None)):
            new_thread_id = str(await client.afork_thread(prev_id))
        else:
            new_thread_id = await SessionManager.fork_thread(prev_id)
            if client is not None and callable(getattr(client, "aupdate_state", None)):
                notice_msg = build_fork_notice_message(
                    source_thread_id=prev_id,
                    new_thread_id=new_thread_id,
                )
                try:
                    await client.aupdate_state(
                        {"configurable": {"thread_id": new_thread_id}},
                        {"messages": [notice_msg]},
                    )
                except Exception:
                    logger.debug("Could not append <fork_notice> via client.aupdate_state", exc_info=True)

        app._previous_thread_id = prev_id
        app._agent_thread_id = new_thread_id
        bind_logging_to_thread(new_thread_id)

        session_state = getattr(app, "_session_state", None)
        if session_state is not None:
            try:
                session_state.previous_thread_id = prev_id
                session_state.thread_id = new_thread_id
            except Exception:
                pass

        for state_attr in ("_goal_state", "_agent_session_state"):
            st = getattr(app, state_attr, None)
            if st is not None and hasattr(st, "thread_id"):
                try:
                    st.thread_id = new_thread_id
                except Exception:
                    pass

        return prev_id, new_thread_id

    async def invoke_compact_conversation(self, thread_id: str | None = None, force: bool = True) -> None:
        """Trigger server-side conversation summarization/compaction."""
        from opscloud.commands._base import CommandContext
        from opscloud.commands.core.compact import CompactHandler

        app = self._app
        handler = CompactHandler()
        ctx = CommandContext(
            raw_command="/compact",
            args="",
            app=app,
            agent=getattr(app, "_agent_graph", None),
        )
        await handler.execute(ctx)

    def toggle_timestamps(self, visible: bool | None = None) -> bool:
        """Toggle message timestamps visibility across chat messages."""
        app = self._app
        if visible is not None:
            app._message_timestamps_visible = visible
        else:
            app._message_timestamps_visible = not getattr(app, "_message_timestamps_visible", False)
        try:
            messages = app.query_one("#messages", MessageList)
            messages.set_timestamps_visible(app._message_timestamps_visible)
        except Exception:
            pass
        return app._message_timestamps_visible

    def get_context_tokens(self) -> int:
        """Get active context tokens for the current turn/session."""
        app = self._app
        adapter_tokens = 0
        if app._adapter and hasattr(app._adapter, "stats") and app._adapter.stats:
            adapter_tokens = getattr(app._adapter.stats, "input_tokens", 0) + getattr(app._adapter.stats, "output_tokens", 0)
        return getattr(app, "_context_tokens", 0) or adapter_tokens

    async def get_conversation_token_count(self) -> int | None:
        """Get approximate token count of conversation message history."""
        app = self._app
        try:
            msgs = None
            if hasattr(app, "_get_thread_state_values"):
                try:
                    state_values = await app._get_thread_state_values()
                    msgs = state_values.get("messages")
                except Exception:
                    pass
            if not msgs:
                msgs = app.get_thread_messages()
            if not msgs:
                return None
            if any(isinstance(m, dict) for m in msgs):
                from langchain_core.messages.utils import convert_to_messages
                msgs = convert_to_messages(msgs)
            from langchain_core.messages.utils import count_tokens_approximately
            return count_tokens_approximately(msgs)
        except Exception:
            return None

    async def has_conversation_messages(self) -> bool:
        """Check if the active session has conversation messages."""
        app = self._app
        try:
            container = app.query_one("#messages", MessageList)
            for child in container.children:
                if isinstance(child, (UserMessage, AssistantMessage)):
                    return True
        except Exception:
            pass

        try:
            msgs = app.get_thread_messages()
            if msgs:
                return True
        except Exception:
            pass

        return False

    def get_thread_messages(self) -> list[Any]:
        """Fetch current thread messages from UI widgets or adapter."""
        app = self._app
        msgs: list[Any] = []
        try:
            from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
            container = app.query_one("#messages", MessageList)
            for child in container.children:
                if isinstance(child, UserMessage):
                    txt = getattr(child, "_raw_content", "") or ""
                    if txt:
                        msgs.append(HumanMessage(content=txt))
                elif isinstance(child, AssistantMessage):
                    txt = getattr(child, "content_text", None) or "".join(getattr(child, "_fragments", []))
                    if txt:
                        msgs.append(AIMessage(content=txt))
                elif isinstance(child, ToolCallMessage):
                    res = getattr(child, "_result", "") or ""
                    call_id = getattr(child, "_call_id", "") or "call_1"
                    if res:
                        msgs.append(ToolMessage(content=str(res), tool_call_id=call_id))
        except Exception:
            pass
        return msgs

    async def show_thread_selector(self) -> None:
        """Open the interactive thread selector modal screen."""
        from opscloud.state.session import SessionManager, get_db_path
        from opscloud.ui.widgets.thread_selector import ThreadSelectorScreen

        app = self._app
        threads = []
        sm = None
        session_state = getattr(app, "_session_state", None)
        if session_state and hasattr(session_state, "session_manager"):
            sm = session_state.session_manager
        else:
            db_path = get_db_path()
            if db_path.exists():
                sm = SessionManager(db_path)

        if sm is not None:
            try:
                raw_threads = await sm.list_threads(limit=50)
                threads = [
                    {
                        "thread_id": t.get("thread_id"),
                        "created_at": t.get("created_at"),
                        "updated_at": t.get("updated_at"),
                        "message_count": t.get("message_count", 0),
                        "title": t.get("title"),
                        "initial_prompt": t.get("initial_prompt", ""),
                    }
                    for t in raw_threads
                ]
            except Exception as exc:
                logger.debug("Failed fetching threads for selector: %s", exc)

        screen = ThreadSelectorScreen(
            threads=threads,
            current_thread_id=app._agent_thread_id,
        )

        def _on_thread_selected(selected_id: str | None) -> None:
            if selected_id == "__new__":
                new_id = str(uuid.uuid4())
                app.run_worker(app.resume_thread(new_id, is_new=True))
            elif selected_id:
                app.run_worker(app.resume_thread(selected_id, is_new=False))

        app.push_screen(screen, _on_thread_selected)

    async def get_recent_threads(self, limit: int = 20) -> list[Any]:
        """Get list of recent threads from session manager."""
        from opscloud.state.session import SessionManager, get_db_path

        app = self._app
        sm = None
        session_state = getattr(app, "_session_state", None)
        if session_state and hasattr(session_state, "session_manager"):
            sm = session_state.session_manager
        else:
            db_path = get_db_path()
            if db_path.exists():
                sm = SessionManager(db_path)

        if sm is not None:
            try:
                return await sm.list_threads(limit=limit)
            except Exception:
                pass
        return []
