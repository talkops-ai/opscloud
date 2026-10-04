"""Memory management and discovery across user and project scopes for opscloud."""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Any

from langgraph.store.base import BaseStore

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


class MemoryRegistry:
    """Discovers and manages memory files across user and project scopes."""

    _instance: MemoryRegistry | None = None
    _lock = threading.Lock()

    def __init__(self, store: BaseStore | None = None) -> None:
        """Initialize MemoryRegistry.

        Args:
            store: Optional LangGraph BaseStore instance.
        """
        self._store = store

    @classmethod
    def get_instance(cls, store: BaseStore | None = None) -> MemoryRegistry:
        """Retrieve the singleton instance of MemoryRegistry.

        Args:
            store: Optional store to configure if not already set.

        Returns:
            MemoryRegistry: The singleton registry instance.
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(store=store)
        elif store is not None and cls._instance._store is None:
            cls._instance._store = store
        return cls._instance

    def get_memory_paths_for_scope(
        self,
        scope: str = "auto",
        project_root: Path | None = None,
        agent_id: str | None = None,
    ) -> list[Path]:
        """Resolve physical memory paths for a given scope ('user', 'project', 'auto')."""
        paths: list[Path] = []
        root = project_root or Path.cwd()

        from opscloud.config import paths as app_paths

        # User scope
        if scope in ("user", "auto"):
            primary_md = app_paths.ensure_user_agent_md(agent_id or app_paths.DEFAULT_AGENT_NAME)
            paths.append(primary_md)
            paths.append(app_paths.DATA_DIR / "AGENTS.md")
            if agent_id and agent_id != app_paths.DEFAULT_AGENT_NAME:
                paths.append(app_paths.user_agent_md(agent_id))

        # Project scope
        if scope in ("project", "auto"):
            paths.append(app_paths.project_opscloud_dir(root) / "AGENTS.md")
            paths.append(root / ".opscloud" / "AGENTS.md")
            paths.append(root / "AGENTS.md")

        resolved_paths: list[Path] = []
        for p in paths:
            try:
                abs_p = p.expanduser().resolve()
                if abs_p not in resolved_paths and abs_p.is_file():
                    resolved_paths.append(abs_p)
            except Exception:
                pass

        return resolved_paths

    def get_all_memory_sources(
        self,
        project_root: Path | None = None,
        agent_id: str | None = None,
    ) -> list[str]:
        """Return a list of all existing AGENTS.md paths."""
        if isinstance(project_root, str) and agent_id is None:
            try:
                p = Path(project_root)
                if p.is_dir() or "/" in project_root or "\\" in project_root:
                    project_root = p
                else:
                    agent_id = project_root
                    project_root = None
            except Exception:
                pass
        paths = self.get_memory_paths_for_scope("auto", project_root=project_root, agent_id=agent_id)
        return [str(p) for p in paths]

    # ── Store-backed memory operations ─────────────────────────────────

    async def save_memory(self, thread_id: str, key: str, value: str) -> None:
        """Save a memory entry for a thread."""
        if self._store:
            await self._store.aput(
                namespace=("memory", thread_id),
                key=key,
                value={"content": value},
            )

    async def get_memory(self, thread_id: str, key: str) -> str | None:
        """Retrieve a memory entry."""
        if self._store:
            item = await self._store.aget(namespace=("memory", thread_id), key=key)
            if item and isinstance(item.value, dict):
                return item.value.get("content")
        return None

    async def list_memories(self, thread_id: str) -> list[dict[str, Any]]:
        """List all memories for a thread."""
        if self._store:
            items = await self._store.asearch(("memory", thread_id))
            return [
                {
                    "key": item.key,
                    "content": (item.value.get("content", "") if isinstance(item.value, dict) else str(item.value)),
                }
                for item in items
            ]
        return []
