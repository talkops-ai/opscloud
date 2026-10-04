"""Middleware to collapse list-based system message content blocks into a single string."""

from typing import Any, Awaitable, Callable

from opscloud.utils.logger import get_logger
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage

from opscloud.middleware.registry import register_middleware

logger = get_logger(__name__)


def unify_system_message(system_message: SystemMessage | None) -> SystemMessage | None:
    if system_message is None:
        return None
    content = getattr(system_message, "content", None)
    if isinstance(content, str):
        return system_message

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text", "")
                if txt:
                    parts.append(txt)
            elif isinstance(block, str) and block:
                parts.append(block)
        unified_text = "".join(parts).strip()
        return SystemMessage(content=unified_text)
    return system_message


@register_middleware(name="unified_system_message")
class UnifiedSystemMessageMiddleware(AgentMiddleware[Any, Any]):
    """Middleware that collapses SystemMessage content blocks into a single string."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        unified_msg = unify_system_message(request.system_message)
        if unified_msg is not None and unified_msg is not request.system_message:
            request = request.override(system_message=unified_msg)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        unified_msg = unify_system_message(request.system_message)
        if unified_msg is not None and unified_msg is not request.system_message:
            request = request.override(system_message=unified_msg)
        return await handler(request)
