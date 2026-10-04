"""Config store factory — auto-detects postgres vs sqlite backend.

Uses the same connection-detection pattern as the checkpointer
(``state/checkpointer.py``): if ``OPSCLOUD_POSTGRES_URI`` or
``DATABASE_URL`` is set, use PostgreSQL; otherwise fall back to SQLite.
"""

from __future__ import annotations

import os

from opscloud.config.store import ConfigStore
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def _has_postgres_config() -> bool:
    """Check if PostgreSQL connection is configured."""
    return bool(
        os.environ.get("POSTGRES_URI") or os.environ.get("OPSCLOUD_POSTGRES_URI") or os.environ.get("DATABASE_URL")
    )


async def create_config_store(backend: str = "auto") -> ConfigStore:
    """Create and initialize a ConfigStore.

    Args:
        backend: ``"auto"`` (detect from env), ``"postgres"``, or ``"sqlite"``.

    Returns:
        Initialized ``ConfigStore`` instance ready for reads/writes.
    """
    if backend == "auto":
        backend = "postgres" if _has_postgres_config() else "sqlite"

    if backend == "postgres":
        from opscloud.config.adapters.postgres import PostgresConfigAdapter

        uri = (
            os.environ.get("POSTGRES_URI")
            or os.environ.get("OPSCLOUD_POSTGRES_URI")
            or os.environ.get("DATABASE_URL", "")
        )
        adapter = PostgresConfigAdapter(uri)

    elif backend == "sqlite":
        from opscloud.config.adapters.sqlite import SqliteConfigAdapter
        from opscloud.config.paths import STATE_DIR

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        db_path = STATE_DIR / "config.db"
        adapter = SqliteConfigAdapter(db_path)

    else:
        raise ValueError(f"Unknown config store backend: {backend!r}")

    store = ConfigStore(adapter)
    await store.initialize()
    logger.info("ConfigStore created with %s backend", backend)
    return store


def create_config_store_sync(backend: str = "auto") -> ConfigStore:
    """Create a ConfigStore synchronously with auto-detected backend.

    Args:
        backend: ``"auto"`` (detect from env), ``"postgres"``, or ``"sqlite"``.

    Returns:
        ``ConfigStore`` instance with auto-detected adapter.
    """
    if backend == "auto":
        backend = "postgres" if _has_postgres_config() else "sqlite"

    if backend == "postgres":
        from opscloud.config.adapters.postgres import PostgresConfigAdapter

        uri = (
            os.environ.get("POSTGRES_URI")
            or os.environ.get("OPSCLOUD_POSTGRES_URI")
            or os.environ.get("DATABASE_URL", "")
        )
        adapter = PostgresConfigAdapter(uri)

    elif backend == "sqlite":
        from opscloud.config.adapters.sqlite import SqliteConfigAdapter
        from opscloud.config.paths import STATE_DIR

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        db_path = STATE_DIR / "config.db"
        adapter = SqliteConfigAdapter(db_path)

    else:
        raise ValueError(f"Unknown config store backend: {backend!r}")

    return ConfigStore(adapter)
