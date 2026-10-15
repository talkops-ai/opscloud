"""LangChain AgentMiddleware for custom slash commands and skills.

Provides:
- Session-scoped `allowed-tools` (`command_permissions`) and turn-scoped
  `disallowed-tools` cleared on the next normal user turn.
- Fine-grained tool permission rule matching (`Bash(git *)`, `Read(/path/**)`).
- Per-command `model` (`haiku`, `sonnet`, `opus`, `inherit`, or full spec) and
  `effort` (`low`, `medium`, `high`, `max`, or integer) overrides on `ModelRequest`.
- `context: fork` recursion guard (`_forked_command_turn`).
- Dynamic activation of path-conditional (`paths:`) commands when matching files
  are accessed.
- Character-budgeted `Skill` tool and system prompt injection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
import fnmatch
from pathlib import Path
import re
from typing import Any

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt.tool_node import ToolCallRequest

from opscloud.commands.custom.expansion import (
    expand_custom_command_prompt,
    resolve_command_with_colon_fallback,
)
from opscloud.commands.custom.loader import (
    activate_conditional_commands_for_paths,
    discover_custom_commands,
    record_command_usage,
)
from opscloud.commands.custom.models import CustomCommandDefinition
from opscloud.middleware.registry import register_middleware
from opscloud.middleware.tool_filter import TOOL_ALIAS_MAP
from opscloud.prompts.custom_commands import (
    build_skill_tool_prompt,
    format_commands_for_budget,
)
from opscloud.state.custom_command_channels import CustomCommandChannels
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_PARAMETERIZED_RULE_RE = re.compile(r"^([A-Za-z0-9_:-]+)\((.+)\)$")

CLAUDE_MODEL_SHORT_ALIASES: dict[str, str] = {
    "haiku": "anthropic:claude-haiku-4-5",
    "sonnet": "anthropic:claude-sonnet-4-5",
    "opus": "anthropic:claude-opus-4-6",
}

FORKED_TURN_BLOCKED_SUBAGENT_TOOLS: frozenset[str] = frozenset({
    "task",
    "agent",
    "invoke_subagent",
    "spawn_subagent",
    "skill",
})

FILE_ACCESS_ARG_KEYS: tuple[str, ...] = (
    "file_path",
    "path",
    "target_file",
    "AbsolutePath",
    "TargetFile",
    "directory",
)


def resolve_command_model_spec(model_str: str | None) -> str | None:
    """Resolve short model aliases (`haiku`, `sonnet`, `opus`, `inherit`) or full specs."""
    if not model_str:
        return None
    cleaned = model_str.strip()
    low = cleaned.lower()
    if low == "inherit":
        return None
    if low in CLAUDE_MODEL_SHORT_ALIASES:
        return CLAUDE_MODEL_SHORT_ALIASES[low]
    return cleaned


def _canonical_tool_names(name: str) -> set[str]:
    """Return lowercase tool name plus its registered OpsCloud tool aliases."""
    low = name.strip().lower()
    names = {low}
    if low in TOOL_ALIAS_MAP:
        names.update(alias.lower() for alias in TOOL_ALIAS_MAP[low])
    for canonical, aliases in TOOL_ALIAS_MAP.items():
        if low in [a.lower() for a in aliases]:
            names.add(canonical.lower())
            names.update(a.lower() for a in aliases)
    return names


def _extract_primary_tool_arg(tool_name: str, tool_args: Mapping[str, Any]) -> str:
    """Extract command string or file path from `tool_args` for parameterized rule matching."""
    canonical = _canonical_tool_names(tool_name)
    if canonical & {"bash", "execute", "run_command"}:
        for k in ("command", "cmd", "CommandLine", "script"):
            val = tool_args.get(k)
            if isinstance(val, str):
                return val.strip()
    for k in (*FILE_ACCESS_ARG_KEYS, "command", "query", "pattern", "url"):
        val = tool_args.get(k)
        if isinstance(val, str):
            return val.strip()
    return ""


def matches_tool_permission_rule(
    tool_name: str,
    tool_args: Mapping[str, Any] | None,
    rule: str,
) -> bool:
    """Return True if `(tool_name, tool_args)` matches a tool permission rule.

    Supports:
    - Bare tool names: `Read`, `Bash`, `Edit`, `mcp__server__*`
    - Parameterized rules: `Bash(git *)`, `Bash(npm run test:*)`, `Read(/path/**)`
    """
    cleaned_rule = rule.strip()
    if not cleaned_rule:
        return False

    tool_names = _canonical_tool_names(tool_name)
    m = _PARAMETERIZED_RULE_RE.match(cleaned_rule)
    if not m:
        rule_low = cleaned_rule.lower()
        if any(fnmatch.fnmatch(candidate, rule_low) for candidate in tool_names):
            return True
        return False

    rule_tool, rule_arg_pat = m.group(1).strip().lower(), m.group(2).strip()
    rule_tool_candidates = _canonical_tool_names(rule_tool)
    if not (tool_names & rule_tool_candidates):
        return False

    if not tool_args:
        return rule_arg_pat in ("*", "**")

    actual_arg = _extract_primary_tool_arg(tool_name, tool_args)
    if not actual_arg:
        return False

    # Normalize `prefix:*` syntax (e.g. `git commit:*` -> `git commit*`)
    norm_pat = rule_arg_pat[:-2] + "*" if rule_arg_pat.endswith(":*") else rule_arg_pat
    if fnmatch.fnmatch(actual_arg, norm_pat):
        return True
    if norm_pat.endswith(" *") and actual_arg == norm_pat[:-2]:
        return True
    return False


def is_tool_disallowed(
    tool_name: str,
    tool_args: Mapping[str, Any] | None,
    disallowed_rules: Sequence[str],
) -> bool:
    """Return True if `tool_name` / `tool_args` matches any rule in `disallowed_rules`."""
    return any(matches_tool_permission_rule(tool_name, tool_args, r) for r in disallowed_rules)


def is_tool_preapproved(
    tool_name: str,
    tool_args: Mapping[str, Any] | None,
    allowed_rules: Sequence[str],
) -> bool:
    """Return True if `tool_name` / `tool_args` matches any rule in `allowed_rules`."""
    return any(matches_tool_permission_rule(tool_name, tool_args, r) for r in allowed_rules)


def _is_unconditionally_disallowed_tool(tool_name: str, disallowed_rules: Sequence[str]) -> bool:
    """Return True if `tool_name` is blocked by an unparameterized rule in `disallowed_rules`."""
    for rule in disallowed_rules:
        if _PARAMETERIZED_RULE_RE.match(rule.strip()):
            continue
        if matches_tool_permission_rule(tool_name, None, rule):
            return True
    return False


def create_custom_command_skill_tool(
    *,
    cwd: Path | None = None,
    project_root: Path | None = None,
) -> BaseTool:
    """Create the `Skill` tool for model-initiated skill and custom command invocation."""
    commands = discover_custom_commands(
        cwd=cwd,
        project_root=project_root,
        include_conditional=False,
    )
    description = build_skill_tool_prompt(commands)

    def _invoke_skill(
        skill: str,
        args: str = "",
        v__args: str | None = None,
        **kwargs: Any,
    ) -> str:
        effective_args = args if args else (v__args or kwargs.get("arguments") or "")
        clean_name = skill.strip().lstrip("/")
        all_cmds = discover_custom_commands(
            cwd=cwd,
            project_root=project_root,
            include_conditional=True,
            use_cache=False,
        )
        by_name: dict[str, CustomCommandDefinition] = {}
        for c in all_cmds:
            by_name[c.name] = c
            for alias in c.aliases:
                by_name.setdefault(alias, c)

        resolved = resolve_command_with_colon_fallback(clean_name, effective_args, by_name.get)
        if resolved is None:
            try:
                from opscloud.commands.custom.loader import load_custom_command_file
                from opscloud.skills.invocation import (
                    discover_skills_and_roots,
                    match_discovered_skill,
                )

                discovered_skills, _ = discover_skills_and_roots(path_base=cwd or project_root)
                matched_skill = match_discovered_skill(discovered_skills, clean_name)
                if matched_skill is not None:
                    skill_path_str = (
                        str(matched_skill.get("path") or "")
                        if isinstance(matched_skill, Mapping)
                        else str(getattr(matched_skill, "path", "") or "")
                    )
                    if skill_path_str:
                        skill_file = Path(skill_path_str)
                        if skill_file.is_dir():
                            skill_file = skill_file / "SKILL.md"
                        if skill_file.is_file():
                            src_raw = (
                                str(matched_skill.get("source") or "")
                                if isinstance(matched_skill, Mapping)
                                else str(getattr(matched_skill, "source", "") or "")
                            )
                            cmd_source = "bundled" if src_raw == "built-in" else "userSettings"
                            loaded_def = load_custom_command_file(
                                skill_file,
                                skill_file.parent.parent,
                                source=cmd_source,  # type: ignore[arg-type]
                                project_dir=project_root,
                            )
                            if loaded_def is not None:
                                resolved = (loaded_def, effective_args)
            except Exception:
                pass

        if resolved is None:
            available = ", ".join(sorted(c.name for c in all_cmds if not c.disable_model_invocation))
            return f"Unknown skill or custom command: '{clean_name}'. Available: {available or 'none'}"

        cmd_def, remaining_args = resolved
        if cmd_def.disable_model_invocation:
            return (
                f"Skill '{cmd_def.name}' has disable-model-invocation=true and can only "
                f"be invoked directly by the user via /{cmd_def.name}."
            )

        record_command_usage(cmd_def.name)
        expanded = expand_custom_command_prompt(
            cmd_def,
            remaining_args,
            cwd=cwd,
            project_dir=project_root,
        )
        return f"{expanded.xml_command_message}\n\n{expanded.expanded_prompt}"

    from pydantic import BaseModel, ConfigDict, Field

    class SkillToolSchema(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        skill: str = Field(description="The skill or custom command name to execute")
        args: str = Field(default="", description="Optional arguments for the skill")

    return StructuredTool.from_function(
        func=_invoke_skill,
        name="Skill",
        description=description,
        args_schema=SkillToolSchema,
    )


@register_middleware(name="custom_commands")
class CustomCommandsMiddleware(AgentMiddleware):
    """LangChain middleware managing custom command permissions, model/effort overrides, and path activation."""

    state_schema = CustomCommandChannels

    def __init__(
        self,
        *,
        cwd: Path | None = None,
        project_root: Path | None = None,
        enforce_allowed_tools: bool = False,
        inject_system_prompt: bool = True,
        model_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.cwd = cwd
        self.project_root = project_root
        self.enforce_allowed_tools = enforce_allowed_tools
        self.inject_system_prompt = inject_system_prompt
        self._model_resolver = model_resolver

    def before_agent(
        self,
        state: Mapping[str, Any],
        runtime: Any = None,
    ) -> dict[str, Any] | None:
        """Bind or clear per-turn custom command permissions before the agent turn starts."""
        req = state.get("custom_command_request")
        existing_perms = dict(state.get("_active_command_permissions") or {})
        session_allowed = list(existing_perms.get("allowed_tools") or [])

        if isinstance(req, Mapping) and req.get("command_name"):
            cmd_name = str(req["command_name"])
            new_allowed = list(req.get("allowed_tools") or [])
            merged_allowed = list(dict.fromkeys([*session_allowed, *new_allowed]))
            disallowed = list(req.get("disallowed_tools") or [])
            model_override = resolve_command_model_spec(req.get("model"))
            effort_override = req.get("effort")
            exec_ctx = "fork" if req.get("execution_context") == "fork" else "inline"

            updated_perms: dict[str, Any] = {
                "command_name": cmd_name,
                "allowed_tools": merged_allowed,
                "disallowed_tools": disallowed,
                "model": model_override,
                "effort": effort_override,
                "execution_context": exec_ctx,
                "agent": req.get("agent"),
                "base_dir": req.get("base_dir"),
            }

            agent_id = str(req.get("agent") or "main")
            invoked_key = f"{agent_id}:{cmd_name}"
            invoked_entry = {
                invoked_key: {
                    "name": cmd_name,
                    "path": req.get("file_path", ""),
                    "content": req.get("custom_command_prompt", ""),
                }
            }

            return {
                "custom_command_request": None,
                "_active_command_permissions": updated_perms,
                "_invoked_custom_commands": invoked_entry,
                "_forked_command_turn": exec_ctx == "fork",
            }

        # Normal user turn:
        # Clear turn-scoped `disallowed_tools`, `model`, `effort`, and `_forked_command_turn`
        # while preserving session-level `allowed_tools` in `command_permissions`.
        if existing_perms.get("disallowed_tools") or existing_perms.get("model") or existing_perms.get("effort") is not None or state.get("_forked_command_turn"):
            cleaned_perms = {
                "command_name": existing_perms.get("command_name", ""),
                "allowed_tools": session_allowed,
                "disallowed_tools": [],
                "model": None,
                "effort": None,
                "execution_context": "inline",
                "agent": None,
                "base_dir": existing_perms.get("base_dir"),
            }
            return {
                "_active_command_permissions": cleaned_perms,
                "_forked_command_turn": False,
            }

        return None

    async def abefore_agent(
        self,
        state: Mapping[str, Any],
        runtime: Any = None,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.before_agent, state, runtime)

    def _prepare_model_request(self, request: ModelRequest) -> ModelRequest:
        state = request.state if isinstance(request.state, Mapping) else {}
        perms = state.get("_active_command_permissions") or {}
        is_forked = bool(state.get("_forked_command_turn"))

        overrides: dict[str, Any] = {}

        # 1. Per-command model and/or effort override
        model_spec = resolve_command_model_spec(perms.get("model"))
        effort_val = perms.get("effort")
        if model_spec or effort_val is not None:
            resolved_model = None
            if self._model_resolver is not None:
                try:
                    resolved_model = self._model_resolver(model_spec or "")
                except Exception:
                    logger.debug("Custom model resolver failed for %s", model_spec)
            else:
                extra_kw = (
                    {"reasoning_effort": str(effort_val)}
                    if effort_val is not None
                    else None
                )
                try:
                    from opscloud.model.factory import create_model

                    res = create_model(model_spec, extra_kwargs=extra_kw)
                    resolved_model = getattr(res, "model", res)
                except Exception:
                    logger.debug(
                        "create_model fallback skipped for model=%s effort=%s",
                        model_spec,
                        effort_val,
                    )
                    if model_spec and effort_val is not None:
                        try:
                            from opscloud.model.factory import create_model

                            res = create_model(None, extra_kwargs=extra_kw)
                            resolved_model = getattr(res, "model", res)
                        except Exception:
                            logger.debug(
                                "create_model effort-only fallback skipped for effort=%s",
                                effort_val,
                            )
            if resolved_model is not None:
                overrides["model"] = resolved_model

        # 2. Isolate message history for `context: fork` turns
        if is_forked and request.messages:
            last_human_idx: int | None = None
            for idx in range(len(request.messages) - 1, -1, -1):
                msg = request.messages[idx]
                msg_type = (
                    msg.get("role") or msg.get("type")
                    if isinstance(msg, Mapping)
                    else getattr(msg, "type", None)
                )
                if msg_type in ("human", "user"):
                    last_human_idx = idx
                    break
            if last_human_idx is not None and last_human_idx > 0:
                overrides["messages"] = list(request.messages[last_human_idx:])

        # 3. Filter tools for unparameterized disallowed_tools & forked turn recursion guard
        disallowed_rules: list[str] = list(perms.get("disallowed_tools") or [])
        if (disallowed_rules or is_forked) and request.tools:
            filtered_tools: list[Any] = []
            for tool_obj in request.tools:
                t_name = (
                    tool_obj.get("name", "")
                    if isinstance(tool_obj, Mapping)
                    else getattr(tool_obj, "name", "")
                )
                if not t_name:
                    filtered_tools.append(tool_obj)
                    continue
                if is_forked and t_name.lower() in FORKED_TURN_BLOCKED_SUBAGENT_TOOLS:
                    continue
                if disallowed_rules and _is_unconditionally_disallowed_tool(t_name, disallowed_rules):
                    continue
                filtered_tools.append(tool_obj)
            overrides["tools"] = filtered_tools

        # 4. Inject character-budgeted custom command listing into system message
        if self.inject_system_prompt:
            active_cmds = discover_custom_commands(
                cwd=self.cwd,
                project_root=self.project_root,
                include_conditional=False,
            )
            listing = format_commands_for_budget(active_cmds)
            if listing:
                section = (
                    "<custom_commands>\n"
                    "The following zero-code slash commands and skills are available in this workspace:\n"
                    f"{listing}\n"
                    "</custom_commands>"
                )
                if request.system_message is not None:
                    overrides["system_message"] = append_to_system_message(
                        request.system_message, section
                    )
                else:
                    overrides["system_message"] = SystemMessage(content=section)

        if overrides:
            return request.override(**overrides)
        return request

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        modified_req = self._prepare_model_request(request)
        return handler(modified_req)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        modified_req = await asyncio.to_thread(self._prepare_model_request, request)
        return await handler(modified_req)

    def _validate_and_track_tool_call(
        self,
        request: ToolCallRequest,
    ) -> ToolMessage | None:
        """Check active command permissions and trigger conditional `paths:` activation."""
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", ""))
        tool_args = tool_call.get("args") or {}
        tool_call_id = str(tool_call.get("id", ""))

        state = request.state if isinstance(request.state, Mapping) else {}
        perms = state.get("_active_command_permissions") or {}
        is_forked = bool(state.get("_forked_command_turn"))

        if is_forked and tool_name.lower() in FORKED_TURN_BLOCKED_SUBAGENT_TOOLS:
            return ToolMessage(
                content=(
                    f"Tool '{tool_name}' is blocked inside a forked custom command subagent turn "
                    "to prevent recursive subagent spawning."
                ),
                tool_call_id=tool_call_id,
                name=tool_name,
                status="error",
            )

        disallowed_rules: list[str] = list(perms.get("disallowed_tools") or [])
        if disallowed_rules and is_tool_disallowed(tool_name, tool_args, disallowed_rules):
            return ToolMessage(
                content=(
                    f"Tool call '{tool_name}' was blocked by active custom command "
                    f"disallowed-tools rules: {disallowed_rules}"
                ),
                tool_call_id=tool_call_id,
                name=tool_name,
                status="error",
            )

        allowed_rules: list[str] = list(perms.get("allowed_tools") or [])
        if self.enforce_allowed_tools and allowed_rules:
            if not is_tool_preapproved(tool_name, tool_args, allowed_rules):
                return ToolMessage(
                    content=(
                        f"Tool call '{tool_name}' is not permitted by the active custom command's "
                        f"allowed-tools rules: {allowed_rules}"
                    ),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                    status="error",
                )

        # Dynamic conditional `paths:` activation when file paths are accessed
        if isinstance(tool_args, Mapping):
            accessed_paths: list[str] = []
            for key in FILE_ACCESS_ARG_KEYS:
                val = tool_args.get(key)
                if isinstance(val, str) and val.strip():
                    accessed_paths.append(val.strip())
            if accessed_paths:
                activate_conditional_commands_for_paths(
                    accessed_paths,
                    cwd=self.cwd,
                    project_root=self.project_root,
                )

        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Any],
    ) -> ToolMessage | Any:
        blocked = self._validate_and_track_tool_call(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Any]],
    ) -> ToolMessage | Any:
        blocked = await asyncio.to_thread(self._validate_and_track_tool_call, request)
        if blocked is not None:
            return blocked
        return await handler(request)
