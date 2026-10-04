"""Stderr marker emission used by server and graph startup entry points in OpsCloud."""

from __future__ import annotations

import sys
import traceback

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

STARTUP_ERROR_MARKER = "OPSCLOUD_STARTUP_ERROR:"


def emit_startup_failure(exc: BaseException) -> None:
    """Report a server graph startup failure to the parent process.

    Emits full traceback for logs, then a single-line
    ``{STARTUP_ERROR_MARKER}{type}: {summary}`` line.
    """
    logger.critical("Failed to initialize server graph", exc_info=exc)
    print(
        f"Failed to initialize server graph: {exc}\n{traceback.format_exc()}",
        file=sys.stderr,
    )
    exc_lines = str(exc).splitlines()
    summary = exc_lines[0] if exc_lines else "<no message>"
    print(
        f"{STARTUP_ERROR_MARKER}{type(exc).__name__}: {summary}",
        file=sys.stderr,
    )


__all__ = [
    "STARTUP_ERROR_MARKER",
    "emit_startup_failure",
]
