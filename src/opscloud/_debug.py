"""Shared debug-logging configuration for verbose file-based tracing.

When the `OPSCODE_DEBUG` environment variable is set, modules that handle
streaming or remote communication can enable detailed file-based logging. This
helper centralizes the setup so the env-var name, file path, and format are
defined in one place.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from opscloud.config.env_vars import (
    DEBUG,
    DEBUG_FILE,
    DEFAULT_DEBUG_FILE,
    LOG_LEVEL,
    is_env_truthy,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_DEBUG_HANDLER_ATTR = "_opscloud_debug_handler"
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
"""Canonical level-name to `logging` level mapping.

The single source of truth for level names and their numeric values, shared with
the Debug Console's level filter so severity ordering is never re-derived from
hardcoded integers.
"""


def resolve_log_level(*, debug_enabled: bool | None = None) -> int:
    """Resolve the configured runtime logging level.

    Args:
        debug_enabled: Whether `OPSCODE_DEBUG` is truthy. When omitted,
            the current environment is checked.

    Returns:
        A standard `logging` level integer. Defaults to `DEBUG` when debug file
        logging is enabled and `INFO` otherwise.
    """
    if debug_enabled is None:
        debug_enabled = is_env_truthy(DEBUG)
    fallback = logging.DEBUG if debug_enabled else logging.INFO
    raw = os.environ.get(LOG_LEVEL)
    if raw is None or not raw.strip():
        return fallback
    level = LOG_LEVELS.get(raw.strip().upper())
    if level is not None:
        return level
    valid = ", ".join(LOG_LEVELS)
    message = f"ignoring invalid {LOG_LEVEL}={raw!r}; expected one of {valid}"
    # stderr for headless / pre-TUI visibility; the logger so it also lands in
    # the always-on in-memory buffer and surfaces in the Debug Console.
    print(f"Warning: {message}", file=sys.stderr)  # noqa: T201
    logger.warning("%s", message)
    return fallback


def configure_debug_logging(target: logging.Logger) -> None:
    """Attach a file handler to *target* when `OPSCODE_DEBUG` is set.

    Intended to be called once on the `opscloud` package logger; child
    module loggers reach the same file via propagation, so individual modules do
    not configure logging themselves.

    The log file defaults to `DEFAULT_DEBUG_FILE` but can be overridden with
    `OPSCODE_DEBUG_FILE`. The handler appends (`mode='a'`) so logs
    are preserved across separate process runs. Calling this again with the same
    resolved path is a no-op: the existing tagged handler is reused rather than
    stacking duplicates. If the resolved path changes, the stale handler is
    closed and replaced.

    Does nothing when `OPSCODE_DEBUG` is not truthy (see `is_env_truthy`).

    Args:
        target: Logger to configure.
    """
    debug_enabled = is_env_truthy(DEBUG)
    level = resolve_log_level(debug_enabled=debug_enabled)
    target.setLevel(level)

    if not debug_enabled:
        return

    debug_path = Path(os.environ.get(DEBUG_FILE, DEFAULT_DEBUG_FILE))
    for existing in list(target.handlers):
        if not (
            isinstance(existing, logging.FileHandler)
            and getattr(existing, _DEBUG_HANDLER_ATTR, False)
        ):
            continue
        if Path(existing.baseFilename) == debug_path:
            # Already configured for this path; reuse rather than duplicate.
            existing.setLevel(level)
            return
        # The debug path changed; drop the stale handler before re-attaching so
        # we don't leak its file descriptor or fan logs out to two files.
        target.removeHandler(existing)
        existing.close()

    try:
        handler = logging.FileHandler(str(debug_path), mode="a")
    except OSError as exc:
        print(  # noqa: T201
            f"Warning: could not open debug log file {debug_path}: {exc}",
            file=sys.stderr,
        )
        return
    setattr(handler, _DEBUG_HANDLER_ATTR, True)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    target.addHandler(handler)


def installed_debug_log_path() -> Path | None:
    """Return the path of the active debug log file, or `None` if not logging.

    Reflects the file handler actually attached by `configure_debug_logging`,
    not the current `OPSCODE_DEBUG` env value. The two diverge when the
    variable is set after import — e.g. via a project/global `.env` loaded during
    settings bootstrap — in which case the variable reads truthy but no handler
    was installed and no log file exists. Callers that surface "full error in
    <path>" hints must use this rather than the env var to avoid pointing users
    at a file that was never created.
    """
    package_logger = logging.getLogger(__package__ or "opscloud")
    for handler in package_logger.handlers:
        if isinstance(handler, logging.FileHandler) and getattr(
            handler, _DEBUG_HANDLER_ATTR, False
        ):
            return Path(handler.baseFilename)
    return None
