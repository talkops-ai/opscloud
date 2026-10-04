"""Plugin protocol and data models for K8s Autopilot."""

from __future__ import annotations

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
    namespaced_skill_name,
    split_plugin_id,
)

__all__ = [
    "ComponentInventory",
    "InstalledPluginEntry",
    "MarketplacePluginEntry",
    "MarketplaceRecord",
    "PluginDiscoveryResult",
    "PluginInstance",
    "PluginManifest",
    "PluginMarketplace",
    "PluginSource",
    "namespaced_skill_name",
    "split_plugin_id",
]
