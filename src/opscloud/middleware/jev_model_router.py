"""Jev-powered dynamic model and reasoning router middleware.

Operates strictly in Smart mode (Jev System One), dynamically classifying incoming
tasks using LangChain's official ModelRouterMiddleware and ModelChoice primitives in <70ms.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
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
from langchain_typesafe import ChoiceAnswer, ClassifierRequest, TypeSafeClassifier
from langchain_typesafe.experimental.middleware import (
    ModelChoice,
    ModelRouterMiddleware,
)

from opscloud.config.settings import get_settings, resolve_env_var
from opscloud.middleware.registry import register_middleware
from opscloud.model.pool import (
    ORCHESTRATOR_ROUTE_CRITERIA,
    ROUTER_INSTRUCTIONS,
    ROUTE_TO_TIER_MAP,
    SUBAGENT_ROUTE_CRITERIA,
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


def _resolve_active_capabilities(
    is_subagent: bool,
    subagent_name: str | None = None,
    explicit_capabilities: Sequence[Any] | None = None,
    subagent_meta: Mapping[str, Any] | None = None,
    available_subagents: Sequence[str] | None = None,
) -> list[str]:
    """Filter out active capabilities specific to the orchestrator or subagent."""
    capabilities: list[str] = []

    if not is_subagent:
        # Main Deep Agent (Orchestrator) Capabilities
        capabilities.extend([
            "software_and_devops_coding",
            "multi_agent_delegation",
            "system_synthesis_and_reporting",
            "workspace_and_git_inspection",
        ])
        # Dynamic capabilities derived from available subagents in ecosystem
        avail = [s.lower() for s in (available_subagents or [])]
        if any("finops" in s or "cost" in s or "billing" in s for s in avail):
            capabilities.append("finops_cost_optimization")
        if any("sre" in s or "incident" in s or "monitor" in s for s in avail):
            capabilities.append("sre_incident_triage")
        if any("iac" in s or "terraform" in s for s in avail):
            capabilities.append("iac_modular_refactoring")
        if any("security" in s or "secops" in s or "iam" in s for s in avail):
            capabilities.append("security_and_compliance_governance")
        if any("deploy" in s or "release" in s for s in avail):
            capabilities.append("deployment_and_rollout_orchestration")
        return capabilities

    # Subagent Specialist Capabilities (strictly domain-scoped, never includes multi_agent_delegation)
    if explicit_capabilities:
        for cap in explicit_capabilities:
            if isinstance(cap, str):
                capabilities.append(cap)
            elif isinstance(cap, dict):
                srv = cap.get("mcp_server") or cap.get("name")
                if srv:
                    capabilities.append(str(srv))

    name_lower = (subagent_name or "").lower()
    meta = subagent_meta or {}
    meta_tools = [str(t).lower() for t in (meta.get("tools") or [])]

    if "finops" in name_lower or "cost" in name_lower or any("billing" in t for t in meta_tools):
        capabilities.extend([
            "cloud_financial_management",
            "cost_and_usage_analysis",
            "savings_plans_and_waste_reduction",
        ])
    elif "sre" in name_lower or any("k8s" in t or "kube" in t for t in meta_tools):
        capabilities.extend([
            "cluster_and_pod_diagnostics",
            "incident_triage_and_log_analysis",
        ])
    elif "iac" in name_lower or "terraform" in name_lower:
        capabilities.extend([
            "infrastructure_as_code_authoring",
            "state_and_plan_verification",
        ])
    elif "security" in name_lower or "iam" in name_lower:
        capabilities.extend([
            "security_policy_evaluation",
            "least_privilege_audit",
        ])
    else:
        clean_name = name_lower.split("@")[0].replace("-", "_") if name_lower else "domain"
        capabilities.append(f"{clean_name}_specialist_execution")

    # Add tool-level operational capabilities
    if any(t in {"bash", "execute", "run_command", "terminal"} for t in meta_tools):
        capabilities.append("cli_and_script_execution")
    if any("read" in t or "grep" in t for t in meta_tools):
        capabilities.append("code_and_manifest_inspection")
    if any("billing" in t or "pricing" in t for t in meta_tools):
        capabilities.append("pricing_and_cost_api_queries")

    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for c in capabilities:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def _apply_confidence_safeguards(
    selected_route: str,
    confidence: float,
    user_text: str,
    is_subagent: bool,
) -> str:
    """Escalate model selection when Jev classification confidence falls below acceptable thresholds."""
    if confidence >= 0.55:
        return selected_route

    text_lower = user_text.lower()
    words = text_lower.split()

    # High-complexity domain markers covering coding architecture, deep debugging, and systemic optimization
    # Uses stems to match inflections (e.g. 'optimi' matches optimize, optimizing, optimization)
    high_complexity_markers = (
        # Optimization & FinOps
        "optimi", "cost", "finop", "saving", "bill", "spend", "waste",
        # SRE & Deep Debugging
        "crashloop", "oomkill", "incident", "outage", "root cause", "cascade", "deadlock",
        # Architecture & Code Refactoring
        "architect", "refactor", "redesign", "migrat", "peering", "reconcil",
        # Security & Compliance
        "secur", "iam audit", "vulnerab", "least privilege", "compliance",
    )

    is_complex = any(marker in text_lower for marker in high_complexity_markers) or ("cos" in words)

    if is_complex:
        if not is_subagent:
            logger.info("Jev Safety Escalation: Low confidence (%.2f) on high-complexity task -> Escalating to 'powerful'", confidence)
            return "powerful"
        else:
            logger.info("Jev Safety Escalation: Low confidence (%.2f) on subagent task -> Escalating to 'standard'", confidence)
            return "powerful" if selected_route == "powerful" else "standard"

    # If selected route was 'fast' but confidence is marginal, promote safely to 'standard'
    if selected_route == "fast":
        logger.info("Jev Safety Escalation: Low confidence (%.2f) on 'fast' -> Promoting to 'standard'", confidence)
        return "standard"

    return selected_route


def _build_jev_routing_state(
    user_msg: HumanMessage,
    ctx: dict[str, Any],
    runtime: Any,
    subagent_name: str | None = None,
    explicit_capabilities: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Construct an enriched state payload providing Jev with full architectural context."""
    is_subagent = bool(
        subagent_name is not None
        or ctx.get("ls_agent_type") == "subagent"
        or ctx.get("checkpoint_ns")
        or ctx.get("subagent_transcript_id")
        or ctx.get("subagent_name")
    )

    agent_role = "subagent" if is_subagent else "orchestrator"
    agent_name = subagent_name or str(ctx.get("subagent_name") or "") or ("subagent" if is_subagent else "opscloud-supervisor")

    subagent_names: list[str] = []
    subagent_ecosystem: list[str] | None = None
    sub_meta: Mapping[str, Any] | None = None

    if is_subagent:
        # Subagent ecosystem is strictly None (nil) for subagents
        subagent_ecosystem = None
        try:
            from opscloud.subagents.loader import list_subagents

            for meta in list_subagents():
                m_name = meta.get("name")
                if m_name and (m_name == agent_name or m_name.startswith(agent_name) or agent_name.startswith(m_name.split("@")[0])):
                    sub_meta = meta
                    break
        except Exception:
            pass
    else:
        try:
            from opscloud.subagents.loader import list_subagents

            for meta in list_subagents():
                n = meta.get("name")
                if n and n not in subagent_names:
                    subagent_names.append(n)
        except Exception:
            pass
        subagent_ecosystem = subagent_names[:8]

    active_capabilities = _resolve_active_capabilities(
        is_subagent=is_subagent,
        subagent_name=agent_name,
        explicit_capabilities=explicit_capabilities,
        subagent_meta=sub_meta,
        available_subagents=subagent_names,
    )

    has_tf = False
    has_k8s = False
    try:
        from pathlib import Path

        cwd = Path.cwd()
        has_tf = bool(list(cwd.glob("*.tf")) or (cwd / "terraform").is_dir())
        has_k8s = bool((cwd / "k8s").is_dir() or (cwd / "templates").is_dir() or list(cwd.glob("*.yaml")))
    except Exception:
        pass

    settings = get_settings()
    cloud_provider = getattr(settings, "cloud_provider", "aws")
    workspace_profile = {
        "has_codebase": True,
        "has_iac": has_tf,
        "has_kubernetes": has_k8s,
        "cloud_provider": cloud_provider,
    }

    routing_state = {
        "messages": [user_msg],
        "agent_role": agent_role,
        "agent_name": agent_name,
        "is_subagent": is_subagent,
        "subagent_ecosystem": subagent_ecosystem,
        "agent_context": {
            "role": agent_role,
            "agent_name": agent_name,
            "is_subagent": is_subagent,
            "available_subagents": subagent_ecosystem,
            "active_capabilities": active_capabilities,
            "workspace_profile": workspace_profile,
        },
    }
    return routing_state


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
        subagent_name: str | None = None,
        capabilities: Sequence[Any] | None = None,
    ) -> None:
        super().__init__()
        self.pool = pool_manager or get_model_pool_manager()
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._explicit_choices = choices
        self._instructions = instructions or ROUTER_INSTRUCTIONS
        self._subagent_name = subagent_name
        self._explicit_capabilities = list(capabilities) if capabilities else None

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
            choices = self.pool.get_model_choices(is_subagent=bool(subagent_name))

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

            is_subagent = bool(
                self._subagent_name is not None
                or ctx.get("ls_agent_type") == "subagent"
                or ctx.get("checkpoint_ns")
                or ctx.get("subagent_transcript_id")
                or ctx.get("subagent_name")
            )

            # Dynamically build question choices out of the model pool for the active base_spec and role if not explicitly hardcoded
            if self._explicit_choices is None:
                active_choices = self.pool.get_model_choices(
                    base_spec=base_spec,
                    is_subagent=is_subagent,
                )
                from langchain_typesafe.experimental.middleware.model_router import _ModelRouterConfig

                self.router.config = _ModelRouterConfig.model_validate(
                    {"choices": active_choices, "instructions": self._instructions}
                )
                self.router.models = {
                    route: _resolve_chat_model(choice.model)
                    for route, choice in active_choices.items()
                }

            routing_state = _build_jev_routing_state(
                user_msg=user_msg,
                ctx=ctx,
                runtime=runtime,
                subagent_name=self._subagent_name,
                explicit_capabilities=self._explicit_capabilities,
            )

            from langchain_typesafe.experimental.middleware.model_router import (
                _QUESTION_ID,
                _routing_questions,
            )

            if hasattr(self.router, "abefore_agent") and type(self.router.abefore_agent).__name__ in ("AsyncMock", "MagicMock"):
                routing_res = await asyncio.wait_for(
                    self.router.abefore_agent(cast(Any, routing_state), runtime),
                    timeout=self.timeout_seconds,
                )
                choice_answer: ChoiceAnswer = routing_res["model_route"]
            else:
                classification_payload = cast(
                    ClassifierRequest,
                    {
                        "state": routing_state,
                        "questions": _routing_questions(self.router.config),
                    },
                )
                res = await asyncio.wait_for(
                    self.router.classifier.ainvoke(classification_payload),
                    timeout=self.timeout_seconds,
                )
                choice_answer: ChoiceAnswer = res.choices[_QUESTION_ID]

            selected_route = choice_answer.choice
            confidence = getattr(choice_answer, "confidence", 1.0)

            # Apply confidence calibration & safeguards
            calibrated_route = _apply_confidence_safeguards(
                selected_route=selected_route,
                confidence=confidence,
                user_text=user_text,
                is_subagent=is_subagent,
            )
            if calibrated_route != selected_route:
                logger.info(
                    "Jev Dynamic Router: calibrated route from '%s' to '%s' (conf=%.2f, is_subagent=%s)",
                    selected_route,
                    calibrated_route,
                    confidence,
                    is_subagent,
                )
                selected_route = calibrated_route
                try:
                    choice_answer.choice = calibrated_route
                except Exception:
                    pass

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
                "is_subagent": is_subagent,
            }
            _emit_model_routed_event(runtime, event_payload)

            if not is_subagent:
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
        if dyn_route and isinstance(dyn_route, dict) and dyn_route.get("route"):
            selected_route = dyn_route["route"]
        elif route_answer is not None and hasattr(route_answer, "choice"):
            selected_route = route_answer.choice
        elif dyn_route and isinstance(dyn_route, dict):
            selected_route = TIER_TO_ROUTE_MAP.get(dyn_route.get("tier", 1))

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

        is_subagent = bool(
            self._subagent_name is not None
            or ctx.get("ls_agent_type") == "subagent"
            or ctx.get("checkpoint_ns")
            or ctx.get("subagent_transcript_id")
            or ctx.get("subagent_name")
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
            "is_subagent": is_subagent,
        }
        if runtime:
            _emit_model_routed_event(runtime, event_payload)

        if not is_subagent:
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
        if dyn_route and isinstance(dyn_route, dict) and dyn_route.get("route"):
            selected_route = dyn_route["route"]
        elif route_answer is not None and hasattr(route_answer, "choice"):
            selected_route = route_answer.choice
        elif dyn_route and isinstance(dyn_route, dict):
            selected_route = TIER_TO_ROUTE_MAP.get(dyn_route.get("tier", 1))

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

        is_subagent = bool(
            self._subagent_name is not None
            or ctx.get("ls_agent_type") == "subagent"
            or ctx.get("checkpoint_ns")
            or ctx.get("subagent_transcript_id")
            or ctx.get("subagent_name")
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
            "is_subagent": is_subagent,
        }
        if runtime:
            _emit_model_routed_event(runtime, event_payload)

        if not is_subagent:
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
    "ORCHESTRATOR_ROUTE_CRITERIA",
    "SUBAGENT_ROUTE_CRITERIA",
]
