"""Configuration management command handler for OpsCode."""

from __future__ import annotations

from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)

# Keys whose values should be masked in display output
_SECRET_INDICATORS = {"api_key", "token", "secret", "password", "credential"}


class ConfigHandler(BaseCommandHandler):
    """Handler for /config — view and manage OpsCode configuration."""

    @property
    def name(self) -> str:
        return "/config"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.IMMEDIATE_UI

    async def execute(self, ctx: CommandContext) -> CommandResult:
        parts = ctx.args.strip().split(maxsplit=2)
        sub = parts[0].lower() if parts and parts[0] else "show"

        if sub == "path":
            return self._show_path(ctx)
        if sub == "show":
            return self._show_config(ctx)
        if sub == "set" and len(parts) >= 3:
            return self._set_config(ctx, parts[1], parts[2])
        if sub == "reset" and len(parts) >= 2:
            return self._reset_config(ctx, parts[1])
        return CommandResult(
            success=False,
            message="Usage: /config [show|set <key> <value>|reset <key>|path]",
        )

    def _get_settings(self, ctx: CommandContext):
        if ctx.settings is not None:
            return ctx.settings
        from opscloud.config.settings import settings
        return settings

    def _show_path(self, ctx: CommandContext) -> CommandResult:
        settings = self._get_settings(ctx)
        if settings is None:
            return CommandResult(success=False, message="Settings not available.")
        path = settings.config_path
        return CommandResult(success=True, message=f"**Config file:** `{path}`")

    def _show_config(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None:
            from opscloud.ui.widgets.config_manager import ConfigManagerScreen
            settings = self._get_settings(ctx)
            screen = ConfigManagerScreen(settings=settings)
            ctx.app.push_screen(screen)
            return CommandResult(success=True, message="", mount_as_app_message=False)

        # Inline fallback: use the manifest for a grouped, source-aware display
        settings = self._get_settings(ctx)
        if settings is None:
            return CommandResult(success=False, message="Settings not available.")

        from opscloud.config.manifest import get_config_options, resolve_scalar

        lines = ["⚙️ **Current Configuration:**\n"]
        current_group = None
        for opt in get_config_options():
            if opt.group != current_group:
                current_group = opt.group
                lines.append(f"\n**{current_group}:**")
            value, source = resolve_scalar(opt, settings=settings)
            if opt.redacted:
                val_str = self._mask_secret(opt.key, str(value)) if value else "`not set`"
            else:
                val_str = f"`{value}`" if value is not None else "`None`"
            source_tag = f" [{source}]" if source != "default" else ""
            lines.append(f"  `{opt.key}`: {val_str}{source_tag}")
        return CommandResult(
            success=True,
            message="\n".join(lines),
        )

    def _set_config(self, ctx: CommandContext, key: str, value: str) -> CommandResult:
        settings = self._get_settings(ctx)
        if settings is None:
            return CommandResult(success=False, message="Settings not available.")
        ok, msg = settings.set_field(key, value)
        return CommandResult(success=ok, message=msg)

    def _reset_config(self, ctx: CommandContext, key: str) -> CommandResult:
        settings = self._get_settings(ctx)
        if settings is None:
            return CommandResult(success=False, message="Settings not available.")
        ok, msg = settings.reset_field(key)
        return CommandResult(success=ok, message=msg)

    @staticmethod
    def _mask_secret(key: str, value: str) -> str:
        """Mask values for keys containing secret indicators."""
        if any(s in key.lower() for s in _SECRET_INDICATORS) and len(value) > 8:
            return f"`{value[:4]}...{value[-4:]}`"
        return f"`{value}`"


__all__ = ["ConfigHandler"]
