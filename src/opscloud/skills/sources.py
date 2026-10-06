"""Ordered skill sources resolver conforming to LangChain dcode architecture.

This module provides the canonical source-of-truth skill discovery function,
mirroring `deepagents_code.agent.get_skill_sources` from official LangChain dcode.
It standardizes skill source resolution across coordinator agents, dynamic subagents,
SkillRegistry, and CLI tools without ad-hoc path slicing or scattered configuration.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from opscloud.config import paths
from opscloud.plugins.adapters.skills import (
    CodeSkillSource,
    DirectorySkillSource,
    PluginSkillSource,
    plugin_skill_sources,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from opscloud.project_utils import ProjectContext
    from opscloud.subagents.types import SubagentMetadata

logger = logging.getLogger(__name__)


def get_skill_sources(
    assistant_id: str = "opscloud",
    project_root: Path | str | None = None,
    project_context: ProjectContext | None = None,
    *,
    include_subagent_skills: bool = False,
    subagents: Sequence[Any] | None = None,
    store: Any = None,
) -> list[CodeSkillSource]:
    """Return ordered skill sources for PluginSkillsMiddleware and audit tooling.

    Follows the official dcode architecture and precedence order (lowest to highest):
    0. Built-in skills (<package>/built_in_skills/)
    1. Plugin skills:
       - When ``include_subagent_skills`` is False (default for coordinator agent):
         Agent plugins (with agents/ folder) are excluded; their skills belong
         exclusively to the subagent. Partner and vertical plugins attach to the coordinator.
       - When ``include_subagent_skills`` is True (for criteria / comprehensive audit):
         All plugin skills across all plugin types are included.
    2. User Deepagents / OpsCloud skills (~/.opscloud/{agent}/skills/)
    3. User Agents skills (~/.agents/skills/)
    4. Project Deepagents / OpsCloud skills (.opscloud/skills/)
    5. Project Agents skills (.agents/skills/)
    6. User Claude (experimental: ~/.claude/skills/)
    7. Project Claude (experimental: .claude/skills/)
    8. Bundled Subagent skills (when ``include_subagent_skills=True``)

    Args:
        assistant_id: Agent identifier for user skill directories.
        project_root: Optional explicit project root directory.
        project_context: Optional ProjectContext for resolving project directories.
        include_subagent_skills: Whether to include subagent-bound skills. Default False.
        subagents: Optional sequence of subagents to extract bundled skills from.
        store: Optional ConfigStore instance for plugin discovery.

    Returns:
        Ordered list of CodeSkillSource entries (path, label) or (path, label, namespace).
    """
    effective_root = (
        project_context.project_root
        if project_context is not None
        else (Path(project_root) if project_root else paths.find_project_root())
    )

    sources: list[CodeSkillSource] = []

    # 0. Built-in skills
    bi_dir = paths.get_built_in_skills_dir()
    if bi_dir.is_dir():
        sources.append((str(bi_dir), "Built-in"))

    # 1. Plugin skills
    try:
        from opscloud.plugins.discovery import discover_plugins

        plugin_result = discover_plugins(project_root=effective_root, store=store)
        if plugin_result.warnings:
            logger.warning("Plugin discovery warnings: %s", plugin_result.warnings)
        sources.extend(
            plugin_skill_sources(
                plugin_result.plugins,
                include_subagent_skills=include_subagent_skills,
            )
        )
    except Exception:
        logger.warning("Could not discover plugin skills", exc_info=True)

    # 2. User Deepagents / OpsCloud skills (~/.opscloud/{agent}/skills/)
    user_skills_dir = paths.get_user_skills_dir(assistant_id)
    if user_skills_dir and user_skills_dir.is_dir():
        sources.append((str(user_skills_dir), "User Skills"))

    # 3. User Agents skills (~/.agents/skills/)
    user_agent_skills_dir = paths.get_user_agent_skills_dir()
    if user_agent_skills_dir and user_agent_skills_dir.is_dir():
        sources.append((str(user_agent_skills_dir), "User Agent Skills"))

    # 4. Project Deepagents / OpsCloud skills (.opscloud/skills/)
    project_skills_dir = (
        project_context.project_skills_dir()
        if project_context is not None and hasattr(project_context, "project_skills_dir")
        else paths.get_project_skills_dir(effective_root)
    )
    if project_skills_dir and project_skills_dir.is_dir():
        sources.append((str(project_skills_dir), "Project Skills"))

    # 5. Project Agents skills (.agents/skills/)
    project_agent_skills_dir = (
        project_context.project_agent_skills_dir()
        if project_context is not None and hasattr(project_context, "project_agent_skills_dir")
        else paths.get_project_agent_skills_dir(effective_root)
    )
    if project_agent_skills_dir and project_agent_skills_dir.is_dir():
        sources.append((str(project_agent_skills_dir), "Project Agent Skills"))

    # 6. Experimental: Claude Code skill directories
    user_claude_skills_dir = paths.get_user_claude_skills_dir()
    if user_claude_skills_dir and user_claude_skills_dir.is_dir():
        sources.append((str(user_claude_skills_dir), "User Claude"))

    project_claude_skills_dir = (
        project_context.project_claude_skills_dir()
        if project_context is not None and hasattr(project_context, "project_claude_skills_dir")
        else paths.get_project_claude_skills_dir(effective_root)
    )
    if project_claude_skills_dir and project_claude_skills_dir.is_dir():
        sources.append((str(project_claude_skills_dir), "Project Claude"))

    # 7. Bundled subagent skills (when explicitly requested, e.g. for criteria/comprehensive audit)
    if include_subagent_skills:
        from opscloud.plugins.adapters.agents import get_subagent_skills_source

        target_subagents = list(subagents) if subagents is not None else []
        if not target_subagents:
            try:
                from opscloud.subagents.loader import list_subagents

                target_subagents.extend(list_subagents(project_root=effective_root, store=store))
            except Exception as exc:
                logger.debug("Could not discover subagents for bundled skills: %s", exc)

        for sub_meta in target_subagents:
            sub_source = get_subagent_skills_source(sub_meta)
            if sub_source and Path(sub_source[0]).is_dir():
                sources.append(sub_source)

    # Deduplicate while preserving precedence order
    seen: set[str] = set()
    deduped: list[CodeSkillSource] = []
    for s in sources:
        path_str = s[0]
        if path_str not in seen and Path(path_str).exists():
            deduped.append(s)
            seen.add(path_str)

    return deduped


__all__ = [
    "CodeSkillSource",
    "DirectorySkillSource",
    "PluginSkillSource",
    "get_skill_sources",
]
