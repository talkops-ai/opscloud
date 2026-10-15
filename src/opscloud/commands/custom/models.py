"""Data models for zero-code custom commands and skills in OpsCloud."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

CustomCommandSource = Literal[
    "policySettings",
    "userSettings",
    "projectSettings",
    "plugin",
    "builtin",
    "mcp",
    "bundled",
]

ExecutionContextMode = Literal["inline", "fork"]
EffortValue = Literal["low", "medium", "high", "max"] | int
ShellType = Literal["bash", "powershell"]


@dataclass(slots=True)
class CustomCommandFrontmatter:
    """Parsed YAML frontmatter from a custom command `.md` or `SKILL.md` file."""

    name: str | None = None
    description: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    argument_hint: str | None = None
    arguments: list[str] = field(default_factory=list)
    model: str | None = None
    effort: EffortValue | None = None
    context: ExecutionContextMode = "inline"
    agent: str | None = None
    background: bool = False
    disable_model_invocation: bool = False
    user_invocable: bool = True
    when_to_use: str | None = None
    paths: list[str] = field(default_factory=list)
    shell: ShellType = "bash"
    hooks: dict[str, Any] = field(default_factory=dict)
    version: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CustomCommandDefinition:
    """A zero-code custom slash command / skill loaded from markdown."""

    name: str
    description: str
    body: str
    frontmatter: CustomCommandFrontmatter
    file_path: Path
    base_dir: Path | None = None
    source: CustomCommandSource = "userSettings"
    loaded_from: Literal["commands_DEPRECATED", "skills", "plugin", "managed", "bundled"] = (
        "commands_DEPRECATED"
    )
    plugin_name: str | None = None
    plugin_root: Path | None = None
    plugin_data_dir: Path | None = None
    aliases: list[str] = field(default_factory=list)
    has_user_specified_description: bool = False

    @property
    def display_name(self) -> str:
        """Human-readable name for the command."""
        return self.frontmatter.name or self.name

    @property
    def allowed_tools(self) -> list[str]:
        """List of tool names or permission rules (e.g., ``Bash(git *)``) pre-approved for this command."""
        return list(self.frontmatter.allowed_tools)

    @property
    def disallowed_tools(self) -> list[str]:
        """List of tool names or permission rules blocked during this command's turn."""
        return list(self.frontmatter.disallowed_tools)

    @property
    def argument_hint(self) -> str | None:
        """Optional hint displayed in slash-command autocomplete."""
        return self.frontmatter.argument_hint

    @property
    def argument_names(self) -> list[str]:
        """Named positional arguments declared in ``arguments`` frontmatter."""
        return list(self.frontmatter.arguments)

    @property
    def model(self) -> str | None:
        """Model override (e.g. ``sonnet``, ``opus``, ``haiku``, or full model ID); ``None`` when inheriting."""
        if self.frontmatter.model and self.frontmatter.model.lower() == "inherit":
            return None
        return self.frontmatter.model

    @property
    def effort(self) -> EffortValue | None:
        """Effort level override (``low``, ``medium``, ``high``, ``max``, or integer budget)."""
        return self.frontmatter.effort

    @property
    def execution_context(self) -> ExecutionContextMode:
        """Execution context mode: ``inline`` (default) or ``fork`` (subagent)."""
        return self.frontmatter.context

    @property
    def agent(self) -> str | None:
        """Subagent type to use when ``context == 'fork'`` (defaults to ``general-purpose``)."""
        return self.frontmatter.agent

    @property
    def background(self) -> bool:
        """Whether this command can run in the background."""
        return self.frontmatter.background

    @property
    def disable_model_invocation(self) -> bool:
        """If True, the LLM cannot invoke this command via the ``Skill`` tool."""
        return self.frontmatter.disable_model_invocation

    @property
    def user_invocable(self) -> bool:
        """If True, the user can invoke this command directly via ``/name`` in the UI."""
        return self.frontmatter.user_invocable

    @property
    def when_to_use(self) -> str | None:
        """Guidance appended to description in the ``Skill`` tool prompt."""
        return self.frontmatter.when_to_use

    @property
    def paths(self) -> list[str]:
        """Glob patterns restricting when this command is dynamically activated."""
        return list(self.frontmatter.paths)

    @property
    def shell(self) -> ShellType:
        """Shell interpreter for inline ``!`` blocks (``bash`` or ``powershell``)."""
        return self.frontmatter.shell

    @property
    def source_label(self) -> str:
        """Human-readable source badge (e.g., ``(project)``, ``(user)``, ``(plugin:name)``)."""
        if self.source == "policySettings":
            return "(policy)"
        if self.source == "projectSettings":
            return "(project)"
        if self.source == "userSettings":
            return "(user)"
        if self.source == "plugin":
            return f"(plugin:{self.plugin_name})" if self.plugin_name else "(plugin)"
        return f"({self.source})"

    def is_hidden_from_slash_autocomplete(self) -> bool:
        """Return whether this command should be hidden from slash autocomplete."""
        return not self.user_invocable


@dataclass(slots=True)
class CommandPermissionContext:
    """Active tool permission and model/effort context bound by an invoked custom command."""

    command_name: str
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    model: str | None = None
    effort: EffortValue | None = None
    execution_context: ExecutionContextMode = "inline"
    agent: str | None = None
    base_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary for LangGraph state storage."""
        return {
            "command_name": self.command_name,
            "allowed_tools": list(self.allowed_tools),
            "disallowed_tools": list(self.disallowed_tools),
            "model": self.model,
            "effort": self.effort,
            "execution_context": self.execution_context,
            "agent": self.agent,
            "base_dir": self.base_dir,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandPermissionContext:
        """Restore from a dictionary stored in LangGraph state."""
        return cls(
            command_name=str(data.get("command_name", "")),
            allowed_tools=[str(x) for x in data.get("allowed_tools", []) if x],
            disallowed_tools=[str(x) for x in data.get("disallowed_tools", []) if x],
            model=data.get("model"),
            effort=data.get("effort"),
            execution_context="fork" if data.get("execution_context") == "fork" else "inline",
            agent=data.get("agent"),
            base_dir=data.get("base_dir"),
        )


@dataclass(slots=True)
class ExpandedCommandPrompt:
    """Result of expanding a custom command (or stacked commands) with user arguments."""

    command_name: str
    display_text: str
    xml_command_message: str
    expanded_prompt: str
    base_dir: Path | None = None
    referenced_files: list[str] = field(default_factory=list)
    permissions: CommandPermissionContext = field(
        default_factory=lambda: CommandPermissionContext(command_name="")
    )
    execution_context: ExecutionContextMode = "inline"
    agent: str | None = None
    background: bool = False
    stacked_commands: list[str] = field(default_factory=list)
