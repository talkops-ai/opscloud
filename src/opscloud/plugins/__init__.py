"""OpsCloud Plugin System.

Provides plugin discovery, marketplace management, and plugin protocol
for extending OpsCloud with new skills, subagents, MCP servers, and capabilities.
All state is stored purely in the local filesystem (~/.opscloud/ and .opscloud/).
"""

from __future__ import annotations

import importlib.metadata
from typing import Any, Protocol

from opscloud.utils.logger import get_logger

from opscloud.plugins.adapters.skills import (
    namespaced_skill_name,
    plugin_skill_roots,
    plugin_skill_sources,
)
from opscloud.plugins.commands_cli import (
    execute_plugin_command,
    setup_plugin_parser,
)
from opscloud.config import (
    DEFAULT_MARKETPLACE_CONFIGS,
    DefaultMarketplaceConfig,
    fetch_marketplace_manifest,
    get_default_marketplace_config,
    is_default_marketplace,
)
from opscloud.plugins.discovery import (
    add_local_marketplace,
    add_marketplace_source,
    discover_plugins as discover_marketplace_plugins,
    install_plugin,
    list_available_plugins,
    list_installed_plugin_ids,
    refresh_all_marketplaces,
    refresh_marketplace,
    remove_marketplace,
    set_installed_plugin_enabled,
    uninstall_plugin,
)
from opscloud.plugins.manifest import (
    PluginManifestError,
    build_inventory,
    find_manifest_path,
    load_manifest,
)
from opscloud.plugins.marketplace import (
    MarketplaceError,
    find_marketplace_manifest,
    load_marketplace,
    load_marketplace_location,
    materialize_marketplace_source,
    materialize_plugin_source,
    parse_marketplace_source,
)
from opscloud.plugins.models import (
    ComponentInventory,
    InstalledPluginEntry,
    MarketplacePluginEntry,
    MarketplaceRecord,
    PluginDiscoveryResult,
    PluginInstance,
    PluginManifest,
    PluginMarketplace,
    PluginSource,
    split_plugin_id,
)
from opscloud.plugins.store import (
    PluginNotFoundError,
    cache_and_register_plugin,
    load_all_enabled_plugin_ids,
    load_installed_plugins,
    load_marketplace_records,
    remove_plugin_enabled_for_scope,
    versioned_cache_path,
)

logger = get_logger(__name__)


class OpsCloudPlugin(Protocol):
    """Protocol interface that external python plugins may implement."""

    name: str

    def register_commands(self) -> list[Any]:
        """Return custom slash commands registered by this plugin."""
        ...

    def register_tools(self) -> list[Any]:
        """Return custom tools registered by this plugin."""
        ...

    def register_renderers(self) -> dict[str, Any]:
        """Return custom tool renderers registered by this plugin."""
        ...

    def get_theme_overrides(self) -> dict[str, str] | None:
        """Return custom theme overrides if provided."""
        ...


class TerraformPlugin:
    """Built-in plugin for Terraform infrastructure tooling."""

    name = "terraform"

    def register_commands(self) -> list[Any]:
        return []

    def register_tools(self) -> list[Any]:
        return []

    def register_renderers(self) -> dict[str, Any]:
        return {}

    def get_theme_overrides(self) -> dict[str, str] | None:
        return None


def discover_plugins() -> list[OpsCloudPlugin]:
    """Discover and return all active Python plugins (built-in + entrypoints)."""
    plugins: list[OpsCloudPlugin] = [TerraformPlugin()]
    try:
        eps = importlib.metadata.entry_points(group="opscloud.plugins")
        for ep in eps:
            try:
                plugin_cls = ep.load()
                plugins.append(plugin_cls())
            except Exception as e:
                logger.warning("Failed to load plugin entry point %s: %s", ep.name, e)
    except Exception:
        pass
    return plugins


__all__ = [
    "ComponentInventory",
    "DEFAULT_MARKETPLACE_CONFIGS",
    "DefaultMarketplaceConfig",
    "InstalledPluginEntry",
    "MarketplaceError",
    "MarketplacePluginEntry",
    "MarketplaceRecord",
    "OpsCloudPlugin",
    "PluginDiscoveryResult",
    "PluginInstance",
    "PluginManifest",
    "PluginManifestError",
    "PluginMarketplace",
    "PluginNotFoundError",
    "PluginSource",
    "TerraformPlugin",
    "add_local_marketplace",
    "add_marketplace_source",
    "build_inventory",
    "cache_and_register_plugin",
    "discover_marketplace_plugins",
    "discover_plugins",
    "execute_plugin_command",
    "fetch_marketplace_manifest",
    "find_manifest_path",
    "find_marketplace_manifest",
    "get_default_marketplace_config",
    "install_plugin",
    "is_default_marketplace",
    "list_available_plugins",
    "list_installed_plugin_ids",
    "load_all_enabled_plugin_ids",
    "load_installed_plugins",
    "load_manifest",
    "load_marketplace",
    "load_marketplace_location",
    "load_marketplace_records",
    "materialize_marketplace_source",
    "materialize_plugin_source",
    "namespaced_skill_name",
    "parse_marketplace_source",
    "plugin_skill_roots",
    "plugin_skill_sources",
    "refresh_all_marketplaces",
    "refresh_marketplace",
    "remove_marketplace",
    "remove_plugin_enabled_for_scope",
    "set_installed_plugin_enabled",
    "setup_plugin_parser",
    "split_plugin_id",
    "uninstall_plugin",
    "versioned_cache_path",
]
