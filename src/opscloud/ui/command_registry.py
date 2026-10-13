"""Unified slash-command registry for OpsCloud.

Every slash command is declared once as an authoritative `CommandDescriptor` in
`COMMAND_DESCRIPTORS`. Autocomplete entries, bypass-tier lookups, and routing
projections are derived automatically from this single source of truth.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    from opscloud.commands._base import BaseCommandHandler


from opscloud.commands._types import BypassTier


class CommandEntry(NamedTuple):
    """Projection carrying only fields needed for autocomplete."""

    name: str
    description: str
    hidden_keywords: str = ""
    argument_hint: str = ""
    display_name: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandDescriptor:
    """Authoritative descriptor carrying metadata, routing, and policy for a slash command."""

    name: str
    description: str
    handler_type: type[BaseCommandHandler] | None = None
    bypass_tier: BypassTier = BypassTier.QUEUED
    aliases: tuple[str, ...] = ()
    hidden_keywords: str = ""
    argument_hint: str = ""
    ui_mode: Literal["message", "modal", "inline"] = "message"

    def to_entry(self) -> CommandEntry:
        """Project this command into a CommandEntry for autocomplete."""
        return CommandEntry(
            name=self.name,
            description=self.description,
            hidden_keywords=self.hidden_keywords,
            argument_hint=self.argument_hint,
        )


SlashCommand = CommandDescriptor


# ── Handler Class Imports for Single Source of Truth ─────────────────────────

from opscloud.commands.core.auth import LoginHandler, LogoutHandler
from opscloud.commands.core.bug import BugHandler
from opscloud.commands.core.clear import ClearHandler, ForceClearHandler
from opscloud.commands.core.cloud import CloudHandler
from opscloud.commands.core.compact import CompactHandler
from opscloud.commands.core.config_cmd import ConfigHandler
from opscloud.commands.core.context import ContextHandler
from opscloud.commands.core.cost import CostHandler
from opscloud.commands.core.doctor import DoctorHandler
from opscloud.commands.core.effort import EffortHandler
from opscloud.commands.core.exit_cmd import ExitHandler
from opscloud.commands.core.fast import FastHandler
from opscloud.commands.core.help_cmd import HelpHandler
from opscloud.commands.core.mcp import McpHandler
from opscloud.commands.core.model import ModelHandler
from opscloud.commands.core.permissions import PermissionsHandler
from opscloud.commands.core.plugins import PluginsHandler
from opscloud.commands.core.pool import PoolHandler
from opscloud.commands.core.resume import ResumeHandler
from opscloud.commands.core.skills import SkillsHandler

from opscloud.commands.power.agents import AgentInvokeHandler, AgentsHandler
from opscloud.commands.power.btw import BtwHandler
from opscloud.commands.power.copy import CopyHandler
from opscloud.commands.power.goal import GoalHandler
from opscloud.commands.power.loop import LoopHandler
from opscloud.commands.power.memory import MemoryHandler, RememberHandler
from opscloud.commands.power.review import ReviewHandler
from opscloud.commands.power.rubric import RubricHandler
from opscloud.commands.power.runtime import (
    AutoUpdateHandler,
    InstallHandler,
    ReloadHandler,
    RestartHandler,
    UpdateHandler,
)
from opscloud.commands.power.skill_creator import SkillCreatorHandler
from opscloud.commands.power.skill_invoke import SkillInvokeHandler
from opscloud.commands.power.tasks import TasksHandler
from opscloud.commands.power.trace import TraceHandler
from opscloud.commands.power.ui_toggles import (
    AutoApprovalHandler,
    ManualApprovalHandler,
    NotificationsHandler,
    ScrollbarHandler,
    SmartApprovalHandler,
    ThemeHandler,
    TimestampsHandler,
)
from opscloud.commands.power.version import VersionHandler
from opscloud.commands.power.workflow import WorkflowHandler, WorkflowInvokeHandler


# ── Authoritative Command Descriptors ────────────────────────────────────────

COMMAND_DESCRIPTORS: tuple[CommandDescriptor, ...] = (
    CommandDescriptor(
        name="/agent",
        handler_type=AgentsHandler,
        aliases=("/agents",),
        description="Activate a plugin agent session or manage active agent",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="modal",
        argument_hint="[<name> [prompt]|show|clear|status]",
        hidden_keywords="agent subagent persona switch activate session",
    ),
    CommandDescriptor(
        name="/agent:",
        handler_type=AgentInvokeHandler,
        aliases=("/agents:",),
        description="Activate a plugin agent session by name directly",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        argument_hint="<name> [prompt]",
        hidden_keywords="agent subagent persona activate direct",
    ),
    CommandDescriptor(
        name="/auto",
        handler_type=AutoApprovalHandler,
        description="Switch to Auto approval mode",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        ui_mode="inline",
        hidden_keywords="approval mode classifier automatic auto-approve shift+tab",
    ),
    CommandDescriptor(
        name="/auto-update",
        handler_type=AutoUpdateHandler,
        description="Toggle automatic update checks on startup",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        ui_mode="inline",
        argument_hint="[on|off|status]",
        hidden_keywords="auto-update autoupdate check startup",
    ),
    CommandDescriptor(
        name="/btw",
        handler_type=BtwHandler,
        aliases=("/aside",),
        description="Provide side commentary or lightweight guidance without derailing flow",
        bypass_tier=BypassTier.IMMEDIATE,
        ui_mode="message",
        argument_hint="<question>",
        hidden_keywords="btw by the way note aside context hint",
    ),
    CommandDescriptor(
        name="/bug",
        handler_type=BugHandler,
        description="Report a bug or provide feedback",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        hidden_keywords="bug feedback report issue problem error crash",
        aliases=("/feedback",),
    ),
    CommandDescriptor(
        name="/clear",
        handler_type=ClearHandler,
        description="Clear the chat and start a new thread",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="inline",
        hidden_keywords="reset new clean",
    ),
    CommandDescriptor(
        name="/cloud",
        handler_type=CloudHandler,
        description="Select active cloud profile (AWS) or manage cloud configuration",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        ui_mode="modal",
        hidden_keywords="aws cloud profile account credentials iac provider",
        argument_hint="[profile-name | status | list]",
        aliases=("/aws",),
    ),
    CommandDescriptor(
        name="/compact",
        handler_type=CompactHandler,
        description="Summarize and offload older conversation history to free context window space",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        hidden_keywords="compact offload summarize context free",
        aliases=("/offload",),
    ),
    CommandDescriptor(
        name="/config",
        handler_type=ConfigHandler,
        description="View and manage OpsCloud configuration",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        ui_mode="modal",
        hidden_keywords="config settings preferences environment env vars",
        argument_hint="[show|set <key> <value>|reset <key>|path]",
    ),
    CommandDescriptor(
        name="/context",
        handler_type=ContextHandler,
        description="Display unified context window token usage and loaded resource counts",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        hidden_keywords="context window tokens usage capacity resources attached",
    ),
    CommandDescriptor(
        name="/copy",
        handler_type=CopyHandler,
        description="Copy the latest assistant message to clipboard",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        ui_mode="inline",
        hidden_keywords="clipboard copy",
    ),
    CommandDescriptor(
        name="/cost",
        handler_type=CostHandler,
        aliases=("/tokens",),
        description="Display token usage and cost metrics (deprecated)",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        hidden_keywords="cost tokens usage pricing",
    ),
    CommandDescriptor(
        name="/doctor",
        handler_type=DoctorHandler,
        description="Run diagnostic health checks on OpsCloud environment",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        hidden_keywords="doctor diagnose health check debug troubleshoot deps dependencies",
    ),
    CommandDescriptor(
        name="/effort",
        handler_type=EffortHandler,
        description="Set reasoning effort level for current model",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="modal",
        hidden_keywords="reasoning thinking level budget effort depth",
        argument_hint="[low|medium|high|max|clear]",
    ),
    CommandDescriptor(
        name="/exit",
        handler_type=ExitHandler,
        description="Exit the application",
        bypass_tier=BypassTier.ALWAYS,
        ui_mode="inline",
        hidden_keywords="exit quit stop bye q",
        aliases=("/quit", "/q"),
    ),
    CommandDescriptor(
        name="/fast",
        handler_type=FastHandler,
        description="Toggle fast mode (cheapest model with low reasoning effort)",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="inline",
        hidden_keywords="fast quick speed cheap haiku flash lite",
    ),
    CommandDescriptor(
        name="/force-clear",
        handler_type=ForceClearHandler,
        description="Stop active work, clear the chat, and start a new thread",
        bypass_tier=BypassTier.ALWAYS,
        ui_mode="inline",
        hidden_keywords="reset interrupt stop kill",
    ),
    CommandDescriptor(
        name="/goal",
        handler_type=GoalHandler,
        description="Set a persistent objective with acceptance criteria",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        hidden_keywords="objective criteria rubric target",
        argument_hint="[<objective>|amend <feedback>|pause|resume|show|clear|model|max-iterations]",
    ),
    CommandDescriptor(
        name="/help",
        handler_type=HelpHandler,
        description="Show help and available commands",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        ui_mode="message",
        hidden_keywords="info doc commands usage",
    ),
    CommandDescriptor(
        name="/install",
        handler_type=InstallHandler,
        description="Install an optional extra package or provider dependency",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        argument_hint="<extra|package> [--package] [--force]",
        hidden_keywords="install pip uv package add",
    ),
    CommandDescriptor(
        name="/login",
        handler_type=LoginHandler,
        description="Open auth manager to manage API keys and credentials",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        ui_mode="modal",
        hidden_keywords="auth login connect api key credentials provider",
        aliases=("/auth", "/connect"),
    ),
    CommandDescriptor(
        name="/logout",
        handler_type=LogoutHandler,
        description="Revoke stored API credentials for current session",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="inline",
        hidden_keywords="auth logout disconnect revoke credentials",
    ),
    CommandDescriptor(
        name="/loop",
        handler_type=LoopHandler,
        description="Schedule periodic recurring commands or checks",
        bypass_tier=BypassTier.IMMEDIATE,
        ui_mode="message",
        argument_hint="<interval> <command>",
        hidden_keywords="loop repeat interval schedule cron periodic recurring",
    ),
    CommandDescriptor(
        name="/manual",
        handler_type=ManualApprovalHandler,
        description="Switch to Manual approval mode",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        ui_mode="inline",
        hidden_keywords="approval mode approve prompt review shift+tab",
    ),
    CommandDescriptor(
        name="/mcp",
        handler_type=McpHandler,
        description="Manage MCP servers and connections",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        ui_mode="modal",
        hidden_keywords="servers mcp tools reconnect",
        argument_hint="[login <server> | reconnect [--force] | status]",
    ),
    CommandDescriptor(
        name="/memory",
        handler_type=MemoryHandler,
        description="Manage persisted conversation learnings and preferences",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        hidden_keywords="memory remember preferences learnings store save",
        argument_hint="[show|save <key> <content>|get <key>|delete <key>|clear]",
    ),
    CommandDescriptor(
        name="/model",
        handler_type=ModelHandler,
        description="Switch models or inspect model settings",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        ui_mode="modal",
        hidden_keywords="llm model provider switch",
        argument_hint="[provider:model] [--default [--clear]]",
    ),
    CommandDescriptor(
        name="/notifications",
        handler_type=NotificationsHandler,
        description="Configure notifications and warnings",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        ui_mode="modal",
        hidden_keywords="alerts warnings toasts",
    ),
    CommandDescriptor(
        name="/permissions",
        handler_type=PermissionsHandler,
        description="View and manage agent permission scopes",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        ui_mode="modal",
        hidden_keywords="permissions access control allowed denied trust safety sandbox",
        argument_hint="[show|grant <scope>|revoke <scope>|reset]",
    ),
    CommandDescriptor(
        name="/plugins",
        handler_type=PluginsHandler,
        description="Browse and manage installed plugins",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="modal",
        hidden_keywords="plugins extensions marketplace addons install manage",
    ),
    CommandDescriptor(
        name="/pool",
        handler_type=PoolHandler,
        description="Configure agent execution model pool (fast, standard, powerful tiers)",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        ui_mode="modal",
        hidden_keywords="pool agent model smart tiers router fast standard powerful",
        argument_hint="[status | clear]",
        aliases=("/agent-pool", "/agent_pool"),
    ),
    CommandDescriptor(
        name="/remember",
        handler_type=RememberHandler,
        description="Save useful context to memory or skills",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        argument_hint="[<text>]",
        hidden_keywords="remember learn save preference context",
    ),
    # Keep after `/remember` so equal-score prefix ties resolve as
    # `re` → `/remember`, `rel` → `/reload`, `res` → `/restart`.
    CommandDescriptor(
        name="/reload",
        handler_type=ReloadHandler,
        description="Reload environment and config",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="inline",
        hidden_keywords="refresh plugin plugins marketplace reload config skills themes env",
    ),
    CommandDescriptor(
        name="/restart",
        handler_type=RestartHandler,
        description="Restart the agent server",
        bypass_tier=BypassTier.ALWAYS,
        ui_mode="inline",
        hidden_keywords="respawn server reconnect connect restart reset kill",
    ),
    CommandDescriptor(
        name="/review",
        handler_type=ReviewHandler,
        description="Run code review on uncommitted changes",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        hidden_keywords="review code diff git changes",
        aliases=("/code-review",),
    ),
    CommandDescriptor(
        name="/rubric",
        handler_type=RubricHandler,
        description="Set acceptance criteria for quality gating",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        hidden_keywords="rubric criteria quality gate grading acceptance",
        argument_hint="[set <criteria>|next <criteria>|show|clear|file <path>|model|max-iterations]",
        aliases=("/criteria",),
    ),
    CommandDescriptor(
        name="/scrollbar",
        handler_type=ScrollbarHandler,
        description="Toggle message list scrollbar visibility",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        ui_mode="inline",
        hidden_keywords="scroll scrollbar toggle",
    ),
    CommandDescriptor(
        name="/skill-creator",
        handler_type=SkillCreatorHandler,
        description="Create or refine agent skills",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        argument_hint="[task]",
        hidden_keywords="skill create scaffold make build new skill-creator",
        aliases=("/skill:skill-creator",),
    ),
    CommandDescriptor(
        name="/skill:",
        handler_type=SkillInvokeHandler,
        description="Invoke an agent skill directly by name",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        argument_hint="<name> [args]",
        hidden_keywords="skill invoke direct run",
    ),
    CommandDescriptor(
        name="/skills",
        handler_type=SkillsHandler,
        description="List all available tools, MCP tools, and skills",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="modal",
        hidden_keywords="skills tools capabilities list available installed functions",
        aliases=("/tools",),
    ),
    CommandDescriptor(
        name="/smart",
        handler_type=SmartApprovalHandler,
        description="Switch to Smart approval mode (TypeSafe AI Jev System One protection)",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        ui_mode="inline",
        hidden_keywords="approval mode smart jev system one fast auto-approve shift+tab",
    ),
    CommandDescriptor(
        name="/tasks",
        handler_type=TasksHandler,
        description="Session TODO list and background task manager",
        bypass_tier=BypassTier.IMMEDIATE,
        ui_mode="message",
        argument_hint="[add <desc>|done <id>|rm <id>|clear]",
        hidden_keywords="tasks todo background jobs session list",
    ),
    CommandDescriptor(
        name="/theme",
        handler_type=ThemeHandler,
        description="Change color theme (dark/light/custom)",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        ui_mode="modal",
        hidden_keywords="color theme dark light palette",
    ),
    CommandDescriptor(
        name="/threads",
        handler_type=ResumeHandler,
        description="Browse past thread history or resume a specific session",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        ui_mode="modal",
        hidden_keywords="continue history sessions resume back previous threads browse",
        argument_hint="[-r [ID]]",
        aliases=("/resume", "/continue"),
    ),
    CommandDescriptor(
        name="/timestamps",
        handler_type=TimestampsHandler,
        description="Toggle message timestamps",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        ui_mode="inline",
        hidden_keywords="time timestamps clock",
    ),
    CommandDescriptor(
        name="/trace",
        handler_type=TraceHandler,
        description="Open this thread in LangSmith",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        ui_mode="message",
        hidden_keywords="langsmith trace telemetry runs smith",
    ),
    CommandDescriptor(
        name="/update",
        handler_type=UpdateHandler,
        description="Check for and install OpsCloud software updates",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        argument_hint="[--deps] [--prerelease]",
        hidden_keywords="update upgrade version check latest",
    ),
    CommandDescriptor(
        name="/version",
        handler_type=VersionHandler,
        description="Show OpsCloud version and environment info",
        bypass_tier=BypassTier.ALWAYS,
        ui_mode="message",
        hidden_keywords="ver build info",
    ),
    CommandDescriptor(
        name="/workflow",
        handler_type=WorkflowHandler,
        description="Trigger a skill as an isolated, parameterised workflow",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        argument_hint="[<name> [params]|show|cancel|detach|attach]",
        hidden_keywords="workflow runbook playbook pipeline job procedure run skill isolated execute",
    ),
    CommandDescriptor(
        name="/workflow:",
        handler_type=WorkflowInvokeHandler,
        description="Start a workflow by name directly",
        bypass_tier=BypassTier.QUEUED,
        ui_mode="message",
        argument_hint="<name> [args]",
        hidden_keywords="workflow invoke run direct",
    ),
)


# ── Projections & Lookups ────────────────────────────────────────────────────

def autocomplete_entries(
    commands: Sequence[CommandDescriptor] = COMMAND_DESCRIPTORS,
) -> tuple[CommandEntry, ...]:
    """Project commands into a sequence of CommandEntry instances for autocomplete.

    Excludes prefix handlers ending in ':' (e.g. '/skill:', '/workflow:');
    specific entries for those are generated dynamically from discovered skills.
    """
    return tuple(
        CommandEntry(
            name=command.name,
            description=command.description,
            hidden_keywords=command.hidden_keywords,
            argument_hint=command.argument_hint,
        )
        for command in commands
        if not command.name.endswith(":")
    )


def command_lookup(
    commands: Sequence[CommandDescriptor] = COMMAND_DESCRIPTORS,
) -> dict[str, CommandDescriptor]:
    """Build a mapping from canonical names and aliases to their CommandDescriptor."""
    lookup: dict[str, CommandDescriptor] = {}
    for command in commands:
        lookup[command.name.lower().strip()] = command
        for alias in command.aliases:
            lookup[alias.lower().strip()] = command
    return lookup


def _build_bypass_set(tier: BypassTier) -> frozenset[str]:
    """Build a lookup frozenset of command names for a specific bypass tier."""
    names: set[str] = set()
    for cmd in COMMAND_DESCRIPTORS:
        if cmd.bypass_tier == tier:
            names.add(cmd.name)
            for alias in cmd.aliases:
                names.add(alias)
    return frozenset(names)


ALWAYS_IMMEDIATE: frozenset[str] = _build_bypass_set(BypassTier.ALWAYS)
IMMEDIATE_UI_CMDS: frozenset[str] = _build_bypass_set(BypassTier.IMMEDIATE_UI)


def get_command(name_or_alias: str) -> CommandDescriptor | None:
    """Find a CommandDescriptor by canonical name or alias."""
    lookup = command_lookup(COMMAND_DESCRIPTORS)
    target = name_or_alias.lower().strip()
    match = lookup.get(target)
    if match is not None:
        return match

    # Prefix match: /skill:<name> -> /skill:, /workflow:<name> -> /workflow:
    for name, desc in lookup.items():
        if name.endswith(":") and target.startswith(name):
            return desc
    return None


_STATIC_SKILL_ALIASES: frozenset[str] = frozenset({"remember", "skill-creator"})
"""Built-in skill names that have a dedicated top-level slash command."""


def _skill_command_entry(skill: dict[str, Any] | Any) -> CommandEntry:
    """Build a single autocomplete entry for a discovered skill."""
    if isinstance(skill, dict):
        name = str(skill.get("name", ""))
        desc = str(skill.get("description", ""))
        source = str(skill.get("source", ""))
    else:
        name = getattr(skill, "name", "")
        desc = getattr(skill, "description", "")
        source = getattr(skill, "source", "")

    machine_name = f"/skill:{name}"
    is_plugin = source == "plugin"

    if is_plugin and ":" in name:
        terminal = name.rsplit(":", 1)[-1]
        display_name = f"/skill:{terminal}"
        plugin_id = name.split(":", 1)[0]
        description = f"({plugin_id}) {desc}"
    else:
        display_name = ""
        description = desc or name

    return CommandEntry(
        name=machine_name,
        description=description,
        hidden_keywords=name.replace(":", " "),
        argument_hint="",
        display_name=display_name,
    )


def build_skill_commands(skills: list[Any]) -> list[CommandEntry]:
    """Build autocomplete entries for discovered skills."""
    entries: list[CommandEntry] = []
    for skill in skills:
        if isinstance(skill, dict):
            name = str(skill.get("name", ""))
        else:
            name = getattr(skill, "name", "")
        if name and name.lower() not in _STATIC_SKILL_ALIASES:
            entries.append(_skill_command_entry(skill))
    return entries


def build_workflow_commands(skills: list[Any]) -> list[CommandEntry]:
    """Build ``/workflow:<name>`` autocomplete entries for workflow-capable skills."""
    from opscloud.workflows.discovery import workflow_from_skill

    entries: list[CommandEntry] = []
    for skill in skills:
        raw = skill if isinstance(skill, dict) else {
            "name": getattr(skill, "name", ""),
            "description": getattr(skill, "description", ""),
            "path": getattr(skill, "path", ""),
            "source": getattr(skill, "source", ""),
            "frontmatter": getattr(skill, "frontmatter", {}),
        }
        spec = workflow_from_skill(raw)
        if spec is None:
            continue
        desc = spec.description.splitlines()[0] if spec.description else spec.name
        is_namespaced = ":" in spec.name
        entries.append(
            CommandEntry(
                name=f"/workflow:{spec.name}",
                description=f"(workflow · {spec.plugin_id}) {desc}" if spec.plugin_id else f"(workflow) {desc}",
                hidden_keywords=f"workflow {spec.name.replace(':', ' ')}",
                argument_hint="[params]",
                display_name=f"/workflow:{spec.short_name}" if is_namespaced else "",
            )
        )
    return entries


def build_agent_commands(agents: list[Any]) -> list[CommandEntry]:
    """Build ``/agent:<name>`` autocomplete entries for plugin-provided subagents."""
    entries: list[CommandEntry] = []
    for agent in agents:
        raw = agent if isinstance(agent, dict) else getattr(agent, "__dict__", {})
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        desc = str(raw.get("description") or "").splitlines()[0] if raw.get("description") else name
        if len(desc) > 100:
            desc = desc[:99] + "…"
        plugin_id = str(raw.get("plugin_id") or "")
        short_name = name.rsplit(":", 1)[-1]
        if "@" in short_name:
            short_name = short_name.split("@", 1)[0]
        is_namespaced = ":" in name or "@" in name
        entries.append(
            CommandEntry(
                name=f"/agent:{name}",
                description=f"(agent · {plugin_id}) {desc}" if plugin_id else f"(agent) {desc}",
                hidden_keywords=f"agent {name.replace(':', ' ').replace('@', ' ')} {short_name}",
                argument_hint="[prompt]",
                display_name=f"/agent:{short_name}",
            )
        )
    return entries


def get_all_entries(
    extra_commands: Sequence[CommandDescriptor | CommandEntry] | None = None,
) -> list[CommandEntry]:
    """Return CommandEntry list for all registered commands and optional skills, including aliases."""
    entries: list[CommandEntry] = []
    for cmd in COMMAND_DESCRIPTORS:
        if cmd.name.endswith(":"):
            continue
        entries.append(cmd.to_entry())
        for alias in cmd.aliases:
            entries.append(
                CommandEntry(
                    name=alias,
                    description=f"Alias for {cmd.name} — {cmd.description}",
                    hidden_keywords=cmd.hidden_keywords,
                    argument_hint=cmd.argument_hint,
                )
            )

    if extra_commands:
        for item in extra_commands:
            if isinstance(item, CommandEntry):
                entries.append(item)
            elif hasattr(item, "to_entry"):
                entries.append(item.to_entry())
                for alias in getattr(item, "aliases", ()):
                    entries.append(
                        CommandEntry(
                            name=alias,
                            description=f"Alias for {item.name} — {item.description}",
                            hidden_keywords=getattr(item, "hidden_keywords", ""),
                            argument_hint=getattr(item, "argument_hint", ""),
                        )
                    )

    return entries


# ── Backward Compatibility Aliases ───────────────────────────────────────────

COMMANDS = COMMAND_DESCRIPTORS


def get_slash_commands() -> tuple[CommandDescriptor, ...]:
    """Return all defined CommandDescriptor instances."""
    return COMMAND_DESCRIPTORS


__all__ = [
    "ALWAYS_IMMEDIATE",
    "BypassTier",
    "COMMANDS",
    "COMMAND_DESCRIPTORS",
    "CommandDescriptor",
    "CommandEntry",
    "IMMEDIATE_UI_CMDS",
    "SlashCommand",
    "autocomplete_entries",
    "build_agent_commands",
    "build_skill_commands",
    "build_workflow_commands",
    "command_lookup",
    "get_all_entries",
    "get_command",
    "get_slash_commands",
]
