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


MAX_SKILL_CONTENT_CHARS = 24000
"""Maximum SKILL.md body length (24,000 characters)."""


def _strip_skill_frontmatter(text: str) -> str:
    """Strip YAML frontmatter delimited by `---` from SKILL.md content."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text.strip()
    end = stripped.find("\n---", 3)
    if end == -1:
        return text.strip()
    after = end + 4
    return stripped[after:].strip()


def format_skill_slash_name(skill: ExtendedSkillMetadata | dict[str, Any] | Any) -> str:
    """Return the canonical slash-command identifier for a skill (without leading ``/``).

    Formatting rules:
    - Non-plugin skills keep their name as-is (e.g. ``reverse-engineer-anything``).
    - Plugin skills use ``<plugin-name>:<skill-name>`` (or ``<plugin-name>:<rel>:<skill-name>``),
      stripping any ``@<marketplace>`` suffix from the plugin namespace and avoiding duplicate
      ``<plugin-name>:`` prefixes.
    """
    if isinstance(skill, str):
        raw_name = skill.strip().lstrip("/")
        source = "plugin" if ":" in raw_name else ""
        plugin_id = ""
    elif isinstance(skill, dict):
        raw_name = str(skill.get("name") or "").strip().lstrip("/")
        source = str(skill.get("source") or "").strip().lower()
        plugin_id = str(skill.get("plugin_id") or "").strip()
    else:
        raw_name = str(getattr(skill, "name", "") or "").strip().lstrip("/")
        source = str(getattr(skill, "source", "") or "").strip().lower()
        plugin_id = str(getattr(skill, "plugin_id", "") or "").strip()

    if raw_name.startswith("skill:"):
        raw_name = raw_name[len("skill:") :].strip()

    if not raw_name:
        return ""

    if ":" in raw_name:
        ns_part, rest = raw_name.split(":", 1)
        clean_plugin = ns_part.split("@", 1)[0].strip()
        clean_rest = rest.strip()
        # Strip duplicate leading "<plugin>:" if SKILL.md frontmatter already included it
        if clean_plugin and clean_rest.lower().startswith(f"{clean_plugin.lower()}:"):
            clean_rest = clean_rest[len(clean_plugin) + 1 :]
        return f"{clean_plugin}:{clean_rest}" if clean_plugin else clean_rest

    if source == "plugin" and plugin_id:
        clean_plugin = plugin_id.split("@", 1)[0].strip()
        clean_rest = raw_name
        if clean_plugin and clean_rest.lower().startswith(f"{clean_plugin.lower()}:"):
            clean_rest = clean_rest[len(clean_plugin) + 1 :]
        if clean_plugin:
            return f"{clean_plugin}:{clean_rest}"

    return raw_name


def match_discovered_skill(
    skills: Any,
    query_name: str,
) -> dict[str, Any] | Any | None:
    """Resolve a skill by canonical ``<plugin>:<skill>``, raw name, or short name.

    Resolution order:
    1. Exact match on canonical slash name (``<plugin>:<skill>`` or ``<skill>``).
    2. Exact match on raw ``skill["name"]`` (including ``@marketplace`` if present).
    3. Exact match on the terminal ``:<short-name>`` segment.
    """
    q = query_name.strip().lstrip("/")
    if q.lower().startswith("skill:"):
        q = q[len("skill:") :].strip()
    q_lower = q.lower()
    if not q_lower or not skills:
        return None

    q_canonical = format_skill_slash_name(q_lower).lower()

    # Pass 1: exact canonical or raw name match
    for skill in skills:
        raw_name = (
            str(skill.get("name") or "")
            if isinstance(skill, dict)
            else str(getattr(skill, "name", "") or "")
        ).strip()
        if not raw_name:
            continue
        canonical = format_skill_slash_name(skill).lower()
        if canonical == q_lower or canonical == q_canonical or raw_name.lower() == q_lower:
            return skill

    # Pass 2: suffix match after ':' (e.g. "signing-in-to-aws" -> "aws-core:signing-in-to-aws")
    for skill in skills:
        canonical = format_skill_slash_name(skill).lower()
        if ":" in canonical and canonical.rsplit(":", 1)[-1] == q_lower:
            return skill

    return None


def build_skill_invocation_envelope(
    skill: ExtendedSkillMetadata | dict[str, Any],
    content: str,
    args: str = "",
) -> SkillInvocationEnvelope:
    """Build the wrapped prompt and persisted metadata for a skill.

    Embeds the ``<SKILL>...</SKILL>`` envelope (with frontmatter stripped and
    body truncated to 24,000 chars) while preserving OpsCloud's skill directory
    context and ``__skill`` checkpoint metadata.

    Args:
        skill: Loaded skill metadata.
        content: Raw `SKILL.md` content.
        args: Optional user request appended after the skill body.

    Returns:
        A `SkillInvocationEnvelope` with the composed prompt and
            `message_kwargs` containing persisted skill metadata.
    """
    raw_name = str(skill.get("name", "unknown"))
    canonical_name = format_skill_slash_name(skill) or raw_name
    stripped_body = _strip_skill_frontmatter(content)

    skill_path = str(skill.get("path", "") or "")
    skill_dir: Path | None = None
    if skill_path:
        skill_p = Path(skill_path)
        skill_dir = (
            skill_p.parent
            if (skill_p.name.lower() == "skill.md" or skill_p.is_file())
            else skill_p
        )

    # Support Claude Code-style dynamic pre-computation (!`cmd`, ```! ... ```, $ARGUMENTS, ${OPSCLOUD_SKILL_DIR})
    if any(
        marker in stripped_body
        for marker in ("!`", "```!", "$ARGUMENTS", "${OPSCLOUD_", "${CLAUDE_")
    ):
        from opscloud.commands.custom.expansion import (
            execute_inline_shell_commands,
            substitute_arguments,
            substitute_environment_variables,
        )

        stripped_body = substitute_arguments(
            stripped_body,
            args,
            auto_append=False,
        )
        stripped_body = substitute_environment_variables(
            stripped_body,
            skill_dir=skill_dir,
            project_dir=Path.cwd(),
        )
        stripped_body = execute_inline_shell_commands(
            stripped_body,
            cwd=Path.cwd(),
            enabled=True,
        )

    if len(stripped_body) > MAX_SKILL_CONTENT_CHARS:
        stripped_body = stripped_body[:MAX_SKILL_CONTENT_CHARS]

    scope_item_xml = (
        f"<SKILL>The user has explicitly invoked the ({canonical_name}) skill. "
        "You must strictly follow the instructions in this skill to process their request. "
        f"Here are its contents:\n{stripped_body}</SKILL>"
    )

    prompt = (
        f"I'm invoking the skill `{canonical_name}`. "
        "Below are the full instructions from the skill's SKILL.md file. "
        "Follow these instructions to complete the task.\n\n"
        f"{scope_item_xml}"
    )
    if skill_dir is not None:
        prompt += f"\n\nSkill directory on disk: `{skill_dir}`"
    if args:
        prompt += f"\n\n**User request:** {args}"

    message_kwargs = {
        "additional_kwargs": {
            "__skill": {
                "name": raw_name,
                "slash_command": f"/{canonical_name}",
                "kind": "SLASH_COMMAND_KIND_SKILL",
                "description": str(skill.get("description", "")),
                "source": str(skill.get("source", "")),
                "args": args,
                "scope_item": scope_item_xml,
                "body": stripped_body,
            },
        },
    }
    return SkillInvocationEnvelope(
        prompt=prompt,
        message_kwargs=message_kwargs,
        skill_name=raw_name,
    )


def parse_skill_command(command: str) -> tuple[str, str]:
    """Extract skill name and args from ``/<plugin>:<skill>``, ``/<skill>``, ``/skill <name>``, or ``/skill:<name>``."""
    clean = command.strip()
    if not clean.startswith("/"):
        return "", ""

    lower = clean.lower()
    if lower.startswith("/skill:"):
        after_prefix = clean[len("/skill:") :].strip()
        parts = after_prefix.split(maxsplit=1)
        if not parts or not parts[0]:
            return "", ""
        return parts[0].lower(), (parts[1] if len(parts) > 1 else "")

    if lower == "/skill" or lower.startswith("/skill "):
        after_prefix = clean[len("/skill") :].strip()
        parts = after_prefix.split(maxsplit=1)
        if not parts or not parts[0]:
            return "", ""
        return parts[0].lstrip("/").lower(), (parts[1] if len(parts) > 1 else "")

    parts = clean[1:].strip().split(maxsplit=1)
    if not parts or not parts[0]:
        return "", ""
    skill_name = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    return skill_name, args


STATIC_SKILL_ALIASES: frozenset[str] = frozenset({"remember", "skill-creator"})

__all__ = [
    "MAX_SKILL_CONTENT_CHARS",
    "STATIC_SKILL_ALIASES",
    "SkillInvocationEnvelope",
    "build_skill_invocation_envelope",
    "discover_skills_and_roots",
    "format_skill_slash_name",
    "match_discovered_skill",
    "parse_skill_command",
]

