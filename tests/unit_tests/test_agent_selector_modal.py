"""Unit tests for AgentSelectorScreen OpsModalScreen migration."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from opscloud.ui.widgets.agent_selector import AgentSelectorScreen


class DummyAgentApp(App[None]):
    """Minimal textual app to mount and test AgentSelectorScreen."""

    def __init__(self, screen_instance: AgentSelectorScreen) -> None:
        super().__init__()
        self._test_screen = screen_instance

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self._test_screen)


@pytest.mark.asyncio
async def test_agent_selector_screen_mount_and_options() -> None:
    agents = ["default", "devops-engineer", "security-auditor"]
    screen = AgentSelectorScreen(
        current_agent="devops-engineer",
        agent_names=agents,
        default_agent="default",
    )
    app = DummyAgentApp(screen)

    async with app.run_test() as pilot:
        opt_list = screen.query_one("#agent-options", OptionList)
        assert opt_list is not None
        assert opt_list.has_focus
        assert opt_list.option_count == 3
        # Current agent is index 1 ("devops-engineer")
        assert opt_list.highlighted == 1

        # Check navigation
        screen.action_cursor_down()
        await pilot.pause()
        assert opt_list.highlighted == 2

        # Check escape / cancel
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1


@pytest.mark.asyncio
async def test_agent_selector_screen_empty() -> None:
    screen = AgentSelectorScreen(
        current_agent=None,
        agent_names=[],
    )
    app = DummyAgentApp(screen)

    async with app.run_test() as pilot:
        # No agent-options list mounted
        assert not screen.query("#agent-options")

        # Empty-state widget is mounted with agent-selector-empty class and visible text
        empty_widget = screen.query_one(".agent-selector-empty")
        assert empty_widget is not None
        assert "No agents found in ~/.opscloud/" in str(empty_widget.render())
        # Display is not 'none'
        assert empty_widget.styles.display != "none"

        # Modal subtitle in empty state only offers esc close
        assert screen.dialog is not None
        subtitle_plain = str(screen.dialog.border_subtitle)
        assert "close" in subtitle_plain
        assert "navigate" not in subtitle_plain
        assert "select" not in subtitle_plain

        # Cancel dismisses cleanly
        screen.action_cancel()
        await pilot.pause()
        assert len(app._screen_stack) == 1


@pytest.mark.asyncio
async def test_agent_selector_responsive_row_width() -> None:
    """Agent selector adapts row formatting width to available space."""
    agents = ["default", "devops-engineer", "security-auditor"]
    screen_wide = AgentSelectorScreen(
        current_agent="devops-engineer",
        agent_names=agents,
        default_agent="default",
    )
    app_wide = DummyAgentApp(screen_wide)

    # Wide terminal (120 cols)
    async with app_wide.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        opt_list = screen_wide.query_one("#agent-options", OptionList)
        assert opt_list is not None
        wide_w = screen_wide._row_width()
        assert wide_w >= 50

    screen_narrow = AgentSelectorScreen(
        current_agent="devops-engineer",
        agent_names=agents,
        default_agent="default",
    )
    app_narrow = DummyAgentApp(screen_narrow)

    # Narrow terminal (60 cols)
    async with app_narrow.run_test(size=(60, 25)) as pilot:
        await pilot.pause()
        opt_list = screen_narrow.query_one("#agent-options", OptionList)
        assert opt_list is not None
        narrow_w = screen_narrow._row_width()
        assert narrow_w <= wide_w
        assert narrow_w < 60


@pytest.mark.asyncio
async def test_agents_handler_lists_plugin_agents() -> None:
    from unittest.mock import MagicMock
    from opscloud.commands._base import CommandContext
    from opscloud.commands.power.agents import AgentsHandler

    handler = AgentsHandler()
    assert handler.name == "/agent"
    assert "/agents" in handler.aliases

    mock_app = MagicMock()
    mock_app.get_discovered_plugin_agents.return_value = [
        {"name": "test-agent@plugin", "plugin_id": "plugin", "description": "Test subagent"}
    ]

    ctx = CommandContext(app=mock_app, raw_command="/agent", args="")
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.message is not None
    assert "Available Plugin Agents" in result.message
    assert "test-agent" in result.message


@pytest.mark.asyncio
async def test_agents_handler_activates_session() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from opscloud.commands._base import CommandContext
    from opscloud.commands.power.agents import AgentsHandler, get_agent_session_state

    handler = AgentsHandler()
    mock_app = MagicMock()
    mock_app._run_agent_session_request = AsyncMock()
    mock_app.get_discovered_plugin_agents.return_value = [
        {"name": "aws-compute@talkops", "plugin_id": "talkops", "description": "AWS specialist"}
    ]

    ctx = CommandContext(app=mock_app, raw_command="/agent aws-compute", args="aws-compute", thread_id="t1")
    result = await handler.execute(ctx)

    assert result.success is True
    assert result.message is not None
    assert "Activated agent persona" in result.message
    state = get_agent_session_state(mock_app)
    assert state.is_active is True
    assert state.short_name == "aws-compute"
    mock_app._run_agent_session_request.assert_called_once()



