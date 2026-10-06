"""Unit tests for PluginManagerScreen search functionality and filtering."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList

from opscloud.ui.widgets.plugin_manager import (
    PluginManagerScreen,
    _ManagerState,
    _MarketplaceRow,
    _PluginRow,
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


def _sample_plugins() -> tuple[_PluginRow, ...]:
    return (
        _PluginRow(
            plugin_id="aws-iac-engineer@talkops-devops-plugins",
            description="Authors, validates, deploys, and troubleshoots AWS infrastructure-as-code: CDK, CloudFormation, AWS Blocks...",
            enabled=False,
            version="0.1.0",
            author="TalkOps AI",
            display_name="AWS IaC Engineer",
        ),
        _PluginRow(
            plugin_id="aws-platform-engineer@talkops-devops-plugins",
            description="Provisions and operates the AWS runtime platform: EC2 and Auto Scaling, EKS/ECS/Fargate containers, VPC and edge networking...",
            enabled=False,
            version="0.1.0",
            author="TalkOps AI",
            display_name="AWS Platform Engineer",
        ),
        _PluginRow(
            plugin_id="aws-sre-agent@talkops-devops-plugins",
            description="Site-reliability agent for AWS workloads: CloudWatch metrics, logs, alarms and Logs Insights, Application Signals SLOs...",
            enabled=False,
            version="0.1.0",
            author="TalkOps AI",
            display_name="AWS SRE Agent",
        ),
    )


def test_filtered_plugins_query_matching() -> None:
    """Verify that _filtered_plugins correctly filters rows by partial query."""
    screen = PluginManagerScreen()
    plugins = _sample_plugins()

    # Empty query returns all
    screen._search_query = ""
    assert len(screen._filtered_plugins(plugins)) == 3

    # "platfo" matches ONLY AWS Platform Engineer
    screen._search_query = "platfo"
    filtered = screen._filtered_plugins(plugins)
    assert len(filtered) == 1
    assert filtered[0].display_name == "AWS Platform Engineer"

    # Case-insensitivity test: "PLATFO"
    screen._search_query = "PLATFO"
    filtered = screen._filtered_plugins(plugins)
    assert len(filtered) == 1
    assert filtered[0].display_name == "AWS Platform Engineer"

    # Query matching description: "CloudFormation" matches AWS IaC Engineer
    screen._search_query = "cloudformation"
    filtered = screen._filtered_plugins(plugins)
    assert len(filtered) == 1
    assert filtered[0].display_name == "AWS IaC Engineer"

    # Non-matching query returns empty
    screen._search_query = "nonexistent_term_xyz"
    filtered = screen._filtered_plugins(plugins)
    assert len(filtered) == 0


@pytest.mark.asyncio
async def test_plugin_manager_search_input_updates_view() -> None:
    """Verify that typing into search input updates options list in real time."""
    screen = PluginManagerScreen()
    plugins = _sample_plugins()
    marketplaces = (_MarketplaceRow(name="talkops-devops-plugins", source="local", plugin_count=3, installed_count=0),)
    mock_state = _ManagerState(available_plugins=plugins, installed_plugins=(), marketplaces=marketplaces, errors=())

    with patch("opscloud.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = DummyPluginManagerApp(screen)
        async with app.run_test() as pilot:
            # Let initial state load
            await pilot.pause()

            options = screen.query_one("#plugin-manager-options", OptionList)
            search_input = screen.query_one("#plugin-manager-search", Input)

            # Initially 3 plugins are shown (plus spacers between them = 5 total option items)
            assert options.option_count >= 3

            # Focus search and type "platfo"
            search_input.focus()
            await pilot.pause()

            # Type "platfo" into search input
            search_input.value = "platfo"
            await pilot.pause()

            # Verify _search_query was updated by on_input_changed
            assert screen._search_query == "platfo"

            # Verify only 1 plugin is now shown
            # OptionList should only have 1 plugin option (id starts with detail:aws-platform-engineer)
            plugin_option_ids = [
                options.get_option_at_index(i).id
                for i in range(options.option_count)
                if options.get_option_at_index(i).id and not options.get_option_at_index(i).id.startswith("spacer:")
            ]
            assert plugin_option_ids == ["detail:aws-platform-engineer@talkops-devops-plugins"]

            # Clear search
            search_input.value = ""
            await pilot.pause()
            assert screen._search_query == ""

            # All 3 plugins return
            plugin_option_ids_all = [
                options.get_option_at_index(i).id
                for i in range(options.option_count)
                if options.get_option_at_index(i).id and not options.get_option_at_index(i).id.startswith("spacer:")
            ]
            assert len(plugin_option_ids_all) == 3


@pytest.mark.asyncio
async def test_plugin_manager_cancel_clears_search() -> None:
    """Verify that action_cancel clears search input first when focused."""
    screen = PluginManagerScreen()
    plugins = _sample_plugins()
    marketplaces = (_MarketplaceRow(name="talkops-devops-plugins", source="local", plugin_count=3, installed_count=0),)
    mock_state = _ManagerState(available_plugins=plugins, installed_plugins=(), marketplaces=marketplaces, errors=())

    with patch("opscloud.ui.widgets.plugin_manager._load_manager_state", return_value=mock_state):
        app = DummyPluginManagerApp(screen)
        async with app.run_test() as pilot:
            await pilot.pause()

            search_input = screen.query_one("#plugin-manager-search", Input)
            search_input.focus()
            search_input.value = "platfo"
            await pilot.pause()

            assert screen._search_query == "platfo"

            # Press Escape (action_cancel)
            screen.action_cancel()
            await pilot.pause()

            # Search query should be cleared, search input still has focus
            assert screen._search_query == ""
            assert search_input.value == ""
            assert search_input.has_focus
