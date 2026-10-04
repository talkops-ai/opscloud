"""Shared path-safety and size limits for read-only repository inspection.

Both the goal-criteria agent's ``_RepositoryToolBudgetMiddleware`` and the rubric
grader's read-only tools let an LLM sub-agent inspect working-directory files.
They must apply identical guarantees: reads stay confined to the repository
root, symlink escapes are rejected in sandboxes and local filesystems, and every
result is size bounded so a single tool call cannot blow the sub-agent's context
budget.

``RepositoryBounds`` centralizes that logic so the middleware and the grader tool
wrappers share one implementation.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import SandboxBackendProtocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deepagents.backends.protocol import BackendProtocol, FileInfo

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Budget constants — shared across criteria agents and rubric graders
# ---------------------------------------------------------------------------

REPOSITORY_TOOL_CALL_LIMIT = 25
"""Maximum number of repository-inspection tool calls per sub-agent run."""

REPOSITORY_READ_LINE_LIMIT = 120
"""Maximum lines a ``read_file`` call returns before truncation."""

REPOSITORY_READ_BYTE_LIMIT = 256_000
"""Maximum file size (bytes) that preflight allows for a ``read_file`` call."""

REPOSITORY_DIRECTORY_ENTRY_LIMIT = 200
"""Maximum directory entries preflight allows for an ``ls`` call."""

REPOSITORY_GLOB_MATCH_LIMIT = 200
"""Maximum match count for a ``glob`` result before truncation."""

REPOSITORY_GREP_MATCH_LIMIT = 100
"""Maximum match count for a ``grep`` result before truncation."""

REPOSITORY_TOOL_RESULT_LIMIT = 12_000
"""Character limit for any single repository tool result body."""

REPOSITORY_TOOL_NAMES = frozenset({"ls", "read_file", "glob", "grep"})
"""Tool names that are valid for read-only repository inspection."""

REPOSITORY_PATH_RESULT_PREFIX = "__OPSCLOUD_REPOSITORY_PATH__"
"""Marker emitted by sandbox containment probes."""

# ---------------------------------------------------------------------------
# Error messages
# ---------------------------------------------------------------------------

REPOSITORY_PATH_ERROR = "Repository path is unavailable."
REPOSITORY_UNAVAILABLE_ERROR = "Repository is temporarily unavailable; the path could not be verified."
REPOSITORY_SIZE_ERROR = "Repository file exceeds the size limit."
REPOSITORY_LISTING_ERROR = "Repository directory exceeds the listing limit."
REPOSITORY_READ_ONLY_ERROR = "Repository inspection is limited to read-only tools."

_BACKEND_ERRORS: tuple[type[BaseException], ...] = (
    NotImplementedError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


class RepositoryBounds:
    """Path-safety and size limits for read-only repository inspection tools."""

    def __init__(
        self,
        backend: BackendProtocol,
        *,
        root: str = "/",
        allowed_tools: Sequence[str] | None = None,
    ) -> None:
        """Initialize RepositoryBounds with backend and access boundaries.

        Args:
            backend: Storage or execution backend to guard.
            root: Root path constraint for repository inspection.
            allowed_tools: Sequence of tool names permitted for execution.
        """
        normalized = root.replace("\\", "/")
        path = PurePosixPath(normalized)
        if not normalized.startswith("/") or ".." in path.parts or "~" in root:
            msg = f"Repository root must be an absolute contained path: {root!r}"
            raise ValueError(msg)
        self._backend = backend
        self._root = str(path)
        self._allowed_tools = frozenset(allowed_tools) if allowed_tools is not None else REPOSITORY_TOOL_NAMES
        self._sandbox = backend if isinstance(backend, SandboxBackendProtocol) else None
        self._filesystem = backend if self._sandbox is None and isinstance(backend, FilesystemBackend) else None
        self._filesystem_root: Path | None = None
        if self._filesystem is not None:
            try:
                self._filesystem_root = self._resolve_filesystem_path(self._root)
            except _BACKEND_ERRORS:
                logger.warning(
                    "Could not resolve the local repository root; local repository paths will be unavailable",
                    exc_info=True,
                )

    @property
    def root(self) -> str:
        """Absolute path that bounds repository reads."""
        return self._root

    def safe_path(self, raw_path: str) -> bool:
        """Return whether an explicit repository path is absolute and contained."""
        path = PurePosixPath(raw_path.replace("\\", "/"))
        root = PurePosixPath(self._root)
        return (
            raw_path.startswith("/")
            and ".." not in path.parts
            and "~" not in raw_path
            and (root == PurePosixPath("/") or path == root or root in path.parents)
        )

    @staticmethod
    def safe_pattern(pattern: str) -> bool:
        """Return whether a relative or absolute glob pattern cannot traverse."""
        path = PurePosixPath(pattern.replace("\\", "/"))
        return ".." not in path.parts and "~" not in pattern

    def _resolve_filesystem_path(self, raw_path: str) -> Path:
        if self._filesystem is None:
            msg = "Local filesystem backend is unavailable."
            raise RuntimeError(msg)
        if self._filesystem.virtual_mode:
            return (self._filesystem.cwd / raw_path.lstrip("/")).resolve(strict=False)
        return Path(raw_path).resolve(strict=False)

    def _filesystem_contains(self, raw_path: str) -> bool:
        if self._filesystem is None:
            return True
        if self._filesystem_root is None:
            return False
        try:
            resolved = self._resolve_filesystem_path(raw_path)
        except _BACKEND_ERRORS:
            logger.warning(
                "Local repository containment check failed; treating the path as unavailable",
                exc_info=True,
            )
            return False
        return resolved == self._filesystem_root or self._filesystem_root in resolved.parents

    def _containment_command(self, raw_path: str) -> str:
        payload = base64.b64encode(json.dumps([self._root, raw_path]).encode()).decode()
        return (
            'python3 -c "import base64,json,os;'
            f"values=json.loads(base64.b64decode('{payload}'));"
            "root=os.path.realpath(values[0]);path=os.path.realpath(values[1]);"
            "contained=os.path.commonpath([root,path])==root;"
            f"print('{REPOSITORY_PATH_RESULT_PREFIX}'+str(int(contained)))\""
        )

    def sandbox_contains(self, raw_path: str) -> bool:
        """Check synchronously whether a target path is contained within the root boundary.

        Args:
            raw_path: Filesystem path to test.

        Returns:
            True if contained within the root, False otherwise.
        """
        if self._sandbox is None:
            return self._filesystem_contains(raw_path)
        try:
            result = self._sandbox.execute(self._containment_command(raw_path))
        except _BACKEND_ERRORS:
            logger.warning(
                "Repository containment check failed; treating the path as unavailable",
                exc_info=True,
            )
            return False
        return result.exit_code in {None, 0} and any(
            line == f"{REPOSITORY_PATH_RESULT_PREFIX}1" for line in result.output.splitlines()
        )

    async def asandbox_contains(self, raw_path: str) -> bool:
        """Check asynchronously whether a target path is contained within the root boundary.

        Args:
            raw_path: Filesystem path to test.

        Returns:
            True if contained within the root, False otherwise.
        """
        if self._sandbox is None:
            if self._filesystem is None:
                return True
            return await asyncio.to_thread(self._filesystem_contains, raw_path)
        try:
            result = await self._sandbox.aexecute(self._containment_command(raw_path))
        except _BACKEND_ERRORS:
            logger.warning(
                "Repository containment check failed; treating the path as unavailable",
                exc_info=True,
            )
            return False
        return result.exit_code in {None, 0} and any(
            line == f"{REPOSITORY_PATH_RESULT_PREFIX}1" for line in result.output.splitlines()
        )

    @staticmethod
    def entry_size(
        entries: Sequence[FileInfo] | None,
        normalized_path: str,
    ) -> int | None:
        """Look up file size for a normalized path in directory listing entries.

        Args:
            entries: Sequence of FileInfo entries.
            normalized_path: Normalized POSIX path string.

        Returns:
            File size in bytes if found, or None.
        """
        for item in entries or []:
            raw = item.get("path") if isinstance(item, dict) else None
            if not isinstance(raw, str):
                continue
            if str(PurePosixPath(raw)) == normalized_path:
                size = item.get("size")
                return size if isinstance(size, int) else None
        return None

    def _validate_search_paths(
        self,
        name: str,
        args: dict[str, Any],
    ) -> str | None:
        path = args.get("path")
        if path is not None and (not isinstance(path, str) or not self.safe_path(path)):
            return REPOSITORY_PATH_ERROR

        patterns = [args.get("pattern")] if name == "glob" else [args.get("glob")]
        if any(
            pattern is not None and (not isinstance(pattern, str) or not self.safe_pattern(pattern))
            for pattern in patterns
        ):
            return REPOSITORY_PATH_ERROR
        return None

    def preflight(self, name: str, args: dict[str, Any]) -> str | None:
        """Perform synchronous safety, path traversal, and size preflight checks.

        Args:
            name: Tool name being invoked.
            args: Tool execution argument dictionary.

        Returns:
            Error message string if validation failed, or None if passed.
        """
        if name not in self._allowed_tools:
            return REPOSITORY_READ_ONLY_ERROR
        if name == "execute":
            return None
        if name in {"glob", "grep"}:
            error = self._validate_search_paths(name, args)
            if error is not None:
                return error
            raw_path = args.get("path")
            if raw_path is None:
                raw_path = self._root
            if not isinstance(raw_path, str) or not self.sandbox_contains(raw_path):
                return REPOSITORY_PATH_ERROR
            return None

        key = "file_path" if name == "read_file" else "path"
        raw_path = args.get(key)
        if not isinstance(raw_path, str):
            return None

        path = PurePosixPath(raw_path.replace("\\", "/"))
        if not self.safe_path(raw_path):
            return REPOSITORY_PATH_ERROR
        if not self.sandbox_contains(raw_path):
            return REPOSITORY_PATH_ERROR

        try:
            result = self._backend.ls(raw_path if name == "ls" else str(path.parent))
        except _BACKEND_ERRORS:
            logger.warning(
                "Repository preflight failed for tool %r; treating the repository as temporarily unavailable",
                name,
                exc_info=True,
            )
            return REPOSITORY_UNAVAILABLE_ERROR
        if result.error is not None:
            return REPOSITORY_PATH_ERROR
        if name == "ls":
            if len(result.entries or []) > REPOSITORY_DIRECTORY_ENTRY_LIMIT:
                return REPOSITORY_LISTING_ERROR
        else:  # read_file
            size = self.entry_size(result.entries, str(path))
            if size is not None and size > REPOSITORY_READ_BYTE_LIMIT:
                return REPOSITORY_SIZE_ERROR
        return None

    async def apreflight(self, name: str, args: dict[str, Any]) -> str | None:
        """Perform asynchronous safety, path traversal, and size preflight checks.

        Args:
            name: Tool name being invoked.
            args: Tool execution argument dictionary.

        Returns:
            Error message string if validation failed, or None if passed.
        """
        if name not in self._allowed_tools:
            return REPOSITORY_READ_ONLY_ERROR
        if name == "execute":
            return None
        if name in {"glob", "grep"}:
            error = self._validate_search_paths(name, args)
            if error is not None:
                return error
            raw_path = args.get("path")
            if raw_path is None:
                raw_path = self._root
            if not isinstance(raw_path, str) or not await self.asandbox_contains(raw_path):
                return REPOSITORY_PATH_ERROR
            return None

        key = "file_path" if name == "read_file" else "path"
        raw_path = args.get(key)
        if not isinstance(raw_path, str):
            return None

        path = PurePosixPath(raw_path.replace("\\", "/"))
        if not self.safe_path(raw_path):
            return REPOSITORY_PATH_ERROR
        if not await self.asandbox_contains(raw_path):
            return REPOSITORY_PATH_ERROR

        try:
            result = await self._backend.als(raw_path if name == "ls" else str(path.parent))
        except _BACKEND_ERRORS:
            logger.warning(
                "Repository preflight failed for tool %r; treating the repository as temporarily unavailable",
                name,
                exc_info=True,
            )
            return REPOSITORY_UNAVAILABLE_ERROR
        if result.error is not None:
            return REPOSITORY_PATH_ERROR
        if name == "ls":
            if len(result.entries or []) > REPOSITORY_DIRECTORY_ENTRY_LIMIT:
                return REPOSITORY_LISTING_ERROR
        elif name == "read_file":
            size = self.entry_size(result.entries, str(path))
            if size is not None and size > REPOSITORY_READ_BYTE_LIMIT:
                return REPOSITORY_SIZE_ERROR
        return None

    def clamp_args(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Clamp tool arguments to safe limits (line limits, match count, root path).

        Args:
            name: Tool name being invoked.
            args: Raw tool arguments.

        Returns:
            New argument dictionary with clamped values.
        """
        clamped = dict(args)
        if name == "read_file":
            limit = clamped.get("limit", REPOSITORY_READ_LINE_LIMIT)
            if not isinstance(limit, int) or isinstance(limit, bool):
                limit = REPOSITORY_READ_LINE_LIMIT
            clamped["limit"] = max(1, min(limit, REPOSITORY_READ_LINE_LIMIT))
        elif name in {"glob", "grep"} and clamped.get("path") is None:
            clamped["path"] = self._root
        if name == "grep":
            count = clamped.get("max_count", REPOSITORY_GREP_MATCH_LIMIT)
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                count = REPOSITORY_GREP_MATCH_LIMIT
            clamped["max_count"] = min(count, REPOSITORY_GREP_MATCH_LIMIT)
        return clamped

    @staticmethod
    def bounded_glob_content(content: str) -> str:
        """Truncate glob output list to the maximum match limit.

        Args:
            content: Raw tool output string containing python list literal.

        Returns:
            Truncated string representation if over the limit, or original content.
        """
        body, separator, notes = content.partition("\n\n")
        try:
            paths = ast.literal_eval(body)
        except (SyntaxError, ValueError):
            return content
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            return content
        if len(paths) <= REPOSITORY_GLOB_MATCH_LIMIT:
            return content
        marker = f"[Glob results limited to the first {REPOSITORY_GLOB_MATCH_LIMIT} matches.]"
        bounded = str(paths[:REPOSITORY_GLOB_MATCH_LIMIT])
        suffix = f"\n\n{notes}" if separator and notes else ""
        return f"{bounded}\n\n{marker}{suffix}"

    def bound_text(self, name: str, content: str) -> str:
        """Enforce maximum byte length limits on tool result output.

        Args:
            name: Tool name invoked.
            content: Raw tool output text.

        Returns:
            Bounded text with truncation indicator if necessary.
        """
        if name == "glob":
            content = self.bounded_glob_content(content)
        if len(content) > REPOSITORY_TOOL_RESULT_LIMIT:
            marker = "\n[Repository tool result shortened to the context limit.]"
            content = content[: REPOSITORY_TOOL_RESULT_LIMIT - len(marker)] + marker
        return content


__all__ = [
    "REPOSITORY_DIRECTORY_ENTRY_LIMIT",
    "REPOSITORY_GLOB_MATCH_LIMIT",
    "REPOSITORY_GREP_MATCH_LIMIT",
    "REPOSITORY_LISTING_ERROR",
    "REPOSITORY_PATH_ERROR",
    "REPOSITORY_READ_BYTE_LIMIT",
    "REPOSITORY_READ_LINE_LIMIT",
    "REPOSITORY_READ_ONLY_ERROR",
    "REPOSITORY_SIZE_ERROR",
    "REPOSITORY_TOOL_CALL_LIMIT",
    "REPOSITORY_TOOL_NAMES",
    "REPOSITORY_TOOL_RESULT_LIMIT",
    "REPOSITORY_UNAVAILABLE_ERROR",
    "RepositoryBounds",
]
