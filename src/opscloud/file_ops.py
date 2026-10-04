"""Helpers for tracking file operations and computing diffs for display."""

from __future__ import annotations

import difflib
from opscloud.utils.logger import get_logger
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = get_logger(__name__)

FileOpStatus = Literal["pending", "success", "error"]


@dataclass
class ApprovalPreview:
    """Data used to render HITL previews."""

    title: str
    details: list[str]
    diff: str | None = None
    diff_title: str | None = None
    error: str | None = None


def _safe_read(path: Path) -> str | None:
    """Read file content, returning None on failure."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Failed to read file %s: %s", path, e)
        return None


def _count_lines(text: str) -> int:
    """Count lines in text, treating empty strings as zero lines."""
    if not text:
        return 0
    return len(text.splitlines())


def compute_unified_diff(
    before: str,
    after: str,
    display_path: str,
    *,
    max_lines: int | None = 800,
    context_lines: int = 3,
) -> str | None:
    """Compute a unified diff between before and after content."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{display_path} (before)",
            tofile=f"{display_path} (after)",
            lineterm="",
            n=context_lines,
        )
    )
    if not diff_lines:
        return None
    if max_lines is not None and len(diff_lines) > max_lines:
        truncated = diff_lines[: max_lines - 1]
        truncated.append("...")
        return "\n".join(truncated)
    return "\n".join(diff_lines)


_SENSITIVE_FILE_NAMES = frozenset(
    {
        ".envrc",
        ".netrc",
        "_netrc",
        ".pgpass",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        ".git-credentials",
        "credentials",
        "credentials.json",
        "token.json",
        "auth.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
"""Basenames (lowercased) that commonly hold secrets and must not be rendered."""

_SENSITIVE_FILE_SUFFIXES = (
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".keystore",
    ".jks",
)
"""File suffixes (lowercased) for private keys / keystores that hold secrets."""


def is_sensitive_file_path(path_str: str | None) -> bool:
    """Return whether a path points at a credential/secret file."""
    if not path_str:
        return False
    try:
        name = Path(path_str).name.lower()
    except (OSError, ValueError, TypeError):
        logger.warning(
            "is_sensitive_file_path: could not parse %r; treating as sensitive",
            path_str,
        )
        return True
    if not name:
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name in _SENSITIVE_FILE_NAMES:
        return True
    return name.endswith(_SENSITIVE_FILE_SUFFIXES)


def format_display_path(path_str: str | None) -> str:
    """Format a path for display."""
    if not path_str:
        return "(unknown)"
    try:
        path = Path(path_str)
        if path.is_absolute():
            return path.name or str(path)
        return str(path)
    except (OSError, ValueError):
        return str(path_str)


def resolve_physical_path(
    path_str: str | None, assistant_id: str | None = None
) -> Path | None:
    """Convert a virtual/relative path to a physical filesystem path."""
    if not path_str:
        return None
    try:
        path = Path(path_str)
        if path.is_absolute():
            return path
        return (Path.cwd() / path).resolve()
    except (OSError, ValueError):
        return None


def build_approval_preview(
    tool_name: str,
    args: dict[str, Any],
    assistant_id: str | None = None,
) -> ApprovalPreview | None:
    """Collect summary info and diff for HITL approvals."""
    path_str = str(args.get("file_path") or args.get("path") or "")
    display_path = format_display_path(path_str)
    physical_path = resolve_physical_path(path_str, assistant_id)

    if tool_name in {"write_file", "write_to_file"}:
        content = str(args.get("content", "") or args.get("code_content", ""))
        before = (
            _safe_read(physical_path)
            if physical_path and physical_path.exists()
            else ""
        )
        after = content
        diff = compute_unified_diff(before or "", after, display_path, max_lines=100)
        additions = 0
        if diff:
            additions = sum(
                1
                for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
        total_lines = _count_lines(after)
        details = [
            f"File: {path_str}",
            "Action: Create new file"
            + (" (overwrites existing content)" if before else ""),
            f"Lines to write: {additions or total_lines}",
        ]
        return ApprovalPreview(
            title=f"Write {display_path}",
            details=details,
            diff=diff,
            diff_title=f"Diff {display_path}",
        )

    if tool_name in {"delete", "delete_file"}:
        details = [f"File: {path_str}", "Action: Delete file or directory"]
        if physical_path is None:
            return ApprovalPreview(
                title=f"Delete {display_path}",
                details=details,
                error="Unable to resolve file path.",
            )
        before = _safe_read(physical_path)
        diff = None
        if before is not None:
            diff = compute_unified_diff(before, "", display_path, max_lines=100)
            details.append(f"Lines to delete: {_count_lines(before)}")
        elif physical_path.exists():
            details.append("Contents: directory or unreadable file")
        return ApprovalPreview(
            title=f"Delete {display_path}",
            details=details,
            diff=diff,
            diff_title=f"Diff {display_path}",
        )

    return None
