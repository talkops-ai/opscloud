"""Context window display command handler for OpsCode."""

from __future__ import annotations

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region
from opscloud.model.config import resolve_model_context_limit
from opscloud.utils.logger import get_logger
from opscloud.utils.session_stats import format_cost

logger = get_logger(__name__)


class ContextHandler(BaseCommandHandler):
    """Handler for /context command displaying unified context window and session usage."""

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
        raw_model = (
            (getattr(ctx.app, "_model", None) if ctx.app else None)
            or ctx.model_spec
            or getattr(ctx.settings, "model", None)
            or getattr(ctx.settings, "model_name", None)
            or "default"
        )
        display_model = raw_model.split(":", 1)[1] if ":" in raw_model else raw_model

        limit = resolve_model_context_limit(raw_model)
        if not limit or limit <= 0:
            limit = getattr(ctx.settings, "model_context_limit", None)

        # 1. Turn active context tokens
        total_tokens = 0
        if ctx.app is not None and hasattr(ctx.app, "get_context_tokens"):
            try:
                total_tokens = ctx.app.get_context_tokens() or 0
            except Exception as exc:
                logger.debug("Failed getting context tokens: %s", exc)

        conv_tokens = 0
        if ctx.app is not None and hasattr(ctx.app, "get_conversation_token_count"):
            try:
                cnt = await ctx.app.get_conversation_token_count()
                conv_tokens = cnt if cnt is not None else 0
            except Exception as exc:
                logger.debug("Failed getting conversation token count: %s", exc)

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

        # 2. Context Window summary and breakdown
        remaining_space = max(0, limit - total_tokens) if (limit and limit > 0) else None
        remaining_pct = (
            (remaining_space / limit) * 100
            if (limit and limit > 0 and remaining_space is not None)
            else None
        )

        if limit and limit > 0:
            pct = (total_tokens / limit) * 100
            sections.append(
                f"**Context Window:** {total_tokens:,} / {limit:,} tokens ({pct:.1f}%) · `{display_model}`"
            )
        else:
            sections.append(f"**Context Window:** {total_tokens:,} tokens · `{display_model}`")

        sections.append(f"  ├ **System Prompt + Tools:** ~{overhead:,} tokens")
        if remaining_space is not None and remaining_pct is not None:
            sections.append(f"  ├ **Conversation History:** ~{conv_tokens:,} tokens")
            sections.append(
                f"  └ **Remaining Space:** ~{remaining_space:,} tokens ({remaining_pct:.1f}% free)"
            )
        else:
            sections.append(f"  └ **Conversation History:** ~{conv_tokens:,} tokens")

        # 3. Cumulative Session Total (since /cost and /tokens are deprecated)
        cumulative_tokens = 0
        session_cost = 0.0
        request_count = 0
        input_tokens = 0
        output_tokens = 0

        if ctx.app is not None:
            raw_cum = getattr(ctx.app, "_cumulative_session_tokens", 0)
            if isinstance(raw_cum, int) and raw_cum > 0:
                cumulative_tokens = raw_cum
            raw_cost = getattr(ctx.app, "_session_cost_usd", 0.0)
            if isinstance(raw_cost, (int, float)) and raw_cost > 0:
                session_cost = float(raw_cost)
            adapter = getattr(ctx.app, "_adapter", None)
            if adapter and hasattr(adapter, "_stats") and adapter._stats:
                stats = adapter._stats
                rc = getattr(stats, "request_count", 0)
                if isinstance(rc, int) and rc > 0:
                    request_count = rc
                inp = getattr(stats, "input_tokens", 0)
                if isinstance(inp, int) and inp > 0:
                    input_tokens = inp
                out = getattr(stats, "output_tokens", 0)
                if isinstance(out, int) and out > 0:
                    output_tokens = out
                if cumulative_tokens == 0 and (input_tokens > 0 or output_tokens > 0):
                    cumulative_tokens = input_tokens + output_tokens
                tot_c = getattr(stats, "total_cost_usd", 0.0)
                if session_cost == 0.0 and isinstance(tot_c, (int, float)) and tot_c > 0:
                    session_cost = float(tot_c)

        if cumulative_tokens == 0 and total_tokens > 0:
            cumulative_tokens = total_tokens
        if request_count == 0 and cumulative_tokens > 0:
            request_count = 1

        cost_display = format_cost(session_cost) if session_cost > 0 else "$0.00"
        req_word = "request" if request_count == 1 else "requests"
        req_str = f" ({request_count} {req_word})"
        sections.append(
            f"\n**Session Total:** {cumulative_tokens:,} tokens · {cost_display}{req_str}"
        )
        if input_tokens > 0 or output_tokens > 0:
            sections.append(f"  ├ **Input Tokens:** {input_tokens:,}")
            sections.append(f"  └ **Output Tokens:** {output_tokens:,}")

        # 4. Active Resources
        tools_list: list[str] = []
        mcp_list: list[str] = []
        skills_list: list[str] = []

        if ctx.app is not None:
            if hasattr(ctx.app, "get_active_tools"):
                try:
                    tools = ctx.app.get_active_tools()
                    tools_list = [t.get("name", "") if isinstance(t, dict) else str(t) for t in tools if t]
                except Exception:
                    pass
            if hasattr(ctx.app, "get_mcp_servers"):
                try:
                    servers = ctx.app.get_mcp_servers()
                    mcp_list = [s.get("name", "") if isinstance(s, dict) else str(s) for s in servers if s]
                except Exception:
                    pass
            if hasattr(ctx.app, "get_discovered_skills"):
                try:
                    skills = ctx.app.get_discovered_skills()
                    skills_list = [s.get("name", "") if isinstance(s, dict) else str(s) for s in skills if s]
                except Exception:
                    pass

        tool_count = len(tools_list)
        mcp_count = len(mcp_list)
        skill_count = len(skills_list)

        sections.append(
            f"\n**Active Resources:** {tool_count} tools · {mcp_count} MCP servers · {skill_count} skills"
        )
        res_branches: list[str] = []
        if tools_list:
            res_branches.append(f"**Tools:** {', '.join(tools_list)}")
        if mcp_list:
            res_branches.append(f"**MCP Servers:** {', '.join(mcp_list)}")
        if skills_list:
            res_branches.append(f"**Skills:** {', '.join(skills_list)}")

        for i, branch in enumerate(res_branches):
            prefix = "  └ " if i == len(res_branches) - 1 else "  ├ "
            sections.append(f"{prefix}{branch}")

        # 5. Infrastructure Context
        infra_lines: list[str] = []
        aws_profile = get_active_aws_profile()
        aws_region = get_active_aws_region()
        cloud_ctx = getattr(ctx.settings, "cloud_context", None)
        kube_ctx = getattr(ctx.settings, "kube_context", None)

        if aws_profile:
            reg_suffix = f", region: {aws_region}" if aws_region else ""
            infra_lines.append(f"**Cloud:** AWS (profile: {aws_profile}{reg_suffix})")
        elif cloud_ctx:
            infra_lines.append(f"**Cloud:** {cloud_ctx}")

        if kube_ctx:
            infra_lines.append(f"**K8s:** {kube_ctx}")

        if infra_lines:
            sections.append("\n**Infrastructure Context:**")
            for i, line in enumerate(infra_lines):
                prefix = "  └ " if i == len(infra_lines) - 1 else "  ├ "
                sections.append(f"{prefix}{line}")

        return CommandResult(success=True, message="\n".join(sections))


__all__ = ["ContextHandler"]
