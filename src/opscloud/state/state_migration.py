"""One-time migration of legacy state files into `~/.opscloud/.state/`.

Earlier prototypes wrote internal database and auth state directly under
`~/.opscloud/`. State now lives in a dedicated `.state/` directory; this
module moves legacy files into place on startup idempotently.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from opscloud.config.paths import DATA_DIR, STATE_DIR

logger = logging.getLogger(__name__)

_LEGACY_STATE_FILES: tuple[str, ...] = (
    "sessions.db",
    "sessions.db-wal",
    "sessions.db-shm",
    "history.jsonl",
    "auth.json",
    "recent_models.json",
    "mcp_trust.json",
)


def _iter_migrations(
    config_dir: Path,
    state_dir: Path,
    names: Iterable[str],
) -> Iterable[tuple[Path, Path]]:
    for name in names:
        yield config_dir / name, state_dir / name


def migrate_legacy_state(
    *,
    config_dir: Path = DATA_DIR,
    state_dir: Path = STATE_DIR,
) -> None:
    """Move legacy state entries from `config_dir` into `state_dir`.

    Idempotent: skips entries whose destination already exists or whose source
    does not exist. Errors on individual entries are logged and swallowed so
    an unmovable file never prevents startup.
    """
    try:
        if not config_dir.is_dir():
            return
    except OSError:
        logger.debug("Could not stat %s; skipping state migration", config_dir, exc_info=True)
        return

    pending: list[tuple[Path, Path]] = []
    for src, dst in _iter_migrations(config_dir, state_dir, _LEGACY_STATE_FILES):
        try:
            if not src.exists():
                continue
            if dst.exists():
                continue
            pending.append((src, dst))
        except OSError:
            continue

    if not pending:
        return

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Could not create state directory %s; skipping migration", state_dir)
        return

    for src, dst in pending:
        try:
            src.rename(dst)
            logger.info("Migrated legacy state %s -> %s", src, dst)
        except OSError as exc:
            logger.warning("Failed to migrate %s -> %s: %s", src, dst, exc)


__all__ = [
    "migrate_legacy_state",
]
