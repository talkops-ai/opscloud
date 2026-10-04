"""Loader for custom and dynamic subagent definitions in OpsCloud.

OpsCloud is a terminal-based coding and cloud operations agent (CLI & TUI).
Unlike containerized web backends, subagents in OpsCloud are strictly dynamic:
- **No built-in subagents**: Built-in subagents are not bundled; domain operators
  are supplied dynamically through **Agent Plugins**, project directories, and user configurations.
- **Dynamic Plugin Subagents**: Agent plugins (plugins with an ``agents/`` directory or
  explicit agent manifests) bundle subagent markdown definitions, scoped skills, and isolated MCP servers.
- **Project and User Definitions**: Custom subagents defined in ``.opscloud/agents/`` or ``~/.opscloud/agents/``.
- **Filesystem & Config as Source of Truth**: No database synchronizations or container rehydration.
- **Async Remote Subagents**: Defined in ``config.toml`` under ``[async_subagents]``.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from deepagents.middleware.async_subagents import AsyncSubAgent
import yaml

from opscloud.config import paths
from opscloud.subagents.types import SubagentMetadata
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def _parse_subagent_file(file_path: Path, *, fallback_name: str | None = None) -> SubagentMetadata | None:
    """Parse a subagent markdown file with YAML frontmatter.

    The file must have YAML frontmatter (delimited by ---) containing at minimum
    a 'description' field. The body of the file becomes the system_prompt.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Skipping subagent %s: could not read file (%s)", file_path, exc)
        return None

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if not match:
        logger.debug("Skipping subagent %s: missing YAML frontmatter block", file_path)
        return None

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logger.warning("Skipping subagent %s: invalid YAML frontmatter (%s)", file_path, exc)
        return None

    if not isinstance(frontmatter, dict):
        return None

    name_value = frontmatter.get("name", fallback_name)
    description_value = frontmatter.get("description")
    model = frontmatter.get("model")
    raw_skills = frontmatter.get("skills")
    raw_tools = frontmatter.get("tools")
    raw_permission_tier = frontmatter.get("permission_tier")

    name = name_value.strip() if isinstance(name_value, str) and name_value.strip() else None
    description = (
        description_value.strip() if isinstance(description_value, str) and description_value.strip() else None
    )

    if name is None or description is None:
        return None

    if isinstance(raw_skills, str):
        skills: list[str] | None = [s.strip() for s in raw_skills.split(",") if s.strip()]
    elif isinstance(raw_skills, list):
        skills = [str(s) for s in raw_skills]
    else:
        skills = None

    if isinstance(raw_tools, str):
        tools: list[str] | None = [t.strip() for t in raw_tools.split(",") if t.strip()]
    elif isinstance(raw_tools, list):
        tools = [str(t) for t in raw_tools]
    else:
        tools = None

    raw_mcp_config = frontmatter.get("mcp_config")
    raw_mcp_files = frontmatter.get("mcp_files")
    raw_capabilities = frontmatter.get("capabilities")

    mcp_config = dict(raw_mcp_config) if isinstance(raw_mcp_config, dict) else None
    mcp_files = [str(f) for f in raw_mcp_files] if isinstance(raw_mcp_files, list) else None
    capabilities = list(raw_capabilities) if isinstance(raw_capabilities, list) else None

    meta: SubagentMetadata = {
        "name": name,
        "description": description,
        "system_prompt": match.group(2).strip(),
        "model": model,
        "skills": skills,
        "tools": tools,
        "permission_tier": raw_permission_tier if isinstance(raw_permission_tier, str) else None,
        "source": "custom",
        "path": str(file_path),
    }
    if capabilities is not None:
        meta["capabilities"] = capabilities
    if mcp_config is not None:
        meta["mcp_config"] = mcp_config
    if mcp_files is not None:
        meta["mcp_files"] = mcp_files

    return meta


parse_subagent_file = _parse_subagent_file


def _scan_subagents_dir(agents_dir: Path, source: str) -> dict[str, SubagentMetadata]:
    """Scan a directory for subagent definitions.

    Expected formats:
    - ``agents_dir/{name}/AGENTS.md``
    - ``agents_dir/{name}.md``
    - Any markdown file directly under ``agents_dir/``
    """
    subagents: dict[str, SubagentMetadata] = {}
    if not agents_dir.exists() or not agents_dir.is_dir():
        return subagents

    for entry in agents_dir.iterdir():
        if entry.is_dir():
            md_file = entry / "AGENTS.md"
            if not md_file.exists():
                direct_md = entry / f"{entry.name}.md"
                if direct_md.exists():
                    md_file = direct_md
                else:
                    candidates = list(entry.glob("*.md"))
                    if candidates:
                        md_file = candidates[0]

            if md_file.exists():
                parsed = _parse_subagent_file(md_file, fallback_name=entry.name)
                if parsed:
                    parsed["source"] = source
                    bundle_mcp = entry / ".mcp.json"
                    if bundle_mcp.exists() and "mcp_files" not in parsed:
                        parsed["mcp_files"] = [str(bundle_mcp)]
                    subagents[parsed["name"]] = parsed
        elif entry.is_file() and entry.suffix.lower() == ".md":
            parsed = _parse_subagent_file(entry, fallback_name=entry.stem)
            if parsed:
                parsed["source"] = source
                subagents[parsed["name"]] = parsed

    return subagents


_load_subagents_from_dir = _scan_subagents_dir


def get_built_in_subagents() -> list[SubagentMetadata]:
    """Return built-in subagents.

    In OpsCloud, there are no static built-in subagents; all dynamic agents
    are provided via Agent Plugins, project definitions, or user configuration.
    This function returns an empty list for backward compatibility.
    """
    return []


def list_subagents(
    *,
    user_agents_dir: Path | None = None,
    project_agents_dir: Path | None = None,
    project_root: Path | None = None,
    include_plugins: bool = True,
    include_builtin: bool = False,  # noqa: ARG001 - kept for backward compatibility
    store: Any = None,  # noqa: ARG001 - kept for backward compatibility
) -> list[SubagentMetadata]:
    """List subagents from user directories, project directories, and agent plugins.

    Precedence order:
    1. User subagents (lowest precedence)
    2. Project subagents (override user subagents of the same name)
    3. Plugin subagents (namespaced by plugin_id, e.g. ``my-plugin@marketplace:subagent``)

    Args:
        user_agents_dir: User-level agents directory (defaults to ``~/.opscloud/agents``).
        project_agents_dir: Project-level agents directory (defaults to ``{project_root}/.opscloud/agents``).
        project_root: Active project worktree root directory.
        include_plugins: Whether to discover agent plugins from the project/marketplace.
        include_builtin: Kept for compatibility (always empty in OpsCloud).
        store: Kept for compatibility.

    Returns:
        List of resolved SubagentMetadata instances.
    """
    discovered: dict[str, SubagentMetadata] = {}

    # 1. User subagents (defaults to ~/.opscloud/agents/ and ~/.agents/)
    resolved_user_dir = user_agents_dir or paths.user_agents_dir()
    if resolved_user_dir.is_dir():
        discovered.update(_scan_subagents_dir(resolved_user_dir, source="user"))

    # Also check ~/.agents/ if different
    shared_agents_dir = paths.AGENTS_SHARED_DIR / "agents"
    if shared_agents_dir.is_dir() and shared_agents_dir != resolved_user_dir:
        discovered.update(_scan_subagents_dir(shared_agents_dir, source="user-shared"))

    # 2. Project subagents (take precedence over user subagents)
    resolved_proj_dir = project_agents_dir
    if resolved_proj_dir is None and project_root is not None:
        candidate = paths.project_agents_dir(project_root)
        if candidate.is_dir():
            resolved_proj_dir = candidate
        elif (project_root / ".agents").is_dir():
            resolved_proj_dir = project_root / ".agents"
        elif (project_root / "agents").is_dir():
            resolved_proj_dir = project_root / "agents"

    if resolved_proj_dir is not None and resolved_proj_dir.is_dir():
        discovered.update(_scan_subagents_dir(resolved_proj_dir, source="project"))

    # 3. Dynamic Agent Plugins (marketplace & project-local plugins)
    if include_plugins:
        try:
            from opscloud.plugins.adapters.agents import discover_plugin_subagents

            plugin_metas = discover_plugin_subagents(project_root=project_root)
            for p_meta in plugin_metas:
                sub_name = p_meta.get("name")
                if sub_name:
                    discovered[sub_name] = p_meta
        except Exception as exc:
            logger.debug("Plugin subagents discovery encountered non-fatal error: %s", exc)

    return list(discovered.values())


async def list_subagents_async(
    *,
    user_agents_dir: Path | None = None,
    project_agents_dir: Path | None = None,
    project_root: Path | None = None,
    include_plugins: bool = True,
    include_builtin: bool = False,
    store: Any = None,
) -> list[SubagentMetadata]:
    """Async wrapper around list_subagents for backward compatibility."""
    return list_subagents(
        user_agents_dir=user_agents_dir,
        project_agents_dir=project_agents_dir,
        project_root=project_root,
        include_plugins=include_plugins,
        include_builtin=include_builtin,
        store=store,
    )


def load_async_subagents(
    config_path: Path | None = None,
    *,
    store: Any = None,  # noqa: ARG001 - kept for backward compatibility
) -> list[AsyncSubAgent]:
    """Load async subagent definitions from config.toml.

    Reads the ``[async_subagents]`` section where each sub-table defines a remote
    LangGraph deployment to expose as an async subagent tool:

    ```toml
    [async_subagents.researcher]
    description = "Research complex topics on the web"
    graph_id = "researcher"
    url = "http://localhost:8123"
    ```
    """
    agents: list[AsyncSubAgent] = []
    resolved_config_path = config_path or paths.CONFIG_PATH

    if not resolved_config_path or not resolved_config_path.exists():
        return agents

    try:
        import tomllib

        with resolved_config_path.open("rb") as f:
            data = tomllib.load(f)

        section = data.get("async_subagents")
        if isinstance(section, dict):
            required = {"description", "graph_id"}
            for name, spec in section.items():
                if not isinstance(spec, dict):
                    continue
                if not required.issubset(spec.keys()):
                    continue
                agent_entry: AsyncSubAgent = {
                    "name": name,
                    "description": str(spec["description"]),
                    "graph_id": str(spec["graph_id"]),
                }
                if "url" in spec and isinstance(spec["url"], str):
                    agent_entry["url"] = os.path.expandvars(spec["url"])
                if "headers" in spec and isinstance(spec["headers"], dict):
                    agent_entry["headers"] = {k: os.path.expandvars(str(v)) for k, v in spec["headers"].items()}
                agents.append(agent_entry)
    except Exception as exc:
        logger.debug("Could not read async subagents from %s: %s", resolved_config_path, exc)

    return agents


async def load_async_subagents_async(
    config_path: Path | None = None,
    *,
    store: Any = None,
) -> list[AsyncSubAgent]:
    """Async wrapper around load_async_subagents."""
    return load_async_subagents(config_path=config_path, store=store)


__all__ = [
    "SubagentMetadata",
    "get_built_in_subagents",
    "list_subagents",
    "list_subagents_async",
    "load_async_subagents",
    "load_async_subagents_async",
    "parse_subagent_file",
]
