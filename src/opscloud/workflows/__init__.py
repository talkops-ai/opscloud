"""Isolated skill workflows (``/workflow``).

A workflow is any multi-step, possibly conditional procedure defined by a skill
bound to the main deep agent (provisioning, releases, incident response,
audits, migrations, reporting, ...) that an operator triggers with parameters.
A single isolated workflow agent resolves the parameters, executes the steps
(including conditional branches) under the normal HITL approval policy, and
reports the outcome — without touching the main transcript.
"""

from opscloud.workflows.discovery import (
    WORKFLOW_BUILT_IN_SKILLS_DIR,
    list_workflows,
    resolve_workflow,
    workflow_from_skill,
)
from opscloud.workflows.models import WorkflowRequest, WorkflowSpec

__all__ = [
    "WORKFLOW_BUILT_IN_SKILLS_DIR",
    "WorkflowRequest",
    "WorkflowSpec",
    "list_workflows",
    "resolve_workflow",
    "workflow_from_skill",
]
