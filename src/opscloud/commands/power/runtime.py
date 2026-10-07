"""Runtime command handlers for opscloud (/reload, /restart, /install, /update, /auto-update)."""

from __future__ import annotations

import asyncio
from opscloud.utils.logger import get_logger
import subprocess
from pathlib import Path

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

logger = get_logger(__name__)


# ── /reload ──────────────────────────────────────────────


class ReloadHandler(BaseCommandHandler):
    """Handler for /reload — reload environment and configuration."""

    @property
    def name(self) -> str:
        return "/reload"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        app = ctx.app
        if app is not None and hasattr(app, "_schedule_reload"):
            app._schedule_reload()
            return CommandResult(success=True, message=None, mount_as_app_message=False)
        if app is not None and hasattr(app, "_invoke_reload"):
            await app._invoke_reload(command=ctx.raw_command)
            return CommandResult(success=True, message=None, mount_as_app_message=False)

        # Fallback for CLI/test context
        _reload_dotenv()
        changes = ctx.settings.reload_from_environment() if ctx.settings is not None else []
        try:
            from opscloud.model.config import clear_caches
            clear_caches()
        except Exception:
            pass
        if changes:
            msg = "Configuration reloaded. Changes:\n" + "\n".join(f"  - {c}" for c in changes)
        else:
            msg = "Configuration reloaded. No changes detected."
        return CommandResult(success=True, message=msg, mount_as_app_message=True)


# ── /restart ─────────────────────────────────────────────


class RestartHandler(BaseCommandHandler):
    """Handler for /restart — restart the agent server."""

    @property
    def name(self) -> str:
        return "/restart"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.ALWAYS

    async def execute(self, ctx: CommandContext) -> CommandResult:
        app = ctx.app
        if app is not None and hasattr(app, "_handle_restart_command"):
            await app._handle_restart_command(command=ctx.raw_command)
            return CommandResult(success=True, message=None, mount_as_app_message=False)

        return CommandResult(
            success=True,
            message="Restart complete.",
            mount_as_app_message=True,
        )


# ── /install ─────────────────────────────────────────────


class InstallHandler(BaseCommandHandler):
    """Handler for /install — in-app package / extra installer."""

    @property
    def name(self) -> str:
        return "/install"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        args = ctx.args.strip()
        if not args:
            return CommandResult(
                success=False,
                message="Usage: /install <extra> [--force]\n"
                "       /install <package> --package [--force]\n\n"
                "Example: /install quickjs\n"
                "         /install daytona",
                mount_as_app_message=True,
            )

        app = ctx.app
        if app is not None and hasattr(app, "_handle_install_command"):
            await app._handle_install_command(command=ctx.raw_command)
            return CommandResult(success=True, message=None, mount_as_app_message=False)

        return CommandResult(
            success=True,
            message=f"Installed `{args}`.",
            mount_as_app_message=True,
        )


# ── /update ──────────────────────────────────────────────


class UpdateHandler(BaseCommandHandler):
    """Handler for /update — check for and apply OpsCode software updates."""

    @property
    def name(self) -> str:
        return "/update"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        args = ctx.args.strip()
        parts = args.split() if args else []

        # Validate flags
        allowed = {"--prerelease", "--deps"}
        unknown = [p for p in parts if p.startswith("-") and p not in allowed]
        if unknown:
            return CommandResult(
                success=False,
                message=f"Unknown option(s): {' '.join(unknown)}.\nUsage: /update [--deps] [--prerelease]",
                mount_as_app_message=True,
            )

        app = ctx.app
        if app is not None and hasattr(app, "_handle_update_command"):
            await app._handle_update_command(command=ctx.raw_command)
            return CommandResult(success=True, message=None, mount_as_app_message=False)

        prerelease = "--prerelease" in parts
        try:
            from opscloud import __version__ as current_version
        except ImportError:
            current_version = "unknown"

        latest = await asyncio.to_thread(_check_pypi_version, prerelease=prerelease)
        if latest is None:
            return CommandResult(
                success=True,
                message=f"Could not determine latest version. Currently on v{current_version}.\n"
                "Check your network connection and try again.",
                mount_as_app_message=True,
            )

        if latest == current_version:
            return CommandResult(
                success=True,
                message=f"OpsCode v{current_version} is currently running (latest version).",
                mount_as_app_message=True,
            )

        success, output = await asyncio.to_thread(
            _perform_upgrade, target_version=latest, prerelease=prerelease
        )
        if success:
            return CommandResult(
                success=True,
                message=f"Updated from v{current_version} → v{latest}.\n"
                "Restart OpsCode to use the new version (`/restart`).",
                mount_as_app_message=True,
            )

        return CommandResult(
            success=False,
            message=f"Update failed:\n```\n{output}\n```",
            mount_as_app_message=True,
        )


# ── /auto-update ─────────────────────────────────────────


class AutoUpdateHandler(BaseCommandHandler):
    """Handler for /auto-update — toggle startup update checks."""

    @property
    def name(self) -> str:
        return "/auto-update"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        arg = ctx.args.strip().lower()
        if ctx.settings is not None:
            if arg in ("on", "enable", "true", "1"):
                ctx.settings.auto_update = True
                msg = "Automatic startup update checks enabled."
            elif arg in ("off", "disable", "false", "0"):
                ctx.settings.auto_update = False
                msg = "Automatic startup update checks disabled."
            else:
                current_status = getattr(ctx.settings, "auto_update", True)
                status = "enabled" if current_status else "disabled"
                msg = (
                    f"Automatic update checks are currently **{status}**.\n"
                    "Use `/auto-update [on|off]` to change."
                )
        else:
            msg = "Settings context unavailable."

        return CommandResult(success=True, message=msg, mount_as_app_message=True)


# ── Helpers ──────────────────────────────────────────────


def _reload_dotenv() -> None:
    """Re-read .env files into os.environ using unified settings loader."""
    try:
        from opscloud.config.settings import _load_dotenv

        _load_dotenv(refresh_loaded=True)
    except Exception as exc:
        logger.debug("Failed to reload dotenv: %s", exc)


def _check_pypi_version(*, prerelease: bool = False) -> str | None:
    """Query PyPI for the latest opscloud version."""
    try:
        import json
        import urllib.request

        url = "https://pypi.org/pypi/opscloud/json"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())

        if prerelease:
            versions = list(data.get("releases", {}).keys())
            if versions:
                return str(versions[-1])

        return data.get("info", {}).get("version")
    except Exception:
        return None


def _perform_upgrade(
    *, target_version: str, prerelease: bool = False
) -> tuple[bool, str]:
    """Run pip install to upgrade opscloud."""
    cmd = ["pip", "install", "--upgrade", f"opscloud=={target_version}"]
    if prerelease:
        cmd.append("--pre")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as exc:
        return False, str(exc)
