"""Unit tests for PoolSelectorScreen (/pool) modal geometry and dropdown interaction."""

from __future__ import annotations

import pytest
from textual.app import App
from textual.widgets import Select
from textual.widgets._select import SelectOverlay

from pathlib import Path

from opscloud.config.toml_config import clear_agent_pool
from opscloud.ui.theme import register_app_themes
from opscloud.ui.widgets.modal_base import OpsModalScreen
from opscloud.ui.widgets.pool_selector import PoolSelectorScreen

_MODAL_TCSS_PATH = Path(__file__).resolve().parents[2] / "src" / "opscloud" / "ui" / "modal.tcss"


class _ThemedPoolApp(App[None]):
    """App carrying OpsCloud theme and modal stylesheet for pool selector testing."""

    CSS_PATH = [str(_MODAL_TCSS_PATH)]

    def __init__(self, screen_to_mount: OpsModalScreen | None = None) -> None:
        super().__init__()
        register_app_themes(self)
        self.theme = "opscloud-dark"
        self._screen_to_mount = screen_to_mount

    async def on_mount(self) -> None:
        if self._screen_to_mount is not None:
            await self.push_screen(self._screen_to_mount)


@pytest.mark.asyncio
async def test_pool_selector_dropdown_overlays_expand_properly():
    """Verify that SelectOverlay dropdowns inside PoolSelectorScreen do not collapse to height 1."""
    clear_agent_pool()
    screen = PoolSelectorScreen(active_provider="google_genai")
    app = _ThemedPoolApp(screen)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        select_ids = [
            "#select-provider",
            "#select-provider-fast",
            "#select-fast",
            "#select-provider-standard",
            "#select-standard",
            "#select-provider-powerful",
            "#select-powerful",
        ]

        for sid in select_ids:
            sel = screen.query_one(sid, Select)
            sel.scroll_visible()
            await pilot.pause()

            # Expand dropdown
            sel.expanded = True
            await pilot.pause()

            overlay = sel.query_one(SelectOverlay)
            assert sel.expanded is True
            # Overlays must have sufficient height to display multiple options, not be squashed to 1
            assert overlay.size.height > 1, f"Overlay for {sid} was squashed to height {overlay.size.height}"
            assert overlay.size.height >= 5

            # Collapse before next
            sel.expanded = False
            await pilot.pause()


@pytest.mark.asyncio
async def test_pool_selector_provider_change_updates_models():
    """Verify selecting a different provider updates the model dropdown options and value."""
    clear_agent_pool()
    screen = PoolSelectorScreen(active_provider="google_genai")
    app = _ThemedPoolApp(screen)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        prov_sel = screen.query_one("#select-provider-fast", Select)
        model_sel = screen.query_one("#select-fast", Select)

        assert prov_sel.value == "google_genai"
        assert "google_genai:" in str(model_sel.value)

        # Switch fast tier provider to openai
        prov_sel.value = "openai"
        await pilot.pause()

        assert prov_sel.value == "openai"
        assert "openai:" in str(model_sel.value)
        # Check that options in model dropdown are now openai models
        assert all("openai:" in str(opt[1]) for opt in model_sel._options)


@pytest.mark.asyncio
async def test_pool_selector_bulk_preset_sync():
    """Verify bulk preset sets all tiers to one provider, and title counter syncs."""
    clear_agent_pool()
    screen = PoolSelectorScreen(active_provider="google_genai")
    app = _ThemedPoolApp(screen)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        bulk_sel = screen.query_one("#select-provider", Select)
        bulk_sel.value = "anthropic"
        await pilot.pause()

        for tier in ("fast", "standard", "powerful"):
            p_sel = screen.query_one(f"#select-provider-{tier}", Select)
            m_sel = screen.query_one(f"#select-{tier}", Select)
            assert p_sel.value == "anthropic"
            assert "anthropic:" in str(m_sel.value)

        # Title counter should reflect single provider "anthropic"
        dialog = screen.dialog
        assert dialog is not None
        assert "anthropic" in str(dialog.border_title)

        # Changing one tier provider to another breaks uniform preset
        fast_prov = screen.query_one("#select-provider-fast", Select)
        fast_prov.value = "openai"
        await pilot.pause()

        assert bulk_sel.value == ""
        assert "multi" in str(dialog.border_title)
