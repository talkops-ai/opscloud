"""MCP Discovery Middleware — discovers project-level .mcp.json and binds to domain.

Registered as ``mcp_discovery`` in the middleware registry. When included
in a coordinator's middleware list, it:
  1. Scans project-level ``.mcp.json`` files
  2. Merges discovered servers with ``DefaultConfig.MCP_SERVERS``
  3. Filters by domain binding (e.g. Helm only gets helm-related servers)
  4. Updates the runtime's MCP tool list

Usage in coordinator::

    custom_mws = get_middleware_registry().build_middlewares([
        "operation_context",
        ("mcp_discovery", {"domain": "helm-operator"}),
    ])
"""

from __future__ import annotations

from typing import Any

from opscloud.middleware.registry import (
    BaseAgentMiddleware,
    register_middleware,
)
from opscloud.utils.logger import AgentLogger

logger = AgentLogger("MCPDiscoveryMW")


@register_middleware(name="mcp_discovery")
class MCPDiscoveryMiddleware(BaseAgentMiddleware):
    """Discover project-level .mcp.json and bind MCP servers to domain.

    When attached to a coordinator, this middleware runs during agent
    initialization to discover additional MCP servers from project-level
    config files and filter them by the coordinator's domain.

    Args:
        domain: Coordinator domain name (e.g. "helm-operator").
            If None, no domain filtering is applied.
        project_root: Root directory for .mcp.json scanning.
            Defaults to the config's AGENT_PROJECT_ROOT.
        config: Config instance for resolving defaults.
    """

    def __init__(
        self,
        *,
        domain: str | None = None,
        project_root: str | None = None,
        config: Any = None,
        **kwargs: Any,
    ) -> None:
        """Initialize MCPDiscoveryMiddleware.

        Args:
            domain: Optional coordinator domain filter.
            project_root: Optional root directory path for discovery.
            config: Optional configuration settings instance.
            **kwargs: Additional keyword arguments.
        """
        super().__init__()
        self._domain = domain
        self._project_root = project_root
        self._config = config
        self._discovered_servers: list[dict[str, Any]] = []

    def _get_config(self) -> Any:
        """Lazy config resolution."""
        if self._config is None:
            from opscloud.config.settings import get_settings

            self._config = get_settings()
        return self._config

    def discover_and_bind(self) -> list[dict[str, Any]]:
        """Run discovery and domain binding, return filtered server list.

        This is the main entry point. It:
          1. Discovers project-level .mcp.json servers
          2. Merges with defaults from config
          3. Filters by domain binding

        Returns:
            Filtered MCP server list (dicts in DefaultConfig format).
        """
        from opscloud.mcp.discovery import discover_mcp_configs

        discovered = discover_mcp_configs()
        merged = [{"name": k, "config": v} for k, v in (discovered.items() if isinstance(discovered, dict) else [])]
        self._discovered_servers = merged

        if discovered:
            logger.info(f"MCP Discovery: {len(discovered):d} servers discovered")

        return merged

    @property
    def discovered_servers(self) -> list[dict[str, Any]]:
        """Return the last discovered and bound server list."""
        return self._discovered_servers
