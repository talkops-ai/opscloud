"""Adapter from discovered plugins to K8s Autopilot subagent metadata."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opscloud.plugins.models import PluginInstance
    from opscloud.subagents.types import SubagentMetadata

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def _enrich_plugin_subagent(meta: SubagentMetadata, plugin: PluginInstance) -> SubagentMetadata:
    p_id = plugin.plugin_id
    raw_name = meta.get("name", "")

    if not raw_name or raw_name == plugin.name or p_id.startswith(f"{raw_name}@"):
        meta["name"] = p_id
    elif not raw_name.startswith(f"{p_id}:"):
        meta["name"] = f"{p_id}:{raw_name}"

    meta["source"] = f"plugin:{p_id}"

    # Attach default plugin skill wildcard if no skills explicitly set
    if not meta.get("skills"):
        meta["skills"] = [f"{p_id}:*"]

    # Attach plugin MCP files or inline config if subagent doesn't specify own
    if "mcp_config" not in meta and "mcp_files" not in meta:
        if plugin.inventory.mcp_files:
            meta["mcp_files"] = [str(p) for p in plugin.inventory.mcp_files]
        elif plugin.manifest and plugin.manifest.inline_mcp:
            meta["mcp_config"] = {"mcpServers": plugin.manifest.inline_mcp}

    return meta


def plugin_subagents(plugins: tuple[PluginInstance, ...] | list[PluginInstance]) -> list[SubagentMetadata]:
    """Discover and parse subagents from enabled plugins."""
    from opscloud.subagents.loader import parse_subagent_file

    subagents: list[SubagentMetadata] = []

    for plugin in plugins:
        for agent_path in plugin.inventory.agents:
            if not agent_path.exists():
                continue
            if agent_path.is_dir():
                # Scan for .md files or AGENTS.md in subdirectory
                for entry in agent_path.iterdir():
                    if entry.is_file() and entry.suffix == ".md":
                        meta = parse_subagent_file(entry, fallback_name=entry.stem)
                        if meta:
                            subagents.append(_enrich_plugin_subagent(meta, plugin))
                    elif entry.is_dir():
                        sub_agents_file = entry / "AGENTS.md"
                        if sub_agents_file.is_file():
                            meta = parse_subagent_file(sub_agents_file, fallback_name=entry.name)
                            if meta:
                                subagents.append(_enrich_plugin_subagent(meta, plugin))
            elif agent_path.is_file() and agent_path.suffix == ".md":
                meta = parse_subagent_file(agent_path, fallback_name=agent_path.stem)
                if meta:
                    subagents.append(_enrich_plugin_subagent(meta, plugin))

    return subagents


def discover_plugin_subagents(project_root: Path | None = None, store: Any = None) -> list[SubagentMetadata]:
    """Discover subagent definitions across all enabled marketplace and project plugins."""
    try:
        from opscloud.plugins.discovery import discover_plugins

        plugins = discover_plugins(project_root=project_root, store=store).plugins
        return plugin_subagents(plugins)
    except Exception as exc:
        logger.warning("Could not discover plugin subagents: %s", exc)
        return []
