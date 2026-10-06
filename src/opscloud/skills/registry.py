"""Skill registry to discover, validate, and index opscloud skills."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import threading
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from opscloud.skills.sources import CodeSkillSource

import yaml

from opscloud.config import paths
from opscloud.config.settings import settings
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


class SkillMetadata(TypedDict):
    """Metadata for a discovered skill including name, source, and path."""

    name: str
    description: str
    domain: str
    path: str
    virtual_path: str
    frontmatter: dict[str, Any]
    system_prompt: str


SkillSourceTuple = tuple[str, str, str | None] | tuple[str, str] | tuple[str, ...]
"""Skill source tuple representing (path, label) or (path, label, namespace)."""


@dataclass(frozen=True)
class SkillSource:

    """A discovered skill."""

    name: str
    """Unique skill name (directory name)."""

    path: Path
    """Path to the skill directory."""

    tier: str = "built-in"
    """Discovery tier: 'built-in', 'user', 'project', 'plugin', 'subagent'."""

    description: str = ""
    """Description from SKILL.md frontmatter."""

    domain: str = ""
    """Domain category."""

    tags: tuple[str, ...] = ()
    """Tags from SKILL.md frontmatter."""

    enabled: bool = True
    """Whether the skill is active."""


def _parse_skill_frontmatter(skill_md: Path) -> tuple[str, tuple[str, ...]]:
    """Extract description and tags from SKILL.md."""
    try:
        content = skill_md.read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if match:
            fm = yaml.safe_load(match.group(1))
            if isinstance(fm, dict):
                desc = fm.get("description", "")
                tags = tuple(fm.get("tags", []))
                return str(desc), tags
    except Exception as exc:
        logger.debug("Failed parsing frontmatter from %s: %s", skill_md, exc)
    return "", ()


class SkillRegistry:
    """In-memory skill registry supporting discovery, registration, and middleware indexing."""

    _instance: SkillRegistry | None = None
    _lock = threading.Lock()

    def __init__(self, store: Any = None) -> None:
        self._skills: dict[str, SkillMetadata] = {}
        self._sources: dict[str, SkillSource] = {}
        self._store = store

    @classmethod
    def get_instance(cls, store: Any = None) -> SkillRegistry:
        """Retrieve the singleton instance of SkillRegistry."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(store=store)
        elif store is not None:
            cls._instance._store = store
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset in-memory skill caches."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance._skills.clear()
                cls._instance._sources.clear()

    def clear(self) -> None:
        """Clear registered skills in-memory."""
        self._skills.clear()
        self._sources.clear()

    def register(self, skill: SkillSource) -> None:
        """Register a discovered skill."""
        self._sources[skill.name] = skill
        self._skills[skill.name] = SkillMetadata(
            name=skill.name,
            description=skill.description,
            domain=skill.domain,
            path=str(skill.path),
            virtual_path=f"/skills/{skill.name}/SKILL.md",
            frontmatter={
                "description": skill.description,
                "tags": list(skill.tags),
                "domain": skill.domain,
            },
            system_prompt="",
        )

    def get(self, name: str) -> SkillSource | None:
        """Retrieve a skill by name."""
        return self._sources.get(name)

    def get_skill(self, name: str) -> SkillMetadata | None:
        """Retrieve SkillMetadata by name."""
        return self._skills.get(name)

    def list_skills(self) -> list[SkillSource]:
        """List all registered SkillSource entries."""
        return list(self._sources.values())

    def get_all_skills(self) -> list[SkillMetadata]:
        """List all registered SkillMetadata entries."""
        return list(self._skills.values())

    def discover(
        self,
        user_dir: Path | None = None,
        project_dir: Path | None = None,
        built_in_dir: Path | None = None,
    ) -> int:
        """Discover skills from provided directories and register them."""
        count = 0
        dirs_to_scan: list[tuple[Path, str]] = []

        if built_in_dir and built_in_dir.is_dir():
            dirs_to_scan.append((built_in_dir, "built-in"))
        if user_dir and user_dir.is_dir():
            dirs_to_scan.append((user_dir, "user"))
        if project_dir and project_dir.is_dir():
            dirs_to_scan.append((project_dir, "project"))

        for base_dir, tier in dirs_to_scan:
            try:
                for item in sorted(base_dir.iterdir()):
                    if not item.is_dir() or item.name.startswith("."):
                        continue
                    skill_md = item / "SKILL.md"
                    if skill_md.is_file():
                        desc, tags = _parse_skill_frontmatter(skill_md)
                        skill = SkillSource(
                            name=item.name,
                            path=item,
                            tier=tier,
                            description=desc,
                            domain="",
                            tags=tags,
                        )
                        self.register(skill)
                        count += 1
            except Exception as exc:
                logger.warning("Error discovering skills from %s: %s", base_dir, exc)

        return count

    def discover_skills(
        self,
        project_root: Path | None = None,
        force: bool = False,
    ) -> None:
        """Discover skills using the unified skill loader."""
        if self._skills and not force:
            return

        from opscloud.skills.load import list_skills

        effective_root = project_root or settings.project_root
        discovered = list_skills(
            project_root=effective_root,
            include_plugins=True,
            include_subagents=False,
        )

        for s in discovered:
            name = s.get("name", "")
            if not name:
                continue
            path_str = s.get("path", "")
            skill_dir = Path(path_str).parent if Path(path_str).is_file() else Path(path_str)
            fm = s.get("frontmatter", {})
            tags = tuple(s.get("tags") or fm.get("tags") or ())
            skill_source = SkillSource(
                name=name,
                path=skill_dir,
                tier=s.get("source", "user"),
                description=s.get("description", ""),
                domain=s.get("domain", ""),
                tags=tags,
                enabled=s.get("enabled", True),
            )
            self.register(skill_source)

    def get_sources_for_middleware(
        self,
        project_root: Any = None,
        include_subagent_skills: bool = False,
        subagents: Any = None,
    ) -> list[CodeSkillSource]:
        """Return sources ordered by precedence for PluginSkillsMiddleware.

        When ``include_subagent_skills`` is False (default), agent-bound plugin skills
        are excluded so that the main agent maintains context isolation. When True,
        all skills across agents, agent plugins, and subagents are returned.
        """
        from opscloud.skills.sources import get_skill_sources

        effective_root = Path(project_root) if project_root else settings.project_root
        return get_skill_sources(
            project_root=effective_root,
            include_subagent_skills=include_subagent_skills,
            subagents=subagents,
            store=self._store,
        )


def get_skill_registry(store: Any = None) -> SkillRegistry:
    """Retrieve the singleton SkillRegistry instance."""
    return SkillRegistry.get_instance(store=store)


__all__ = [
    "SkillMetadata",
    "SkillRegistry",
    "SkillSource",
    "SkillSourceTuple",
    "_parse_skill_frontmatter",
    "get_skill_registry",
]

