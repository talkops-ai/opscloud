"""Middleware for runtime model selection via LangGraph runtime context."""

from typing import Any, Awaitable, Callable

from opscloud.utils.logger import get_logger
from deepagents._models import (
    get_model_identifier,
    model_matches_spec,
)
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langgraph.types import Command

from opscloud.middleware.registry import register_middleware

logger = get_logger(__name__)

_ANTHROPIC_ONLY_SETTINGS: set[str] = {"cache_control"}


def _get_ls_provider(model: object) -> str | None:
    """Return the provider name reported by the chat model."""
    try:
        ls_params = getattr(model, "_get_ls_params", None)
        if ls_params:
            params = ls_params()
            if isinstance(params, dict):
                provider = params.get("ls_provider")
                if isinstance(provider, str):
                    return provider
    except Exception:
        pass
    return None


def _is_anthropic_model(model: object) -> bool:
    return _get_ls_provider(model) == "anthropic"


@register_middleware(name="configurable_model")
class ConfigurableModelMiddleware(AgentMiddleware):
    """Swap the model or per-call settings from runtime.context."""

    def __init__(self, *, persist_model_state: bool = True) -> None:
        self._persist_model_state = persist_model_state

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        resolved_req, resolved_spec, resolved_params = self._apply_overrides(request)
        response = handler(resolved_req)

        command = (
            self._checkpoint_command(resolved_spec, resolved_params)
            if self._persist_model_state
            else None
        )
        if command is None:
            return response
        return ExtendedModelResponse(model_response=response, command=command)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        resolved_req, resolved_spec, resolved_params = (
            await self._apply_overrides_async(request)
        )
        response = await handler(resolved_req)

        command = (
            self._checkpoint_command(resolved_spec, resolved_params)
            if self._persist_model_state
            else None
        )
        if command is None:
            return response
        return ExtendedModelResponse(model_response=response, command=command)

    def _apply_overrides(
        self, request: ModelRequest
    ) -> tuple[ModelRequest, str | None, dict[str, Any] | None]:
        ctx = self._get_context(request)
        if ctx is None:
            return request, self._model_spec_from_model(request.model), None

        model_spec = ctx.get("model")
        model_params = ctx.get("model_params") or {}

        model_result = None
        if model_spec and model_spec not in {"auto", "dynamic"} and not model_matches_spec(request.model, model_spec):
            from opscloud.model.factory import create_model
            from opscloud.exceptions import ModelConfigError

            try:
                model_result = create_model(model_spec)
            except ModelConfigError:
                logger.exception(
                    "Failed to resolve model override '%s'; keeping current model.",
                    model_spec,
                )

        updated_request = self._build_overrides(request, model_result, model_params)
        resolved_spec = self._model_spec_from_result(
            model_result, updated_request.model
        )
        return updated_request, resolved_spec, model_params

    async def _apply_overrides_async(
        self, request: ModelRequest
    ) -> tuple[ModelRequest, str | None, dict[str, Any] | None]:
        import asyncio

        ctx = self._get_context(request)
        if ctx is None:
            return request, self._model_spec_from_model(request.model), None

        model_spec = ctx.get("model")
        model_params = ctx.get("model_params") or {}

        model_result = None
        if model_spec and model_spec not in {"auto", "dynamic"} and not model_matches_spec(request.model, model_spec):
            from opscloud.model.factory import create_model
            from opscloud.exceptions import ModelConfigError

            try:
                model_result = await asyncio.to_thread(create_model, model_spec)
            except ModelConfigError:
                logger.exception(
                    "Failed to resolve model override '%s'; keeping current model.",
                    model_spec,
                )

        updated_request = self._build_overrides(request, model_result, model_params)
        resolved_spec = self._model_spec_from_result(
            model_result, updated_request.model
        )
        return updated_request, resolved_spec, model_params

    def _build_overrides(
        self, request: ModelRequest, model_result: Any, model_params: dict[str, Any]
    ) -> ModelRequest:
        overrides: dict[str, Any] = {}

        new_model = model_result.model if model_result is not None else None
        if new_model is not None:
            overrides["model"] = new_model

        if model_params:
            overrides["model_settings"] = {**request.model_settings, **model_params}

        # Switch away from Anthropic -> strip Anthropic settings
        if new_model is not None and not _is_anthropic_model(new_model):
            settings = overrides.get("model_settings", request.model_settings)
            dropped = settings.keys() & _ANTHROPIC_ONLY_SETTINGS
            if dropped:
                overrides["model_settings"] = {
                    k: v for k, v in settings.items() if k not in dropped
                }

        # Patch the Model Identity section in system_prompt if model changed
        prompt = getattr(request, "system_prompt", None)
        if model_result is not None and isinstance(prompt, str):
            from opscloud.prompts import MODEL_IDENTITY_RE, build_model_identity_section

            new_identity = build_model_identity_section(
                model_result.model_name,
                provider=model_result.provider,
                context_limit=model_result.context_limit,
                unsupported_modalities=getattr(model_result, "unsupported_modalities", frozenset()),
            )
            patched = MODEL_IDENTITY_RE.sub(new_identity, prompt, count=1)
            if patched != prompt:
                overrides["system_prompt"] = patched
            elif "### Model Identity" in prompt:
                logger.warning(
                    "System prompt contains '### Model Identity' but regex did not match; "
                    "identity was not updated for model '%s'.",
                    model_result.model_name,
                )

        if not overrides:
            return request

        return request.override(**overrides)

    def _get_context(self, request: ModelRequest) -> dict[str, Any] | None:
        runtime = request.runtime
        if runtime is None or runtime.context is None:
            return None
        ctx = runtime.context
        if isinstance(ctx, dict):
            return ctx
        return {
            "model": getattr(ctx, "model", None),
            "model_params": getattr(ctx, "model_params", None),
            "thread_id": getattr(ctx, "thread_id", None),
        }

    def _model_spec_from_model(self, model: Any) -> str | None:
        provider = _get_ls_provider(model)
        model_name = get_model_identifier(model)
        if provider and model_name:
            return f"{provider}:{model_name}"
        return None

    def _model_spec_from_result(self, model_result: Any, model: Any) -> str | None:
        if (
            model_result is not None
            and model_result.provider
            and model_result.model_name
        ):
            return f"{model_result.provider}:{model_result.model_name}"
        return self._model_spec_from_model(model)

    def _checkpoint_command(
        self, model_spec: str | None, model_params: dict[str, Any] | None
    ) -> Command[Any] | None:
        update: dict[str, Any] = {}
        if model_spec:
            update["_model_spec"] = model_spec
        if model_params:
            update["_model_params"] = model_params
        if not update:
            return None
        return Command(update=update)
