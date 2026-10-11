"""Builder for the isolated workflow agent.

The agent is compiled once per main-agent build and invoked by
``WorkflowMiddleware`` from inside the main graph's ``before_agent`` node, so
its transcript never reaches the main deep agent. There is no planner and no
rubric grader: the agent resolves the run parameters (asking once for missing
required ones), executes the selected runbook, and reports the outcome.

Middleware stack (outermost first)::

    TodoListMiddleware                 write_todos for step tracking
    FilesystemMiddleware               ls / read / write / edit / glob / grep / execute
    AutoModeHITLMiddleware |           same approval policy as the main agent
      AsyncApprovalHITLMiddleware      (plugin MCP tools, shell, writes, ...)
    ConfigurableModelMiddleware        runtime model selection
    JevDynamicModelRouterMiddleware    model routing
    CLICompactionMiddleware            long-run context compaction
    CodeModelRetryMiddleware           transient model error retries
    CostTrackingMiddleware(nested)     cost rolled up into the main session
    ShellAllowListMiddleware           (when configured)
    ServerHooksMiddleware              PreToolUse / PostToolUse hooks
    MCPContextMiddleware               MCP server inventory in the prompt
    MCPToolMiddleware                  MCP tool execution
    PluginSkillsMiddleware             workflow-only built-in skills
    WorkflowPlaybookMiddleware         pins SKILL.md + run parameters
    WorkflowToolsMiddleware            update_workflow
    AskUserMiddleware                  missing parameters / operator decisions
    ManagedMemoryGuardMiddleware
    SubagentArtifactsMiddleware
    UnifiedSystemMessageMiddleware
    PatchToolCallsMiddleware

No ``task`` (subagent delegation) and no ``js_eval``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opscloud.utils.logger import get_logger
from opscloud.workflows.discovery import WORKFLOW_BUILT_IN_SKILLS_DIR
from opscloud.workflows.prompts import WORKFLOW_AGENT_SYSTEM_PROMPT

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langchain_core.language_models import BaseChatModel
    from langchain_core.tools import BaseTool

logger = get_logger(__name__)

WORKFLOW_AGENT_NAME = "workflow_agent"
"""Name used for model routing / tool filtering of the workflow agent."""

_RECURSION_LIMIT = 1_000

_EXCLUDED_TOOL_NAMES = frozenset({"task", "js_eval"})


def workflow_skill_sources() -> list[tuple[str, str]]:
    """Return the workflow agent's exclusive skill sources (empty if none are installed)."""
    if WORKFLOW_BUILT_IN_SKILLS_DIR.is_dir() and any(WORKFLOW_BUILT_IN_SKILLS_DIR.glob("*/SKILL.md")):
        return [(str(WORKFLOW_BUILT_IN_SKILLS_DIR), "Workflow")]
    return []


def create_workflow_agent(
    *,
    model: str | BaseChatModel,
    backend: BackendProtocol,
    tools: Sequence[BaseTool],
    worktree_root: str | Path,
    assistant_id: str = "opscloud",
    interactive: bool = True,
    auto_approve: bool = False,
    shell_allow_list: list[str] | None = None,
    interrupt_on: Any | None = None,
    mcp_server_info: Sequence[Any] | None = None,
    mcp_config: dict[str, Any] | None = None,
    mcp_tools: list[Any] | None = None,
) -> Any:
    """Create the isolated workflow agent.

    Args:
        model: Chat model (or model id) shared with the main agent.
        backend: Filesystem/shell backend shared with the main agent.
        tools: Main-agent tools (MCP tools included); ``task``/``js_eval`` are dropped.
        worktree_root: Working directory for shell commands and hooks.
        assistant_id: Agent identifier.
        interactive: Whether an operator is attached (enables auto-mode HITL).
        auto_approve: Skip approvals entirely (mirrors the main agent).
        shell_allow_list: Shell commands that never need approval.
        interrupt_on: The main agent's HITL interrupt mapping, reused verbatim so
            workflows follow exactly the same approval policy.
        mcp_server_info: Cached MCP server inventory.
        mcp_config: MCP server configuration.
        mcp_tools: Loaded MCP tools.
    """
    from deepagents.middleware import FilesystemMiddleware
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from langchain.agents import create_agent
    from langchain.agents.middleware import TodoListMiddleware

    from opscloud.agent.config import CLIContextSchema
    from opscloud.agent.factory import _subagent_cli_middleware
    from opscloud.middleware.workflow_executor import (
        WorkflowAgentState,
        WorkflowPlaybookMiddleware,
        WorkflowToolsMiddleware,
    )

    agent_tools = [t for t in tools if getattr(t, "name", None) not in _EXCLUDED_TOOL_NAMES]

    cli_middleware = _subagent_cli_middleware(
        has_explicit_model=False,
        assistant_id=assistant_id,
        subagent_name=WORKFLOW_AGENT_NAME,
        interactive=interactive,
        auto_approve=auto_approve,
        shell_allow_list=shell_allow_list,
        interrupt_on=interrupt_on,
        worktree_root=worktree_root,
        custom_middleware=[WorkflowPlaybookMiddleware(), WorkflowToolsMiddleware()],
        mcp_server_info=mcp_server_info,
        mcp_config=mcp_config,
        mcp_tools=mcp_tools,
        backend=backend,
        model=model,
        skill_sources_override=workflow_skill_sources(),
        include_telemetry=False,
    )

    middleware: list[Any] = [
        TodoListMiddleware(),
        FilesystemMiddleware(backend=backend),
        *cli_middleware,
        PatchToolCallsMiddleware(),
    ]
    return create_agent(
        model=model,
        tools=agent_tools,
        middleware=middleware,
        system_prompt=WORKFLOW_AGENT_SYSTEM_PROMPT,
        state_schema=WorkflowAgentState,
        context_schema=CLIContextSchema,
        name="workflow_agent",
    ).with_config(
        {
            "recursion_limit": _RECURSION_LIMIT,
            "run_name": "OpsCloud workflow",
        }
    )


__all__ = [
    "WORKFLOW_AGENT_NAME",
    "create_workflow_agent",
    "workflow_skill_sources",
]
