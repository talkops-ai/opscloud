"""Re-export classifier-backed Auto mode components."""

from __future__ import annotations

from opscloud.middleware.auto_mode_hitl import (
    AUTO_MODE_COUNTERS_NAMESPACE,
    AUTO_MODE_EVENT_TYPE,
    READONLY_SAFE_TOOLS,
    USER_PROMPT_METADATA_KEY,
    AsyncApprovalHITLMiddleware,
    AutoDecision,
    AutoDecisionBatch,
    AutoDecisionCategory,
    AutoModeCounters,
    AutoModeHITLMiddleware,
    AutoModeState,
    AutoTempArtifact,
    AutoTempArtifactMutation,
    DynamicInterruptMapping,
    PlannedDecision,
    PromptMetadata,
    _async_routing_mode,
    _merge_temp_artifacts,
    user_prompt_metadata,
)
from opscloud.middleware.headless_mcp_guard import (
    HeadlessMCPGuardMiddleware,
    gated_mcp_tool_names,
)

def is_potentially_destructive(command: str) -> bool:
    """Classify whether a shell command is potentially destructive."""
    from opscloud.security.shell_safety import classify_command
    return classify_command(command) == "dangerous"


__all__ = [
    "AUTO_MODE_COUNTERS_NAMESPACE",
    "AUTO_MODE_EVENT_TYPE",
    "READONLY_SAFE_TOOLS",
    "USER_PROMPT_METADATA_KEY",
    "AsyncApprovalHITLMiddleware",
    "AutoDecision",
    "AutoDecisionBatch",
    "AutoDecisionCategory",
    "AutoModeCounters",
    "AutoModeHITLMiddleware",
    "AutoModeState",
    "AutoTempArtifact",
    "AutoTempArtifactMutation",
    "DynamicInterruptMapping",
    "HeadlessMCPGuardMiddleware",
    "PlannedDecision",
    "PromptMetadata",
    "_async_routing_mode",
    "_merge_temp_artifacts",
    "gated_mcp_tool_names",
    "is_potentially_destructive",
    "user_prompt_metadata",
]
