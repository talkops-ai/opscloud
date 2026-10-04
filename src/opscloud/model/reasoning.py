"""Reasoning effort support and provider-native thinking configuration.

Translates standard `reasoning_effort` ('low', 'medium', 'high', 'max')
into provider-native request shapes:
- Google GenAI (Gemini): `include_thoughts=True`, `thinking_level="HIGH"`, `thinking_budget=8192`
- Anthropic (Claude): `thinking={"type": "enabled", "budget_tokens": 8192}`
- OpenAI: `reasoning={"effort": "high"}`
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opscloud.model.config import (
    MODEL_PROFILES,
    ModelSpec,
    detect_provider,
    get_model_profile as _config_get_model_profile,
    get_model_profiles,
    normalize_model_spec,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

SUPPORTED_EFFORT_LEVELS = ("none", "off", "minimal", "low", "medium", "high", "xhigh", "max")


def _resolve_provider(model_spec: str | None) -> str:
    """Resolve provider name from model spec string, supporting bare or prefixed formats."""
    if not model_spec:
        return ""
    parsed = ModelSpec.try_parse(model_spec)
    if parsed is not None and parsed.provider:
        return parsed.provider
    return detect_provider(model_spec) or ""


def get_model_profile(spec: str) -> dict[str, Any] | None:
    """Return model profile for spec."""
    if not spec:
        return None
    entry = _config_get_model_profile(spec)
    if entry and entry.get("profile"):
        return dict(entry["profile"])
    return None


def _model_profile(
    model_spec: str | None, *, cli_override: dict[str, Any] | None = None
) -> Mapping[str, Any] | None:
    """Return the reasoning-capable profile for `model_spec`."""
    if not model_spec:
        return None
    entry = get_model_profiles(cli_override=cli_override).get(model_spec)
    if entry is None:
        entry = _config_get_model_profile(model_spec)
    profile = cli_override if entry is None else entry.get("profile")
    if profile is None:
        return None
    if not isinstance(profile, Mapping):
        logger.warning(
            "Ignoring model profile for %s with unexpected type %s",
            model_spec,
            type(profile).__name__,
        )
        return None
    reasoning_output = profile.get("reasoning_output")
    if reasoning_output is not None and not isinstance(reasoning_output, bool):
        logger.warning(
            "Ignoring reasoning_output for %s with unexpected type %s",
            model_spec,
            type(reasoning_output).__name__,
        )
        return None
    if reasoning_output is not True:
        return None
    return profile


def supported_efforts_for_model(
    model_spec: str | None, *, cli_override: dict[str, Any] | None = None
) -> tuple[str, ...]:
    """Return the ordered reasoning effort levels supported by `model_spec`."""
    profile = _model_profile(model_spec, cli_override=cli_override)
    if profile is None or "reasoning_effort_levels" not in profile:
        return ()
    levels = profile["reasoning_effort_levels"]
    if not isinstance(levels, list):
        return ()
    for level in levels:
        if not isinstance(level, str):
            return ()
    return tuple(levels)


def default_effort_for_model(
    model_spec: str | None, *, cli_override: dict[str, Any] | None = None
) -> str | None:
    """Return the profile's reasoning effort default independently of its levels."""
    profile = _model_profile(model_spec, cli_override=cli_override)
    if profile is None or "reasoning_effort_default" not in profile:
        return None
    default = profile.get("reasoning_effort_default")
    if default is not None:
        return str(default)
    return None


def is_effort_supported_for_model(
    model_spec: str, effort: str, *, cli_override: dict[str, Any] | None = None
) -> bool:
    """Check whether a given reasoning effort level is supported by the specified model."""
    if not model_spec:
        return False
    supported = supported_efforts_for_model(model_spec, cli_override=cli_override)
    return any(s.casefold() == effort.casefold() for s in supported)


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def _effort_paths(provider: str) -> tuple[tuple[str, ...], ...]:
    if provider in ("openai", "openai_codex", "azure_openai"):
        return (("reasoning", "effort"), ("reasoning_effort",))
    if provider == "anthropic":
        return (("effort",), ("reasoning_effort",), ("output_config", "effort"), ("thinking",))
    if provider in ("google_genai", "google", "google_vertexai"):
        return (("thinking_level",), ("reasoning_effort",), ("thinking_config", "thinking_level"), ("include_thoughts",))
    return (("reasoning_effort",),)


def _path_is_present(model_params: Mapping[str, Any], path: tuple[str, ...]) -> bool:
    if len(path) == 1:
        return path[0] in model_params
    nested = model_params.get(path[0])
    return isinstance(nested, Mapping) and path[1] in nested


def has_explicit_effort_model_params(model_spec: str | None, model_params: dict[str, Any] | None) -> bool:
    """Return whether canonical or native effort parameters are present."""
    if not model_spec or not model_params:
        return False
    provider = _resolve_provider(model_spec)
    return any(_path_is_present(model_params, path) for path in _effort_paths(provider))


def current_effort_from_model_params(model_spec: str | None, model_params: dict[str, Any] | None) -> str | None:
    """Read canonical or native effort settings using integration precedence."""
    if not model_spec or not model_params:
        return None
    provider = _resolve_provider(model_spec)

    if provider in {"openai", "openai_codex", "azure_openai"}:
        reasoning = model_params.get("reasoning")
        if isinstance(reasoning, Mapping) and "effort" in reasoning:
            return _str_or_none(reasoning["effort"])
    elif provider == "anthropic":
        if "effort" in model_params and model_params["effort"] is not None:
            return _str_or_none(model_params["effort"])
        output_config = model_params.get("output_config")
        if isinstance(output_config, Mapping) and "effort" in output_config:
            return _str_or_none(output_config["effort"])
        thinking = model_params.get("thinking")
        if isinstance(thinking, Mapping):
            budget = thinking.get("budget_tokens")
            if budget:
                budget_map = {1024: "low", 2048: "low", 4096: "medium", 8192: "high", 16384: "max"}
                if budget in budget_map:
                    return budget_map[budget]
    elif provider in {"google_genai", "google", "google_vertexai"}:
        if model_params.get("thinking_budget") == 0 or model_params.get("include_thoughts") is False:
            return "off"
        if "reasoning_effort" in model_params and model_params["reasoning_effort"] is not None:
            return _str_or_none(model_params["reasoning_effort"])
        if "thinking_level" in model_params and model_params["thinking_level"] is not None:
            return _str_or_none(model_params["thinking_level"])
        thinking_config = model_params.get("thinking_config")
        if isinstance(thinking_config, Mapping) and "thinking_level" in thinking_config:
            return _str_or_none(thinking_config["thinking_level"])
    elif provider == "fireworks":
        mk = model_params.get("model_kwargs")
        if isinstance(mk, Mapping) and "reasoning_effort" in mk:
            return _str_or_none(mk["reasoning_effort"])
    elif provider == "xai":
        eb = model_params.get("extra_body")
        if isinstance(eb, Mapping) and "reasoning_effort" in eb:
            return _str_or_none(eb["reasoning_effort"])

    return _str_or_none(model_params.get("reasoning_effort"))


def _remove_nested_key(params: dict[str, Any], container: str, key: str) -> None:
    nested = params.get(container)
    if not isinstance(nested, Mapping):
        return
    remaining = dict(nested)
    remaining.pop(key, None)
    if remaining:
        params[container] = remaining
    else:
        params.pop(container, None)


def without_effort_model_params(model_spec: str | None, existing: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove canonical and native effort settings without changing siblings."""
    if not existing:
        return None
    cleaned = dict(existing)
    cleaned.pop("reasoning_effort", None)

    provider = _resolve_provider(model_spec)
    if provider in ("openai", "openai_codex", "azure_openai"):
        _remove_nested_key(cleaned, "reasoning", "effort")
    elif provider == "anthropic":
        cleaned.pop("effort", None)
        _remove_nested_key(cleaned, "output_config", "effort")
        cleaned.pop("thinking", None)
    elif provider in ("google_genai", "google", "google_vertexai"):
        cleaned.pop("thinking_level", None)
        cleaned.pop("thinking_budget", None)
        cleaned.pop("include_thoughts", None)
        _remove_nested_key(cleaned, "thinking_config", "thinking_level")
        _remove_nested_key(cleaned, "thinking_config", "thinking_budget")
    elif provider == "fireworks":
        _remove_nested_key(cleaned, "model_kwargs", "reasoning_effort")
    elif provider == "xai":
        _remove_nested_key(cleaned, "extra_body", "reasoning_effort")
    return cleaned or None


def with_effort_model_params(model_spec: str | None, existing: dict[str, Any] | None, effort: str) -> dict[str, Any]:
    """Replace existing effort settings with the standard flat parameter or provider-native parameter.

    If effort is off/none/clear/0/reset, or if the model does not support reasoning effort,
    returns a cleaned parameter dictionary with all reasoning and thinking keys stripped.
    """
    updated = without_effort_model_params(model_spec, existing) or {}
    eff_lower = effort.lower()

    # Disabling / clearing reasoning
    if eff_lower in ("off", "none", "clear", "0", "reset", ""):
        return updated

    # If model is specified and does not support this effort level, do not inject effort
    if model_spec and not is_effort_supported_for_model(model_spec, effort):
        return updated

    provider = _resolve_provider(model_spec)
    if provider in ("google_genai", "google", "google_vertexai"):
        valid_level = eff_lower if eff_lower in ("minimal", "low", "medium", "high") else "medium"
        updated["reasoning_effort"] = valid_level
        updated["include_thoughts"] = True
    elif provider == "anthropic":
        updated["reasoning_effort"] = effort
        budget = {
            "low": 1024,
            "medium": 4096,
            "high": 8192,
            "max": 16384,
        }.get(eff_lower, 4096)
        updated["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif provider in ("openai", "openai_codex", "azure_openai"):
        updated["reasoning_effort"] = effort
    elif provider == "fireworks":
        mk = dict(updated.get("model_kwargs", {}))
        mk["reasoning_effort"] = effort
        updated["model_kwargs"] = mk
    elif provider == "xai":
        eb = dict(updated.get("extra_body", {}))
        eb["reasoning_effort"] = effort
        updated["extra_body"] = eb
    else:
        updated["reasoning_effort"] = effort

    return updated
