"""Typed configuration for app-to-server subprocess communication."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opscloud._constants import DEFAULT_ASSISTANT_ID, SERVER_ENV_PREFIX


def _read_env_bool(suffix: str, *, default: bool = False) -> bool:
    raw = os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")
    if raw is None:
        return default
    return raw.lower() == "true"


def _read_env_str(suffix: str) -> str | None:
    return os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")


@dataclass(frozen=True)
class ServerConfig:
    """Configuration payload passed to the LangGraph server subprocess."""

    model: str | None = None
    assistant_id: str = DEFAULT_ASSISTANT_ID
    system_prompt: str | None = None
    auto_approve: bool = False
    interactive: bool = True
    enable_shell: bool = True
    cwd: str | None = None
    project_root: str | None = None
    approval_mode: str = "manual"
    shell_allow_list: tuple[str, ...] | None = None
    read_only: bool = False
    aws_profile: str | None = None
    aws_region: str | None = None
    mcp_config_path: str | None = None
    no_mcp: bool = False
    trust_project_mcp: bool = False
    server_log_level: str = "WARNING"

    def to_env(self) -> dict[str, str]:
        env: dict[str, str] = {
            f"{SERVER_ENV_PREFIX}ASSISTANT_ID": self.assistant_id,
            f"{SERVER_ENV_PREFIX}AUTO_APPROVE": str(self.auto_approve).lower(),
            f"{SERVER_ENV_PREFIX}INTERACTIVE": str(self.interactive).lower(),
            f"{SERVER_ENV_PREFIX}ENABLE_SHELL": str(self.enable_shell).lower(),
            f"{SERVER_ENV_PREFIX}APPROVAL_MODE": self.approval_mode,
            f"{SERVER_ENV_PREFIX}READ_ONLY": str(self.read_only).lower(),
            f"{SERVER_ENV_PREFIX}NO_MCP": str(self.no_mcp).lower(),
            f"{SERVER_ENV_PREFIX}TRUST_PROJECT_MCP": str(self.trust_project_mcp).lower(),
            f"{SERVER_ENV_PREFIX}LOG_LEVEL": self.server_log_level,
        }
        if self.model:
            env[f"{SERVER_ENV_PREFIX}MODEL"] = self.model
        if self.system_prompt:
            env[f"{SERVER_ENV_PREFIX}SYSTEM_PROMPT"] = self.system_prompt
        effective_cwd = self.cwd or str(Path.cwd().resolve())
        env[f"{SERVER_ENV_PREFIX}CWD"] = effective_cwd
        from opscloud.config.paths import find_project_root

        resolved_root = self.project_root or find_project_root(effective_cwd)
        if resolved_root:
            env[f"{SERVER_ENV_PREFIX}PROJECT_ROOT"] = str(resolved_root)
        if self.shell_allow_list:
            env[f"{SERVER_ENV_PREFIX}SHELL_ALLOW_LIST"] = ",".join(self.shell_allow_list)
        if self.aws_profile:
            env[f"{SERVER_ENV_PREFIX}AWS_PROFILE"] = self.aws_profile
        if self.aws_region:
            env[f"{SERVER_ENV_PREFIX}AWS_REGION"] = self.aws_region
        if self.mcp_config_path:
            env[f"{SERVER_ENV_PREFIX}MCP_CONFIG_PATH"] = self.mcp_config_path
        return env

    @classmethod
    def from_env(cls) -> ServerConfig:
        raw_sal = _read_env_str("SHELL_ALLOW_LIST")
        shell_allow_list = tuple(s.strip() for s in raw_sal.split(",") if s.strip()) if raw_sal else None
        server_log_level = _read_env_str("LOG_LEVEL") or "WARNING"
        return cls(
            model=_read_env_str("MODEL"),
            assistant_id=_read_env_str("ASSISTANT_ID") or DEFAULT_ASSISTANT_ID,
            system_prompt=_read_env_str("SYSTEM_PROMPT"),
            auto_approve=_read_env_bool("AUTO_APPROVE", default=False),
            interactive=_read_env_bool("INTERACTIVE", default=True),
            enable_shell=_read_env_bool("ENABLE_SHELL", default=True),
            cwd=_read_env_str("CWD"),
            project_root=_read_env_str("PROJECT_ROOT"),
            approval_mode=_read_env_str("APPROVAL_MODE") or "manual",
            shell_allow_list=shell_allow_list,
            read_only=_read_env_bool("READ_ONLY", default=False),
            aws_profile=_read_env_str("AWS_PROFILE"),
            aws_region=_read_env_str("AWS_REGION"),
            mcp_config_path=_read_env_str("MCP_CONFIG_PATH"),
            no_mcp=_read_env_bool("NO_MCP", default=False),
            trust_project_mcp=_read_env_bool("TRUST_PROJECT_MCP", default=False),
            server_log_level=server_log_level,
        )

    @classmethod
    def from_cli_args(
        cls,
        *,
        project_context: Any = None,
        model_name: str | None = None,
        model: str | None = None,
        assistant_id: str = DEFAULT_ASSISTANT_ID,
        system_prompt: str | None = None,
        auto_approve: bool = False,
        interactive: bool = True,
        enable_shell: bool = True,
        cwd: str | None = None,
        project_root: str | None = None,
        approval_mode: str = "manual",
        shell_allow_list: tuple[str, ...] | list[str] | None = None,
        read_only: bool = False,
        aws_profile: str | None = None,
        aws_region: str | None = None,
        mcp_config_path: str | None = None,
        no_mcp: bool = False,
        trust_project_mcp: bool = False,
        server_log_level: str = "WARNING",
    ) -> ServerConfig:
        resolved_cwd = (
            str(project_context.user_cwd)
            if project_context is not None
            else (cwd or str(Path.cwd().resolve()))
        )
        resolved_project_root = (
            str(project_context.project_root)
            if project_context is not None and project_context.project_root is not None
            else project_root
        )
        sal = tuple(shell_allow_list) if shell_allow_list else None
        return cls(
            model=model or model_name,
            assistant_id=assistant_id,
            system_prompt=system_prompt,
            auto_approve=auto_approve,
            interactive=interactive,
            enable_shell=enable_shell,
            cwd=resolved_cwd,
            project_root=resolved_project_root,
            approval_mode=approval_mode,
            shell_allow_list=sal,
            read_only=read_only,
            aws_profile=aws_profile,
            aws_region=aws_region,
            mcp_config_path=mcp_config_path,
            no_mcp=no_mcp,
            trust_project_mcp=trust_project_mcp,
            server_log_level=server_log_level,
        )

