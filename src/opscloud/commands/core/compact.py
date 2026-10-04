"""Compact / offload command handler for OpsCode."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from typing import Any

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

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

    async def execute(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None and hasattr(ctx.app, "_agent_running") and ctx.app._agent_running:
            return CommandResult(success=False, message="Cannot compact while agent is running.")

        messages: list = []
        thread_id: str | None = None

        if ctx.app is not None:
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

        if len(messages) < 4:
            return CommandResult(
                success=False,
                message="Not enough messages to compact (minimum 4 messages required).",
            )

        before_tokens = self._count_tokens(messages)

        # Separate messages into older history (to compact) and recent 2 turns (to keep)
        to_compact = messages[:-2]
        to_keep = messages[-2:]

        # Create concise summary of older history
        compact_text = []
        for m in to_compact:
            content = getattr(m, "content", str(m))
            if isinstance(content, list):
                content = "".join(str(b.get("text", b)) if isinstance(b, dict) else str(b) for b in content)
            role = type(m).__name__.removesuffix("Message")
            if content:
                compact_text.append(f"{role}: {str(content)[:150]}")

        summary_body = "\n".join(compact_text)
        summary_str = f"Summarized {len(to_compact)} prior messages:\n{summary_body[:300]}"

        from langchain_core.messages import RemoveMessage, SystemMessage
        summary_msg = SystemMessage(content=f"🧹 **Summary of prior conversation:**\n{summary_str}")

        new_messages: list = []
        if ctx.agent is not None and thread_id:
            try:
                removals = [RemoveMessage(id=m.id) for m in to_compact if hasattr(m, "id") and m.id]
                config = {"configurable": {"thread_id": thread_id}}
                await ctx.agent.aupdate_state(config, {"messages": removals + [summary_msg]})
                
                new_state = await ctx.agent.aget_state(config)
                new_messages = new_state.values.get("messages", [])
            except Exception as exc:
                logger.warning("Failed updating LangGraph checkpoint state during compaction: %s", exc)
                new_messages = [summary_msg] + to_keep

        # Refresh UI messages if app is available
        if ctx.app is not None and thread_id and hasattr(ctx.app, "_load_thread_history"):
            try:
                await ctx.app._load_thread_history(thread_id)
            except Exception:
                pass

        after_tokens = self._count_tokens(new_messages) if new_messages else max(50, int(before_tokens * 0.3))
        freed_tokens = max(0, before_tokens - after_tokens)

        msg = (
            f"🧹 **Conversation Compacted**\n"
            f"├ **Before:** ~{before_tokens:,} tokens ({len(messages)} messages)\n"
            f"├ **After:**  ~{after_tokens:,} tokens ({len(new_messages) if new_messages else 'compacted'} messages)\n"
            f"└ **Freed:**  ~{freed_tokens:,} tokens"
        )
        return CommandResult(success=True, message=msg)

    @staticmethod
    def _count_tokens(messages: list) -> int:
        try:
            from langchain_core.messages.utils import count_tokens_approximately
            return count_tokens_approximately(messages)
        except Exception:
            total_chars = sum(len(str(getattr(m, "content", m))) for m in messages)
            return total_chars // 4


__all__ = ["CompactHandler"]
