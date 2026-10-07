"""Unit tests for /cloud command, AWS profile discovery, TOML persistence, and CloudProfileSelector."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opscloud.commands._base import CommandContext
from opscloud.commands.core.cloud import CloudHandler
from opscloud.config.cloud_profiles import (
    AWSProfileInfo,
    get_active_aws_profile,
    list_aws_profiles,
    set_active_aws_profile,
)
from opscloud.config.settings import Settings
from opscloud.config.toml_config import (
    clear_aws_profile,
    load_aws_profile,
    load_recent_aws_profiles,
    read_config_toml,
    save_aws_profile,
    save_recent_aws_profile,
)


@pytest.fixture
def temp_config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


# ── TOML Persistence Tests ───────────────────────────────


def test_save_and_load_aws_profile(temp_config_path: Path):
    """Test saving AWS profile to config.toml and loading it back."""
    assert load_aws_profile(temp_config_path) is None

    ok = save_aws_profile("staging", temp_config_path)
    assert ok is True

    loaded = load_aws_profile(temp_config_path)
    assert loaded == "staging"

    data = read_config_toml(temp_config_path)
    assert data["aws"]["profile"] == "staging"


def test_clear_aws_profile(temp_config_path: Path):
    """Test clearing AWS profile from config.toml."""
    save_aws_profile("production", temp_config_path)
    assert load_aws_profile(temp_config_path) == "production"

    ok = clear_aws_profile(temp_config_path)
    assert ok is True
    assert load_aws_profile(temp_config_path) is None


def test_recent_aws_profiles(tmp_path: Path, monkeypatch):
    """Test MRU recent AWS profile persistence."""
    test_state_file = tmp_path / "recent_cloud_profiles.json"
    monkeypatch.setattr("opscloud.config.toml_config._RECENT_PROFILES_FILE", test_state_file)
    monkeypatch.setattr("opscloud.config.toml_config.STATE_DIR", tmp_path)

    save_recent_aws_profile("dev")
    save_recent_aws_profile("staging")
    save_recent_aws_profile("prod")

    recents = load_recent_aws_profiles()
    assert recents == ["prod", "staging", "dev"]

    save_recent_aws_profile("staging")
    recents_updated = load_recent_aws_profiles()
    assert recents_updated == ["staging", "prod", "dev"]


# ── Cloud Profiles Discovery & Active State Tests ────────


def test_list_aws_profiles_mock_cli():
    """Test profile listing via aws configure list-profiles mock."""
    mock_res = MagicMock(return_code=0, stdout="default\nprod\nstaging\n")
    mock_res.returncode = 0
    with patch("subprocess.run", return_value=mock_res):
        profiles = list_aws_profiles()
        names = [p.name for p in profiles]
        assert "default" in names
        assert "prod" in names
        assert "staging" in names


def test_get_and_set_active_aws_profile(monkeypatch, tmp_path: Path):
    """Test active profile switching and env variable propagation."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)

    from opscloud.config.settings import get_settings
    get_settings().aws_profile = "default"

    test_config = tmp_path / "config.toml"
    monkeypatch.setattr("opscloud.config.cloud_profiles.save_aws_profile", lambda p: save_aws_profile(p, test_config))
    monkeypatch.setattr("opscloud.config.cloud_profiles.load_aws_profile", lambda: load_aws_profile(test_config))
    monkeypatch.setattr("opscloud.config.cloud_profiles.save_recent_aws_profile", lambda p: None)
    from opscloud.config.toml_config import save_aws_region as real_save_reg
    monkeypatch.setattr("opscloud.config.toml_config.save_aws_region", lambda r: real_save_reg(r, test_config))

    # Default fallback
    assert get_active_aws_profile() == "default"

    # Set new active profile with region
    set_active_aws_profile("prod-eu", region="eu-west-1", persist=True)

    assert os.environ.get("AWS_PROFILE") == "prod-eu"
    assert os.environ.get("AWS_DEFAULT_PROFILE") == "prod-eu"
    assert os.environ.get("AWS_REGION") == "eu-west-1"
    assert get_active_aws_profile() == "prod-eu"
    assert load_aws_profile(test_config) == "prod-eu"


# ── CloudHandler Command Tests ───────────────────────────


@pytest.mark.asyncio
async def test_cloud_handler_metadata():
    handler = CloudHandler()
    assert handler.name == "/cloud"
    assert "/aws" in handler.aliases


@pytest.mark.asyncio
async def test_cloud_handler_interactive_trigger():
    handler = CloudHandler()
    mock_app = MagicMock()
    called = False

    async def mock_show():
        nonlocal called
        called = True

    mock_app._show_cloud_selector = mock_show

    ctx = CommandContext(app=mock_app, raw_command="/cloud", args="")
    result = await handler.execute(ctx)

    assert result.success is True
    assert called is True


@pytest.mark.asyncio
async def test_cloud_handler_direct_switch(monkeypatch, tmp_path: Path):
    test_config = tmp_path / "config.toml"
    monkeypatch.setattr("opscloud.config.cloud_profiles.save_aws_profile", lambda p: save_aws_profile(p, test_config))
    monkeypatch.setattr("opscloud.config.cloud_profiles.save_recent_aws_profile", lambda p: None)

    handler = CloudHandler()
    settings = Settings()
    ctx = CommandContext(
        app=None,
        raw_command="/cloud test-account",
        args="test-account",
        settings=settings,
    )

    result = await handler.execute(ctx)
    assert result.success is True
    assert "test-account" in result.message
    assert os.environ.get("AWS_PROFILE") == "test-account"
    assert settings.aws_profile == "test-account"


@pytest.mark.asyncio
async def test_cloud_handler_list_subcommand():
    handler = CloudHandler()
    ctx = CommandContext(app=None, raw_command="/cloud list", args="list")
    result = await handler.execute(ctx)

    assert result.success is True
    assert "Configured AWS Profiles" in result.message


@pytest.mark.asyncio
async def test_cloud_handler_status_subcommand():
    handler = CloudHandler()
    ctx = CommandContext(app=None, raw_command="/cloud status", args="status")
    result = await handler.execute(ctx)

    assert result.success is True
    assert "Current Cloud Context" in result.message
    assert "AWS" in result.message


# ── UI Widget Tests ──────────────────────────────────────


def test_cloud_selector_screen_init():
    from opscloud.ui.widgets.cloud_selector import CloudProfileSelectorScreen

    screen = CloudProfileSelectorScreen(current_profile="dev-profile")
    assert screen._current_profile == "dev-profile"


def test_cloud_option_in_place_selection():
    """Verify CloudProfileOption supports in-place selection without rebuilding DOM."""
    from opscloud.config.cloud_profiles import AWSProfileInfo
    from opscloud.ui.widgets.cloud_selector import CloudProfileOption

    prof = AWSProfileInfo(name="staging-us-east-1", region="us-east-1", account_id="123456789012")
    opt = CloudProfileOption(
        label="  staging-us-east-1",
        profile_info=prof,
        index=0,
        is_selected=False,
    )
    assert not opt.is_selected
    assert "cloud-option-selected" not in opt.classes

    # Toggle selected True in-place
    opt.set_selected(True)
    assert opt.is_selected
    assert "cloud-option-selected" in opt.classes
    assert "› staging-us-east-1" in opt.render_label_text()

    # Toggle selected False in-place
    opt.set_selected(False)
    assert not opt.is_selected
    assert "cloud-option-selected" not in opt.classes
    assert "  staging-us-east-1" in opt.render_label_text()


async def test_cloud_selector_navigation_and_batch_mount():
    """Verify wrap-around navigation, page navigation, and fuzzy filter in CloudProfileSelectorScreen."""
    from opscloud.config.cloud_profiles import AWSProfileInfo
    from opscloud.ui.app import OpsCloudApp
    from opscloud.ui.widgets.cloud_selector import CloudProfileSelectorScreen
    from textual.widgets import Input

    app = OpsCloudApp()
    async with app.run_test(headless=True) as pilot:
        screen = CloudProfileSelectorScreen(current_profile="dev-profile")
        screen._load_profile_data = lambda: [
            AWSProfileInfo(name="dev-profile", region="us-west-2"),
            AWSProfileInfo(name="prod-profile", region="us-east-1", account_id="111222333444"),
            AWSProfileInfo(name="staging-profile", region="eu-west-1"),
            AWSProfileInfo(name="security-audit", region="us-east-1"),
        ]
        app.push_screen(screen)
        await pilot.pause()

        # Input should be focused immediately
        inp = screen.query_one("#cloud-filter", Input)
        assert inp.has_focus

        assert screen._loaded is True
        assert len(screen._option_widgets) > 0
        total_widgets = len(screen._option_widgets)

        # Selection starts at 0
        assert screen._selected_index == 0

        # Up arrow from 0 wraps to end
        screen.action_move_up()
        assert screen._selected_index == total_widgets - 1

        # Down arrow wraps back to 0
        screen.action_move_down()
        assert screen._selected_index == 0

        # Page down and page up
        screen.action_page_down()
        assert screen._selected_index >= 0
        screen.action_page_up()
        assert screen._selected_index == 0

        # Tab complete
        screen.action_tab_complete()
        assert inp.value == screen._option_widgets[screen._selected_index].profile_name

        # Fuzzy filter test
        inp.value = "audit"
        await pilot.pause()
        filtered_names = [opt.profile_name for opt in screen._option_widgets]
        assert "security-audit" in filtered_names

        # Resize refit
        screen._fit_cloud_list()



def test_get_active_aws_region(monkeypatch, tmp_path: Path):
    from opscloud.config.cloud_profiles import get_active_aws_region
    from opscloud.config.toml_config import load_aws_region, save_aws_region

    test_config = tmp_path / "config.toml"
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setattr("opscloud.config.paths.CONFIG_PATH", test_config)
    monkeypatch.setattr("opscloud.config.toml_config.CONFIG_PATH", test_config)
    monkeypatch.setattr("opscloud.config.cloud_profiles.Path.home", lambda: tmp_path)
    monkeypatch.setattr("opscloud.config.cloud_profiles.load_aws_profile", lambda: None)
    monkeypatch.setattr("opscloud.config.cloud_profiles.load_aws_region", lambda: load_aws_region(test_config))
    from opscloud.config.settings import get_settings
    monkeypatch.setattr(get_settings(), "aws_profile", "default")
    monkeypatch.setattr(get_settings(), "aws_region", "us-east-1")

    # 1. Fallback when nothing set
    assert get_active_aws_region() is None

    # 2. ~/.aws/config discovery when present
    fake_aws_dir = tmp_path / ".aws"
    fake_aws_dir.mkdir(parents=True, exist_ok=True)
    (fake_aws_dir / "config").write_text("[default]\nregion = us-west-2\n", encoding="utf-8")
    assert get_active_aws_region() == "us-west-2"

    # 3. Persisted in config.toml takes priority over ~/.aws/config default
    save_aws_region("eu-central-1", test_config)
    assert get_active_aws_region() == "eu-central-1"

    # 4. Environment variable override takes highest priority
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert get_active_aws_region() == "ap-south-1"


def test_profile_switch_region_sequence(monkeypatch, tmp_path: Path):
    """Regression test: switching krayak -> prod -> krayak retains profile-specific regions."""
    from opscloud.config.cloud_profiles import (
        get_active_aws_profile,
        get_active_aws_region,
        get_aws_profile_info,
        set_active_aws_profile,
    )
    from opscloud.config.toml_config import load_aws_profile, load_aws_region

    test_config = tmp_path / "config.toml"
    fake_aws_dir = tmp_path / ".aws"
    fake_aws_dir.mkdir(parents=True, exist_ok=True)

    aws_config_content = (
        "[default]\n"
        "region = eu-central-1\n\n"
        "[prod]\n"
        "region = eu-central-1\n\n"
        "[krayak]\n"
        "region = ap-south-1\n\n"
        "[profile shared_d_c]\n"
        "region = us-west-2\n\n"
        "[profile shared_d_c_adm]\n"
        "source_profile = shared_d_c\n"
        "role_arn = arn:aws:iam::123456789012:role/admin\n"
    )
    (fake_aws_dir / "config").write_text(aws_config_content, encoding="utf-8")

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setattr("opscloud.config.paths.CONFIG_PATH", test_config)
    monkeypatch.setattr("opscloud.config.toml_config.CONFIG_PATH", test_config)
    monkeypatch.setattr("opscloud.config.cloud_profiles.Path.home", lambda: tmp_path)

    # 1. Switch to krayak (ap-south-1)
    set_active_aws_profile("krayak", persist=True)
    assert get_active_aws_profile() == "krayak"
    assert get_active_aws_region() == "ap-south-1"
    assert os.environ.get("AWS_REGION") == "ap-south-1"
    assert load_aws_profile(test_config) == "krayak"
    assert load_aws_region(test_config) == "ap-south-1"

    # 2. Switch to prod (eu-central-1)
    set_active_aws_profile("prod", persist=True)
    assert get_active_aws_profile() == "prod"
    assert get_active_aws_region() == "eu-central-1"
    assert os.environ.get("AWS_REGION") == "eu-central-1"
    assert load_aws_profile(test_config) == "prod"
    assert load_aws_region(test_config) == "eu-central-1"

    # 3. Switch back to krayak -> must cleanly restore ap-south-1
    set_active_aws_profile("krayak", persist=True)
    assert get_active_aws_profile() == "krayak"
    assert get_active_aws_region() == "ap-south-1"
    assert os.environ.get("AWS_REGION") == "ap-south-1"
    assert load_aws_profile(test_config) == "krayak"
    assert load_aws_region(test_config) == "ap-south-1"

    # 4. Switch to role-assumed profile inheriting region via source_profile
    set_active_aws_profile("shared_d_c_adm", persist=True)
    assert get_active_aws_profile() == "shared_d_c_adm"
    assert get_active_aws_region() == "us-west-2"
    assert os.environ.get("AWS_REGION") == "us-west-2"
    assert load_aws_profile(test_config) == "shared_d_c_adm"
    assert load_aws_region(test_config) == "us-west-2"


@pytest.mark.asyncio
async def test_status_bar_cloud_display():
    from opscloud.ui.widgets.status import StatusBar

    bar = StatusBar()
    # Test set_cloud updates reactives
    bar.set_cloud(profile="krayak", region="ap-south-1")
    assert bar.cloud_profile == "krayak"
    assert bar.cloud_region == "ap-south-1"

    bar.set_cloud(profile="production", region="")
    assert bar.cloud_profile == "production"
    assert bar.cloud_region == ""


def test_welcome_banner_no_cloud_column():
    from opscloud.ui.widgets.welcome import WelcomeBanner

    banner = WelcomeBanner()
    banner._skills_data = [("aws-core", "Core AWS")]
    banner._subagents_data = []

    right_text = banner._build_right_panel()
    plain = right_text.plain

    # Verify Cloud section is NOT displayed in banner (shown on status bar instead)
    assert "Cloud" not in plain
    assert "aws:" not in plain
    assert "Skills" in plain
    assert "Agents" in plain

    # update_cloud is a safe no-op
    banner.update_cloud("prod-account", "us-east-1")
    assert "Cloud" not in banner._build_right_panel().plain


@pytest.mark.asyncio
async def test_cloud_handler_headless_no_args():
    """Verify headless /cloud with no args returns status instead of dead push_screen."""
    handler = CloudHandler()
    ctx = CommandContext(app=None, raw_command="/cloud", args="")
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.push_screen is None
    assert "Current Cloud Context" in result.message
    assert "Active Profile:" in result.message


@pytest.mark.asyncio
async def test_cloud_handler_default_setting(monkeypatch, tmp_path: Path):
    """Verify /cloud --default saves and activates profile cleanly."""
    test_config = tmp_path / "config.toml"
    monkeypatch.setattr("opscloud.config.paths.CONFIG_PATH", test_config)
    monkeypatch.setattr("opscloud.config.toml_config.CONFIG_PATH", test_config)
    monkeypatch.setattr("opscloud.config.cloud_profiles.Path.home", lambda: tmp_path)

    handler = CloudHandler()
    settings = Settings()
    ctx = CommandContext(
        app=None,
        raw_command="/cloud --default prod-corp",
        args="--default prod-corp",
        settings=settings,
    )

    result = await handler.execute(ctx)
    assert result.success is True
    assert "Default AWS profile set to `prod-corp`" in result.message
    assert settings.aws_profile == "prod-corp"
    assert load_aws_profile(test_config) == "prod-corp"


def test_cloud_profile_discovery_caching(tmp_path: Path, monkeypatch):
    """Verify that profile discovery and metadata queries use in-memory caching."""
    from opscloud.config.cloud_profiles import (
        get_aws_profile_info,
        invalidate_aws_profile_cache,
        list_aws_profiles,
    )

    fake_aws_dir = tmp_path / ".aws"
    fake_aws_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = fake_aws_dir / "config"
    cfg_file.write_text("[default]\nregion = us-east-1\n\n[profile cached_prof]\nregion = eu-west-1\n", encoding="utf-8")

    monkeypatch.setattr("opscloud.config.cloud_profiles.Path.home", lambda: tmp_path)
    invalidate_aws_profile_cache()

    info1 = get_aws_profile_info("cached_prof")
    assert info1.region == "eu-west-1"

    # Modify file directly on disk without changing mtime or query cached
    info2 = get_aws_profile_info("cached_prof")
    assert info1 is info2  # Identity check confirms same cached instance

    # Invalidating cache yields fresh query
    invalidate_aws_profile_cache()
    info3 = get_aws_profile_info("cached_prof")
    assert info3.region == "eu-west-1"


def test_cloud_selector_unconfigured_profile_notification():
    """Verify typing an unknown profile name triggers warning notification."""
    from opscloud.ui.widgets.cloud_selector import CloudProfileSelectorScreen

    screen = CloudProfileSelectorScreen(current_profile="default")
    screen._all_profiles = [AWSProfileInfo(name="default"), AWSProfileInfo(name="staging")]
    screen._option_widgets = []
    screen._filter_text = "non-existent-prof"

    notified: list[str] = []
    screen.notify = lambda msg, **kwargs: notified.append(msg)
    dismissed: list[tuple[str, str | None] | None] = []
    screen.dismiss = lambda res: dismissed.append(res)

    screen.action_select()
    assert len(notified) == 1
    assert "not configured in ~/.aws/config" in notified[0]
    assert dismissed == [("non-existent-prof", None)]


def test_status_bar_narrow_cloud_rendering():
    """Verify that narrow terminal widths truncate profile names and hide region."""
    import textual._context
    from rich.console import Console
    from textual.widgets import Static
    from opscloud.ui.widgets.status import StatusBar

    mock_app = MagicMock()
    mock_app.size.width = 120
    mock_app.console = Console()
    token = textual._context.active_app.set(mock_app)

    try:
        bar = StatusBar()
        rendered: list[Any] = []
        display = MagicMock()
        display.update = lambda t: rendered.append(t)
        bar.query_one = lambda sel, *args: display if sel == "#cloud-display" else MagicMock()

        bar.set_cloud(profile="my-extremely-long-production-profile", region="us-east-1")
        bar._render_cloud()

        assert len(rendered) >= 1
        text_content = rendered[-1]
        assert "aws:" in text_content.plain
        assert "my-extremely-long-production-profile" in text_content.plain
        assert "(us-east-1)" in text_content.plain

        # Narrow terminal: < 90
        mock_app.size.width = 75
        bar._render_cloud()
        narrow_content = rendered[-1]
        assert "aws:" in narrow_content.plain
        assert "…" in narrow_content.plain
        # Region must be hidden in narrow mode
        assert "(us-east-1)" not in narrow_content.plain
    finally:
        textual._context.active_app.reset(token)

