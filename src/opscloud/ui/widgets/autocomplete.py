"""Autocomplete system for @ mentions and / commands.

This is a custom implementation that handles trigger-based completion
for slash commands (/) and file mentions (@).
"""

from __future__ import annotations

import asyncio
from opscloud.utils.logger import get_logger
import os
import shutil

# S404: subprocess is required for git ls-files to get project file list
import subprocess  # noqa: S404
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from opscloud.project_utils import find_project_root
from opscloud.security.unicode_security import sanitize_control_chars

logger = get_logger(__name__)


def _get_git_executable() -> str | None:
    """Get full path to git executable using shutil.which().

    Returns:
        Full path to git executable, or None if not found.
    """
    return shutil.which("git")


if TYPE_CHECKING:
    from textual import events

    from opscloud.ui.command_registry import CommandEntry


class CompletionResult(StrEnum):
    """Result of handling a key event in the completion system."""

    IGNORED = "ignored"  # Key not handled, let default behavior proceed
    HANDLED = "handled"  # Key handled, prevent default
    SUBMIT = "submit"  # Key triggers submission (e.g., Enter on slash command)


class CompletionView(Protocol):
    """Protocol for views that can display completion suggestions."""

    def render_completion_suggestions(
        self, suggestions: list[tuple[str, str]], selected_index: int
    ) -> None:
        """Render the completion suggestions popup.

        Args:
            suggestions: List of (label, description) tuples
            selected_index: Index of currently selected item
        """
        ...

    def clear_completion_suggestions(self) -> None:
        """Hide/clear the completion suggestions popup."""
        ...

    def replace_completion_range(self, start: int, end: int, replacement: str) -> None:
        """Replace text in the input from start to end with replacement.

        Args:
            start: Start index in the input text
            end: End index in the input text
            replacement: Text to insert
        """
        ...


class CompletionController(Protocol):
    """Protocol for completion controllers."""

    def can_handle(self, text: str, cursor_index: int) -> bool:
        """Check if this controller can handle the current input state."""
        ...

    def on_text_changed(self, text: str, cursor_index: int) -> None:
        """Called when input text changes."""
        ...

    def on_key(
        self, event: events.Key, text: str, cursor_index: int
    ) -> CompletionResult:
        """Handle a key event. Returns how the event was handled."""
        ...

    def reset(self) -> None:
        """Reset/clear the completion state."""
        ...


# ============================================================================
# Slash Command Completion
# ============================================================================


MAX_SUGGESTIONS = 10
"""UI cap so the completion popup doesn't get unwieldy."""

_MIN_SLASH_FUZZY_SCORE = 25
"""Minimum score for slash-command fuzzy matches."""

_MIN_DESC_SEARCH_LEN = 2
"""Minimum query length to search command descriptions (avoids single-char noise)."""


class SlashCommandController:
    """Controller for / slash command completion."""

    def __init__(
        self,
        commands: list[CommandEntry],
        view: CompletionView,
    ) -> None:
        """Initialize the slash command controller.

        Args:
            commands: List of `CommandEntry` instances.
            view: View to render suggestions to.
        """
        self._commands = commands
        self._view = view
        self._suggestions: list[tuple[str, str]] = []
        self._suggestion_names: list[str] = []
        self._selected_index = 0

    def update_commands(self, commands: list[CommandEntry]) -> None:
        """Replace the commands list and reset suggestions."""
        self._commands = commands
        self.reset()

    @staticmethod
    def can_handle(text: str, cursor_index: int) -> bool:  # noqa: ARG004
        """Handle input that starts with /.

        Returns:
            True if text starts with slash, indicating a command.
        """
        return text.startswith("/")

    def reset(self) -> None:
        """Clear suggestions."""
        if self._suggestions:
            self._suggestions.clear()
            self._suggestion_names.clear()
            self._selected_index = 0
            self._view.clear_completion_suggestions()

    def name_prefix_matches(self, text: str, cursor_index: int) -> list[CommandEntry]:
        """Return commands whose names start with the current slash query."""
        if cursor_index < 0 or cursor_index > len(text):
            return []
        if not self.can_handle(text, cursor_index):
            return []

        search = text[1:cursor_index].lower()
        if not search or " " in search:
            return []

        return [
            entry
            for entry in self._commands
            if entry.name.lstrip("/").lower().startswith(search)
        ]

    @staticmethod
    def _score_command(search: str, cmd: str, desc: str, keywords: str = "") -> float:
        """Score a command against a search string. Higher = better match."""
        if not search:
            return 0.0
        name = cmd.lstrip("/").lower()
        lower_desc = desc.lower()
        if name.startswith(search):
            return 200.0
        if search in name:
            return 150.0
        if keywords and len(search) >= _MIN_DESC_SEARCH_LEN:
            for kw in keywords.lower().split():
                if kw.startswith(search) or search in kw:
                    return 120.0
        if len(search) >= _MIN_DESC_SEARCH_LEN and search in lower_desc:
            idx = lower_desc.find(search)
            if idx == 0 or lower_desc[idx - 1] == " ":
                return 110.0
            return 90.0
        name_ratio = SequenceMatcher(None, search, name).ratio()
        desc_ratio = SequenceMatcher(None, search, lower_desc).ratio()
        best = max(name_ratio * 60, desc_ratio * 30)
        return best if best >= _MIN_SLASH_FUZZY_SCORE else 0.0

    def on_text_changed(self, text: str, cursor_index: int) -> None:
        """Update suggestions when text changes."""
        if cursor_index < 0 or cursor_index > len(text):
            self.reset()
            return

        if not self.can_handle(text, cursor_index):
            self.reset()
            return

        search = text[1:cursor_index].lower()

        if " " in search:
            self.reset()
            return

        if not search:
            selected = list(self._commands)[:MAX_SUGGESTIONS]
        else:
            scored = [
                (score, entry)
                for entry in self._commands
                if (
                    score := self._score_command(
                        search, entry.name, entry.description, entry.hidden_keywords
                    )
                )
                > 0
            ]
            scored.sort(key=lambda x: -x[0])
            selected = [entry for _, entry in scored[:MAX_SUGGESTIONS]]

        if selected:
            self._suggestions = [
                (
                    entry.display_name if getattr(entry, "display_name", "") else entry.name,
                    entry.description,
                )
                for entry in selected
            ]
            self._suggestion_names = [entry.name for entry in selected]
            self._selected_index = 0
            self._view.render_completion_suggestions(
                self._suggestions, self._selected_index
            )
        else:
            self.reset()

    def on_key(
        self, event: events.Key, text: str, cursor_index: int
    ) -> CompletionResult:
        """Handle key events for navigation and selection."""
        if not self._suggestions:
            return CompletionResult.IGNORED

        match event.key:
            case "tab" | "space":
                if self._apply_selected_completion(cursor_index):
                    return CompletionResult.HANDLED
                return CompletionResult.IGNORED
            case "enter":
                if self._apply_selected_completion(cursor_index):
                    return CompletionResult.SUBMIT
                return CompletionResult.HANDLED
            case "down":
                self._move_selection(1)
                return CompletionResult.HANDLED
            case "up":
                self._move_selection(-1)
                return CompletionResult.HANDLED
            case "escape":
                self.reset()
                return CompletionResult.HANDLED
            case _:
                return CompletionResult.IGNORED

    def _move_selection(self, delta: int) -> None:
        """Move selection up or down."""
        if not self._suggestions:
            return
        count = len(self._suggestions)
        self._selected_index = (self._selected_index + delta) % count
        self._view.render_completion_suggestions(
            self._suggestions, self._selected_index
        )

    def _apply_selected_completion(self, cursor_index: int) -> bool:
        """Apply the currently selected completion."""
        if not self._suggestions:
            return False

        command = self._suggestion_names[self._selected_index]
        self._view.replace_completion_range(0, cursor_index, command)
        self.reset()
        return True

    def apply_name_prefix_completion(
        self, match: CommandEntry, cursor_index: int
    ) -> None:
        """Apply a command-name prefix match."""
        self._view.replace_completion_range(0, cursor_index, match.name)
        self.reset()


# ============================================================================
# Fuzzy File Completion (scoped to current working directory)
# ============================================================================

_MAX_FALLBACK_FILES = 1000
"""Hard cap on files returned by the non-git glob fallback."""

_MIN_FUZZY_SCORE = 15
"""Minimum score to include in file-completion results."""

_MIN_FUZZY_RATIO = 0.4
"""SequenceMatcher threshold for filename-only fuzzy matches."""

_NOT_A_REPO_MARKER = "not a git repository"
"""Marker in `git ls-files` stderr for a non-repository directory."""

_GIT_STDERR_LOG_LIMIT = 500
"""Max characters of git stderr to include in a diagnostic log line."""


def _run_git_ls_files(
    git_path: str, root: Path, extra_args: list[str]
) -> tuple[bool, list[str]]:
    """Run `git ls-files` with the given arguments and return file paths."""
    try:
        result = subprocess.run(  # noqa: S603
            [git_path, "ls-files", *extra_args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        logger.debug("git ls-files %s failed to run", extra_args, exc_info=True)
        return False, []
    if result.returncode != 0:
        stderr = sanitize_control_chars(result.stderr, max_length=_GIT_STDERR_LOG_LIMIT)
        if _NOT_A_REPO_MARKER not in stderr.lower():
            logger.debug(
                "git ls-files failed: root=%s args=%s exit=%d stderr=%s",
                root,
                extra_args,
                result.returncode,
                stderr,
            )
        return False, []
    return True, [f for f in result.stdout.strip().split("\n") if f]


def _get_project_files(root: Path) -> list[str]:
    """Get project files using git ls-files or fallback to glob."""
    git_path = _get_git_executable()
    if git_path:
        tracked_ok, tracked = _run_git_ls_files(git_path, root, [])
        if tracked_ok:
            _, untracked = _run_git_ls_files(
                git_path, root, ["--others", "--exclude-standard"]
            )
            seen: set[str] = set()
            files: list[str] = []
            for f in (*tracked, *untracked):
                if f not in seen:
                    seen.add(f)
                    files.append(f)
            return files

    files = []
    try:
        for pattern in ["*", "*/*", "*/*/*", "*/*/*/*"]:
            for p in root.glob(pattern):
                if p.is_file() and not any(part.startswith(".") for part in p.parts):
                    files.append(p.relative_to(root).as_posix())
                if len(files) >= _MAX_FALLBACK_FILES:
                    break
            if len(files) >= _MAX_FALLBACK_FILES:
                break
    except OSError:
        logger.debug("glob fallback failed for %s", root, exc_info=True)
    return files


def _fuzzy_score(query: str, candidate: str) -> float:
    """Score a candidate against query. Higher = better match."""
    query_lower = query.lower()
    candidate_normalized = candidate.replace("\\", "/")
    candidate_lower = candidate_normalized.lower()

    filename = candidate_normalized.rsplit("/", 1)[-1].lower()
    filename_start = candidate_lower.rfind("/") + 1

    if query_lower in filename:
        idx = filename.find(query_lower)
        if idx == 0:
            return 150 + (1 / len(candidate))
        if idx > 0 and filename[idx - 1] in "_-.":
            return 120 + (1 / len(candidate))
        return 100 + (1 / len(candidate))

    if query_lower in candidate_lower:
        idx = candidate_lower.find(query_lower)
        if idx == filename_start:
            return 80 + (1 / len(candidate))
        if idx == 0 or candidate[idx - 1] in "/_-.":
            return 60 + (1 / len(candidate))
        return 40 + (1 / len(candidate))

    filename_ratio = SequenceMatcher(None, query_lower, filename).ratio()
    if filename_ratio > _MIN_FUZZY_RATIO:
        return filename_ratio * 30

    ratio = SequenceMatcher(None, query_lower, candidate_lower).ratio()
    return ratio * 15


def _is_dotpath(path: str) -> bool:
    """Check if path contains dotfiles/dotdirs."""
    return any(part.startswith(".") for part in path.split("/"))


def _path_depth(path: str) -> int:
    """Get depth of path."""
    return path.count("/")


def _fuzzy_search(
    query: str,
    candidates: list[str],
    limit: int = 10,
    *,
    include_dotfiles: bool = False,
) -> list[str]:
    """Return top matches sorted by score."""
    filtered = (
        candidates
        if include_dotfiles
        else [c for c in candidates if not _is_dotpath(c)]
    )

    if not query:
        sorted_files = sorted(filtered, key=lambda p: (_path_depth(p), p.lower()))
        return sorted_files[:limit]

    scored = [
        (score, c)
        for c in filtered
        if (score := _fuzzy_score(query, c)) >= _MIN_FUZZY_SCORE
    ]
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:limit]]


def _scope_files_to_cwd(files: list[str], project_root: Path, cwd: Path) -> list[str]:
    """Scope a project-root-relative file list to paths under `cwd`."""
    if cwd == project_root:
        return files
    try:
        relative_cwd = cwd.relative_to(project_root).as_posix()
    except ValueError:
        return []
    prefix = f"{relative_cwd}/"
    return [path[len(prefix) :] for path in files if path.startswith(prefix)]


class FuzzyFileController:
    """Controller for @ file completion with fuzzy matching from current cwd."""

    def __init__(
        self,
        view: CompletionView,
        cwd: Path | None = None,
    ) -> None:
        """Initialize the fuzzy file controller."""
        self._view = view
        self._cwd = (cwd or Path.cwd()).resolve()
        self._project_root = find_project_root(self._cwd) or self._cwd
        self._suggestions: list[tuple[str, str]] = []
        self._selected_index = 0
        self._file_cache: list[str] | None = None
        self._project_root_pending = False
        self._cache_generation = 0

    def _get_files(self) -> list[str]:
        """Get cached file list or refresh."""
        if self._file_cache is None:
            files = _get_project_files(self._project_root)
            self._file_cache = _scope_files_to_cwd(files, self._project_root, self._cwd)
        return self._file_cache

    def refresh_cache(self) -> None:
        """Force refresh of file cache."""
        self._cache_generation += 1
        self._file_cache = None

    def set_cwd(self, cwd: Path) -> None:
        """Switch completion roots to a new cwd."""
        self._cache_generation += 1
        self._cwd = cwd.resolve()
        self._project_root = self._cwd
        self._project_root_pending = True
        self._file_cache = None
        self.reset()

    async def warm_cache(self, *, force: bool = False) -> None:
        """Pre-populate the file cache off the event loop."""
        cwd = self._cwd
        generation = self._cache_generation
        if self._project_root_pending:
            root = await asyncio.to_thread(find_project_root, cwd)
            if generation != self._cache_generation:
                return
            resolved = root or cwd
            if resolved != self._project_root:
                self._file_cache = None
            self._project_root = resolved
            self._project_root_pending = False
        if not force and self._file_cache is not None:
            return
        project_root = self._project_root
        try:
            files = await asyncio.to_thread(_get_project_files, project_root)
            if generation == self._cache_generation:
                self._file_cache = _scope_files_to_cwd(files, project_root, cwd)
        except Exception:
            logger.debug("File-cache warm failed for %s", project_root, exc_info=True)

    @staticmethod
    def can_handle(text: str, cursor_index: int) -> bool:
        """Handle input that contains @ not followed by space."""
        if cursor_index <= 0 or cursor_index > len(text):
            return False

        before_cursor = text[:cursor_index]
        if "@" not in before_cursor:
            return False

        at_index = before_cursor.rfind("@")
        if cursor_index <= at_index:
            return False

        fragment = before_cursor[at_index:cursor_index]
        return bool(fragment) and " " not in fragment

    def reset(self) -> None:
        """Clear suggestions."""
        if self._suggestions:
            self._suggestions.clear()
            self._selected_index = 0
            self._view.clear_completion_suggestions()

    def on_text_changed(self, text: str, cursor_index: int) -> None:
        """Update suggestions when text changes."""
        if not self.can_handle(text, cursor_index):
            self.reset()
            return

        before_cursor = text[:cursor_index]
        at_index = before_cursor.rfind("@")
        search = before_cursor[at_index + 1 :]

        suggestions = self._get_fuzzy_suggestions(search)

        if suggestions:
            self._suggestions = suggestions
            self._selected_index = 0
            self._view.render_completion_suggestions(
                self._suggestions, self._selected_index
            )
        else:
            self.reset()

    def _get_fuzzy_suggestions(self, search: str) -> list[tuple[str, str]]:
        """Get fuzzy file suggestions."""
        files = self._get_files()
        include_dots = search.startswith(".")
        matches = _fuzzy_search(
            search, files, limit=MAX_SUGGESTIONS, include_dotfiles=include_dots
        )

        suggestions: list[tuple[str, str]] = []
        for path in matches:
            ext = Path(path).suffix.lower()
            type_hint = ext[1:] if ext else "file"
            suggestions.append((f"@{path}", type_hint))

        return suggestions

    def on_key(
        self, event: events.Key, text: str, cursor_index: int
    ) -> CompletionResult:
        """Handle key events for navigation and selection."""
        if not self._suggestions:
            return CompletionResult.IGNORED

        match event.key:
            case "tab" | "enter":
                if self._apply_selected_completion(text, cursor_index):
                    return CompletionResult.HANDLED
                return CompletionResult.IGNORED
            case "down":
                self._move_selection(1)
                return CompletionResult.HANDLED
            case "up":
                self._move_selection(-1)
                return CompletionResult.HANDLED
            case "escape":
                self.reset()
                return CompletionResult.HANDLED
            case _:
                return CompletionResult.IGNORED

    def _move_selection(self, delta: int) -> None:
        """Move selection up or down."""
        if not self._suggestions:
            return
        count = len(self._suggestions)
        self._selected_index = (self._selected_index + delta) % count
        self._view.render_completion_suggestions(
            self._suggestions, self._selected_index
        )

    def _apply_selected_completion(self, text: str, cursor_index: int) -> bool:
        """Apply the currently selected completion."""
        if not self._suggestions:
            return False

        label, _ = self._suggestions[self._selected_index]
        before_cursor = text[:cursor_index]
        at_index = before_cursor.rfind("@")

        if at_index < 0:
            return False

        self._view.replace_completion_range(at_index, cursor_index, label)
        self.reset()
        return True


PathCompletionController = FuzzyFileController


# ============================================================================
# Multi-Completion Manager
# ============================================================================


class MultiCompletionManager:
    """Manages multiple completion controllers, delegating to the active one."""

    def __init__(self, controllers: list[CompletionController]) -> None:
        """Initialize with a list of controllers."""
        self._controllers = controllers
        self._active: CompletionController | None = None

    def on_text_changed(self, text: str, cursor_index: int) -> None:
        """Handle text change, activating the appropriate controller."""
        candidate = None
        for controller in self._controllers:
            if controller.can_handle(text, cursor_index):
                candidate = controller
                break

        if candidate is None:
            if self._active is not None:
                self._active.reset()
                self._active = None
            return

        if candidate is not self._active:
            if self._active is not None:
                self._active.reset()
            self._active = candidate

        candidate.on_text_changed(text, cursor_index)

    def on_key(
        self, event: events.Key, text: str, cursor_index: int
    ) -> CompletionResult:
        """Handle key event, delegating to active controller."""
        if self._active is None:
            return CompletionResult.IGNORED
        return self._active.on_key(event, text, cursor_index)

    def reset(self) -> None:
        """Reset all controllers."""
        if self._active is not None:
            self._active.reset()
            self._active = None


# ── AutocompletePopup (widget stub) ──────────────────────


class AutocompletePopup:
    """Stub namespace for the ``CommandSelected`` message."""

    from textual.message import Message as _Message

    class CommandSelected(_Message):
        """Fired when the user picks a slash command from the popup."""

        def __init__(self, command_name: str, *, by_enter: bool = False) -> None:
            super().__init__()
            self.command_name = command_name
            self.by_enter = by_enter
