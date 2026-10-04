"""Unit tests for CodeModelRetryMiddleware and transient error resilience."""

import asyncio
from unittest.mock import MagicMock
import pytest
from langgraph.errors import GraphBubbleUp

from opscloud.middleware.model_retry import (
    CodeModelRetryMiddleware,
    compute_delay,
    is_transient_error,
    MODEL_RETRY_EVENT_TYPE,
)


class DummyExceptionWithStatus(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class DummyExceptionWithResponse(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.response = MagicMock()
        self.response.status_code = status_code


def test_is_transient_error_status_codes():
    # Transient HTTP status codes
    assert is_transient_error(DummyExceptionWithStatus("Rate limited", 429)) is True
    assert is_transient_error(DummyExceptionWithStatus("Server error", 500)) is True
    assert is_transient_error(DummyExceptionWithStatus("Bad gateway", 502)) is True
    assert is_transient_error(DummyExceptionWithStatus("Unavailable", 503)) is True
    assert is_transient_error(DummyExceptionWithStatus("Gateway timeout", 504)) is True
    assert is_transient_error(DummyExceptionWithStatus("Request timeout", 408)) is True
    assert is_transient_error(DummyExceptionWithStatus("Conflict", 409)) is True

    # Check via response object
    assert is_transient_error(DummyExceptionWithResponse("Too many requests", 429)) is True

    # Non-transient status codes
    assert is_transient_error(DummyExceptionWithStatus("Bad request", 400)) is False
    assert is_transient_error(DummyExceptionWithStatus("Unauthorized", 401)) is False
    assert is_transient_error(DummyExceptionWithStatus("Forbidden", 403)) is False
    assert is_transient_error(DummyExceptionWithStatus("Not found", 404)) is False


def test_is_transient_error_control_flow_and_signals():
    # Never retry control flow or termination exceptions
    assert is_transient_error(KeyboardInterrupt()) is False
    assert is_transient_error(SystemExit()) is False
    assert is_transient_error(GraphBubbleUp()) is False

    # String signal detection
    assert is_transient_error(Exception("Connection reset by peer")) is True
    assert is_transient_error(Exception("Server overloaded, try again later")) is True
    assert is_transient_error(Exception("API rate limit exceeded")) is True
    assert is_transient_error(Exception("Request timed out waiting for response")) is True
    assert is_transient_error(Exception("Throttling exception from AWS STS")) is True

    # Plain non-transient error
    assert is_transient_error(ValueError("Invalid syntax in argument")) is False


def test_compute_delay():
    # Delay should increase with attempts and stay within bounds
    d1 = compute_delay(1)
    d2 = compute_delay(2)
    d3 = compute_delay(3)

    assert d1 >= 0.1
    assert d2 >= d1 * 0.5  # accounting for jitter
    assert d3 <= 12.0  # max delay capped at 10.0 + jitter


def test_model_retry_middleware_success():
    mw = CodeModelRetryMiddleware(max_retries=3)
    request = MagicMock()
    handler = MagicMock(return_value="success_response")

    res = mw.wrap_model_call(request, handler)
    assert res == "success_response"
    assert handler.call_count == 1


def test_model_retry_middleware_retries_and_recovers(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)

    mw = CodeModelRetryMiddleware(max_retries=3)
    emitted_events = []

    mock_runtime = MagicMock()
    mock_runtime.stream_writer = lambda event: emitted_events.append(event)
    request = MagicMock()
    request.runtime = mock_runtime

    calls = 0

    def failing_then_succeeding_handler(req):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise DummyExceptionWithStatus("Rate limited", 429)
        return "recovered"

    res = mw.wrap_model_call(request, failing_then_succeeding_handler)
    assert res == "recovered"
    assert calls == 3
    assert len(emitted_events) == 2
    assert emitted_events[0]["type"] == MODEL_RETRY_EVENT_TYPE
    assert emitted_events[0]["attempt"] == 1
    assert emitted_events[1]["attempt"] == 2


def test_model_retry_middleware_exhausts_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)

    mw = CodeModelRetryMiddleware(max_retries=2)
    request = MagicMock()
    request.runtime = None

    handler = MagicMock(side_effect=DummyExceptionWithStatus("Rate limited", 429))

    with pytest.raises(DummyExceptionWithStatus):
        mw.wrap_model_call(request, handler)

    # 1 initial + 2 retries = 3 calls total
    assert handler.call_count == 3


def test_model_retry_middleware_non_transient_no_retry():
    mw = CodeModelRetryMiddleware(max_retries=3)
    request = MagicMock()
    handler = MagicMock(side_effect=ValueError("Non transient parameter error"))

    with pytest.raises(ValueError):
        mw.wrap_model_call(request, handler)

    assert handler.call_count == 1


@pytest.mark.asyncio
async def test_async_model_retry_middleware(monkeypatch):
    async def dummy_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", dummy_sleep)

    mw = CodeModelRetryMiddleware(max_retries=2)
    request = MagicMock()
    request.runtime = None

    calls = 0

    async def async_handler(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DummyExceptionWithStatus("Service unavailable", 503)
        return "async_success"

    res = await mw.awrap_model_call(request, async_handler)
    assert res == "async_success"
    assert calls == 2
