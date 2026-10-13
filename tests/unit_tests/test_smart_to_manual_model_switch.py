"""Unit tests for restoring the default model when transitioning from Smart to Manual mode."""

import pytest
from unittest.mock import MagicMock

from opscloud.approval_mode import ApprovalMode
from opscloud.ui.app import OpsCloudApp
from opscloud.config.settings import get_settings
from opscloud.ui.textual_adapter import TextualAdapter


@pytest.mark.asyncio
async def test_smart_to_manual_switches_back_to_default_model(monkeypatch, tmp_path):
    """Verify that when a model was routed in smart mode, switching back to manual mode restores default model."""
    # Mock config.toml to specify gpt-5.6-luna as default
    test_config = tmp_path / "config.toml"
    test_config.write_text('[models]\ndefault = "openai:gpt-5.6-luna"\n')
    monkeypatch.setattr("opscloud.config.paths.CONFIG_PATH", test_config)
    monkeypatch.setattr("opscloud.config.toml_config.CONFIG_PATH", test_config)

    app = OpsCloudApp(model="openai:gpt-5.6-luna", defer_server_start=True)
    assert app._model == "openai:gpt-5.6-luna"
    assert app._manual_model == "openai:gpt-5.6-luna"
    assert app._smart_routed_active is False

    # Switch to SMART mode
    await app._set_approval_mode(ApprovalMode.SMART)
    assert app._approval_mode == ApprovalMode.SMART
    assert app._manual_model == "openai:gpt-5.6-luna"

    # Simulate smart mode routing event (e.g. 'hi how are you' routes to gemini-3.6-flash)
    adapter = TextualAdapter(client=MagicMock(), assistant_id="opscloud", app=app, status_bar=MagicMock())
    adapter._status_bar = MagicMock()
    app._status_bar = adapter._status_bar
    app._adapter = adapter

    event = {
        "type": "model_routed",
        "provider": "google_genai",
        "model": "gemini-3.6-flash",
        "effort": "off",
        "is_subagent": False,
    }
    adapter._handle_custom_stream_event(event, is_main_agent=True)

    # In smart mode, app._model and status bar now reflect the routed model
    assert app._model == "google_genai:gemini-3.6-flash"
    assert app._smart_routed_active is True
    assert app._manual_model == "openai:gpt-5.6-luna"
    adapter._status_bar.set_model.assert_called_with(
        provider="google_genai",
        model="gemini-3.6-flash",
        effort="off",
    )

    # Now switch back to MANUAL mode (e.g. via /manual)
    await app._set_approval_mode(ApprovalMode.MANUAL)
    assert app._approval_mode == ApprovalMode.MANUAL

    # Verify that the model is restored to the default model (openai:gpt-5.6-luna)
    assert app._model == "openai:gpt-5.6-luna"
    assert app._smart_routed_active is False

    # Verify status bar was updated back to default model
    adapter._status_bar.set_model.assert_called_with(
        provider="openai",
        model="gpt-5.6-luna",
        effort="medium",
    )

    # Verify settings singleton was updated back
    settings = get_settings()
    assert settings.model == "openai:gpt-5.6-luna"


@pytest.mark.asyncio
async def test_smart_to_manual_preserves_explicit_pre_smart_model(monkeypatch, tmp_path):
    """Verify that if no explicit default is configured, the pre-smart manual model is restored."""
    test_config = tmp_path / "config.toml"
    test_config.write_text('[models]\n')
    monkeypatch.setattr("opscloud.config.paths.CONFIG_PATH", test_config)
    monkeypatch.setattr("opscloud.config.toml_config.CONFIG_PATH", test_config)

    app = OpsCloudApp(model="anthropic:claude-sonnet-4-5", defer_server_start=True)
    status_bar_mock = MagicMock()
    app._status_bar = status_bar_mock

    # Switch to SMART mode
    await app._set_approval_mode(ApprovalMode.SMART)
    assert app._manual_model == "anthropic:claude-sonnet-4-5"

    # Simulate smart mode routing event to gemini-3.6-flash
    adapter = TextualAdapter(client=MagicMock(), assistant_id="opscloud", app=app, status_bar=status_bar_mock)
    app._adapter = adapter

    adapter._handle_custom_stream_event({
        "type": "model_routed",
        "provider": "google_genai",
        "model": "gemini-3.6-flash",
        "effort": "off",
        "is_subagent": False,
    }, is_main_agent=True)

    assert app._model == "google_genai:gemini-3.6-flash"
    assert app._smart_routed_active is True

    # Switch back to MANUAL mode
    await app._set_approval_mode(ApprovalMode.MANUAL)
    assert app._approval_mode == ApprovalMode.MANUAL
    assert app._model == "anthropic:claude-sonnet-4-5"
    assert app._smart_routed_active is False
    status_bar_mock.set_model.assert_called_with(
        provider="anthropic",
        model="claude-sonnet-4-5",
        effort="",
    )


@pytest.mark.asyncio
async def test_smart_to_auto_mode_also_restores_default_model(monkeypatch, tmp_path):
    """Verify that switching from Smart to Auto mode also restores default model."""
    test_config = tmp_path / "config.toml"
    test_config.write_text('[models]\ndefault = "openai:gpt-5.6-luna"\n')
    monkeypatch.setattr("opscloud.config.paths.CONFIG_PATH", test_config)
    monkeypatch.setattr("opscloud.config.toml_config.CONFIG_PATH", test_config)

    app = OpsCloudApp(model="openai:gpt-5.6-luna", defer_server_start=True)
    status_bar_mock = MagicMock()
    app._status_bar = status_bar_mock
    app._notify_auto_mode_enabled_once = MagicMock()

    await app._set_approval_mode(ApprovalMode.SMART)
    adapter = TextualAdapter(client=MagicMock(), assistant_id="opscloud", app=app, status_bar=status_bar_mock)
    app._adapter = adapter

    adapter._handle_custom_stream_event({
        "type": "model_routed",
        "provider": "google_genai",
        "model": "gemini-3.6-flash",
        "effort": "off",
        "is_subagent": False,
    }, is_main_agent=True)

    assert app._model == "google_genai:gemini-3.6-flash"

    # Switch to AUTO mode
    await app._set_approval_mode(ApprovalMode.AUTO)
    assert app._approval_mode == ApprovalMode.AUTO
    assert app._model == "openai:gpt-5.6-luna"
    assert app._smart_routed_active is False


@pytest.mark.asyncio
async def test_jev_router_bypasses_and_clears_stale_routes_in_manual_mode():
    """Verify that in Manual mode, Jev router clears stale checkpointed routes and does not override model calls."""
    from opscloud.middleware.jev_model_router import JevDynamicModelRouterMiddleware
    from langchain_core.messages import HumanMessage
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    middleware = JevDynamicModelRouterMiddleware()

    class DummyRuntime:
        def __init__(self, context=None):
            self.context = context or {}

    manual_runtime = DummyRuntime(context={"approval_mode": "manual", "smart": False, "model": "openai:gpt-5.6-luna"})

    # Prior smart turn left stale routes in checkpoint state
    stale_state = {
        "messages": [HumanMessage(content="what was my previous question ?")],
        "model_route": MagicMock(choice="fast", confidence=1.0),
        "_dynamic_model_route": {
            "tier": 0,
            "spec": "google_genai:gemini-3.6-flash",
            "effort": "off",
            "route": "fast",
        },
    }

    # 1. abefore_agent in manual mode must return dict clearing stale routes from state
    clear_result = await middleware.abefore_agent(stale_state, manual_runtime)
    assert clear_result == {"model_route": None, "_dynamic_model_route": None}

    # 2. awrap_model_call with stale checkpoint state must NOT override the model call
    base_model = MagicMock()
    mock_request = MagicMock(spec=ModelRequest)
    mock_request.runtime = manual_runtime
    mock_request.model = base_model
    mock_request.model_settings = {}
    mock_request.state = stale_state

    handler_received_request = None

    async def fake_handler(req):
        nonlocal handler_received_request
        handler_received_request = req
        return MagicMock(spec=ModelResponse)

    await middleware.awrap_model_call(mock_request, fake_handler)

    # Handler must be called with the original request, untouched by Jev
    assert handler_received_request is mock_request
    assert not mock_request.override.called


@pytest.mark.asyncio
async def test_textual_adapter_ignores_model_routed_event_in_manual_mode():
    """Verify that adapter drops any stray model_routed events if the UI is in manual mode."""
    app = OpsCloudApp(model="openai:gpt-5.6-luna", defer_server_start=True)
    status_bar_mock = MagicMock()
    app._status_bar = status_bar_mock
    app._approval_mode = ApprovalMode.MANUAL

    adapter = TextualAdapter(client=MagicMock(), assistant_id="opscloud", app=app, status_bar=status_bar_mock)
    app._adapter = adapter

    event = {
        "type": "model_routed",
        "provider": "google_genai",
        "model": "gemini-3.6-flash",
        "effort": "off",
        "is_subagent": False,
    }
    adapter._handle_custom_stream_event(event, is_main_agent=True)

    # Status bar and app model must remain untouched
    assert app._model == "openai:gpt-5.6-luna"
    assert app._smart_routed_active is False
    assert not status_bar_mock.set_model.called

