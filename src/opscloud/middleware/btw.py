"""``BtwSideQuestionMiddleware`` — `/btw` side-question isolation middleware.

Isolates `/btw` ephemeral side questions from the main agent loop:

1. Wraps the user's side question with ``BTW_PROMPT_PREFIX``.
2. Overrides ``request.tools = []`` in ``wrap_model_call`` / ``awrap_model_call``
   so the LLM has zero bound tools for the side-question turn.
3. Short-circuits in ``after_model`` / ``aafter_model`` with ``jump_to: "end"``
   so the side question completes in a single turn with no follow-up turns,
   rubric grading, or continuation loops.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    hook_config,
)

from opscloud.middleware.registry import register_middleware
from opscloud.state.btw_channels import BtwChannels
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

logger = get_logger(__name__)

# System instruction prefix for `/btw` side questions.
BTW_PROMPT_PREFIX: str = (
    "This is a side question from the user. You MUST answer directly in a single response. "
    "Your tools are disabled. You cannot read files, write files, run commands, search, or take any actions. "
    "You must complete your response in a single turn with no follow-up turns. "
    "Only use information already in the conversation context or your own knowledge."
    'Never promise to take action or say "let me check...".'
    "Here is the question: "
)

# Default side-question timeout in seconds.
BTW_TIMEOUT_SECONDS: float = 60.0


def format_btw_prompt(question: str) -> str:
    """Wrap *question* with the `/btw` side-question prompt prefix."""
    cleaned = question.strip()
    if cleaned.startswith(BTW_PROMPT_PREFIX):
        return cleaned
    return f"{BTW_PROMPT_PREFIX}{cleaned}"


def is_ephemeral_message(message: object) -> bool:
    """Return True if *message* is an ephemeral `/btw` message that should be excluded from compaction."""
    if isinstance(message, Mapping):
        kwargs = message.get("additional_kwargs")
        if isinstance(kwargs, Mapping) and kwargs.get("ephemeral") is True:
            return True
        content = message.get("content")
    else:
        kwargs = getattr(message, "additional_kwargs", None)
        if isinstance(kwargs, Mapping) and kwargs.get("ephemeral") is True:
            return True
        content = getattr(message, "content", None)

    if isinstance(content, str):
        if content.startswith(BTW_PROMPT_PREFIX) or content.startswith("[ASIDE — ephemeral side question"):
            return True
    return False


def is_btw_turn(state: Any, runtime: Any = None) -> bool:
    """Determine whether the current graph invocation is an isolated `/btw` side-question turn."""
    if isinstance(state, Mapping) and state.get("_btw_side_question"):
        return True
    if runtime is not None:
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, Mapping) and ctx.get("_btw_side_question"):
            return True
        if getattr(ctx, "_btw_side_question", False):
            return True
        cfg = getattr(runtime, "config", None)
        if isinstance(cfg, Mapping):
            configurable = cfg.get("configurable")
            if isinstance(configurable, Mapping) and configurable.get("_btw_side_question"):
                return True
    if isinstance(state, Mapping):
        messages = state.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if is_ephemeral_message(last):
                return True
    return False


_is_btw_turn = is_btw_turn


@register_middleware(name="btw_side_question")
class BtwSideQuestionMiddleware(AgentMiddleware[BtwChannels, Any]):
    """Enforce tool-free, single-turn execution for `/btw` side questions."""

    state_schema = BtwChannels

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        """Strip all bound tools when answering a `/btw` side question."""
        if is_btw_turn(request.state, getattr(request, "runtime", None)):
            request = request.override(tools=[])
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        """Asynchronously strip all bound tools when answering a `/btw` side question."""
        if is_btw_turn(request.state, getattr(request, "runtime", None)):
            request = request.override(tools=[])
        return await handler(request)

    @hook_config(can_jump_to=["end"])
    def after_model(
        self,
        state: BtwChannels,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        """Terminate immediately after the single model response on a `/btw` turn."""
        if is_btw_turn(state, runtime):
            return {"_btw_side_question": True, "jump_to": "end"}
        return None

    @hook_config(can_jump_to=["end"])
    async def aafter_model(
        self,
        state: BtwChannels,
        runtime: Runtime[Any],
    ) -> dict[str, Any] | None:
        """Asynchronously terminate immediately after the single model response on a `/btw` turn."""
        if is_btw_turn(state, runtime):
            return {"_btw_side_question": True, "jump_to": "end"}
        return None


__all__ = [
    "BTW_PROMPT_PREFIX",
    "BTW_TIMEOUT_SECONDS",
    "BtwSideQuestionMiddleware",
    "format_btw_prompt",
    "is_btw_turn",
    "is_ephemeral_message",
]
