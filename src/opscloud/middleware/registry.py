"""Middleware registry — singleton with ``@register_middleware`` decorator."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import threading
from typing import Any, TypeVar

from langchain.agents.middleware.types import AgentMiddleware

BaseAgentMiddleware = AgentMiddleware

T = TypeVar("T", bound=type)


class MiddlewareRegistry:
    """Registry of middleware providers. Preserves insertion order."""

    _instance: MiddlewareRegistry | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        """Initialize an empty middleware registry."""
        self._registry: dict[str, tuple[type[AgentMiddleware], dict[str, Any]]] = {}

    def register(
        self,
        name: str,
        cls: type[AgentMiddleware],
        *,
        default_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Register a middleware class under *name*."""
        self._registry[name] = (cls, default_kwargs or {})

    def get(self, name: str) -> type[AgentMiddleware] | None:
        """Return the middleware class for *name*, or ``None``."""
        entry = self._registry.get(name)
        return entry[0] if entry else None

    def list_registered(self) -> list[str]:
        """Return sorted list of registered middleware names."""
        return sorted(self._registry.keys())

    def build_stack(
        self,
        *,
        exclude: set[str] | None = None,
        **kwargs: Any,
    ) -> list[AgentMiddleware]:
        """Build the full middleware stack, preserving insertion order."""
        items = [(name, item) for name, item in self._registry.items() if not (exclude and name in exclude)]
        stack = []
        for name, (cls, default_kwargs) in items:
            inst_kwargs = {**default_kwargs, **kwargs.get(name, {})}
            stack.append(cls(**inst_kwargs))
        return stack

    def build_middleware(self, name: str, **kwargs: Any) -> AgentMiddleware:
        """Instantiate a single registered middleware by name."""
        entry = self._registry.get(name)
        if entry is None:
            raise KeyError(f"Middleware {name!r} not registered")
        cls, default_kwargs = entry
        merged = {**default_kwargs, **kwargs}
        return cls(**merged)

    @classmethod
    def get_instance(cls) -> MiddlewareRegistry:
        """Return the singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def ensure_discovered(cls) -> MiddlewareRegistry:
        """Ensure all middleware modules are imported (firing @register_middleware)."""
        inst = cls.get_instance()
        if inst._registry:
            return inst

        import importlib

        _modules = [
            "opscloud.middleware.ask_user",
            "opscloud.middleware.auto_mode",
            "opscloud.middleware.auto_mode_hitl",
            "opscloud.middleware.compaction",
            "opscloud.middleware.configurable_model",
            "opscloud.middleware.cost_tracking",
            "opscloud.middleware.glm_stall_recovery",
            "opscloud.middleware.goal_criteria",
            "opscloud.middleware.goal_state_notice",
            "opscloud.middleware.goal_tools",
            "opscloud.middleware.headless_mcp_guard",
            "opscloud.middleware.local_context",
            "opscloud.middleware.mcp_context",
            "opscloud.middleware.mcp_middleware",
            "opscloud.middleware.memory_guard",
            "opscloud.middleware.model_retry",
            "opscloud.middleware.reliable_rubric",
            "opscloud.middleware.resume_state",
            "opscloud.middleware.server_hooks",
            "opscloud.middleware.shell_allow_list",
            "opscloud.middleware.skills",
            "opscloud.middleware.subagents",
            "opscloud.middleware.tool_filter",
            "opscloud.middleware.unified_system_message",
        ]
        for mod_name in _modules:
            with contextlib.suppress(ImportError):
                importlib.import_module(mod_name)

        return inst


def get_middleware_registry() -> MiddlewareRegistry:
    """Module-level accessor for the singleton ``MiddlewareRegistry``."""
    return MiddlewareRegistry.ensure_discovered()


def register_middleware(
    name: str,
    *,
    default_kwargs: dict[str, Any] | None = None,
) -> Callable[[T], T]:
    """Decorator to register a middleware class."""

    def decorator(cls: T) -> T:
        get_middleware_registry().register(name, cls, default_kwargs=default_kwargs)  # type: ignore[arg-type]
        return cls

    return decorator
