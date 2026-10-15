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

        # Discover and register zero-code custom commands (.opscloud/commands, .claude/commands, etc.)
        self.reload_custom_commands()

        self._discovered = True

    def reload_custom_commands(self) -> None:
        """Discover and register zero-code custom commands without overwriting built-in commands."""
        try:
            from opscloud.commands.custom.handler import (
                CustomCommandHandler,
                discover_custom_command_handlers,
            )

            # Remove previously registered CustomCommandHandler instances prior to reload
            stale_keys = [
                k for k, h in self._handlers.items() if isinstance(h, CustomCommandHandler)
            ]
            for k in stale_keys:
                self._handlers.pop(k, None)

            for custom_handler in discover_custom_command_handlers():
                canonical = custom_handler.name.lower().strip()
                if canonical not in self._handlers:
                    self._handlers[canonical] = custom_handler
                for alias in custom_handler.aliases:
                    alias_key = alias.lower().strip()
                    if alias_key not in self._handlers:
                        self._handlers[alias_key] = custom_handler
        except Exception as exc:
            logger.debug("Custom command discovery skipped in router: %s", exc)

    def resolve_command(
        self, raw_command: str
    ) -> tuple[BaseCommandHandler | None, str, str]:
        """Resolve a raw `/command ...` string with space-to-colon namespace fallback.

        Returns `(handler, resolved_cmd_name, remaining_args)`.
        """
        clean = raw_command.strip()
        if not clean:
            return None, "", ""

        parts = clean.split(maxsplit=1)
        cmd_name = parts[0].lower()
        remaining = parts[1].strip() if len(parts) > 1 else ""

        handler = self.get_handler(cmd_name)
        if handler is not None:
            return handler, cmd_name, remaining

        # Space-to-colon fallback: `/prefix sub arg` -> `/prefix:sub` with `arg`
        candidate_prefix = cmd_name
        tail = remaining
        while tail:
            sub_parts = tail.split(maxsplit=1)
            sub_tok = sub_parts[0].lower()
            rest = sub_parts[1].strip() if len(sub_parts) > 1 else ""
            candidate_prefix = f"{candidate_prefix}:{sub_tok}"
            matched = self.get_handler(candidate_prefix)
            if matched is not None:
                return matched, candidate_prefix, rest
            tail = rest

        return None, cmd_name, remaining

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
          1. Check for stacked custom prompt commands (`/cmd1 /cmd2 args`, up to 5).
          2. Extract command name with space-to-colon fallback (`/ns sub` -> `/ns:sub`).
          3. Apply safety gate for high-risk commands.
          4. Validate and execute.
        """
        from dataclasses import replace

        clean = command.strip()
        if not clean:
            return CommandResult(success=False, message="Empty command.")

        # Check for stacked custom prompt commands (`/cmd1 /cmd2 args`)
        try:
            from opscloud.commands.custom.expansion import peel_stacked_prompt_commands
            from opscloud.commands.custom.handler import (
                CustomCommandHandler,
                execute_stacked_custom_commands,
            )

            def _lookup_custom_def(name: str):
                key = f"/{name.lstrip('/').lower()}"
                h = self._handlers.get(key)
                if isinstance(h, CustomCommandHandler):
                    return h.definition
                return None

            stacked = peel_stacked_prompt_commands(clean, _lookup_custom_def)
            if len(stacked) > 1:
                return await execute_stacked_custom_commands(stacked, ctx)
        except Exception as exc:
            logger.debug("Stacked command check skipped: %s", exc)

        handler, cmd_name, resolved_args = self.resolve_command(clean)

        if handler is None:
            skill_handler = self._handlers.get("/skill:")
            resolve_skill_fn = getattr(skill_handler, "_resolve_skill", None)
            if callable(resolve_skill_fn):
                try:
                    if resolve_skill_fn(ctx, cmd_name.lstrip("/")):
                        handler = skill_handler
                except Exception:
                    pass

        if handler is None:
            return CommandResult(success=False, message=f"Unknown command: {cmd_name}")

        if resolved_args != getattr(ctx, "args", ""):
            try:
                ctx = replace(ctx, args=resolved_args)
            except Exception:
                pass

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


def discover_command_handlers() -> tuple[BaseCommandHandler, ...]:
    """Discover and return all built-in concrete BaseCommandHandler instances."""
    sub_packages = [
        "opscloud.commands.core",
        "opscloud.commands.power",
    ]
    handlers: dict[str, BaseCommandHandler] = {}
    for pkg_name in sub_packages:
        try:
            pkg = importlib.import_module(pkg_name)
            if hasattr(pkg, "__path__"):
                for _, mod_name, _ in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg_name}."):
                    try:
                        mod = importlib.import_module(mod_name)
                        for _, obj in inspect.getmembers(mod, inspect.isclass):
                            if (
                                issubclass(obj, BaseCommandHandler)
                                and obj is not BaseCommandHandler
                                and not inspect.isabstract(obj)
                            ):
                                try:
                                    inst = obj()
                                    canonical = inst.name.lower().strip()
                                    if canonical not in handlers:
                                        handlers[canonical] = inst
                                except Exception as exc:
                                    logger.warning("Could not instantiate handler %s: %s", obj.__name__, exc)
                    except Exception as exc:
                        logger.warning("Failed importing module %s: %s", mod_name, exc)
            for _, obj in inspect.getmembers(pkg, inspect.isclass):
                if (
                    issubclass(obj, BaseCommandHandler)
                    and obj is not BaseCommandHandler
                    and not inspect.isabstract(obj)
                ):
                    try:
                        inst = obj()
                        canonical = inst.name.lower().strip()
                        if canonical not in handlers:
                            handlers[canonical] = inst
                    except Exception:
                        pass
        except Exception:
            pass

    return tuple(handlers.values())


__all__ = ["CommandRouter", "discover_command_handlers"]
