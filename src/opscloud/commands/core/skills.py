"""Skills and tools listing command handler for OpsCode."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from pathlib import Path
from typing import Any

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


def _extract_name_desc(item: Any) -> tuple[str, str, str, str]:
    """Extract (name, description, path, source) safely from dict or object."""
    if isinstance(item, str):
        return item, "", "", "unknown"
    if isinstance(item, dict):
        name = str(item.get("name") or "unnamed")
        desc = str(item.get("description") or "")
        path = str(item.get("path") or "")
        source = str(item.get("source") or "unknown")
        return name, desc, path, source
    name = str(getattr(item, "name", "unnamed"))
    desc = str(getattr(item, "description", ""))
    path = str(getattr(item, "path", ""))
    source = str(getattr(item, "source", "unknown"))
    return name, desc, path, source


class SkillsHandler(BaseCommandHandler):
    """Handler for /skills (/tools) — list all available tools and skills."""

    @property
    def name(self) -> str:
        return "/skills"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/tools",)

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
        # Delegate to the app's dedicated skills viewer launcher (like /plugins → _show_plugin_manager)
        if ctx.app is not None and hasattr(ctx.app, "_open_skills_viewer") and ctx.raw_command != "/tools":
            try:
                ctx.app._open_skills_viewer()
                return CommandResult(success=True, message="", mount_as_app_message=False)
            except Exception as e:
                logger.warning("Failed to open SkillsViewerScreen: %s", e)

        sections: list[str] = []

        # 1. Fetch Discovered Skills
        skills_raw: list[Any] = []
        if ctx.app and hasattr(ctx.app, "get_discovered_skills"):
            raw = ctx.app.get_discovered_skills()
            if raw is not None:
                skills_raw = list(raw)

        # Categorize skills by source
        user_skills: list[tuple[str, str, str]] = []
        project_skills: list[tuple[str, str, str]] = []
        plugin_skills: list[tuple[str, str, str]] = []
        builtin_skills: list[tuple[str, str, str]] = []

        for s in skills_raw:
            name, desc, path, source = _extract_name_desc(s)
            item = (name, desc, path)
            if source == "user":
                user_skills.append(item)
            elif source == "project":
                project_skills.append(item)
            elif source == "plugin":
                plugin_skills.append(item)
            elif source == "built-in":
                builtin_skills.append(item)
            else:
                builtin_skills.append(item)

        # Build Skills Output Sections
        if project_skills:
            lines = []
            for name, desc, path in project_skills:
                loc = f"\n    Location: `{Path(path).parent}/`" if path else ""
                d_str = f"\n    Purpose: {desc}" if desc else ""
                lines.append(f"  • **{name}**{loc}{d_str}")
            sections.append("🎯 **Project Skills:**\n\n" + "\n\n".join(lines))

        if user_skills:
            lines = []
            for name, desc, path in user_skills:
                loc = f"\n    Location: `{Path(path).parent}/`" if path else ""
                d_str = f"\n    Purpose: {desc}" if desc else ""
                lines.append(f"  • **{name}**{loc}{d_str}")
            sections.append("🎯 **User Skills:**\n\n" + "\n\n".join(lines))

        if plugin_skills:
            lines = []
            for name, desc, path in plugin_skills:
                loc = f"\n    Location: `{Path(path).parent}/`" if path else ""
                d_str = f"\n    Purpose: {desc}" if desc else ""
                lines.append(f"  • **{name}**{loc}{d_str}")
            sections.append("🔌 **Plugin Skills:**\n\n" + "\n\n".join(lines))

        if builtin_skills:
            lines = []
            for name, desc, path in builtin_skills:
                loc = f"\n    Location: `{Path(path).parent}/`" if path else ""
                d_str = f"\n    Purpose: {desc}" if desc else ""
                lines.append(f"  • **{name}**{loc}{d_str}")
            sections.append("📦 **Built-in Skills:**\n\n" + "\n\n".join(lines))


        # 2. Built-in Tools & MCP Tools (shown when /tools alias is called or if explicitly requested)
        if ctx.raw_command == "/tools":
            builtin_tools_raw: list[Any] = []
            if ctx.app and hasattr(ctx.app, "get_active_tools"):
                raw_t = ctx.app.get_active_tools()
                if raw_t is not None:
                    builtin_tools_raw = list(raw_t)

            if builtin_tools_raw:
                tool_lines = []
                for item in builtin_tools_raw:
                    t_name, t_desc, _, _ = _extract_name_desc(item)
                    tool_lines.append(f"  • `{t_name}`: {t_desc}" if t_desc else f"  • `{t_name}`")
                if tool_lines:
                    sections.append("🛠️ **Built-in Core Tools:**\n" + "\n".join(tool_lines))

            if ctx.app and hasattr(ctx.app, "get_mcp_servers"):
                raw_mcp = ctx.app.get_mcp_servers()
                mcp_servers: list[Any] = list(raw_mcp) if raw_mcp is not None else []
                for srv in mcp_servers:
                    srv_name = getattr(srv, "name", str(srv.get("name") if isinstance(srv, dict) else "unknown"))
                    status = getattr(srv, "status", srv.get("status") if isinstance(srv, dict) else "ok")
                    status_icon = "✅" if status in ("ok", True) else "⚠️"
                    raw_tools = srv.get("tools") if isinstance(srv, dict) else getattr(srv, "tools", ())
                    tools = list(raw_tools) if raw_tools is not None else []
                    tool_lines = []
                    for t in tools:
                        t_name, t_desc, _, _ = _extract_name_desc(t)
                        tool_lines.append(f"  • `{t_name}`: {t_desc}" if t_desc else f"  • `{t_name}`")
                    if tool_lines:
                        sections.append(f"🔌 **MCP Server `{srv_name}` ({status_icon} {status}):**\n" + "\n".join(tool_lines))

        if not sections:
            empty_msg = (
                "🎯 **Skills**\n\n"
                "No skills found.\n"
                "Skills are loaded from these directories (highest precedence first):\n"
                "  1. `.opscloud/skills/`                 project skills\n"
                "  2. `~/.opscloud/skills/`               user skills\n"
                "  3. Installed plugins                 plugin skills\n"
            )
            return CommandResult(success=True, message=empty_msg)

        return CommandResult(
            success=True,
            message="\n\n".join(sections),
        )


__all__ = ["SkillsHandler"]


