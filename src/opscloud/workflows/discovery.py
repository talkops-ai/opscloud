"""Discover which main-agent skills can run as isolated workflows.

Every skill bound directly to the main deep agent (built-in, user, project,
and *vertical* plugin skills — agent-plugin skills stay with their subagent)
is a workflow by default.  Authors can opt out through SKILL.md frontmatter,
either top-level or under ``metadata``::

    ---
    name: restart-unhealthy-pods
    description: ...
    workflow: true            # default true; false hides it from /workflow
    ---

The Agent Skills spec only allows string values under ``metadata``, so the
spec-compliant form is ``metadata: {workflow: "false"}``; both forms work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opscloud.utils.logger import get_logger
from opscloud.workflows.models import WorkflowSpec

logger = get_logger(__name__)

WORKFLOW_FLAG_KEYS: tuple[str, ...] = ("workflow", "opscloud-workflow")
DEFAULT_WORKFLOW_ENABLED = True
"""Default when a skill declares no ``workflow`` flag."""

WORKFLOW_BUILT_IN_SKILLS_DIR = Path(__file__).parent / "built_in_skills"
"""Skills attached to the workflow agent (never to the main agent)."""


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
    return default


def _flag(frontmatter: dict[str, Any], keys: tuple[str, ...], default: bool) -> bool:
    metadata = frontmatter.get("metadata")
    for container in (frontmatter, metadata if isinstance(metadata, dict) else {}):
        for key in keys:
            if key in container:
                return _coerce_bool(container[key], default)
    return default


def _resolve_skill_md(path: str) -> str:
    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / "SKILL.md"
    return str(candidate)


def workflow_from_skill(skill: dict[str, Any]) -> WorkflowSpec | None:
    """Build a ``WorkflowSpec`` from one discovered skill dict, or ``None``."""
    name = str(skill.get("name") or "").strip()
    path = str(skill.get("path") or "").strip()
    if not name or not path:
        return None
    if skill.get("enabled") is False or skill.get("source") == "subagent":
        return None
    raw_frontmatter = skill.get("frontmatter")
    frontmatter: dict[str, Any] = dict(raw_frontmatter) if isinstance(raw_frontmatter, dict) else {}
    if not _flag(frontmatter, WORKFLOW_FLAG_KEYS, DEFAULT_WORKFLOW_ENABLED):
        return None
    return WorkflowSpec(
        name=name.lower(),
        short_name=name.rsplit(":", 1)[-1].lower(),
        description=str(skill.get("description") or "").strip(),
        path=_resolve_skill_md(path),
        source=str(skill.get("source") or "built-in"),
        plugin_id=(str(skill["plugin_id"]) if skill.get("plugin_id") else (name.split(":", 1)[0] if ":" in name else None)),
        frontmatter=frontmatter,
    )


def list_workflows(skills: list[dict[str, Any]] | None = None) -> list[WorkflowSpec]:
    """Return workflow-capable skills, discovering main-agent skills if needed.

    Args:
        skills: Pre-discovered skill dicts (e.g. the TUI's cached list). When
            omitted, skills are discovered with ``include_subagents=False`` so
            agent-plugin skills are excluded.
    """
    if skills is None:
        try:
            from opscloud.skills.loader import list_skills

            skills = [dict(s) for s in list_skills(include_plugins=True, include_subagents=False)]
        except Exception as exc:
            logger.warning("Workflow discovery failed: %s", exc)
            skills = []
    specs: list[WorkflowSpec] = []
    seen: set[str] = set()
    for skill in skills:
        spec = workflow_from_skill(skill)
        if spec is None or spec.name in seen:
            continue
        seen.add(spec.name)
        specs.append(spec)
    specs.sort(key=lambda s: (s.plugin_id or "", s.short_name))
    return specs


def resolve_workflow(name: str, workflows: list[WorkflowSpec]) -> WorkflowSpec | None:
    """Resolve ``name`` by fully qualified name first, then by terminal name."""
    wanted = name.strip().lower()
    if not wanted:
        return None
    for spec in workflows:
        if spec.name == wanted:
            return spec
    matches = [spec for spec in workflows if spec.short_name == wanted]
    return matches[0] if len(matches) == 1 else (matches[0] if matches else None)


__all__ = [
    "DEFAULT_WORKFLOW_ENABLED",
    "WORKFLOW_BUILT_IN_SKILLS_DIR",
    "list_workflows",
    "resolve_workflow",
    "workflow_from_skill",
]
