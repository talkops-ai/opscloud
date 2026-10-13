"""UI toggle command handlers for OpsCode (scrollbar, timestamps, notifications)."""

from __future__ import annotations

from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.security.approval_mode import ApprovalMode

logger = get_logger(__name__)


class ScrollbarHandler(BaseCommandHandler):
    """Handler for /scrollbar command to toggle chat scrollbar visibility."""

    @property
    def name(self) -> str:
        return "/scrollbar"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        label = "toggled"
        if ctx.app is not None:
            try:
                from opscloud.ui.widgets.messages import MessageList
                messages = ctx.app.query_one("#messages", MessageList)
                current = getattr(messages.styles, "scrollbar_size_vertical", 1)
                new_size = 0 if current == 1 else 1
                messages.styles.scrollbar_size_vertical = new_size
                label = "hidden" if new_size == 0 else "shown"
            except Exception as exc:
                logger.debug("Failed toggling scrollbar on #messages: %s", exc)

        return CommandResult(
            success=True,
            notify=f"Chat scrollbar {label}.",
            notify_severity="information",
            mount_as_app_message=False,
        )


class TimestampsHandler(BaseCommandHandler):
    """Handler for /timestamps command to toggle message timestamps."""

    @property
    def name(self) -> str:
        return "/timestamps"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        visible = True
        if ctx.app is not None:
            if hasattr(ctx.app, "toggle_timestamps"):
                visible = bool(ctx.app.toggle_timestamps())
            elif hasattr(ctx.app, "_message_timestamps_visible"):
                ctx.app._message_timestamps_visible = not ctx.app._message_timestamps_visible
                visible = ctx.app._message_timestamps_visible

        label = "shown" if visible else "hidden"
        return CommandResult(
            success=True,
            notify=f"Message timestamps {label}.",
            notify_severity="information",
            mount_as_app_message=False,
        )


class NotificationsHandler(BaseCommandHandler):
    """Handler for /notifications command to open notification settings modal."""

    @property
    def name(self) -> str:
        return "/notifications"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.IMMEDIATE_UI

    async def execute(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None:
            try:
                from opscloud.model.config import is_warning_suppressed
                from opscloud.ui.widgets.notification_settings import (
                    WARNING_TOGGLES,
                    NotificationSettingsScreen,
                )

                suppressed: set[str] = set()
                for key, _ in WARNING_TOGGLES:
                    if is_warning_suppressed(key):
                        suppressed.add(key)

                ctx.app.push_screen(NotificationSettingsScreen(suppressed=suppressed))
            except Exception as exc:
                logger.debug("Failed opening NotificationSettingsScreen: %s", exc)

        return CommandResult(
            success=True,
            notify="Opened notification settings.",
            notify_severity="information",
            mount_as_app_message=False,
        )


class ThemeHandler(BaseCommandHandler):
    """Handler for /theme command to select or switch theme."""

    @property
    def name(self) -> str:
        return "/theme"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.IMMEDIATE_UI

    async def execute(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None:
            arg = ctx.args.strip()
            if arg and hasattr(ctx.app, "_switch_theme"):
                await ctx.app._switch_theme(arg)
                return CommandResult(success=True, mount_as_app_message=False)
            elif hasattr(ctx.app, "_show_theme_selector"):
                await ctx.app._show_theme_selector()
                return CommandResult(success=True, mount_as_app_message=False)
        return CommandResult(success=False, message="Theme selector requires active TUI application.")


class AutoApprovalHandler(BaseCommandHandler):
    """Handler for /auto command to switch to Auto approval mode."""

    @property
    def name(self) -> str:
        return "/auto"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None and hasattr(ctx.app, "_set_approval_mode"):
            await ctx.app._set_approval_mode(ApprovalMode.AUTO)
            return CommandResult(success=True, mount_as_app_message=False)
        return CommandResult(success=False, message="Approval mode toggle requires active TUI application.")


class ManualApprovalHandler(BaseCommandHandler):
    """Handler for /manual command to switch to Manual approval mode."""

    @property
    def name(self) -> str:
        return "/manual"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None and hasattr(ctx.app, "_set_approval_mode"):
            await ctx.app._set_approval_mode(ApprovalMode.MANUAL)
            return CommandResult(success=True, mount_as_app_message=False)
        return CommandResult(success=False, message="Approval mode toggle requires active TUI application.")


class SmartApprovalHandler(BaseCommandHandler):
    """Handler for /smart command to switch to Smart approval mode."""

    @property
    def name(self) -> str:
        return "/smart"

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.CORE

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.SIDE_EFFECT_FREE

    async def execute(self, ctx: CommandContext) -> CommandResult:
        if ctx.app is not None and hasattr(ctx.app, "_set_approval_mode"):
            await ctx.app._set_approval_mode(ApprovalMode.SMART)
            return CommandResult(success=True, mount_as_app_message=False)
        return CommandResult(success=False, message="Approval mode toggle requires active TUI application.")


__all__ = [
    "AutoApprovalHandler",
    "ManualApprovalHandler",
    "NotificationsHandler",
    "ScrollbarHandler",
    "SmartApprovalHandler",
    "ThemeHandler",
    "TimestampsHandler",
]
