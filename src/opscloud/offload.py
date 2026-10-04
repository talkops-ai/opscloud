"""Storage paths for offloaded conversation history in OpsCode."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath

from opscloud.config.paths import DATA_DIR
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_FALLBACK_ARTIFACTS_ROOT = "/opscloud-artifacts-fallback"


@dataclass(frozen=True)
class _ArtifactsStorage:
    """Agent-visible artifacts root and optional routed large-result directory."""

    root: str
    large_results_dir: Path | None = None


def _filesystem_tool_path(path: PurePath) -> str:
    """Represent an absolute host path in the filesystem tool path format."""
    normalized = path.as_posix()
    if path.drive and not path.drive.startswith("\\\\"):
        return f"//?/{normalized}"
    return normalized


_EPHEMERAL_OFFLOAD_STORAGE = False
_UNIQUE_OFFLOAD_FALLBACK_ROOT: Path | None = None


def offload_storage_is_ephemeral() -> bool:
    """Return whether offload history is routed to non-persistent storage."""
    return _EPHEMERAL_OFFLOAD_STORAGE


def _harden_dir(path: Path) -> None:
    """Create `path` if needed and restrict it to the current user."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        msg = f"Path is not a directory: {path}"
        raise OSError(msg)
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        msg = f"Directory is owned by another user: {path}"
        raise PermissionError(msg)
    path.chmod(0o700)


def _probe_writable(path: Path) -> None:
    """Confirm `path` accepts new files (catches read-only mounts)."""
    with tempfile.NamedTemporaryFile(dir=path, prefix=".write-test-"):
        pass


def _artifacts_root() -> _ArtifactsStorage:
    """Return storage configuration for offloaded artifacts."""
    getuid = getattr(os, "getuid", None)
    suffix = str(getuid()) if getuid is not None else str(os.getpid())
    temp_root = Path(tempfile.gettempdir())
    root = temp_root / f"opscloud-artifacts-{suffix}"
    try:
        _harden_dir(root)
        _probe_writable(root)
    except (OSError, RuntimeError):
        logger.warning(
            "Predictable per-user artifacts directory is unavailable; routing "
            "large results from a stable virtual prefix to private temporary storage",
            exc_info=True,
        )
        unique = Path(
            tempfile.mkdtemp(prefix=f"opscloud-artifacts-{suffix}-", dir=temp_root)
        )
        _harden_dir(unique)
        _probe_writable(unique)
        return _ArtifactsStorage(
            root=_FALLBACK_ARTIFACTS_ROOT,
            large_results_dir=unique,
        )
    return _ArtifactsStorage(root=_filesystem_tool_path(root))


def _offload_fallback_root() -> Path:
    """Return a writable base directory for offloaded conversation history."""

    def _prepare_user_dir() -> Path:
        base = DATA_DIR
        base.mkdir(parents=True, exist_ok=True)
        archive_dir = base / "conversation_history"
        _harden_dir(archive_dir)
        _probe_writable(archive_dir)
        return base

    def _prepare_temp_dir(path: Path) -> Path:
        _harden_dir(path)
        _probe_writable(path)
        return path

    global _EPHEMERAL_OFFLOAD_STORAGE, _UNIQUE_OFFLOAD_FALLBACK_ROOT  # noqa: PLW0603
    if _UNIQUE_OFFLOAD_FALLBACK_ROOT is not None:
        _EPHEMERAL_OFFLOAD_STORAGE = True
        return _UNIQUE_OFFLOAD_FALLBACK_ROOT
    try:
        root = _prepare_user_dir()
    except (RuntimeError, OSError):
        logger.warning(
            "User data directory is not writable; falling back to temporary "
            "offload storage, which may not persist across restarts",
            exc_info=True,
        )
    else:
        _EPHEMERAL_OFFLOAD_STORAGE = False
        return root

    _EPHEMERAL_OFFLOAD_STORAGE = True
    getuid = getattr(os, "getuid", None)
    suffix = str(getuid()) if getuid is not None else str(os.getpid())
    temp_root = Path(tempfile.gettempdir())
    path = temp_root / f"opscloud-{suffix}"
    try:
        return _prepare_temp_dir(path)
    except (OSError, RuntimeError):
        logger.warning(
            "Per-user temporary offload directory is unavailable; creating "
            "a private unique directory",
            exc_info=True,
        )
        unique = Path(tempfile.mkdtemp(prefix=f"opscloud-{suffix}-", dir=temp_root))
        _UNIQUE_OFFLOAD_FALLBACK_ROOT = _prepare_temp_dir(unique)
        return _UNIQUE_OFFLOAD_FALLBACK_ROOT


def delete_offloaded_history(thread_id: str) -> bool:
    """Remove a thread's offloaded conversation-history archive."""
    if not thread_id:
        return False
    try:
        archive_dir = _offload_fallback_root() / "conversation_history"
    except (OSError, RuntimeError):
        logger.warning(
            "Could not resolve offload root to clean history for thread %s",
            thread_id,
            exc_info=True,
        )
        return False
    archive_path = archive_dir / f"{thread_id}.md"
    if archive_path.parent != archive_dir:
        logger.warning(
            "Refusing to delete offloaded history for suspicious thread id %r",
            thread_id,
        )
        return False
    try:
        archive_path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning(
            "Failed to delete offloaded conversation history for thread %s",
            thread_id,
            exc_info=True,
        )
        return False
    logger.debug("Deleted offloaded conversation history for thread %s", thread_id)
    return True
