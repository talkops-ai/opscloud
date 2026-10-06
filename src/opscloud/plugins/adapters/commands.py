"""Adapter from discovered plugins to OpsCloud slash commands."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from opscloud.plugins.models import PluginInstance

logger = get_logger(__name__)


class PluginCommandHandler(BaseCommandHandler):
    """Command handler for plugin-contributed slash command workflow files."""

    def __init__(
        self,
        *,
        command_name: str,
        description: str,
        instructions: str,
        plugin_id: str,
        aliases: tuple[str, ...] = (),
    ) -> None:
        self._name = command_name if command_name.startswith("/") else f"/{command_name}"
        self._description = description
        self._instructions = instructions
        self._plugin_id = plugin_id
        self._aliases = aliases

    @property
    def name(self) -> str:
        return self._name

    @property
    def aliases(self) -> tuple[str, ...]:
        return self._aliases

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.AUTOMATION

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    @property
    def description(self) -> str:
        return self._description

    async def execute(self, ctx: CommandContext) -> CommandResult:
        # Build prompt message combining plugin command instructions and user arguments
        user_input = ctx.args.strip()
        combined_prompt = (
            f"### Plugin Command Execution: {self._name} (from {self._plugin_id})\n\n"
            f"{self._instructions}"
        )
        if user_input:
            combined_prompt += f"\n\n**User Input / Target Arguments**:\n{user_input}"

        return CommandResult(
            success=True,
            message=combined_prompt,
            mount_as_app_message=True,
            notify=f"Triggered plugin command {self._name}",
        )


def _parse_command_file(path: Path) -> tuple[str, str] | None:
    """Read a markdown command file and return (description, body)."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read plugin command file %s: %s", path, exc)
        return None

    # Check for optional YAML frontmatter
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if match:
        body = match.group(2).strip()
        description = "Plugin slash command"
        try:
            import yaml

            fm = yaml.safe_load(match.group(1))
            if isinstance(fm, dict) and isinstance(fm.get("description"), str):
                description = fm["description"]
        except Exception:
            pass
        return description, body

    first_line = content.splitlines()[0] if content.splitlines() else "Plugin command"
    desc = first_line.lstrip("#").strip() or "Plugin command"
    return desc, content.strip()


def plugin_commands(
    plugins: tuple[PluginInstance, ...] | list[PluginInstance],
) -> list[BaseCommandHandler]:
    """Discover and instantiate command handlers for enabled plugins."""
    handlers: list[BaseCommandHandler] = []

    for plugin in plugins:
        p_id = plugin.plugin_id
        p_name = plugin.name
        for cmd_path in plugin.inventory.commands:
            if not cmd_path.exists():
                continue
            cmd_files: list[Path] = []
            if cmd_path.is_dir():
                cmd_files.extend(sorted(cmd_path.glob("*.md")))
            elif cmd_path.is_file() and cmd_path.suffix == ".md":
                cmd_files.append(cmd_path)

            for file in cmd_files:
                parsed = _parse_command_file(file)
                if not parsed:
                    continue
                desc, instructions = parsed
                stem = file.stem.lower().strip()

                primary_name = f"/{stem}"
                namespaced_alias = f"/{p_name}:{stem}"
                full_scoped_alias = f"/{p_id}:{stem}"

                handler = PluginCommandHandler(
                    command_name=primary_name,
                    description=desc,
                    instructions=instructions,
                    plugin_id=p_id,
                    aliases=(namespaced_alias, full_scoped_alias),
                )
                handlers.append(handler)

    return handlers


def discover_plugin_commands(
    project_root: Path | None = None,
) -> list[BaseCommandHandler]:
    """Discover command handlers across all enabled marketplace and project plugins."""
    try:
        from opscloud.plugins.discovery import discover_plugins
        from opscloud.plugins.project_plugins import load_project_plugins

        handlers: list[BaseCommandHandler] = []

        # 1. Enabled marketplace plugins
        plugins = discover_plugins(project_root=project_root).plugins
        handlers.extend(plugin_commands(plugins))

        # 2. Project-local marketplace plugins
        if project_root is not None:
            proj_res = load_project_plugins(project_root)
            for cmd_path in proj_res.main_commands:
                cmd_files: list[Path] = []
                if cmd_path.is_dir():
                    cmd_files.extend(sorted(cmd_path.glob("*.md")))
                elif cmd_path.is_file() and cmd_path.suffix == ".md":
                    cmd_files.append(cmd_path)

                for file in cmd_files:
                    parsed = _parse_command_file(file)
                    if not parsed:
                        continue
                    desc, instructions = parsed
                    stem = file.stem.lower().strip()
                    primary_name = f"/{stem}"
                    handler = PluginCommandHandler(
                        command_name=primary_name,
                        description=desc,
                        instructions=instructions,
                        plugin_id="project",
                    )
                    handlers.append(handler)

        return handlers
    except Exception as exc:
        logger.warning("Could not discover plugin commands: %s", exc)
        return []
