"""Skill loader for CLI commands and agent runtime.

This module provides filesystem-based skill discovery across built-in, plugin,
user, project, and experimental Claude skill locations. It wraps prebuilt
middleware functionality from deepagents.middleware.skills and adapts it for
direct filesystem access needed by CLI commands and runtime discovery.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Literal, cast

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.skills import (
    SkillMetadata,
    _list_skills as list_skills_from_backend,
)
import yaml

from opscloud._version import __version__ as _cli_version
from opscloud.config import paths
from opscloud.config.env_vars import EXTRA_SKILLS_DIRS
from opscloud.skills.merge import merge_skill

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


class ExtendedSkillMetadata(SkillMetadata, total=False):
    """Extended skill metadata for CLI display and runtime, adding source tracking.

    Attributes:
        source: Origin of the skill. One of `'built-in'`, `'plugin'`, `'user'`,
            `'project'`, or `'claude (experimental)'`.
    """

    source: Literal["built-in", "plugin", "user", "project", "claude (experimental)", "subagent"]
    location: str
    virtual_path: str
    scope: str
    plugin_id: str | None
    plugin_type: str | None
    domain: str
    tags: list[str]
    preview: str
    enabled: bool
    frontmatter: dict[str, Any]


def _parse_skill_file(skill_md: Path) -> dict[str, Any]:
    """Parse SKILL.md for frontmatter and markdown preview."""
    meta: dict[str, Any] = {
        "description": "",
        "license": None,
        "compatibility": None,
        "tags": [],
        "domain": "",
        "preview": "",
        "frontmatter": {},
    }
    if not skill_md.is_file():
        return meta

    try:
        content = skill_md.read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
        if match:
            fm_text, body_text = match.group(1), match.group(2)
            fm = yaml.safe_load(fm_text)
            if isinstance(fm, dict):
                meta["frontmatter"] = fm
                meta["description"] = str(fm.get("description") or "").strip()
                meta["license"] = fm.get("license")
                meta["compatibility"] = fm.get("compatibility")
                raw_tags = fm.get("tags") or []
                meta["tags"] = [str(t) for t in raw_tags] if isinstance(raw_tags, (list, tuple)) else []
                meta["domain"] = str(fm.get("domain") or "")

            body_clean = body_text.strip()
            body_lines = [
                line.strip()
                for line in body_clean.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if body_lines:
                meta["preview"] = "\n".join(body_lines[:4])
            else:
                meta["preview"] = meta["description"]
        else:
            lines = [
                line_item.strip()
                for line_item in content.splitlines()
                if line_item.strip() and not line_item.strip().startswith("#")
            ]
            meta["preview"] = "\n".join(lines[:4]) if lines else ""
    except Exception as exc:
        logger.debug("Failed parsing skill metadata from %s: %s", skill_md, exc)

    return meta


def list_skills(
    *,
    built_in_skills_dir: Path | None = None,
    plugin_skill_sources: Sequence[tuple[Path, str]] = (),
    user_skills_dir: Path | None = None,
    project_skills_dir: Path | None = None,
    user_agent_skills_dir: Path | None = None,
    project_agent_skills_dir: Path | None = None,
    user_claude_skills_dir: Path | None = None,
    project_claude_skills_dir: Path | None = None,
    include_plugins: bool = True,
    include_subagents: bool = False,
    project_root: Path | None = None,
    store: Any = None,
) -> list[ExtendedSkillMetadata]:
    """List skills from built-in, plugin, user, and/or project directories.

    Precedence order (lowest to highest):
    0. `built_in_skills_dir` (`<package>/built_in_skills/`)
    1. `plugin_skill_sources`
    2. `user_skills_dir` (`~/.opscloud/{agent}/skills/` or `~/.opscloud/skills/`)
    3. `user_agent_skills_dir` (`~/.agents/skills/`)
    4. `project_skills_dir` (`.opscloud/skills/`)
    5. `project_agent_skills_dir` (`.agents/skills/`)
    6. `user_claude_skills_dir` (`~/.claude/skills/`, experimental)
    7. `project_claude_skills_dir` (`.claude/skills/`, experimental)

    Skills from higher-precedence directories override those with the same name.

    Returns:
        Merged list of skill metadata from all sources.
    """
    all_skills: dict[str, ExtendedSkillMetadata] = {}
    merged_source_labels: dict[str, str | None] = {}

    effective_root = project_root
    if effective_root is None:
        try:
            from opscloud.config.settings import settings

            effective_root = settings.project_root or paths.find_project_root()
        except Exception:
            effective_root = paths.find_project_root()

    bi_dir = built_in_skills_dir or paths.get_built_in_skills_dir()
    u_dir = user_skills_dir or paths.get_user_skills_dir("opscloud")
    ua_dir = user_agent_skills_dir or paths.get_user_agent_skills_dir()
    p_dir = project_skills_dir or paths.get_project_skills_dir(effective_root)
    pa_dir = project_agent_skills_dir or paths.get_project_agent_skills_dir(effective_root)
    uc_dir = user_claude_skills_dir or paths.get_user_claude_skills_dir()
    pc_dir = project_claude_skills_dir or paths.get_project_claude_skills_dir(effective_root)

    # Resolve plugin skill sources if not explicitly supplied
    plugin_sources = list(plugin_skill_sources)
    if not plugin_sources and include_plugins:
        try:
            from opscloud.plugins.adapters.skills import discover_plugin_skill_sources_and_roots

            p_srcs, _ = discover_plugin_skill_sources_and_roots(project_root=effective_root)
            plugin_sources.extend(p_srcs)
        except Exception as exc:
            logger.debug("Could not resolve plugin skill sources in list_skills: %s", exc)

    sources: list[tuple[Path | None, str, bool, str]] = [
        (bi_dir, "built-in", False, ""),
        *[
            (path, "plugin", False, namespace)
            for path, namespace in plugin_sources
        ],
        (u_dir, "user", False, ""),
        (ua_dir, "user", False, ""),
        (p_dir, "project", False, ""),
        (pa_dir, "project", False, ""),
        (uc_dir, "claude (experimental)", True, ""),
        (pc_dir, "claude (experimental)", True, ""),
    ]

    # Optional subagents bundled skills
    if include_subagents:
        try:
            from opscloud.subagents.loader import list_subagents

            for sa in list_subagents(project_root=project_root, include_plugins=True):
                sa_path = sa.get("path")
                if sa_path:
                    p = Path(sa_path)
                    bundle_dir = p.parent.parent if p.parent.name == "agents" else p.parent
                    skills_dir = bundle_dir / "skills"
                    if skills_dir.is_dir():
                        sources.append((skills_dir, "subagent", False, sa["name"]))
        except Exception as exc:
            logger.debug("Could not discover subagent skills: %s", exc)

    for skill_dir, source_label, experimental, namespace in sources:
        if not skill_dir or not skill_dir.exists():
            continue
        try:
            backend = FilesystemBackend(root_dir=str(skill_dir), virtual_mode=False)
            if namespace:
                from opscloud.middleware.skills import load_namespaced_skills

                skills = load_namespaced_skills(
                    backend, str(skill_dir.resolve()), namespace
                )
            else:
                skills = list_skills_from_backend(backend=backend, source_path=".")

            if experimental and skills:
                logger.debug(
                    "Discovered %d skill(s) from experimental Claude path: %s",
                    len(skills),
                    skill_dir,
                )

            for skill in skills:
                extra: dict[str, object] = {
                    "source": source_label,
                    "scope": source_label.upper(),
                }
                if source_label == "built-in":
                    extra["metadata"] = {
                        **(skill.get("metadata") or {}),
                        "opscloud-version": _cli_version,
                    }

                name = skill.get("name", "")
                skill_path = Path(skill.get("path", "")) if skill.get("path") else (skill_dir / name)
                skill_md_path = skill_path if skill_path.is_file() else (skill_path / "SKILL.md")

                parsed_meta = _parse_skill_file(skill_md_path)
                desc = parsed_meta["description"] or skill.get("description", "")

                extended = cast(
                    "ExtendedSkillMetadata",
                    {
                        **skill,
                        **extra,
                        "name": name,
                        "description": desc,
                        "path": str(skill_md_path if skill_md_path.is_file() else skill_path),
                        "location": str(skill_path.resolve() if skill_path.is_dir() else skill_path.parent.resolve()),
                        "virtual_path": f"/skills/{name}/SKILL.md",
                        "plugin_id": namespace or None,
                        "license": parsed_meta.get("license") or skill.get("license"),
                        "compatibility": parsed_meta.get("compatibility") or skill.get("compatibility"),
                        "tags": parsed_meta.get("tags") or [],
                        "domain": parsed_meta.get("domain") or "",
                        "preview": parsed_meta.get("preview") or desc[:150],
                        "enabled": True,
                        "frontmatter": parsed_meta.get("frontmatter") or {},
                    },
                )
                merge_skill(
                    all_skills,
                    merged_source_labels,
                    extended,
                    source_label=source_label,
                )
        except Exception:
            logger.warning(
                "Could not load skills from %s",
                skill_dir,
                exc_info=True,
            )

    return list(all_skills.values())


def load_skill_content(
    skill_path: str,
    *,
    allowed_roots: Sequence[Path] = (),
) -> str | None:
    """Read full raw SKILL.md content for a skill, verifying containment safety (SSRF/traversal prevention).

    Args:
        skill_path: Path to the SKILL.md file or skill directory.
        allowed_roots: Skill root directories the resolved path must be contained within.

    Returns:
        Full text content of the SKILL.md file, or None on read failure.

    Raises:
        PermissionError: If the resolved path is outside all allowed_roots.
    """
    path = Path(skill_path).resolve()
    if path.is_dir():
        path = path / "SKILL.md"

    if allowed_roots:
        resolved_roots = [root.resolve() for root in allowed_roots]
        if not any(path.is_relative_to(root) for root in resolved_roots):
            logger.warning(
                "Skill path %s is outside all allowed roots, refusing to read",
                skill_path,
            )
            msg = (
                f"Skill path {skill_path} resolves outside all allowed skill "
                "directories. If this is a symlink, add the target directory to "
                f"{EXTRA_SKILLS_DIRS} or [skills].extra_allowed_dirs "
                f"in {paths.PATHS.display(paths.CONFIG_PATH)}."
            )
            raise PermissionError(msg)

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.warning(
            "Could not read skill content from %s", skill_path, exc_info=True
        )
        return None


def get_skill_by_name(
    name: str,
    *,
    project_root: Path | None = None,
    store: Any = None,
) -> ExtendedSkillMetadata | None:
    """Get a single skill's metadata by name."""
    skills = list_skills(project_root=project_root, store=store, include_subagents=True)
    normalized = name.strip().lower()
    for s in skills:
        s_name = s.get("name", "").lower()
        if s_name == normalized or (":" in s_name and s_name.rsplit(":", 1)[-1] == normalized):
            return s
    return None


def get_skill_content_by_name(
    name: str,
    *,
    project_root: Path | None = None,
    store: Any = None,
) -> tuple[ExtendedSkillMetadata | None, str | None]:
    """Get skill metadata and its raw markdown content by name."""
    skill = get_skill_by_name(name, project_root=project_root, store=store)
    if not skill:
        return None, None
    skill_path = skill.get("path")
    if not skill_path:
        return skill, None
    content = load_skill_content(skill_path)
    return skill, content


__all__ = [
    "ExtendedSkillMetadata",
    "SkillMetadata",
    "get_skill_by_name",
    "get_skill_content_by_name",
    "list_skills",
    "load_skill_content",
]
