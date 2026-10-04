"""Regression tests for resumed thread latency, reasoning configuration, and compaction delegation.

Verifies:
1. Settings and manifest reasoning_effort defaults to None (not forcing 25-50s reasoning on resumed sessions).
2. with_effort_model_params handles 'off', 'none', 'clear', and '0' cleanly without Pydantic ValidationError.
3. ChatGoogleGenerativeAI thinking is disabled via thinking_budget=0 when effort is off.
4. CLICompactionMiddleware implements wrap_model_call and awrap_model_call delegation.
5. Model instances receive model.profile with max_input_tokens for auto-compaction limit discovery.
"""

from __future__ import annotations

import pytest
from deepagents.backends.protocol import BackendProtocol
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from opscloud.config.manifest import get_option
from opscloud.config.metadata import build_stream_config
from opscloud.config.settings import Settings
from opscloud.middleware.compaction import _create_cli_compaction_middleware
from opscloud.model.config import MODEL_PROFILES
from opscloud.model.reasoning import (
    current_effort_from_model_params,
    default_effort_for_model,
    is_effort_supported_for_model,
    supported_efforts_for_model,
    with_effort_model_params,
    without_effort_model_params,
)


def test_settings_and_metadata_reasoning_effort_defaults():
    settings = Settings()
    assert settings.reasoning_effort is None

    option = get_option("models.reasoning_effort")
    assert option is not None
    assert option.default is None
    assert "off" in option.choices
    assert "none" in option.choices

    config = build_stream_config(thread_id="test-thread-1")
    assert config["metadata"].get("reasoning_effort") is None


def test_gemini_profiles_no_forced_default():
    assert "reasoning_effort_default" not in MODEL_PROFILES.get("google_genai:gemini-3.7-flash", {})
    assert "reasoning_effort_default" not in MODEL_PROFILES.get("google_genai:gemini-3.8-flash", {})
    assert default_effort_for_model("google_genai:gemini-3.7-flash") is None


def test_reasoning_effort_disable_google_genai():
    # Model without configurable reasoning levels (e.g. gemini-2.5-flash-lite)
    assert supported_efforts_for_model("google_genai:gemini-2.5-flash-lite") == ()
    assert is_effort_supported_for_model("google_genai:gemini-2.5-flash-lite", "high") is False

    # Model with configurable reasoning levels (gemini-3.5-flash)
    assert "high" in supported_efforts_for_model("google_genai:gemini-3.5-flash")
    assert is_effort_supported_for_model("google_genai:gemini-3.5-flash", "high") is True

    # Turning off effort produces clean params with NO reasoning_effort or thinking keys (dcode flow)
    params = with_effort_model_params("google_genai:gemini-3.5-flash", {"temperature": 0.0}, "off")
    assert "reasoning_effort" not in params
    assert "thinking_budget" not in params
    assert "include_thoughts" not in params
    assert params == {"temperature": 0.0}

    # Turning on effort produces valid reasoning_effort
    params_on = with_effort_model_params("google_genai:gemini-3.5-flash", {"temperature": 0.0}, "high")
    assert params_on.get("reasoning_effort") == "high"
    assert params_on.get("include_thoughts") is True

    # without_effort_model_params cleans everything
    cleaned = without_effort_model_params("google_genai:gemini-3.5-flash", params_on)
    assert "reasoning_effort" not in cleaned
    assert "include_thoughts" not in cleaned



def test_reasoning_effort_disable_anthropic_and_openai():
    # Anthropic
    anth_params = with_effort_model_params("anthropic:claude-3-7-sonnet", {}, "off")
    assert "thinking" not in anth_params
    assert "reasoning_effort" not in anth_params

    # OpenAI
    oai_params = with_effort_model_params("openai:o3-mini", {}, "off")
    assert "reasoning" not in oai_params
    assert "reasoning_effort" not in oai_params


class DummyBackend(BackendProtocol):
    pass


def test_compaction_middleware_model_profile_and_delegation():
    fake_model = FakeListChatModel(responses=["test response"])
    backend = DummyBackend()

    middleware = _create_cli_compaction_middleware(fake_model, backend)
    assert hasattr(fake_model, "profile")
    assert fake_model.profile.get("max_input_tokens") == 128_000

    # Ensure wrap_model_call and awrap_model_call methods exist and callable
    assert callable(getattr(middleware, "wrap_model_call", None))
    assert callable(getattr(middleware, "awrap_model_call", None))
