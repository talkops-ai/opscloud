"""Dynamic Model Pool Manager for Jev-powered autonomous model routing.

Discovers candidate models and provider-native reasoning parameters directly
from OpsCloud's model capability registry (AVAILABLE_MODELS and MODEL_PROFILES)
without hardcoding provider model strings.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import threading
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_typesafe.experimental.middleware import ModelChoice

from opscloud.model.config import (
    AVAILABLE_MODELS,
    DEFAULT_PROVIDER_PRIORITY,
    detect_provider,
    get_model_profile,
    has_provider_credentials,
    normalize_model_spec,
)
from opscloud.model.factory import ModelResult, create_model
from opscloud.model.reasoning import (
    default_effort_for_model,
    supported_efforts_for_model,
    with_effort_model_params,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


ROUTER_INSTRUCTIONS = "Choose the least costly model that can complete the task."

DEFAULT_ROUTE_CRITERIA: dict[str, str] = {
    "fast": (
        "Direct lookups, greetings, chit-chat, conceptual explanations, status checks, "
        "or simple text queries requiring no cloud CLI tools or complex multi-step reasoning."
    ),
    "standard": (
        "Routine cloud operations, AWS/GCP/Azure CLI execution, Kubernetes manifests, "
        "Terraform edits, cloud resource listing/mutations, and standard DevOps tasks."
    ),
    "powerful": (
        "Complex incident triage, debugging crashlooping pods, distributed telemetry and trace correlation, "
        "root-cause analysis, and complex cloud architecture design."
    ),
}

ROUTE_TO_TIER_MAP: dict[str, int] = {
    "fast": 0,
    "standard": 1,
    "powerful": 2,
    "tier_0_fast": 0,
    "tier_1_standard": 1,
    "tier_2_deep": 2,
}

TIER_TO_ROUTE_MAP: dict[int, str] = {
    0: "fast",
    1: "standard",
    2: "powerful",
}


@dataclass(frozen=True)
class TierResolution:
    """Resolved model spec and reasoning effort for a specific operational tier."""

    tier: int
    spec: str
    effort: str
    is_reasoning_enabled: bool


class DynamicModelPoolManager:
    """Manages dynamic model tiering and hot instances for the Jev model router."""

    def __init__(self) -> None:
        self._instances: dict[str, ModelResult] = {}
        self._lock = threading.Lock()

    def _resolve_provider(self, provider: str | None = None, base_spec: str | None = None) -> str:
        """Determine the active provider from explicit parameter, base spec, or credentials across all supported providers."""
        if provider:
            return provider
        if base_spec and base_spec not in ("dynamic", "auto", "default"):
            inferred = detect_provider(base_spec)
            if inferred and inferred not in ("dynamic", "auto"):
                return inferred
            if ":" in base_spec:
                cand = base_spec.split(":", 1)[0]
                if cand in AVAILABLE_MODELS:
                    return cand

        # Check configured model in ModelConfig or Settings if credentials exist
        try:
            from opscloud.config.settings import get_settings
            from opscloud.model.config import ModelConfig

            config = ModelConfig.load()
            settings = get_settings()
            for cand in (
                getattr(settings, "model_name", None),
                getattr(settings, "model", None),
                config.default_model,
                config.recent_model,
            ):
                if cand and cand not in ("dynamic", "auto", "default"):
                    cand_prov = detect_provider(cand) or (cand.split(":", 1)[0] if ":" in cand else None)
                    if cand_prov and cand_prov in AVAILABLE_MODELS and has_provider_credentials(cand_prov) is True:
                        return cand_prov
        except Exception as exc:
            logger.debug("Failed inspecting configured default/recent model for provider resolution: %s", exc)

        all_supported = [p for p in DEFAULT_PROVIDER_PRIORITY if p in AVAILABLE_MODELS] + [
            p for p in AVAILABLE_MODELS if p not in DEFAULT_PROVIDER_PRIORITY
        ]
        for prov in all_supported:
            if has_provider_credentials(prov) is True:
                return prov

        return "google_genai"

    def discover_tiers(
        self,
        provider: str | None = None,
        base_spec: str | None = None,
    ) -> dict[int, tuple[str, str]]:
        """Dynamically build Tier 0, 1, and 2 definitions from registry metadata.

        Args:
            provider: Target provider name, or None to infer.
            base_spec: Baseline model spec selected by the session/thread.

        Returns:
            Dictionary mapping tier level (0, 1, 2) to (model_spec, reasoning_effort).
        """
        # 1. Prioritize user-configured pool from config.toml [agent_pool]
        from opscloud.config.toml_config import load_agent_pool

        user_pool = load_agent_pool()
        if user_pool:
            resolved_provider = self._resolve_provider(provider, base_spec)
            pool_prov = user_pool.get("provider")
            if not pool_prov:
                sample = user_pool.get("standard") or user_pool.get("fast") or user_pool.get("powerful") or ""
                pool_prov = detect_provider(sample) if sample else None

            if not provider or not pool_prov or pool_prov == resolved_provider:
                t0_raw = user_pool.get("fast")
                t1_raw = user_pool.get("standard")
                t2_raw = user_pool.get("powerful")

                def _norm(raw_spec: str) -> str:
                    s = raw_spec.strip()
                    if ":" not in s:
                        return f"{resolved_provider}:{s}"
                    return normalize_model_spec(s)

                if t0_raw and t1_raw and t2_raw:
                    t0 = _norm(t0_raw)
                    t1 = _norm(t1_raw)
                    t2 = _norm(t2_raw)

                    t0_efforts = supported_efforts_for_model(t0)
                    e0 = "off" if ("off" in t0_efforts or not t0_efforts) else ("minimal" if "minimal" in t0_efforts else "off")

                    t1_efforts = supported_efforts_for_model(t1)
                    e1 = "medium" if "medium" in t1_efforts else (default_effort_for_model(t1) or "off")

                    t2_efforts = supported_efforts_for_model(t2)
                    e2 = "max" if "max" in t2_efforts else ("high" if "high" in t2_efforts else ("medium" if "medium" in t2_efforts else (default_effort_for_model(t2) or "off")))

                    return {
                        0: (t0, e0),
                        1: (t1, e1),
                        2: (t2, e2),
                    }

        all_supported = [p for p in DEFAULT_PROVIDER_PRIORITY if p in AVAILABLE_MODELS] + [
            p for p in AVAILABLE_MODELS if p not in DEFAULT_PROVIDER_PRIORITY
        ]

        if provider in ("dynamic", "auto") or base_spec in ("dynamic", "auto"):
            specs = []
            for prov in all_supported:
                if has_provider_credentials(prov) is True:
                    specs.extend([f"{prov}:{m[0]}" for m in AVAILABLE_MODELS.get(prov, [])])
        else:
            resolved_provider = self._resolve_provider(provider, base_spec)
            models = AVAILABLE_MODELS.get(resolved_provider, [])
            specs = [f"{resolved_provider}:{m[0]}" for m in models]

        if not specs and base_spec:
            specs = [normalize_model_spec(base_spec)]
        elif not specs:
            specs = ["google_genai:gemini-2.5-flash"]

        pro_tokens = {"pro", "opus", "o3", "astra", "sol", "reasoner", "r1"}
        fast_tokens = {"flash", "mini", "haiku", "lite", "micro", "scout", "small"}

        def _spec_tokens(s: str) -> set[str]:
            name = s.split(":", 1)[1] if ":" in s else s
            tokens = set(re.split(r"[-_:/]", name.lower()))
            sub_tokens: set[str] = set()
            for tok in tokens:
                if "." in tok:
                    sub_tokens.update(tok.split("."))
            return tokens | sub_tokens

        tier2_pro_candidates: list[str] = []
        tier2_fallback_candidates: list[str] = []
        tier1_candidates: list[str] = []
        tier0_candidates: list[str] = []

        for spec in specs:
            tokens = _spec_tokens(spec)
            is_pro = bool(tokens & pro_tokens)
            is_fast = bool(tokens & fast_tokens)
            prof_entry = get_model_profile(spec)
            prof = prof_entry["profile"] if prof_entry else {}
            has_reasoning = prof.get("reasoning_output", False) or bool(supported_efforts_for_model(spec))

            if is_pro:
                tier2_pro_candidates.append(spec)
            elif has_reasoning:
                tier2_fallback_candidates.append(spec)

            if (is_fast and has_reasoning) or (not is_pro and not is_fast):
                tier1_candidates.append(spec)

            if is_fast:
                tier0_candidates.append(spec)

        normalized_base = normalize_model_spec(base_spec) if base_spec and base_spec not in ("dynamic", "auto", "default") else None

        # Tier 2 (Frontier Deep Reasoning): prioritize true Pro/Flagship models with reasoning effort
        tier2_reasoning_pro = [s for s in tier2_pro_candidates if supported_efforts_for_model(s)]
        if normalized_base and normalized_base in tier2_reasoning_pro:
            t2 = normalized_base
        elif tier2_reasoning_pro:
            t2 = tier2_reasoning_pro[0]
        elif normalized_base and normalized_base in tier2_pro_candidates:
            t2 = normalized_base
        elif tier2_pro_candidates:
            t2 = tier2_pro_candidates[0]
        elif normalized_base and normalized_base in tier2_fallback_candidates:
            t2 = normalized_base
        elif tier2_fallback_candidates:
            t2 = tier2_fallback_candidates[0]
        else:
            t2 = normalized_base or specs[0]

        # Tier 1 (Standard Execution): prefer fast reasoning model distinct from T2
        t1_options = [s for s in tier1_candidates if s != t2]
        if normalized_base and normalized_base in t1_options:
            t1 = normalized_base
        elif t1_options:
            t1 = t1_options[0]
        else:
            t1 = tier1_candidates[0] if tier1_candidates else specs[0]

        # Tier 0 (Fast Reflex): prefer fast model distinct from T1 and T2
        t0_options = [s for s in tier0_candidates if s != t1 and s != t2]
        if normalized_base and normalized_base in t0_options:
            t0 = normalized_base
        elif t0_options:
            fast_priority_tokens = {"3.6", "lite", "flash-lite", "micro", "nano", "mini", "haiku"}
            t0 = next((s for s in t0_options if bool(_spec_tokens(s) & fast_priority_tokens)), t0_options[0])
        else:
            t0 = tier0_candidates[0] if tier0_candidates else specs[-1]

        # Resolve reasoning efforts dynamically from model capabilities
        # Tier 0: Disable reasoning to eliminate latency and cost on reflex queries
        e0 = "off"

        # Tier 1: Standard reasoning (profile default, medium, or low)
        t1_efforts = supported_efforts_for_model(t1)
        if "medium" in t1_efforts:
            e1 = "medium"
        elif "low" in t1_efforts:
            e1 = "low"
        else:
            e1 = default_effort_for_model(t1) or "off"

        # Tier 2: Deep frontier reasoning (prefer max > xhigh > high > medium)
        t2_efforts = supported_efforts_for_model(t2)
        if "max" in t2_efforts:
            e2 = "max"
        elif "xhigh" in t2_efforts:
            e2 = "xhigh"
        elif "high" in t2_efforts:
            e2 = "high"
        elif "medium" in t2_efforts:
            e2 = "medium"
        else:
            e2 = default_effort_for_model(t2) or "off"

        return {
            0: (t0, e0),
            1: (t1, e1),
            2: (t2, e2),
        }

    def get_model_for_tier(
        self,
        tier: int,
        provider: str | None = None,
        base_spec: str | None = None,
        requested_effort: str | None = None,
    ) -> tuple[BaseChatModel, str, str, dict[str, Any]]:
        """Instantiate or retrieve a cached model instance for the target tier.

        Returns:
            tuple[BaseChatModel, selected_spec, effort_level, model_settings]
        """
        tiers = self.discover_tiers(provider=provider, base_spec=base_spec)
        selected_spec, default_effort = tiers.get(tier, tiers.get(1, (base_spec or "google_genai:gemini-2.5-flash", "off")))
        effort = requested_effort or default_effort
        if tier == 0 or effort in ("off", "none", "clear", "0", "reset") or not supported_efforts_for_model(selected_spec):
            model_settings = {}
        else:
            model_settings = with_effort_model_params(selected_spec, {}, effort)

        cache_key = f"{selected_spec}::effort={effort}"
        with self._lock:
            cached = self._instances.get(cache_key)
            if cached is None:
                try:
                    res = create_model(selected_spec, extra_kwargs=model_settings or None)
                    self._instances[cache_key] = res
                    cached = res
                except Exception as exc:
                    logger.warning(
                        "Failed to instantiate model %s with effort %s: %s; falling back to base model",
                        selected_spec,
                        effort,
                        exc,
                    )
                    cached = create_model(base_spec)
                    selected_spec = cached.model_name
                    effort = "off"
                    model_settings = {}

        return cached.model, selected_spec, effort, model_settings

    def get_model_choices(
        self,
        provider: str | None = None,
        base_spec: str | None = None,
        criteria: dict[str, str] | None = None,
    ) -> dict[str, ModelChoice]:
        """Build dictionary of ModelChoice instances for ModelRouterMiddleware.

        Resolves distinct provider models and reasoning settings from the pool
        and maps them to standard routing keys ('fast', 'standard', 'powerful').
        The candidate model spec and reasoning effort are explicitly embedded in
        the criteria so the candidate model names appear on the input payload sent to Jev.
        """
        criteria_map = criteria or DEFAULT_ROUTE_CRITERIA
        m0, spec0, eff0, _ = self.get_model_for_tier(0, provider=provider, base_spec=base_spec)
        m1, spec1, eff1, _ = self.get_model_for_tier(1, provider=provider, base_spec=base_spec)
        m2, spec2, eff2, _ = self.get_model_for_tier(2, provider=provider, base_spec=base_spec)

        def _format_criteria(spec: str, eff: str, base_desc: str) -> str:
            if base_desc.startswith("Model:"):
                return base_desc
            return f"Model: {spec} (effort: {eff}). {base_desc}"

        return {
            "fast": ModelChoice(
                model=m0,
                criteria=_format_criteria(spec0, eff0, criteria_map.get("fast", DEFAULT_ROUTE_CRITERIA["fast"])),
            ),
            "standard": ModelChoice(
                model=m1,
                criteria=_format_criteria(spec1, eff1, criteria_map.get("standard", DEFAULT_ROUTE_CRITERIA["standard"])),
            ),
            "powerful": ModelChoice(
                model=m2,
                criteria=_format_criteria(spec2, eff2, criteria_map.get("powerful", DEFAULT_ROUTE_CRITERIA["powerful"])),
            ),
        }


_GLOBAL_POOL_MANAGER: DynamicModelPoolManager | None = None


def get_model_pool_manager() -> DynamicModelPoolManager:
    """Return singleton instance of DynamicModelPoolManager."""
    global _GLOBAL_POOL_MANAGER
    if _GLOBAL_POOL_MANAGER is None:
        _GLOBAL_POOL_MANAGER = DynamicModelPoolManager()
    return _GLOBAL_POOL_MANAGER


__all__ = [
    "DEFAULT_ROUTE_CRITERIA",
    "DynamicModelPoolManager",
    "ModelChoice",
    "ROUTER_INSTRUCTIONS",
    "ROUTE_TO_TIER_MAP",
    "TIER_TO_ROUTE_MAP",
    "TierResolution",
    "get_model_pool_manager",
]
