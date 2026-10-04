"""UI toggle command handlers for OpsCode (scrollbar, timestamps, notifications)."""

from __future__ import annotations

from opscloud.utils.logger import get_logger

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel

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


__all__ = ["NotificationsHandler", "ScrollbarHandler", "TimestampsHandler"]
