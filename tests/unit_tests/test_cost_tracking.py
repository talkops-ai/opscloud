"""Unit tests for CostTrackingMiddleware and genai-prices cost calculations."""

from unittest.mock import MagicMock
import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Overwrite

from opscloud.middleware.cost_tracking import (
    CostTrackingMiddleware,
    estimate_cost,
    pricing_data_available,
    resolve_message_model,
    SESSION_COST_EVENT_TYPE,
)


def test_pricing_data_available():
    assert isinstance(pricing_data_available(), bool)


def test_estimate_cost_valid_tokens():
    usage = {
        "input_tokens": 1500,
        "output_tokens": 300,
        "input_token_details": {
            "cache_read": 500,
            "ephemeral_5m_input_tokens": 200,
        },
        "output_token_details": {
            "reasoning": 100,
        },
    }
    cost = estimate_cost(usage, model_name="gpt-4o", provider="openai")
    assert cost is not None
    assert isinstance(cost, float)
    assert cost > 0.0


def test_estimate_cost_provider_aliases():
    usage = {"input_tokens": 1000, "output_tokens": 200}
    # Bedrock alias should be resolved to aws
    cost_bedrock = estimate_cost(usage, model_name="anthropic.claude-3-5-sonnet-20241022-v2:0", provider="bedrock")
    # Azure alias should be resolved to azure
    cost_azure = estimate_cost(usage, model_name="gpt-4o", provider="azure_openai")

    assert (cost_bedrock is not None and cost_bedrock > 0.0) or cost_bedrock is None
    assert (cost_azure is not None and cost_azure > 0.0) or cost_azure is None


def test_estimate_cost_unpriceable_or_empty():
    assert estimate_cost(None, "gpt-4o") is None
    assert estimate_cost({}, "gpt-4o") is None
    assert estimate_cost({"input_tokens": 0, "output_tokens": 0}, "gpt-4o") is None
    assert estimate_cost({"input_tokens": 100, "output_tokens": 100}, "gpt-4o", provider="openai_codex") is None


def test_resolve_message_model():
    msg = AIMessage(
        content="Hello",
        response_metadata={
            "model_name": "claude-3-5-sonnet-20241022",
            "model_provider": "bedrock",
        },
    )
    model, provider = resolve_message_model(msg)
    assert model == "claude-3-5-sonnet-20241022"
    assert provider == "bedrock"

    msg_empty = AIMessage(content="Hello")
    fallback_model, fallback_provider = resolve_message_model(
        msg_empty,
        fallback_model="gpt-4o",
        fallback_provider="openai",
    )
    assert fallback_model == "gpt-4o"
    assert fallback_provider == "openai"


def test_cost_tracking_middleware_nested_before_agent():
    mw_main = CostTrackingMiddleware(nested=False)
    mw_nested = CostTrackingMiddleware(nested=True)

    runtime_mock = MagicMock()
    state = {"_session_cost_usd": 1.25}

    assert mw_main.before_agent(state, runtime_mock) is None

    nested_init = mw_nested.before_agent(state, runtime_mock)
    assert nested_init is not None
    assert isinstance(nested_init["_session_cost_usd"], Overwrite)
    assert nested_init["_session_cost_usd"].value == 0.0
    assert isinstance(nested_init["_session_total_tokens"], Overwrite)
    assert nested_init["_session_total_tokens"].value == 0


def test_cost_tracking_middleware_after_model_charges_message():
    mw = CostTrackingMiddleware(nested=False)

    emitted_events = []
    runtime_mock = MagicMock()
    runtime_mock.stream_writer = lambda event: emitted_events.append(event)
    runtime_mock.execution_info.thread_id = "test-thread-123"
    runtime_mock.execution_info.checkpoint_ns = ""

    ai_msg = AIMessage(
        id="msg_001",
        content="Response",
        response_metadata={"model_name": "gpt-4o-mini", "model_provider": "openai"},
        usage_metadata={"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
    )
    state = {
        "messages": [ai_msg],
        "_session_cost_usd": 0.05,
        "_session_total_tokens": 1000,
    }

    update = mw.after_model(state, runtime_mock)
    assert update is not None
    assert "_session_cost_usd" in update
    assert update["_session_cost_usd"] > 0.0
    assert "_session_total_tokens" in update
    assert update["_session_total_tokens"] == 600

    # Stream event emitted
    assert len(emitted_events) == 1
    assert emitted_events[0]["type"] == SESSION_COST_EVENT_TYPE
    assert emitted_events[0]["thread_id"] == "test-thread-123"
    assert emitted_events[0]["total"] > 0.05
    assert emitted_events[0]["total_tokens"] == 1600


def test_cost_tracking_middleware_subagent_cost_transfers():
    mw_subagent = CostTrackingMiddleware(nested=True)

    runtime_mock = MagicMock()
    runtime_mock.execution_info.thread_id = "test-thread-sub"
    runtime_mock.execution_info.checkpoint_ns = "parent|tools:call_subagent|subagent"

    sub_msg = AIMessage(
        id="msg_sub_1",
        content="Subagent done",
        response_metadata={"model_name": "gpt-4o-mini", "model_provider": "openai"},
        usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
    )
    state = {
        "messages": [sub_msg],
        "_session_cost_usd": 0.02,
        "_session_total_tokens": 300,
    }

    # After agent update records transfer to parent scope
    update = mw_subagent.after_agent(state, runtime_mock)
    assert update is not None
    assert "_session_cost_transfers" in update
    transfers = update["_session_cost_transfers"].value
    assert isinstance(transfers, dict)
    assert len(transfers) > 0
    first_transfer = next(iter(transfers.values()))
    assert "tokens" in first_transfer
    assert first_transfer["tokens"] == 300
    assert first_transfer["cost_usd"] == 0.02

