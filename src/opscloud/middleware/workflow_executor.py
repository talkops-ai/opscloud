"""Middleware that runs *inside* the isolated workflow agent.

* ``WorkflowPlaybookMiddleware`` — pins the selected SKILL.md (the workflow
  definition) and the
  run parameters into every model call, so one compiled agent serves every
  workflow.
* ``WorkflowToolsMiddleware`` — exposes ``update_workflow``
  (complete / blocked / failed) so the agent reports the run outcome.

There is deliberately no planning or rubric grading here: a workflow is a
skill-defined procedure executed as written. Verification-driven work belongs
to ``/goal``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, NotRequired, cast, override

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command
from pydantic import Field

from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse

logger = get_logger(__name__)

WORKFLOW_TOOL_NAMES = frozenset({"update_workflow"})
_PLAYBOOK_CHAR_LIMIT = 120_000
_NOTE_CHAR_LIMIT = 4_000

WorkflowOutcome = Literal["complete", "blocked", "failed"]
"""Outcomes the agent can report through ``update_workflow``."""


class WorkflowAgentState(AgentState):
    """State of the nested workflow agent graph."""

    workflow_context: NotRequired[dict[str, Any] | None]
    """Public input: ``{name, skill_path, args}``."""

    workflow_tool_update: NotRequired[dict[str, Any] | None]
    """Public output: last ``update_workflow`` call ``{status, note, artifacts}``."""


# ---------------------------------------------------------------------------
# Playbook injection
# ---------------------------------------------------------------------------


def _read_playbook(skill_path: str) -> str:
    try:
        from opscloud.skills.loader import load_skill_content

        content = load_skill_content(skill_path) or ""
    except Exception:
        try:
            content = Path(skill_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            content = ""
    if len(content) > _PLAYBOOK_CHAR_LIMIT:
        content = content[:_PLAYBOOK_CHAR_LIMIT] + "\n\n…(workflow definition truncated; read the file for the rest)"
    return content


def build_playbook_block(context: Mapping[str, Any] | None) -> str | None:
    """Render the ``<workflow_definition>`` and ``<workflow_run>`` system blocks."""
    if not isinstance(context, Mapping):
        return None
    skill_path = str(context.get("skill_path") or "").strip()
    if not skill_path:
        return None
    playbook = _read_playbook(skill_path)
    if not playbook.strip():
        return None
    skill_dir = str(Path(skill_path).parent)
    name = str(context.get("name") or "")
    args = str(context.get("args") or "").strip() or "(none provided)"
    return (
        f'<workflow_definition name="{name}" skill_md="{skill_path}" directory="{skill_dir}">\n'
        f"{playbook}\n</workflow_definition>\n\n"
        f"<workflow_run>\n### Workflow\n{name}\n\n### Operator arguments\n{args}\n</workflow_run>"
    )


class WorkflowPlaybookMiddleware(AgentMiddleware[WorkflowAgentState, Any]):
    """Inject the selected workflow definition and run parameters into the system message."""

    state_schema = WorkflowAgentState

    def _modified(self, request: ModelRequest) -> ModelRequest:
        raw_context = request.state.get("workflow_context")
        context: Mapping[str, Any] | None = (
            cast("Mapping[str, Any]", raw_context) if isinstance(raw_context, Mapping) else None
        )
        block = build_playbook_block(context)
        if not block:
            return request
        return request.override(system_message=append_to_system_message(request.system_message, block))

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Pin the playbook into a synchronous model call."""
        return handler(self._modified(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Pin the playbook into an asynchronous model call."""
        return await handler(self._modified(request))


# ---------------------------------------------------------------------------
# update_workflow tool
# ---------------------------------------------------------------------------

_OUTCOME_REPLIES: dict[str, str] = {
    "complete": "Workflow marked complete. Now give the operator a concise final summary.",
    "blocked": "Workflow marked blocked. Ask the operator for what you need.",
    "failed": (
        "Workflow marked failed. Tell the operator which step failed, the error, and the "
        "state things were left in."
    ),
}


def _update_workflow_command(
    *,
    status: WorkflowOutcome,
    note: str,
    artifacts: list[str] | None,
    tool_call_id: str,
) -> Command[Any]:
    clean_note = (note or "").strip()
    if not clean_note:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"Provide a note with evidence before marking the workflow {status}.",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    clean_artifacts = [a.strip() for a in (artifacts or []) if a.strip()]
    return Command(
        update={
            "workflow_tool_update": {
                "status": status,
                "note": clean_note[:_NOTE_CHAR_LIMIT],
                "artifacts": clean_artifacts,
            },
            "messages": [ToolMessage(content=_OUTCOME_REPLIES[status], tool_call_id=tool_call_id)],
        }
    )


class WorkflowToolsMiddleware(AgentMiddleware[WorkflowAgentState, Any]):
    """Expose ``update_workflow`` to the workflow agent."""

    state_schema = WorkflowAgentState

    def __init__(self) -> None:
        """Initialize the workflow tool set."""
        super().__init__()

        @tool
        def update_workflow(
            status: Annotated[
                WorkflowOutcome,
                Field(
                    description=(
                        "`complete` when every workflow step finished; `blocked` when you "
                        "cannot continue without the operator; `failed` when a step failed "
                        "and the workflow defines no recovery path."
                    )
                ),
            ],
            note: Annotated[
                str,
                Field(
                    max_length=_NOTE_CHAR_LIMIT,
                    description=(
                        "What was done and the key evidence, the specific blocker, or the "
                        "failing step and its error."
                    ),
                ),
            ],
            tool_call_id: Annotated[str, InjectedToolCallId],
            artifacts: Annotated[
                list[str] | None,
                Field(description="Absolute paths of files produced by this run (reports, logs, exports)."),
            ] = None,
        ) -> Command[Any]:
            """Report the outcome of the current workflow run.

            Call once the run reaches an outcome: `complete`, `blocked` (needs the
            operator), or `failed`. Does not end the conversation by itself; follow
            up with a summary or a question for the operator.

            Returns:
                Command recording the outcome and a confirmation tool message.
            """
            return _update_workflow_command(
                status=status,
                note=note,
                artifacts=artifacts,
                tool_call_id=tool_call_id,
            )

        self.tools = [update_workflow]


__all__ = [
    "WORKFLOW_TOOL_NAMES",
    "WorkflowAgentState",
    "WorkflowOutcome",
    "WorkflowPlaybookMiddleware",
    "WorkflowToolsMiddleware",
    "_update_workflow_command",
    "build_playbook_block",
]
