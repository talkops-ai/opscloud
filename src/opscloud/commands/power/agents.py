"""``/agents`` — Opens the agent selector modal for switching agent personas."""

from __future__ import annotations

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel


class AgentsHandler(BaseCommandHandler):
    """List and switch agent personas."""

    @property
    def name(self) -> str:
        return "/agents"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.IMMEDIATE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        app = ctx.app

        # TUI mode — push agent selector screen
        if app is not None and hasattr(app, "_show_agent_selector"):
            try:
                app._show_agent_selector()
                return CommandResult(
                    success=True,
                    message=None,
                    mount_as_app_message=False,
                )
            except Exception:
                pass

        # Non-interactive fallback — list available agents
        agents = _list_agents()
        if not agents:
            return CommandResult(
                success=True,
                message="No agent configurations found.\n"
                "Create agent files in `.opscloud/agents/` or `~/.opscloud/agents/`.",
            )

        lines = ["**Available Agents:**", ""]
        for agent in agents:
            name = agent.get("name", "unknown")
            desc = agent.get("description", "")
            source = agent.get("source", "")
            tag = f" ({source})" if source else ""
            lines.append(f"  • **{name}**{tag} — {desc}" if desc else f"  • **{name}**{tag}")

        return CommandResult(success=True, message="\n".join(lines))


def _list_agents() -> list[dict[str, str]]:
    """Discover agent configurations from standard directories."""
    from pathlib import Path
    from opscloud.config import paths as config_paths

    agents: list[dict[str, str]] = []
    search_dirs: list[tuple[Path, str]] = [
        (config_paths.user_agents_dir(), "user"),
        (Path.cwd() / ".opscloud" / "agents", "project"),
    ]

    for base, source in search_dirs:
        if not base.is_dir():
            continue
        for agent_dir in sorted(base.iterdir()):
            if not agent_dir.is_dir():
                continue
            agents_md = agent_dir / "AGENTS.md"
            if agents_md.is_file():
                desc = ""
                try:
                    first_lines = agents_md.read_text(encoding="utf-8").splitlines()[:5]
                    for line in first_lines:
                        stripped = line.strip().lstrip("#").strip()
                        if stripped:
                            desc = stripped
                            break
                except OSError:
                    pass
                agents.append({
                    "name": agent_dir.name,
                    "description": desc,
                    "source": source,
                })

    return agents
