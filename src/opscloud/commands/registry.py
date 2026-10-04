"""Command registry — facade over ``CommandRouter`` providing plugin-aware
command management, source tracking, and the public API the plugin
architecture audit expects.

This module provides ``CommandRegistry`` as a singleton that wraps
``CommandRouter`` with:

* Explicit plugin-command tracking and source attribution.
* A ``list_plugin_commands()`` method for TUI diagnostics.
* Auto-discovery on first access (lazy initialization).
* A unified ``register()`` / ``get_handler()`` / ``dispatch()`` surface.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import threading
from typing import TYPE_CHECKING, Any

from opscloud.commands._base import BaseCommandHandler, CommandResult
from opscloud.commands._router import CommandRouter

if TYPE_CHECKING:
    from opscloud.commands._base import CommandContext

logger = get_logger(__name__)


class CommandRegistry:
    """Singleton registry tracking all slash commands (built-in + plugin).

    Wraps the underlying ``CommandRouter`` and adds:

    * Source-tracking for each registered handler (``built-in`` vs ``plugin:<id>``).
    * ``list_plugin_commands()`` for TUI command palette filtering.
    * Thread-safe lazy auto-discovery on first ``get_handler`` or ``dispatch``.
    """

    _instance: CommandRegistry | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._router = CommandRouter()
        self._plugin_handlers: dict[str, list[BaseCommandHandler]] = {}
        self._sources: dict[str, str] = {}  # handler_name -> source label
        self._discovered = False

    @classmethod
    def get_instance(cls) -> CommandRegistry:
        """Return the process-wide singleton."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton — primarily for testing."""
        with cls._lock:
            cls._instance = None

    # ── Registration ─────────────────────────────────────────

    def register(
        self,
        handler: BaseCommandHandler,
        *,
        source: str = "built-in",
    ) -> None:
        """Register a command handler with source tracking.

        Args:
            handler: The command handler instance.
            source: A label such as ``"built-in"`` or ``"plugin:<plugin_id>"``.
        """
        self._router.register(handler)
        canonical = handler.name.lower().strip()
        self._sources[canonical] = source
        for alias in handler.aliases:
            self._sources[alias.lower().strip()] = source

        if source.startswith("plugin:"):
            plugin_id = source[len("plugin:"):]
            self._plugin_handlers.setdefault(plugin_id, []).append(handler)

        logger.debug(
            "[CommandRegistry] Registered %s (aliases=%s, source=%s)",
            canonical,
            handler.aliases,
            source,
        )

    def register_plugin_commands(
        self,
        handlers: list[BaseCommandHandler],
        *,
        plugin_id: str,
    ) -> None:
        """Batch-register plugin-contributed command handlers."""
        for handler in handlers:
            self.register(handler, source=f"plugin:{plugin_id}")

    # ── Discovery ────────────────────────────────────────────

    def discover(self) -> None:
        """Auto-discover built-in and plugin commands.

        Delegates to ``CommandRouter.auto_discover()`` which imports all
        command sub-packages and then calls ``discover_plugin_commands()``.
        Source tracking is added on top.
        """
        if self._discovered:
            return

        # Discover built-in commands first via the router
        self._router.auto_discover()

        # Track sources for all handlers the router discovered
        for name, handler in self._router.all_handlers.items():
            if name not in self._sources:
                # Check if this was registered by the plugin adapter
                plugin_id = getattr(handler, "_plugin_id", None)
                if plugin_id:
                    self._sources[name] = f"plugin:{plugin_id}"
                    self._plugin_handlers.setdefault(plugin_id, []).append(handler)
                else:
                    self._sources[name] = "built-in"

        self._discovered = True
        logger.debug(
            "[CommandRegistry] Discovery complete: %d commands (%d plugin)",
            len(self._router.all_handlers),
            sum(len(v) for v in self._plugin_handlers.values()),
        )

    def _ensure_discovered(self) -> None:
        """Lazy auto-discover on first access."""
        if not self._discovered:
            self.discover()

    # ── Lookup ───────────────────────────────────────────────

    def get_handler(self, command_name: str) -> BaseCommandHandler | None:
        """Look up a command handler by name or alias."""
        self._ensure_discovered()
        return self._router.get_handler(command_name)

    def get_source(self, command_name: str) -> str | None:
        """Return the source label for a command (e.g. ``'built-in'`` or ``'plugin:my-plugin'``)."""
        self._ensure_discovered()
        return self._sources.get(command_name.lower().strip())

    # ── Dispatch ─────────────────────────────────────────────

    async def dispatch(self, command: str, ctx: CommandContext) -> CommandResult:
        """Parse and dispatch a slash command through the underlying router."""
        self._ensure_discovered()
        return await self._router.dispatch(command, ctx)

    # ── Introspection ────────────────────────────────────────

    def list_plugin_commands(self) -> dict[str, list[BaseCommandHandler]]:
        """Return plugin-contributed commands grouped by plugin_id."""
        self._ensure_discovered()
        return dict(self._plugin_handlers)

    def list_all_commands(self) -> dict[str, BaseCommandHandler]:
        """Return all registered command handlers."""
        self._ensure_discovered()
        return self._router.all_handlers

    @property
    def all_handlers(self) -> dict[str, BaseCommandHandler]:
        """Alias for ``list_all_commands()`` matching ``CommandRouter``'s API."""
        return self.list_all_commands()
