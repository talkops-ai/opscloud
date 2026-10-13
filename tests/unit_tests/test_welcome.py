"""Unit tests for Welcome Banner widget."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from opscloud.ui.widgets.welcome import (
    WelcomeBanner,
    _BannerInfo,
    _InfoPanel,
    _LeftPanel,
)


class WelcomeHarnessApp(App[None]):
    """App mounting WelcomeBanner to test rendering and lifecycle."""

    def __init__(self):
        super().__init__()
        from opscloud.ui.theme import register_app_themes

        register_app_themes(self)
        self.banner = WelcomeBanner()

    def get_theme_variable_defaults(self) -> dict[str, str]:
        from opscloud.ui.theme import get_css_variable_defaults, get_theme_colors

        colors = get_theme_colors(self)
        return get_css_variable_defaults(colors=colors)

    def compose(self) -> ComposeResult:
        yield self.banner


@pytest.mark.asyncio
async def test_welcome_banner_renders_cleanly():
    """Verify WelcomeBanner mounts two panels without error."""
    app = WelcomeHarnessApp()
    async with app.run_test(size=(120, 24)) as pilot:
        assert app.banner is not None
        left = app.banner.query_one(_LeftPanel)
        right = app.banner.query_one(_InfoPanel)
        assert left is not None
        assert right is not None


@pytest.mark.asyncio
async def test_welcome_banner_environment_info_update():
    """Verify WelcomeBanner dynamically updates model and cloud profile."""
    app = WelcomeHarnessApp()
    async with app.run_test(size=(120, 24)) as pilot:
        app.banner.update_model("google_genai:gemini-3.7-flash")
        app.banner.update_cloud(profile="krayak", region="ap-south-1")
        await pilot.pause()

        assert app.banner._info.model_spec == "google_genai:gemini-3.7-flash"
        assert app.banner._info.cloud_profile == "krayak"
        assert app.banner._info.cloud_region == "ap-south-1"


@pytest.mark.asyncio
async def test_left_panel_animation_continues_and_reflects_health():
    """Verify _LeftPanel animation timer keeps running and sweeps reflect health state."""
    app = WelcomeHarnessApp()
    async with app.run_test(size=(120, 24)) as pilot:
        left_panel = app.banner.query_one(_LeftPanel)
        left_panel._cycle_count = 2
        left_panel.status = "connected"

        # Advance beam past logo width
        left_panel.beam_x = 64
        left_panel._advance_beam()
        await pilot.pause()

        # Timer should keep running to display the continuous light beam
        assert left_panel._anim_timer is not None
        assert left_panel.beam_x == -6

        # Test status transitions reflect proper health line colors
        app.banner.set_warning()
        await pilot.pause()
        assert left_panel.status == "warning"

        app.banner.set_connected()
        await pilot.pause()
        assert left_panel.status == "connected"
