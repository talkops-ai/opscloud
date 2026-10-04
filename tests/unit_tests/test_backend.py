"""Unit tests for OpsCloud backend execution, registry, and composite routing."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opscloud.backend.composite import (
    CompositeBackend,
    OpsCloudCompositeBackend,
)
from opscloud.backend.local import LocalShellBackend, _build_shell_env
from opscloud.backend.registry import (
    BackendRegistry,
    get_backend_class,
    get_backend_registry,
    register_backend,
)
from opscloud.backend.sandbox_config import SandboxConfig
from opscloud.backend.sandbox_factory import (
    WORKSPACE_IGNORE_DIRS,
    get_project_root,
    get_sandbox_backend,
    get_sandbox_working_dir,
)


class TestBackendRegistry:
    """Tests for BackendRegistry singleton, registration, and builder."""

    def test_singleton(self) -> None:
        r1 = get_backend_registry()
        r2 = get_backend_registry()
        assert r1 is r2

    def test_register_and_build(self) -> None:
        registry = BackendRegistry()

        class DummyBackend:
            def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
                self.kwargs = kwargs

        registry.register("dummy", DummyBackend)
        backend = registry.build("dummy", root_dir="/tmp/opscloud_test")
        assert isinstance(backend, DummyBackend)
        assert backend.kwargs["root_dir"] == "/tmp/opscloud_test"

    def test_build_unknown_raises(self) -> None:
        registry = BackendRegistry()
        with pytest.raises(ValueError, match="not registered"):
            registry.build("nonexistent_provider")

    def test_list_registered(self) -> None:
        registry = BackendRegistry()

        class Alpha:
            pass

        class Beta:
            pass

        registry.register("beta_backend", Beta)
        registry.register("alpha_backend", Alpha)
        assert registry.list_registered() == ["alpha_backend", "beta_backend"]

    def test_decorator_registers(self) -> None:
        registry = BackendRegistry()

        @register_backend("test_custom_provider")
        class CustomProvider:
            pass

        global_reg = get_backend_registry()
        assert "test_custom_provider" in global_reg.list_registered()
        assert get_backend_class("test_custom_provider") is CustomProvider


class TestLocalShellBackend:
    """Tests for LocalShellBackend AWS and DevOps environment handling."""

    def test_local_backend_registered(self) -> None:
        registry = get_backend_registry()
        assert "local" in registry.list_registered()

    def test_local_shell_backend_execute(self, tmp_path: Path) -> None:
        backend = LocalShellBackend(root_dir=tmp_path)
        res = backend.execute("echo 'hello opscloud'")
        assert res.exit_code == 0
        assert "hello opscloud" in res.output

    def test_aws_env_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE123456789")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretkey123456789")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "sessiontoken123")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("AWS_PROFILE", "production")
        monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/DevOpsRole")
        monkeypatch.setenv("AWS_SDK_LOAD_CONFIG", "1")

        env = _build_shell_env()
        assert env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE123456789"
        assert env["AWS_SECRET_ACCESS_KEY"] == "secretkey123456789"
        assert env["AWS_SESSION_TOKEN"] == "sessiontoken123"
        assert env["AWS_REGION"] == "us-west-2"
        assert env["AWS_PROFILE"] == "production"
        assert env["AWS_ROLE_ARN"] == "arn:aws:iam::123456789012:role/DevOpsRole"
        assert env["AWS_SDK_LOAD_CONFIG"] == "1"

    def test_aws_pager_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_PAGER", raising=False)
        env = _build_shell_env()
        assert env["AWS_PAGER"] == ""

    def test_aws_pager_preserved_if_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_PAGER", "cat")
        env = _build_shell_env()
        assert env["AWS_PAGER"] == "cat"

    def test_iac_prefix_env_captured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TF_VAR_environment", "staging")
        monkeypatch.setenv("TF_IN_AUTOMATION", "1")
        monkeypatch.setenv("TERRAGRUNT_TFPATH", "tofu")
        monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", "123456789012")
        monkeypatch.setenv("PULUMI_ACCESS_TOKEN", "pul-secret")
        monkeypatch.setenv("SAM_CLI_TELEMETRY", "0")
        monkeypatch.setenv("KUBECONFIG", "/custom/kube/config")

        env = _build_shell_env()
        assert env["TF_VAR_environment"] == "staging"
        assert env["TF_IN_AUTOMATION"] == "1"
        assert env["TERRAGRUNT_TFPATH"] == "tofu"
        assert env["CDK_DEFAULT_ACCOUNT"] == "123456789012"
        assert env["PULUMI_ACCESS_TOKEN"] == "pul-secret"
        assert env["SAM_CLI_TELEMETRY"] == "0"
        assert env["KUBECONFIG"] == "/custom/kube/config"

    def test_safe_keys_included(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/devops")
        monkeypatch.setenv("SHELL", "/bin/zsh")
        monkeypatch.setenv("USER", "devops")

        env = _build_shell_env()
        assert env["HOME"] == "/home/devops"
        assert env["SHELL"] == "/bin/zsh"
        assert env["USER"] == "devops"

    def test_path_prepend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        env = _build_shell_env()
        assert "/usr/local/bin" in env["PATH"]
        assert "/opt/homebrew/bin" in env["PATH"]

    def test_sensitive_env_not_leaked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SECRET_PASSWORD", "hunter2")
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pwd@db/prod")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_123456789")

        env = _build_shell_env()
        assert "SECRET_PASSWORD" not in env
        assert "DATABASE_URL" not in env
        assert "GITHUB_TOKEN" not in env

    def test_local_shell_backend_sync_cloud_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Verify _sync_cloud_env syncs active profile and region, clearing static keys for named profiles."""
        backend = LocalShellBackend(root_dir=tmp_path)
        backend._env["AWS_ACCESS_KEY_ID"] = "OLD_KEY"
        backend._env["AWS_SECRET_ACCESS_KEY"] = "OLD_SECRET"

        monkeypatch.setattr("opscloud.config.cloud_profiles.get_active_aws_profile", lambda: "prod_corp")
        monkeypatch.setattr("opscloud.config.cloud_profiles.get_active_aws_region", lambda: "eu-west-1")

        backend._sync_cloud_env()

        assert backend._env["AWS_PROFILE"] == "prod_corp"
        assert backend._env["AWS_DEFAULT_PROFILE"] == "prod_corp"
        assert backend._env["AWS_REGION"] == "eu-west-1"
        assert backend._env["AWS_DEFAULT_REGION"] == "eu-west-1"
        # Conflicting static credentials must be cleared for named profiles
        assert "AWS_ACCESS_KEY_ID" not in backend._env
        assert "AWS_SECRET_ACCESS_KEY" not in backend._env

        # Switching to profile without explicit region must clear stale region keys
        monkeypatch.setattr("opscloud.config.cloud_profiles.get_active_aws_region", lambda: None)
        backend._sync_cloud_env()
        assert "AWS_REGION" not in backend._env
        assert "AWS_DEFAULT_REGION" not in backend._env


class TestOpsCloudCompositeBackend:
    """Tests for OpsCloudCompositeBackend routing, cleanup, and compatibility."""

    def test_composite_backend_virtual_routing(self, tmp_path: Path) -> None:
        local_backend = LocalShellBackend(root_dir=tmp_path)
        composite = OpsCloudCompositeBackend(default=local_backend)

        # Write file to virtual /large_tool_results/ prefix
        write_res = composite.write("/large_tool_results/test.txt", "evidence data")
        assert write_res.error is None

        # Read back through composite backend
        read_res = composite.read("/large_tool_results/test.txt")
        assert read_res.file_data["content"] == "evidence data"

        # Write file to virtual /conversation_history/ prefix
        history_res = composite.write("/conversation_history/session_001.json", '{"history": []}')
        assert history_res.error is None
        read_hist = composite.read("/conversation_history/session_001.json")
        assert read_hist.file_data["content"] == '{"history": []}'

        composite.cleanup()

    def test_composite_cleanup(self) -> None:
        backend = OpsCloudCompositeBackend(default=MagicMock())
        temp_dir = backend._large_results_dir
        assert Path(temp_dir).exists()
        backend.cleanup()
        assert not Path(temp_dir).exists()

    def test_context_manager(self) -> None:
        with OpsCloudCompositeBackend(default=MagicMock()) as backend:
            temp_dir = backend._large_results_dir
            assert Path(temp_dir).exists()
        assert not Path(temp_dir).exists()

    def test_custom_routes_merged(self) -> None:
        custom_backend = MagicMock()
        backend = OpsCloudCompositeBackend(
            default=MagicMock(),
            routes={"/custom/": custom_backend},
        )
        assert "/custom/" in backend.routes
        assert "/large_tool_results/" in backend.routes
        assert "/conversation_history/" in backend.routes
        backend.cleanup()

    def test_aliases(self) -> None:
        assert CompositeBackend is OpsCloudCompositeBackend


class TestSandboxConfig:
    """Tests for SandboxConfig parsing and defaults."""

    def test_default_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPSCLOUD_SANDBOX_PROVIDER", raising=False)
        monkeypatch.delenv("SANDBOX_PROVIDER", raising=False)
        monkeypatch.delenv("OPSCLOUD_SANDBOX_SNAPSHOT", raising=False)
        monkeypatch.delenv("SANDBOX_SNAPSHOT", raising=False)

        cfg = SandboxConfig.from_env()
        assert cfg.provider == "local"
        assert cfg.sync_workspace is True

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPSCLOUD_SANDBOX_PROVIDER", "langsmith")
        monkeypatch.setenv("OPSCLOUD_SANDBOX_ID", "sb-12345")
        monkeypatch.setenv("OPSCLOUD_SANDBOX_IMAGE", "custom-aws-image:latest")
        monkeypatch.setenv("OPSCLOUD_SANDBOX_SETUP_SCRIPT", "/scripts/init.sh")
        monkeypatch.setenv("OPSCLOUD_SANDBOX_SNAPSHOT", "aws-devops-v1")
        monkeypatch.setenv("OPSCLOUD_SANDBOX_SYNC_WORKSPACE", "false")

        cfg = SandboxConfig.from_env()
        assert cfg.provider == "langsmith"
        assert cfg.sandbox_id == "sb-12345"
        assert cfg.image == "custom-aws-image:latest"
        assert cfg.setup_script_path == "/scripts/init.sh"
        assert cfg.snapshot_name == "aws-devops-v1"
        assert cfg.sync_workspace is False

    def test_toml_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPSCLOUD_SANDBOX_PROVIDER", raising=False)
        monkeypatch.delenv("SANDBOX_PROVIDER", raising=False)
        monkeypatch.delenv("OPSCLOUD_SANDBOX_SYNC_WORKSPACE", raising=False)
        monkeypatch.delenv("SANDBOX_SYNC_WORKSPACE", raising=False)

        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            """
[sandboxes]
default = "daytona"
image = "python:3.12-alpine"
sync_workspace = false

[sandboxes.daytona]
workspace_type = "docker"
"""
        )
        cfg = SandboxConfig.load(cfg_file)
        assert cfg.provider == "daytona"
        assert cfg.image == "python:3.12-alpine"
        assert cfg.sync_workspace is False
        assert "daytona" in cfg.providers
        assert cfg.providers["daytona"]["workspace_type"] == "docker"


class TestSandboxFactory:
    """Tests for sandbox factory, working dirs, and ignore lists."""

    def test_working_dirs(self) -> None:
        assert get_sandbox_working_dir("langsmith") == "/root"
        assert get_sandbox_working_dir("modal") == "/workspace"
        assert get_sandbox_working_dir("daytona") == "/home/daytona"
        assert get_sandbox_working_dir("runloop") == "/home/user"
        assert get_sandbox_working_dir("agentcore") == "/workspace"
        assert get_sandbox_working_dir("unknown") == "/workspace"

    def test_workspace_ignore_includes_iac(self) -> None:
        assert ".terraform" in WORKSPACE_IGNORE_DIRS
        assert ".terragrunt-cache" in WORKSPACE_IGNORE_DIRS
        assert "cdk.out" in WORKSPACE_IGNORE_DIRS
        assert ".git" in WORKSPACE_IGNORE_DIRS

    def test_get_sandbox_backend_local(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("OPSCLOUD_SANDBOX_PROVIDER", "local")
        monkeypatch.setattr("opscloud.backend.sandbox_factory.get_project_root", lambda: tmp_path)

        backend = get_sandbox_backend(thread_id="test_thread")
        assert isinstance(backend, LocalShellBackend)
        res = backend.execute("echo 'sandbox local test'")
        assert res.exit_code == 0
        assert "sandbox local test" in res.output
