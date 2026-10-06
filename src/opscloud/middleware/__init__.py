"""OpsCloud middleware modules and centralized registry."""

from __future__ import annotations

from opscloud.middleware.registry import (
    BaseAgentMiddleware,
    MiddlewareRegistry,
    get_middleware_registry,
    register_middleware,
)
from opscloud.middleware.configurable_model import ConfigurableModelMiddleware
from opscloud.middleware.jev_model_router import JevDynamicModelRouterMiddleware
from opscloud.middleware.local_context import LocalContextMiddleware
from opscloud.middleware.ask_user import AskUserMiddleware
from opscloud.middleware.resume_state import ResumeStateMiddleware
from opscloud.middleware.compaction import create_cli_compaction_middleware
from opscloud.middleware.skills import PluginSkillsMiddleware
from opscloud.middleware.cost_tracking import ACTIVE_SESSION_THREAD_ID, CostState, CostTrackingMiddleware
from opscloud.middleware.auto_mode_hitl import AutoModeHITLMiddleware, AutoModeState
from opscloud.middleware.auto_mode import AUTO_MODE_EVENT_TYPE, is_potentially_destructive
from opscloud.middleware.subagents import SubagentsMiddleware
from opscloud.middleware.subagent_telemetry import SubagentTelemetryMiddleware
from opscloud.middleware.goal_state_notice import (
    GOAL_CONTROL_MESSAGE_SOURCE,
    GOAL_STATE_MESSAGE_SOURCE,
    build_goal_state_notice,
    is_conversation_control_message,
)
from opscloud.middleware.goal_tools import GoalToolsMiddleware
from opscloud.middleware.goal_criteria import (
    GoalAmendRequest,
    GoalCreateRequest,
    GoalCriteriaMiddleware,
    GoalCriteriaRequest,
    GoalCriteriaState,
    GoalProposal,
)
from opscloud.middleware.reliable_rubric import ReliableRubricMiddleware
from opscloud.middleware.unified_system_message import (
    UnifiedSystemMessageMiddleware,
    unify_system_message,
)
from opscloud.middleware._repository_bounds import (
    RepositoryBounds,
    REPOSITORY_TOOL_NAMES,
)
from opscloud.middleware.glm_stall_recovery import GlmTerminalStallRecoveryMiddleware
from opscloud.middleware.headless_mcp_guard import HeadlessMCPGuardMiddleware
from opscloud.middleware.mcp_context import MCPContextMiddleware
from opscloud.middleware.mcp_middleware import MCPToolMiddleware, normalize_mcp_arguments
from opscloud.middleware.memory_guard import ManagedMemoryGuardMiddleware
from opscloud.middleware.model_retry import CodeModelRetryMiddleware
from opscloud.middleware.server_hooks import ServerHooksMiddleware, ServerHooksState
from opscloud.middleware.shell_allow_list import ShellAllowListMiddleware
from opscloud.middleware.tool_filter import ToolFilterMiddleware, TOOL_ALIAS_MAP

__all__ = [
    "BaseAgentMiddleware",
    "MiddlewareRegistry",
    "register_middleware",
    "get_middleware_registry",
    "ConfigurableModelMiddleware",
    "JevDynamicModelRouterMiddleware",
    "LocalContextMiddleware",
    "AskUserMiddleware",
    "ResumeStateMiddleware",
    "CostTrackingMiddleware",
    "CostState",
    "ACTIVE_SESSION_THREAD_ID",
    "create_cli_compaction_middleware",
    "PluginSkillsMiddleware",
    "AutoModeHITLMiddleware",
    "AutoModeState",
    "AUTO_MODE_EVENT_TYPE",
    "is_potentially_destructive",
    "SubagentsMiddleware",
    "SubagentTelemetryMiddleware",
    "GoalToolsMiddleware",
    "GoalCriteriaMiddleware",
    "GoalCriteriaRequest",
    "GoalCriteriaState",
    "GoalCreateRequest",
    "GoalAmendRequest",
    "GoalProposal",
    "ReliableRubricMiddleware",
    "UnifiedSystemMessageMiddleware",
    "unify_system_message",
    "GOAL_CONTROL_MESSAGE_SOURCE",
    "GOAL_STATE_MESSAGE_SOURCE",
    "build_goal_state_notice",
    "is_conversation_control_message",
    "RepositoryBounds",
    "REPOSITORY_TOOL_NAMES",
    "GlmTerminalStallRecoveryMiddleware",
    "HeadlessMCPGuardMiddleware",
    "MCPContextMiddleware",
    "MCPToolMiddleware",
    "normalize_mcp_arguments",
    "ManagedMemoryGuardMiddleware",
    "CodeModelRetryMiddleware",
    "ServerHooksMiddleware",
    "ServerHooksState",
    "ShellAllowListMiddleware",
    "ToolFilterMiddleware",
    "TOOL_ALIAS_MAP",
]
