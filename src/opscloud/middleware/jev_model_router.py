"""Jev-powered dynamic model and reasoning router middleware.

Operates strictly in Smart mode (Jev System One), dynamically classifying incoming
tasks using LangChain's official ModelRouterMiddleware and ModelChoice primitives in <70ms.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import os
from typing import Any, Awaitable, Callable, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import HumanMessage
from langchain_typesafe import ChoiceAnswer, TypeSafeClassifier
from langchain_typesafe.experimental.middleware import (
    ModelChoice,
    ModelRouterMiddleware,
)

from opscloud.config.settings import get_settings, resolve_env_var
from opscloud.middleware.registry import register_middleware
from opscloud.model.pool import (
    ROUTER_INSTRUCTIONS,
    ROUTE_TO_TIER_MAP,
    TIER_TO_ROUTE_MAP,
    DynamicModelPoolManager,
    get_model_pool_manager,
)
from opscloud.model.reasoning import (
    supported_efforts_for_model,
    without_effort_model_params,
)
from opscloud.security.approval_mode import ApprovalMode
from opscloud.security.approval_mode_source import ApprovalPolicyResolver
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def _split_spec(spec: str) -> tuple[str, str]:
    if ":" in spec:
        p, m = spec.split(":", 1)
        return p, m
    return "", spec


def _emit_model_routed_event(runtime: Any, payload: dict[str, Any]) -> None:
    """Emit model_routed event via LangGraph stream_writer (stream_mode='custom')."""
    if runtime is None:
        return
    writer = getattr(runtime, "stream_writer", None)
    if not callable(writer) and hasattr(runtime, "config"):
        cfg = getattr(runtime, "config", None)
        if isinstance(cfg, dict):
            configurable = cfg.get("configurable", {})
            pregel_rt = configurable.get("__pregel_runtime")
            writer = getattr(pregel_rt, "stream_writer", None)

    if callable(writer):
        try:
            writer(payload)
            logger.debug("Emitted model_routed event via stream_writer: %s", payload)
        except Exception as exc:
            logger.debug("Failed to emit model_routed via stream_writer: %s", exc)


def _extract_runtime_context(runtime: object) -> dict[str, object]:
    """Extract merged context dict from a runtime object, merging config, context, and execution_info."""
    merged: dict[str, object] = {}
    if runtime is None:
        return merged

    if isinstance(runtime, Mapping):
        merged.update(runtime)

    cfg = getattr(runtime, "config", None)
    if isinstance(cfg, Mapping):
        merged.update(cfg)
        configurable = cfg.get("configurable")
        if isinstance(configurable, Mapping):
            merged.update(configurable)
        metadata = cfg.get("metadata")
        if isinstance(metadata, Mapping):
            merged.update(metadata)

    ctx = getattr(runtime, "context", None)
    if isinstance(ctx, Mapping):
        merged.update(ctx)
    elif ctx is not None:
        if hasattr(ctx, "__dict__"):
            merged.update({k: v for k, v in vars(ctx).items() if not k.startswith("_")})
    elif hasattr(runtime, "__dict__"):
        merged.update({k: v for k, v in vars(runtime).items() if not k.startswith("_")})

    exec_info = getattr(runtime, "execution_info", None)
    if exec_info is not None:
        exec_thread = getattr(exec_info, "thread_id", None)
        if exec_thread and "thread_id" not in merged:
            merged["thread_id"] = str(exec_thread)
        exec_run = getattr(exec_info, "run_id", None)
        if exec_run and "run_id" not in merged:
            merged["run_id"] = str(exec_run)

    return merged


async def _ais_smart_mode(runtime: object, store: object = None) -> bool:
    """Check whether execution is running under Jev Smart mode."""
    if runtime is None:
        settings = get_settings()
        return getattr(settings, "approval_mode", None) == "smart" or getattr(settings, "smart", False) is True

    context = _extract_runtime_context(runtime)
    if store is None:
        store = getattr(runtime, "store", None)

    # 1. Direct flags in context or configurable
    raw_mode = context.get("approval_mode")
    if (
        context.get("smart") is True
        or (isinstance(raw_mode, str) and raw_mode.lower() in ("smart", "jev"))
        or raw_mode == ApprovalMode.SMART
    ):
        return True

    # 2. Check store lookup via approval policy resolver
    try:
        from opscloud.security.approval_mode_source import _aresolve_approval_mode

        mode = await _aresolve_approval_mode(context, store)
        if mode == ApprovalMode.SMART:
            return True
    except Exception as exc:
        logger.debug("Failed resolving approval mode asynchronously from store: %s", exc)

    # 3. Explicit mode check on context
    mode = ApprovalPolicyResolver._extract_explicit_mode(context)
    if mode == ApprovalMode.SMART:
        return True

    # 4. Check global settings
    settings = get_settings()
    if settings is not None:
        if getattr(settings, "approval_mode", None) == "smart" or getattr(settings, "smart", False) is True:
            return True

    return False


def _is_smart_mode_sync(runtime: object, store: object = None) -> bool:
    """Synchronous check whether execution is running under Jev Smart mode."""
    if runtime is None:
        settings = get_settings()
        return getattr(settings, "approval_mode", None) == "smart" or getattr(settings, "smart", False) is True

    context = _extract_runtime_context(runtime)
    if store is None:
        store = getattr(runtime, "store", None)

    # 1. Direct flags in context or configurable
    raw_mode = context.get("approval_mode")
    if (
        context.get("smart") is True
        or (isinstance(raw_mode, str) and raw_mode.lower() in ("smart", "jev"))
        or raw_mode == ApprovalMode.SMART
    ):
        return True

    # 2. Check store lookup via approval policy resolver
    try:
        from opscloud.security.approval_mode_source import _resolve_approval_mode

        mode = _resolve_approval_mode(context, store)
        if mode == ApprovalMode.SMART:
            return True
    except Exception as exc:
        logger.debug("Failed resolving approval mode synchronously from store: %s", exc)

    # 3. Explicit mode check on context
    mode = ApprovalPolicyResolver._extract_explicit_mode(context)
    if mode == ApprovalMode.SMART:
        return True

    # 4. Check global settings
    settings = get_settings()
    if settings is not None:
        if getattr(settings, "approval_mode", None) == "smart" or getattr(settings, "smart", False) is True:
            return True

    return False


def _extract_last_user_text(state: dict[str, Any]) -> str | None:
    """Extract text of the most recent user/human message across message formats."""
    messages = state.get("messages", []) if isinstance(state, Mapping) else getattr(state, "messages", [])
    if not isinstance(messages, (list, tuple)):
        return None

    for msg in reversed(messages):
        # 1. HumanMessage instance
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                text_parts: list[str] = []
                for part in content:
                    if isinstance(part, str):
                        text_parts.append(part)
                    elif isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(str(part.get("text", "")))
                return " ".join(text_parts).strip()
            return str(content).strip()

        # 2. Message object with .type or .role
        msg_type = getattr(msg, "type", None) or getattr(msg, "role", None)
        if msg_type in ("human", "user"):
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, str):
                        text_parts.append(part)
                    elif isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(str(part.get("text", "")))
                return " ".join(text_parts).strip()
            return str(content).strip()

        # 3. Dict-based message
        if isinstance(msg, dict):
            role = msg.get("role") or msg.get("type")
            if role in ("human", "user"):
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, str):
                            text_parts.append(part)
                        elif isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(str(part.get("text", "")))
                    return " ".join(text_parts).strip()
                return str(content).strip()

    return None


def _resolve_chat_model(model_or_spec: str | Any) -> Any:
    """Resolve a model specification or instance safely."""
    if not isinstance(model_or_spec, str):
        return model_or_spec
    try:
        from langchain.chat_models import init_chat_model

        return init_chat_model(model_or_spec)
    except Exception as exc:
        logger.debug("init_chat_model(%s) failed: %s; falling back to create_model", model_or_spec, exc)
        from opscloud.model.factory import create_model

        return create_model(model_or_spec).model


@register_middleware(name="jev_model_router")
class JevDynamicModelRouterMiddleware(AgentMiddleware[Any, Any]):
    """Dynamic model and reasoning effort router powered by TypeSafe AI Jev.

    Composes LangChain's official ModelRouterMiddleware, allowing either
    explicit ModelChoice configuration or autonomous discovery from
    OpsCloud's DynamicModelPoolManager.
    """

    def __init__(
        self,
        choices: Mapping[str, ModelChoice] | None = None,
        instructions: str | None = None,
        pool_manager: DynamicModelPoolManager | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 1.5,
    ) -> None:
        super().__init__()
        self.pool = pool_manager or get_model_pool_manager()
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._explicit_choices = choices
        self._instructions = instructions or ROUTER_INSTRUCTIONS

        resolved_key = self._api_key
        if not resolved_key:
            settings_key = getattr(get_settings(), "typesafe_api_key", None)
            resolved_key = settings_key or resolve_env_var("TYPESAFE_API_KEY", fallback_names=("JEV_API_KEY",))
        if resolved_key and "TYPESAFE_API_KEY" not in os.environ:
            os.environ["TYPESAFE_API_KEY"] = resolved_key
        elif "TYPESAFE_API_KEY" not in os.environ:
            # Placeholder prevents TypeSafeClassifier instantiation from failing when no key configured yet
            os.environ.setdefault("TYPESAFE_API_KEY", "placeholder")

        if choices is None:
            choices = self.pool.get_model_choices()

        self.router = ModelRouterMiddleware(
            choices=choices,
            instructions=self._instructions,
        )

        if resolved_key and resolved_key != "placeholder":
            self.router.classifier = TypeSafeClassifier(api_key=resolved_key)

    @property
    def config(self) -> Any:
        return self.router.config

    @property
    def models(self) -> dict[str, Any]:
        return self.router.models

    @property
    def classifier(self) -> TypeSafeClassifier:
        return self.router.classifier

    @classifier.setter
    def classifier(self, value: TypeSafeClassifier) -> None:
        self.router.classifier = value

    def is_available(self) -> bool:
        """Check whether TypeSafe API credentials are configured."""
        if self._api_key and self._api_key != "placeholder":
            return True
        settings_key = getattr(get_settings(), "typesafe_api_key", None)
        if settings_key and settings_key != "placeholder":
            return True
        key = resolve_env_var("TYPESAFE_API_KEY", fallback_names=("JEV_API_KEY",))
        return bool(key and key != "placeholder")

    def _is_smart_mode(self, context: Any) -> bool:
        """Check whether execution is running under Jev Smart mode (backward compatibility)."""
        if context is None:
            return False
        mode = ApprovalPolicyResolver._extract_explicit_mode(context)
        if mode == ApprovalMode.SMART:
            return True
        if isinstance(context, dict):
            if context.get("smart") is True or context.get("approval_mode") == "smart":
                return True
        if getattr(context, "smart", False) is True:
            return True
        if getattr(context, "approval_mode", None) == "smart":
            return True
        return False

    def _get_base_spec(self, context: Any) -> str | None:
        """Extract baseline session model spec from runtime context or settings."""
        if isinstance(context, Mapping):
            spec = context.get("model")
            if spec and spec not in {"auto", "dynamic"}:
                return str(spec)
            cfg = context.get("configurable")
            if isinstance(cfg, Mapping):
                spec = cfg.get("model")
                if spec and spec not in {"auto", "dynamic"}:
                    return str(spec)
        elif context is not None:
            spec = getattr(context, "model", None)
            if spec and spec not in {"auto", "dynamic"}:
                return str(spec)
        settings = get_settings()
        spec = getattr(settings, "model_name", None) or getattr(settings, "model", None)
        return str(spec) if spec and spec not in {"auto", "dynamic"} else None

    @staticmethod
    def _latest_human_message(state: Any) -> HumanMessage:
        """Return the latest human message from agent state across message formats."""
        messages = state.get("messages", []) if isinstance(state, Mapping) else getattr(state, "messages", [])
        if not isinstance(messages, (list, tuple)):
            return HumanMessage(content="")

        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                return msg
            if isinstance(msg, dict):
                role = msg.get("role") or msg.get("type")
                if role in ("human", "user"):
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        return HumanMessage(content=content)
                    if isinstance(content, list):
                        parts = [
                            p if isinstance(p, str) else str(p.get("text", ""))
                            for p in content
                            if isinstance(p, (str, dict))
                        ]
                        return HumanMessage(content=" ".join(parts))
                    return HumanMessage(content=str(content))
            msg_type = getattr(msg, "type", None) or getattr(msg, "role", None)
            if msg_type in ("human", "user"):
                content = getattr(msg, "content", "")
                if isinstance(content, str):
                    return HumanMessage(content=content)
                return HumanMessage(content=str(content))

        return HumanMessage(content="")

    # ── Turn-level routing classification (before_agent) ──

    async def abefore_agent(
        self,
        state: dict[str, Any],
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Evaluate task and select model route asynchronously using TypeSafe Jev."""
        if not await _ais_smart_mode(runtime):
            logger.debug("Jev Dynamic Router: smart mode not active; skipping dynamic model routing.")
            return None

        if not self.is_available():
            logger.debug("Smart mode active but TypeSafe API key not configured; skipping dynamic model routing.")
            return None

        user_msg = self._latest_human_message(state)
        user_text = user_msg.content if isinstance(user_msg.content, str) else str(user_msg.content)
        if not user_text.strip():
            logger.debug("Jev Dynamic Router: no user message found in state; skipping dynamic routing.")
            return None

        try:
            ctx = _extract_runtime_context(runtime)
            base_spec = self._get_base_spec(ctx)

            # Dynamically build question choices out of the model pool for the active base_spec if not explicitly hardcoded
            if self._explicit_choices is None:
                active_choices = self.pool.get_model_choices(base_spec=base_spec)
                from langchain_typesafe.experimental.middleware.model_router import _ModelRouterConfig

                self.router.config = _ModelRouterConfig.model_validate(
                    {"choices": active_choices, "instructions": self._instructions}
                )
                self.router.models = {
                    route: _resolve_chat_model(choice.model)
                    for route, choice in active_choices.items()
                }

            router_state = dict(state)
            router_state["messages"] = [user_msg]
            routing_res = await asyncio.wait_for(
                self.router.abefore_agent(cast(Any, router_state), runtime),
                timeout=self.timeout_seconds,
            )
            choice_answer: ChoiceAnswer = routing_res["model_route"]
            selected_route = choice_answer.choice
            confidence = getattr(choice_answer, "confidence", 1.0)
            target_tier = ROUTE_TO_TIER_MAP.get(selected_route, 1)

            # Retrieve model spec & reasoning effort details for TUI & settings
            chat_model, selected_spec, final_effort, _ = self.pool.get_model_for_tier(
                target_tier,
                base_spec=base_spec,
            )
            provider, model_name = _split_spec(selected_spec)

            logger.info(
                "Jev Dynamic Router: evaluated task %r -> selected route=%s (tier=%d, %s, effort=%s, conf=%.2f)",
                user_text[:80] + ("..." if len(user_text) > 80 else ""),
                selected_route,
                target_tier,
                selected_spec,
                final_effort,
                confidence,
            )

            event_payload = {
                "type": "model_routed",
                "spec": selected_spec,
                "provider": provider,
                "model": model_name,
                "effort": final_effort,
                "tier": target_tier,
                "route": selected_route,
                "confidence": confidence,
            }
            _emit_model_routed_event(runtime, event_payload)

            settings = get_settings()
            if settings is not None:
                settings.model = model_name
                settings.model_name = model_name
                settings.model_provider = provider
                if final_effort:
                    settings.reasoning_effort = final_effort

            try:
                await adispatch_custom_event(
                    "model_routed",
                    event_payload,
                    config=getattr(runtime, "config", None),
                )
            except Exception as ev_exc:
                logger.debug("Failed to dispatch model_routed callback event: %s", ev_exc)

            return {
                "model_route": choice_answer,
                "_dynamic_model_route": {
                    "spec": selected_spec,
                    "effort": final_effort,
                    "tier": target_tier,
                    "route": selected_route,
                    "confidence": confidence,
                },
            }

        except Exception as exc:
            logger.warning(
                "Jev Dynamic Router: classification failed (%s); falling back to base model",
                exc,
            )
            return None

    def before_agent(
        self,
        state: dict[str, Any],
        runtime: Any,
    ) -> dict[str, Any] | None:
        """Synchronous version of before_agent."""
        if not _is_smart_mode_sync(runtime) or not self.is_available():
            return None

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(asyncio.run, self.abefore_agent(state, runtime)).result()
        return asyncio.run(self.abefore_agent(state, runtime))

    # ── Per-model-call interception (wrap_model_call) ────

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT] | ExtendedModelResponse[ResponseT]:
        """Apply dynamic model and reasoning effort parameters to the outgoing model call."""
        runtime = getattr(request, "runtime", None)
        route_answer = request.state.get("model_route") if isinstance(request.state, dict) else None
        dyn_route = request.state.get("_dynamic_model_route") if isinstance(request.state, dict) else None

        is_smart = (route_answer is not None) or (dyn_route is not None) or await _ais_smart_mode(runtime)
        if not is_smart:
            return await handler(request)

        selected_route = None
        if route_answer is not None and hasattr(route_answer, "choice"):
            selected_route = route_answer.choice
        elif dyn_route and isinstance(dyn_route, dict):
            selected_route = dyn_route.get("route") or TIER_TO_ROUTE_MAP.get(dyn_route.get("tier", 1))

        if not selected_route or selected_route not in self.models:
            selected_route = "standard" if "standard" in self.models else next(iter(self.models.keys()))

        selected_model = self.models[selected_route]
        tier = ROUTE_TO_TIER_MAP.get(selected_route, 1)

        ctx = _extract_runtime_context(runtime)
        base_spec = self._get_base_spec(ctx)
        chat_model, selected_spec, final_effort, native_settings = self.pool.get_model_for_tier(
            tier,
            base_spec=base_spec,
        )

        logger.info(
            "Jev Dynamic Router: dispatching model call -> %s (route=%s, effort=%s, tier=%d)",
            selected_spec,
            selected_route,
            final_effort,
            tier,
        )

        provider, model_name = _split_spec(selected_spec)
        event_payload = {
            "type": "model_routed",
            "spec": selected_spec,
            "provider": provider,
            "model": model_name,
            "effort": final_effort,
            "tier": tier,
            "route": selected_route,
        }
        if runtime:
            _emit_model_routed_event(runtime, event_payload)

        settings = get_settings()
        if settings is not None:
            settings.model = model_name
            settings.model_name = model_name
            settings.model_provider = provider
            if final_effort:
                settings.reasoning_effort = final_effort

        if final_effort in ("off", "none", "clear", "0", "reset") or not supported_efforts_for_model(selected_spec):
            merged_settings = without_effort_model_params(selected_spec, request.model_settings or {}) or {}
        else:
            merged_settings = {**(request.model_settings or {}), **native_settings}
        updated_request = request.override(
            model=selected_model or chat_model,
            model_settings=merged_settings,
        )
        return await handler(updated_request)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | ExtendedModelResponse[ResponseT]:
        """Synchronous model call wrapper."""
        runtime = getattr(request, "runtime", None)
        route_answer = request.state.get("model_route") if isinstance(request.state, dict) else None
        dyn_route = request.state.get("_dynamic_model_route") if isinstance(request.state, dict) else None

        is_smart = (route_answer is not None) or (dyn_route is not None) or _is_smart_mode_sync(runtime)
        if not is_smart:
            return handler(request)

        selected_route = None
        if route_answer is not None and hasattr(route_answer, "choice"):
            selected_route = route_answer.choice
        elif dyn_route and isinstance(dyn_route, dict):
            selected_route = dyn_route.get("route") or TIER_TO_ROUTE_MAP.get(dyn_route.get("tier", 1))

        if not selected_route or selected_route not in self.models:
            selected_route = "standard" if "standard" in self.models else next(iter(self.models.keys()))

        selected_model = self.models[selected_route]
        tier = ROUTE_TO_TIER_MAP.get(selected_route, 1)

        ctx = _extract_runtime_context(runtime)
        base_spec = self._get_base_spec(ctx)
        chat_model, selected_spec, final_effort, native_settings = self.pool.get_model_for_tier(
            tier,
            base_spec=base_spec,
        )

        logger.info(
            "Jev Dynamic Router: dispatching model call -> %s (route=%s, effort=%s, tier=%d)",
            selected_spec,
            selected_route,
            final_effort,
            tier,
        )

        provider, model_name = _split_spec(selected_spec)
        event_payload = {
            "type": "model_routed",
            "spec": selected_spec,
            "provider": provider,
            "model": model_name,
            "effort": final_effort,
            "tier": tier,
            "route": selected_route,
        }
        if runtime:
            _emit_model_routed_event(runtime, event_payload)

        settings = get_settings()
        if settings is not None:
            settings.model = model_name
            settings.model_name = model_name
            settings.model_provider = provider
            if final_effort:
                settings.reasoning_effort = final_effort

        if final_effort in ("off", "none", "clear", "0", "reset") or not supported_efforts_for_model(selected_spec):
            merged_settings = without_effort_model_params(selected_spec, request.model_settings or {}) or {}
        else:
            merged_settings = {**(request.model_settings or {}), **native_settings}
        updated_request = request.override(
            model=selected_model or chat_model,
            model_settings=merged_settings,
        )
        return handler(updated_request)


__all__ = [
    "JevDynamicModelRouterMiddleware",
    "ModelChoice",
    "ModelRouterMiddleware",
]
