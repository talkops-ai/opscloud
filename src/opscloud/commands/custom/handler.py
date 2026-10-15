"""CommandHandler implementation for zero-code custom commands.

Supports inline prompt injection, `context: fork` isolated subagent execution,
stacked prompt command chaining, and session re-invocation deduplication.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.commands.custom.expansion import (
    expand_custom_command_prompt,
    format_command_xml_tags,
)
from opscloud.commands.custom.loader import (
    discover_custom_commands,
    record_command_usage,
)
from opscloud.commands.custom.models import (
    CommandPermissionContext,
    CustomCommandDefinition,
    CustomCommandSource,
    ExpandedCommandPrompt,
)


def prepare_forked_command_context(
    expanded: ExpandedCommandPrompt,
    *,
    parent_messages: list[Any] | None = None,
) -> dict[str, Any]:
    """Prepare isolated subagent execution state for `context: fork`.

    Clones/isolates message history so the forked subagent runs with the
    expanded command prompt as its task, applies the command's `allowed-tools`,
    `disallowed-tools`, `model`, and `effort` overrides, and sets
    `_forked_command_turn = True` to prevent recursive subagent spawning.
    """
    return {
        "command_name": expanded.command_name,
        "agent_type": expanded.agent or "general-purpose",
        "prompt": expanded.expanded_prompt,
        "xml_command_message": expanded.xml_command_message,
        "permissions": expanded.permissions.to_dict(),
        "parent_message_count": len(parent_messages) if parent_messages else 0,
        "_forked_command_turn": True,
        "background": expanded.background,
    }


def _extract_context_fields(
    ctx: CommandContext | Any,
    override_args: str | None = None,
) -> tuple[str, Path | None, Path | None, str | None, Any, bool, dict[str, Any] | None]:
    """Extract args, cwd, project_dir, session_id, effort, execute_shell, and invoked_registry from CommandContext."""
    args = override_args if override_args is not None else getattr(ctx, "args", "") or ""
    extra: dict[str, Any] = getattr(ctx, "extra", None) or {}

    settings = getattr(ctx, "settings", None) or getattr(ctx, "config", None)
    cwd_raw = extra.get("cwd") or getattr(settings, "user_cwd", None)
    cwd = Path(cwd_raw) if cwd_raw else None

    project_raw = (
        extra.get("project_root")
        or extra.get("project_dir")
        or getattr(settings, "project_root", None)
    )
    project_dir = Path(project_raw) if project_raw else None

    session_obj = getattr(ctx, "session", None) or getattr(ctx, "session_state", None)
    session_id = (
        getattr(ctx, "thread_id", None)
        or getattr(session_obj, "thread_id", None)
        or extra.get("session_id")
    )

    effort = getattr(settings, "reasoning_effort", None) or extra.get("effort")
    execute_shell = bool(extra.get("execute_inline_shell", True))

    invoked_registry: dict[str, Any] | None = None
    if session_obj is not None:
        if not hasattr(session_obj, "invoked_custom_commands"):
            try:
                setattr(session_obj, "invoked_custom_commands", {})
            except Exception:
                pass
        invoked_registry = getattr(session_obj, "invoked_custom_commands", None)
    if invoked_registry is None and isinstance(extra.get("invoked_custom_commands"), dict):
        invoked_registry = extra["invoked_custom_commands"]

    return args, cwd, project_dir, session_id, effort, execute_shell, invoked_registry


class CustomCommandHandler(BaseCommandHandler):
    """Slash-command handler backed by a user, project, or plugin `.md` / `SKILL.md` file."""

    def __init__(self, definition: CustomCommandDefinition) -> None:
        self.definition = definition
        clean = definition.name.lstrip("/")
        self._name = f"/{clean}"
        self._description = definition.description
        hint = f" {definition.argument_hint}" if definition.argument_hint else " [arguments]"
        self.usage = f"/{clean}{hint}"
        self._aliases = tuple(
            f"/{a.lstrip('/')}" for a in definition.aliases if a.lstrip("/") and a.lstrip("/") != clean
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def aliases(self) -> tuple[str, ...]:
        return self._aliases

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.AUTOMATION

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.READ_ONLY

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(
        self,
        ctx_or_args: CommandContext | str,
        context: CommandContext | None = None,
    ) -> CommandResult:
        """Expand the custom command template and return structured execution payload."""
        if isinstance(ctx_or_args, str):
            raw_args = ctx_or_args
            ctx = context or CommandContext(app=None, args=raw_args)
        else:
            ctx = ctx_or_args
            raw_args = ctx.args

        record_command_usage(self.definition.name)

        (
            args,
            cwd,
            project_dir,
            session_id,
            effort,
            execute_shell,
            invoked_registry,
        ) = _extract_context_fields(ctx, override_args=raw_args)

        expanded = expand_custom_command_prompt(
            self.definition,
            args,
            cwd=cwd,
            project_dir=project_dir,
            session_id=session_id,
            effort=effort,
            execute_shell=execute_shell,
        )

        reinvoked = False
        if isinstance(invoked_registry, dict):
            existing = invoked_registry.get(self.definition.name)
            if existing and existing.get("content") == self.definition.body:
                reinvoked = True
            invoked_registry[self.definition.name] = {
                "name": self.definition.name,
                "path": str(self.definition.file_path),
                "content": self.definition.body,
                "last_args": args,
            }

        payload: dict[str, Any] = {
            "custom_command_prompt": expanded.expanded_prompt,
            "plugin_command_prompt": expanded.expanded_prompt,
            "xml_command_message": expanded.xml_command_message,
            "display_text": expanded.display_text,
            "command_name": expanded.command_name,
            "source": self.definition.source,
            "file_path": str(self.definition.file_path),
            "base_dir": str(expanded.base_dir) if expanded.base_dir else None,
            "referenced_files": expanded.referenced_files,
            "allowed_tools": expanded.permissions.allowed_tools,
            "disallowed_tools": expanded.permissions.disallowed_tools,
            "model": expanded.permissions.model,
            "effort": expanded.permissions.effort,
            "execution_context": expanded.execution_context,
            "agent": expanded.agent,
            "background": expanded.background,
            "permissions": expanded.permissions.to_dict(),
            "reinvoked": reinvoked,
            "stacked_commands": expanded.stacked_commands,
        }

        if expanded.execution_context == "fork":
            payload["forked_context"] = prepare_forked_command_context(expanded)

        # If running inside the TUI app, dispatch the expanded prompt + permissions to the agent
        app = getattr(ctx, "app", None)
        if app is not None and hasattr(app, "_send_to_agent"):
            full_prompt = f"{expanded.xml_command_message}\n\n{expanded.expanded_prompt}"
            if hasattr(app, "_run_custom_command_turn"):
                await app._run_custom_command_turn(full_prompt, payload)
            else:
                await app._send_to_agent(full_prompt)
            return CommandResult(
                success=True,
                message=None,
                data=payload,
                mount_as_app_message=False,
                notify=f"Triggered custom command {self._name}",
            )

        return CommandResult(
            success=True,
            message=expanded.expanded_prompt,
            data=payload,
            mount_as_app_message=True,
            notify=f"Triggered custom command {self._name}",
        )


async def execute_stacked_custom_commands(
    stacked: list[tuple[CustomCommandDefinition, str]],
    context: CommandContext,
) -> CommandResult:
    """Execute 2..5 stacked prompt commands in a single turn.

    Expands each command in order, joins their XML headers and expanded prompts,
    and unions their `allowed-tools` and `disallowed-tools` permissions.
    """
    (
        _,
        cwd,
        project_dir,
        session_id,
        effort,
        execute_shell,
        _,
    ) = _extract_context_fields(context)

    expanded_list: list[ExpandedCommandPrompt] = []
    combined_allowed: list[str] = []
    combined_disallowed: list[str] = []
    combined_files: list[str] = []
    command_names: list[str] = []
    override_model: str | None = None
    override_effort: Any = None
    execution_context = "inline"
    agent_override: str | None = None

    for cmd_def, cmd_args in stacked:
        record_command_usage(cmd_def.name)
        exp = expand_custom_command_prompt(
            cmd_def,
            cmd_args,
            cwd=cwd,
            project_dir=project_dir,
            session_id=session_id,
            effort=effort,
            execute_shell=execute_shell,
        )
        expanded_list.append(exp)
        command_names.append(cmd_def.name)
        for tool in exp.permissions.allowed_tools:
            if tool not in combined_allowed:
                combined_allowed.append(tool)
        for tool in exp.permissions.disallowed_tools:
            if tool not in combined_disallowed:
                combined_disallowed.append(tool)
        for ref in exp.referenced_files:
            if ref not in combined_files:
                combined_files.append(ref)
        if exp.permissions.model:
            override_model = exp.permissions.model
        if exp.permissions.effort is not None:
            override_effort = exp.permissions.effort
        if exp.execution_context == "fork":
            execution_context = "fork"
            if exp.agent:
                agent_override = exp.agent

    combined_prompt = "\n\n---\n\n".join(e.expanded_prompt for e in expanded_list)
    combined_xml = "\n".join(
        format_command_xml_tags(cmd_def.name, cmd_args) for cmd_def, cmd_args in stacked
    )
    display_text = " ".join(e.display_text for e in expanded_list)
    primary_name = command_names[0] if command_names else ""

    merged_permissions = CommandPermissionContext(
        command_name=primary_name,
        allowed_tools=combined_allowed,
        disallowed_tools=combined_disallowed,
        model=override_model,
        effort=override_effort,
        execution_context=execution_context,  # type: ignore[arg-type]
        agent=agent_override,
    )

    payload: dict[str, Any] = {
        "custom_command_prompt": combined_prompt,
        "plugin_command_prompt": combined_prompt,
        "xml_command_message": combined_xml,
        "display_text": display_text,
        "command_name": primary_name,
        "referenced_files": combined_files,
        "allowed_tools": combined_allowed,
        "disallowed_tools": combined_disallowed,
        "model": override_model,
        "effort": override_effort,
        "execution_context": execution_context,
        "agent": agent_override,
        "permissions": merged_permissions.to_dict(),
        "stacked_commands": command_names,
    }

    app = getattr(context, "app", None)
    if app is not None and hasattr(app, "_send_to_agent"):
        full_prompt = f"{combined_xml}\n\n{combined_prompt}"
        if hasattr(app, "_run_custom_command_turn"):
            await app._run_custom_command_turn(full_prompt, payload)
        else:
            await app._send_to_agent(full_prompt)
        return CommandResult(
            success=True,
            message=None,
            data=payload,
            mount_as_app_message=False,
        )

    return CommandResult(
        success=True,
        message=combined_prompt,
        data=payload,
        mount_as_app_message=True,
    )


def discover_custom_command_handlers(
    *,
    cwd: Path | None = None,
    project_root: Path | None = None,
    extra_dirs: list[tuple[Path, CustomCommandSource, str | None]] | None = None,
    include_plugins: bool = True,
    include_conditional: bool = True,
    use_cache: bool = True,
) -> list[CustomCommandHandler]:
    """Discover all custom commands and wrap them as `CustomCommandHandler` instances."""
    definitions = discover_custom_commands(
        cwd=cwd,
        project_root=project_root,
        extra_dirs=extra_dirs,
        include_plugins=include_plugins,
        include_conditional=include_conditional,
        use_cache=use_cache,
    )
    return [CustomCommandHandler(defn) for defn in definitions]
