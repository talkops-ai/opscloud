"""Discovers and parses MCP server configs from files with clear precedence.

Pure file-based discovery (no database dependency):
Precedence (lowest to highest):
1. Global user configs (~/.agents/mcp.json, ~/.agents/.mcp.json)
2. User configs (~/.opscloud/mcp.json, ~/.opscloud/.mcp.json)
3. Plugin-declared MCP configs
4. Project configs (e.g. {project_root}/.opscloud/mcp.json, {project_root}/.mcp.json)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


class MCPServerConfig(TypedDict, total=False):
    """Configuration for a discovered MCP server (command, args, env, transport)."""

    command: str | None
    args: list[str] | None
    env: dict[str, str] | None
    url: str | None
    transport: str | None
    headers: dict[str, str] | None
    source: str | None
    auth_token_env_var: str | None
    disabled_tools: list[str] | None
    allowed_tools: list[str] | None
    enabled: bool
    trusted: bool


def _load_mcp_json(path: Path) -> dict[str, dict[str, Any]]:
    """Safely load and parse an .mcp.json file into a dictionary of server configs."""
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}

        servers = data.get("mcpServers") or data.get("mcp_servers")
        if servers is None and ("command" in data or "url" in data or "serverUrl" in data):
            # Single server definition (fallback)
            servers = {path.stem: data}
        elif servers is None:
            # Check if root dict is already mapping name -> config
            if data and all(
                isinstance(v, dict) and ("command" in v or "url" in v or "serverUrl" in v)
                for v in data.values()
            ):
                servers = data

        if isinstance(servers, dict):
            return servers
    except Exception as exc:
        logger.warning("Failed to parse MCP config at %s: %s", path, exc)
    return {}


def _parse_server_config(
    name: str, config: dict[str, Any], source_name: str
) -> MCPServerConfig:
    """Normalize a raw server config dictionary into MCPServerConfig."""
    url = config.get("url") or config.get("serverUrl")
    command = config.get("command")
    transport = config.get("transport") or config.get("type")
    if not transport:
        transport = ("sse" if "sse" in str(url).lower() else "http") if url else "stdio"

    # Enabled status
    enabled = True
    if "disabled" in config:
        enabled = not bool(config["disabled"])
    elif "enabled" in config:
        enabled = bool(config["enabled"])

    raw_disabled = config.get("disabled_tools") or config.get("disabledTools") or []
    raw_allowed = config.get("allowed_tools") or config.get("allowedTools") or []

    disabled_tools = list(raw_disabled) if isinstance(raw_disabled, (list, tuple)) else []
    allowed_tools = list(raw_allowed) if isinstance(raw_allowed, (list, tuple)) else []

    trusted = bool(config.get("trusted", source_name != "project"))

    return MCPServerConfig(
        command=command,
        args=list(config.get("args") or []),
        env=dict(config.get("env") or {}),
        url=url,
        transport=transport,
        headers=dict(config.get("headers") or {}),
        source=source_name,
        auth_token_env_var=config.get("auth_token_env_var"),
        disabled_tools=disabled_tools,
        allowed_tools=allowed_tools,
        enabled=enabled,
        trusted=trusted,
    )


class MCPDiscovery:
    """Discovers and parses MCP server configs from filesystem sources."""

    def __init__(self, store: Any = None) -> None:
        """Initialize MCPDiscovery.
        
        Args:
            store: Kept for backwards compatibility; not used in terminal file-based model.
        """
        self._store = store

    def discover(self, project_root: Path | None = None) -> dict[str, MCPServerConfig]:
        """Discover and merge MCP server configs across global, user, plugin, and project files.

        Precedence (lowest to highest):
          1. Global configs (~/.agents/mcp.json)
          2. User configs (~/.opscloud/.mcp.json)
          3. Plugin-declared configs
          4. Project configs (project root .mcp.json)
        """
        from opscloud.config import paths

        effective_project_root = project_root or Path.cwd()

        # Build candidate file list in lowest -> highest precedence order
        candidates: list[tuple[Path, str]] = []

        # 1. Global user configs (~/.agents/)
        candidates.append((paths.AGENTS_SHARED_DIR / "mcp.json", "global"))
        candidates.append((paths.AGENTS_SHARED_DIR / ".mcp.json", "global"))

        # 2. User data dir configs (~/.opscloud/)
        candidates.append((paths.DATA_DIR / "mcp.json", "user"))
        candidates.append((paths.GLOBAL_MCP_PATH, "user"))

        merged_servers: dict[str, MCPServerConfig] = {}

        # Load global and user configs first
        for path, source_name in candidates:
            servers = _load_mcp_json(path)
            for name, config in servers.items():
                if isinstance(config, dict):
                    clean_config = _parse_server_config(name, config, source_name)
                    if name not in merged_servers:
                        merged_servers[name] = clean_config
                    else:
                        merged_servers[name] = {**merged_servers[name], **clean_config}

        # 3. Discover plugin-declared MCP servers
        try:
            from opscloud.plugins.adapters.mcp import discover_plugin_mcp_configs

            plugin_configs = discover_plugin_mcp_configs(project_dir=effective_project_root)
            if isinstance(plugin_configs, dict):
                for name, config in plugin_configs.items():
                    if isinstance(config, dict):
                        clean_config = _parse_server_config(name, config, "plugin")
                        if name not in merged_servers:
                            merged_servers[name] = clean_config
                        else:
                            merged_servers[name] = {**merged_servers[name], **clean_config}
        except Exception as exc:
            logger.debug("Could not discover plugin MCP configs: %s", exc)

        # 3b. Local workspace plugins (.mcp.json in plugins/ directories)
        plugins_dir = effective_project_root / "plugins"
        if plugins_dir.is_dir():
            for plugin in sorted(plugins_dir.iterdir()):
                if plugin.is_dir() and not plugin.name.startswith("."):
                    for candidate in (plugin / ".mcp.json", plugin / "mcp.json"):
                        if candidate.is_file():
                            for name, config in _load_mcp_json(candidate).items():
                                if isinstance(config, dict):
                                    clean_config = _parse_server_config(
                                        name, config, f"plugin:{plugin.name}"
                                    )
                                    if name not in merged_servers:
                                        merged_servers[name] = clean_config
                                    else:
                                        merged_servers[name] = {**merged_servers[name], **clean_config}

        # 4. Project configs (lowest to highest project precedence)
        # Note: paths.project_mcp_paths lists [.mcp.json, mcp.json, .opscloud/.mcp.json, .opscloud/mcp.json]
        # Reversing them means .mcp.json in the project root is loaded last and wins.
        project_candidate_paths = list(reversed(paths.project_mcp_paths(effective_project_root)))
        for path in project_candidate_paths:
            servers = _load_mcp_json(path)
            for name, config in servers.items():
                if isinstance(config, dict):
                    clean_config = _parse_server_config(name, config, "project")
                    if name not in merged_servers:
                        merged_servers[name] = clean_config
                    else:
                        merged_servers[name] = {**merged_servers[name], **clean_config}

        return merged_servers

    async def discover_and_sync_async(
        self, project_root: Path | None = None, store: Any = None
    ) -> dict[str, MCPServerConfig]:
        """Asynchronous discover entrypoint for compatibility.
        
        Discovers configs from disk and registers them with MCPSessionManager.
        """
        results = self.discover(project_root=project_root)
        try:
            from opscloud.mcp.session_manager import MCPSessionManager

            MCPSessionManager.get_instance(results, purge_missing=False)
        except Exception as e:
            logger.debug("Failed to sync discovered configs to MCPSessionManager: %s", e)
        return results


def discover_mcp_configs(project_root: Path | None = None) -> dict[str, MCPServerConfig]:
    """Discover active MCP server configs from disk."""
    return MCPDiscovery().discover(project_root)
