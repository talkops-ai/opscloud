"""Plugin adapters package for K8s Autopilot."""

from opscloud.plugins.adapters.skills import (
    namespaced_skill_name,
    plugin_skill_roots,
    plugin_skill_sources,
)

__all__ = [
    "namespaced_skill_name",
    "plugin_skill_roots",
    "plugin_skill_sources",
]
