"""Context window display command handler for OpsCode."""

from __future__ import annotations

from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


class ContextHandler(BaseCommandHandler):
    """Handler for /context command displaying unified context window usage."""

    @property
    def name(self) -> str:
        return "/context"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        model = (
            getattr(ctx.settings, "model_name", None)
            or ctx.model_spec
            or (getattr(ctx.app, "_model", None) if ctx.app else None)
            or "default"
        )

        total_tokens = 0
        if ctx.app is not None and hasattr(ctx.app, "get_context_tokens"):
            try:
                total_tokens = ctx.app.get_context_tokens() or 0
            except Exception as exc:
                logger.debug("Failed getting context tokens: %s", exc)

        conv_tokens: int = 0
        if ctx.app is not None and hasattr(ctx.app, "get_conversation_token_count"):
            try:
                cnt = await ctx.app.get_conversation_token_count()
                conv_tokens = cnt if cnt is not None else 0
            except Exception as exc:
                logger.debug("Failed getting conversation token count: %s", exc)

        limit: int | None = getattr(ctx.settings, "model_context_limit", None)

        # Estimate base system prompt + tool definitions if no model API response has been recorded yet
        if total_tokens > 0:
            overhead = max(0, total_tokens - conv_tokens)
        else:
            # Fallback estimation before first LLM turn in current app session
            tool_count_est = 4
            if ctx.app is not None and hasattr(ctx.app, "get_active_tools"):
                try:
                    tool_count_est = len(ctx.app.get_active_tools()) or 4
                except Exception:
                    pass
            overhead = 450 + (tool_count_est * 180)
            total_tokens = overhead + conv_tokens

        sections: list[str] = []

        # 1. Token usage summary
        if limit and limit > 0:
            pct = (total_tokens / limit) * 100
            sections.append(f"📊 **Context Window:** {total_tokens:,} / {limit:,} tokens ({pct:.1f}%) · `{model}`")
        else:
            sections.append(f"📊 **Context Window:** {total_tokens:,} tokens · `{model}`")

        # 2. Token Breakdown
        sections.append(f"  ├ **System Prompt + Tools:** ~{overhead:,} tokens")
        sections.append(f"  └ **Conversation History:** ~{conv_tokens:,} tokens")

        # 3. Active Resources
        tool_count = 0
        mcp_count = 0
        skill_count = 0

        if ctx.app is not None:
            if hasattr(ctx.app, "get_active_tools"):
                try:
                    tool_count = len(ctx.app.get_active_tools())
                except Exception:
                    pass
            if hasattr(ctx.app, "get_mcp_servers"):
                try:
                    mcp_count = len(ctx.app.get_mcp_servers())
                except Exception:
                    pass
            if hasattr(ctx.app, "get_discovered_skills"):
                try:
                    skill_count = len(ctx.app.get_discovered_skills())
                except Exception:
                    pass

        sections.append(f"\n🧩 **Active Resources:** {tool_count} tools · {mcp_count} MCP servers · {skill_count} skills")

        # 4. Infrastructure Context (OpsCode specific)
        cloud_ctx = getattr(ctx.settings, "cloud_context", None)
        kube_ctx = getattr(ctx.settings, "kube_context", None)

        if cloud_ctx or kube_ctx:
            sections.append("\n☁️ **Infrastructure Context:**")
            if cloud_ctx:
                sections.append(f"  ├ **Cloud:** {cloud_ctx}")
            if kube_ctx:
                sections.append(f"  └ **K8s:** {kube_ctx}")

        return CommandResult(success=True, message="\n".join(sections))


__all__ = ["ContextHandler"]
