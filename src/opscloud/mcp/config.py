"""Validation and environment-variable expansion for MCP server config.

Resolves `${VAR}` and `${VAR:-default}` references in supported configuration
fields (`command`, `url`, `args`, `env`, `headers`) and validates their types.
A `${VAR:-default}` reference falls back to `default` when `VAR` is unset or empty (POSIX `:-` semantics).
"""

from __future__ import annotations

import copy
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^{}]*))?\}")
"""Matches a supported reference: `${VAR}` or `${VAR:-default}`.

Group 1 is the variable name; group 2 (present only for the `:-` form) is
the default. A bare `$VAR` and a literal `$` are intentionally not matched.
"""

_ENV_BRACE_RE = re.compile(r"\$\{")
"""Matches a `${` brace-open, used to catch malformed `${...}` references."""


def _interpolate_env(value: str, *, field: str) -> str:
    """Expand `${VAR}` / `${VAR:-default}` references in one config string.

    A bare `$VAR` (no braces) and a literal `$` pass through untouched;
    only the braced forms expand. `${VAR:-default}` uses `default` when
    `VAR` is unset or empty. A `${...}` that does not parse as one of the
    supported forms (e.g. `${VAR-default}` or an unterminated `${VAR`) is
    rejected rather than silently emitted, so a typo cannot inject a
    garbage value into a URL, command, or header.

    Args:
        value: Raw configuration string.
        field: Fully qualified field path for error messages.

    Returns:
        The interpolated string.

    Raises:
        RuntimeError: If a required environment variable is unset, or the
            string contains a malformed `${...}` reference.
    """

    def replace(match: re.Match[str]) -> str:
        """Resolve regex environment variable substitution match.

        Args:
            match: Regular expression match containing variable name and default.

        Returns:
            str: Resolved environment variable value or fallback default.

        Raises:
            RuntimeError: If variable is required and unset without a default.
        """
        name = match.group(1)
        default = match.group(2)
        resolved = os.environ.get(name)
        # A non-empty value always wins, for both `${VAR}` and `${VAR:-default}`.
        if resolved:
            if (resolved.startswith('"') and resolved.endswith('"')) or (
                resolved.startswith("'") and resolved.endswith("'")
            ):
                resolved = resolved[1:-1]
            return resolved
        # `resolved` is now "" (set but empty) or None (unset).
        if default is not None:
            # `${VAR:-default}`: `:-` falls back for empty *and* unset (POSIX).
            if (default.startswith('"') and default.endswith('"')) or (
                default.startswith("'") and default.endswith("'")
            ):
                default = default[1:-1]
            return default
        if resolved is not None:
            # `${VAR}` set to "": no default, so emit the empty value.
            return resolved
        # `${VAR}` unset with no default: the only hard error.
        msg = f"{field} references unset env var {name}. Set {name} in the environment or provide a default."
        raise RuntimeError(msg)

    # Reject any `${` that isn't the start of a well-formed reference.
    ref_spans = [match.span() for match in _ENV_REF_RE.finditer(value)]
    for brace in _ENV_BRACE_RE.finditer(value):
        if not any(start <= brace.start() < end for start, end in ref_spans):
            msg = f"{field} contains a malformed '${{...}}' reference. Use '${{VAR}}' or '${{VAR:-default}}'."
            raise RuntimeError(msg)

    return _ENV_REF_RE.sub(replace, value)


def _resolve_string(value: object, *, field: str) -> str:
    """Validate and interpolate one string field."""
    if not isinstance(value, str):
        msg = f"{field} must be a string, got {type(value).__name__}"
        raise TypeError(msg)
    return _interpolate_env(value, field=field)


def _resolve_mapping_values(
    values: Mapping[str, object],
    *,
    field: str,
) -> dict[str, str]:
    """Validate and interpolate string values in a mapping field."""
    result: dict[str, str] = {}
    for name, value in values.items():
        resolved_val = _resolve_string(value, field=f"{field}.{name}")
        if field.endswith("headers"):
            resolved_val = resolved_val.strip()
            if (resolved_val.startswith('"') and resolved_val.endswith('"')) or (
                resolved_val.startswith("'") and resolved_val.endswith("'")
            ):
                resolved_val = resolved_val[1:-1].strip()
        result[name] = resolved_val
    return result


def resolve_mcp_server_env(
    server_name: str,
    server_config: Mapping[str, object],
) -> dict[str, Any]:
    """Resolve `${VAR}` references in one MCP server's supported fields.

    Interpolates the `command`, `url`, `args`, `env`, and `headers`
    fields; every other field is copied through verbatim. The input is not mutated.

    Args:
        server_name: Server name used in field-specific error messages.
        server_config: Raw server configuration.

    Returns:
        A resolved copy of the server configuration.

    Raises:
        TypeError: If a supported field has the wrong type — a non-string
            scalar value, or `args`/`env`/`headers` with the wrong container type.
        RuntimeError: If a required environment variable is unset or reference is malformed.
    """
    resolved: dict[str, Any] = copy.deepcopy(dict(server_config))
    prefix = f"mcpServers.{server_name}"

    for name in ("command", "url"):
        if name in resolved and resolved[name] is not None:
            resolved[name] = _resolve_string(resolved[name], field=f"{prefix}.{name}")

    if "args" in resolved and resolved["args"] is not None:
        args = resolved["args"]
        if not isinstance(args, (list, tuple)):
            msg = f"{prefix}.args must be a list, got {type(args).__name__}"
            raise TypeError(msg)
        resolved["args"] = [_resolve_string(value, field=f"{prefix}.args[{index}]") for index, value in enumerate(args)]

    for name in ("env", "headers"):
        if name not in resolved or resolved[name] is None:
            continue
        values = resolved[name]
        if not isinstance(values, dict):
            msg = f"{prefix}.{name} must be a dictionary, got {type(values).__name__}"
            raise TypeError(msg)
        resolved[name] = _resolve_mapping_values(values, field=f"{prefix}.{name}")

    return resolved


def sync_mcp_env_aliases() -> None:
    """Harmonize environment variable aliases for MCP server compatibility.

    Ensures bidirectional synchronization between standard cloud/AWS config keys:
    - AWS_REGION <-> AWS_DEFAULT_REGION
    - AWS_PROFILE <-> AWS_DEFAULT_PROFILE
    - KUBECONFIG <-> K8S_KUBECONFIG
    """
    # AWS Region
    region = os.environ.get("AWS_REGION")
    default_region = os.environ.get("AWS_DEFAULT_REGION")
    if region and not default_region:
        os.environ["AWS_DEFAULT_REGION"] = region
    elif default_region and not region:
        os.environ["AWS_REGION"] = default_region

    # AWS Profile
    profile = os.environ.get("AWS_PROFILE")
    default_profile = os.environ.get("AWS_DEFAULT_PROFILE")
    if profile and not default_profile:
        os.environ["AWS_DEFAULT_PROFILE"] = profile
    elif default_profile and not profile:
        os.environ["AWS_PROFILE"] = default_profile

    # Kubeconfig
    kube = os.environ.get("KUBECONFIG")
    k8s_kube = os.environ.get("K8S_KUBECONFIG")
    if kube and not k8s_kube:
        os.environ["K8S_KUBECONFIG"] = kube
    elif k8s_kube and not kube:
        os.environ["KUBECONFIG"] = k8s_kube

