"""Helpers for loading and formatting skill invocations."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opscloud.config.env_vars import EXTRA_SKILLS_DIRS
from opscloud.config.paths import (
    get_built_in_skills_dir,
    get_project_agent_skills_dir,
    get_project_claude_skills_dir,
    get_project_skills_dir,
    get_user_agent_skills_dir,
    get_user_claude_skills_dir,
    get_user_skills_dir,
)

if TYPE_CHECKING:
    from opscloud.skills.load import ExtendedSkillMetadata


@dataclass(frozen=True)
class SkillInvocationEnvelope:
    """Structured prompt and checkpoint metadata for a skill invocation.

    Attributes:
        prompt: Composed prompt that wraps `SKILL.md` content with
            invocation instructions.
        message_kwargs: Extra fields merged into the initial HumanMessage.
        skill_name: Invoked skill name for trace attribution.
    """

    prompt: str
    message_kwargs: dict[str, Any]
    skill_name: str


def discover_skills_and_roots(
    assistant_id: str = "opscloud",
    *,
    plugin_skill_sources: tuple[tuple[Path, str], ...] = (),
    plugin_skill_roots: tuple[Path, ...] = (),
    path_base: Path | None = None,
) -> tuple[list[ExtendedSkillMetadata], list[Path]]:
    """Discover skills and build pre-resolved containment roots.

    Args:
        assistant_id: Agent identifier used to resolve user skill directories.
        plugin_skill_sources: Plugin-owned skill directories and namespaces.
        plugin_skill_roots: Plugin-owned roots allowed for content loading.
        path_base: User working directory for resolving relative skill roots.

    Returns:
        Tuple of `(skill metadata list, pre-resolved containment roots)`.
    """
    from opscloud.config.settings import settings
    from opscloud.skills.load import list_skills
    from opscloud.skills.trust import load_trusted_skill_dirs

    effective_project_root = settings.project_root

    # Auto-resolve plugin skills if none provided
    p_sources = list(plugin_skill_sources)
    p_roots = list(plugin_skill_roots)
    if not p_sources:
        try:
            from opscloud.plugins.adapters.skills import discover_plugin_skill_sources_and_roots

            discovered_p_sources, discovered_p_roots = discover_plugin_skill_sources_and_roots(
                project_root=effective_project_root
            )
            p_sources.extend(discovered_p_sources)
            p_roots.extend(discovered_p_roots)
        except Exception:
            pass

    skills = list_skills(
        built_in_skills_dir=get_built_in_skills_dir(),
        plugin_skill_sources=p_sources,
        user_skills_dir=get_user_skills_dir(assistant_id),
        project_skills_dir=get_project_skills_dir(effective_project_root),
        user_agent_skills_dir=get_user_agent_skills_dir(),
        project_agent_skills_dir=get_project_agent_skills_dir(effective_project_root),
        user_claude_skills_dir=get_user_claude_skills_dir(),
        project_claude_skills_dir=get_project_claude_skills_dir(effective_project_root),
    )

    roots = [
        path.resolve()
        for path in (
            get_built_in_skills_dir(),
            *p_roots,
            get_user_skills_dir(assistant_id),
            get_project_skills_dir(effective_project_root),
            get_user_agent_skills_dir(),
            get_project_agent_skills_dir(effective_project_root),
            get_user_claude_skills_dir(),
            get_project_claude_skills_dir(effective_project_root),
        )
        if path is not None and path.exists()
    ]

    # Add extra skills dirs from environment and settings
    extra_dirs_val = os.environ.get(EXTRA_SKILLS_DIRS, "")
    if extra_dirs_val:
        base = path_base or Path.cwd()
        for part in extra_dirs_val.split(os.pathsep):
            part = part.strip()
            if part:
                p = (base / part).resolve() if not os.path.isabs(part) else Path(part).resolve()
                if p.is_dir():
                    roots.append(p)

    extra_settings_dirs = getattr(settings, "extra_skills_dirs", None)
    if extra_settings_dirs and isinstance(extra_settings_dirs, (list, tuple)):
        base = path_base or Path.cwd()
        for item in extra_settings_dirs:
            p = (base / item).resolve() if not os.path.isabs(str(item)) else Path(item).resolve()
            if p.is_dir():
                roots.append(p)

    # Persisted in-the-moment approvals extend the containment allowlist
    roots.extend(load_trusted_skill_dirs())

    return skills, roots


def build_skill_invocation_envelope(
    skill: ExtendedSkillMetadata | dict[str, Any],
    content: str,
    args: str = "",
) -> SkillInvocationEnvelope:
    """Build the wrapped prompt and persisted metadata for a skill.

    Args:
        skill: Loaded skill metadata.
        content: Raw `SKILL.md` content.
        args: Optional user request appended after the skill body.

    Returns:
        A `SkillInvocationEnvelope` with the composed prompt and
            `message_kwargs` containing persisted skill metadata.
    """
    skill_name = str(skill.get("name", "unknown"))
    prompt = (
        f"I'm invoking the skill `{skill_name}`. "
        "Below are the full instructions from the skill's SKILL.md file. "
        "Follow these instructions to complete the task.\n\n"
        f"---\n{content}\n---"
    )
    skill_path = str(skill.get("path", "") or "")
    if skill_path:
        skill_p = Path(skill_path)
        skill_dir = skill_p.parent if skill_p.is_file() else skill_p
        prompt += f"\n\nSkill directory on disk: `{skill_dir}`"
    if args:
        prompt += f"\n\n**User request:** {args}"

    message_kwargs = {
        "additional_kwargs": {
            "__skill": {
                "name": skill_name,
                "description": str(skill.get("description", "")),
                "source": str(skill.get("source", "")),
                "args": args,
            },
        },
    }
    return SkillInvocationEnvelope(
        prompt=prompt,
        message_kwargs=message_kwargs,
        skill_name=skill_name,
    )


def parse_skill_command(command: str) -> tuple[str, str]:
    """Extract skill name and args from a ``/skill:<name>`` command."""
    if not command.startswith("/skill:"):
        return "", ""
    after_prefix = command[len("/skill:") :].strip()
    parts = after_prefix.split(maxsplit=1)
    if not parts or not parts[0]:
        return "", ""
    skill_name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return skill_name, args


STATIC_SKILL_ALIASES: frozenset[str] = frozenset({"remember", "skill-creator"})

__all__ = [
    "STATIC_SKILL_ALIASES",
    "SkillInvocationEnvelope",
    "build_skill_invocation_envelope",
    "discover_skills_and_roots",
    "parse_skill_command",
]
