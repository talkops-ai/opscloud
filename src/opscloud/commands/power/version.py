"""Version / about command handler for OpsCode."""

from __future__ import annotations

import sys

from opscloud._version import __version__
from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel


class VersionHandler(BaseCommandHandler):
    """Handler for /version command."""

    @property
    def name(self) -> str:
        return "/version"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ()

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.ALWAYS

    async def execute(self, ctx: CommandContext) -> CommandResult:
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        model_str = (
            ctx.model_spec
            or getattr(ctx.settings, "model_name", None)
            or (getattr(ctx.app, "_model", None) if ctx.app else None)
            or "default"
        )
        msg = (
            "### OpsCloud Version & Runtime\n\n"
            f"- **OpsCloud CLI:** `{__version__}`\n"
            f"- **OpsCloud SDK:** `{__version__}`\n"
            f"- **Python:** `{py_ver}`\n"
            f"- **Active Model:** `{model_str}`"
        )
        return CommandResult(success=True, message=msg)


__all__ = ["VersionHandler"]
