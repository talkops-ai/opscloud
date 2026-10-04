"""Trust store for skill directories that resolve outside trusted roots.

`load_skill_content` refuses to read a `SKILL.md` whose resolved path falls
outside every trusted skill root — this stops a symlink inside a skill
directory from reading arbitrary files. The static escape hatch is the
`OPSCLOUD_EXTRA_SKILLS_DIRS` env var / `[skills].extra_allowed_dirs`
config allowlist.

This module adds an in-the-moment, persistent approval path: when a skill
resolves outside the trusted roots, the user is asked once to allow the
resolved target directory, and the decision is remembered. Trust is keyed by
the approved target directory — the canonical path resolved and shown to the
user at approval time, stored as-is and never re-resolved.

Two distinct post-approval swaps are caught by two distinct layers, so neither
grants access the user never approved:

* Re-pointing the *discovery* symlink (the `SKILL.md` path) at a new target is
    caught by containment enforcement in `load_skill_content`: the new target is
    not on the allowlist, so the read is refused and the user is re-prompted.
    The stored trust entry — the original resolved target — is untouched.
* Replacing the *stored* directory itself (or one of its parents) with a symlink
    is caught by the `resolve()`-to-self re-verification in
    `load_trusted_skill_dirs`, which drops the stale entry rather than following
    the injected symlink to a directory the user never approved.

Trust entries are app-managed bookkeeping (a set of approved directories), not
user-facing configuration, so they live alongside the other state files under
`~/.opscloud/.state/skill_trust.json` rather than in the hand-editable
`config.toml`.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping

from opscloud.config import paths

logger = logging.getLogger(__name__)

_STORAGE_VERSION = 1
"""Schema version stamped into `skill_trust.json`; bump on incompatible changes."""


class _TrustEntry(TypedDict):
    """One trusted-directory record in the store's `dirs` map."""

    trusted_at: str
    """ISO-8601 UTC timestamp of when the directory was approved."""


class _TrustStore(TypedDict):
    """On-disk shape of `skill_trust.json`."""

    version: int
    """Schema version written to the store file."""

    dirs: dict[str, _TrustEntry]
    """Trusted directories keyed by their approved absolute path."""


class RevokeResult(Enum):
    """Outcome of a `revoke_skill_dir_trust` call.

    Distinguishing `NOT_FOUND` from `REMOVED` lets the CLI print an honest
    message instead of a false success when the target was never trusted.
    """

    REMOVED = "removed"
    """An entry existed and was removed from the store."""

    NOT_FOUND = "not_found"
    """No matching entry existed; the store was left unchanged."""

    ERROR = "error"
    """The store could not be read or the removal could not be persisted."""


def _default_store_path() -> Path:
    """Return ``~/.opscloud/.state/skill_trust.json``."""
    return paths.SKILL_TRUST_PATH


def _normalize(target_dir: Path | str) -> str:
    """Return the resolved absolute string form of a directory key."""
    return str(Path(target_dir).expanduser().resolve())


def _approved_key(target_dir: Path | str) -> str:
    """Return the already-approved directory key without resolving again."""
    return str(Path(target_dir).expanduser())


def _load_store(store_path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Read the JSON trust store file.

    Args:
        store_path: Path to the trust store file.
        strict: When `True`, a store that exists but cannot be read or parsed
            re-raises instead of degrading to `{}`. Read/modify/write callers
            pass `strict=True` so a transient read error aborts the write
            rather than silently rebuilding the store from an empty dict.

    Returns:
        Parsed JSON data, or an empty dict when missing/unreadable.
    """
    if not store_path.exists():
        return {}
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        if strict:
            raise
        logger.warning(
            "Skill trust store %s is corrupt; treating as empty: %s", store_path, exc
        )
        return {}
    except OSError as exc:
        if strict:
            raise
        logger.warning(
            "Could not read skill trust store %s; treating as empty: %s",
            store_path,
            exc,
        )
        return {}
    if not isinstance(data, dict):
        if strict:
            msg = f"Skill trust store {store_path} is not a JSON object"
            raise ValueError(msg)
        logger.warning(
            "Skill trust store %s is not a JSON object; ignoring", store_path
        )
        return {}
    version = data.get("version")
    if version is not None and (
        not isinstance(version, int) or version > _STORAGE_VERSION
    ):
        if strict:
            msg = (
                f"Skill trust store {store_path} has an unrecognized schema "
                f"version {version!r} (this build understands <= {_STORAGE_VERSION}); "
                f"refusing to read it"
            )
            raise ValueError(msg)
        logger.warning(
            "Skill trust store %s has an unrecognized schema version %r "
            "(this build understands <= %s); treating as empty",
            store_path,
            version,
            _STORAGE_VERSION,
        )
        return {}
    return data


def _save_store(data: Mapping[str, Any], store_path: Path) -> bool:
    """Atomic write of JSON trust data to `store_path` using mkstemp + Path.replace."""
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=store_path.parent, suffix=".tmp")
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
        try:
            with handle as f:
                json.dump(data, f, indent=2)
            Path(tmp_path).replace(store_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise
    except (OSError, ValueError):
        logger.exception("Failed to save skill trust store to %s", store_path)
        return False
    return True


def _read_dirs(store_path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Return the `dirs` mapping from the store, or an empty dict."""
    dirs = _load_store(store_path, strict=strict).get("dirs", {})
    return dirs if isinstance(dirs, dict) else {}


def is_skill_dir_trusted(
    target_dir: Path | str,
    *,
    store_path: Path | None = None,
) -> bool:
    """Check whether a resolved skill directory has been trusted."""
    if store_path is None:
        store_path = _default_store_path()
    return _normalize(target_dir) in _read_dirs(store_path)


def trust_skill_dir(
    target_dir: Path | str,
    *,
    store_path: Path | None = None,
) -> bool:
    """Persist trust for a resolved skill directory."""
    if store_path is None:
        store_path = _default_store_path()

    try:
        data = _load_store(store_path, strict=True)
    except (OSError, ValueError):
        logger.exception(
            "Refusing to persist skill trust: could not read existing store %s",
            store_path,
        )
        return False

    key = _approved_key(target_dir)
    try:
        is_canonical = key == _normalize(target_dir)
    except OSError:
        is_canonical = True
    if not is_canonical:
        logger.warning(
            "trust_skill_dir called with a non-canonical path %r; the stored "
            "entry will be dropped at read time. Pass an already-resolved "
            "directory.",
            target_dir,
        )

    dirs = data.get("dirs")
    if not isinstance(dirs, dict):
        dirs = {}
    dirs[key] = _TrustEntry(trusted_at=datetime.now(UTC).isoformat())
    return _save_store(_TrustStore(version=_STORAGE_VERSION, dirs=dirs), store_path)


def revoke_skill_dir_trust(
    target_dir: Path | str,
    *,
    store_path: Path | None = None,
) -> RevokeResult:
    """Remove trust for a skill directory.

    Matches on both the approved key form and the fully-resolved form.
    """
    if store_path is None:
        store_path = _default_store_path()

    try:
        data = _load_store(store_path, strict=True)
    except (OSError, ValueError):
        logger.exception(
            "Refusing to revoke skill trust: could not read existing store %s",
            store_path,
        )
        return RevokeResult.ERROR

    dirs = data.get("dirs")
    if not isinstance(dirs, dict):
        return RevokeResult.NOT_FOUND

    keys = {_approved_key(target_dir), _normalize(target_dir)}
    removed = False
    for key in keys:
        if key in dirs:
            del dirs[key]
            removed = True
    if not removed:
        return RevokeResult.NOT_FOUND

    data["version"] = _STORAGE_VERSION
    data["dirs"] = dirs
    return RevokeResult.REMOVED if _save_store(data, store_path) else RevokeResult.ERROR


def clear_trusted_skill_dirs(*, store_path: Path | None = None) -> bool:
    """Remove all trusted skill directories."""
    if store_path is None:
        store_path = _default_store_path()

    if not store_path.exists():
        return True
    return _save_store(_TrustStore(version=_STORAGE_VERSION, dirs={}), store_path)


def list_trusted_skill_dirs(
    *,
    store_path: Path | None = None,
    strict: bool = False,
) -> list[str]:
    """Return the sorted list of trusted skill directory paths."""
    if store_path is None:
        store_path = _default_store_path()
    return sorted(_read_dirs(store_path, strict=strict))


def list_trusted_skill_dir_entries(
    *,
    store_path: Path | None = None,
    strict: bool = False,
) -> list[tuple[str, str]]:
    """Return trusted directories paired with their approval timestamps."""
    if store_path is None:
        store_path = _default_store_path()
    entries: list[tuple[str, str]] = []
    for path, entry in _read_dirs(store_path, strict=strict).items():
        trusted_at = entry.get("trusted_at", "") if isinstance(entry, dict) else ""
        entries.append((path, trusted_at if isinstance(trusted_at, str) else ""))
    return sorted(entries)


def load_trusted_skill_dirs(*, store_path: Path | None = None) -> list[Path]:
    """Return verified trusted skill directories as canonical `Path` objects.

    Re-verifies `stored.resolve() == stored` so that post-approval symlink swaps
    are dropped rather than granting access to unintended paths.
    """
    verified: list[Path] = []
    for entry in list_trusted_skill_dirs(store_path=store_path):
        stored = Path(entry)
        try:
            resolves_to_self = stored.resolve() == stored
        except (OSError, RuntimeError):
            logger.warning(
                "Trusted skill directory %s could not be resolved; "
                "ignoring the trust entry.",
                entry,
                exc_info=True,
            )
            continue
        if resolves_to_self:
            verified.append(stored)
        else:
            logger.warning(
                "Trusted skill directory %s no longer resolves to itself "
                "(a symlink may have been introduced since approval); "
                "ignoring the stale trust entry.",
                entry,
            )
    return verified


class SkillTrustStore:
    """Backward compatibility wrapper around the persistent skill trust store."""

    def __init__(self, trust_file_path: Path | None = None) -> None:
        self.trust_file_path = trust_file_path or _default_store_path()

    def compute_hash(self, skill_path: Path) -> str:
        resolved = str(skill_path.expanduser().resolve())
        return hashlib.sha256(resolved.encode("utf-8")).hexdigest()

    def is_trusted(self, name: str, skill_path: Path) -> bool:
        try:
            resolved = skill_path.expanduser().resolve()
        except Exception:
            return False

        # Auto-trust built-in skills
        built_in_dir = paths.get_built_in_skills_dir()
        try:
            if resolved.is_relative_to(built_in_dir.resolve()):
                return True
        except Exception:
            pass

        # Auto-trust user home skills
        user_sd = paths.get_user_skills_dir("opscloud")
        try:
            if resolved.is_relative_to(user_sd.resolve()):
                return True
        except Exception:
            pass

        user_agents_dir = paths.get_user_agent_skills_dir()
        if user_agents_dir:
            try:
                if resolved.is_relative_to(user_agents_dir.resolve()):
                    return True
            except Exception:
                pass

        return is_skill_dir_trusted(resolved, store_path=self.trust_file_path)

    def trust_skill(self, name: str, skill_path: Path) -> None:
        try:
            resolved = skill_path.expanduser().resolve()
            trust_skill_dir(resolved, store_path=self.trust_file_path)
        except Exception as e:
            logger.warning("Could not trust skill path: %s", e)


__all__ = [
    "RevokeResult",
    "SkillTrustStore",
    "clear_trusted_skill_dirs",
    "is_skill_dir_trusted",
    "list_trusted_skill_dir_entries",
    "list_trusted_skill_dirs",
    "load_trusted_skill_dirs",
    "revoke_skill_dir_trust",
    "trust_skill_dir",
]
