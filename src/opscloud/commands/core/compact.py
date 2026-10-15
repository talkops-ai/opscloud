"""Compact and offload command handler for OpsCloud.

Implements on-demand ``/compact`` and ``/offload`` conversation compaction:
- Parses ``/compact [custom_instructions]`` via ``parse_compact_request``.
- Flushes all prior thread steps to dual line-aligned JSONL transcripts
  (``.system_generated/logs/transcript.jsonl`` and ``transcript_full.jsonl``)
  via ``sync_messages_to_transcript`` before removing any messages from
  LangGraph state, ensuring zero data loss.
- Enforces tool-call pairing and turn invariants on the kept window via
  ``remove_unpaired_tool_calls_and_results`` and
  ``ensure_not_starting_with_tool_call``.
- Generates a structured 6-section continuation ``<summary>...</summary>``
  (with ``ON-DEMAND COMPACTION`` instructions when provided) via the active LLM
  or deterministic ``generate_fallback_summary``, wrapped in the
  ``<CONTEXT_SUMMARY>`` envelope.
"""

from __future__ import annotations

import contextlib
from typing import Any

from langchain_core.messages import RemoveMessage, SystemMessage

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.middleware.compaction import (
    agenerate_llm_continuation_summary,
    build_context_summary_envelope,
    ensure_not_starting_with_tool_call,
    parse_compact_request,
    remove_unpaired_tool_calls_and_results,
    sync_messages_to_transcript,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


class CompactHandler(BaseCommandHandler):
    """Handler for /compact and /offload commands to summarize and free context space."""

    @property
    def name(self) -> str:
        return "/compact"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/offload",)

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    @staticmethod
    def _resolve_custom_instructions(ctx: CommandContext) -> str:
        if ctx.args and ctx.args.strip():
            return ctx.args.strip()
        if ctx.raw_command and ctx.raw_command.strip():
            _, instructions = parse_compact_request(ctx.raw_command)
            if instructions:
                return instructions
        return ""

    @staticmethod
    def _resolve_active_model(ctx: CommandContext) -> Any | None:
        if ctx.app is not None:
            for attr in ("_active_model", "_model", "model"):
                candidate = getattr(ctx.app, attr, None)
                if candidate is not None and hasattr(candidate, "ainvoke"):
                    return candidate
        if ctx.agent is not None:
            for attr in ("model", "_model"):
                candidate = getattr(ctx.agent, attr, None)
                if candidate is not None and hasattr(candidate, "ainvoke"):
                    return candidate
        return None

    async def execute(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None and hasattr(ctx.app, "_agent_running") and ctx.app._agent_running:
            return CommandResult(success=False, message="Cannot compact while agent is running.")

        messages: list[Any] = []
        thread_id: str | None = ctx.thread_id

        if ctx.app is not None:
            if not thread_id:
                thread_id = getattr(ctx.app, "_agent_thread_id", None)
            if hasattr(ctx.app, "get_thread_messages"):
                try:
                    messages = ctx.app.get_thread_messages()
                except Exception as exc:
                    logger.debug("Failed getting thread messages: %s", exc)

        if not messages and ctx.agent is not None and thread_id:
            try:
                state = await ctx.agent.aget_state({"configurable": {"thread_id": thread_id}})
                messages = state.values.get("messages", [])
            except Exception as exc:
                logger.debug("Failed fetching state from agent: %s", exc)

        from opscloud.middleware.btw import is_ephemeral_message

        messages = [m for m in messages if not is_ephemeral_message(m)]

        if len(messages) < 4:
            return CommandResult(
                success=False,
                message="Not enough messages to compact (minimum 4 messages required).",
            )

        effective_thread_id = thread_id or "default"
        custom_instructions = self._resolve_custom_instructions(ctx)

        # 1. Persist 100% of current messages to dual line-aligned JSONL transcripts
        #    (transcript.jsonl + transcript_full.jsonl) before removing any messages.
        compact_transcript_path, full_transcript_path = sync_messages_to_transcript(
            messages,
            thread_id=effective_thread_id,
        )

        before_tokens = self._count_tokens(messages)

        # 2. Separate messages into older history (to compact) and recent 2 turns (to keep),
        #    preserving tool-call pairing and valid turn boundaries on the kept tail.
        to_compact = messages[:-2]
        to_keep = ensure_not_starting_with_tool_call(
            remove_unpaired_tool_calls_and_results(messages[-2:])
        )
        if not to_keep:
            to_keep = messages[-2:]

        # 3. Generate structured 6-section <summary> (LLM with deterministic fallback)
        #    and wrap in the full <CONTEXT_SUMMARY> envelope.
        active_model = self._resolve_active_model(ctx)
        session_summary = await agenerate_llm_continuation_summary(
            active_model,
            to_compact,
            custom_instructions=custom_instructions,
        )
        envelope_content = build_context_summary_envelope(
            to_compact,
            session_summary,
            thread_id=effective_thread_id,
        )
        summary_msg = SystemMessage(content=envelope_content)

        new_messages: list[Any] = []
        if ctx.agent is not None and thread_id:
            try:
                removals = [RemoveMessage(id=m.id) for m in to_compact if hasattr(m, "id") and m.id]
                config = {"configurable": {"thread_id": thread_id}}
                await ctx.agent.aupdate_state(config, {"messages": removals + [summary_msg]})

                new_state = await ctx.agent.aget_state(config)
                new_messages = new_state.values.get("messages", [])
            except Exception as exc:
                logger.warning("Failed updating LangGraph checkpoint state during compaction: %s", exc)
                new_messages = [summary_msg] + list(to_keep)
        else:
            new_messages = [summary_msg] + list(to_keep)

        # Refresh UI messages if app is available
        if ctx.app is not None and thread_id and hasattr(ctx.app, "_load_thread_history"):
            with contextlib.suppress(Exception):
                await ctx.app._load_thread_history(thread_id)

        after_tokens = self._count_tokens(new_messages) if new_messages else max(50, int(before_tokens * 0.3))
        freed_tokens = max(0, before_tokens - after_tokens)

        msg = (
            f"**Conversation Compacted**\n"
            f"├ **Before:** ~{before_tokens:,} tokens ({len(messages)} messages)\n"
            f"├ **After:**  ~{after_tokens:,} tokens ({len(new_messages) if new_messages else 'compacted'} messages)\n"
            f"├ **Freed:**  ~{freed_tokens:,} tokens\n"
            f"└ **Transcript:** `{compact_transcript_path}`"
        )
        return CommandResult(
            success=True,
            message=msg,
            data={
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "freed_tokens": freed_tokens,
                "transcript_path": str(compact_transcript_path),
                "full_transcript_path": str(full_transcript_path),
                "context_summary": envelope_content,
            },
        )

    @staticmethod
    def _count_tokens(messages: list[Any]) -> int:
        try:
            from langchain_core.messages.utils import count_tokens_approximately

            return count_tokens_approximately(messages)
        except Exception:
            total_chars = sum(len(str(getattr(m, "content", m))) for m in messages)
            return total_chars // 4


__all__ = ["CompactHandler"]
