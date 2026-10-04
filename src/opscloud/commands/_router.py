"""Command router / dispatcher — central command execution hub."""

from __future__ import annotations

import importlib
import inspect
from opscloud.utils.logger import get_logger
import pkgutil
from typing import TYPE_CHECKING, Any

from opscloud.commands._base import BaseCommandHandler, CommandResult
from opscloud.commands._guards import require_confirmation
from opscloud.commands._types import SafetyLevel

if TYPE_CHECKING:
    from opscloud.commands._base import CommandContext

logger = get_logger(__name__)


class CommandRouter:
    """Central dispatcher mapping slash command strings to handler instances."""

    def __init__(self) -> None:
        self._handlers: dict[str, BaseCommandHandler] = {}
        self._discovered: bool = False

    def register(self, handler: BaseCommandHandler) -> None:
        """Register a handler for its primary name and all aliases."""
        canonical_name = handler.name.lower().strip()
        self._handlers[canonical_name] = handler
        for alias in handler.aliases:
            alias_name = alias.lower().strip()
            self._handlers[alias_name] = handler

    def get_handler(self, command_name: str) -> BaseCommandHandler | None:
        """Look up handler instance by command name or alias.

        Supports exact match first, then prefix match for ``/skill:*`` commands.
        """
        clean_name = command_name.lower().strip()
        handler = self._handlers.get(clean_name)
        if handler is not None:
            return handler

        # Prefix match: /skill:<name> → handler registered as "/skill:"
        for prefix, h in self._handlers.items():
            if prefix.endswith(":") and clean_name.startswith(prefix):
                return h

        return None

    def auto_discover(self) -> None:
        """Import command sub-packages and auto-register all concrete BaseCommandHandler subclasses."""
        if self._discovered:
            return

        sub_packages = [
            "opscloud.commands.core",
            "opscloud.commands.power",
            "opscloud.commands.devops",
        ]

        for pkg_name in sub_packages:
            try:
                pkg = importlib.import_module(pkg_name)
                # Crawl modules in sub-package if package path exists
                if hasattr(pkg, "__path__"):
                    for _, mod_name, _ in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg_name}."):
                        try:
                            mod = importlib.import_module(mod_name)
                            self._register_handlers_from_module(mod)
                        except Exception as exc:
                            logger.warning("Failed importing command module %s: %s", mod_name, exc)
                self._register_handlers_from_module(pkg)
            except ImportError:
                logger.debug("Command sub-package %s not yet available", pkg_name)

        # Discover and register plugin commands
        try:
            from opscloud.plugins.adapters.commands import discover_plugin_commands

            for plugin_handler in discover_plugin_commands():
                self.register(plugin_handler)
        except Exception as exc:
            logger.debug("Plugin command discovery skipped in router: %s", exc)

        self._discovered = True

    def _register_handlers_from_module(self, module: Any) -> None:
        """Find and register all concrete BaseCommandHandler subclasses in module."""
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseCommandHandler) and obj is not BaseCommandHandler and not inspect.isabstract(obj):
                try:
                    handler_instance = obj()
                    self.register(handler_instance)
                except Exception as exc:
                    logger.warning("Could not instantiate command handler %s: %s", obj.__name__, exc)

    async def dispatch(self, command: str, ctx: CommandContext) -> CommandResult:
        """Parse command, validate, evaluate safety gate, and execute handler.

        Dispatch flow:
          1. Extract command name (first token).
          2. Look up handler (exact match, then prefix match for ``/skill:``).
          3. Apply safety gate for high-risk commands.
          4. Validate and execute.
        """
        clean = command.strip()
        if not clean:
            return CommandResult(success=False, message="Empty command.")

        cmd_name = clean.split()[0].lower()
        handler = self.get_handler(cmd_name)

        if handler is None:
            return CommandResult(success=False, message=f"Unknown command: {cmd_name}")

        # Safety Gate for High Risk and Destructive commands
        if handler.safety_level in (SafetyLevel.HIGH_RISK, SafetyLevel.DESTRUCTIVE):
            approved = await require_confirmation(handler, ctx)
            if not approved:
                return CommandResult(success=False, message="Cancelled by user.")

        # Pre-execution Validation
        validation_error = handler.validate(ctx)
        if validation_error:
            return CommandResult(success=False, message=validation_error)

        # Execution
        try:
            return await handler.execute(ctx)
        except Exception as exc:
            logger.exception("Command %s execution failed", cmd_name)
            return CommandResult(
                success=False,
                message=f"Command failed: {type(exc).__name__}: {exc}",
            )

    @property
    def all_handlers(self) -> dict[str, BaseCommandHandler]:
        """Return shallow copy of all registered command handlers."""
        return dict(self._handlers)


__all__ = ["CommandRouter"]
