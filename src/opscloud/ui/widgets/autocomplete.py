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
from collections.abc import Sequence
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

    def apply_selection(self, index: int, text: str, cursor_index: int) -> bool:
        """Apply a clicked completion row."""
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
        kw_tokens = keywords.lower().split() if keywords else []
        is_skill_entry = "skill" in kw_tokens
        is_alias = lower_desc.startswith("alias for ") or lower_desc.startswith("alias ")

        # Support legacy /skill:<query> search against skill entries
        if search.startswith("skill:") and is_skill_entry:
            sub_q = search[len("skill:") :].strip()
            if not sub_q:
                return 225.0
            search = sub_q

        # 1. Exact match on canonical command
        if name == search:
            return 250.0 if not is_alias else 185.0

        # 1b. Exact match on skill/subcommand suffix after ':'
        if ":" in name and name.rsplit(":", 1)[-1] == search:
            return 240.0 if not is_alias else 180.0

        # 2. Direct colon-subcommands of search (e.g. /chrome-devtools-plugin:foo or /agent:foo)
        if (
            name.startswith(f"{search}:")
            or (":" in name and name.split(":", 1)[0] == search)
            or (":" in name and is_skill_entry and name.split(":", 1)[0].startswith(search) and len(search) >= 2)
            or (search in ("ag", "age", "agen") and name.startswith("agent:"))
        ):
            return 230.0

        # 3. Canonical commands matching prefix (or skill short-name prefix after ':')
        if name.startswith(search):
            return 210.0 if not is_alias else 170.0
        if ":" in name and name.rsplit(":", 1)[-1].startswith(search):
            return 205.0 if not is_alias else 165.0

        # 4. Substring in name
        if search in name:
            return 150.0 if not is_alias else 130.0

        # 5. Keywords match
        if kw_tokens and len(search) >= _MIN_DESC_SEARCH_LEN:
            for kw in kw_tokens:
                if kw.startswith(search) or search in kw:
                    return 120.0

        # 6. Description match
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

        # Handle parameterized subcommands like "/agent <name>" or "/skill <name>"
        if " " in search:
            cmd_part, _, sub_search = search.partition(" ")
            if cmd_part in ("agent", "agents", "skill"):
                sub_clean = sub_search.strip()
                if cmd_part == "skill":
                    matching_entries = [
                        entry
                        for entry in self._commands
                        if "skill" in entry.hidden_keywords.lower().split()
                        and not entry.name.lower().startswith("/skill-")
                        and entry.name.lower() != "/skills"
                        and (
                            not sub_clean
                            or sub_clean in entry.name.lower()
                            or (getattr(entry, "display_name", "") and sub_clean in entry.display_name.lower())
                            or sub_clean in entry.description.lower()
                            or sub_clean in entry.hidden_keywords.lower()
                        )
                    ]
                else:
                    prefix = "/agent:"
                    matching_entries = [
                        entry for entry in self._commands
                        if entry.name.lower().startswith(prefix)
                        and (
                            not sub_clean
                            or sub_clean in entry.name.lower()
                            or (getattr(entry, "display_name", "") and sub_clean in entry.display_name.lower())
                            or sub_clean in entry.description.lower()
                        )
                    ]
                if matching_entries:
                    self._suggestions = [
                        (
                            entry.display_name if getattr(entry, "display_name", "") else entry.name,
                            entry.description,
                        )
                        for entry in matching_entries[:MAX_SUGGESTIONS]
                    ]
                    self._suggestion_names = [
                        entry.display_name if getattr(entry, "display_name", "") else entry.name
                        for entry in matching_entries[:MAX_SUGGESTIONS]
                    ]
                    self._selected_index = 0
                    self._view.render_completion_suggestions(
                        self._suggestions, self._selected_index
                    )
                    return
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
            self._suggestion_names = [
                entry.display_name if getattr(entry, "display_name", "") else entry.name
                for entry in selected
            ]
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

    def apply_selection(self, index: int, text: str, cursor_index: int) -> bool:  # noqa: ARG002
        """Apply a clicked slash-command completion row.

        Returns:
            Whether the selected row was applied.
        """
        if index < 0 or index >= len(self._suggestions):
            return False
        self._selected_index = index
        return self._apply_selected_completion(cursor_index)

    def apply_name_prefix_completion(
        self, match: CommandEntry, cursor_index: int
    ) -> None:
        """Apply a command-name prefix match."""
        self._view.replace_completion_range(0, cursor_index, match.name)
        self.reset()


# ============================================================================
# Fuzzy File Completion (scoped to current working directory)
# ============================================================================

_MAX_FALLBACK_FILES = 2000
"""Hard cap on entries returned by live filesystem walks."""

_MIN_FUZZY_SCORE = 15.0
"""Minimum score to include in file-completion results."""

_MIN_FUZZY_RATIO = 0.4
"""SequenceMatcher threshold for filename-only fuzzy matches."""

_NOT_A_REPO_MARKER = "not a git repository"
"""Marker in `git ls-files` stderr for a non-repository directory."""

_GIT_STDERR_LOG_LIMIT = 500
"""Max characters of git stderr to include in a diagnostic log line."""

# Path scoring constants for `@` file and directory completion
_EXACT_MATCH_SCORE = 262144.0
_PREFIX_BASE_BONUS = 131072.0
_PREFIX_RATIO_MULT = 100.0
_BASENAME_BONUS = 65536.0
_LENGTH_PENALTY = 0.01

_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        ".eggs",
    }
)


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


def _extract_parent_dirs(
    rel_path: str, seen: set[str], out: list[str]
) -> None:
    """Extract all parent directory paths (with trailing '/') from a relative path."""
    norm = rel_path.replace("\\", "/").strip("/")
    while "/" in norm:
        norm = norm.rsplit("/", 1)[0]
        if not norm or norm == ".":
            break
        dir_entry = f"{norm}/"
        if dir_entry in seen:
            break
        seen.add(dir_entry)
        out.append(dir_entry)


def _append_walked_entries(
    root: Path,
    target_dir: Path | None = None,
    seen: set[str] | None = None,
    results: list[str] | None = None,
    *,
    max_entries: int = _MAX_FALLBACK_FILES,
    max_depth: int | None = None,
) -> list[str]:
    """Walk directories via `os.scandir` to discover live files and directories.

    Ensures newly created files and directories (including empty directories)
    are discovered live without waiting on a static cache.
    """
    if seen is None:
        seen = set()
    if results is None:
        results = []

    start_dir = target_dir if target_dir is not None else root
    try:
        if not start_dir.is_dir():
            return results
    except OSError:
        return results

    queue: list[tuple[Path, int]] = [(start_dir, 0)]
    while queue and len(results) < max_entries:
        current_dir, depth = queue.pop(0)
        if max_depth is not None and depth >= max_depth:
            continue
        try:
            with os.scandir(current_dir) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue

        for entry in entries:
            name = entry.name
            if name in _IGNORED_DIRS or name.endswith(".egg-info"):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue

            try:
                rel = Path(entry.path).relative_to(root).as_posix()
            except ValueError:
                continue

            if is_dir:
                dir_rel = f"{rel}/"
                if dir_rel not in seen:
                    seen.add(dir_rel)
                    results.append(dir_rel)
                queue.append((Path(entry.path), depth + 1))
            elif is_file:
                if rel not in seen:
                    seen.add(rel)
                    results.append(rel)
                    _extract_parent_dirs(rel, seen, results)

            if len(results) >= max_entries:
                break

    return results


def _get_project_files(root: Path) -> list[str]:
    """Get project files and directories using git ls-files plus live scandir walk."""
    seen: set[str] = set()
    entries: list[str] = []

    git_path = _get_git_executable()
    if git_path:
        ok, git_files = _run_git_ls_files(
            git_path, root, ["--cached", "--others", "--exclude-standard"]
        )
        if ok:
            for f in git_files:
                norm = f.replace("\\", "/")
                if norm in seen:
                    continue
                full_path = root / norm
                try:
                    if not full_path.exists():
                        continue
                except OSError:
                    continue
                seen.add(norm)
                entries.append(norm)
                _extract_parent_dirs(norm, seen, entries)

            # Live walk top-level and immediate subdirectories so empty or newly
            # created directories not tracked by git are included.
            _append_walked_entries(
                root, root, seen, entries, max_entries=_MAX_FALLBACK_FILES, max_depth=3
            )
            return entries

    _append_walked_entries(root, root, seen, entries, max_entries=_MAX_FALLBACK_FILES)
    return entries


def _is_subsequence(needle: str, haystack: str) -> bool:
    """Return True if all characters of `needle` appear in order in `haystack`."""
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _fuzzy_score(query: str, candidate: str) -> float:
    """Score a candidate path against query."""
    candidate_normalized = candidate.replace("\\", "/")
    while candidate_normalized.startswith("./"):
        candidate_normalized = candidate_normalized[2:]

    norm_path = candidate_normalized.rstrip("/")
    norm_lower = norm_path.lower()
    base_lower = norm_lower.rsplit("/", 1)[-1]
    length_penalty = len(candidate_normalized) * _LENGTH_PENALTY

    query_norm = query.replace("\\", "/").strip()
    while query_norm.startswith("./"):
        query_norm = query_norm[2:]
    query_lower = query_norm.rstrip("/").lower()

    if not query_lower:
        return _BASENAME_BONUS - length_penalty

    # 1. Exact match on full path or basename
    if norm_lower == query_lower or base_lower == query_lower:
        return _EXACT_MATCH_SCORE - length_penalty

    # 2. Basename prefix match
    if base_lower.startswith(query_lower):
        ratio_bonus = round(_PREFIX_RATIO_MULT * len(query_lower) / max(1, len(base_lower)))
        return _PREFIX_BASE_BONUS + ratio_bonus - length_penalty

    # 3. Basename substring / word-boundary match
    if query_lower in base_lower:
        idx = base_lower.find(query_lower)
        ratio_bonus = round(50.0 * len(query_lower) / max(1, len(base_lower)))
        if idx > 0 and base_lower[idx - 1] in "_-.":
            return _BASENAME_BONUS + 90.0 + ratio_bonus - length_penalty
        return _BASENAME_BONUS + 70.0 + ratio_bonus - length_penalty

    # 4. Full relative path prefix or substring match
    if query_lower in norm_lower:
        idx = norm_lower.find(query_lower)
        ratio_bonus = round(60.0 * len(query_lower) / max(1, len(norm_lower)))
        if idx == 0:
            return _PREFIX_BASE_BONUS + ratio_bonus - length_penalty
        if norm_lower[idx - 1] in "/_-.":
            return 4096.0 + ratio_bonus - length_penalty
        return 2048.0 + ratio_bonus - length_penalty

    # 5. Basename subsequence or fuzzy typo match
    if _is_subsequence(query_lower, base_lower):
        return _BASENAME_BONUS + 40.0 * (len(query_lower) / max(1, len(base_lower))) - length_penalty

    filename_ratio = SequenceMatcher(None, query_lower, base_lower).ratio()
    if filename_ratio > _MIN_FUZZY_RATIO:
        return _BASENAME_BONUS + filename_ratio * 30.0 - length_penalty

    # 6. Full path subsequence or fuzzy ratio fallback
    if _is_subsequence(query_lower, norm_lower):
        return 1024.0 * (len(query_lower) / max(1, len(norm_lower))) - length_penalty

    ratio = SequenceMatcher(None, query_lower, norm_lower).ratio()
    return ratio * 15.0 - length_penalty


def _is_dotpath(path: str) -> bool:
    """Check if path contains dotfiles/dotdirs."""
    return any(part.startswith(".") for part in path.rstrip("/").split("/") if part)


def _path_depth(path: str) -> int:
    """Get depth of path (ignoring trailing slash on directories)."""
    return path.rstrip("/").count("/")


def _fuzzy_search(
    query: str,
    candidates: list[str],
    limit: int = 10,
    *,
    include_dotfiles: bool = False,
) -> list[str]:
    """Return top path matches sorted by score."""
    filtered = (
        candidates
        if include_dotfiles
        else [c for c in candidates if not _is_dotpath(c)]
    )

    query_clean = query.replace("\\", "/").rstrip("/")
    if not query_clean:
        # Empty-query ranking: score = 65536 - 0.01 * len(relPath),
        # with top-level entries prioritized and ties broken lexicographically.
        sorted_files = sorted(
            filtered,
            key=lambda p: (_path_depth(p) > 0, len(p), p.lower()),
        )
        return sorted_files[:limit]

    scored = [
        (score, c)
        for c in filtered
        if (score := _fuzzy_score(query_clean, c)) >= _MIN_FUZZY_SCORE
    ]
    scored.sort(key=lambda x: (-x[0], x[1].lower()))
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
    return [
        path[len(prefix) :]
        for path in files
        if path.startswith(prefix) and len(path) > len(prefix)
    ]


def _resolve_search_base(
    raw_query: str, cwd: Path
) -> tuple[Path, str, str, bool]:
    """Resolve search base directory, display prefix, and leaf query from `@` query.

    Resolution rules:
    - Supports `~/...`, `./...`, `../...`, and workspace subdirectory prefixes (`src/...`).
    - Returns `(search_base, display_prefix, subquery, is_external_base)`.
    """
    normalized = raw_query.replace("\\", "/")

    if normalized == "~" or normalized.startswith("~/"):
        sub = normalized[2:] if normalized.startswith("~/") else ""
        base = Path.home()
        prefix = "~/"
        is_external = True
    elif normalized.startswith("./") or normalized.startswith("../") or normalized in (".", ".."):
        if normalized in (".", ".."):
            normalized = f"{normalized}/"
        base = cwd
        prefix = ""
        sub = normalized
        while sub.startswith("./") or sub.startswith("../"):
            if sub.startswith("./"):
                prefix += "./"
                sub = sub[2:]
            else:
                prefix += "../"
                sub = sub[3:]
                base = base.parent
        is_external = True
    else:
        base = cwd
        prefix = ""
        sub = normalized
        is_external = False

    if "/" in sub:
        dir_part, leaf = sub.rsplit("/", 1)
        candidate_dir = base / dir_part
        try:
            if candidate_dir.is_dir():
                return (
                    candidate_dir,
                    f"{prefix}{dir_part}/",
                    leaf,
                    is_external,
                )
        except OSError:
            pass

    return base, prefix, sub, is_external


class FuzzyFileController:
    """Controller for @ file completion with live workspace discovery."""

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

    def _get_files(self, target_dir: Path | None = None) -> list[str]:
        """Get live workspace file and directory candidates."""
        base_files = _get_project_files(self._project_root)
        scoped = _scope_files_to_cwd(base_files, self._project_root, self._cwd)

        seen = set(scoped)
        results = list(scoped)
        # Always perform a live scandir walk on cwd (and target_dir if specified)
        # so any newly created file or directory appears immediately.
        _append_walked_entries(
            self._cwd,
            self._cwd,
            seen,
            results,
            max_entries=_MAX_FALLBACK_FILES,
        )
        if target_dir is not None and target_dir != self._cwd:
            _append_walked_entries(
                self._cwd,
                target_dir,
                seen,
                results,
                max_entries=_MAX_FALLBACK_FILES,
            )
        self._file_cache = results
        return results

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
        """Handle input that contains @ at start of input or after whitespace."""
        if cursor_index <= 0 or cursor_index > len(text):
            return False

        before_cursor = text[:cursor_index]
        if "@" not in before_cursor:
            return False

        at_index = before_cursor.rfind("@")
        if cursor_index <= at_index:
            return False

        if at_index > 0 and not before_cursor[at_index - 1].isspace():
            return False

        fragment = before_cursor[at_index:cursor_index]
        return bool(fragment) and not any(ch.isspace() for ch in fragment)

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

    @staticmethod
    def _format_suggestion(display_path: str) -> tuple[str, str]:
        """Format a candidate path into `(@path, description)`."""
        if display_path.endswith("/"):
            return (f"@{display_path}", "Directory")
        ext = Path(display_path).suffix.lower()
        type_hint = ext[1:] if ext else "file"
        return (f"@{display_path}", type_hint)

    def _get_fuzzy_suggestions(self, search: str) -> list[tuple[str, str]]:
        """Get live fuzzy file and directory suggestions."""
        search_base, display_prefix, leaf_query, is_external = _resolve_search_base(
            search, self._cwd
        )
        include_dots = (
            leaf_query.startswith(".")
            or any(
                part.startswith(".") and part not in (".", "..")
                for part in search.replace("\\", "/").split("/")
            )
        )

        if is_external:
            candidates = _append_walked_entries(
                search_base,
                search_base,
                max_entries=_MAX_FALLBACK_FILES,
                max_depth=3,
            )
            matches = _fuzzy_search(
                leaf_query,
                candidates,
                limit=MAX_SUGGESTIONS,
                include_dotfiles=include_dots,
            )
            return [
                self._format_suggestion(f"{display_prefix}{path}")
                for path in matches
            ]

        all_files = self._get_files(target_dir=search_base)

        if display_prefix:
            scoped_candidates = [
                p[len(display_prefix) :]
                for p in all_files
                if p.startswith(display_prefix) and len(p) > len(display_prefix)
            ]
            scoped_matches = _fuzzy_search(
                leaf_query,
                scoped_candidates,
                limit=MAX_SUGGESTIONS,
                include_dotfiles=include_dots,
            )
            if scoped_matches or not leaf_query:
                return [
                    self._format_suggestion(f"{display_prefix}{path}")
                    for path in scoped_matches
                ]

        matches = _fuzzy_search(
            search,
            all_files,
            limit=MAX_SUGGESTIONS,
            include_dotfiles=include_dots,
        )
        return [self._format_suggestion(path) for path in matches]

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
        """Apply the currently selected completion.

        When a directory (ending with '/') is selected, keeps the completion
        popup open and immediately drills down into that directory's contents.
        """
        if not self._suggestions:
            return False

        label, _ = self._suggestions[self._selected_index]
        before_cursor = text[:cursor_index]
        at_index = before_cursor.rfind("@")

        if at_index < 0:
            return False

        self._view.replace_completion_range(at_index, cursor_index, label)
        if label.endswith("/"):
            new_text = f"{text[:at_index]}{label}{text[cursor_index:]}"
            new_cursor = at_index + len(label)
            self.on_text_changed(new_text, new_cursor)
        else:
            self.reset()
        return True

    def apply_selection(self, index: int, text: str, cursor_index: int) -> bool:
        """Apply a clicked file completion row.

        Returns:
            Whether the selected row was applied.
        """
        if index < 0 or index >= len(self._suggestions):
            return False
        self._selected_index = index
        return self._apply_selected_completion(text, cursor_index)


PathCompletionController = FuzzyFileController


# ============================================================================
# Multi-Completion Manager
# ============================================================================


class MultiCompletionManager:
    """Manages multiple completion controllers, delegating to the active one."""

    def __init__(self, controllers: Sequence[CompletionController]) -> None:
        """Initialize with a list of controllers."""
        self._controllers = list(controllers)
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

    def apply_selection(self, index: int, text: str, cursor_index: int) -> bool:
        """Apply a clicked completion through the active controller.

        Returns:
            Whether the active controller applied the row.
        """
        if self._active is None:
            return False
        if hasattr(self._active, "apply_selection"):
            return self._active.apply_selection(index, text, cursor_index)
        return False


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
