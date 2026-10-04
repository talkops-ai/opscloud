"""Model-node retry middleware for OpsCloud terminal & TUI execution.

Wraps model invocation with exponential backoff and jitter for transient errors:
- Rate limits (HTTP 429, ResourceExhausted)
- Timeouts (HTTP 408, 504, APITimeoutError)
- Connection drops and resets (Botocore ConnectionClosedError, Anthropic APIConnectionError, OpenAI APIConnectionError)
- Server faults (HTTP 500, 502, 503)

Emits real-time stream events so the terminal status bar updates with countdown/attempt details.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import math
import random
import time
from typing import TYPE_CHECKING, Any
import uuid

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langgraph.errors import GraphBubbleUp

from opscloud.middleware.registry import register_middleware
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_MODEL_RETRIES = 3
MODEL_RETRY_EVENT_TYPE = "model_retry"

_INITIAL_DELAY_SECONDS = 0.5
_BACKOFF_FACTOR = 2.0
_MAX_DELAY_SECONDS = 10.0
_JITTER_FRACTION = 0.15
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})

_TRANSIENT_SDK_EXC_NAMES = frozenset(
    {
        ("anthropic", "APIConnectionError"),
        ("anthropic", "APIConnectionTimeoutError"),
        ("anthropic", "APITimeoutError"),
        ("botocore", "ConnectionClosedError"),
        ("botocore", "ConnectTimeoutError"),
        ("botocore", "EndpointConnectionError"),
        ("botocore", "ReadTimeoutError"),
        ("google", "Aborted"),
        ("google", "DeadlineExceeded"),
        ("google", "ResourceExhausted"),
        ("google", "ServiceUnavailable"),
        ("openai", "APIConnectionError"),
        ("openai", "APITimeoutError"),
        ("urllib3", "ConnectTimeoutError"),
        ("urllib3", "ReadTimeoutError"),
        ("httpx", "ConnectTimeout"),
        ("httpx", "ReadTimeout"),
        ("httpx", "NetworkError"),
    }
)


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status", "http_status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    # Check response object
    resp = getattr(exc, "response", None)
    if resp is not None:
        val = getattr(resp, "status_code", None)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    return None


def is_transient_error(exc: Exception) -> bool:
    """Return whether the exception represents a transient, retryable failure."""
    if isinstance(exc, (KeyboardInterrupt, SystemExit, GraphBubbleUp)):
        return False

    code = _extract_status_code(exc)
    if code is not None and code in _RETRYABLE_STATUS_CODES:
        return True

    # Check known exception class names
    exc_cls = type(exc)
    exc_name = exc_cls.__name__
    module_root = exc_cls.__module__.split(".")[0].lower()

    for pkg, name in _TRANSIENT_SDK_EXC_NAMES:
        if (pkg == module_root or pkg in exc_cls.__module__.lower()) and exc_name == name:
            return True

    # Common string signals
    exc_str = str(exc).lower()
    transient_signals = (
        "rate limit",
        "too many requests",
        "overloaded",
        "connection reset",
        "timeout",
        "timed out",
        "time out",
        "service unavailable",
        "throttling",
    )
    return any(sig in exc_str for sig in transient_signals)


def compute_delay(attempt: int) -> float:
    """Compute exponential backoff delay with jitter."""
    base = _INITIAL_DELAY_SECONDS * (_BACKOFF_FACTOR ** (attempt - 1))
    bounded = min(base, _MAX_DELAY_SECONDS)
    jitter = bounded * _JITTER_FRACTION * (random.random() * 2 - 1)
    return max(0.1, bounded + jitter)


@register_middleware(name="model_retry")
class CodeModelRetryMiddleware(AgentMiddleware):
    """Retries transient LLM failures with exponential backoff and telemetry events."""

    def __init__(self, max_retries: int = DEFAULT_MODEL_RETRIES) -> None:
        super().__init__()
        self.max_retries = max_retries

    def _emit_retry_event(
        self,
        request: ModelRequest,
        attempt: int,
        delay: float,
        exc: Exception,
    ) -> None:
        state = getattr(request, "state", {}) or {}
        runtime = getattr(request, "runtime", None)
        writer = getattr(runtime, "stream_writer", None) if runtime else None
        code = _extract_status_code(exc)
        status_msg = (
            f"Transient error ({type(exc).__name__}"
            f"{f' {code}' if code else ''}); retrying attempt {attempt}/{self.max_retries} "
            f"in {delay:.1f}s..."
        )
        logger.warning(status_msg)
        if callable(writer):
            try:
                writer(
                    {
                        "type": MODEL_RETRY_EVENT_TYPE,
                        "attempt": attempt,
                        "max_retries": self.max_retries,
                        "delay": delay,
                        "message": status_msg,
                        "error": str(exc),
                    }
                )
            except Exception:
                pass

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        attempt = 0
        while True:
            try:
                return handler(request)
            except Exception as exc:
                if attempt >= self.max_retries or not is_transient_error(exc):
                    raise
                attempt += 1
                delay = compute_delay(attempt)
                self._emit_retry_event(request, attempt, delay, exc)
                time.sleep(delay)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        attempt = 0
        while True:
            try:
                return await handler(request)
            except Exception as exc:
                if attempt >= self.max_retries or not is_transient_error(exc):
                    raise
                attempt += 1
                delay = compute_delay(attempt)
                self._emit_retry_event(request, attempt, delay, exc)
                await asyncio.sleep(delay)


__all__ = [
    "DEFAULT_MODEL_RETRIES",
    "MODEL_RETRY_EVENT_TYPE",
    "CodeModelRetryMiddleware",
    "compute_delay",
    "is_transient_error",
]
