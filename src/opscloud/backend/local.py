"""Local shell and filesystem execution backend for AWS operations.

Extends ``LocalShellBackend`` with a curated shell environment that preserves
AWS CLI, AWS CDK, Terraform/OpenTofu, Terragrunt, Kubernetes, and DevOps tool
credentials and environment configurations.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend as SDKLocalShellBackend

from opscloud.backend.registry import register_backend
from opscloud.config.paths import AWS_PRESERVE_ENV_VARS

# Tool directories to prepend to PATH for CLI tool discovery
COMMON_TOOL_DIRS: tuple[str, ...] = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/.cargo/bin",
    "~/.aws/bin",
)

# Prefix matching for AWS and Cloud IaC ecosystem tools
ALLOWED_ENV_PREFIXES: tuple[str, ...] = (
    "AWS_",
    "TF_VAR_",
    "TF_",
    "TERRAGRUNT_",
    "KUBE_",
    "HELM_",
    "CDK_",
    "PULUMI_",
    "SAM_",
)


def _build_shell_env() -> dict[str, str]:
    """Curate environment preserving safe shell vars, tool PATHs, and AWS/cloud credentials."""
    safe_keys = {
        "PATH",
        "HOME",
        "SHELL",
        "TERM",
        "LANG",
        "USER",
        "LOGNAME",
        "PWD",
        "EDITOR",
        "VISUAL",
        "LC_ALL",
        "LOCALE",
    }
    env: dict[str, str] = {}
    for key in safe_keys:
        if key in os.environ:
            env[key] = os.environ[key]

    # Prepend standard binary locations to PATH
    path_val = env.get("PATH", "")
    for tool_dir in COMMON_TOOL_DIRS:
        expanded = os.path.expanduser(tool_dir)
        if expanded not in path_val.split(os.pathsep):
            path_val = f"{expanded}{os.pathsep}{path_val}" if path_val else expanded
    env["PATH"] = path_val

    # Ensure AWS_PAGER is disabled by default so aws CLI commands do not hang waiting for paging
    if "AWS_PAGER" in os.environ:
        env["AWS_PAGER"] = os.environ["AWS_PAGER"]
    else:
        env["AWS_PAGER"] = ""

    # Prevent interactive terminal prompts in subshells (e.g. Git credentials, CLI pagers)
    env["GIT_TERMINAL_PROMPT"] = os.environ.get("GIT_TERMINAL_PROMPT", "0")
    if "PAGER" not in env:
        env["PAGER"] = os.environ.get("PAGER", "cat")

    # Preserve all AWS & Cloud IaC environment variables
    for key, val in os.environ.items():
        if key in AWS_PRESERVE_ENV_VARS or key.startswith(ALLOWED_ENV_PREFIXES):
            env[key] = val

    return env


@register_backend("local")
class LocalShellBackend(SDKLocalShellBackend):
    """Local shell and filesystem backend specialized for AWS DevOps/SRE operations."""

    def __init__(
        self,
        root_dir: Path | str,
        virtual_mode: bool = False,
        inherit_env: bool = False,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize LocalShellBackend with sanitized AWS cloud environment.

        Args:
            root_dir: Root directory for file and command operations.
            virtual_mode: Whether to restrict operations to virtual filesystem.
            inherit_env: Whether to inherit parent process environment variables.
            env: Explicit environment variables dictionary.
            **kwargs: Additional keyword arguments for SDKLocalShellBackend.
        """
        effective_env = env if env is not None else _build_shell_env()
        super().__init__(
            root_dir=Path(root_dir),
            virtual_mode=virtual_mode,
            inherit_env=inherit_env,
            env=effective_env,
            **kwargs,
        )

    def _sync_cloud_env(self) -> None:
        """Dynamically sync active AWS profile and credentials into the shell execution environment.

        Reads latest persisted cloud profile from config.toml, ambient AWS_PROFILE,
        and synchronizes AWS_PROFILE, AWS_DEFAULT_PROFILE, and region.
        When an explicit named profile is active, purges any static conflicting
        AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY from self._env so AWS CLI and boto3
        properly use the profile's authentic credentials rather than failing.
        """
        from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region

        active_profile = get_active_aws_profile()
        if active_profile:
            self._env["AWS_PROFILE"] = active_profile
            self._env["AWS_DEFAULT_PROFILE"] = active_profile
            os.environ["AWS_PROFILE"] = active_profile
            os.environ["AWS_DEFAULT_PROFILE"] = active_profile

            # Clear static credentials when a named profile is selected so AWS CLI
            # authentication precedence does not prioritize stale or unauthenticated env keys.
            if active_profile != "default":
                self._env.pop("AWS_ACCESS_KEY_ID", None)
                self._env.pop("AWS_SECRET_ACCESS_KEY", None)
                self._env.pop("AWS_SESSION_TOKEN", None)

        active_region = get_active_aws_region()
        if active_region:
            self._env["AWS_REGION"] = active_region
            self._env["AWS_DEFAULT_REGION"] = active_region
            os.environ["AWS_REGION"] = active_region
            os.environ["AWS_DEFAULT_REGION"] = active_region
        else:
            self._env.pop("AWS_REGION", None)
            self._env.pop("AWS_DEFAULT_REGION", None)
            os.environ.pop("AWS_REGION", None)
            os.environ.pop("AWS_DEFAULT_REGION", None)

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> Any:
        """Execute a shell command with dynamically synchronized cloud profile environment."""
        self._sync_cloud_env()
        return super().execute(command, timeout=timeout)

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> Any:
        """Asynchronously execute shell command with dynamically synchronized cloud profile environment."""
        self._sync_cloud_env()
        return await super().aexecute(command, timeout=timeout)


__all__ = [
    "ALLOWED_ENV_PREFIXES",
    "COMMON_TOOL_DIRS",
    "LocalShellBackend",
    "_build_shell_env",
]
