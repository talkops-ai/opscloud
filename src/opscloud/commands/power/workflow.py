"""``/workflow`` — trigger a runbook / playbook skill as an isolated workflow.

A workflow is a series of steps (possibly conditional) defined by a skill's
SKILL.md — think of a parameterised CI job or an SRE runbook.

  1. ``/workflow:<name> [args]`` (or ``/workflow <name> [args]``) submits a
     ``start`` request with the operator's parameters.
  2. The isolated workflow agent resolves the parameters (asking once for any
     required ones that are missing), then executes the steps. Tool calls go
     through the normal HITL approval policy.
  3. The agent reports ``complete`` / ``blocked`` / ``failed``. While the run is
     open and attached, plain chat input is routed to the workflow agent
     instead of the main agent.

The workflow transcript never enters the main conversation.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_OPEN_STATUSES = frozenset({"running", "blocked"})


class WorkflowState:
    """TUI mirror of the thread's workflow channels (``_workflow_*``)."""

    def __init__(self) -> None:
        self.thread_id: str | None = None
        """Thread the mirrored run belongs to."""
        self.run_id: str | None = None
        self.name: str | None = None
        self.args: str | None = None
        self.status: str | None = None
        self.note: str | None = None
        self.artifacts: list[str] = []
        self.attached: bool = True
        """Whether plain chat input is routed to the open workflow."""
        self.announced: tuple[str | None, ...] = ()
        """``(run_id, status, note)`` last reported to the operator (dedupe)."""

    def clear(self) -> None:
        """Forget the mirrored run (the checkpoint stays authoritative)."""
        self.__init__()

    @property
    def short_name(self) -> str:
        return (self.name or "").rsplit(":", 1)[-1]

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN_STATUSES

    @property
    def accepts_replies(self) -> bool:
        """Whether plain chat input should continue the workflow."""
        return self.attached and self.is_open


def get_workflow_state(app: object) -> WorkflowState:
    """Get or create the ``WorkflowState`` attached to an app instance."""
    existing = getattr(app, "_workflow_state", None)
    if not isinstance(existing, WorkflowState):
        app._workflow_state = WorkflowState()  # type: ignore[attr-defined]
    return app._workflow_state  # type: ignore[attr-defined]


def sync_workflow_status_label(app: object, state: WorkflowState) -> None:
    """Reflect the workflow in the status bar (shares the rubric label slot)."""
    status_bar = getattr(app, "_status_bar", None)
    set_label = getattr(status_bar, "set_rubric_label", None) if status_bar is not None else None
    if set_label is None:
        return
    labels = {
        "running": "▶ Workflow: {name}",
        "blocked": "⚠ Workflow: {name} · needs input",
    }
    template = labels.get(state.status or "")
    try:
        if template:
            suffix = "" if state.attached else " (detached)"
            set_label(template.format(name=state.short_name) + suffix)
        else:
            # Hand the slot back to /goal.
            from opscloud.commands.power.goal import GoalHandler, get_goal_state

            GoalHandler._sync_status_rubric(app, get_goal_state(app))
    except Exception:
        pass  # Status bar may not be mounted yet


async def refresh_workflow_state(app: object) -> WorkflowState:
    """Re-read ``_workflow_*`` channels from the checkpoint (best effort)."""
    sync: Any = getattr(app, "_sync_workflow_state_from_checkpoint", None)
    if callable(sync):
        try:
            await cast("Any", sync(announce=False))
        except Exception:
            logger.debug("Workflow state refresh failed", exc_info=True)
    return get_workflow_state(app)


def _discover(app: object) -> list[Any]:
    from opscloud.workflows.discovery import list_workflows

    skills: list[dict[str, Any]] | None = None
    getter: Any = getattr(app, "get_discovered_skills", None)
    if callable(getter):
        try:
            res = getter()
            if isinstance(res, list):
                skills = res
        except Exception:
            skills = None
    return list_workflows(skills)


def _list_text(app: object) -> str:
    workflows = _discover(app)
    if not workflows:
        return (
            "No workflows available. Any skill bound to the main agent becomes a "
            "workflow unless its SKILL.md sets `workflow: false`."
        )
    groups: dict[str, list[Any]] = {}
    for spec in workflows:
        groups.setdefault(spec.plugin_id or spec.source or "built-in", []).append(spec)
    lines = ["**Available workflows**", ""]
    for group in sorted(groups):
        lines.append(f"**{group}**")
        for spec in groups[group]:
            desc = spec.description.splitlines()[0] if spec.description else ""
            if len(desc) > 100:
                desc = desc[:99] + "…"
            lines.append(f"- `/workflow:{spec.name}` — {desc}" if desc else f"- `/workflow:{spec.name}`")
        lines.append("")
    lines.append(_usage_text())
    return "\n".join(lines)


def _usage_text() -> str:
    return (
        "**Usage:**\n\n"
        "```\n"
        "/workflow:<name> [args]\n"
        "/workflow <name> [args]\n"
        "/workflow show\n"
        "/workflow cancel\n"
        "/workflow detach   # send chat to the main agent\n"
        "/workflow attach   # send chat to the open workflow\n"
        "```\n\n"
        "Args are the run parameters (free text, `key=value`, or flags), e.g. "
        "`/workflow:restart-pods env=prod service=checkout`. The workflow agent asks "
        "once for any required parameter that is missing, then executes the runbook; "
        "tool calls follow the normal approval policy. The workflow never touches "
        "the main conversation."
    )


def _show_text(state: WorkflowState) -> str:
    if not state.name:
        return "No workflow in this thread.\n\n" + _usage_text()
    lines = [f"**Workflow:** `{state.name}`", f"**Status:** {state.status or 'unknown'}"]
    if state.args:
        lines.append(f"**Arguments:** `{state.args}`")
    if state.note:
        lines.append(f"**Note:** {state.note}")
    if state.artifacts:
        lines.append("**Artifacts:**\n\n" + "\n".join(f"- `{a}`" for a in state.artifacts))
    if state.is_open:
        lines.append(
            "Chat input is routed to this workflow."
            if state.attached
            else "Workflow is detached; chat goes to the main agent. Use `/workflow attach` to resume."
        )
    return "\n\n".join(lines)


async def start_workflow(ctx: CommandContext, name: str, args: str) -> CommandResult:
    """Resolve ``name`` and submit a ``start`` request with the operator's parameters."""
    from opscloud.workflows.discovery import resolve_workflow

    app = ctx.app
    spec = resolve_workflow(name, _discover(app))
    if spec is None:
        return CommandResult(
            success=False,
            message=f"Workflow `{name}` not found.\n\nRun `/workflow` to list available workflows.",
        )
    state = await refresh_workflow_state(app)
    if state.is_open:
        return CommandResult(
            success=False,
            message=(
                f"Workflow `{state.name}` is still {state.status}. "
                "Finish it or run `/workflow cancel` before starting another."
            ),
        )
    runner: Any = getattr(app, "_run_workflow_request", None)
    if not callable(runner):
        return CommandResult(success=False, message="Workflows require the interactive TUI.")
    request = {
        "kind": "start",
        "request_id": str(uuid.uuid4()),
        "workflow": spec.to_dict(),
        "args": args.strip(),
    }
    state.attached = True
    await cast("Any", runner(request))
    return CommandResult(success=True, message=None, mount_as_app_message=False)


class WorkflowHandler(BaseCommandHandler):
    """List, start, inspect, and manage isolated skill workflows."""

    @property
    def name(self) -> str:
        return "/workflow"

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
        state = await refresh_workflow_state(app)
        remainder = ctx.args.strip()
        sub, _, rest = remainder.partition(" ")
        sub = sub.lower()

        if not remainder or sub == "list":
            return CommandResult(success=True, message=_list_text(app))

        if sub in {"show", "status"}:
            return CommandResult(success=True, message=_show_text(state))

        if sub in {"cancel", "stop", "clear"}:
            if not state.is_open:
                return CommandResult(success=False, message="No open workflow to cancel.")
            runner: Any = getattr(app, "_run_workflow_request", None)
            if callable(runner):
                await cast("Any", runner({"kind": "cancel", "request_id": str(uuid.uuid4())}))
            return CommandResult(success=True, message=None, mount_as_app_message=False)

        if sub in {"detach", "exit"}:
            if not state.is_open:
                return CommandResult(success=False, message="No running workflow to detach from.")
            state.attached = False
            sync_workflow_status_label(app, state)
            return CommandResult(
                success=True,
                message=f"Detached from `{state.name}`. Chat now goes to the main agent; "
                "`/workflow attach` to resume.",
            )

        if sub == "attach":
            if not state.is_open:
                return CommandResult(success=False, message="No running workflow to attach to.")
            state.attached = True
            sync_workflow_status_label(app, state)
            return CommandResult(
                success=True,
                message=f"Attached to `{state.name}`. Chat input now continues the workflow.",
            )

        return await start_workflow(ctx, sub, rest.strip())


class WorkflowInvokeHandler(BaseCommandHandler):
    """``/workflow:<name> [args]`` — start a workflow by name (autocomplete target)."""

    @property
    def name(self) -> str:
        # Prefix handler — matched by the router's ``startswith`` check.
        return "/workflow:"

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
        head, _, args = ctx.raw_command.strip().partition(" ")
        name = head[len("/workflow:") :].strip() if head.lower().startswith("/workflow:") else ""
        if not name:
            return CommandResult(success=False, message="Usage: /workflow:<name> [args]")
        return await start_workflow(ctx, name, args.strip())


__all__ = [
    "WorkflowHandler",
    "WorkflowInvokeHandler",
    "WorkflowState",
    "get_workflow_state",
    "start_workflow",
    "sync_workflow_status_label",
]
