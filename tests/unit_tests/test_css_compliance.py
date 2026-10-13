"""Unit tests for Step 9: CSS compliance and modal styling standards."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any
import pytest

from textual.app import App, ComposeResult
from opscloud.config.settings import (
    get_glyphs,
    is_ascii_mode,
    reset_settings_for_testing,
)
from opscloud.ui.theme import register_app_themes
from opscloud.ui.widgets.cloud_selector import CloudProfileSelectorScreen
from opscloud.ui.widgets.effort_selector import EffortSelectorScreen
from opscloud.ui.widgets.theme_selector import ThemeSelectorScreen


class _TestHarnessApp(App[None]):
    """Test app harness for mounting modal screens."""

    def __init__(self, screen_instance: Any) -> None:
        super().__init__()
        self._test_screen = screen_instance
        register_app_themes(self)
        self.theme = "opscloud-dark"

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


def test_ui_css_uses_semantic_colors() -> None:
    """Verify all TCSS files in src/opscloud/ui use semantic theme variables rather than concrete hex/rgb colors."""
    ui_root = Path("src/opscloud/ui")
    concrete_color = re.compile(
        r"#[0-9a-fA-F]{3,8}\b|rgba?\(",
        re.IGNORECASE,
    )
    offenders: list[str] = []

    for path in ui_root.rglob("*.tcss"):
        text = path.read_text(encoding="utf-8")
        if concrete_color.search(text):
            offenders.append(str(path))

    assert offenders == [], f"Found concrete colors in TCSS stylesheets: {offenders}"


def test_modal_backdrop_ownership() -> None:
    """Verify OpsModalScreen owns the modal backdrop and app.tcss does not compete."""
    app_tcss = Path("src/opscloud/ui/app.tcss").read_text(encoding="utf-8")
    modal_tcss = Path("src/opscloud/ui/modal.tcss").read_text(encoding="utf-8")

    # app.tcss must not declare competing ModalScreen backdrop
    assert "ModalScreen" not in app_tcss, "app.tcss must not declare ModalScreen rules"
    assert "rgba(0, 0, 0" not in app_tcss, "app.tcss must not contain concrete backdrop colors"

    # modal.tcss must declare semantic OpsModalScreen backdrop
    assert "OpsModalScreen {" in modal_tcss
    assert "background: $background 70%;" in modal_tcss


@pytest.mark.asyncio
async def test_ascii_mode_modal_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify representative command modals render correctly under ASCII charset mode."""
    monkeypatch.setenv("OPSCLOUD_UI_CHARSET_MODE", "ascii")
    reset_settings_for_testing()
    assert is_ascii_mode() is True
    assert get_glyphs().bullet == "-"
    assert get_glyphs().circle_filled == "[*]"

    try:
        # Test Cloud Profile Selector in ASCII mode
        app1 = _TestHarnessApp(CloudProfileSelectorScreen())
        async with app1.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app1.screen is not None
            # Modal must mount successfully and contain no crashed widgets
            assert app1.screen.is_mounted

        # Test Theme Selector in ASCII mode
        app2 = _TestHarnessApp(ThemeSelectorScreen(current_theme="opscloud-dark"))
        async with app2.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app2.screen is not None
            assert app2.screen.is_mounted

        # Test Effort Selector in ASCII mode
        app3 = _TestHarnessApp(
            EffortSelectorScreen(
                model_spec="claude-3-7-sonnet",
                efforts=("low", "medium", "high"),
                current_effort="medium",
            )
        )
        async with app3.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app3.screen is not None
            assert app3.screen.is_mounted

    finally:
        reset_settings_for_testing()
