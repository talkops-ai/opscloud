"""Command surface type definitions."""

from enum import StrEnum
from typing import Literal

from opscloud.ui.command_registry import BypassTier

NotifySeverity = Literal["information", "warning", "error"]


class SafetyLevel(StrEnum):
    """Risk classification for every command."""

    READ_ONLY = "read_only"
    """No side effects: e.g. /help, /tokens, /context."""

    LOW_RISK = "low_risk"
    """Reversible changes: e.g. /theme, /effort, /model."""

    HIGH_RISK = "high_risk"
    """Significant changes: e.g. /tf-plan, /deploy (dry-run)."""

    DESTRUCTIVE = "destructive"
    """Irreversible changes: e.g. /tf-apply, /rollback on prod."""


class CommandCategory(StrEnum):
    """Taxonomy layer a command belongs to."""

    CORE = "core"
    POWER = "power"
    DEVOPS = "devops"
    AUTOMATION = "automation"


__all__ = ["BypassTier", "CommandCategory", "NotifySeverity", "SafetyLevel"]
