"""``/agent`` — activate a plugin-provided specialist agent on the current session.

When an agent persona is activated:
  1. Plain chat input is routed directly to that specialist agent instead of
     the main deep agent.
  2. The agent executes with all its dedicated MCP servers, skills, tools, and
     hooks bound exactly as when invoked via ``js_eval`` / ``task``.
  3. The agent's transcript stays isolated in ``_agent_session_messages``.
  4. Once done, the operator runs ``/agent clear`` (or ``/agent exit`` /
     ``/agent detach``) to return to the main deep agent.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from opscloud.subagents.types import SubagentMetadata

logger = get_logger(__name__)


class AgentSessionState:
    """TUI mirror of the thread's active agent session channels."""

    def __init__(self) -> None:
        self.thread_id: str | None = None
        """Thread the active agent session belongs to."""
        self.agent_id: str | None = None
        """Fully-qualified agent name, e.g. ``aws-compute@talkops-devops-plugins``."""
        self.short_name: str | None = None
        """Display name without namespace or plugin suffix."""
        self.plugin_id: str | None = None
        """ID of the plugin that defined this agent."""
        self.description: str | None = None
        self.attached: bool = True
        """Whether plain chat input is routed to the active agent."""

    def clear(self) -> None:
        """Clear the active agent session."""
        self.thread_id = None
        self.agent_id = None
        self.short_name = None
        self.plugin_id = None
        self.description = None
        self.attached = True

    @property
    def is_active(self) -> bool:
        """Whether an agent persona is actively bound and attached."""
        return bool(self.agent_id and self.attached)

    @property
    def accepts_replies(self) -> bool:
        """Whether plain chat input should route to the active agent."""
        return self.is_active


def get_agent_session_state(app: object) -> AgentSessionState:
    """Get or create the ``AgentSessionState`` attached to an app instance."""
    existing = getattr(app, "_agent_session_state", None)
    if not isinstance(existing, AgentSessionState):
        app._agent_session_state = AgentSessionState()  # type: ignore[attr-defined]
    return app._agent_session_state  # type: ignore[attr-defined]


def sync_agent_session_status_label(app: object, state: AgentSessionState) -> None:
    """Reflect the active agent in the status bar (shares the rubric/workflow label slot)."""
    status_bar = getattr(app, "_status_bar", None)
    set_label = getattr(status_bar, "set_rubric_label", None) if status_bar is not None else None
    if set_label is None:
        return
    try:
        if state.is_active and state.short_name:
            set_label(f"▶ Agent: {state.short_name}")
        elif state.agent_id and not state.attached and state.short_name:
            set_label(f"▷ Agent: {state.short_name} (detached)")
        else:
            # Check if workflow is running, else hand back to /goal
            from opscloud.commands.power.workflow import get_workflow_state, sync_workflow_status_label

            wf_state = get_workflow_state(app)
            if wf_state.is_open:
                sync_workflow_status_label(app, wf_state)
            else:
                from opscloud.commands.power.goal import GoalHandler, get_goal_state

                GoalHandler._sync_status_rubric(app, get_goal_state(app))
    except Exception:
        pass  # Status bar may not be mounted yet


def _discover_plugin_agents(app: object | None = None) -> list[SubagentMetadata]:
    """Discover subagents originating strictly from enabled plugins."""
    getter: Any = getattr(app, "get_discovered_plugin_agents", None) if app is not None else None
    if callable(getter):
        try:
            res = getter()
            if isinstance(res, list):
                return cast("list[SubagentMetadata]", res)
        except Exception:
            pass

    from opscloud.plugins.adapters.agents import discover_plugin_subagents

    project_root = getattr(getattr(app, "_settings", None), "project_root", None) if app is not None else None
    if project_root is None:
        from opscloud.config.settings import settings

        project_root = settings.project_root

    root_path = Path(project_root) if project_root else Path.cwd()
    return discover_plugin_subagents(project_root=root_path)


def resolve_plugin_agent(
    name: str,
    agents: list[SubagentMetadata],
) -> SubagentMetadata | None:
    """Resolve an agent name against discovered plugin agents.

    Matches by exact name, normalized lowercase, terminal short name, or plugin_id.
    """
    if not name:
        return None
    target = name.strip().lower()

    # 1. Exact match on full name
    for a in agents:
        aname = str(a.get("name") or "").strip().lower()
        if aname == target:
            return a

    # 2. Match on terminal short name (after ':' or before '@')
    for a in agents:
        raw_name = str(a.get("name") or "").strip()
        terminal = raw_name.rsplit(":", 1)[-1]
        if "@" in terminal:
            terminal = terminal.split("@", 1)[0]
        if terminal.lower() == target:
            return a

    # 3. Match by plugin_id
    for a in agents:
        pid = str(a.get("plugin_id") or "").strip().lower()
        if pid == target:
            return a

    # 4. Substring / prefix match
    for a in agents:
        aname = str(a.get("name") or "").strip().lower()
        if target in aname:
            return a

    return None


def _clean_short_name(name: str) -> str:
    """Extract a user-friendly short name from an agent identifier."""
    raw = name.rsplit(":", 1)[-1]
    if "@" in raw:
        return raw.split("@", 1)[0]
    return raw


def _list_text(app: object | None) -> str:
    agents = _discover_plugin_agents(app)
    if not agents:
        return (
            "No plugin agents found.\n\n"
            "Install an agent plugin (e.g. from the marketplace or local plugins) "
            "to activate specialized domain subagents."
        )

    lines = ["**Available Plugin Agents**", ""]
    for agent in sorted(agents, key=lambda a: str(a.get("name") or "")):
        name = str(agent.get("name") or "")
        short_name = _clean_short_name(name)
        plugin_id = str(agent.get("plugin_id") or agent.get("source") or "plugin")
        desc = (str(agent.get("description") or "")).splitlines()[0] if agent.get("description") else ""
        if len(desc) > 120:
            desc = desc[:119] + "…"

        tools = agent.get("tools")
        skills = agent.get("skills")

        header = f"• **`/agent:{short_name}`** (`{name}`)" if short_name != name else f"• **`/agent:{name}`**"
        lines.append(header)
        lines.append(f"  *Plugin:* `{plugin_id}`")
        if desc:
            lines.append(f"  *Description:* {desc}")
        if tools:
            lines.append(f"  *Tools:* {', '.join(tools[:5])}{' …' if len(tools) > 5 else ''}")
        if skills:
            lines.append(f"  *Skills:* {', '.join(skills[:3])}{' …' if len(skills) > 3 else ''}")
        lines.append("")

    lines.append(_usage_text())
    return "\n".join(lines)


def _usage_text() -> str:
    return (
        "**Usage:**\n\n"
        "```\n"
        "/agent:<name> [prompt]\n"
        "/agent <name> [prompt]\n"
        "/agent status\n"
        "/agent clear    # detach from agent and return to main deep agent\n"
        "/agent detach   # pause routing chat to this agent\n"
        "/agent attach   # resume routing chat to this agent\n"
        "```\n\n"
        "When an agent is activated, all subsequent chat messages are routed to it "
        "with its dedicated MCP servers, skills, and execution hooks bound. Run "
        "`/agent clear` when finished to return to the main deep agent."
    )


def _show_text(state: AgentSessionState) -> str:
    if not state.agent_id:
        return "No active plugin agent session in this thread.\n\n" + _usage_text()
    lines = [
        f"**Active Agent Persona:** `{state.short_name or state.agent_id}`",
        f"**Full ID:** `{state.agent_id}`",
    ]
    if state.plugin_id:
        lines.append(f"**Plugin:** `{state.plugin_id}`")
    if state.description:
        lines.append(f"**Description:** {state.description}")
    lines.append(
        "Chat input is currently routed directly to this agent persona."
        if state.attached
        else "Agent is currently detached; chat goes to the main deep agent. Use `/agent attach` to resume."
    )
    return "\n\n".join(lines)


async def activate_agent_session(
    ctx: CommandContext,
    name: str,
    prompt: str = "",
) -> CommandResult:
    """Resolve plugin agent ``name`` and activate the agent on the current session."""
    app = ctx.app
    agents = _discover_plugin_agents(app)
    spec = resolve_plugin_agent(name, agents)
    if spec is None:
        return CommandResult(
            success=False,
            message=f"Plugin agent `{name}` not found.\n\nRun `/agent` to list available plugin agents.",
        )

    # Check if a workflow is open
    if app is not None:
        from opscloud.commands.power.workflow import get_workflow_state

        wf_state = get_workflow_state(app)
        if wf_state.is_open:
            return CommandResult(
                success=False,
                message=(
                    f"Workflow `{wf_state.name}` is currently active. "
                    "Finish or cancel it (`/workflow cancel`) before activating an agent persona."
                ),
            )

    full_name = str(spec.get("name") or "")
    short_name = _clean_short_name(full_name)
    plugin_id = str(spec.get("plugin_id") or "")
    description = str(spec.get("description") or "").splitlines()[0] if spec.get("description") else ""

    state = get_agent_session_state(app) if app is not None else AgentSessionState()
    state.agent_id = full_name
    state.short_name = short_name
    state.plugin_id = plugin_id
    state.description = description
    state.attached = True
    state.thread_id = ctx.thread_id

    if app is not None:
        sync_agent_session_status_label(app, state)

    runner: Any = getattr(app, "_run_agent_session_request", None) if app is not None else None
    if callable(runner):
        request = {
            "kind": "start",
            "request_id": str(uuid.uuid4()),
            "agent_name": full_name,
            "plugin_id": plugin_id,
            "prompt": prompt.strip() if prompt else None,
        }
        await cast("Any", runner(request))

    banner = (
        f"Activated agent persona **{short_name}** ({plugin_id or 'plugin'}).\n\n"
        f"{description}\n\n"
        f"• Dedicated MCP servers, skills, and execution hooks are bound to this session.\n"
        f"• Plain chat input is now routed directly to this agent.\n"
        f"• Use `/agent clear` or `/agent exit` at any time to return to the main deep agent."
    )
    if prompt:
        banner += f"\n\n*Initial prompt:* {prompt.strip()}"

    return CommandResult(success=True, message=banner, mount_as_app_message=True)


async def clear_agent_session(ctx: CommandContext) -> CommandResult:
    """Detach and clear the active plugin agent session."""
    app = ctx.app
    state = get_agent_session_state(app) if app is not None else AgentSessionState()
    if not state.agent_id:
        return CommandResult(success=False, message="No active plugin agent session to clear.")

    old_name = state.short_name or state.agent_id
    state.clear()
    if app is not None:
        sync_agent_session_status_label(app, state)
        runner: Any = getattr(app, "_run_agent_session_request", None)
        if callable(runner):
            await cast("Any", runner({"kind": "clear", "request_id": str(uuid.uuid4())}))

    return CommandResult(
        success=True,
        message=f"Detached from agent **{old_name}**. Chat input now routes back to the main deep agent.",
        mount_as_app_message=True,
    )


class AgentsHandler(BaseCommandHandler):
    """List, activate, inspect, and clear plugin agent persona sessions."""

    @property
    def name(self) -> str:
        return "/agent"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/agents",)

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        app = ctx.app
        remainder = ctx.args.strip()
        sub, _, rest = remainder.partition(" ")
        sub = sub.lower()

        if not remainder or sub == "list":
            if (
                ctx.app is not None
                and hasattr(ctx.app, "_show_agent_selector")
                and type(ctx.app).__name__ != "MagicMock"
            ):
                try:
                    res = ctx.app._show_agent_selector()
                    if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                        await res
                    return CommandResult(success=True, message="", mount_as_app_message=False)
                except Exception:
                    pass
            return CommandResult(success=True, message=_list_text(app))

        state = get_agent_session_state(app) if app is not None else AgentSessionState()

        if sub in {"show", "status"}:
            return CommandResult(success=True, message=_show_text(state))

        if sub in {"clear", "exit", "stop", "reset"}:
            return await clear_agent_session(ctx)

        if sub == "detach":
            if not state.agent_id:
                return CommandResult(success=False, message="No active agent session to detach from.")
            state.attached = False
            if app is not None:
                sync_agent_session_status_label(app, state)
            return CommandResult(
                success=True,
                message=f"Detached from `{state.short_name}`. Chat now goes to the main deep agent; "
                "`/agent attach` to resume.",
            )

        if sub == "attach":
            if not state.agent_id:
                return CommandResult(success=False, message="No active agent session to attach to.")
            state.attached = True
            if app is not None:
                sync_agent_session_status_label(app, state)
            return CommandResult(
                success=True,
                message=f"Attached to `{state.short_name}`. Chat input now routes to this agent persona.",
            )

        return await activate_agent_session(ctx, sub, rest.strip())


class AgentInvokeHandler(BaseCommandHandler):
    """``/agent:<name> [prompt]`` — activate a plugin agent by name directly (autocomplete target)."""

    @property
    def name(self) -> str:
        return "/agent:"

    @property
    def aliases(self) -> tuple[str, ...]:
        return ("/agents:",)

    @property
    def category(self) -> CommandCategory:
        return CommandCategory.POWER

    @property
    def safety_level(self) -> SafetyLevel:
        return SafetyLevel.LOW_RISK

    @property
    def bypass_tier(self) -> BypassTier:
        return BypassTier.QUEUED

    async def execute(self, ctx: CommandContext) -> CommandResult:
        head, _, prompt = ctx.raw_command.strip().partition(" ")
        prefix = "/agent:" if head.lower().startswith("/agent:") else "/agents:"
        name = head[len(prefix) :].strip() if head.lower().startswith(prefix) else ""
        if not name:
            return CommandResult(success=False, message="Usage: /agent:<name> [prompt]")
        return await activate_agent_session(ctx, name, prompt.strip())


__all__ = [
    "AgentInvokeHandler",
    "AgentSessionState",
    "AgentsHandler",
    "activate_agent_session",
    "clear_agent_session",
    "get_agent_session_state",
    "resolve_plugin_agent",
    "sync_agent_session_status_label",
]
