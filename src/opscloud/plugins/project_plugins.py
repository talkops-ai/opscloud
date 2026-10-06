"""Auto-discovery and loading of project-local plugin marketplaces.

When a project contains a marketplace manifest inside its ``.opscloud`` directory
(or project root, e.g. ``.claude-plugin/marketplace.json``), this module automatically
discovers all declared plugins and bifurcates them into two categories:

1. **Agent plugins** — plugins whose source root contains an ``agents/``
   directory. Each ``*.md`` file under ``agents/`` becomes a subagent
   definition whose skills, MCP servers, and commands are bound *exclusively*
   to that subagent (they are NOT exposed on the main agent).

2. **Non-agent plugins** — plugins without an ``agents/`` directory. Their
   skills, MCP servers, and commands are bound to the **main** agent
   via the standard middleware/session-manager pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opscloud.plugins.manifest import build_inventory, load_manifest
from opscloud.plugins.marketplace import (
    MarketplaceError,
    _load_marketplace_file,
    find_marketplace_manifest,
    materialize_plugin_source,
)
from opscloud.plugins.models import (
    ComponentInventory,
    PluginInstance,
)

from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from opscloud.plugins.adapters.skills import PluginSkillSource
    from opscloud.subagents.types import SubagentMetadata

logger = get_logger(__name__)


@dataclass
class ProjectPluginResult:
    """Aggregated result of auto-loading project-local plugins."""

    subagent_metas: list[SubagentMetadata] = field(default_factory=list)
    """Subagent metadata from agent plugins (bound to subagents only)."""

    main_skill_sources: list[PluginSkillSource] = field(default_factory=list)
    """Skill source tuples for non-agent plugins (bound to main agent)."""

    main_mcp_configs: list[dict[str, Any]] = field(default_factory=list)
    """MCP server configs from non-agent plugins (bound to main agent)."""

    main_commands: list[Path] = field(default_factory=list)
    """Command paths from non-agent plugins (bound to main agent)."""

    warnings: list[str] = field(default_factory=list)
    """Non-fatal warnings encountered during discovery."""


def _has_agents(inventory: ComponentInventory) -> bool:
    """Return True if the inventory contains at least one populated agents dir or file."""
    for path in inventory.agents:
        try:
            if path.is_dir() and any(path.iterdir()):
                return True
            if path.is_file() and path.suffix == ".md":
                return True
        except OSError:
            continue
    return False


def _collect_skill_names(inventory: ComponentInventory) -> list[str]:
    """Derive skill names from the inventory's skill paths.

    Convention: each ``skills/<name>/SKILL.md`` yields skill name ``<name>``.
    A bare ``SKILL.md`` at the plugin root yields the parent folder name.
    """
    names: list[str] = []
    for path in inventory.skills:
        try:
            if path.is_dir():
                for child in sorted(path.iterdir()):
                    if child.is_dir() and (child / "SKILL.md").is_file():
                        names.append(child.name)
            elif path.name == "SKILL.md":
                names.append(path.parent.name)
        except OSError:
            continue
    return names


def _read_mcp_config(mcp_files: tuple[Path, ...]) -> dict[str, Any] | None:
    """Read and merge MCP server configs from discovered ``.mcp.json`` files."""
    combined_servers: dict[str, Any] = {}
    for mcp_path in mcp_files:
        try:
            if not mcp_path.is_file():
                continue
            content = mcp_path.read_text(encoding="utf-8")
            try:
                raw = json.loads(content)
            except json.JSONDecodeError:
                from opscloud.plugins.adapters.mcp import _try_repair_mcp_json

                raw = _try_repair_mcp_json(content)
                if raw is None:
                    raise
            servers = raw.get("mcpServers") or raw.get("mcp_servers") or raw
            if isinstance(servers, dict):
                combined_servers.update(servers)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not parse MCP config %s: %s", mcp_path, exc)
    return {"mcpServers": combined_servers} if combined_servers else None


def _build_plugin_instance(
    plugin_name: str,
    marketplace_name: str,
    source_root: Path,
    fallback_name: str,
) -> tuple[PluginInstance | None, list[str]]:
    """Build a ``PluginInstance`` from a resolved source root."""
    warnings: list[str] = []
    try:
        manifest, _manifest_path, manifest_warnings = load_manifest(
            source_root, fallback_name=fallback_name
        )
    except Exception as exc:
        return None, [f"Skipping plugin {plugin_name}: {exc}"]
    warnings.extend(manifest_warnings)

    name = manifest.name if manifest and manifest.name else fallback_name
    inventory = build_inventory(source_root, manifest, tuple(warnings))

    try:
        from opscloud.plugins.store import plugin_data_dir

        instance = PluginInstance(
            plugin_id=f"{plugin_name}@{marketplace_name}",
            name=name,
            marketplace=marketplace_name,
            version=manifest.version if manifest is not None else None,
            root=source_root,
            data_dir=plugin_data_dir(f"{plugin_name}@{marketplace_name}"),
            manifest=manifest,
            inventory=inventory,
        )
    except (ValueError, OSError) as exc:
        return None, [f"Skipping plugin {plugin_name}: {exc}"]
    return instance, list(inventory.warnings)


def _process_agent_plugin(
    plugin: PluginInstance,
) -> tuple[list[SubagentMetadata], list[str]]:
    """Process an agent plugin: parse subagents and bind skills + MCP to each."""
    from opscloud.plugins.adapters.agents import _enrich_plugin_subagent
    from opscloud.subagents.loader import parse_subagent_file

    subagents: list[SubagentMetadata] = []
    warnings: list[str] = []

    skill_names = _collect_skill_names(plugin.inventory)
    mcp_file_paths = [str(p) for p in plugin.inventory.mcp_files if p.is_file()]

    for agent_path in plugin.inventory.agents:
        try:
            if not agent_path.exists():
                continue
        except OSError:
            continue

        md_files: list[tuple[Path, str]] = []
        if agent_path.is_dir():
            for entry in sorted(agent_path.iterdir()):
                if entry.is_file() and entry.suffix == ".md":
                    md_files.append((entry, entry.stem))
                elif entry.is_dir():
                    agents_md = entry / "AGENTS.md"
                    if agents_md.is_file():
                        md_files.append((agents_md, entry.name))
        elif agent_path.is_file() and agent_path.suffix == ".md":
            md_files.append((agent_path, agent_path.stem))

        for md_path, fallback_name in md_files:
            meta = parse_subagent_file(md_path, fallback_name=fallback_name)
            if meta is None:
                warnings.append(f"Could not parse agent definition: {md_path}")
                continue

            if skill_names and not meta.get("skills"):
                meta["skills"] = list(skill_names)

            if mcp_file_paths and not meta.get("mcp_files"):
                meta["mcp_files"] = list(mcp_file_paths)

            subagents.append(_enrich_plugin_subagent(meta, plugin))

    return subagents, warnings


def _process_main_plugin(
    plugin: PluginInstance,
) -> tuple[list[tuple[str, str, str]], list[dict[str, Any]], list[Path], list[str]]:
    """Process a non-agent plugin: yield skills, MCP, and commands for main agent."""
    from opscloud.plugins.adapters.skills import plugin_skill_sources

    warnings: list[str] = []
    skill_sources = plugin_skill_sources((plugin,))

    mcp_configs: list[dict[str, Any]] = []
    mcp_config = _read_mcp_config(plugin.inventory.mcp_files)
    if mcp_config:
        mcp_configs.append(mcp_config)
    if plugin.manifest and plugin.manifest.inline_mcp:
        mcp_configs.append({"mcpServers": plugin.manifest.inline_mcp})

    command_paths: list[Path] = list(plugin.inventory.commands)

    return skill_sources, mcp_configs, command_paths, warnings


def load_project_plugins(project_root: Path | str | None) -> ProjectPluginResult:
    """Auto-discover and load all plugins from a project's marketplace manifest.

    Args:
        project_root: Path to the project root directory.

    Returns:
        A ``ProjectPluginResult`` with bifurcated agent and non-agent plugin data.
    """
    result = ProjectPluginResult()
    if project_root is None:
        return result

    root = Path(project_root)
    if not root.is_dir():
        return result

    # Check candidate locations for marketplace manifest
    manifest_path = None
    for candidate in (root / ".opscloud", root / ".opscode", root):
        if candidate.is_dir():
            manifest_path = find_marketplace_manifest(candidate)
            if manifest_path is not None:
                break

    if manifest_path is None:
        return result

    try:
        marketplace = _load_marketplace_file(manifest_path)
    except MarketplaceError as exc:
        result.warnings.append(f"Could not load project marketplace: {exc}")
        return result

    logger.info(
        "Auto-loading project marketplace %r with %d plugin(s)",
        marketplace.name,
        len(marketplace.plugins),
    )

    from opscloud.plugins.store import load_all_disabled_plugin_ids

    disabled_ids = load_all_disabled_plugin_ids(project_root=root)

    for entry in marketplace.plugins:
        plugin_id = f"{entry.name}@{marketplace.name}"
        if plugin_id in disabled_ids:
            logger.info("Skipping project plugin %s (explicitly disabled)", plugin_id)
            continue

        source_root = materialize_plugin_source(marketplace, entry)
        if source_root is None:
            result.warnings.append(f"Plugin {entry.name!r}: could not resolve source")
            continue

        plugin, plugin_warnings = _build_plugin_instance(
            plugin_name=entry.name,
            marketplace_name=marketplace.name,
            source_root=source_root,
            fallback_name=entry.name,
        )
        result.warnings.extend(plugin_warnings)
        if plugin is None:
            continue

        if _has_agents(plugin.inventory):
            subagent_metas, agent_warnings = _process_agent_plugin(plugin)
            result.subagent_metas.extend(subagent_metas)
            result.warnings.extend(agent_warnings)
            logger.debug(
                "Plugin %s: agent plugin → %d subagent(s)",
                entry.name,
                len(subagent_metas),
            )
        else:
            skills, mcps, cmds, main_warnings = _process_main_plugin(plugin)
            result.main_skill_sources.extend(skills)
            result.main_mcp_configs.extend(mcps)
            result.main_commands.extend(cmds)
            result.warnings.extend(main_warnings)
            logger.debug(
                "Plugin %s: main plugin → %d skill source(s), %d MCP config(s), %d command(s)",
                entry.name,
                len(skills),
                len(mcps),
                len(cmds),
            )

    return result
