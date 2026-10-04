"""GLM terminal stall recovery middleware for handling model stalls in headless turns."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, SystemMessage

from opscloud.middleware.registry import register_middleware
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_TERMINAL_STALL_RECOVERY_SUFFIX = """\
<terminal_stall_recovery>
Your prior attempt exhausted its output budget without taking an action. Stop \
explaining or planning and call a tool now to create or update the requested \
deliverable. Prefer the smallest valid artifact, then run one discriminating check. \
Keep any reasoning brief enough to reach the tool call.
</terminal_stall_recovery>"""


def _is_fireworks_glm_5p2_model(model: object) -> bool:
    name = getattr(model, "model_name", None) or getattr(model, "model", None) or getattr(model, "name", "") or ""
    return "glm-5.2" in str(name).lower() or "glm-5p2" in str(name).lower()


@register_middleware(name="glm_stall_recovery")
class GlmTerminalStallRecoveryMiddleware(AgentMiddleware[Any, Any]):
    """Recover a headless GLM-5.2 turn that hit the output cap."""

    @staticmethod
    def _is_terminal_stall(response: ModelResponse[Any]) -> bool:
        if response.structured_response is not None or len(response.result) != 1:
            return False
        message = response.result[0]
        if not isinstance(message, AIMessage) or message.tool_calls:
            return False
        metadata = message.response_metadata
        return isinstance(metadata, dict) and metadata.get("finish_reason") == "length"

    @staticmethod
    def _recovery_request(request: ModelRequest[Any]) -> ModelRequest[Any]:
        system_msg = request.system_message
        if system_msg:
            try:
                from deepagents.middleware._utils import append_to_system_message

                new_system_msg = append_to_system_message(system_msg, _TERMINAL_STALL_RECOVERY_SUFFIX)
            except ImportError:
                content_str = getattr(system_msg, "text", str(system_msg.content))
                recovery_prompt = f"{content_str}\n\n{_TERMINAL_STALL_RECOVERY_SUFFIX}"
                new_system_msg = SystemMessage(content=recovery_prompt)
        else:
            new_system_msg = SystemMessage(content=_TERMINAL_STALL_RECOVERY_SUFFIX)

        existing_model_kwargs = request.model_settings.get("model_kwargs")
        model_kwargs = dict(existing_model_kwargs) if isinstance(existing_model_kwargs, Mapping) else {}
        model_kwargs["reasoning_effort"] = "none"
        model_settings = {**request.model_settings, "model_kwargs": model_kwargs}
        return request.override(
            system_message=new_system_msg,
            tool_choice="any",
            model_settings=model_settings,
        )

    @classmethod
    def _should_recover(cls, response: ModelResponse[Any], *, model: object) -> bool:
        return _is_fireworks_glm_5p2_model(model) and cls._is_terminal_stall(response)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Wrap synchronous model call to recover from GLM-5.2 headless stall conditions."""
        response = handler(request)
        if self._should_recover(response, model=request.model):
            logger.info("GLM-5.2 headless turn stalled at output cap; retrying once")
            response = handler(self._recovery_request(request))
            self._log_if_still_stalled(response)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        """Wrap asynchronous model call to recover from GLM-5.2 headless stall conditions."""
        response = await handler(request)
        if self._should_recover(response, model=request.model):
            logger.info("GLM-5.2 headless turn stalled at output cap; retrying once")
            response = await handler(self._recovery_request(request))
            self._log_if_still_stalled(response)
        return response

    @classmethod
    def _log_if_still_stalled(cls, response: ModelResponse[Any]) -> None:
        if cls._is_terminal_stall(response):
            logger.warning(
                "GLM-5.2 stall recovery retry still produced no tool call; "
                "the headless run may end without a deliverable"
            )
