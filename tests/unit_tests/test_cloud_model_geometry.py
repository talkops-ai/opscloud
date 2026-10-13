"""Tests for /cloud and /model modal dynamic height fitting, 24-row cap, and short terminal bounds."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from opscloud.config.cloud_profiles import AWSProfileInfo
from opscloud.ui.theme import get_css_variable_defaults, get_theme_colors, register_app_themes
from opscloud.ui.widgets.cloud_selector import CloudProfileSelectorScreen, MAX_VISIBLE_ROWS, MIN_VISIBLE_ROWS
from opscloud.ui.widgets.model_selector import ModelSelectorScreen
from opscloud.ui.widgets.modal_base import OpsModalScreen


class _ThemedModalApp(App):
    """App carrying OpsCloud theme variables for testing modal screens."""

    def __init__(self, screen_to_mount: OpsModalScreen | None = None) -> None:
        super().__init__()
        register_app_themes(self)
        self.theme = "opscloud-dark"
        self._screen_to_mount = screen_to_mount

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return get_css_variable_defaults(colors=get_theme_colors(self))

    def compose(self) -> ComposeResult:
        yield Static("Background content", id="bg")

    async def on_mount(self) -> None:
        if self._screen_to_mount is not None:
            await self.push_screen(self._screen_to_mount)


def _make_mock_profiles(count: int) -> list[AWSProfileInfo]:
    return [
        AWSProfileInfo(
            name=f"profile-{i:02d}",
            region="us-east-1",
            account_id=f"1234567890{i:02d}",
        )
        for i in range(count)
    ]


def _make_mock_models(count: int) -> list[tuple[str, str, str]]:
    return [
        (f"model-{i:02d}", f"Test Model {i:02d}", "openai")
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_cloud_selector_few_results_hugs_height():
    """When only 1 profile exists, list height shrinks to MIN_VISIBLE_ROWS."""
    mock_profs = _make_mock_profiles(1)
    with patch("opscloud.ui.widgets.cloud_selector.list_aws_profiles", return_value=mock_profs):
        modal = CloudProfileSelectorScreen()
        app = _ThemedModalApp(modal)
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            list_widget = modal.query_one(".cloud-list", VerticalScroll)
            # 1 profile + 1 header = 2 mounted rows -> clamped to MIN_VISIBLE_ROWS (3)
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value == MIN_VISIBLE_ROWS


@pytest.mark.asyncio
async def test_cloud_selector_many_results_caps_at_24():
    """When 35 profiles exist, list height expands to MAX_VISIBLE_ROWS (24)."""
    mock_profs = _make_mock_profiles(35)
    with patch("opscloud.ui.widgets.cloud_selector.list_aws_profiles", return_value=mock_profs):
        modal = CloudProfileSelectorScreen()
        app = _ThemedModalApp(modal)
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            list_widget = modal.query_one(".cloud-list", VerticalScroll)
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value == MAX_VISIBLE_ROWS


@pytest.mark.asyncio
async def test_cloud_selector_filter_shrinks_and_clearing_expands():
    """Filtering to 1 profile shrinks list; clearing filter expands back to cap."""
    mock_profs = [AWSProfileInfo(name="unique-target-profile")] + [
        AWSProfileInfo(name=f"other-cluster-{i:02d}") for i in range(35)
    ]
    with patch("opscloud.ui.widgets.cloud_selector.list_aws_profiles", return_value=mock_profs):
        modal = CloudProfileSelectorScreen()
        app = _ThemedModalApp(modal)
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            list_widget = modal.query_one(".cloud-list", VerticalScroll)
            # Type filter that matches only 1 profile
            filter_input = modal.query_one("#cloud-filter", Input)
            filter_input.value = "unique-target-profile"
            await pilot.pause()

            # Should shrink to min rows (1 header + 1 option = 2 -> clamped to MIN_VISIBLE_ROWS)
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value == MIN_VISIBLE_ROWS

            # Clear filter
            filter_input.value = ""
            await pilot.pause()

            # Should expand back to 24
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value == MAX_VISIBLE_ROWS


@pytest.mark.asyncio
async def test_cloud_selector_short_terminal_bounds_list():
    """On short terminal (20 rows), list is bounded by viewport and dialog fits."""
    mock_profs = _make_mock_profiles(35)
    with patch("opscloud.ui.widgets.cloud_selector.list_aws_profiles", return_value=mock_profs):
        modal = CloudProfileSelectorScreen()
        app = _ThemedModalApp(modal)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            list_widget = modal.query_one(".cloud-list", VerticalScroll)
            # 20 - 12 = 8 available rows
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value <= 8
            # Modal dialog itself stays inside screen height
            assert modal.dialog is not None
            assert modal.dialog.region.height <= 20


@pytest.mark.asyncio
async def test_model_selector_few_results_hugs_height():
    """When only 1 model exists, model-list shrinks to MIN_VISIBLE_ROWS."""
    mock_models = _make_mock_models(1)
    with patch("opscloud.ui.widgets.model_selector.get_available_models_list", return_value=mock_models):
        modal = ModelSelectorScreen()
        app = _ThemedModalApp(modal)
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            list_widget = modal.query_one(".model-list", VerticalScroll)
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value <= 4


@pytest.mark.asyncio
async def test_model_selector_many_results_caps_at_24():
    """When 35 models exist, model-list expands to MAX_VISIBLE_ROWS (24)."""
    mock_models = _make_mock_models(35)
    with patch("opscloud.ui.widgets.model_selector.get_available_models_list", return_value=mock_models):
        modal = ModelSelectorScreen()
        modal._recommended_only = False
        app = _ThemedModalApp(modal)
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            list_widget = modal.query_one(".model-list", VerticalScroll)
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value == MAX_VISIBLE_ROWS


@pytest.mark.asyncio
async def test_model_selector_filter_shrinks_and_clearing_expands():
    """Filtering model selector shrinks list; clearing filter expands back."""
    mock_models = _make_mock_models(35)
    with patch("opscloud.ui.widgets.model_selector.get_available_models_list", return_value=mock_models):
        modal = ModelSelectorScreen()
        modal._recommended_only = False
        app = _ThemedModalApp(modal)
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            list_widget = modal.query_one(".model-list", VerticalScroll)
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value == MAX_VISIBLE_ROWS

            # Filter to single model
            filter_input = modal.query_one("#model-filter", Input)
            filter_input.value = "Test Model 01"
            await pilot.pause()
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value <= 4

            # Clear filter
            filter_input.value = ""
            await pilot.pause()
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value == MAX_VISIBLE_ROWS


@pytest.mark.asyncio
async def test_model_selector_short_terminal_bounds_list():
    """On short terminal (20 rows), model-list is bounded by viewport and dialog fits."""
    mock_models = _make_mock_models(35)
    with patch("opscloud.ui.widgets.model_selector.get_available_models_list", return_value=mock_models):
        modal = ModelSelectorScreen()
        modal._recommended_only = False
        app = _ThemedModalApp(modal)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            list_widget = modal.query_one(".model-list", VerticalScroll)
            assert list_widget.styles.height is not None
            assert list_widget.styles.height.value <= 8
            assert modal.dialog is not None
            assert modal.dialog.region.height <= 20
