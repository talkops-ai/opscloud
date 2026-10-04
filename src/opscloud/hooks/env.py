"""Sanitized subprocess environments for Hooks v2 command handlers."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "APIKEY", "CREDENTIAL")


def is_secret_env_var(name: str) -> bool:
    """Return whether an environment variable name carries secret material.

    Args:
        name: Name of the environment variable.

    Returns:
        True if the name contains markers indicating a key, token, secret,
        password, or credential.
    """
    upper = name.upper()
    return any(marker in upper for marker in _SECRET_NAME_MARKERS)


def sanitize_hook_environ(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an inherited environment safe to pass to hook subprocesses.

    Strips values whose names look like secrets (e.g. AWS_SECRET_ACCESS_KEY,
    OPENAI_API_KEY, ANTHROPIC_API_KEY). Hooks are user-authored code, but
    ambient credentials should not be exposed to child processes unnecessarily.

    Args:
        source: Environment mapping to sanitize. Defaults to `os.environ`.

    Returns:
        A new sanitized dictionary containing only non-secret environment variables.
    """
    env = os.environ if source is None else source
    return {key: value for key, value in env.items() if not is_secret_env_var(key)}
