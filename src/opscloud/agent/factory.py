"""Core Agent Factory for OpsCloud.

Produces a compiled LangGraph ``Pregel`` graph wired with the complete
middleware stack, composite backend, dynamic subagents, skills, MCP servers,
and security controls with Database as Source of Truth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
import concurrent.futures
import fnmatch
from pathlib import Path
from typing import Any, cast

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware import MemoryMiddleware
from deepagents.middleware.async_subagents import AsyncSubAgent
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from opscloud.agent.config import AgentContextSchema, CLIContextSchema
from opscloud.backend.composite import OpsCloudCompositeBackend
from opscloud.backend.local import LocalShellBackend
from opscloud.config import paths
from opscloud.config.settings import get_settings
from opscloud.project_utils import ProjectContext, get_server_project_context
from opscloud.mcp.discovery import discover_mcp_configs
from opscloud.memory.registry import MemoryRegistry
from opscloud.middleware.ask_user import AskUserMiddleware
from opscloud.middleware.auto_mode import (
    AsyncApprovalHITLMiddleware,
    AutoModeHITLMiddleware,
)
from opscloud.middleware.compaction import (
    _create_cli_compaction_middleware,
)
from opscloud.middleware.configurable_model import ConfigurableModelMiddleware
from opscloud.middleware.jev_model_router import JevDynamicModelRouterMiddleware
from opscloud.middleware.cost_tracking import CostTrackingMiddleware
from opscloud.middleware.glm_stall_recovery import (
    GlmTerminalStallRecoveryMiddleware,
)
from opscloud.middleware.goal_criteria import (
    GoalCriteriaMiddleware,
    _WebSearchBudgetMiddleware,
    create_goal_criteria_agent,
    create_goal_criteria_fallback_agent,
)
from opscloud.middleware.goal_tools import GoalToolsMiddleware
from opscloud.middleware.headless_mcp_guard import (
    HeadlessMCPGuardMiddleware,
    gated_mcp_tool_names,
)
from opscloud.middleware.local_context import LocalContextMiddleware
from opscloud.middleware.mcp_middleware import MCPToolMiddleware
from opscloud.middleware.memory_guard import ManagedMemoryGuardMiddleware
from opscloud.middleware.model_retry import CodeModelRetryMiddleware
from opscloud.middleware.reliable_rubric import ReliableRubricMiddleware
from opscloud.middleware.resume_state import ResumeStateMiddleware
from opscloud.middleware.server_hooks import ServerHooksMiddleware
from opscloud.middleware.shell_allow_list import ShellAllowListMiddleware
from opscloud.middleware.skills import (
    PluginSkillsMiddleware,
    discover_skill_dirs,
)
from opscloud.middleware.subagents import SubagentsMiddleware
from opscloud.middleware.tool_filter import ToolFilterMiddleware
from opscloud.middleware.unified_system_message import (
    UnifiedSystemMessageMiddleware,
)
from opscloud.model.factory import create_model
from opscloud.prompts import (
    get_base_system_prompt,
    get_memory_system_prompt,
)
from opscloud.rubrics.evaluator import (
    _RUBRIC_GRADER_SYSTEM_PROMPT,
    _create_rubric_grader_tools,
)
from opscloud.skills.registry import SkillRegistry, SkillSourceTuple
from opscloud.subagents.loader import list_subagents
from opscloud.subagents.types import SubagentMetadata
from opscloud.tools.catalog import register_all_tools
from opscloud.tools.registry import ToolRegistry
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def _build_hitl_interrupt_config(auto_approve: bool) -> dict[str, bool]:
    """Build HITL interrupt configuration for tools."""
    if auto_approve:
        return {}
    return {
        "execute": True,
        "delete_file": True,
        "edit_file": True,
        "write_file": True,
    }


def _format_description(tool_call: Any = None, *args: Any, **kwargs: Any) -> str:
    if isinstance(tool_call, str) and args and isinstance(args[0], dict):
        name = tool_call
        tool_args = args[0]
    elif isinstance(tool_call, dict):
        name = tool_call.get("name") or tool_call.get("tool_name") or ""
        tool_args = tool_call.get("args") or tool_call.get("parameters") or {}
    else:
        name = getattr(tool_call, "name", None) or getattr(tool_call, "tool_name", None) or ""
        tool_args = getattr(tool_call, "args", {}) or getattr(tool_call, "parameters", {}) or {}

    if not isinstance(tool_args, dict):
        tool_args = {}

    if name in {"execute", "run_command"}:
        cmd = tool_args.get("command") or tool_args.get("CommandLine") or ""
        return f"Execute shell command: {cmd}"
    if name in {"write_file", "write_to_file"}:
        path = tool_args.get("file_path") or tool_args.get("TargetFile") or ""
        return f"Write file: {path}"
    if name in {"edit_file", "replace_file_content", "multi_replace_file_content"}:
        path = tool_args.get("file_path") or tool_args.get("TargetFile") or ""
        return f"Edit file: {path}"
    if name in {"task", "subagent"}:
        sub_type = tool_args.get("subagent_type") or tool_args.get("name") or tool_args.get("agent") or "subagent"
        desc = tool_args.get("description") or tool_args.get("prompt") or tool_args.get("task") or ""
        return f"Spawn {sub_type} subagent: {desc}" if desc else f"Spawn {sub_type} subagent"
    if name == "js_eval":
        code_snippet = str(tool_args.get("code") or "").strip()
        first_line = code_snippet.split("\n")[0][:80]
        return (
            f"Evaluate script / dispatch subagents: {first_line}"
            if first_line
            else "Evaluate script / dispatch subagents"
        )
    if ":" in name:
        srv, tname = name.split(":", 1)
        args_str = ", ".join(f"{k}={v}" for k, v in list(tool_args.items())[:3])
        return f"Execute {srv} action '{tname}' ({args_str})" if args_str else f"Execute {srv} action '{tname}'"
    args_str = ", ".join(f"{k}={v}" for k, v in list(tool_args.items())[:3])
    return f"Execute tool '{name}' ({args_str})" if args_str else f"Execute tool '{name}'"


def _should_interrupt_tool_call(
    request: Any = None,
    *args: Any,
    auto_mode_enabled: bool = True,
    **kwargs: Any,
) -> bool:
    """Decide whether stock HITL should pause for a gated tool call.

    Follows the unified approval mode hierarchy:
    1. If server hooks already decided permission -> False (never interrupt).
    2. If mode in ("auto", "smart") -> False when classifier is enabled, True if classifier disabled.
    3. Internal AGENTS.md writes or read-only filesystem tools -> False.
    4. In "manual" mode -> True (require human confirmation).
    """
    from opscloud._constants import READONLY_FS_TOOLS
    from opscloud.middleware.auto_mode import _async_routing_mode
    from opscloud.middleware.server_hooks import hook_decided_permission
    from opscloud.security.approval_mode import ApprovalMode, coerce_approval_mode
    from opscloud.security.approval_mode_source import _resolve_approval_mode

    state = (
        getattr(request, "state", None)
        or (request.get("state") if isinstance(request, dict) else None)
        or (args[0] if args and isinstance(args[0], dict) else None)
    )
    tool_call = (
        getattr(request, "tool_call", None)
        or getattr(request, "action", None)
        or (request.get("tool_call") if isinstance(request, dict) else None)
        or (request.get("action") if isinstance(request, dict) else None)
        or request
    )
    tool_id = str(tool_call.get("id") or "") if isinstance(tool_call, dict) else getattr(tool_call, "id", "")
    if hook_decided_permission(state, tool_id):
        return False

    mode = _async_routing_mode(state)
    if mode is None and isinstance(state, Mapping) and state.get("approval_mode"):
        mode = coerce_approval_mode(state.get("approval_mode"))
    if mode is None:
        runtime = (
            getattr(request, "runtime", None)
            or (request.get("runtime") if isinstance(request, dict) else None)
            or kwargs.get("runtime")
        )
        ctx = getattr(runtime, "context", None) if runtime else None
        if not ctx and hasattr(runtime, "config"):
            ctx = getattr(runtime, "config", None)
        if not ctx and isinstance(state, dict):
            ctx = state
        mode = _resolve_approval_mode(ctx, getattr(runtime, "store", None) if runtime else None)

    if mode in (ApprovalMode.AUTO, ApprovalMode.SMART, "auto", "smart"):
        return not auto_mode_enabled

    # Never interrupt internal memory file writes (e.g. AGENTS.md) or read-only tools
    tool_name = str(tool_call.get("name") or tool_call.get("action") or "") if isinstance(tool_call, dict) else getattr(tool_call, "name", "")
    tool_args = tool_call.get("args") or tool_call.get("parameters") or {} if isinstance(tool_call, dict) else getattr(tool_call, "args", {})
    path_str = str(tool_args.get("path") or tool_args.get("TargetFile") or tool_args.get("file_path") or "")
    if tool_name in READONLY_FS_TOOLS or "AGENTS.md" in path_str:
        return False

    return True


def _interrupt_predicate(
    *,
    auto_mode_enabled: bool,
) -> Callable[[Any], bool]:
    """Bind runtime eligibility into a stock-HITL predicate.

    Returns a predicate suitable for ``InterruptOnConfig.when`` that closes
    over ``auto_mode_enabled``.
    """

    def should_interrupt(request: Any) -> bool:
        """Evaluate whether the given tool call request warrants an interrupt.

        Args:
            request: The tool execution request to check.

        Returns:
            bool: True if execution should pause for human approval.
        """
        return _should_interrupt_tool_call(request, auto_mode_enabled=auto_mode_enabled)

    return should_interrupt


INTERPRETER_PTC_SAFE_PRESET: frozenset[str] = frozenset({"read_file", "glob", "grep"})
_INTERPRETER_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "execute",
        "write_file",
        "edit_file",
        "delete_file",
        "replace_file_content",
        "multi_replace_file_content",
        "write_to_file",
        "write_todos",
        "task",
        "subagent",
    }
)


def _resolve_ptc_option(
    ptc: str | bool | list[str] | None,
    *,
    tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]],
    acknowledge_unsafe: bool,
    auto_approve: bool,
) -> list[str] | None:
    if ptc is False or ptc is None or ptc == []:
        return None

    live_names: list[str] = []
    for candidate in tools:
        if isinstance(candidate, BaseTool):
            name = candidate.name
            if isinstance(name, str):
                live_names.append(name)
        elif isinstance(candidate, dict):
            raw_name = candidate.get("name")
            if isinstance(raw_name, str):
                live_names.append(raw_name)
        else:
            attr = getattr(candidate, "name", None)
            if isinstance(attr, str):
                live_names.append(attr)
    live_set: set[str] = set(live_names)

    if isinstance(ptc, str):
        normalized = ptc.strip().lower()
        if normalized == "safe":
            return sorted(INTERPRETER_PTC_SAFE_PRESET)
        if normalized == "all":
            if not auto_approve and not acknowledge_unsafe:
                msg = (
                    "interpreter_ptc='all' exposes every host tool to PTC "
                    "calls that bypass HITL approval. Set "
                    "interpreter_ptc_acknowledge_unsafe=True (or use "
                    "auto_approve=True) to opt in."
                )
                raise ValueError(msg)
            included = sorted(live_set)
            write_included = sorted(_INTERPRETER_WRITE_TOOLS & live_set)
            if write_included:
                logger.info(
                    "interpreter_ptc='all' includes write/shell tools: %s",
                    write_included,
                )
            return included
        msg = f"Invalid interpreter_ptc preset {ptc!r}. Must be 'safe', 'all', or a list of tool names."
        raise ValueError(msg)

    if isinstance(ptc, list):
        for item in ptc:
            if not isinstance(item, str):
                msg = "interpreter_ptc list must contain only strings."
                raise TypeError(msg)
        configured_set = set(ptc)
        unacknowledged = configured_set & _INTERPRETER_WRITE_TOOLS
        if unacknowledged and not auto_approve and not acknowledge_unsafe:
            unack_str = ", ".join(sorted(unacknowledged))
            msg = (
                f"interpreter_ptc contains write/shell tools ({unack_str}) "
                "that bypass HITL approval. Set "
                "interpreter_ptc_acknowledge_unsafe=True (or use "
                "auto_approve=True) to opt in."
            )
            raise ValueError(msg)
        return sorted(configured_set)

    if ptc:
        return sorted(INTERPRETER_PTC_SAFE_PRESET)

    msg = "interpreter_ptc must be a string, boolean, list of strings, or None."
    raise TypeError(msg)


def _subagent_cli_middleware(
    *,
    has_explicit_model: bool,
    assistant_id: str,
    subagent_name: str,
    allowed_tools: Sequence[str] | None = None,
    allowed_skills: Sequence[str] | None = None,
    capabilities: Sequence[dict[str, Any]] | None = None,
    interactive: bool = True,
    shell_allow_list: list[str] | None = None,
    interrupt_on: dict[str, Any] | None = None,
    worktree_root: str | Path | None = None,
    subagent_path: str | None = None,
    custom_middleware: Sequence[Any] | None = None,
    mcp_server_info: Sequence[Any] | None = None,
    mcp_config: dict[str, Any] | None = None,
    mcp_tools: list[Any] | None = None,
) -> list[Any]:
    middleware: list[AgentMiddleware[Any, Any]] = []
    if interrupt_on is not None:
        middleware.append(AsyncApprovalHITLMiddleware(interrupt_on))

    if not has_explicit_model:
        middleware.append(ConfigurableModelMiddleware(persist_model_state=False))

    middleware.append(CodeModelRetryMiddleware())
    middleware.append(CostTrackingMiddleware(nested=True))

    if not interactive:
        middleware.append(GlmTerminalStallRecoveryMiddleware())

    if shell_allow_list:
        middleware.append(ShellAllowListMiddleware(shell_allow_list))

    subagent_cwd = Path(worktree_root) if worktree_root is not None else Path.cwd()
    middleware.append(
        ServerHooksMiddleware(
            cwd=subagent_cwd,
            emit_stop=False,
            mcp_tools=mcp_tools or (),
        )
    )

    # MCP Context Middleware — injects MCP server inventory into system prompt
    if mcp_server_info or mcp_config:
        from opscloud.middleware.mcp_context import MCPContextMiddleware

        middleware.append(MCPContextMiddleware(mcp_server_info=mcp_server_info, mcp_config=mcp_config))

    if mcp_server_info or mcp_config or mcp_tools:
        middleware.append(MCPToolMiddleware())

    if allowed_tools or capabilities:
        middleware.append(ToolFilterMiddleware(allowed_patterns=allowed_tools, capabilities=capabilities))

    skill_sources: list[SkillSourceTuple] = []
    if subagent_path:
        p = Path(subagent_path)
        bundle_dir = p.parent.parent if p.parent.name == "agents" else p.parent
        sub_skills_dir = bundle_dir / "skills"
        if sub_skills_dir.exists() and sub_skills_dir.is_dir():
            skill_sources.append((str(sub_skills_dir), f"Subagent ({subagent_name})"))

    global_sources = SkillRegistry.get_instance().get_sources_for_middleware()
    if allowed_skills is not None:
        backend_fs = FilesystemBackend(virtual_mode=False)
        for src in global_sources:
            src_path = src[0]
            try:
                found_dirs = discover_skill_dirs(backend_fs, src_path)
                for dir_path, _ in found_dirs:
                    skill_name = Path(dir_path).name
                    if any(
                        fnmatch.fnmatch(skill_name, pat) or fnmatch.fnmatch(skill_name.lower(), pat.lower())
                        for pat in allowed_skills
                    ):
                        skill_sources.append(src)
                        break
            except Exception as exc:
                logger.debug("Skill discovery failed for source %s: %s", src_path, exc)
    elif not skill_sources:
        skill_sources.extend(global_sources)

    if skill_sources:
        middleware.append(
            PluginSkillsMiddleware(
                backend=FilesystemBackend(virtual_mode=False),
                sources=skill_sources,
                allowed_skills=allowed_skills,
            )
        )

    if custom_middleware:
        for extra_mw in custom_middleware:
            middleware.append(extra_mw)

    from opscloud.middleware.ask_user import AskUserMiddleware

    middleware.append(AskUserMiddleware())

    middleware.append(ManagedMemoryGuardMiddleware())
    middleware.append(UnifiedSystemMessageMiddleware())
    return middleware


def create_opscloud_agent(
    model: str | BaseChatModel | None = None,
    *,
    assistant_id: str = "opscloud",
    cloud_provider: str | None = None,
    tools: Sequence[BaseTool | Callable[..., Any] | dict[str, Any]] | None = None,
    mcp_tools: Sequence[BaseTool] | None = None,
    system_prompt: str | None = None,
    interactive: bool = True,
    auto_approve: bool = False,
    enable_shell: bool = True,
    enable_interpreter: bool = True,
    enable_ask_user: bool = True,
    checkpointer: Any | None = None,
    store: Any | None = None,
    config_store: Any | None = None,
    cwd: str | Path | None = None,
    project_context: ProjectContext | None = None,
    thread_id: str | None = None,
    reasoning_effort: str | None = None,
    extra_kwargs: dict[str, Any] | None = None,
    goal_criteria_tools: Sequence[BaseTool | Callable[..., Any]] | None = None,
    async_subagents: Sequence[AsyncSubAgent] | None = None,
    fs_tools: Sequence[str] | None = None,
    **kwargs: Any,
) -> tuple[CompiledStateGraph[Any, Any, Any, Any], OpsCloudCompositeBackend]:
    """Create the OpsCloud agent graph with DB-backed resources.

    Returns:
        tuple[Pregel, OpsCloudCompositeBackend]: Compiled Pregel graph and backend.
    """
    settings = get_settings()

    # 1. Resolve working directory following dcode conventions
    resolved_context = project_context or get_server_project_context()
    effective_cwd: Path
    if cwd is not None:
        effective_cwd = Path(cwd).resolve()
    elif resolved_context is not None:
        effective_cwd = resolved_context.user_cwd
    elif settings.cwd is not None:
        effective_cwd = settings.cwd.resolve()
    else:
        effective_cwd = Path.cwd().resolve()

    effective_project_root = (
        resolved_context.project_root
        if resolved_context and resolved_context.project_root
        else settings.effective_project_root
    )
    project_scope_root = effective_project_root or effective_cwd

    # 2. Create composite backend
    default_backend = LocalShellBackend(root_dir=effective_cwd)
    composite_backend = OpsCloudCompositeBackend(default=default_backend)

    # 3. Resolve active chat model
    eff = (
        reasoning_effort
        or (extra_kwargs.get("reasoning_effort") if extra_kwargs else None)
        or getattr(settings, "reasoning_effort", None)
    )
    model_provider: str | None = None
    model_context_limit: int | None = None
    model_unsupported_modalities: frozenset[str] = frozenset()

    if isinstance(model, str) or model is None:
        merged_kwargs = dict(extra_kwargs or {})
        if eff:
            merged_kwargs["reasoning_effort"] = eff
        model_res = create_model(model, extra_kwargs=merged_kwargs if merged_kwargs else None)
        model_res.apply_to_settings()
        active_model = model_res.model
        model_name = getattr(active_model, "model_name", model or "default-model")
        model_provider = model_res.provider
        model_context_limit = model_res.context_limit
        model_unsupported_modalities = model_res.unsupported_modalities
    else:
        active_model = model
        model_name = getattr(active_model, "model_name", "custom-model")
        model_provider = getattr(active_model, "_llm_type", None)

    # 4. Register catalog tools and build tool list
    register_all_tools()
    tool_registry = ToolRegistry.get_instance()

    all_tools: list[BaseTool] = []
    if tools:
        for t in tools:
            if isinstance(t, BaseTool):
                all_tools.append(t)

    # Append default tools if not already present (core web exploration)
    # Following dcode conventions: web_search is only bound when Tavily is configured
    default_tool_names = ["fetch_url"]
    if getattr(settings, "has_tavily", False):
        default_tool_names.insert(0, "web_search")
    for tool_name in default_tool_names:
        if not any(getattr(t, "name", None) == tool_name for t in all_tools):
            try:
                all_tools.append(tool_registry.build_tool(tool_name, backend=composite_backend))
            except Exception as e:
                logger.warning("Failed to build registered tool %s: %s", tool_name, e)

    # 5. MCP Discovery
    mcp_tools_list: list[BaseTool] = []
    cached_infos: list[Any] | None = None
    mcp_configs: dict[str, Any] | None = None
    if mcp_tools is not None:
        mcp_tools_list = list(mcp_tools)
        all_tools.extend(mcp_tools)
    else:
        mcp_configs = discover_mcp_configs(project_scope_root)
        from opscloud.mcp.preload import (
            get_cached_mcp_server_infos,
            preload_mcp_metadata,
        )
        from opscloud.mcp.session_manager import (
            MCPSessionManager,
            build_mcp_tools_from_server_infos,
        )

        mcp_manager = MCPSessionManager.get_instance(mcp_configs or {})
        if mcp_configs:
            try:
                cached_infos = get_cached_mcp_server_infos()
                cached_by_name = {info.name: info for info in cached_infos}
                missing_configs = {
                    k: v
                    for k, v in mcp_configs.items()
                    if k not in cached_by_name or not cached_by_name[k].enabled or cached_by_name[k].status != "ok"
                }

                if missing_configs:
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None

                    if loop is not None and loop.is_running():
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            new_infos = pool.submit(asyncio.run, preload_mcp_metadata(missing_configs)).result()
                    else:
                        new_infos = asyncio.run(preload_mcp_metadata(missing_configs))
                    cached_infos = get_cached_mcp_server_infos()

                coordinator_infos = [
                    info for info in cached_infos if info.name in mcp_configs and info.enabled and info.status == "ok"
                ]
                list(coordinator_infos)

                discovered_tools = build_mcp_tools_from_server_infos(coordinator_infos, mcp_manager)
                if discovered_tools:
                    mcp_tools_list.extend(discovered_tools)
                    all_tools.extend(discovered_tools)
                logger.info("Active MCP servers loaded: %d (tools: %d)", len(mcp_configs), len(discovered_tools))
            except Exception as e:
                logger.warning("Failed to initialize MCP tools in agent factory: %s", e)

    # 6. Discover memory sources (ensuring ~/.opscloud/AGENTS.md exists)
    from opscloud.config.paths import ensure_user_agent_md

    ensure_user_agent_md(assistant_id)
    memory_registry = MemoryRegistry.get_instance(store=config_store)
    memory_sources_str = memory_registry.get_all_memory_sources(project_root=project_scope_root)

    # 7. Discover skills
    skill_registry = SkillRegistry.get_instance(store=config_store)
    skill_registry.discover_skills(project_scope_root, force=True)
    skill_registry.get_sources_for_middleware()

    # 8. Define approval policies and subagents
    interrupt_on: Any | None = None
    if auto_approve:
        interrupt_on = {}
    else:
        from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

        from opscloud.middleware.auto_mode import DynamicInterruptMapping

        default_tool_hitl_config: InterruptOnConfig = {
            "allowed_decisions": ["approve", "reject"],
            "description": _format_description,
            "when": _should_interrupt_tool_call,
        }
        interrupt_on = DynamicInterruptMapping(default_config=default_tool_hitl_config)

    user_agents_dir = paths.user_agents_dir(assistant_id)
    project_agents_dir = paths.project_agents_dir(project_scope_root)
    if not project_agents_dir.is_dir() and (project_scope_root / "agents").is_dir():
        project_agents_dir = project_scope_root / "agents"

    subagent_metas = list_subagents(
        user_agents_dir=user_agents_dir if user_agents_dir.is_dir() else None,
        project_agents_dir=project_agents_dir if project_agents_dir.is_dir() else None,
        project_root=project_scope_root,
        include_plugins=True,
    )

    subagent_by_name: dict[str, SubagentMetadata] = {}
    for meta in subagent_metas:
        name = meta.get("name")
        if name:
            subagent_by_name[name] = meta

    allow_list = getattr(settings, "shell_allow_list", None) or []

    from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT, SubAgent

    compiled_subagents: list[SubAgent] = []
    base_subagent_tools = [t for t in all_tools if t not in mcp_tools_list]
    for name, subagent_meta in subagent_by_name.items():
        model_spec = subagent_meta.get("model")
        sub_prompt = subagent_meta.get("system_prompt") or ""
        subagent_dict: dict[str, Any] = {
            "name": subagent_meta.get("name") or name,
            "description": subagent_meta.get("description") or "",
            "system_prompt": sub_prompt,
        }
        if model_spec:
            try:
                sub_model_res = create_model(model_spec)
                subagent_dict["model"] = sub_model_res.model
            except Exception as e:
                logger.warning("Failed to resolve model %s for subagent %s: %s", model_spec, name, e)

        subagent_name = subagent_meta.get("name") or name
        subagent_path = subagent_meta.get("path")
        custom_mw = subagent_meta.get("middleware")

        # Build per-subagent MCP config and tools for this subagent
        subagent_mcp_tools: list[BaseTool] = []
        sub_mcp_server_infos: list[Any] = []
        subagent_path_obj = Path(subagent_path) if subagent_path else None
        bundle_dir = None
        if subagent_path_obj:
            if subagent_path_obj.parent.name == "agents":
                bundle_dir = subagent_path_obj.parent.parent
            else:
                bundle_dir = subagent_path_obj.parent

        from opscloud.plugins.adapters.mcp import subagent_mcp_configs

        servers: dict[str, Any] = {}
        mcp_files_raw = subagent_meta.get("mcp_files")
        mcp_files: list[str | Path] | None = list(mcp_files_raw) if mcp_files_raw is not None else None
        if bundle_dir and bundle_dir.is_dir():
            servers.update(subagent_mcp_configs(subagent_name, bundle_dir, mcp_files, project_dir=project_scope_root))

        raw_mcp_cfg = subagent_meta.get("mcp_config")
        if raw_mcp_cfg and isinstance(raw_mcp_cfg, dict):
            servers.update(raw_mcp_cfg.get("mcpServers") or raw_mcp_cfg)

        if servers:
            from opscloud.mcp.preload import (
                get_cached_mcp_server_infos,
                preload_mcp_metadata,
            )
            from opscloud.mcp.session_manager import (
                MCPSessionManager,
                build_mcp_tools_from_server_infos,
            )

            sub_mcp_manager = MCPSessionManager.get_instance(servers, purge_missing=False)
            sub_mcp_manager.register_servers(servers)
            try:
                cached_infos = get_cached_mcp_server_infos()
                cached_by_name = {info.name: info for info in cached_infos}
                missing_configs = {k: v for k, v in servers.items() if k not in cached_by_name}

                if missing_configs:
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None

                    if loop is not None and loop.is_running():
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            new_infos = pool.submit(asyncio.run, preload_mcp_metadata(missing_configs)).result()
                    else:
                        new_infos = asyncio.run(preload_mcp_metadata(missing_configs))
                    cached_infos.extend(new_infos)
                    cached_by_name.update({info.name: info for info in new_infos})

                sub_mcp_server_infos = [cached_by_name[k] for k in servers if k in cached_by_name]
                sub_tools = build_mcp_tools_from_server_infos(sub_mcp_server_infos, sub_mcp_manager)
                if sub_tools:
                    subagent_mcp_tools.extend(sub_tools)
            except Exception as exc:
                logger.warning("Could not initialize subagent %s MCP tools: %s", subagent_name, exc)

        if subagent_mcp_tools:
            subagent_dict["tools"] = [*base_subagent_tools, *subagent_mcp_tools]
        else:
            subagent_dict["tools"] = list(base_subagent_tools)

        sub_middleware = _subagent_cli_middleware(
            has_explicit_model=bool(model_spec),
            assistant_id=assistant_id,
            subagent_name=subagent_name,
            allowed_tools=subagent_meta.get("tools"),
            allowed_skills=subagent_meta.get("skills"),
            capabilities=subagent_meta.get("capabilities"),
            interactive=interactive,
            shell_allow_list=allow_list if not interactive and allow_list else None,
            interrupt_on=interrupt_on,
            worktree_root=effective_cwd,
            subagent_path=subagent_path if subagent_path else None,
            custom_middleware=custom_mw if isinstance(custom_mw, list) else None,
            mcp_server_info=sub_mcp_server_infos or None,
            mcp_config=servers if not sub_mcp_server_infos else None,
            mcp_tools=subagent_mcp_tools or None,
        )
        if sub_middleware:
            subagent_dict["middleware"] = sub_middleware
        if interrupt_on is not None:
            subagent_dict["interrupt_on"] = {}

        compiled_subagents.append(cast(SubAgent, subagent_dict))
        logger.info(
            "Compiled subagent '%s' (tools: %d)",
            subagent_name,
            len(subagent_dict.get("tools", [])),
            extra={"subagent": subagent_name, "tools_count": len(subagent_dict.get("tools", []))},
        )

    if not any(sub.get("name") == GENERAL_PURPOSE_SUBAGENT["name"] for sub in compiled_subagents):
        gp_name = GENERAL_PURPOSE_SUBAGENT["name"]
        gp_description = GENERAL_PURPOSE_SUBAGENT["description"]
        gp_system_prompt = GENERAL_PURPOSE_SUBAGENT.get("system_prompt") or ""
        gp_middleware = _subagent_cli_middleware(
            has_explicit_model=False,
            assistant_id=assistant_id,
            subagent_name=gp_name,
            interactive=interactive,
            interrupt_on=interrupt_on,
            worktree_root=effective_cwd,
        )
        gp_subagent: dict[str, Any] = {
            "name": gp_name,
            "description": gp_description,
            "system_prompt": gp_system_prompt,
            "tools": list(base_subagent_tools),
            "middleware": gp_middleware,
        }
        if interrupt_on is not None:
            gp_subagent["interrupt_on"] = {}
        compiled_subagents.append(cast(SubAgent, gp_subagent))
        if gp_name not in subagent_by_name:
            subagent_by_name[gp_name] = cast(
                SubagentMetadata,
                {
                    "name": gp_name,
                    "description": gp_description,
                    "system_prompt": gp_system_prompt,
                    "is_plugin": False,
                },
            )
        logger.info(
            "Compiled subagent '%s'",
            gp_name,
            extra={"subagent": gp_name},
        )


    # 9. Build system prompt
    effective_prompt = system_prompt or get_base_system_prompt(
        assistant_id=assistant_id,
        interactive=interactive,
        cwd=effective_cwd,
        fs_tools=fs_tools,
        model_name=model_name,
        model_provider=model_provider,
        model_context_limit=model_context_limit,
        model_unsupported_modalities=model_unsupported_modalities,
        cloud_provider=cloud_provider or getattr(settings, "cloud_provider", "aws"),
        sandbox_type=getattr(settings, "sandbox_provider", "local"),
        has_search=any(getattr(t, "name", None) == "web_search" for t in all_tools),
    )

    # 10. Build ordered middleware stack (matching OpsCode execution order)
    agent_middleware: list[AgentMiddleware[Any, Any]] = []

    # 10.1 ConfigurableModelMiddleware, Jev Dynamic Model Router & Model Retry
    agent_middleware.append(ConfigurableModelMiddleware())
    agent_middleware.append(JevDynamicModelRouterMiddleware())
    agent_middleware.append(CodeModelRetryMiddleware())

    # 10.2 Non-interactive guards
    if not interactive:
        agent_middleware.append(GlmTerminalStallRecoveryMiddleware())
        if mcp_tools_list and (gated_names := gated_mcp_tool_names(mcp_tools_list)):
            agent_middleware.append(HeadlessMCPGuardMiddleware(gated_names))

    # 10.3 ResumeState, CostTracking, GoalTools
    agent_middleware.extend([ResumeStateMiddleware(), CostTrackingMiddleware(), GoalToolsMiddleware()])

    # 10.4 AskUserMiddleware
    if enable_ask_user and interactive:
        agent_middleware.append(AskUserMiddleware())

    # 10.5 MCP tool arguments and error handling
    if mcp_tools_list:
        if cached_infos or mcp_configs:
            from opscloud.middleware.mcp_context import MCPContextMiddleware

            agent_middleware.append(
                MCPContextMiddleware(mcp_server_info=cached_infos, mcp_config=mcp_configs)
            )
        agent_middleware.append(MCPToolMiddleware())

    # 10.5 MemoryMiddleware & ManagedMemoryGuardMiddleware
    if memory_sources_str:
        agent_middleware.append(
            MemoryMiddleware(
                backend=FilesystemBackend(virtual_mode=False),
                sources=memory_sources_str,
                system_prompt=get_memory_system_prompt(headless=not interactive),
            )
        )
        agent_middleware.append(ManagedMemoryGuardMiddleware(guarded_paths=memory_sources_str))

    # 10.6 PluginSkillsMiddleware (dynamic live discovery across plugin install/uninstall lifecycle)
    agent_middleware.append(
        PluginSkillsMiddleware(
            backend=FilesystemBackend(virtual_mode=False),
        )
    )

    # 10.7 CodeInterpreterMiddleware (if enabled)
    if enable_interpreter:
        try:
            from langchain_core._api import suppress_langchain_beta_warning
            from langchain_quickjs import CodeInterpreterMiddleware, PTCOption

            ptc_names = _resolve_ptc_option(
                getattr(settings, "interpreter_ptc", "safe"),
                tools=all_tools,
                acknowledge_unsafe=getattr(settings, "interpreter_ptc_acknowledge_unsafe", False),
                auto_approve=auto_approve,
            )
            ptc_option: PTCOption | None = cast(PTCOption, list(ptc_names)) if ptc_names is not None else None

            with suppress_langchain_beta_warning():
                agent_middleware.append(
                    CodeInterpreterMiddleware(
                        tool_name="js_eval",
                        timeout=getattr(settings, "interpreter_timeout_seconds", 30),
                        memory_limit=getattr(settings, "interpreter_memory_limit_mb", 64) * 1024 * 1024,
                        max_ptc_calls=getattr(settings, "interpreter_max_ptc_calls", 50),
                        max_result_chars=getattr(settings, "interpreter_max_result_chars", 50000),
                        ptc=ptc_option,
                        subagents=True,
                    )
                )
        except ImportError:
            logger.warning("langchain-quickjs is not installed. CodeInterpreterMiddleware disabled.")

    # 10.8 LocalContextMiddleware
    from opscloud.config.langsmith import get_langsmith_project_name

    tracing_project_name = get_langsmith_project_name()
    agent_middleware.append(
        LocalContextMiddleware(
            backend=composite_backend,
            working_dir=effective_cwd,
            tracing_project=tracing_project_name,
            user_tracing_project=getattr(settings, "user_langchain_project", tracing_project_name),
        )
    )

    # 10.9 ShellAllowListMiddleware
    allow_list = getattr(settings, "shell_allow_list", None) or []
    if allow_list:
        agent_middleware.append(ShellAllowListMiddleware(allow_list))

    # 10.10 HITL approval (AutoModeHITLMiddleware / AsyncApprovalHITLMiddleware)
    auto_mode_config: tuple[Path, list[str]] | None = None
    if interrupt_on is not None and interactive and not auto_approve:
        auto_mode_config = (Path(effective_cwd), list(allow_list))

    if auto_mode_config is not None and interrupt_on is not None:
        agent_middleware.append(
            AutoModeHITLMiddleware(
                interrupt_on,
                worktree_root=auto_mode_config[0],
                shell_allow_list=auto_mode_config[1],
            )
        )
    elif interrupt_on is not None:
        agent_middleware.append(AsyncApprovalHITLMiddleware(interrupt_on))

    # 10.11 ServerHooksMiddleware
    hooks_cwd = Path(effective_cwd) if effective_cwd else Path.cwd()
    agent_middleware.append(ServerHooksMiddleware(cwd=hooks_cwd, mcp_tools=mcp_tools_list))

    # 10.12 GoalCriteriaMiddleware
    criteria_agent = None
    fallback_agent = None
    criteria_context_tools: list[Any] = list(goal_criteria_tools or ())
    if not criteria_context_tools and mcp_tools_list:
        criteria_context_tools = [
            t
            for t in mcp_tools_list
            if (
                isinstance(getattr(t, "metadata", None), dict)
                and getattr(t, "metadata", {}).get("readOnlyHint") is True
            )
        ]

    try:
        criteria_skill_sources = skill_registry.get_sources_for_middleware(
            project_scope_root,
            include_subagent_skills=True,
            subagents=list(subagent_by_name.values()),
        )
        criteria_backend = getattr(composite_backend, "default", composite_backend)
        criteria_agent = create_goal_criteria_agent(
            model=active_model,
            repository_backend=criteria_backend,
            repository_root=str(project_scope_root),
            context_tools=criteria_context_tools,
            subagent_metas=list(subagent_by_name.values()),
            skill_sources=criteria_skill_sources,
        )
    except Exception as exc:
        logger.warning(
            "Failed to create goal criteria agent: %s; using fallback agent",
            exc,
        )

    try:
        fallback_agent = create_goal_criteria_fallback_agent(
            model=active_model,
        )
    except Exception as exc:
        logger.warning("Failed to create goal criteria fallback agent: %s", exc)

    from opscloud.tools.goal_tools import set_active_criteria_agent

    set_active_criteria_agent(criteria_agent, fallback_agent)

    agent_middleware.append(
        GoalCriteriaMiddleware(
            criteria_agent=criteria_agent,
            fallback_agent=fallback_agent,
        )
    )

    # 10.13 CLICompactionMiddleware
    agent_middleware.append(_create_cli_compaction_middleware(active_model, composite_backend))

    # 10.14 ReliableRubricMiddleware
    rubric_grader_tools = _create_rubric_grader_tools(composite_backend, repository_root=str(project_scope_root))
    grader_middleware: list[AgentMiddleware[Any, Any]] = [
        _WebSearchBudgetMiddleware(),
    ]
    agent_middleware.append(
        ReliableRubricMiddleware(
            model=active_model,
            system_prompt=_RUBRIC_GRADER_SYSTEM_PROMPT,
            tools=rubric_grader_tools,
            grader_middleware=grader_middleware,
            grader_context_schema=CLIContextSchema,
        )
    )

    # 10.15 SubagentsMiddleware
    agent_middleware.append(SubagentsMiddleware(subagent_metas=list(subagent_by_name.values())))

    # 10.16 UnifiedSystemMessageMiddleware
    agent_middleware.append(UnifiedSystemMessageMiddleware())

    # Ensure all middleware trace policies record full input state schemas in LangSmith
    from opscloud.config.langsmith import enable_full_middleware_tracing

    enable_full_middleware_tracing()

    # 11. Compile the agent graph
    from opscloud.subagents.loader import load_async_subagents

    resolved_async_subagents: list[AsyncSubAgent] = []
    if async_subagents is not None:
        resolved_async_subagents = list(async_subagents)
    else:
        try:
            resolved_async_subagents = load_async_subagents(store=config_store or store)
        except Exception as exc:
            logger.warning("Failed to load async subagents: %s", exc)
            resolved_async_subagents = []

    all_subagents: list[Any] = list(compiled_subagents)
    if resolved_async_subagents:
        all_subagents.extend(resolved_async_subagents)

    from opscloud.state import OpsCloudAgentState

    graph = create_deep_agent(
        model=active_model,
        system_prompt=effective_prompt,
        tools=all_tools,
        backend=composite_backend,
        subagents=all_subagents,
        middleware=agent_middleware,
        checkpointer=checkpointer,
        store=store,
        name=assistant_id,
        context_schema=CLIContextSchema,
        state_schema=OpsCloudAgentState,
    )

    return graph, composite_backend


__all__ = [
    "AgentContextSchema",
    "CLIContextSchema",
    "_resolve_ptc_option",
    "_subagent_cli_middleware",
    "create_opscloud_agent",
]


create_agent = create_opscloud_agent
create_k8s_autopilot_agent = create_opscloud_agent

__all__ = [
    'create_opscloud_agent',
    'create_agent',
    'create_k8s_autopilot_agent',
    'CLIContextSchema',
    'AgentContextSchema',
]
