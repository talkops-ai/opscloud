"""Unified slash-command registry for OpsCloud.

Every slash command is declared once as a `SlashCommand` entry in `COMMANDS`.
Bypass-tier frozensets and autocomplete entries are derived automatically.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NamedTuple



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


class CommandEntry(NamedTuple):
    """Projection carrying only fields needed for autocomplete."""

    name: str
    description: str
    hidden_keywords: str = ""
    argument_hint: str = ""
    display_name: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class SlashCommand:
    """A single slash-command definition."""

    name: str
    description: str
    bypass_tier: BypassTier
    hidden_keywords: str = ""
    argument_hint: str = ""
    aliases: tuple[str, ...] = ()

    def to_entry(self) -> CommandEntry:
        """Project this command into a CommandEntry for autocomplete."""
        return CommandEntry(
            name=self.name,
            description=self.description,
            hidden_keywords=self.hidden_keywords,
            argument_hint=self.argument_hint,
        )


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        name="/agents",
        description="Browse and switch between available agents & subagents",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        hidden_keywords="switch profile persona subagents",
    ),
    SlashCommand(
        name="/auto",
        description="Switch to Auto approval mode",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        hidden_keywords="approval mode classifier automatic auto-approve shift+tab",
    ),
    SlashCommand(
        name="/manual",
        description="Switch to Manual approval mode",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        hidden_keywords="approval mode approve prompt review shift+tab",
    ),
    SlashCommand(
        name="/smart",
        description="Switch to Smart approval mode (TypeSafe AI Jev System One protection)",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        hidden_keywords="approval mode smart jev system one fast auto-approve shift+tab",
    ),
    SlashCommand(
        name="/bug",
        description="Report a bug or provide feedback",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="bug feedback report issue problem error crash",
        aliases=("/feedback",),
    ),
    SlashCommand(
        name="/clear",
        description="Clear the chat and start a new thread",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="reset new clean",
    ),
    SlashCommand(
        name="/compact",
        description="Summarize and offload older conversation history to free context window space",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="compact offload summarize context free",
        aliases=("/offload",),
    ),
    SlashCommand(
        name="/context",
        description="Display unified context window token usage and loaded resource counts",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="context window tokens usage capacity resources attached",
    ),
    SlashCommand(
        name="/copy",
        description="Copy the latest assistant message to clipboard",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        hidden_keywords="clipboard copy",
    ),
    SlashCommand(
        name="/effort",
        description="Set reasoning effort level for current model",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="reasoning thinking level budget effort depth",
        argument_hint="[low|medium|high|max|clear]",
    ),
    SlashCommand(
        name="/exit",
        description="Exit the application",
        bypass_tier=BypassTier.ALWAYS,
        hidden_keywords="exit quit stop bye q",
        aliases=("/quit", "/q"),
    ),
    SlashCommand(
        name="/fast",
        description="Toggle fast mode (cheapest model with low reasoning effort)",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="fast quick speed cheap haiku flash lite",
    ),
    SlashCommand(
        name="/force-clear",
        description="Stop active work, clear the chat, and start a new thread",
        bypass_tier=BypassTier.ALWAYS,
        hidden_keywords="reset interrupt stop kill",
    ),
    SlashCommand(
        name="/goal",
        description="Set a persistent objective with acceptance criteria",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="objective criteria rubric target",
        argument_hint="[<objective>|amend <feedback>|pause|resume|show|clear|model|max-iterations]",
    ),
    SlashCommand(
        name="/help",
        description="Show help and available commands",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        hidden_keywords="info doc commands usage",
    ),
    SlashCommand(
        name="/mcp",
        description="Manage MCP servers and connections",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        hidden_keywords="servers mcp tools reconnect",
        argument_hint="[login <server> | reconnect [--force] | status]",
    ),
    SlashCommand(
        name="/cloud",
        description="Select active cloud profile (AWS) or manage cloud configuration",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        hidden_keywords="aws cloud profile account credentials iac provider",
        argument_hint="[profile-name | status | list]",
        aliases=("/aws",),
    ),
    SlashCommand(
        name="/model",
        description="Switch models or inspect model settings",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        hidden_keywords="llm model provider switch",
        argument_hint="[provider:model] [--default [--clear]]",
    ),
    SlashCommand(
        name="/pool",
        description="Configure agent execution model pool (fast, standard, powerful tiers)",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        hidden_keywords="pool agent model smart tiers router fast standard powerful",
        argument_hint="[status | clear]",
        aliases=("/agent-pool",),
    ),
    SlashCommand(
        name="/notifications",
        description="Configure notifications and warnings",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        hidden_keywords="alerts warnings toasts",
    ),
    SlashCommand(
        name="/threads",
        description="Browse past thread history or resume a specific session",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        hidden_keywords="continue history sessions resume back previous threads browse",
        argument_hint="[-r [ID]]",
    ),
    SlashCommand(
        name="/scrollbar",
        description="Toggle message list scrollbar visibility",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        hidden_keywords="scroll scrollbar toggle",
    ),
    SlashCommand(
        name="/theme",
        description="Change color theme (dark/light/custom)",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        hidden_keywords="color theme dark light palette",
    ),
    SlashCommand(
        name="/timestamps",
        description="Toggle message timestamps",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        hidden_keywords="time timestamps clock",
    ),
    SlashCommand(
        name="/trace",
        description="Open this thread in LangSmith",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        hidden_keywords="langsmith trace telemetry runs smith",
    ),
    SlashCommand(
        name="/version",
        description="Show OpsCloud version and environment info",
        bypass_tier=BypassTier.ALWAYS,
        hidden_keywords="ver build info",
    ),
    SlashCommand(
        name="/config",
        description="View and manage OpsCloud configuration",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        hidden_keywords="config settings preferences environment env vars",
        argument_hint="[show|set <key> <value>|reset <key>|path]",
    ),
    SlashCommand(
        name="/doctor",
        description="Run diagnostic health checks on OpsCloud environment",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="doctor diagnose health check debug troubleshoot deps dependencies",
    ),
    SlashCommand(
        name="/login",
        description="Open auth manager to manage API keys and credentials",
        bypass_tier=BypassTier.IMMEDIATE_UI,
        hidden_keywords="auth login connect api key credentials provider",
        aliases=("/auth", "/connect"),
    ),
    SlashCommand(
        name="/logout",
        description="Revoke stored API credentials for current session",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="auth logout disconnect revoke credentials",
    ),
    SlashCommand(
        name="/permissions",
        description="View and manage agent permission scopes",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="permissions access control allowed denied trust safety sandbox",
        argument_hint="[show|grant <scope>|revoke <scope>|reset]",
    ),
    SlashCommand(
        name="/plugins",
        description="Browse and manage installed plugins",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="plugins extensions marketplace addons install manage",
    ),
    SlashCommand(
        name="/skills",
        description="List all available tools, MCP tools, and skills",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="skills tools capabilities list available installed functions",
        aliases=("/tools",),
    ),
    # ── Power User Commands (Phase 4) ──────────────────
    SlashCommand(
        name="/rubric",
        description="Set acceptance criteria for quality gating",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="rubric criteria quality gate grading acceptance",
        argument_hint="[set <criteria>|next <criteria>|show|clear|file <path>|model|max-iterations]",
        aliases=("/criteria",),
    ),
    SlashCommand(
        name="/memory",
        description="Manage persisted conversation learnings and preferences",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="memory remember preferences learnings store save",
        argument_hint="[show|save <key> <content>|get <key>|delete <key>|clear]",
    ),
    SlashCommand(
        name="/remember",
        description="Save useful context to memory or skills",
        bypass_tier=BypassTier.QUEUED,
        argument_hint="[<text>]",
        hidden_keywords="remember learn save preference context",
    ),
    # Keep after `/remember` so equal-score prefix ties resolve as
    # `re` → `/remember`, `rel` → `/reload`, `res` → `/restart`.
    SlashCommand(
        name="/reload",
        description="Reload environment and config",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="refresh plugin plugins marketplace reload config skills themes env",
    ),
    SlashCommand(
        name="/restart",
        description="Restart the agent server",
        bypass_tier=BypassTier.ALWAYS,
        hidden_keywords="respawn server reconnect connect restart reset kill",
    ),
    SlashCommand(
        name="/skill-creator",
        description="Create or refine agent skills",
        bypass_tier=BypassTier.QUEUED,
        argument_hint="[task]",
        hidden_keywords="skill create scaffold make build new skill-creator",
        aliases=("/skill:skill-creator",),
    ),
    SlashCommand(
        name="/review",
        description="Run code review on uncommitted changes",
        bypass_tier=BypassTier.QUEUED,
        hidden_keywords="review code diff git changes",
        aliases=("/code-review",),
    ),
    SlashCommand(
        name="/install",
        description="Install an optional extra package or provider dependency",
        bypass_tier=BypassTier.QUEUED,
        argument_hint="<extra|package> [--package] [--force]",
        hidden_keywords="install pip uv package add",
    ),
    SlashCommand(
        name="/update",
        description="Check for and install OpsCloud software updates",
        bypass_tier=BypassTier.QUEUED,
        argument_hint="[--deps] [--prerelease]",
        hidden_keywords="update upgrade version check latest",
    ),
    SlashCommand(
        name="/auto-update",
        description="Toggle automatic update checks on startup",
        bypass_tier=BypassTier.SIDE_EFFECT_FREE,
        argument_hint="[on|off|status]",
        hidden_keywords="auto-update autoupdate check startup",
    ),
)


def _build_bypass_set(tier: BypassTier) -> frozenset[str]:
    """Build a lookup frozenset of command names for a specific bypass tier."""
    names: set[str] = set()
    for cmd in COMMANDS:
        if cmd.bypass_tier == tier:
            names.add(cmd.name)
            for alias in cmd.aliases:
                names.add(alias)
    return frozenset(names)


ALWAYS_IMMEDIATE: frozenset[str] = _build_bypass_set(BypassTier.ALWAYS)
IMMEDIATE_UI_CMDS: frozenset[str] = _build_bypass_set(BypassTier.IMMEDIATE_UI)


def get_command(name_or_alias: str) -> SlashCommand | None:
    """Find a SlashCommand by canonical name or alias."""
    target = name_or_alias.lower().strip()
    for cmd in COMMANDS:
        if cmd.name == target or target in cmd.aliases:
            return cmd
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
    """Build autocomplete entries for discovered skills.

    Each skill becomes a `/skill:<name>` entry with its description
    and the skill name as a hidden keyword for fuzzy matching. Plugin skills
    keep their namespaced machine name for matching/insertion but present a
    short, source-tagged label in the popup.

    Skills that already have a dedicated slash command in `COMMANDS`
    (e.g., `remember` → `/remember`, `skill-creator` → `/skill-creator`)
    are excluded to avoid duplicate autocomplete entries.
    """
    entries: list[CommandEntry] = []
    for skill in skills:
        if isinstance(skill, dict):
            name = str(skill.get("name", ""))
        else:
            name = getattr(skill, "name", "")
        if name and name.lower() not in _STATIC_SKILL_ALIASES:
            entries.append(_skill_command_entry(skill))
    return entries


def get_all_entries(
    extra_commands: Sequence[SlashCommand | CommandEntry] | None = None,
) -> list[CommandEntry]:
    """Return CommandEntry list for all registered commands and optional skills, including aliases."""
    entries: list[CommandEntry] = []
    for cmd in COMMANDS:
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
            elif isinstance(item, SlashCommand):
                entries.append(item.to_entry())
                for alias in item.aliases:
                    entries.append(
                        CommandEntry(
                            name=alias,
                            description=f"Alias for {item.name} — {item.description}",
                            hidden_keywords=item.hidden_keywords,
                            argument_hint=item.argument_hint,
                        )
                    )

    return entries


def get_slash_commands() -> tuple[SlashCommand, ...]:
    """Return all defined SlashCommand instances."""
    return COMMANDS

