"""Dynamic tool registry to register and instantiate tools."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import threading
from typing import Any

from langchain_core.tools import BaseTool


class ToolRegistry:
    """Registry to bind and configure tools dynamically."""

    _instance: ToolRegistry | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        """Initialize ToolRegistry with empty registry and category mappings."""
        self._registry: dict[str, Callable[..., BaseTool] | BaseTool] = {}
        self._categories: dict[str, str] = {}

    @classmethod
    def get_instance(cls) -> ToolRegistry:
        """Retrieve the singleton instance of ToolRegistry.

        Returns:
            ToolRegistry: The singleton tool registry instance.
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def register(
        self,
        name: str,
        factory: Callable[..., BaseTool] | BaseTool,
        category: str = "default",
    ) -> None:
        """Register a tool factory function or tool instance."""
        self._registry[name] = factory
        self._categories[name] = category

    def build_tool(self, name: str, **kwargs: Any) -> BaseTool:
        """Build and configure a registered tool."""
        if name not in self._registry:
            raise KeyError(f"Tool '{name}' is not registered in ToolRegistry.")
        entry = self._registry[name]
        if callable(entry) and not isinstance(entry, BaseTool):
            return entry(**kwargs)
        return entry

    def get(self, name: str) -> BaseTool | None:
        """Return the tool registered under name, or None."""
        try:
            return self.build_tool(name)
        except Exception:
            return None

    def list_registered(self) -> list[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._registry.keys())

    def list_by_category(self, category: str) -> list[str]:
        """Return sorted list of tools in a specific category."""
        return sorted(name for name, cat in self._categories.items() if cat == category)

    def get_categories(self) -> list[str]:
        """Return sorted list of all categories."""
        return sorted(set(self._categories.values()))

    def build_all(
        self,
        names: list[str] | None = None,
        *,
        exclude_categories: set[str] | None = None,
        include_categories: set[str] | None = None,
        **kwargs: Any,
    ) -> list[BaseTool]:
        """Build tools by name or by category filters."""
        if names is not None:
            tools: list[BaseTool] = []
            for name in names:
                with contextlib.suppress(KeyError):
                    tools.append(self.build_tool(name, **kwargs))
            return tools

        tools = []
        for name, _ in self._registry.items():
            cat = self._categories.get(name, "default")
            if exclude_categories and cat in exclude_categories:
                continue
            if include_categories and cat not in include_categories:
                continue
            with contextlib.suppress(Exception):
                tools.append(self.build_tool(name, **kwargs))
        return tools


def get_tool_registry() -> ToolRegistry:
    """Return the singleton ToolRegistry instance."""
    return ToolRegistry.get_instance()
