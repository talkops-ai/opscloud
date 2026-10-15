"""Unit tests for CLI settings management and startup workspace folder trust."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from opscloud.config.cli_settings import (
    CliSetting,
    CliSettingsManager,
    PermissionsSetting,
    SettingsMalformedError,
    SettingsValidationError,
    atomic_write_settings,
    get_cli_settings_manager,
    merge_cli_settings,
    parse_settings_fields,
    reset_cli_settings_manager_for_testing,
    resolve_path_no_slash,
    validate_cli_settings,
)
from opscloud.ui.app import OpsCloudApp
from opscloud.ui.widgets.workspace_trust import (
    SettingsErrorScreen,
    WorkspaceTrustScreen,
)


def _patch_startup_heavy_ops():
    """Patch heavy background services during TUI mount tests."""
    return (
        patch.object(OpsCloudApp, "_start_server_background", new_callable=AsyncMock),
        patch.object(OpsCloudApp, "_discover_startup_skills", new_callable=AsyncMock),
        patch.object(OpsCloudApp, "_preload_mcp_metadata_background", new_callable=AsyncMock),
    )


class TestResolvePathNoSlash:
    """Tests for resolve_path_no_slash canonical path normalization."""

    def test_strips_trailing_slash(self, tmp_path: Path):
        folder = tmp_path / "my_project"
        folder.mkdir()
        resolved = resolve_path_no_slash(f"{folder}/")
        assert not resolved.endswith("/")
        assert resolved == str(folder)

    def test_preserves_root_slash(self):
        assert resolve_path_no_slash("/") == "/"

    def test_expands_tilde_and_normalizes_dot_segments(self):
        home = str(Path.home())
        assert resolve_path_no_slash("~") == home
        assert resolve_path_no_slash("~/my_repo/../my_repo/") == f"{home}/my_repo"


class TestCliSettingParseValidateMarshalMerge:
    """Tests for CliSetting schema parsing, validation, sparse marshaling, and 3-way merge."""

    def test_marshal_sparse_omits_defaults_and_preserves_unknown_fields(self):
        setting = CliSetting()
        # Default setting marshals to empty JSON object + trailing newline
        raw = setting.marshal_sparse().decode("utf-8")
        assert json.loads(raw) == {}
        assert raw.endswith("\n")

        # Setting non-default fields and unknown fields
        setting.color_scheme = "Tokyo Night"
        setting.trusted_workspaces = ["/Users/test/project"]
        setting.editor = "terminal"  # default -> omitted
        setting.unknown_fields = {"customExperimentalFlag": True}

        parsed = json.loads(setting.marshal_sparse().decode("utf-8"))
        assert parsed == {
            "colorScheme": "Tokyo Night",
            "trustedWorkspaces": ["/Users/test/project"],
            "customExperimentalFlag": True,
        }
        assert "editor" not in parsed

    def test_validate_cli_settings_malformed_json_vs_field_validation_errors(
        self, tmp_path: Path
    ):
        # Malformed JSON raises SettingsMalformedError
        with pytest.raises(SettingsMalformedError):
            validate_cli_settings(b"{invalid json")

        # Invalid field type raises SettingsValidationError; parse_settings_fields returns partial + errors
        ws = tmp_path / "proj"
        ws.mkdir()
        raw_dict = {
            "colorScheme": "Nord",
            "historySize": "not-an-int",
            "trustedWorkspaces": [f"{ws}/"],
            "extraFutureKey": {"nested": 1},
        }
        with pytest.raises(SettingsValidationError) as exc_info:
            validate_cli_settings(json.dumps(raw_dict).encode("utf-8"))
        assert any("historySize" in err for err in exc_info.value.errors)

        parsed, errors = parse_settings_fields(raw_dict)
        assert any("historySize" in err for err in errors)
        assert parsed.color_scheme == "Nord"
        assert parsed.history_size == 0  # fell back to default
        assert parsed.trusted_workspaces == [resolve_path_no_slash(ws)]
        assert parsed.unknown_fields == {"extraFutureKey": {"nested": 1}}

    def test_three_way_merge_cli_settings(self):
        base = CliSetting(
            color_scheme="OpsCloud Dark",
            trusted_workspaces=["/ws/a"],
            permissions=PermissionsSetting(allow=["Bash(git status)"]),
            unknown_fields={"keepMe": 1},
        )
        # Disk concurrently added /ws/disk and changed model
        disk = CliSetting(
            color_scheme="OpsCloud Dark",
            model="claude-sonnet-4-5",
            trusted_workspaces=["/ws/a", "/ws/disk"],
            permissions=PermissionsSetting(allow=["Bash(git status)", "Read(*)"]),
            unknown_fields={"keepMe": 1, "addedOnDisk": "yes"},
        )
        # In-memory target added /ws/target and changed colorScheme
        target = CliSetting(
            color_scheme="Catppuccin Mocha",
            trusted_workspaces=["/ws/a", "/ws/target"],
            permissions=PermissionsSetting(allow=["Bash(git status)", "Glob(*)"]),
            unknown_fields={"keepMe": 1},
        )

        merged = merge_cli_settings(disk, target, base)
        assert merged.color_scheme == "Catppuccin Mocha"
        assert merged.model == "claude-sonnet-4-5"
        assert merged.trusted_workspaces == ["/ws/a", "/ws/disk", "/ws/target"]
        assert merged.permissions is not None
        assert merged.permissions.allow == ["Bash(git status)", "Read(*)", "Glob(*)"]
        assert merged.unknown_fields == {"keepMe": 1, "addedOnDisk": "yes"}


class TestAtomicWriteAndQuarantine:
    """Tests for atomic_write_settings (0600 tmp + rename) and quarantine_cli_settings."""

    def test_atomic_write_creates_0600_file_without_leftover_tmp(self, tmp_path: Path):
        settings_path = tmp_path / "nested" / "settings.json"
        atomic_write_settings(settings_path, b'{"trustedWorkspaces": []}\n')

        assert settings_path.exists()
        mode = stat.S_IMODE(os.stat(settings_path).st_mode)
        assert mode == 0o600
        tmp_files = list(settings_path.parent.glob("*.tmp"))
        assert tmp_files == []

    def test_quarantine_cli_settings_renames_to_corrupted_timestamp(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{corrupted", encoding="utf-8")

        mgr = CliSettingsManager(settings_path=settings_path)
        mgr.init_cli_settings()
        assert mgr.settings_load_error is not None

        quarantined = mgr.quarantine_cli_settings()
        assert not settings_path.exists()
        assert quarantined is not None
        assert quarantined.exists()
        assert quarantined.name.startswith("settings.json.corrupted.")
        assert quarantined.read_text(encoding="utf-8") == "{corrupted"
        assert mgr.settings_load_error is None


class TestCliSettingsManagerWorkspaceTrust:
    """Tests for CliSettingsManager workspace trust persistence and project evaluation."""

    def test_untrusted_then_trust_workspace_persists_to_settings_json(self, tmp_path: Path):
        settings_file = tmp_path / ".opscloud" / "settings.json"
        workspace = tmp_path / "untrusted_repo"
        workspace.mkdir()

        mgr = CliSettingsManager(settings_path=settings_file)
        mgr.init_cli_settings()
        mgr.init_workspace_dirs(workspace)

        assert mgr.workspace_trusted() is False
        assert mgr.is_startup_workspace_trusted(workspace) is False

        # Trusting the workspace writes canonical path to settings.json
        updated = mgr.trust_workspace(workspace)
        assert resolve_path_no_slash(workspace) in updated.trusted_workspaces
        assert mgr.workspace_trusted() is True
        assert settings_file.exists()

        disk_data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert disk_data["trustedWorkspaces"] == [resolve_path_no_slash(workspace)]

        # Subsequent manager instance evaluates workspace as trusted on startup
        mgr2 = CliSettingsManager(settings_path=settings_file)
        mgr2.init_cli_settings()
        mgr2.init_workspace_dirs(workspace)
        assert mgr2.workspace_trusted() is True

    def test_workspace_in_saved_project_is_trusted(self, tmp_path: Path):
        from opscloud.config import paths

        settings_file = paths.DATA_DIR / "settings.json"
        projects_dir = paths.DATA_DIR / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)

        project_ws = tmp_path / "saved_project_ws"
        project_ws.mkdir(parents=True)

        # Write a project definition in ~/.opscloud/projects/my_proj.json
        (projects_dir / "my_proj.json").write_text(
            json.dumps({"name": "my_proj", "workspaces": [str(project_ws)]}),
            encoding="utf-8",
        )

        mgr = CliSettingsManager(settings_path=settings_file)
        mgr.init_cli_settings()
        assert mgr.is_workspace_in_any_project(project_ws) is True
        assert mgr.init_workspace_dirs(project_ws) is True
        assert mgr.workspace_trusted() is True

    def test_write_cli_settings_refuses_to_overwrite_corrupted_disk_file(self, tmp_path: Path):
        settings_file = tmp_path / ".opscloud" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("{corrupted json", encoding="utf-8")

        mgr = CliSettingsManager(settings_path=settings_file)
        mgr.init_cli_settings()
        assert mgr.settings_load_error is not None

        with pytest.raises(SettingsMalformedError, match="refusing to overwrite"):
            mgr.write_cli_settings(CliSetting(color_scheme="Dracula"))

        # Corrupted file on disk was untouched
        assert settings_file.read_text(encoding="utf-8") == "{corrupted json"


class TestWorkspaceTrustTUI:
    """Integration tests for WorkspaceTrustScreen and SettingsErrorScreen in OpsCloudApp."""

    @pytest.mark.asyncio
    async def test_untrusted_workspace_shows_prompt_and_accepting_persists_trust(
        self, tmp_path: Path
    ):
        workspace = tmp_path / "my_new_workspace"
        workspace.mkdir()
        reset_cli_settings_manager_for_testing()

        p1, p2, p3 = _patch_startup_heavy_ops()
        with p1, p2, p3:
            app = OpsCloudApp(cwd=workspace, check_workspace_trust=True)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                # WorkspaceTrustScreen should be active
                assert isinstance(app.screen, WorkspaceTrustScreen)
                assert app._workspace_trusted is False

                # Press Enter to confirm default selection ("Yes, I trust this folder")
                await pilot.press("enter")
                await pilot.pause()

                assert not isinstance(app.screen, WorkspaceTrustScreen)
                assert app._workspace_trusted is True

        # Verify settings.json has trustedWorkspaces populated
        mgr = get_cli_settings_manager()
        assert resolve_path_no_slash(workspace) in mgr.settings.trusted_workspaces

        # Second launch in the same workspace should NOT prompt for trust
        reset_cli_settings_manager_for_testing()
        p1, p2, p3 = _patch_startup_heavy_ops()
        with p1, p2, p3:
            app2 = OpsCloudApp(cwd=workspace, check_workspace_trust=True)
            async with app2.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                assert not isinstance(app2.screen, WorkspaceTrustScreen)
                assert app2._workspace_trusted is True

    @pytest.mark.asyncio
    async def test_untrusted_workspace_selecting_no_exits_without_trusting(
        self, tmp_path: Path
    ):
        workspace = tmp_path / "untrusted_exit_workspace"
        workspace.mkdir()
        reset_cli_settings_manager_for_testing()

        p1, p2, p3 = _patch_startup_heavy_ops()
        with p1, p2, p3:
            app = OpsCloudApp(cwd=workspace, check_workspace_trust=True)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                assert isinstance(app.screen, WorkspaceTrustScreen)

                # Navigate down to "No, exit" and press Enter
                await pilot.press("down")
                await pilot.pause()
                assert app.screen.selected == 1
                await pilot.press("enter")
                await pilot.pause()

        mgr = get_cli_settings_manager()
        assert resolve_path_no_slash(workspace) not in mgr.settings.trusted_workspaces

    @pytest.mark.asyncio
    async def test_corrupted_settings_shows_error_screen_then_quarantines_and_prompts_trust(
        self, tmp_path: Path
    ):
        from opscloud.config import paths

        workspace = tmp_path / "workspace_after_corrupt"
        workspace.mkdir()
        paths.USER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        paths.USER_SETTINGS_PATH.write_text("{bad json!", encoding="utf-8")
        reset_cli_settings_manager_for_testing()

        p1, p2, p3 = _patch_startup_heavy_ops()
        with p1, p2, p3:
            app = OpsCloudApp(cwd=workspace, check_workspace_trust=True)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                # Phase 3 (SettingsErrorScreen) takes precedence before Phase 5 (WorkspaceTrustScreen)
                assert isinstance(app.screen, SettingsErrorScreen)

                # Press Enter to quarantine corrupted file and continue with defaults
                await pilot.press("enter")
                await pilot.pause()

                # Corrupted file should be quarantined
                corrupted_backups = list(
                    paths.USER_SETTINGS_PATH.parent.glob("settings.json.corrupted.*")
                )
                assert len(corrupted_backups) == 1

                # Now Phase 5 (WorkspaceTrustScreen) should be presented
                assert isinstance(app.screen, WorkspaceTrustScreen)
                await pilot.press("enter")
                await pilot.pause()
                assert app._workspace_trusted is True
