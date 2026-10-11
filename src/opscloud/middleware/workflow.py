"""``WorkflowMiddleware`` — run a skill-defined procedure as an isolated workflow.

The TUI submits one typed ``workflow_request`` with ``messages: []``. This
middleware handles it in ``before_agent`` and jumps to ``"end"`` so the main
deep agent never runs and its transcript is never touched:

* ``start``    → seeds a fresh isolated transcript and runs the workflow agent
                 with the operator's parameters
* ``continue`` → appends an operator reply and re-runs the workflow agent
* ``cancel``   → clears the run

There is no planning step and no rubric grading: the workflow agent executes
the workflow as written under the main agent's HITL approval policy and reports
``complete`` / ``blocked`` / ``failed`` through ``update_workflow``.

The workflow transcript is persisted in the private ``_workflow_messages``
channel, so runs survive resume (including HITL interrupts) and stay invisible
to the main agent.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphBubbleUp

from opscloud.middleware.registry import register_middleware
from opscloud.state.workflow_channels import (
    ACTIVE_WORKFLOW_STATUSES,
    WorkflowChannels,
    coerce_workflow_status,
)
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

logger = get_logger(__name__)

WORKFLOW_NODE_PREFIX = "WorkflowMiddleware."
"""Stream namespace prefix for nested workflow-agent events (used by the TUI)."""

_KICKOFF_TEMPLATE = (
    "Run the workflow `{name}`.\n\n"
    "Operator arguments: {args}\n\n"
    "Resolve the workflow's inputs from these arguments (ask once for any required "
    "ones that are missing), execute the workflow steps, and finish by calling "
    "`update_workflow`."
)


def _cleared_run() -> dict[str, Any]:
    return {
        "_workflow_run_id": None,
        "_workflow_name": None,
        "_workflow_skill_path": None,
        "_workflow_args": None,
        "_workflow_status": None,
        "_workflow_status_note": None,
        "_workflow_artifacts": None,
        "_workflow_messages": {"reset": True},
    }


def _outcome_update(result: dict[str, Any]) -> dict[str, Any]:
    """Derive the workflow status from one workflow-agent turn result."""
    raw_tool = result.get("workflow_tool_update")
    tool_update: dict[str, Any] = raw_tool if isinstance(raw_tool, dict) else {}
    reported = tool_update.get("status")
    if reported in {"complete", "blocked", "failed"}:
        status = str(reported)
        note = tool_update.get("note")
    else:
        # The agent ended its turn without reporting an outcome (e.g. it asked
        # something in prose); keep the run attached for the operator's reply.
        status = "running"
        note = None
    update: dict[str, Any] = {"_workflow_status": status, "_workflow_status_note": note}
    artifacts = tool_update.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        update["_workflow_artifacts"] = [str(a) for a in artifacts]
    return update


@register_middleware(name="workflow")
class WorkflowMiddleware(AgentMiddleware[WorkflowChannels, Any]):
    """Dispatch typed workflow requests to the isolated workflow agent."""

    state_schema = WorkflowChannels

    def __init__(self, workflow_agent: Any | None = None) -> None:
        """Initialize with the compiled nested workflow agent.

        Args:
            workflow_agent: Tool-enabled agent that executes the selected workflow.
        """
        super().__init__()
        self._agent = workflow_agent

    # ── helpers ──────────────────────────────────────────

    @staticmethod
    def _emit(runtime: Any, payload: dict[str, Any]) -> None:
        writer = getattr(runtime, "stream_writer", None)
        if callable(writer):
            try:
                writer({"type": "workflow_phase", **payload})
            except Exception:
                logger.debug("Failed to emit workflow phase event", exc_info=True)

    @staticmethod
    def _finish(update: dict[str, Any]) -> dict[str, Any]:
        return {**update, "workflow_request": None, "_workflow_turn": True, "jump_to": "end"}

    def _reject(self, message: str) -> dict[str, Any]:
        logger.warning("Workflow request rejected: %s", message)
        return self._finish({"_workflow_status_note": message})

    @staticmethod
    def _agent_input(state: dict[str, Any], new_message: HumanMessage) -> dict[str, Any]:
        history = list(state.get("_workflow_messages") or [])
        return {
            "messages": [*history, new_message],
            "workflow_context": {
                "name": state.get("_workflow_name") or "",
                "skill_path": state.get("_workflow_skill_path") or "",
                "args": state.get("_workflow_args") or "",
            },
            "workflow_tool_update": None,
        }

    @staticmethod
    def _agent_update(result: object, new_message: HumanMessage, *, reset: bool = False) -> dict[str, Any]:
        values: dict[str, Any] = result if isinstance(result, dict) else {}
        messages = values.get("messages")
        transcript = list(messages) if isinstance(messages, list) else [new_message]
        update = _outcome_update(values)
        update["_workflow_messages"] = {"reset": True, "messages": transcript} if reset else transcript
        transfers = values.get("_session_cost_transfers")
        if isinstance(transfers, dict) and transfers:
            update["_session_cost_transfers"] = transfers
        return update

    # ── request validation ───────────────────────────────

    def _validate(self, state: dict[str, Any], request: dict[str, Any]) -> str | None:
        kind = request.get("kind")
        status = coerce_workflow_status(state.get("_workflow_status"))
        if kind == "start":
            workflow = request.get("workflow")
            if not isinstance(workflow, dict) or not workflow.get("path"):
                return "Workflow request is missing the workflow definition."
            if status in ACTIVE_WORKFLOW_STATUSES and not request.get("replace"):
                return (
                    f"Workflow `{state.get('_workflow_name')}` is still {status}. "
                    "Cancel it with `/workflow cancel` before starting another."
                )
        elif kind == "continue":
            if status not in ACTIVE_WORKFLOW_STATUSES:
                return "No running workflow to continue."
            if not str(request.get("message") or "").strip():
                return "Empty workflow message."
        elif kind != "cancel":
            return f"Unknown workflow request kind: {kind!r}."
        if kind != "cancel" and self._agent is None:
            return "Workflow agent is unavailable."
        return None

    # ── operations ───────────────────────────────────────

    def _begin(
        self,
        values: dict[str, Any],
        request: dict[str, Any],
        runtime: Any,
    ) -> tuple[dict[str, Any] | None, HumanMessage | None, dict[str, Any]]:
        """Prepare one request.

        Returns:
            ``(agent_input, message, base_update)``. ``agent_input`` is ``None``
            when no agent run is needed (``cancel``); otherwise the agent result
            is merged into ``base_update`` by ``_complete``.
        """
        kind = request["kind"]
        if kind == "cancel":
            return None, None, {**_cleared_run(), "_workflow_status": "cancelled"}

        if kind == "start":
            workflow = request["workflow"]
            run = {
                **_cleared_run(),
                # Deterministic so a resumed (HITL-interrupted) node re-creates the same run.
                "_workflow_run_id": str(request.get("request_id") or uuid.uuid4()),
                "_workflow_name": workflow.get("name"),
                "_workflow_skill_path": workflow.get("path"),
                "_workflow_args": request.get("args") or "",
                "_workflow_status": "running",
            }
            merged = {**values, **run, "_workflow_messages": []}
            kickoff = HumanMessage(
                content=_KICKOFF_TEMPLATE.format(
                    name=run["_workflow_name"] or "",
                    args=run["_workflow_args"] or "(none provided)",
                )
            )
            self._emit(runtime, {"phase": "running", "name": run["_workflow_name"]})
            return self._agent_input(merged, kickoff), kickoff, run

        # continue
        message = HumanMessage(content=str(request.get("message")))
        self._emit(runtime, {"phase": "running", "name": values.get("_workflow_name")})
        return self._agent_input(values, message), message, {}

    def _complete(
        self,
        request: dict[str, Any],
        base: dict[str, Any],
        message: HumanMessage,
        result: object,
    ) -> dict[str, Any]:
        reset = request["kind"] == "start"
        return self._finish({**base, **self._agent_update(result, message, reset=reset)})

    def _prepare(self, state: WorkflowChannels) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        """Return ``(values, request, early_update)``."""
        values: dict[str, Any] = dict(state)
        request = values.get("workflow_request")
        if not isinstance(request, dict):
            early = {"_workflow_turn": False} if values.get("_workflow_turn") else None
            return values, None, early
        error = self._validate(values, request)
        if error:
            return values, None, self._reject(error)
        return values, request, None

    def _failed(self, kind: str, exc: Exception) -> dict[str, Any]:
        logger.exception("Workflow %s request failed", kind)
        return self._finish({"_workflow_status": "failed", "_workflow_status_note": f"{type(exc).__name__}: {exc}"})

    # ── hooks ────────────────────────────────────────────

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: WorkflowChannels, runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Handle a pending workflow request synchronously."""
        values, request, early = self._prepare(state)
        if request is None:
            return early
        try:
            agent_input, message, base = self._begin(values, request, runtime)
            if agent_input is None or message is None:
                return self._finish(base)
            result = self._agent.invoke(agent_input, context=getattr(runtime, "context", None))  # type: ignore[union-attr]
            return self._complete(request, base, message, result)
        except GraphBubbleUp:
            # HITL / ask_user interrupt inside the workflow agent: let it reach the
            # TUI; the node re-runs on resume and the nested agent continues.
            raise
        except Exception as exc:
            return self._failed(request["kind"], exc)

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state: WorkflowChannels, runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Handle a pending workflow request asynchronously."""
        values, request, early = self._prepare(state)
        if request is None:
            return early
        try:
            agent_input, message, base = self._begin(values, request, runtime)
            if agent_input is None or message is None:
                return self._finish(base)
            result = await self._agent.ainvoke(agent_input, context=getattr(runtime, "context", None))  # type: ignore[union-attr]
            return self._complete(request, base, message, result)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            return self._failed(request["kind"], exc)


def workflow_is_open(state: dict[str, Any]) -> bool:
    """Return whether ``state`` holds an open (non-terminal) workflow run."""
    return coerce_workflow_status(state.get("_workflow_status")) in ACTIVE_WORKFLOW_STATUSES


__all__ = [
    "WORKFLOW_NODE_PREFIX",
    "WorkflowMiddleware",
    "_outcome_update",
    "workflow_is_open",
]
