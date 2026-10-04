"""Memory storage — structured precedence-based memory management for opscloud.

Precedence for memory locations:
  1. **Project-scoped** (highest priority): ``{project_root}/.opscloud/memory/``
  2. **User-scoped** (fallback): ``~/.opscloud/memory/``

Each memory entry is a plain Markdown file. Memories are loaded at session start
and injected into the system prompt or queried via ``/memory`` commands.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
import time
from typing import Sequence

from opscloud.config.paths import DATA_DIR
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_MEMORY_LINE_COUNT = 250
"""Hard cap on memory lines injected into context."""

_MAX_MEMORY_BYTES = 30_000
"""Hard cap on total memory bytes injected into context."""


@dataclass(frozen=True)
class MemoryEntry:
    """A single persisted memory entry."""

    key: str
    content: str
    source: str  # "project" | "user"
    path: Path
    modified_at: float


class MemoryStore:
    """CRUD operations for Markdown-file-based persistent memories.

    Precedence rules:
      - Writes go to the project store if a project root is detected,
        otherwise to the user store.
      - Reads merge both stores, with project entries overriding user
        entries of the same key.
    """

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        user_home: Path | None = None,
    ) -> None:
        self._project_dir = (
            (project_root / ".opscloud" / "memory") if project_root else None
        )
        base_user = (user_home / ".opscloud") if user_home else DATA_DIR
        self._user_dir = base_user / "memory"

    # ── Write ────────────────────────────────────────────

    def save(self, key: str, content: str, *, scope: str = "auto") -> Path:
        """Persist a memory entry.

        Args:
            key: Filename stem (e.g. ``"prefer-terraform-fmt"``).
            content: Markdown content to store.
            scope: ``"project"``, ``"user"``, or ``"auto"`` (default).
                Auto writes to project if available, else user.

        Returns:
            Path: Path to the written file.
        """
        target_dir = self._resolve_write_dir(scope)
        target_dir.mkdir(parents=True, exist_ok=True)

        safe_key = _sanitize_key(key)
        target = target_dir / f"{safe_key}.md"
        target.write_text(content, encoding="utf-8")
        return target

    def delete(self, key: str) -> bool:
        """Delete a memory by key from both scopes.

        Args:
            key: Filename stem to delete.

        Returns:
            bool: True if at least one file was deleted, False otherwise.
        """
        safe_key = _sanitize_key(key)
        deleted = False
        for directory in self._all_dirs():
            path = directory / f"{safe_key}.md"
            if path.is_file():
                try:
                    path.unlink()
                    deleted = True
                except OSError as exc:
                    logger.warning("Failed to delete memory file %s: %s", path, exc)
        return deleted

    # ── Read ─────────────────────────────────────────────

    def list_all(self) -> list[MemoryEntry]:
        """List all memory entries, project overriding user for same key."""
        entries: dict[str, MemoryEntry] = {}

        # User first (lower priority)
        for entry in self._scan_dir(self._user_dir, source="user"):
            entries[entry.key] = entry

        # Project second (higher priority, overwrites same key)
        if self._project_dir:
            for entry in self._scan_dir(self._project_dir, source="project"):
                entries[entry.key] = entry

        return sorted(entries.values(), key=lambda e: e.key)

    def get(self, key: str) -> MemoryEntry | None:
        """Get a single memory by key (project takes precedence)."""
        safe_key = _sanitize_key(key)
        # Check project first
        if self._project_dir:
            path = self._project_dir / f"{safe_key}.md"
            if path.is_file():
                return _read_entry(path, source="project")
        # Then user
        path = self._user_dir / f"{safe_key}.md"
        if path.is_file():
            return _read_entry(path, source="user")
        return None

    def load_context(self) -> str:
        """Build the system-prompt memory injection string.

        Returns:
            str: Concatenated memory content capped at line/byte limits.
        """
        entries = self.list_all()
        if not entries:
            return ""

        parts: list[str] = []
        total_lines = 0
        total_bytes = 0

        for entry in entries:
            lines = entry.content.splitlines()
            if total_lines + len(lines) > _MAX_MEMORY_LINE_COUNT:
                break
            encoded = entry.content.encode("utf-8")
            if total_bytes + len(encoded) > _MAX_MEMORY_BYTES:
                break
            parts.append(f"## Memory: {entry.key}\n{entry.content}")
            total_lines += len(lines)
            total_bytes += len(encoded)

        if not parts:
            return ""

        return (
            "# Persisted Memories\n"
            "The following are learnings extracted from previous conversations.\n\n"
            + "\n\n".join(parts)
        )

    # ── Private ──────────────────────────────────────────

    def _resolve_write_dir(self, scope: str) -> Path:
        if scope == "project" and self._project_dir:
            return self._project_dir
        if scope == "user":
            return self._user_dir
        if self._project_dir:
            return self._project_dir
        return self._user_dir

    def _all_dirs(self) -> list[Path]:
        dirs = [self._user_dir]
        if self._project_dir:
            dirs.append(self._project_dir)
        return dirs

    @staticmethod
    def _scan_dir(directory: Path | None, *, source: str) -> list[MemoryEntry]:
        if directory is None or not directory.is_dir():
            return []
        entries: list[MemoryEntry] = []
        for path in sorted(directory.glob("*.md")):
            entry = _read_entry(path, source=source)
            if entry is not None:
                entries.append(entry)
        return entries


def _read_entry(path: Path, *, source: str) -> MemoryEntry | None:
    try:
        content = path.read_text(encoding="utf-8")
        return MemoryEntry(
            key=path.stem,
            content=content,
            source=source,
            path=path,
            modified_at=path.stat().st_mtime,
        )
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read memory file %s: %s", path, exc)
        return None


def _sanitize_key(key: str) -> str:
    """Sanitize a memory key for use as a filename stem."""
    key = key.strip().lower()
    key = re.sub(r"[^\w\-]", "-", key)
    key = re.sub(r"-{2,}", "-", key)
    key = key.strip("-")
    return key[:64] or "memory"
