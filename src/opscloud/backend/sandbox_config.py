"""Configuration model for sandboxed backend execution environments in OpsCloud."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any


@dataclass
class SandboxConfig:
    """Settings required to provision, configure, and connect to a sandbox environment."""

    provider: str = "local"
    """Target provider identifier ('local', 'langsmith', 'daytona', 'modal', 'runloop', 'vercel', 'agentcore')."""

    sandbox_id: str | None = None
    """Optional existing remote sandbox ID to reconnect to."""

    image: str | None = None
    """Optional container base image name or Dockerfile ref."""

    setup_script_path: str | None = None
    """Optional path to a shell script to run inside the sandbox on boot."""

    snapshot_name: str | None = None
    """Optional snapshot or blueprint name to boot from (supported by LangSmith/Runloop)."""

    sync_workspace: bool = True
    """Whether to automatically synchronize local project files into the sandbox (default True)."""

    providers: dict[str, Any] = field(default_factory=dict)
    """Optional provider-specific configurations parsed from [sandboxes.<provider>]."""

    @classmethod
    def load(cls, config_path: Path | None = None) -> SandboxConfig:
        """Load sandbox configuration with layered precedence: Env > TOML > Defaults."""
        from opscloud.config.toml_config import read_config_toml

        toml_data = read_config_toml(config_path)
        sandboxes_table = toml_data.get("sandboxes", {})
        if not isinstance(sandboxes_table, dict):
            sandboxes_table = {}

        # Provider / default
        env_provider = os.getenv("OPSCLOUD_SANDBOX_PROVIDER") or os.getenv("SANDBOX_PROVIDER")
        toml_provider = sandboxes_table.get("default") or sandboxes_table.get("provider")
        provider = env_provider or toml_provider or "local"

        # Sandbox ID
        env_sandbox_id = os.getenv("OPSCLOUD_SANDBOX_ID") or os.getenv("SANDBOX_ID")
        toml_sandbox_id = sandboxes_table.get("sandbox_id")
        sandbox_id = env_sandbox_id or toml_sandbox_id

        # Image
        env_image = os.getenv("OPSCLOUD_SANDBOX_IMAGE") or os.getenv("SANDBOX_IMAGE")
        toml_image = sandboxes_table.get("image")
        image = env_image or toml_image

        # Setup script path
        env_setup_script = os.getenv("OPSCLOUD_SANDBOX_SETUP_SCRIPT") or os.getenv("SANDBOX_SETUP_SCRIPT")
        toml_setup_script = sandboxes_table.get("setup_script_path")
        setup_script_path = env_setup_script or toml_setup_script

        # Snapshot name
        env_snapshot = os.getenv("OPSCLOUD_SANDBOX_SNAPSHOT") or os.getenv("SANDBOX_SNAPSHOT")
        toml_snapshot = sandboxes_table.get("snapshot_name") or sandboxes_table.get("snapshot")
        snapshot_name = env_snapshot or toml_snapshot

        # Sync workspace
        sync_raw = os.getenv("OPSCLOUD_SANDBOX_SYNC_WORKSPACE") or os.getenv("SANDBOX_SYNC_WORKSPACE")
        if sync_raw is not None:
            sync_workspace = sync_raw.lower() not in ("false", "0", "no")
        elif "sync_workspace" in sandboxes_table:
            val = sandboxes_table["sync_workspace"]
            sync_workspace = bool(val) if not isinstance(val, str) else val.lower() not in ("false", "0", "no")
        else:
            sync_workspace = True

        # Extract nested provider tables (e.g. [sandboxes.daytona])
        nested_providers: dict[str, Any] = {}
        for k, v in sandboxes_table.items():
            if isinstance(v, dict):
                nested_providers[k] = v

        provider_clean = str(provider).lower().strip()
        if provider_clean in ("none", ""):
            provider_clean = "local"

        return cls(
            provider=provider_clean,
            sandbox_id=str(sandbox_id) if sandbox_id else None,
            image=str(image) if image else None,
            setup_script_path=str(setup_script_path) if setup_script_path else None,
            snapshot_name=str(snapshot_name) if snapshot_name else None,
            sync_workspace=sync_workspace,
            providers=nested_providers,
        )

    @classmethod
    def from_env(cls) -> SandboxConfig:
        """Load sandbox configuration (delegates to load())."""
        return cls.load()


__all__ = ["SandboxConfig"]
