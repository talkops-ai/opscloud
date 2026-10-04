"""Utilities for project root detection and project-specific configuration."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from opscloud.server import SERVER_ENV_PREFIX

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)


@dataclass(frozen=True)
class ProjectContext:
    """Explicit user/project path context for project-sensitive behavior."""

    user_cwd: Path
    project_root: Path | None = None

    def __post_init__(self) -> None:
        if not self.user_cwd.is_absolute():
            msg = f"user_cwd must be absolute, got {self.user_cwd!r}"
            raise ValueError(msg)
        if self.project_root is not None and not self.project_root.is_absolute():
            msg = f"project_root must be absolute, got {self.project_root!r}"
            raise ValueError(msg)

    @classmethod
    def from_user_cwd(cls, user_cwd: str | Path) -> ProjectContext:
        resolved_cwd = Path(user_cwd).expanduser().resolve()
        return cls(
            user_cwd=resolved_cwd,
            project_root=find_project_root(resolved_cwd),
        )

    def resolve_user_path(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self.user_cwd / candidate).resolve()

    def project_agent_md_paths(self) -> list[Path]:
        if self.project_root is None:
            return []
        return find_project_agent_md(self.project_root)

    def project_skills_dir(self) -> Path | None:
        if self.project_root is None:
            return None
        return self.project_root / ".opscloud" / "skills"

    def project_agents_dir(self) -> Path | None:
        if self.project_root is None:
            return None
        return self.project_root / ".opscloud" / "agents"

    def project_agent_skills_dir(self) -> Path | None:
        if self.project_root is None:
            return None
        return self.project_root / ".agents" / "skills"


def get_server_project_context(
    env: Mapping[str, str] | None = None,
) -> ProjectContext | None:
    """Read the server project context from environment transport data."""
    environment = os.environ if env is None else env
    raw_cwd = environment.get(f"{SERVER_ENV_PREFIX}CWD")
    if not raw_cwd:
        return None

    try:
        user_cwd = Path(raw_cwd).expanduser().resolve()
        raw_project_root = environment.get(f"{SERVER_ENV_PREFIX}PROJECT_ROOT")
        project_root = (
            Path(raw_project_root).expanduser().resolve()
            if raw_project_root
            else find_project_root(user_cwd)
        )
    except OSError:
        logger.warning(
            "Could not resolve server project context from CWD=%s",
            raw_cwd,
            exc_info=True,
        )
        return None

    return ProjectContext(user_cwd=user_cwd, project_root=project_root)


def find_project_root(start_path: str | Path | None = None) -> Path | None:
    """Find the project root using unified marker and git metadata detection."""
    from opscloud.config.paths import find_project_root as _find_paths_root

    return _find_paths_root(start_path)


def find_project_agent_md(project_root: Path) -> list[Path]:
    """Find project-specific AGENTS.md file(s)."""
    project_root_resolved = project_root.resolve()
    candidates = [
        project_root_resolved / ".opscloud" / "AGENTS.md",
        project_root_resolved / "AGENTS.md",
    ]
    paths: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError) as exc:
            logger.warning(
                "Skipping AGENTS.md candidate %s: %s",
                candidate,
                exc,
            )
            continue

        try:
            resolved.relative_to(project_root_resolved)
        except ValueError:
            logger.warning(
                "Skipping AGENTS.md symlink %s: target %s is outside the project root %s",
                candidate,
                resolved,
                project_root_resolved,
            )
            continue

        if candidate.absolute() == resolved:
            paths.append(candidate)
        else:
            paths.append(resolved)
    return paths
