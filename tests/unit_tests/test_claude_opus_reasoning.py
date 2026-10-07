"""Unit tests for Claude Opus 5 adaptive thinking and multi-level reasoning effort.

Validates that:
1. `with_effort_model_params` for Anthropic does not inject fixed-budget `thinking={"type": "enabled", ...}`.
2. `create_model` on `anthropic:claude-opus-5` with `max` effort succeeds and request payloads produce
   native `output_config.effort` and adaptive thinking without ValueError.
3. ChatAnthropic natively handles adaptive thinking without unsupported beta headers or block_binding.
4. All supported effort levels (low, medium, high, xhigh, max) and clearing (off, clear) work properly.
5. Legacy Claude models (claude-3-7-sonnet) and non-reasoning models (claude-3-5-sonnet) remain functional.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from opscloud.model.factory import clear_model_cache, create_model
from opscloud.model.pool import DynamicModelPoolManager
from opscloud.model.reasoning import (
    current_effort_from_model_params,
    default_effort_for_model,
    has_explicit_effort_model_params,
    is_effort_supported_for_model,
    supported_efforts_for_model,
    with_effort_model_params,
    without_effort_model_params,
)


def _get_payload(model: Any, prompt: str = "test prompt") -> dict[str, Any]:
    """Safely obtain request payload from model without static type errors on BaseChatModel."""
    payload_func = getattr(model, "_get_request_payload", None)
    if callable(payload_func):
        return cast(dict[str, Any], payload_func(prompt))
    return {}


def test_claude_opus_5_effort_levels():
    """Verify profile advertises correct reasoning levels for claude-opus-5."""
    levels = supported_efforts_for_model("anthropic:claude-opus-5")
    assert levels == ("low", "medium", "high", "xhigh", "max")
    assert is_effort_supported_for_model("anthropic:claude-opus-5", "max") is True
    assert is_effort_supported_for_model("anthropic:claude-opus-5", "xhigh") is True
    assert default_effort_for_model("anthropic:claude-opus-5") == "high"


def test_with_effort_model_params_anthropic_no_fixed_budget():
    """Verify with_effort_model_params sets reasoning_effort without injecting budget_tokens."""
    params = with_effort_model_params("anthropic:claude-opus-5", {}, "max")
    assert params == {"reasoning_effort": "max"}
    assert "thinking" not in params

    # Legacy thinking in existing params is cleanly stripped
    legacy = {"thinking": {"type": "enabled", "budget_tokens": 16384}}
    cleaned_params = with_effort_model_params("anthropic:claude-opus-5", legacy, "max")
    assert cleaned_params == {"reasoning_effort": "max"}
    assert "thinking" not in cleaned_params


def test_without_effort_model_params_anthropic():
    """Verify without_effort_model_params strips reasoning_effort, effort, output_config.effort, and legacy thinking."""
    existing = {
        "reasoning_effort": "max",
        "effort": "max",
        "output_config": {"effort": "max", "other_key": "val"},
        "thinking": {"type": "enabled", "budget_tokens": 8192},
        "temperature": 0.0,
    }
    cleaned = without_effort_model_params("anthropic:claude-opus-5", existing)
    assert cleaned is not None
    assert "reasoning_effort" not in cleaned
    assert "effort" not in cleaned
    assert "effort" not in cleaned.get("output_config", {})
    assert cleaned.get("output_config", {}).get("other_key") == "val"
    assert "thinking" not in cleaned
    assert cleaned.get("temperature") == 0.0


def test_create_model_claude_opus_5_max_effort():
    """Verify create_model with claude-opus-5 and max effort constructs valid ChatAnthropic payload."""
    clear_model_cache()
    res = create_model("anthropic:claude-opus-5", extra_kwargs={"reasoning_effort": "max"})
    assert res.model is not None

    # Call payload generation to ensure no ValueError: `thinking={"type": "enabled", ...}` is raised
    payload = _get_payload(res.model, "can you help me in reducing the aws costs")
    assert payload["model"] == "claude-opus-5"
    assert payload.get("output_config", {}).get("effort") == "max"

    # Adaptive thinking must be present without invalid token budgets
    thinking = payload.get("thinking")
    assert isinstance(thinking, dict)
    assert thinking.get("type") == "adaptive"
    assert "budget_tokens" not in thinking


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_create_model_claude_opus_5_all_efforts(effort: str):
    """Verify all supported effort levels succeed on claude-opus-5."""
    clear_model_cache()
    res = create_model("anthropic:claude-opus-5", extra_kwargs={"reasoning_effort": effort})
    payload = _get_payload(res.model, "test prompt")
    assert payload.get("output_config", {}).get("effort") == effort
    assert payload.get("thinking", {}).get("type") == "adaptive"


def test_create_model_claude_opus_5_off_effort():
    """Verify effort off disables reasoning effort on claude-opus-5."""
    clear_model_cache()
    res = create_model("anthropic:claude-opus-5", extra_kwargs={"reasoning_effort": "off"})
    payload = _get_payload(res.model, "test prompt")
    assert payload.get("output_config") is None or "effort" not in payload.get("output_config", {})


def test_claude_3_7_sonnet_reasoning_effort():
    """Verify claude-3-7-sonnet works with reasoning_effort and does not error."""
    clear_model_cache()
    res = create_model("anthropic:claude-3-7-sonnet-20250219", extra_kwargs={"reasoning_effort": "high"})
    payload = _get_payload(res.model, "test prompt")
    assert payload.get("output_config", {}).get("effort") == "high"


def test_dynamic_pool_tier_2_anthropic():
    """Verify DynamicModelPoolManager provides tier 2 Claude Opus 5 without thinking payload error."""
    clear_model_cache()
    pool = DynamicModelPoolManager()
    chat_model, spec, eff, native_settings = pool.get_model_for_tier(
        2,
        provider="anthropic",
        base_spec="anthropic:claude-opus-5",
    )
    assert spec.startswith("anthropic:claude-opus-5")
    assert eff == "max"
    assert native_settings == {"reasoning_effort": "max"}

    payload = _get_payload(chat_model, "test message")
    assert payload.get("output_config", {}).get("effort") == "max"
    assert payload.get("thinking", {}).get("type") == "adaptive"


def test_switch_from_opus_effort_to_openai(monkeypatch):
    """Verify switching from an Opus max effort session to OpenAI models never fails or leaks invalid parameters."""
    clear_model_cache()
    # 1. Non-reasoning OpenAI model (GPT-4o) should not receive reasoning_effort or thinking
    res_4o = create_model("openai:gpt-4o", extra_kwargs={"reasoning_effort": "max"})
    assert res_4o.model is not None
    p_4o = _get_payload(res_4o.model, "hello")
    assert "reasoning_effort" not in p_4o
    assert "thinking" not in p_4o
    assert "output_config" not in p_4o

    # 2. Reasoning OpenAI model (o3-mini) receives native reasoning.effort mapping
    clear_model_cache()
    res_o3 = create_model("openai:o3-mini", extra_kwargs={"reasoning_effort": "high"})
    assert res_o3.model is not None
    p_o3 = _get_payload(res_o3.model, "hello")
    assert p_o3.get("reasoning", {}).get("effort") == "high"
    assert "thinking" not in p_o3


def test_switch_from_opus_effort_to_grok(monkeypatch):
    """Verify switching from an Opus max effort session to Grok (xAI) does not leak parameters."""
    monkeypatch.setenv("XAI_API_KEY", "dummy-xai-key")
    clear_model_cache()
    res = create_model("xai:grok-2", extra_kwargs={"reasoning_effort": "max"})
    assert res.model is not None
    p = _get_payload(res.model, "hello")
    assert p["model"] == "grok-2"
    assert "reasoning_effort" not in p
    assert "thinking" not in p
    assert "output_config" not in p


def test_switch_from_opus_effort_to_chinese_models(monkeypatch):
    """Verify switching from an Opus max effort session to Chinese models (DeepSeek, Fireworks) succeeds."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-deepseek-key")
    monkeypatch.setenv("FIREWORKS_API_KEY", "dummy-fireworks-key")

    # DeepSeek
    clear_model_cache()
    res_ds = create_model("deepseek:deepseek-chat", extra_kwargs={"reasoning_effort": "max"})
    assert res_ds.model is not None
    p_ds = _get_payload(res_ds.model, "hello")
    assert p_ds["model"] == "deepseek-chat"
    assert "reasoning_effort" not in p_ds
    assert "thinking" not in p_ds

    # Fireworks (Qwen / Kimi)
    clear_model_cache()
    res_fw = create_model("fireworks:accounts/fireworks/models/qwen2p5-coder-32b-instruct", extra_kwargs={"reasoning_effort": "max"})
    assert res_fw.model is not None
    assert getattr(res_fw.model, "model_kwargs", {}) == {}

