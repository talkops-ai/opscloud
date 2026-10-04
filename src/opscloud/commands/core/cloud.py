"""Cloud provider and AWS profile management command handler for OpsCloud."""

from __future__ import annotations

import inspect
from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.config.cloud_profiles import (
    get_active_aws_profile,
    get_aws_profile_info,
    list_aws_profiles,
    set_active_aws_profile,
)
from opscloud.config.toml_config import (
    clear_aws_profile,
    load_aws_profile,
)

logger = get_logger(__name__)


class CloudHandler(BaseCommandHandler):
    """Handler for /cloud (and /aws) command to switch AWS profile or inspect cloud context."""

    @property
    def name(self) -> str:
        return "/cloud"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/aws",)

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
        args = ctx.args.strip()

        # 1. Interactive picker when no args provided
        if not args:
            if ctx.app is not None:
                show_cloud = getattr(ctx.app, "_show_cloud_selector", None)
                if callable(show_cloud):
                    res = show_cloud()
                    if inspect.isawaitable(res):
                        await res
                    return CommandResult(success=True, mount_as_app_message=False)
            return self._handle_status()

        # 2. Subcommands: list, status, default
        if args in ("list", "--list", "-l"):
            return self._handle_list()

        if args in ("status", "--status", "info", "--info"):
            return self._handle_status()

        if args.startswith("--default"):
            return self._handle_default(ctx, args)

        # 3. Direct switch: `/cloud <profile>` or `/cloud set <profile>`
        profile_name = args
        if profile_name.startswith("set "):
            profile_name = profile_name[4:].strip()

        if not profile_name:
            return CommandResult(success=False, message="Please specify an AWS profile name.")

        info = get_aws_profile_info(profile_name)
        target_region = info.region if info else None

        # Activate and persist across sessions
        set_active_aws_profile(profile_name, region=target_region, persist=True)

        if ctx.settings is not None:
            ctx.settings.aws_profile = profile_name
            if target_region:
                ctx.settings.aws_region = target_region

        if ctx.app is not None:
            switch_fn = getattr(ctx.app, "_switch_cloud_profile", None)
            if callable(switch_fn):
                res = switch_fn(profile_name, region=target_region)
                if inspect.isawaitable(res):
                    await res

        region_str = f" ({target_region})" if target_region else ""
        return CommandResult(
            success=True,
            message=(
                f"**Switched Cloud Profile:** Active AWS profile set to `{profile_name}`{region_str} "
                "(saved to `config.toml` — active across all sessions)."
            ),
        )

    def _handle_list(self) -> CommandResult:
        """List all discovered AWS profiles."""
        profiles = list_aws_profiles()
        active = get_active_aws_profile()
        default = load_aws_profile()

        lines = ["**Configured AWS Profiles:**\n"]
        for p in profiles:
            flags = []
            if p.name == active:
                flags.append("**active**")
            if p.name == default:
                flags.append("default")
            flag_str = f" *({', '.join(flags)})*" if flags else ""

            details = []
            if p.region:
                details.append(f"region: `{p.region}`")
            if p.account_id:
                details.append(f"account: `{p.account_id}`")
            elif p.role_arn:
                details.append("role assumed")
            detail_str = f" — {', '.join(details)}" if details else ""

            lines.append(f"- `{p.name}`{flag_str}{detail_str}")

        lines.append("\n*Use `/cloud <name>` or `/cloud` to switch profile.*")
        return CommandResult(success=True, message="\n".join(lines))

    def _handle_status(self) -> CommandResult:
        """Inspect current cloud provider and profile status."""
        active = get_active_aws_profile()
        persisted = load_aws_profile()

        from opscloud.config.aws import resolve_aws_identity

        identity = resolve_aws_identity(profile=active)

        lines = [
            "☁️ **Current Cloud Context:**",
            f"- **Provider:** AWS",
            f"- **Active Profile:** `{active}`",
            f"- **Persisted in config.toml:** `{persisted or 'none (using env/default)'}`",
            f"- **Region:** `{identity.region}`",
        ]

        if identity.is_authenticated:
            if identity.account_id:
                lines.append(f"- **Account ID:** `{identity.account_id}`")
            if identity.arn:
                lines.append(f"- **Caller ARN:** `{identity.arn}`")
            lines.append("- **Authentication:** ✅ Validated via STS")
        else:
            lines.append("- **Authentication:** ⚠️ Not validated / credentials local")

        return CommandResult(success=True, message="\n".join(lines))

    def _handle_default(self, ctx: CommandContext, args: str) -> CommandResult:
        """Handle setting or clearing default AWS profile in config.toml."""
        rest = args[len("--default") :].strip()
        if rest == "--clear":
            clear_aws_profile()
            return CommandResult(success=True, message="Default AWS profile cleared from config.toml.")

        if rest:
            set_active_aws_profile(rest, persist=True)
            if ctx.settings is not None:
                ctx.settings.aws_profile = rest
            return CommandResult(
                success=True,
                message=f"Default AWS profile set to `{rest}` in config.toml.",
            )

        return CommandResult(
            success=False,
            message="Usage: `/cloud --default <profile_name>` or `/cloud --default --clear`",
        )


__all__ = ["CloudHandler"]
