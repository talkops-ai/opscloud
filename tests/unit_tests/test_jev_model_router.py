"""Unit tests for Jev-powered dynamic model and reasoning router."""

from __future__ import annotations

import logging
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_typesafe import TypeSafeClassifier

from opscloud.middleware.jev_model_router import (
    JevDynamicModelRouterMiddleware,
    ModelChoice,
    ModelRouterMiddleware,
)
from opscloud.model.pool import DynamicModelPoolManager


class DummyRuntime:
    def __init__(self, context=None):
        self.context = context


@pytest.fixture(autouse=True)
def isolate_agent_pool(monkeypatch):
    from opscloud.config import toml_config

    orig_load = toml_config.load_agent_pool

    def _safe_load(config_path=None):
        if config_path is None:
            return None
        return orig_load(config_path)

    monkeypatch.setattr("opscloud.config.toml_config.load_agent_pool", _safe_load)


@pytest.fixture
def pool_manager():
    return DynamicModelPoolManager()


def test_dynamic_tier_discovery_google_genai(pool_manager):
    tiers = pool_manager.discover_tiers(provider="google_genai")
    assert 0 in tiers
    assert 1 in tiers
    assert 2 in tiers

    spec0, effort0 = tiers[0]
    spec1, effort1 = tiers[1]
    spec2, effort2 = tiers[2]

    assert effort0 == "off"
    assert effort2 in ("high", "max")
    assert "google_genai" in spec0
    assert "google_genai" in spec2


def test_dynamic_tier_discovery_openai(pool_manager):
    tiers = pool_manager.discover_tiers(provider="openai")
    assert len(tiers) == 3
    assert tiers[0][1] == "off"
    assert tiers[2][1] in ("high", "max", "medium")
    assert "openai" in tiers[0][0]


def test_dynamic_tier_discovery_anthropic(pool_manager):
    tiers = pool_manager.discover_tiers(provider="anthropic")
    assert len(tiers) == 3
    assert tiers[0][1] == "off"
    assert tiers[2][1] in ("high", "max")
    assert "anthropic" in tiers[0][0]


def test_dynamic_tiering_with_single_reasoning_base_spec(pool_manager):
    tiers = pool_manager.discover_tiers(base_spec="google_genai:gemini-3.7-flash")
    assert tiers[0][1] == "off"
    assert tiers[2][1] == "high"


def test_dynamic_tier_discovery_all_22_providers(pool_manager):
    from opscloud.model.config import AVAILABLE_MODELS

    for prov in AVAILABLE_MODELS:
        tiers = pool_manager.discover_tiers(provider=prov)
        assert 0 in tiers
        assert 1 in tiers
        assert 2 in tiers

        t0_spec, t0_effort = tiers[0]
        t1_spec, t1_effort = tiers[1]
        t2_spec, t2_effort = tiers[2]

        assert prov in t0_spec
        assert prov in t1_spec
        assert prov in t2_spec
        assert t0_effort in ("off", "minimal", "none")


def test_pool_manager_get_model_choices(pool_manager):
    # 1. Orchestrator criteria (default)
    choices = pool_manager.get_model_choices(provider="google_genai", is_subagent=False)
    assert "fast" in choices
    assert "standard" in choices
    assert "powerful" in choices

    assert isinstance(choices["fast"], ModelChoice)
    assert isinstance(choices["standard"], ModelChoice)
    assert isinstance(choices["powerful"], ModelChoice)

    fast_crit = choices["fast"].criteria
    standard_crit = choices["standard"].criteria
    powerful_crit = choices["powerful"].criteria

    assert isinstance(fast_crit, str)
    assert isinstance(standard_crit, str)
    assert isinstance(powerful_crit, str)

    # Candidate model specs and efforts are embedded directly in criteria for Jev
    assert "Model: google_genai:gemini-3.6-flash (effort: off)" in fast_crit
    assert "Model: google_genai:gemini-3.8-flash (effort: medium)" in standard_crit
    assert "Model: google_genai:gemini-3.1-pro-preview (effort: high)" in powerful_crit

    assert "Conversational greetings" in fast_crit
    assert "Focused single-domain" in standard_crit
    assert "Multi-agent delegation" in powerful_crit

    # 2. Subagent criteria
    sub_choices = pool_manager.get_model_choices(provider="google_genai", is_subagent=True)
    sub_fast_crit = sub_choices["fast"].criteria
    sub_standard_crit = sub_choices["standard"].criteria
    sub_powerful_crit = sub_choices["powerful"].criteria

    assert isinstance(sub_fast_crit, str)
    assert isinstance(sub_standard_crit, str)
    assert isinstance(sub_powerful_crit, str)

    assert "Direct single-item lookups" in sub_fast_crit
    assert "Domain-specific workflows" in sub_standard_crit
    assert "Deep cross-service root-cause correlation" in sub_powerful_crit


@pytest.mark.asyncio
async def test_middleware_sends_candidate_models_in_question_choices():
    middleware = JevDynamicModelRouterMiddleware()

    smart_runtime = DummyRuntime(context={"approval_mode": "smart", "smart": True, "model": "google_genai:gemini-3.7-flash"})
    state = {"messages": [HumanMessage(content="what is my current namespace")]}

    mock_choice = MagicMock(choice="fast", confidence=1.0)
    mock_response = MagicMock(choices={"model_route": mock_choice})

    with patch.object(middleware, "is_available", return_value=True):
        with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = mock_response

            with patch("opscloud.middleware.jev_model_router.adispatch_custom_event", new=AsyncMock()):
                result = await middleware.abefore_agent(state, smart_runtime)
                assert result is not None

                mock_inv.assert_called_once()
                inv_payload = mock_inv.call_args[0][0]
                assert "questions" in inv_payload
                assert "model_route" in inv_payload["questions"]
                criteria = inv_payload["questions"]["model_route"].criteria

                assert "fast" in criteria
                assert "standard" in criteria
                assert "powerful" in criteria
                fast_route_crit = criteria["fast"]
                standard_route_crit = criteria["standard"]
                powerful_route_crit = criteria["powerful"]
                assert isinstance(fast_route_crit, str)
                assert isinstance(standard_route_crit, str)
                assert isinstance(powerful_route_crit, str)
                assert "google_genai:gemini-3.6-flash" in fast_route_crit
                assert "google_genai:gemini-3.7-flash" in standard_route_crit
                assert "google_genai:gemini-3.1-pro-preview" in powerful_route_crit


def test_auto_detect_provider_with_various_credentials(pool_manager):
    with patch("opscloud.model.pool.has_provider_credentials", side_effect=lambda p: p == "xai"):
        with patch("opscloud.model.config.ModelConfig.load") as mock_cfg:
            mock_cfg.return_value.default_model = None
            mock_cfg.return_value.recent_model = None
            assert pool_manager._resolve_provider() == "xai"

    with patch("opscloud.model.pool.has_provider_credentials", side_effect=lambda p: p == "mistralai"):
        with patch("opscloud.model.config.ModelConfig.load") as mock_cfg:
            mock_cfg.return_value.default_model = None
            mock_cfg.return_value.recent_model = None
            assert pool_manager._resolve_provider() == "mistralai"

    with patch("opscloud.model.pool.has_provider_credentials", side_effect=lambda p: p == "cohere"):
        with patch("opscloud.model.config.ModelConfig.load") as mock_cfg:
            mock_cfg.return_value.default_model = None
            mock_cfg.return_value.recent_model = None
            assert pool_manager._resolve_provider() == "cohere"


def test_thinking_level_max_effort_prioritization(pool_manager):
    tiers_anthropic = pool_manager.discover_tiers(provider="anthropic")
    assert tiers_anthropic[2][1] == "max"

    tiers_openai = pool_manager.discover_tiers(provider="openai")
    assert tiers_openai[2][1] == "max"


@pytest.mark.asyncio
async def test_official_model_router_custom_choices(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "mock-test-key-typesafe")
    mock_fast = MagicMock(spec=BaseChatModel)
    mock_powerful = MagicMock(spec=BaseChatModel)

    router = ModelRouterMiddleware(
        choices={
            "fast": ModelChoice(
                model=mock_fast,
                criteria="Direct lookups, extraction, and localized changes.",
            ),
            "powerful": ModelChoice(
                model=mock_powerful,
                criteria="Architecture and high-stakes decisions.",
            ),
        },
        instructions="Choose the least costly model that can complete the task.",
    )

    assert "fast" in router.models
    assert "powerful" in router.models

    mock_choice = MagicMock(choice="fast", confidence=1.0)
    with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_ainvoke:
        mock_ainvoke.return_value = MagicMock(choices={"model_route": mock_choice})

        state = {"messages": [HumanMessage(content="what is my current namespace")]}
        res = await router.abefore_agent(cast(Any, state), cast(Any, None))
        assert res == {"model_route": mock_choice}

        # Verify wrap_model_call routes to mock_fast
        mock_req = MagicMock(spec=ModelRequest)
        mock_req.state = res

        def fake_override(**kwargs):
            r = MagicMock(spec=ModelRequest)
            r.model = kwargs.get("model")
            return r

        mock_req.override.side_effect = fake_override

        routed_target = None

        async def handler(r):
            nonlocal routed_target
            routed_target = r.model
            return MagicMock(spec=ModelResponse)

        await router.awrap_model_call(mock_req, handler)
        assert routed_target is mock_fast


@pytest.mark.asyncio
async def test_middleware_bypasses_manual_and_auto_modes():
    middleware = JevDynamicModelRouterMiddleware()

    # Manual mode context
    manual_runtime = DummyRuntime(context={"approval_mode": "manual", "smart": False})
    state = {"messages": [HumanMessage(content="Hello there!")]}
    result_manual = await middleware.abefore_agent(state, manual_runtime)
    assert result_manual is None

    # Auto mode context
    auto_runtime = DummyRuntime(context={"approval_mode": "auto", "smart": False, "auto_approve": True})
    result_auto = await middleware.abefore_agent(state, auto_runtime)
    assert result_auto is None


@pytest.mark.asyncio
async def test_middleware_activates_in_smart_mode_tier0():
    middleware = JevDynamicModelRouterMiddleware()

    smart_runtime = DummyRuntime(context={"approval_mode": "smart", "smart": True, "model": "google_genai:gemini-3.7-flash"})
    state = {"messages": [HumanMessage(content="hi how are you")]}

    mock_choice = MagicMock(choice="fast", confidence=1.0)
    mock_response = MagicMock(choices={"model_route": mock_choice})

    with patch.object(middleware, "is_available", return_value=True):
        with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = mock_response

            with patch("opscloud.middleware.jev_model_router.adispatch_custom_event", new=AsyncMock()) as mock_event:
                result = await middleware.abefore_agent(state, smart_runtime)

                assert result is not None
                route = result.get("_dynamic_model_route")
                assert route is not None
                assert route["tier"] == 0
                assert route["effort"] == "off"
                assert route["route"] == "fast"
                assert result["model_route"] == mock_choice

                # Verify custom event was dispatched for UI
                mock_event.assert_called_once()
                call_args = mock_event.call_args[0]
                assert call_args[0] == "model_routed"
                assert call_args[1]["effort"] == "off"
                assert call_args[1]["route"] == "fast"


@pytest.mark.asyncio
async def test_middleware_activates_in_smart_mode_tier2():
    middleware = JevDynamicModelRouterMiddleware()

    smart_runtime = DummyRuntime(context={"approval_mode": "smart", "smart": True, "model": "google_genai:gemini-3.7-flash"})
    state = {"messages": [HumanMessage(content="Investigate why pod crashloopbackoff cascades across 4 nodes and root cause it")]}

    mock_choice = MagicMock(choice="powerful", confidence=0.88)
    mock_response = MagicMock(choices={"model_route": mock_choice})

    with patch.object(middleware, "is_available", return_value=True):
        with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = mock_response

            with patch("opscloud.middleware.jev_model_router.adispatch_custom_event", new=AsyncMock()):
                result = await middleware.abefore_agent(state, smart_runtime)

                assert result is not None
                route = result.get("_dynamic_model_route")
                assert route is not None
                assert route["tier"] == 2
                assert route["effort"] == "high"
                assert route["route"] == "powerful"
                assert result["model_route"] == mock_choice


@pytest.mark.asyncio
async def test_awrap_model_call_overrides_model_and_settings_in_smart_mode():
    middleware = JevDynamicModelRouterMiddleware()

    mock_original_model = MagicMock()
    mock_routed_model = MagicMock()

    smart_runtime = DummyRuntime(context={"approval_mode": "smart", "smart": True})
    mock_request = MagicMock(spec=ModelRequest)
    mock_request.runtime = smart_runtime
    mock_request.model = mock_original_model
    mock_request.model_settings = {}
    mock_request.state = {
        "model_route": MagicMock(choice="fast", confidence=1.0),
        "_dynamic_model_route": {
            "tier": 0,
            "spec": "google_genai:gemini-3.6-flash",
            "effort": "off",
            "route": "fast",
        },
    }

    def fake_override(**kwargs):
        new_req = MagicMock(spec=ModelRequest)
        new_req.model = kwargs.get("model", mock_request.model)
        new_req.model_settings = kwargs.get("model_settings", mock_request.model_settings)
        return new_req

    mock_request.override.side_effect = fake_override

    with patch.object(
        middleware.pool,
        "get_model_for_tier",
        return_value=(mock_routed_model, "google_genai:gemini-3.6-flash", "off", {}),
    ):
        handler_called_with = None

        async def fake_handler(req):
            nonlocal handler_called_with
            handler_called_with = req
            return MagicMock(spec=ModelResponse)

        await middleware.awrap_model_call(mock_request, fake_handler)

        assert handler_called_with is not None
        assert handler_called_with.model_settings == {}


@pytest.mark.asyncio
async def test_fallback_on_classifier_failure():
    middleware = JevDynamicModelRouterMiddleware()

    smart_runtime = DummyRuntime(context={"approval_mode": "smart", "smart": True})
    state = {"messages": [HumanMessage(content="test query")]}

    with patch.object(middleware, "is_available", return_value=True):
        with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_inv:
            mock_inv.side_effect = TimeoutError("Jev API timeout")

            result = await middleware.abefore_agent(state, smart_runtime)
            assert result is None


def test_pool_manager_instantiates_tier0_with_effort_off(pool_manager):
    with patch("opscloud.model.config.has_provider_credentials", return_value=True):
        with patch("opscloud.model.factory.has_provider_credentials", return_value=True):
            model, spec, effort, settings = pool_manager.get_model_for_tier(
                tier=0, base_spec="google_genai:gemini-3.7-flash"
            )
            assert effort == "off"
            assert "thinking_budget" not in settings
            assert "include_thoughts" not in settings
            assert "reasoning_effort" not in settings
            assert settings == {}
            assert getattr(model, "reasoning_effort", None) is None


@pytest.mark.asyncio
async def test_middleware_activates_in_smart_mode_tier1_for_ops_execution(caplog):
    middleware = JevDynamicModelRouterMiddleware()

    smart_runtime = DummyRuntime(context={"approval_mode": "smart", "smart": True, "model": "google_genai:gemini-3.7-flash"})
    state = {"messages": [HumanMessage(content="can you list down all the aws s3 bucket use the krayak profile")]}

    mock_choice = MagicMock(choice="standard", confidence=0.99)
    mock_response = MagicMock(choices={"model_route": mock_choice})

    with caplog.at_level(logging.INFO):
        with patch.object(middleware, "is_available", return_value=True):
            with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_inv:
                mock_inv.return_value = mock_response

                with patch("opscloud.middleware.jev_model_router.adispatch_custom_event", new=AsyncMock()):
                    result = await middleware.abefore_agent(state, smart_runtime)

                    assert result is not None
                    route = result.get("_dynamic_model_route")
                    assert route is not None
                    assert route["tier"] == 1
                    assert route["route"] == "standard"
                    assert route["effort"] == "medium"
                    assert "gemini-3.7-flash" in route["spec"]

                    log_text = caplog.text
                    assert "evaluated task 'can you list down all the aws s3 bucket" in log_text
                    assert "selected route=standard (tier=1" in log_text


@pytest.mark.asyncio
async def test_middleware_detects_smart_mode_from_configurable_or_store():
    middleware = JevDynamicModelRouterMiddleware()

    class ConfigRuntime:
        def __init__(self):
            self.context = None
            self.config = {"configurable": {"approval_mode": "smart"}}
            self.store = None

    state = {"messages": [{"role": "user", "content": "aws s3 ls"}]}

    mock_choice = MagicMock(choice="standard", confidence=0.95)
    mock_response = MagicMock(choices={"model_route": mock_choice})

    with patch.object(middleware, "is_available", return_value=True):
        with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = mock_response

            with patch("opscloud.middleware.jev_model_router.adispatch_custom_event", new=AsyncMock()):
                result = await middleware.abefore_agent(state, ConfigRuntime())
                assert result is not None
                assert result["_dynamic_model_route"]["tier"] == 1
                assert result["_dynamic_model_route"]["route"] == "standard"
                assert result["model_route"] == mock_choice


@pytest.mark.asyncio
async def test_message_extraction_helper():
    from opscloud.middleware.jev_model_router import _extract_last_user_text

    assert _extract_last_user_text({"messages": [{"role": "user", "content": "hello"}]}) == "hello"

    state_blocks = {
        "messages": [
            {
                "type": "human",
                "content": [
                    {"type": "text", "text": "can you check"},
                    {"type": "text", "text": "these buckets"},
                ],
            }
        ]
    }
    assert _extract_last_user_text(state_blocks) == "can you check these buckets"


def test_user_configured_agent_pool_in_toml(tmp_path, pool_manager):
    from opscloud.config.toml_config import (
        clear_agent_pool,
        load_agent_pool,
        save_agent_pool,
    )

    tmp_config = tmp_path / "config.toml"

    # Initially empty
    assert load_agent_pool(tmp_config) is None

    # Save custom pool
    custom_pool = {
        "fast": "google_genai:gemini-2.5-flash-lite",
        "standard": "google_genai:gemini-2.5-flash",
        "powerful": "google_genai:gemini-2.5-pro",
    }
    assert save_agent_pool(custom_pool, provider="google_genai", config_path=tmp_config) is True

    loaded = load_agent_pool(tmp_config)
    assert loaded is not None
    assert loaded["fast"] == "google_genai:gemini-2.5-flash-lite"
    assert loaded["standard"] == "google_genai:gemini-2.5-flash"
    assert loaded["powerful"] == "google_genai:gemini-2.5-pro"

    # Pool manager respects user-configured pool
    with patch("opscloud.config.toml_config.load_agent_pool", return_value=loaded):
        tiers = pool_manager.discover_tiers(provider="google_genai")
        assert tiers[0][0] == "google_genai:gemini-2.5-flash-lite"
        assert tiers[1][0] == "google_genai:gemini-2.5-flash"
        assert tiers[2][0] == "google_genai:gemini-2.5-pro"

        # Model choices also reflect user-configured pool in criteria
        choices = pool_manager.get_model_choices(provider="google_genai")
        c_fast = choices["fast"].criteria
        c_standard = choices["standard"].criteria
        c_powerful = choices["powerful"].criteria
        assert isinstance(c_fast, str)
        assert isinstance(c_standard, str)
        assert isinstance(c_powerful, str)
        assert "gemini-2.5-flash-lite" in c_fast
        assert "gemini-2.5-flash" in c_standard
        assert "gemini-2.5-pro" in c_powerful

    # Clear pool
    assert clear_agent_pool(tmp_config) is True
    assert load_agent_pool(tmp_config) is None


@pytest.mark.asyncio
async def test_pool_command_handler(tmp_path):
    from opscloud.commands._base import CommandContext
    from opscloud.commands.core.pool import PoolHandler

    handler = PoolHandler()
    assert handler.name == "/pool"
    assert "/agent-pool" in handler.aliases

    mock_app = MagicMock()
    mock_app._show_pool_selector = AsyncMock()

    # 1. Status command
    ctx_status = CommandContext(app=mock_app, raw_command="/pool status", args="status")
    res_status = await handler.execute(ctx_status)
    assert res_status.success is True
    assert res_status.message is not None and "Agent Model Pool Status" in res_status.message

    # 2. Set command
    with patch("opscloud.commands.core.pool.save_agent_pool") as mock_save:
        ctx_set = CommandContext(
            app=mock_app,
            raw_command="/pool set fast=gemini-3.6-flash standard=gemini-3.7-flash powerful=gemini-3.1-pro-preview",
            args="set fast=gemini-3.6-flash standard=gemini-3.7-flash powerful=gemini-3.1-pro-preview",
        )
        res_set = await handler.execute(ctx_set)
        assert res_set.success is True
        mock_save.assert_called_once()
        saved_dict = mock_save.call_args[0][0]
        assert saved_dict["fast"] == "gemini-3.6-flash"
        assert saved_dict["standard"] == "gemini-3.7-flash"
        assert saved_dict["powerful"] == "gemini-3.1-pro-preview"

    # 3. Clear command
    with patch("opscloud.commands.core.pool.clear_agent_pool") as mock_clear:
        ctx_clear = CommandContext(app=mock_app, raw_command="/pool clear", args="clear")
        res_clear = await handler.execute(ctx_clear)
        assert res_clear.success is True
        mock_clear.assert_called_once()
        assert res_clear.message is not None and "Cleared" in res_clear.message

    # 4. Interactive UI command (no args)
    ctx_ui = CommandContext(app=mock_app, raw_command="/pool", args="")
    res_ui = await handler.execute(ctx_ui)
    assert res_ui.success is True
    mock_app._show_pool_selector.assert_called_once()


# ── Benchmark Test Suite & Safeguard Tests (OPSCLOUD-JEV-09) ───────────────────


@pytest.mark.parametrize(
    ("prompt", "expected_route", "expected_tier", "simulated_choice", "simulated_conf"),
    [
        (
            "can you help me in optimizing my aws cost",
            "powerful",
            2,
            "powerful",
            0.88,
        ),
        (
            "pod payment-service is crashlooping with OOMKilled across 3 nodes",
            "powerful",
            2,
            "powerful",
            0.92,
        ),
        (
            "refactor our terraform vpc module to support dual-region transit gateway peering",
            "powerful",
            2,
            "powerful",
            0.89,
        ),
        (
            "list all running ec2 instances in us-east-1 and filter by tag Environment=prod",
            "standard",
            1,
            "standard",
            0.95,
        ),
        (
            "update Dockerfile to use python:3.12-slim and add non-root user",
            "standard",
            1,
            "standard",
            0.85,
        ),
        (
            "what is your capability?",
            "fast",
            0,
            "fast",
            0.96,
        ),
        (
            "show me lines 20-50 of main.tf",
            "fast",
            0,
            "fast",
            0.94,
        ),
    ],
)
@pytest.mark.asyncio
async def test_operational_benchmark_classification(
    prompt: str,
    expected_route: str,
    expected_tier: int,
    simulated_choice: str,
    simulated_conf: float,
):
    """Verify classification mapping across all 7 operational benchmark test cases (TC-1 to TC-7)."""
    middleware = JevDynamicModelRouterMiddleware()
    runtime = DummyRuntime(context={"approval_mode": "smart", "smart": True, "model": "google_genai:gemini-3.7-flash"})
    state = {"messages": [HumanMessage(content=prompt)]}

    mock_choice = MagicMock(choice=simulated_choice, confidence=simulated_conf)
    mock_response = MagicMock(choices={"model_route": mock_choice})

    with patch.object(middleware, "is_available", return_value=True):
        with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = mock_response
            with patch("opscloud.middleware.jev_model_router.adispatch_custom_event", new=AsyncMock()):
                res = await middleware.abefore_agent(state, runtime)
                assert res is not None
                route_info = res["_dynamic_model_route"]
                assert route_info["route"] == expected_route
                assert route_info["tier"] == expected_tier


@pytest.mark.asyncio
async def test_confidence_safeguard_escalates_incident_prompt():
    """Verify the exact incident prompt escalates from standard (conf=0.40) to powerful on Orchestrator."""
    middleware = JevDynamicModelRouterMiddleware(subagent_name=None)
    runtime = DummyRuntime(context={"approval_mode": "smart", "smart": True})
    # The actual user prompt from the incident
    state = {"messages": [HumanMessage(content="can you help me in optimizing my aws cos")]}

    # Simulate Jev returning standard with near-entropy confidence 0.40
    mock_choice = MagicMock(choice="standard", confidence=0.40)
    mock_response = MagicMock(choices={"model_route": mock_choice})

    with patch.object(middleware, "is_available", return_value=True):
        with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = mock_response
            with patch("opscloud.middleware.jev_model_router.adispatch_custom_event", new=AsyncMock()):
                res = await middleware.abefore_agent(state, runtime)
                assert res is not None
                route_info = res["_dynamic_model_route"]
                # Safeguard must escalate orchestrator from standard -> powerful due to 'optimizing' / 'cos' markers
                assert route_info["route"] == "powerful"
                assert route_info["tier"] == 2


@pytest.mark.asyncio
async def test_confidence_safeguard_for_subagent_escalates_to_standard():
    """Verify low confidence on subagent specialist task escalates to standard (Tier 1), not powerful."""
    middleware = JevDynamicModelRouterMiddleware(subagent_name="aws-finops-agent")
    runtime = DummyRuntime(context={"approval_mode": "smart", "smart": True, "ls_agent_type": "subagent"})
    state = {"messages": [HumanMessage(content="audit unattached ebs volumes")]}

    mock_choice = MagicMock(choice="fast", confidence=0.40)
    mock_response = MagicMock(choices={"model_route": mock_choice})

    with patch.object(middleware, "is_available", return_value=True):
        with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = mock_response
            with patch("opscloud.middleware.jev_model_router.adispatch_custom_event", new=AsyncMock()):
                res = await middleware.abefore_agent(state, runtime)
                assert res is not None
                route_info = res["_dynamic_model_route"]
                # Subagent low confidence promotes to standard
                assert route_info["route"] == "standard"
                assert route_info["tier"] == 1


@pytest.mark.asyncio
async def test_confidence_safeguard_promotes_ambiguous_fast_to_standard():
    """Verify low-confidence fast route without high-complexity markers safely promotes to standard."""
    middleware = JevDynamicModelRouterMiddleware()
    runtime = DummyRuntime(context={"approval_mode": "smart", "smart": True})
    state = {"messages": [HumanMessage(content="run the check routine")]}

    mock_choice = MagicMock(choice="fast", confidence=0.42)
    mock_response = MagicMock(choices={"model_route": mock_choice})

    with patch.object(middleware, "is_available", return_value=True):
        with patch.object(TypeSafeClassifier, "ainvoke", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = mock_response
            with patch("opscloud.middleware.jev_model_router.adispatch_custom_event", new=AsyncMock()):
                res = await middleware.abefore_agent(state, runtime)
                assert res is not None
                route_info = res["_dynamic_model_route"]
                assert route_info["route"] == "standard"
                assert route_info["tier"] == 1


@pytest.mark.asyncio
async def test_enriched_routing_state_orchestrator_vs_subagent():
    """Verify enriched state payload has subagent_ecosystem on orchestrator and None (nil) on subagent."""
    from opscloud.middleware.jev_model_router import _build_jev_routing_state

    user_msg = HumanMessage(content="can you help me in optimizing my aws cost")

    # 1. Main Orchestrator state
    orch_state = _build_jev_routing_state(
        user_msg=user_msg,
        ctx={},
        runtime=None,
        subagent_name=None,
    )
    assert orch_state["is_subagent"] is False
    assert orch_state["agent_role"] == "orchestrator"
    assert orch_state["agent_name"] == "opscloud-supervisor"
    assert isinstance(orch_state["subagent_ecosystem"], list)
    assert "software_and_devops_coding" in orch_state["agent_context"]["active_capabilities"]
    assert "multi_agent_delegation" in orch_state["agent_context"]["active_capabilities"]
    assert "system_synthesis_and_reporting" in orch_state["agent_context"]["active_capabilities"]

    # 2. Subagent state
    sub_state = _build_jev_routing_state(
        user_msg=user_msg,
        ctx={"ls_agent_type": "subagent"},
        runtime=None,
        subagent_name="aws-finops-agent",
    )
    assert sub_state["is_subagent"] is True
    assert sub_state["agent_role"] == "subagent"
    assert sub_state["agent_name"] == "aws-finops-agent"
    # CRITICAL: subagent_ecosystem must be None (nil) for subagents
    assert sub_state["subagent_ecosystem"] is None
    assert sub_state["agent_context"]["available_subagents"] is None
    # Subagent capabilities must NOT include multi_agent_delegation
    assert "multi_agent_delegation" not in sub_state["agent_context"]["active_capabilities"]
    assert "cloud_financial_management" in sub_state["agent_context"]["active_capabilities"]
    assert "cost_and_usage_analysis" in sub_state["agent_context"]["active_capabilities"]


def test_multi_provider_agent_pool_tier_discovery(tmp_path, pool_manager):
    """Verify that multi-provider agent pools resolve correctly across different providers and effort levels."""
    from opscloud.config.toml_config import clear_agent_pool, load_agent_pool, save_agent_pool

    tmp_config = tmp_path / "config.toml"
    multi_pool = {
        "fast": "openai:gpt-4o-mini",
        "standard": "google_genai:gemini-2.5-flash",
        "powerful": "anthropic:claude-sonnet-4-6",
    }
    assert save_agent_pool(multi_pool, provider="multi", config_path=tmp_config) is True

    loaded = load_agent_pool(tmp_config)
    assert loaded is not None
    assert loaded["fast"] == "openai:gpt-4o-mini"
    assert loaded["standard"] == "google_genai:gemini-2.5-flash"
    assert loaded["powerful"] == "anthropic:claude-sonnet-4-6"
    assert loaded["provider"] == "multi"

    with patch("opscloud.config.toml_config.load_agent_pool", return_value=loaded):
        # 1. Resolves multi-provider pool regardless of passed provider
        for prov in ("google_genai", "openai", "anthropic", None):
            tiers = pool_manager.discover_tiers(provider=prov)
            assert tiers[0][0] == "openai:gpt-4o-mini"
            assert tiers[1][0] == "google_genai:gemini-2.5-flash"
            assert tiers[2][0] == "anthropic:claude-sonnet-4-6"
            assert tiers[0][1] == "off"
            assert tiers[1][1] in ("off", "medium")
            assert tiers[2][1] in ("high", "max")

        # 2. Resolves multi-provider pool with base_spec
        tiers_base = pool_manager.discover_tiers(base_spec="google_genai:gemini-2.5-flash")
        assert tiers_base[0][0] == "openai:gpt-4o-mini"
        assert tiers_base[1][0] == "google_genai:gemini-2.5-flash"
        assert tiers_base[2][0] == "anthropic:claude-sonnet-4-6"

        # 3. Model choices criteria reflect distinct providers
        choices = pool_manager.get_model_choices(provider="google_genai")
        assert "gpt-4o-mini" in choices["fast"].criteria
        assert "gemini-2.5-flash" in choices["standard"].criteria
        assert "claude-sonnet-4-6" in choices["powerful"].criteria


def test_pool_manager_validate_pool(pool_manager):
    """Verify validation of pool configurations and credential warning detection."""
    # 1. Valid multi-provider pool
    is_valid, warnings = pool_manager.validate_pool({
        "fast": "openai:gpt-4o-mini",
        "standard": "google_genai:gemini-2.5-flash",
        "powerful": "anthropic:claude-sonnet-4-6",
    })
    assert is_valid is True
    # warnings may be empty or contain missing credential notices depending on env
    assert isinstance(warnings, list)

    # 2. Incomplete pool (missing tier)
    is_valid_incomplete, errs = pool_manager.validate_pool({
        "fast": "openai:gpt-4o-mini",
        "standard": "",
    })
    assert is_valid_incomplete is False
    assert any("standard" in e for e in errs)

    # 3. Unrecognized provider
    is_valid_unknown, warn_unknown = pool_manager.validate_pool({
        "fast": "invalid_provider:model-x",
        "standard": "google_genai:gemini-2.5-flash",
        "powerful": "anthropic:claude-sonnet-4-5",
    })
    assert is_valid_unknown is True
    assert any("invalid_provider" in w for w in warn_unknown)

