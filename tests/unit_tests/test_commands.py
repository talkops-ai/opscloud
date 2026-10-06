"""Unit tests for OpsCloud slash command system and CommandRegistry."""

import pytest
from unittest.mock import MagicMock
from opscloud.commands.registry import CommandRegistry
from opscloud.commands._base import BaseCommandHandler, CommandContext, CommandResult
from opscloud.commands._types import BypassTier, CommandCategory, SafetyLevel
from opscloud.commands.core.exit_cmd import ExitHandler


@pytest.fixture(autouse=True)
def reset_registry():
    CommandRegistry.reset()
    yield
    CommandRegistry.reset()


def test_command_registry_discovery():
    registry = CommandRegistry.get_instance()
    all_cmds = registry.list_all_commands()
    assert "/exit" in all_cmds
    assert "/help" in all_cmds
    assert "/model" in all_cmds
    assert "/clear" in all_cmds


def test_exit_command_and_aliases():
    registry = CommandRegistry.get_instance()
    exit_handler = registry.get_handler("/exit")
    assert exit_handler is not None
    assert isinstance(exit_handler, ExitHandler)

    quit_handler = registry.get_handler("/quit")
    assert quit_handler is not None
    assert isinstance(quit_handler, ExitHandler)

    q_handler = registry.get_handler("/q")
    assert q_handler is not None
    assert isinstance(q_handler, ExitHandler)


@pytest.mark.asyncio
async def test_exit_command_execution():
    mock_app = MagicMock()
    mock_app.exit = MagicMock()
    mock_app.prepare_exit = MagicMock()

    ctx = CommandContext(app=mock_app, raw_command="/quit", args="")
    handler = ExitHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert mock_app.prepare_exit.called
    assert mock_app.exit.called


@pytest.mark.asyncio
async def test_dispatch_slash_command():
    registry = CommandRegistry.get_instance()
    mock_app = MagicMock()
    mock_app.exit = MagicMock()

    ctx = CommandContext(app=mock_app, raw_command="/q", args="")
    result = await registry.dispatch("/q", ctx)
    assert result.success is True
    assert mock_app.exit.called


def test_plugin_command_registration_and_source():
    registry = CommandRegistry.get_instance()

    class CustomPluginHandler(BaseCommandHandler):
        @property
        def name(self) -> str:
            return "/mycmd"

        @property
        def aliases(self) -> tuple[str, ...]:
            return ("/mc",)

        @property
        def category(self) -> CommandCategory:
            return CommandCategory.DEVOPS

        @property
        def safety_level(self) -> SafetyLevel:
            return SafetyLevel.READ_ONLY

        @property
        def bypass_tier(self) -> BypassTier:
            return BypassTier.NORMAL

        async def execute(self, ctx: CommandContext) -> CommandResult:
            return CommandResult(success=True, feedback_message="custom output")

    custom_handler = CustomPluginHandler()
    registry.register(custom_handler, source="plugin:custom-aws-plugin")

    handler = registry.get_handler("/mycmd")
    assert handler is custom_handler
    alias_handler = registry.get_handler("/mc")
    assert alias_handler is custom_handler

    assert registry.get_source("/mycmd") == "plugin:custom-aws-plugin"
    assert registry.get_source("/mc") == "plugin:custom-aws-plugin"
    assert registry.get_source("/exit") == "built-in"

    plugin_cmds = registry.list_plugin_commands()
    assert "custom-aws-plugin" in plugin_cmds
    assert custom_handler in plugin_cmds["custom-aws-plugin"]


@pytest.mark.asyncio
async def test_config_command_execution():
    from opscloud.commands.core.config_cmd import ConfigHandler
    from opscloud.config.settings import Settings

    handler = ConfigHandler()
    test_settings = Settings(aws_region="us-east-1")

    # 1. /config path
    ctx_path = CommandContext(app=None, raw_command="/config path", args="path", settings=test_settings)
    res_path = await handler.execute(ctx_path)
    assert res_path.success is True
    assert "config.toml" in res_path.message

    # 2. /config show (text fallback)
    ctx_show = CommandContext(app=None, raw_command="/config show", args="show", settings=test_settings)
    res_show = await handler.execute(ctx_show)
    assert res_show.success is True
    assert "Current Configuration" in res_show.message
    assert "aws.region" in res_show.message

    # 3. /config set
    ctx_set = CommandContext(app=None, raw_command="/config set aws_region eu-central-1", args="set aws_region eu-central-1", settings=test_settings)
    res_set = await handler.execute(ctx_set)
    assert res_set.success is True
    assert test_settings.aws_region == "eu-central-1"

    # 4. /config reset
    ctx_reset = CommandContext(app=None, raw_command="/config reset aws_region", args="reset aws_region", settings=test_settings)
    res_reset = await handler.execute(ctx_reset)
    assert res_reset.success is True
    assert test_settings.aws_region == "us-east-1"


@pytest.mark.asyncio
async def test_pool_command_execution():
    from opscloud.commands.core.pool import PoolHandler
    from opscloud.config.toml_config import clear_agent_pool, load_agent_pool
    from unittest.mock import AsyncMock

    handler = PoolHandler()
    clear_agent_pool()

    # 1. /pool status
    ctx_status = CommandContext(app=None, raw_command="/pool status", args="status")
    res_status = await handler.execute(ctx_status)
    assert res_status.success is True
    assert res_status.message is not None
    assert "Fast (Tier 0" in res_status.message

    # 2. /pool set fast=openai:fast standard=openai:std powerful=openai:pow
    ctx_set = CommandContext(
        app=None,
        raw_command="/pool set fast=openai:fast standard=openai:std powerful=openai:pow",
        args="set fast=openai:fast standard=openai:std powerful=openai:pow",
    )
    res_set = await handler.execute(ctx_set)
    assert res_set.success is True
    assert res_set.message is not None
    assert "Agent Model Pool Updated" in res_set.message
    saved = load_agent_pool()
    assert saved is not None
    assert saved["fast"] == "openai:fast"
    assert saved["standard"] == "openai:std"
    assert saved["powerful"] == "openai:pow"

    # 3. /pool clear
    ctx_clear = CommandContext(app=None, raw_command="/pool clear", args="clear")
    res_clear = await handler.execute(ctx_clear)
    assert res_clear.success is True
    assert load_agent_pool() is None

    # 4. /pool (interactive modal invocation)
    mock_app = MagicMock()
    mock_app._show_pool_selector = AsyncMock()
    ctx_ui = CommandContext(app=mock_app, raw_command="/pool", args="")
    res_ui = await handler.execute(ctx_ui)
    assert res_ui.success is True
    mock_app._show_pool_selector.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_selector_screen_mount():
    from textual.app import App
    from textual.widgets import Select
    from opscloud.ui.widgets.pool_selector import PoolSelectorScreen
    from opscloud.config.toml_config import clear_agent_pool

    clear_agent_pool()

    class TestApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(PoolSelectorScreen(active_provider="openai"))

    test_app = TestApp()
    async with test_app.run_test(size=(120, 50)) as pilot:
        screen = test_app.screen
        assert isinstance(screen, PoolSelectorScreen)
        s_fast = screen.query_one("#select-fast", Select)
        s_std = screen.query_one("#select-standard", Select)
        assert s_fast is not None
        assert s_std is not None
        assert screen.query_one("#select-powerful") is not None

        # 1. Expand s_fast
        await pilot.click("#select-fast")
        assert s_fast.expanded

        # 2. Escape collapses dropdown without dismissing screen
        await pilot.press("escape")
        assert not s_fast.expanded
        assert isinstance(test_app.screen, PoolSelectorScreen)

        # 3. Outside click collapses dropdown
        await pilot.click("#select-fast")
        assert s_fast.expanded
        await pilot.click(".pool-title")
        assert not s_fast.expanded

        # 4. Click cancel button dismisses screen
        await pilot.click("#btn-cancel")
        assert not isinstance(test_app.screen, PoolSelectorScreen)


async def test_pool_selector_buttons_visible_in_constrained_terminal():
    from textual.app import App
    from opscloud.ui.widgets.pool_selector import PoolSelectorScreen
    from opscloud.config.toml_config import clear_agent_pool

    clear_agent_pool()

    class TestApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(PoolSelectorScreen(active_provider="google_genai"))

    test_app = TestApp()
    # Test on a compact 80x26 terminal (standard laptop/split-pane height)
    async with test_app.run_test(size=(80, 26)):
        screen = test_app.screen
        assert isinstance(screen, PoolSelectorScreen)

        dialog = screen.query_one("PoolSelectorScreen > Vertical")
        btn_save = screen.query_one("#btn-save")
        btn_reset = screen.query_one("#btn-reset")
        btn_cancel = screen.query_one("#btn-cancel")
        help_bar = screen.query_one(".pool-help")

        dialog_bottom = dialog.region.y + dialog.region.height

        # Ensure all action buttons and help footer are within the visible dialog boundary
        assert btn_save.visible
        assert btn_reset.visible
        assert btn_cancel.visible
        assert (btn_save.region.y + btn_save.region.height) <= dialog_bottom
        assert (btn_reset.region.y + btn_reset.region.height) <= dialog_bottom
        assert (btn_cancel.region.y + btn_cancel.region.height) <= dialog_bottom
        assert (help_bar.region.y + help_bar.region.height) <= dialog_bottom


async def test_pool_selector_save_and_reset_actions():
    from textual.app import App
    from opscloud.ui.widgets.pool_selector import PoolSelectorScreen
    from opscloud.config.toml_config import clear_agent_pool, load_agent_pool

    clear_agent_pool()

    saved_result = None

    def on_dismiss(res):
        nonlocal saved_result
        saved_result = res

    class TestApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(PoolSelectorScreen(active_provider="google_genai"), on_dismiss)

    test_app = TestApp()
    async with test_app.run_test(size=(100, 35)) as pilot:
        screen = test_app.screen
        assert isinstance(screen, PoolSelectorScreen)

        # Click save button
        await pilot.click("#btn-save")
        await pilot.pause()

        # Modal dismissed and saved pool persisted to config
        assert saved_result is not None
        assert "fast" in saved_result
        assert "standard" in saved_result
        assert "powerful" in saved_result

        persisted = load_agent_pool()
        assert persisted is not None
        assert persisted["fast"] == saved_result["fast"]

        # Reopen and test reset
        reset_result = None

        def on_reset_dismiss(res):
            nonlocal reset_result
            reset_result = res

        screen2 = PoolSelectorScreen(active_provider="google_genai")
        test_app.push_screen(screen2, on_reset_dismiss)
        await pilot.pause()

        await pilot.click("#btn-reset")
        await pilot.pause()

        assert reset_result == {"_cleared": True}
        assert load_agent_pool() is None


@pytest.mark.asyncio
async def test_goal_command_conversational_amendment_when_active():
    from opscloud.commands.power.goal import GoalHandler, get_goal_state
    from unittest.mock import AsyncMock

    mock_app = MagicMock()
    mock_app._run_goal_criteria_request = AsyncMock()
    state = get_goal_state(mock_app)
    state.objective = "Create a file named nginx.conf with http and server blocks"
    state.status = "active"
    state.rubric = (
        "- nginx.conf is created in working directory\n"
        "- nginx.conf contains http block\n"
        "- The nginx.conf configuration syntax is valid."
    )

    ctx = CommandContext(
        app=mock_app,
        raw_command="/goal lets skip the nginx.conf configuration check",
        args="lets skip the nginx.conf configuration check",
    )
    handler = GoalHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert mock_app._run_goal_criteria_request.called
    call_req = mock_app._run_goal_criteria_request.call_args[0][0]
    assert call_req["kind"] == "amend"
    assert call_req["objective"] == "Create a file named nginx.conf with http and server blocks"
    assert "nginx.conf is created in working directory" in call_req["criteria"]
    assert call_req["feedback"] == "lets skip the nginx.conf configuration check"


@pytest.mark.asyncio
async def test_goal_command_conversational_amendment_when_blocked():
    from opscloud.commands.power.goal import GoalHandler, get_goal_state
    from unittest.mock import AsyncMock

    mock_app = MagicMock()
    mock_app._run_goal_criteria_request = AsyncMock()
    state = get_goal_state(mock_app)
    state.objective = "Deploy ECS service"
    state.status = "blocked"
    state.rubric = "- Cluster healthy\n- Service running\n- ALB target healthy"

    ctx = CommandContext(
        app=mock_app,
        raw_command="/goal skip ALB target check",
        args="skip ALB target check",
    )
    handler = GoalHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert mock_app._run_goal_criteria_request.called
    call_req = mock_app._run_goal_criteria_request.call_args[0][0]
    assert call_req["kind"] == "amend"
    assert call_req["objective"] == "Deploy ECS service"
    assert call_req["feedback"] == "skip ALB target check"


@pytest.mark.asyncio
async def test_goal_command_explicit_new_replaces_active():
    from opscloud.commands.power.goal import GoalHandler, get_goal_state
    from unittest.mock import AsyncMock

    mock_app = MagicMock()
    mock_app._run_goal_criteria_request = AsyncMock()
    state = get_goal_state(mock_app)
    state.objective = "Old nginx goal"
    state.status = "active"
    state.rubric = "- Old criteria"

    ctx = CommandContext(
        app=mock_app,
        raw_command="/goal new Deploy Postgres RDS database",
        args="new Deploy Postgres RDS database",
    )
    handler = GoalHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert mock_app._run_goal_criteria_request.called
    call_req = mock_app._run_goal_criteria_request.call_args[0][0]
    assert call_req["kind"] == "create"
    assert call_req["objective"] == "Deploy Postgres RDS database"


@pytest.mark.asyncio
async def test_goal_command_create_when_no_active_goal():
    from opscloud.commands.power.goal import GoalHandler, get_goal_state
    from unittest.mock import AsyncMock

    mock_app = MagicMock()
    mock_app._run_goal_criteria_request = AsyncMock()
    state = get_goal_state(mock_app)
    state.clear()

    ctx = CommandContext(
        app=mock_app,
        raw_command="/goal Create a file named nginx.conf",
        args="Create a file named nginx.conf",
    )
    handler = GoalHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    assert mock_app._run_goal_criteria_request.called
    call_req = mock_app._run_goal_criteria_request.call_args[0][0]
    assert call_req["kind"] == "create"
    assert call_req["objective"] == "Create a file named nginx.conf"


@pytest.mark.asyncio
async def test_context_command_no_emojis_and_complete_metrics():
    from opscloud.commands.core.context import ContextHandler
    from unittest.mock import AsyncMock

    mock_app = MagicMock()
    mock_app._model = "google_genai:gemini-3.5-flash"
    mock_app.get_context_tokens = MagicMock(return_value=14074)
    mock_app.get_conversation_token_count = AsyncMock(return_value=119)
    mock_app._cumulative_session_tokens = 37658
    mock_app._session_cost_usd = 0.03579855
    mock_adapter = MagicMock()
    mock_adapter._stats = MagicMock(request_count=3, input_tokens=36420, output_tokens=1238, total_cost_usd=0.03579855)
    mock_app._adapter = mock_adapter

    mock_app.get_active_tools = MagicMock(return_value=["fetch_url", "bash", "edit_file"])
    mock_app.get_mcp_servers = MagicMock(return_value=[])
    mock_app.get_discovered_skills = MagicMock(return_value=["skill-creator", "cloud-doctor"])

    ctx = CommandContext(app=mock_app, raw_command="/context", args="")
    handler = ContextHandler()
    result = await handler.execute(ctx)

    assert result.success is True
    msg = result.message

    # Verify NO emojis in message
    emojis = ["📊", "🧩", "☁️", "🤖", "⚡", "✨", "🔥"]
    for e in emojis:
        assert e not in msg, f"Found emoji {e} in context command output"

    # Verify context window and breakdown
    assert "**Context Window:**" in msg
    assert "14,074" in msg
    assert "System Prompt + Tools:" in msg
    assert "Conversation History:" in msg
    assert "Remaining Space:" in msg

    # Verify session totals
    assert "**Session Total:**" in msg
    assert "37,658 tokens" in msg
    assert "$0.04" in msg
    assert "(3 requests)" in msg
    assert "Input Tokens:" in msg
    assert "36,420" in msg
    assert "Output Tokens:" in msg
    assert "1,238" in msg

    # Verify active resources
    assert "**Active Resources:**" in msg
    assert "3 tools" in msg
    assert "2 skills" in msg
    assert "**Tools:** fetch_url, bash, edit_file" in msg
    assert "**Skills:** skill-creator, cloud-doctor" in msg




