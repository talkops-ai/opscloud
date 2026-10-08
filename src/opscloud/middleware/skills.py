"""Skills middleware adapter supporting namespaced plugin skills."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import fnmatch
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

from deepagents.backends.protocol import FileInfo, LsResult
from deepagents.backends.utils import to_posix_path
from deepagents.middleware import skills as sdk_skills
from deepagents.middleware.skills import SkillsMiddleware

from opscloud.middleware.registry import register_middleware
from opscloud.plugins.adapters.skills import (
    SkillNamespace,
    namespaced_skill_name,
)

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime
    from opscloud.skills.registry import SkillSourceTuple
    from opscloud.skills.sources import CodeSkillSource

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_PLUGIN_SKILL_SOURCE_LENGTH = 3
_SKILL_FILE = "SKILL.md"


def _entries(ls_result: object) -> list[FileInfo]:
    if isinstance(ls_result, LsResult):
        return list(ls_result.entries or [])
    if isinstance(ls_result, list):
        return cast(list[FileInfo], ls_result)
    return []


def _child_dirs(entries: list[FileInfo], root: str) -> list[tuple[str, str]]:
    root_posix = PurePosixPath(to_posix_path(root))
    dirs: list[tuple[str, str]] = []
    for entry in entries:
        if not entry.get("is_dir"):
            continue
        path = entry["path"]
        name = PurePosixPath(to_posix_path(path)).name
        if PurePosixPath(to_posix_path(path)) == root_posix:
            continue
        dirs.append((name, path))
    return dirs


def _has_skill_file(entries: list[FileInfo], root: str) -> bool:
    root_posix = PurePosixPath(to_posix_path(root))
    for entry in entries:
        path = PurePosixPath(to_posix_path(entry["path"]))
        if path.name == _SKILL_FILE and path.parent == root_posix:
            return True
    return False


def _skill_md_path(skill_dir: str) -> str:
    return str(PurePosixPath(to_posix_path(skill_dir)) / _SKILL_FILE)


def _namespace_skill(
    skill: sdk_skills.SkillMetadata,
    namespace: SkillNamespace,
    subfolders: tuple[str, ...],
) -> sdk_skills.SkillMetadata:
    return cast(
        sdk_skills.SkillMetadata,
        {
            **skill,
            "name": namespaced_skill_name(namespace, skill["name"], subfolders),
        },
    )


def discover_skill_dirs(
    backend: BackendProtocol,
    source_path: str,
) -> list[tuple[str, tuple[str, ...]]]:
    """Recursively search for directories containing a SKILL.md definition under source_path.

    Args:
        backend: Storage backend providing directory listing capabilities.
        source_path: Root filesystem path to search.

    Returns:
        List of tuples mapping resolved skill directory path to relative path segments.
    """
    found: list[tuple[str, tuple[str, ...]]] = []
    source_root = Path(source_path).resolve()
    visited: set[Path] = set()
    stack: list[tuple[str, tuple[str, ...]]] = [(str(source_root), ())]
    while stack:
        current, path_segments = stack.pop()
        try:
            resolved = Path(current).resolve()
        except (OSError, RuntimeError):
            logger.warning("Could not resolve plugin skill directory %s", current)
            continue
        if not resolved.is_relative_to(source_root) or resolved in visited:
            continue
        visited.add(resolved)
        resolved_path = str(resolved)
        entries = _entries(backend.ls(resolved_path))
        if _has_skill_file(entries, resolved_path):
            found.append((resolved_path, path_segments[:-1]))
            continue
        for name, path in _child_dirs(entries, resolved_path):
            stack.append((path, (*path_segments, name)))
    return found


def load_namespaced_skills(
    backend: BackendProtocol,
    source_path: str,
    namespace: SkillNamespace,
) -> list[sdk_skills.SkillMetadata]:
    """Discover and parse all skill definitions within a source root under a given namespace.

    Args:
        backend: Storage backend providing file reading and listing.
        source_path: Root directory path containing skills.
        namespace: Skill namespace identifier for prefixing skill names.

    Returns:
        List of loaded and namespaced SkillMetadata objects.
    """
    skill_dirs = discover_skill_dirs(backend, source_path)
    if not skill_dirs:
        return []
    paths = [_skill_md_path(skill_dir) for skill_dir, _ in skill_dirs]
    responses = backend.download_files(paths)
    skills: list[sdk_skills.SkillMetadata] = []
    for (skill_dir, segments), path, response in zip(skill_dirs, paths, responses, strict=True):
        skill = sdk_skills._skill_metadata_from_response(response, skill_dir, path)
        if skill is not None:
            skills.append(_namespace_skill(skill, namespace, segments))
    return skills


CRITERIA_SKILLS_SYSTEM_PROMPT = """## Skills System & Domain Capabilities

OpsCloud operates with a skills library providing domain knowledge and specialized cloud operations procedures. Use these capabilities to understand what criteria and verification targets are achievable:
{skills_locations}{skills_load_warnings}
**Available Skills:**

{skills_list}"""


@register_middleware(name="skills")
class PluginSkillsMiddleware(SkillsMiddleware):
    """Load namespaced plugin skills with optional whitelist filtering for subagent scoping."""

    def __init__(
        self,
        *,
        backend: BackendProtocol | None = None,
        sources: Sequence[CodeSkillSource | SkillSourceTuple | tuple[str, ...]] | None = None,
        skill_sources: Sequence[Any] | None = None,
        system_prompt: str | None = None,
        allowed_skills: Sequence[str] | None = None,
        include_subagent_skills: bool = False,
        subagents: Sequence[Any] | None = None,
        planning_mode: bool = False,
        project_root: Path | str | None = None,
    ) -> None:
        """Initialize PluginSkillsMiddleware with backend, sources, and filtering options.

        Args:
            backend: Storage backend instance.
            sources: Custom skill root sources.
            skill_sources: Structured skill source definitions.
            system_prompt: Custom system prompt template for skill injection.
            allowed_skills: Optional whitelist of skill names permitted for execution.
            include_subagent_skills: Whether to inherit subagent-specific skills.
            subagents: Sequence of subagents whose skills to discover.
            planning_mode: Whether planning mode is active.
            project_root: Optional project root path for skill discovery.
        """
        self._planning_mode = planning_mode
        self._project_root = Path(project_root) if project_root else None
        if system_prompt is None:
            system_prompt = CRITERIA_SKILLS_SYSTEM_PROMPT if planning_mode else sdk_skills.SKILLS_SYSTEM_PROMPT

        if backend is None:
            from deepagents.backends.filesystem import FilesystemBackend

            backend = FilesystemBackend(virtual_mode=False)

        self._dynamic_sources = sources is None and skill_sources is None
        self._include_subagent_skills = include_subagent_skills
        self._subagents = subagents
        if sources is None:
            if skill_sources is not None:
                sources = [(str(getattr(s, "path", s)), getattr(s, "name", str(s))) for s in skill_sources]
            else:
                from opscloud.skills.sources import get_skill_sources

                sources = get_skill_sources(
                    project_root=self._project_root,
                    include_subagent_skills=include_subagent_skills,
                    subagents=subagents,
                )

        sdk_sources = [(source[0], source[1]) for source in sources]
        super().__init__(
            backend=backend,
            sources=sdk_sources,
            system_prompt=system_prompt,
        )
        self._namespaces = tuple(
            source[2] if len(source) == _PLUGIN_SKILL_SOURCE_LENGTH else None for source in sources
        )
        self._allowed_skills = tuple(allowed_skills) if allowed_skills is not None else None
        self._cached_live_skills: list[sdk_skills.SkillMetadata] | None = None

    def _format_skills_locations(self) -> str:
        """Format skills locations for display in system prompt."""
        if self._planning_mode:
            return ""
        return super()._format_skills_locations()

    def _format_skills_load_warnings(self, errors: list[str]) -> str:
        """Format skills load warnings for display in system prompt."""
        if self._planning_mode:
            return ""
        return super()._format_skills_load_warnings(errors)

    def _format_skills_list(self, skills: list[sdk_skills.SkillMetadata]) -> str:
        """Format skills metadata cleanly when in planning mode without filesystem paths."""
        if self._planning_mode:
            if not skills:
                return "(No specialized skills currently loaded.)"
            lines: list[str] = []
            for skill in skills:
                name = skill.get("name", "")
                desc = skill.get("description", "").strip()
                lines.append(f"- **{name}**: {desc}")
            return "\n".join(lines)
        return super()._format_skills_list(skills)

    def _is_skill_allowed(self, skill_name: str) -> bool:
        if self._allowed_skills is None:
            return True
        for pattern in self._allowed_skills:
            if fnmatch.fnmatch(skill_name, pattern) or fnmatch.fnmatch(skill_name.lower(), pattern.lower()):
                return True
            if ":" in pattern:
                prefix, base_pat = pattern.split(":", 1)
                if base_pat == "*" or fnmatch.fnmatch(skill_name, base_pat) or fnmatch.fnmatch(skill_name.lower(), base_pat.lower()):
                    return True
        return False

    def _get_live_skills(self) -> tuple[list[sdk_skills.SkillMetadata], list[str]]:
        if self._dynamic_sources:
            from opscloud.skills.sources import get_skill_sources

            live_sources = get_skill_sources(
                project_root=self._project_root,
                include_subagent_skills=self._include_subagent_skills,
                subagents=self._subagents,
            )
            self.sources = [s[0] for s in live_sources]
            self.source_labels = [s[1] for s in live_sources]
            self._namespaces = tuple(s[2] if len(s) == _PLUGIN_SKILL_SOURCE_LENGTH else None for s in live_sources)

        backend = self._backend
        all_skills: dict[str, sdk_skills.SkillMetadata] = {}
        errors: list[str] = []

        active_sources: list[tuple[str, str, str | None]] = []
        for source_path, source_label, namespace in zip(
            self.sources, self.source_labels, self._namespaces, strict=True
        ):
            if namespace is None:
                source_skills, source_error = sdk_skills._list_skills_with_errors(backend, source_path)
                if source_error is not None:
                    errors.append(source_error)
            else:
                source_skills = load_namespaced_skills(backend, source_path, namespace)

            source_has_skills = False
            for skill in source_skills:
                skill_name = skill["name"]
                if self._is_skill_allowed(skill_name):
                    all_skills[skill_name] = skill
                    source_has_skills = True

            if source_has_skills:
                active_sources.append((source_path, source_label, namespace))

        if active_sources:
            self.sources = [s[0] for s in active_sources]
            self.source_labels = [s[1] for s in active_sources]
            self._namespaces = tuple(s[2] for s in active_sources)

        return list(all_skills.values()), errors

    def before_agent(
        self,
        state: sdk_skills.SkillsState,
        runtime: Runtime,
        config: RunnableConfig,
    ) -> sdk_skills.SkillsStateUpdate | None:
        """Discover live skills and prepare state updates before agent execution.

        Args:
            state: Current agent state.
            runtime: Active execution runtime.
            config: Runnable configuration for the invocation.

        Returns:
            SkillsStateUpdate if changes were detected, or None.
        """
        live_skills, errors = self._get_live_skills()
        self._cached_live_skills = live_skills
        current_skills = state.get("skills_metadata") or []
        current_names = [s["name"] for s in current_skills]
        live_names = [s["name"] for s in live_skills]

        if current_names == live_names and state.get("skills_metadata") is not None:
            return None

        update = sdk_skills.SkillsStateUpdate(skills_metadata=live_skills)
        if errors:
            logger.warning("Skills load errors: %s", errors)
            update["skills_load_errors"] = errors
        return update

    async def abefore_agent(
        self,
        state: sdk_skills.SkillsState,
        runtime: Runtime,
        config: RunnableConfig,
    ) -> sdk_skills.SkillsStateUpdate | None:
        """Asynchronously discover live skills and prepare state updates before agent execution.

        Args:
            state: Current agent state.
            runtime: Active execution runtime.
            config: Runnable configuration for the invocation.

        Returns:
            SkillsStateUpdate if changes were detected, or None.
        """
        return await asyncio.to_thread(self.before_agent, state, runtime, config)

    def modify_request(self, request: Any) -> Any:
        """Filter out stale skill metadata before formatting the system prompt.

        Args:
            request: Model request to modify.

        Returns:
            Modified request with pruned skill metadata.
        """
        live_skills = getattr(self, "_cached_live_skills", None)
        if live_skills is None:
            if hasattr(request, "state") and isinstance(request.state, dict) and request.state.get("skills_metadata"):
                live_skills = request.state["skills_metadata"]
            else:
                live_skills, _ = self._get_live_skills()
            self._cached_live_skills = live_skills

        live_names = {s["name"] for s in live_skills}

        if hasattr(request, "state") and isinstance(request.state, dict):
            # Prune any stale or uninstalled skills from request state before formatting system prompt
            existing = request.state.get("skills_metadata") or []
            request.state["skills_metadata"] = [s for s in existing if s.get("name") in live_names]
            if not request.state["skills_metadata"]:
                request.state["skills_metadata"] = live_skills

        return super().modify_request(request)


__all__ = [
    "PluginSkillsMiddleware",
    "discover_skill_dirs",
    "load_namespaced_skills",
]
