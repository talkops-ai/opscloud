"""Lightweight git metadata helpers for state detection and repository inspection in OpsCloud."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import re
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

logger = get_logger(__name__)

_GIT_DIR_POINTER_PREFIX = "gitdir: "
"""Prefix used by worktree-style `.git` files to point at the real git dir."""

_GIT_HEAD_REF_PREFIX = "ref: "
"""Prefix used by `HEAD` when it points at a named ref instead of a commit."""

_GIT_REF_PREFIXES = ("refs/heads/", "refs/remotes/", "refs/tags/", "refs/")
"""Known git ref prefixes stripped when formatting a branch-like display name."""

_git_dir_cache: dict[str, Path] = {}
"""Positive-only cache of resolved git metadata directories keyed by lookup path."""


def _abbreviate_git_ref(ref: str) -> str:
    """Convert a full git ref into a short display name."""
    for prefix in _GIT_REF_PREFIXES:
        if ref.startswith(prefix):
            return ref.removeprefix(prefix)
    return ref


def _parse_git_dir_pointer(git_entry: Path) -> Path | None:
    """Resolve a `.git` file containing a `gitdir:` pointer."""
    try:
        raw = git_entry.read_text(encoding="utf-8").strip()
    except OSError:
        logger.debug("Failed to read gitdir pointer from %s", git_entry, exc_info=True)
        return None

    if not raw.startswith(_GIT_DIR_POINTER_PREFIX):
        return None

    pointer = raw.removeprefix(_GIT_DIR_POINTER_PREFIX).strip()
    if not pointer:
        return None

    git_dir = Path(pointer)
    if not git_dir.is_absolute():
        git_dir = git_entry.parent / git_dir
    return git_dir.resolve(strict=False)


def _normalize_lookup_path(path: str | Path) -> Path:
    """Normalize a lookup path for git metadata discovery."""
    try:
        return Path(path).expanduser().resolve(strict=False)
    except OSError:
        return Path(path).expanduser()


def _find_git_dir_uncached(path: Path) -> Path | None:
    """Locate the effective git metadata directory without using caches."""
    current = path
    if not current.is_dir():
        current = current.parent

    for directory in (current, *current.parents):
        git_entry = directory / ".git"
        if git_entry.is_dir():
            return git_entry
        if git_entry.is_file():
            git_dir = _parse_git_dir_pointer(git_entry)
            if git_dir is not None and git_dir.is_dir():
                return git_dir
            return None

    return None


def find_git_dir(path: str | Path) -> Path | None:
    """Locate the effective git metadata directory with positive caching."""
    normalized = _normalize_lookup_path(path)
    key = str(normalized)
    cached = _git_dir_cache.get(key)
    if cached is not None and cached.is_dir():
        return cached

    git_dir = _find_git_dir_uncached(normalized)
    if git_dir is not None:
        _git_dir_cache[key] = git_dir
    return git_dir


def find_git_root(path: str | Path) -> Path | None:
    """Locate the repository root for a path."""
    current = _normalize_lookup_path(path)
    if not current.is_dir():
        current = current.parent

    for directory in (current, *current.parents):
        git_entry = directory / ".git"
        if git_entry.is_dir():
            return directory
        if git_entry.is_file():
            git_dir = _parse_git_dir_pointer(git_entry)
            if git_dir is not None and git_dir.is_dir():
                return directory
            return None

    return None


def read_git_branch_from_filesystem(path: str | Path) -> str | None:
    """Read the current git branch from repository metadata."""
    git_dir = find_git_dir(path)
    if git_dir is None:
        return ""

    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.debug("Git HEAD file not found in %s", git_dir)
        return None
    except OSError:
        logger.debug("Failed to read git HEAD from %s", git_dir, exc_info=True)
        return None

    if not head:
        return ""
    if head.startswith(_GIT_HEAD_REF_PREFIX):
        ref = head.removeprefix(_GIT_HEAD_REF_PREFIX).strip()
        return _abbreviate_git_ref(ref) if ref else None
    return "HEAD"


def read_git_branch_via_subprocess(path: str | Path) -> str:
    """Fall back to `git rev-parse` for unusual repository layouts."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            cwd=path,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        logger.debug("Git branch detection timed out")
    except OSError:
        logger.debug("Git branch detection failed", exc_info=True)
    return ""


def resolve_git_branch(path: str | Path) -> str:
    """Resolve the current git branch with a filesystem-first strategy."""
    branch = read_git_branch_from_filesystem(path)
    if branch is not None:
        return branch
    return read_git_branch_via_subprocess(path)


_GIT_SHA_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
"""Matches a full 40-char SHA-1 (or 64-char SHA-256) git object id."""


def read_git_commit_sha_from_filesystem(path: str | Path) -> str | None:
    """Read the current HEAD commit SHA from repository metadata."""
    git_dir = find_git_dir(path)
    if git_dir is None:
        return ""

    head_path = git_dir / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.debug("Git HEAD file not found in %s", git_dir)
        return None
    except OSError:
        logger.debug("Failed to read git HEAD from %s", git_dir, exc_info=True)
        return None

    if not head:
        return None
    if not head.startswith(_GIT_HEAD_REF_PREFIX):
        return head if _GIT_SHA_RE.match(head) else None

    ref = head.removeprefix(_GIT_HEAD_REF_PREFIX).strip()
    if not ref:
        return None

    loose_ref = git_dir / Path(ref)
    try:
        sha = loose_ref.read_text(encoding="utf-8").strip()
        if _GIT_SHA_RE.match(sha):
            return sha
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("Failed to read loose ref %s", loose_ref, exc_info=True)
        return None

    return _read_packed_ref(git_dir, ref)


def _read_packed_ref(git_dir: Path, ref: str) -> str | None:
    """Resolve a ref to its SHA from packed-refs."""
    try:
        packed = (git_dir / "packed-refs").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.debug("Failed to read packed-refs in %s", git_dir, exc_info=True)
        return None

    for line in packed.splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _GIT_SHA_RE.match(sha):
            return sha
    return None


def read_git_commit_sha_via_subprocess(path: str | Path) -> str:
    """Fall back to git rev-parse HEAD for unusual repository layouts."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            cwd=path,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        logger.debug("Git commit detection timed out")
    except OSError:
        logger.debug("Git commit detection failed", exc_info=True)
    return ""


def resolve_git_commit_sha(path: str | Path) -> str:
    """Resolve the current HEAD commit SHA, filesystem-first."""
    sha = read_git_commit_sha_from_filesystem(path)
    if sha is not None:
        return sha
    return read_git_commit_sha_via_subprocess(path)


def read_git_remote_url_from_filesystem(path: str | Path) -> str | None:
    """Read the origin remote URL from the repository config file."""
    git_dir = find_git_dir(path)
    if git_dir is None:
        return ""

    try:
        raw = (git_dir / "config").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.debug("Failed to read git config in %s", git_dir, exc_info=True)
        return None

    in_origin = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_origin = stripped.replace(" ", "").lower() == '[remote"origin"]'
            continue
        if in_origin and stripped.lower().startswith("url"):
            _, _, value = stripped.partition("=")
            url = value.strip()
            if url:
                return url
    return None


def read_git_remote_url_via_subprocess(path: str | Path) -> str:
    """Fall back to git config --get remote.origin.url."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            cwd=path,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        logger.debug("Git remote detection timed out")
    except OSError:
        logger.debug("Git remote detection failed", exc_info=True)
    return ""


def resolve_git_remote_url(path: str | Path) -> str:
    """Resolve the origin remote URL, filesystem-first."""
    url = read_git_remote_url_from_filesystem(path)
    if url is not None:
        return url
    return read_git_remote_url_via_subprocess(path)


_REPO_PROVIDERS: dict[str, str] = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
}


class RepositoryMetadata(NamedTuple):
    """Parsed origin remote attribution."""

    url: str
    provider: str
    name: str


def parse_repository_metadata(remote_url: str) -> RepositoryMetadata | None:
    """Derive repository attribution from an origin remote URL."""
    url = (remote_url or "").strip()
    if not url:
        return None

    host: str
    repo_path: str
    if "://" in url:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        repo_path = parsed.path.lstrip("/")
    else:
        userhost, sep, repo_path = url.partition(":")
        if not sep:
            return None
        host = userhost.rsplit("@", 1)[-1].lower()
        repo_path = repo_path.lstrip("/")

    repo_path = repo_path.strip("/").removesuffix(".git").strip("/")
    if not host or not repo_path:
        return None

    provider = _REPO_PROVIDERS.get(host, "other")
    normalized_url = f"https://{host}/{repo_path}"
    return RepositoryMetadata(normalized_url, provider, repo_path)


# Aliases for backward compatibility
get_git_branch = resolve_git_branch
get_git_remote_url = resolve_git_remote_url
get_git_root = find_git_root

__all__ = [
    "RepositoryMetadata",
    "find_git_dir",
    "find_git_root",
    "get_git_branch",
    "get_git_remote_url",
    "get_git_root",
    "parse_repository_metadata",
    "read_git_branch_from_filesystem",
    "read_git_branch_via_subprocess",
    "read_git_commit_sha_from_filesystem",
    "read_git_commit_sha_via_subprocess",
    "read_git_remote_url_from_filesystem",
    "read_git_remote_url_via_subprocess",
    "resolve_git_branch",
    "resolve_git_commit_sha",
    "resolve_git_remote_url",
]
