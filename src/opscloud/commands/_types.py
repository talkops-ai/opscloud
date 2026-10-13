"""Command surface type definitions."""

from enum import StrEnum
from typing import Literal

class BypassTier(StrEnum):
    """Classification that controls whether a command can skip the message queue."""

    ALWAYS = "always"
    """Execute regardless of any busy state."""

    CONNECTING = "connecting"
    """Bypass only during initial server connection."""

    IMMEDIATE_UI = "immediate_ui"
    """Open modal UI immediately; real work deferred via callback."""

    IMMEDIATE = "immediate"
    """Execute immediately without queuing; does not open modal UI."""

    SIDE_EFFECT_FREE = "side_effect_free"
    """Execute side effect immediately; defer chat output until idle."""

    QUEUED = "queued"
    """Must wait in the queue when the app is busy."""

    NORMAL = "queued"
    """Backward compatibility alias for QUEUED."""

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
