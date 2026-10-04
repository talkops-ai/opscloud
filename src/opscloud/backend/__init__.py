"""Backend module for OpsCloud — shell execution, sandbox environments, and filesystem access."""

from __future__ import annotations

from typing import Any

from opscloud.backend.registry import (
    BackendRegistry,
    get_backend_class,
    get_backend_registry,
    register_backend,
)

__all__ = [
    "BackendRegistry",
    "CompositeBackend",
    "LocalShellBackend",
    "OpsCloudCompositeBackend",
    "SandboxConfig",
    "cleanup_all_sandboxes",
    "cleanup_sandbox",
    "clear_sandbox_cache",
    "create_sandbox_backend",
    "get_backend_class",
    "get_backend_registry",
    "get_sandbox_backend",
    "register_backend",
]


def __getattr__(name: str) -> Any:
    """Lazy-load backend classes to avoid circular imports and unnecessary dependencies."""
    _lazy_map = {
        "LocalShellBackend": "opscloud.backend.local",
        "OpsCloudCompositeBackend": "opscloud.backend.composite",
        "CompositeBackend": "opscloud.backend.composite",
        "SandboxConfig": "opscloud.backend.sandbox_config",
        "get_sandbox_backend": "opscloud.backend.sandbox_factory",
        "create_sandbox_backend": "opscloud.backend.sandbox_factory",
        "cleanup_sandbox": "opscloud.backend.sandbox_factory",
        "cleanup_all_sandboxes": "opscloud.backend.sandbox_factory",
        "clear_sandbox_cache": "opscloud.backend.sandbox_factory",
    }
    if name in _lazy_map:
        import importlib

        mod = importlib.import_module(_lazy_map[name])
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
