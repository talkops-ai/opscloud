"""Unit tests for plugin uninstall lifecycle, scope resolution, and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from opscloud.plugins.discovery import (
    uninstall_plugin,
)
from opscloud.plugins.store import (
    PluginNotFoundError,
    add_installed_plugin,
    load_installed_plugin_entries,
    load_user_enabled_plugin_ids,
    set_plugin_enabled_for_scope,
)
from opscloud.ui.widgets.plugin_manager import (
    PluginManagerScreen,
    _PluginRow,
    _load_manager_state,
)


class DummyPluginManagerApp(App):
    """Minimal textual app to mount and test PluginManagerScreen."""

    def __init__(self, screen_instance: PluginManagerScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.fixture
def isolated_plugin_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up an isolated plugin and config environment in tmp_path."""
    config_dir = tmp_path / ".opscloud"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir = config_dir / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("OPSCLOUD_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("OPSCLOUD_PLUGIN_DIR", str(plugin_dir))

    from opscloud.config import paths

    monkeypatch.setattr(
        paths,
        "project_settings_path",
        lambda pr: pr / ".opscloud" / "settings.json",
    )
    monkeypatch.setattr(
        paths,
        "project_local_settings_path",
        lambda pr: pr / ".opscloud" / "settings.local.json",
    )

    from opscloud.plugins import store

    monkeypatch.setattr(store, "_state_dir", lambda: state_dir)
    monkeypatch.setattr(store, "_installed_plugins_path", lambda: state_dir / "installed_plugins.json")
    monkeypatch.setattr(store, "plugin_storage_root", lambda: plugin_dir)
    monkeypatch.setattr(store, "_user_settings_path", lambda: config_dir / "settings.json")
    monkeypatch.setattr(store, "load_marketplace_records", lambda project_root=None: {})

    return tmp_path


def test_cross_project_uninstall_fallback(isolated_plugin_env: Path) -> None:
    """Verify that a local plugin installed in repo_a can be uninstalled from repo_b.

    Forensic reproduction of ISSUE-OPSCLOUD-PLUGIN-002: cross-workspace mismatch
    should trigger resilient fallback removal rather than silent no-op.
    """
    repo_a = isolated_plugin_env / "repo_a"
    repo_b = isolated_plugin_env / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    cache_dir = isolated_plugin_env / "cache" / "devsecops"
    cache_dir.mkdir(parents=True)
    (cache_dir / "plugin.json").write_text("{}", encoding="utf-8")

    pid = "aws-agents-for-devsecops@agent-toolkit-for-aws"

    # Install locally in repo_a
    add_installed_plugin(
        pid,
        install_path=str(cache_dir),
        version="1.1.0",
        scope="local",
        project_root=repo_a,
    )

    # Verify present initially
    entries_before = load_installed_plugin_entries()
    assert pid in entries_before
    assert entries_before[pid][0].project_path == str(repo_a)

    # Invoke uninstall from repo_b with scope="local" (as happens when running OpsCloud from repo_b)
    result = uninstall_plugin(pid, scope="local", project_root=repo_b)
    assert result is True

    # Record must be removed
    entries_after = load_installed_plugin_entries()
    assert pid not in entries_after
    assert not cache_dir.is_dir(), "Cache directory should be purged"


def test_uninstall_nonexistent_plugin_raises_plugin_not_found(
    isolated_plugin_env: Path,
) -> None:
    """Verify that attempting to uninstall a non-existent plugin raises PluginNotFoundError."""
    with pytest.raises(PluginNotFoundError, match="is not installed"):
        uninstall_plugin("nonexistent-plugin@some-marketplace")

    with pytest.raises(PluginNotFoundError, match="is not installed"):
        uninstall_plugin("nonexistent-plugin@some-marketplace", scope="user")


def test_uninstall_multi_scope_cache_retention(isolated_plugin_env: Path) -> None:
    """Verify cache directory survives when another scope still references it."""
    repo = isolated_plugin_env / "repo"
    repo.mkdir()

    cache_dir = isolated_plugin_env / "cache" / "shared"
    cache_dir.mkdir(parents=True)
    (cache_dir / "plugin.json").write_text("{}", encoding="utf-8")

    pid = "shared-plugin@market"

    # Installed in user scope AND project scope
    add_installed_plugin(
        pid, install_path=str(cache_dir), version="1.0.0", scope="user"
    )
    add_installed_plugin(
        pid,
        install_path=str(cache_dir),
        version="1.0.0",
        scope="project",
        project_root=repo,
    )

    # Uninstall project scope
    uninstall_plugin(pid, scope="project", project_root=repo)
    assert cache_dir.is_dir(), "Cache must survive while user scope still references it"

    entries = load_installed_plugin_entries()
    assert pid in entries
    assert len(entries[pid]) == 1
    assert entries[pid][0].scope == "user"

    # Uninstall user scope
    uninstall_plugin(pid, scope="user")
    assert not cache_dir.is_dir(), "Cache must be deleted when no scopes reference it"
    assert pid not in load_installed_plugin_entries()


def test_global_uninstall_removes_all_scopes(isolated_plugin_env: Path) -> None:
    """Verify that calling uninstall without scope (dcode style) removes all scoped entries."""
    repo_a = isolated_plugin_env / "repo_a"
    repo_b = isolated_plugin_env / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    cache_dir = isolated_plugin_env / "cache" / "multi"
    cache_dir.mkdir(parents=True)

    pid = "multi-scope@market"
    add_installed_plugin(
        pid, install_path=str(cache_dir), version="1.0.0", scope="local", project_root=repo_a
    )
    add_installed_plugin(
        pid, install_path=str(cache_dir), version="1.0.0", scope="local", project_root=repo_b
    )

    # Calling uninstall_plugin with scope=None removes all entries
    result = uninstall_plugin(pid, scope=None)
    assert result is True
    assert pid not in load_installed_plugin_entries()
    assert not cache_dir.is_dir()


def test_settings_cleaned_up_on_uninstall(isolated_plugin_env: Path) -> None:
    """Verify that enabledPlugins in settings is purged when plugin is uninstalled."""
    pid = "tracked-plugin@market"
    cache_dir = isolated_plugin_env / "cache" / "tracked"
    cache_dir.mkdir(parents=True)

    add_installed_plugin(
        pid, install_path=str(cache_dir), version="1.0.0", scope="user"
    )
    set_plugin_enabled_for_scope(pid, True, scope="user")

    assert pid in load_user_enabled_plugin_ids()

    uninstall_plugin(pid, scope="user")
    assert pid not in load_user_enabled_plugin_ids()


@pytest.mark.asyncio
async def test_tui_action_uninstall_integration(isolated_plugin_env: Path) -> None:
    """Verify TUI /plugins screen uninstalls plugin and updates status without silent failure."""
    repo = isolated_plugin_env / "my_project"
    repo.mkdir()

    cache_dir = isolated_plugin_env / "cache" / "tui_test"
    cache_dir.mkdir(parents=True)
    (cache_dir / "plugin.json").write_text("{}", encoding="utf-8")

    pid = "aws-containers@talkops-devops-plugins"
    add_installed_plugin(
        pid,
        install_path=str(cache_dir),
        version="0.1.0",
        scope="local",
        project_root=repo,
    )

    screen = PluginManagerScreen(project_root=repo)
    app = DummyPluginManagerApp(screen)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Select the plugin
        row = _PluginRow(
            plugin_id=pid,
            description="Container skills",
            enabled=True,
            version="0.1.0",
            author="TalkOps",
            display_name="AWS Containers",
            scope="local",
            project_path=str(repo),
        )
        screen._selected_plugin = row

        # Simulate clicking uninstall action
        from textual.widgets._option_list import Option
        class DummyOptionSelected:
            option_id = "action:uninstall"
            option = Option("Uninstall", id="action:uninstall")

        await screen.on_option_list_option_selected(DummyOptionSelected())
        await pilot.pause()

        # Verify status reports uninstalled
        assert screen._status is not None
        assert "Uninstalled AWS Containers" in screen._status
        assert screen._error is None

        # Verify state persistence: plugin removed from json and cache deleted
        assert pid not in load_installed_plugin_entries()
        assert not cache_dir.is_dir()


@pytest.mark.asyncio
async def test_tui_action_uninstall_cross_project_preservation(
    isolated_plugin_env: Path,
) -> None:
    """Verify TUI preserves original project_path from state and uninstalls foreign repo plugin."""
    foreign_repo = isolated_plugin_env / "foreign_repo"
    current_repo = isolated_plugin_env / "current_repo"
    foreign_repo.mkdir()
    current_repo.mkdir()

    cache_dir = isolated_plugin_env / "cache" / "cross_test"
    cache_dir.mkdir(parents=True)
    (cache_dir / "plugin.json").write_text("{}", encoding="utf-8")

    pid = "terraform@talkops-devops-plugins"
    add_installed_plugin(
        pid,
        install_path=str(cache_dir),
        version="1.0.0",
        scope="local",
        project_root=foreign_repo,
    )

    # Load state as if running from current_repo
    state = _load_manager_state(project_root=current_repo)
    # The installed plugin row should preserve foreign_repo in project_path
    installed_rows = [r for r in state.installed_plugins if r.plugin_id == pid]
    assert len(installed_rows) == 1
    assert installed_rows[0].project_path == str(foreign_repo)

    # Now mount screen with current_repo
    screen = PluginManagerScreen(project_root=current_repo)
    app = DummyPluginManagerApp(screen)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen._selected_plugin = installed_rows[0]

        from textual.widgets._option_list import Option

        class DummyOptionSelected:
            option = Option("Uninstall", id="action:uninstall")

        await screen.on_option_list_option_selected(DummyOptionSelected())
        await pilot.pause()

        assert "Uninstalled" in (screen._status or "")
        assert pid not in load_installed_plugin_entries()
        assert not cache_dir.is_dir()


def test_cli_uninstall_command_success_and_failure(
    isolated_plugin_env: Path,
) -> None:
    """Verify CLI plugin uninstall command handles success and errors properly."""
    import argparse
    from opscloud.plugins.commands_cli import execute_plugin_command

    cache_dir = isolated_plugin_env / "cache" / "cli_test"
    cache_dir.mkdir(parents=True)
    pid = "cli-test@market"

    add_installed_plugin(
        pid, install_path=str(cache_dir), version="1.0.0", scope="user"
    )

    # Successful uninstall
    args = argparse.Namespace(
        plugin_command="uninstall",
        plugin_id=pid,
        scope=None,
        output_format="text",
    )
    out = execute_plugin_command(args)
    assert f"Uninstalled plugin {pid}." in (out or "")
    assert pid not in load_installed_plugin_entries()

    # Repeating uninstall should now fail with SystemExit(1)
    with pytest.raises(SystemExit) as exc_info:
        execute_plugin_command(args)
    assert exc_info.value.code == 1
