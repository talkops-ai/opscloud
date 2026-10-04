"""Model configuration structures, catalogs, and helpers for LLM providers.

Matches the OpsCode model system:
- Supports all providers: Google GenAI, Anthropic, OpenAI, OpenRouter, Groq, DeepSeek, Ollama, etc.
- Detailed capabilities: reasoning_output, tool_calling, max_tokens, etc.
- Dynamic spec resolution: ``provider:model`` format
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import importlib.util
import json
import os
from pathlib import Path
import socket
from types import MappingProxyType
from typing import Any, TypedDict, cast
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from opscloud.config.settings import get_settings, resolve_env_var
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

PROVIDER_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "baseten": "BASETEN_API_KEY",
    "bedrock": "AWS_ACCESS_KEY_ID",
    "cohere": "COHERE_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "google_vertexai": "GOOGLE_CLOUD_PROJECT",
    "groq": "GROQ_API_KEY",
    "huggingface": "HUGGINGFACEHUB_API_TOKEN",
    "ibm": "WATSONX_APIKEY",
    "litellm": "LITELLM_API_KEY",
    "meta": "MODEL_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "perplexity": "PPLX_API_KEY",
    "together": "TOGETHER_API_KEY",
    "typesafe": "TYPESAFE_API_KEY",
    "xai": "XAI_API_KEY",
    "openai_codex": "OPENAI_API_KEY",
    "google_anthropic_vertex": "GOOGLE_CLOUD_PROJECT",
}

CODEX_PROVIDER = "openai_codex"
CODEX_MODELS: frozenset[str] = frozenset(
    {
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "gpt-5.2",
    }
)

PROVIDER_BASE_URL_ENV: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL"),
    "azure_openai": ("AZURE_OPENAI_ENDPOINT",),
    "baseten": ("BASETEN_BASE_URL", "BASETEN_API_BASE"),
    "cohere": ("CO_API_URL",),
    "deepseek": ("DEEPSEEK_API_BASE",),
    "fireworks": ("FIREWORKS_BASE_URL", "FIREWORKS_API_BASE"),
    "google_genai": ("GOOGLE_GEMINI_BASE_URL",),
    "groq": ("GROQ_BASE_URL", "GROQ_API_BASE"),
    "huggingface": ("HF_INFERENCE_ENDPOINT",),
    "ibm": ("WATSONX_URL",),
    "meta": ("MODEL_API_BASE",),
    "mistralai": ("MISTRAL_BASE_URL",),
    "nvidia": ("NVIDIA_BASE_URL",),
    "openai": ("OPENAI_BASE_URL", "OPENAI_API_BASE"),
    "openai_codex": ("OPENAI_BASE_URL", "OPENAI_API_BASE"),
    "openrouter": ("OPENROUTER_API_BASE",),
    "perplexity": ("PERPLEXITY_BASE_URL",),
    "together": ("TOGETHER_API_BASE",),
    "typesafe": ("TYPESAFE_BASE_URL", "TYPESAFE_API_BASE"),
    "xai": ("XAI_API_BASE",),
}

IMPLICIT_AUTH_PROVIDERS: set[str] = {"google_vertexai", "google_anthropic_vertex"}
NO_AUTH_REQUIRED_PROVIDERS: set[str] = {"ollama", "dynamic"}
OPTIONAL_AUTH_ENV: dict[str, str] = {"ollama": "OLLAMA_API_KEY"}


class ProviderAuthState(StrEnum):
    """Authentication state for a single LLM provider credential."""

    CONFIGURED = "configured"
    MISSING = "missing"
    IMPLICIT = "implicit"
    NOT_REQUIRED = "not_required"
    MANAGED = "managed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderAuthStatus:
    """Aggregate authentication status across all configured providers."""

    state: str
    provider: str
    env_var: str | None = None
    detail: str = ""

    def as_legacy_bool(self) -> bool | None:
        """Convert the auth state into a legacy boolean representation.

        Returns:
            True if authenticated or managed, False if missing, or None if unknown.
        """
        if self.state in (
            ProviderAuthState.CONFIGURED,
            ProviderAuthState.IMPLICIT,
            ProviderAuthState.NOT_REQUIRED,
            ProviderAuthState.MANAGED,
        ):
            return True
        if self.state == ProviderAuthState.MISSING:
            return False
        return None


@dataclass(frozen=True)
class ModelSpec:
    """Parsed model specification with provider, model name, and parameters."""

    provider: str
    model: str

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("Provider cannot be empty")
        if not self.model:
            raise ValueError("Model cannot be empty")

    @classmethod
    def parse(cls, spec: str) -> ModelSpec:
        """Parse a model specification string into a ModelSpec instance.

        Args:
            spec: String in 'provider:model' format.

        Returns:
            ModelSpec instance.

        Raises:
            ValueError: If spec does not contain ':' or components are empty.
        """
        if ":" not in spec:
            raise ValueError(f"Invalid spec '{spec}': must be provider:model format")
        provider, model = spec.split(":", 1)
        return cls(provider=provider.strip(), model=model.strip())

    @classmethod
    def try_parse(cls, spec: str) -> ModelSpec | None:
        """Attempt to parse a model specification string without raising an exception.

        Args:
            spec: String in 'provider:model' format.

        Returns:
            ModelSpec instance if valid, None otherwise.
        """
        try:
            return cls.parse(spec)
        except ValueError:
            return None

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


class ModelProfile(TypedDict, total=False):
    """Capability profile for a specific model (context window, features, pricing)."""

    name: str
    max_input_tokens: int
    max_output_tokens: int
    text_inputs: bool
    image_inputs: bool
    audio_inputs: bool
    pdf_inputs: bool
    video_inputs: bool
    reasoning_output: bool
    reasoning_effort_levels: list[str]
    reasoning_effort_default: str
    tool_calling: bool
    structured_output: bool
    status: str | None


class ModelProfileEntry(TypedDict):
    """Single entry in the model profile registry mapping spec to capabilities."""

    profile: ModelProfile
    overridden_keys: set[str]


class ProviderConfig(TypedDict, total=False):
    """Configuration for an LLM provider (API keys, base URLs, overrides)."""

    enabled: bool
    models: list[str]
    api_key_env: str
    base_url: str
    base_url_env: str
    class_path: str
    params: dict[str, Any]
    profile: dict[str, Any]
    display_name: str


@dataclass(frozen=True)
class ModelConfig:
    """Complete model configuration including all providers and their profiles."""

    default_model: str | None = None
    recent_model: str | None = None
    providers: Mapping[str, ProviderConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.providers, MappingProxyType):
            object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))

    @classmethod
    def load(cls) -> ModelConfig:
        """Load the active model configuration from application settings and config.toml.

        Returns:
            ModelConfig initialized with default and recent model selections.
        """
        settings = get_settings()
        explicit = getattr(settings, "model_name", None) or getattr(settings, "model", None)

        from opscloud.config.toml_config import load_default_model, load_recent_model
        default = explicit or load_default_model()
        recent = explicit or load_recent_model() or default
        return cls(default_model=default, recent_model=recent)

    def is_provider_enabled(self, provider: str) -> bool:
        """Check whether a model provider is enabled in configuration.

        Args:
            provider: Provider name (e.g. 'openai', 'anthropic').

        Returns:
            True if enabled; False otherwise.
        """
        provider_config = self.providers.get(provider)
        if provider_config is None:
            return True
        return provider_config.get("enabled", True)

    def get_kwargs(self, provider: str, *, model_name: str | None = None) -> dict[str, Any]:
        """Retrieve model initialization keyword arguments for a provider.

        Args:
            provider: Provider identifier.
            model_name: Optional specific model name for model-specific overrides.

        Returns:
            Dictionary of provider kwargs.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return {}
        params = dict(provider_config.get("params", {}))
        if model_name and isinstance(params.get(model_name), dict):
            model_params = params.pop(model_name)
            params = {k: v for k, v in params.items() if not isinstance(v, dict)}
            params.update(model_params)
        else:
            params = {k: v for k, v in params.items() if not isinstance(v, dict)}
        return params

    def get_base_url(self, provider: str) -> str | None:
        """Resolve the effective base URL for a provider from env or config.

        Args:
            provider: Provider identifier.

        Returns:
            Resolved base URL string or None.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return None
        base_url_env = provider_config.get("base_url_env")
        if base_url_env:
            val = resolve_env_var(base_url_env)
            if val:
                return val
        return provider_config.get("base_url")

    def get_base_url_env(self, provider: str) -> str | None:
        """Get the configured base URL environment variable name for a provider.

        Args:
            provider: Provider identifier.

        Returns:
            Environment variable name or None.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return None
        return provider_config.get("base_url_env")

    def get_api_key_env(self, provider: str) -> str | None:
        """Get the configured API key environment variable name for a provider.

        Args:
            provider: Provider identifier.

        Returns:
            Environment variable name or None.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return None
        return provider_config.get("api_key_env")

    def get_class_path(self, provider: str) -> str | None:
        """Get the custom class path configured for a provider's model wrapper.

        Args:
            provider: Provider identifier.

        Returns:
            Python import path string or None.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return None
        return provider_config.get("class_path")

    def get_profile_overrides(self, provider: str, *, model_name: str | None = None) -> dict[str, Any]:
        """Retrieve profile capability overrides for a provider or model.

        Args:
            provider: Provider identifier.
            model_name: Optional specific model name.

        Returns:
            Dictionary of profile overrides.
        """
        provider_config = self.providers.get(provider)
        if not provider_config:
            return {}
        profile = dict(provider_config.get("profile", {}))
        if model_name and isinstance(profile.get(model_name), dict):
            model_profile = profile.pop(model_name)
            profile = {k: v for k, v in profile.items() if not isinstance(v, dict)}
            profile.update(model_profile)
        else:
            profile = {k: v for k, v in profile.items() if not isinstance(v, dict)}
        return profile


def get_credential_env_var(provider: str) -> str | None:
    """Return the primary environment variable name for a provider's API key.

    Args:
        provider: Provider identifier.

    Returns:
        Environment variable name or None.
    """
    return PROVIDER_API_KEY_ENV.get(provider)


def get_base_url_env_vars(provider: str) -> tuple[str, ...]:
    """Return candidate environment variable names for a provider's base URL.

    Args:
        provider: Provider identifier.

    Returns:
        Tuple of candidate environment variable names.
    """
    return PROVIDER_BASE_URL_ENV.get(provider, ())


PROVIDER_SETTINGS_FIELD_MAP: dict[str, str] = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "google_genai": "google_api_key",
    "google": "google_api_key",
    "groq": "groq_api_key",
    "deepseek": "deepseek_api_key",
    "openrouter": "openrouter_api_key",
    "mistralai": "mistral_api_key",
    "mistral": "mistral_api_key",
    "fireworks": "fireworks_api_key",
    "together": "together_api_key",
    "xai": "xai_api_key",
    "cohere": "cohere_api_key",
    "perplexity": "perplexity_api_key",
    "nvidia": "nvidia_api_key",
    "huggingface": "huggingface_api_key",
    "bedrock": "aws_access_key_id",
    "azure_openai": "azure_openai_api_key",
    "baseten": "baseten_api_key",
    "google_vertexai": "google_cloud_project",
    "ibm": "watsonx_apikey",
    "litellm": "litellm_api_key",
    "meta": "model_api_key",
    "typesafe": "typesafe_api_key",
}

PROVIDER_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "google_genai": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "mistralai": ("MISTRAL_API_KEY", "MISTRALAI_API_KEY"),
    "mistral": ("MISTRAL_API_KEY", "MISTRALAI_API_KEY"),
    "fireworks": ("FIREWORKS_API_KEY", "FIREWORKS_AI_API_KEY"),
    "perplexity": ("PPLX_API_KEY", "PERPLEXITY_API_KEY"),
    "typesafe": ("TYPESAFE_API_KEY", "JEV_API_KEY"),
}


def get_provider_auth_status(provider: str) -> ProviderAuthStatus:
    """Inspect and resolve authentication status for a provider.

    Args:
        provider: Provider identifier.

    Returns:
        ProviderAuthStatus indicating current configuration state and credentials.
    """
    if provider in NO_AUTH_REQUIRED_PROVIDERS:
        return ProviderAuthStatus(state=ProviderAuthState.NOT_REQUIRED, provider=provider, detail="local provider")

    if provider in IMPLICIT_AUTH_PROVIDERS:
        return ProviderAuthStatus(state=ProviderAuthState.IMPLICIT, provider=provider, detail="implicit auth")

    settings = get_settings()

    if provider in ("bedrock", "bedrock_converse", "anthropic_bedrock"):
        from opscloud.config.aws import (
            AWS_CREDENTIAL_ENV_SOURCES,
            get_boto3_session,
            resolve_env_kwargs,
            validate_aws_credentials,
        )

        is_valid, err = validate_aws_credentials()
        if not is_valid:
            return ProviderAuthStatus(
                state=ProviderAuthState.MISSING,
                provider=provider,
                env_var="AWS_SECRET_ACCESS_KEY",
                detail=err or "Incomplete AWS credentials",
            )
        env_lookup = lambda k: os.environ.get(f"OPSCLOUD_{k}") or os.environ.get(k)
        resolved = resolve_env_kwargs(AWS_CREDENTIAL_ENV_SOURCES, lookup=env_lookup)
        has_keys = bool(
            (resolved.get("aws_access_key_id") and resolved.get("aws_secret_access_key"))
            or (getattr(settings, "aws_access_key_id", None) and getattr(settings, "aws_secret_access_key", None))
        )
        has_profile = bool(
            resolved.get("profile_name")
            or (getattr(settings, "aws_profile", None) and getattr(settings, "aws_profile") != "default")
        )
        if has_keys or has_profile:
            return ProviderAuthStatus(
                state=ProviderAuthState.CONFIGURED,
                provider=provider,
                env_var="AWS_ACCESS_KEY_ID",
                detail="AWS credentials configured",
            )
        try:
            sess = get_boto3_session()
            if sess.get_credentials() is not None:
                return ProviderAuthStatus(
                    state=ProviderAuthState.CONFIGURED,
                    provider=provider,
                    detail="AWS ambient credentials active",
                )
        except Exception:
            pass
        return ProviderAuthStatus(
            state=ProviderAuthState.MISSING,
            provider=provider,
            env_var="AWS_ACCESS_KEY_ID",
            detail="AWS credentials not found. Configure AWS_PROFILE or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY",
        )

    env_var = get_credential_env_var(provider)

    val = None
    if env_var:
        val = resolve_env_var(env_var)

    if not val and provider in PROVIDER_KEY_ALIASES:
        for alias in PROVIDER_KEY_ALIASES[provider]:
            val = resolve_env_var(alias)
            if val:
                break

    if not val:
        field = PROVIDER_SETTINGS_FIELD_MAP.get(provider)
        if field and hasattr(settings, field):
            val = getattr(settings, field)

    if val:
        return ProviderAuthStatus(state=ProviderAuthState.CONFIGURED, provider=provider, env_var=env_var)

    optional_env = OPTIONAL_AUTH_ENV.get(provider)
    if optional_env and resolve_env_var(optional_env):
        return ProviderAuthStatus(state=ProviderAuthState.CONFIGURED, provider=provider, env_var=optional_env)

    if not env_var:
        return ProviderAuthStatus(state=ProviderAuthState.UNKNOWN, provider=provider, detail="credentials unknown")

    return ProviderAuthStatus(
        state=ProviderAuthState.MISSING, provider=provider, env_var=env_var, detail=f"{env_var} is not set"
    )


def has_provider_credentials(provider: str) -> bool | None:
    """Determine whether credentials are available for a given provider.

    Args:
        provider: Provider identifier.

    Returns:
        True if credentials exist, False if missing, None if indeterminate.
    """
    return get_provider_auth_status(provider).as_legacy_bool()


def apply_stored_credentials(provider: str | None = None) -> bool:
    """Bridge credentials into process env vars for LangChain model factories across all providers."""
    if not provider:
        try:
            from opscloud.config.toml_config import apply_stored_credentials as _toml_apply
            _toml_apply()
            return True
        except Exception:
            return False

    settings = get_settings()
    env_var = get_credential_env_var(provider)
    applied = False

    val = None
    if env_var:
        val = resolve_env_var(env_var)

    if not val and provider in PROVIDER_KEY_ALIASES:
        for alias in PROVIDER_KEY_ALIASES[provider]:
            val = resolve_env_var(alias)
            if val:
                break

    if not val:
        field = PROVIDER_SETTINGS_FIELD_MAP.get(provider)
        if field and hasattr(settings, field):
            val = getattr(settings, field)

    if val:
        if env_var:
            os.environ[env_var] = str(val)
        if provider in PROVIDER_KEY_ALIASES:
            for alias in PROVIDER_KEY_ALIASES[provider]:
                os.environ[alias] = str(val)
        applied = True

    if provider in ("google_genai", "google"):
        use_vertex = resolve_env_var("GOOGLE_GENAI_USE_VERTEXAI") or str(
            getattr(settings, "google_genai_use_vertexai", False)
        )
        if use_vertex.strip().lower() in ("true", "1", "yes"):
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
            applied = True
        proj = resolve_env_var("GOOGLE_CLOUD_PROJECT") or getattr(settings, "google_cloud_project", None)
        if proj:
            os.environ["GOOGLE_CLOUD_PROJECT"] = str(proj)
        loc = resolve_env_var("GOOGLE_CLOUD_LOCATION") or getattr(settings, "google_cloud_location", None)
        if loc:
            os.environ["GOOGLE_CLOUD_LOCATION"] = str(loc)

    return applied


# ── Model Profiles & Catalog ─────────────────────────────

MODEL_PROFILES: dict[str, ModelProfile] = {
    # Anthropic
    "anthropic:claude-3-7-sonnet-20250219": {
        "name": "Claude 3.7 Sonnet (Thinking)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-3-7-sonnet": {
        "name": "Claude 3.7 Sonnet (Thinking)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-3-5-sonnet-20241022": {
        "name": "Claude 3.5 Sonnet",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-3-5-sonnet": {
        "name": "Claude 3.5 Sonnet",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-3-5-sonnet-latest": {
        "name": "Claude 3.5 Sonnet",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-3-5-haiku-20241022": {
        "name": "Claude 3.5 Haiku",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-3-5-haiku": {
        "name": "Claude 3.5 Haiku",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-3-5-haiku-latest": {
        "name": "Claude 3.5 Haiku",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-3-opus-20240229": {
        "name": "Claude 3 Opus",
        "max_input_tokens": 200_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-3-opus-latest": {
        "name": "Claude 3 Opus",
        "max_input_tokens": 200_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-sonnet-4-5": {
        "name": "Claude Sonnet 4.5",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-sonnet-4-6": {
        "name": "Claude Sonnet 4.6",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "high",
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-sonnet-5": {
        "name": "Claude Sonnet 5",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "xhigh", "max"],
        "reasoning_effort_default": "high",
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-opus-4-7": {
        "name": "Claude Opus 4.7",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "xhigh", "max"],
        "reasoning_effort_default": "high",
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-opus-4-8": {
        "name": "Claude Opus 4.8",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "xhigh", "max"],
        "reasoning_effort_default": "high",
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-opus-5": {
        "name": "Claude Opus 5",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "xhigh", "max"],
        "reasoning_effort_default": "high",
        "tool_calling": True,
        "structured_output": True,
    },
    "anthropic:claude-haiku-4-5": {
        "name": "Claude Haiku 4.5",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # OpenAI
    "openai:gpt-4o": {
        "name": "GPT-4o",
        "max_input_tokens": 128_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:gpt-4o-mini": {
        "name": "GPT-4o mini",
        "max_input_tokens": 128_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:chatgpt-4o-latest": {
        "name": "ChatGPT-4o Latest",
        "max_input_tokens": 128_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:o3-mini": {
        "name": "o3 Mini",
        "max_input_tokens": 200_000,
        "max_output_tokens": 100_000,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:o1": {
        "name": "o1",
        "max_input_tokens": 200_000,
        "max_output_tokens": 100_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:o1-mini": {
        "name": "o1 Mini",
        "max_input_tokens": 128_000,
        "max_output_tokens": 65_536,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:gpt-4-turbo": {
        "name": "GPT-4 Turbo",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:gpt-5.5": {
        "name": "GPT-5.5",
        "max_input_tokens": 400_000,
        "max_output_tokens": 128_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["none", "low", "medium", "high", "xhigh"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:gpt-5.5-pro": {
        "name": "GPT-5.5 Pro",
        "max_input_tokens": 400_000,
        "max_output_tokens": 128_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["none", "low", "medium", "high", "xhigh"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:gpt-5.4": {
        "name": "GPT-5.4",
        "max_input_tokens": 400_000,
        "max_output_tokens": 128_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["none", "low", "medium", "high", "xhigh"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openai:gpt-5.4-mini": {
        "name": "GPT-5.4 mini",
        "max_input_tokens": 400_000,
        "max_output_tokens": 128_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["none", "low", "medium", "high", "xhigh"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },

    # Azure OpenAI
    "azure_openai:gpt-4o": {
        "name": "GPT-4o (Azure)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "azure_openai:gpt-4o-mini": {
        "name": "GPT-4o Mini (Azure)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "azure_openai:o1": {
        "name": "o1 (Azure)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 100_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "azure_openai:o3-mini": {
        "name": "o3 Mini (Azure)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 100_000,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "azure_openai:gpt-5.4": {
        "name": "GPT-5.4 (Azure)",
        "max_input_tokens": 400_000,
        "max_output_tokens": 128_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["none", "low", "medium", "high", "xhigh"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },

    # Google GenAI
    "google_genai:gemini-2.5-flash-lite": {
        "name": "Gemini 2.5 Flash-Lite",
        "max_input_tokens": 1_048_576,
        "max_output_tokens": 65_536,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-2.5-flash": {
        "name": "Gemini 2.5 Flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-2.5-pro": {
        "name": "Gemini 2.5 Pro",
        "max_input_tokens": 2_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-2.0-flash": {
        "name": "Gemini 2.0 Flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-2.0-flash-thinking-exp-01-21": {
        "name": "Gemini 2.0 Flash Thinking",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 65_536,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-2.0-pro-exp-02-05": {
        "name": "Gemini 2.0 Pro Experimental",
        "max_input_tokens": 2_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-1.5-pro": {
        "name": "Gemini 1.5 Pro",
        "max_input_tokens": 2_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-1.5-flash": {
        "name": "Gemini 1.5 Flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-3.8-flash": {
        "name": "Gemini 3.8 Flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-3.7-flash": {
        "name": "Gemini 3.7 Flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-3.6-flash": {
        "name": "Gemini 3.6 Flash",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-3.5-flash": {
        "name": "Gemini 3.5 Flash",
        "max_input_tokens": 1_048_576,
        "max_output_tokens": 65_536,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["minimal", "low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "google_genai:gemini-3.5-flash-lite": {
        "name": "Gemini 3.5 Flash-Lite",
        "max_input_tokens": 1_048_576,
        "max_output_tokens": 65_536,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["minimal", "low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },

    # Google Vertex AI
    "google_vertexai:gemini-2.5-flash": {
        "name": "Gemini 2.5 Flash (Vertex)",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "tool_calling": True,
        "structured_output": True,
    },
    "google_vertexai:gemini-2.5-pro": {
        "name": "Gemini 2.5 Pro (Vertex)",
        "max_input_tokens": 2_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "google_vertexai:gemini-2.0-flash": {
        "name": "Gemini 2.0 Flash (Vertex)",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "google_vertexai:gemini-1.5-pro": {
        "name": "Gemini 1.5 Pro (Vertex)",
        "max_input_tokens": 2_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "google_vertexai:claude-3-7-sonnet@20250219": {
        "name": "Claude 3.7 Sonnet (Vertex)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "google_vertexai:claude-3-5-sonnet@20241022": {
        "name": "Claude 3.5 Sonnet (Vertex)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # AWS Bedrock
    "bedrock:anthropic.claude-3-7-sonnet-20250219-v1:0": {
        "name": "Claude 3.7 Sonnet (Bedrock)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:us.anthropic.claude-3-7-sonnet-20250219-v1:0": {
        "name": "Claude 3.7 Sonnet Cross-Region (Bedrock)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:anthropic.claude-3-5-sonnet-20241022-v2:0": {
        "name": "Claude 3.5 Sonnet v2 (Bedrock)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:us.anthropic.claude-3-5-sonnet-20241022-v2:0": {
        "name": "Claude 3.5 Sonnet v2 Cross-Region (Bedrock)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:anthropic.claude-3-5-haiku-20241022-v1:0": {
        "name": "Claude 3.5 Haiku (Bedrock)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:us.anthropic.claude-3-5-haiku-20241022-v1:0": {
        "name": "Claude 3.5 Haiku Cross-Region (Bedrock)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:amazon.nova-pro-v1:0": {
        "name": "Amazon Nova Pro",
        "max_input_tokens": 300_000,
        "max_output_tokens": 5_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:amazon.nova-lite-v1:0": {
        "name": "Amazon Nova Lite",
        "max_input_tokens": 300_000,
        "max_output_tokens": 5_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:amazon.nova-micro-v1:0": {
        "name": "Amazon Nova Micro",
        "max_input_tokens": 128_000,
        "max_output_tokens": 5_000,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:meta.llama3-3-70b-instruct-v1:0": {
        "name": "Llama 3.3 70B (Bedrock)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:meta.llama3-1-405b-instruct-v1:0": {
        "name": "Llama 3.1 405B (Bedrock)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:meta.llama3-1-70b-instruct-v1:0": {
        "name": "Llama 3.1 70B (Bedrock)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "bedrock:amazon.titan-text-premier-v1:0": {
        "name": "Amazon Titan Premier",
        "max_input_tokens": 32_000,
        "max_output_tokens": 3_072,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # DeepSeek
    "deepseek:deepseek-chat": {
        "name": "DeepSeek V3",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "deepseek:deepseek-reasoner": {
        "name": "DeepSeek R1",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },

    # OpenRouter
    "openrouter:anthropic/claude-3-7-sonnet": {
        "name": "Claude 3.7 Sonnet (OpenRouter)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 64_000,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high", "max"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:anthropic/claude-3-5-sonnet": {
        "name": "Claude 3.5 Sonnet (OpenRouter)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:openai/gpt-4o": {
        "name": "GPT-4o (OpenRouter)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:openai/o3-mini": {
        "name": "o3 Mini (OpenRouter)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 100_000,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:deepseek/deepseek-chat": {
        "name": "DeepSeek V3 (OpenRouter)",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:deepseek/deepseek-r1": {
        "name": "DeepSeek R1 (OpenRouter)",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:google/gemini-2.5-flash": {
        "name": "Gemini 2.5 Flash (OpenRouter)",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:meta-llama/llama-3.3-70b-instruct": {
        "name": "Llama 3.3 70B (OpenRouter)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "openrouter:qwen/qwen-2.5-coder-32b-instruct": {
        "name": "Qwen 2.5 Coder 32B (OpenRouter)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # Groq
    "groq:llama-3.3-70b-versatile": {
        "name": "Llama 3.3 70B (Groq)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 32_768,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "groq:deepseek-r1-distill-llama-70b": {
        "name": "DeepSeek R1 Distill 70B (Groq)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 32_768,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "groq:llama-3.1-8b-instant": {
        "name": "Llama 3.1 8B Instant (Groq)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "groq:mixtral-8x7b-32768": {
        "name": "Mixtral 8x7B (Groq)",
        "max_input_tokens": 32_768,
        "max_output_tokens": 32_768,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # Together AI
    "together:deepseek-ai/DeepSeek-V3": {
        "name": "DeepSeek V3 (Together)",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "together:deepseek-ai/DeepSeek-R1": {
        "name": "DeepSeek R1 (Together)",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "together:meta-llama/Llama-3.3-70B-Instruct-Turbo": {
        "name": "Llama 3.3 70B Turbo (Together)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "together:Qwen/Qwen2.5-72B-Instruct-Turbo": {
        "name": "Qwen 2.5 72B Turbo (Together)",
        "max_input_tokens": 32_768,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # Fireworks AI
    "fireworks:accounts/fireworks/models/deepseek-v3": {
        "name": "DeepSeek V3 (Fireworks)",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "fireworks:accounts/fireworks/models/deepseek-r1": {
        "name": "DeepSeek R1 (Fireworks)",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "fireworks:accounts/fireworks/models/llama-v3p3-70b-instruct": {
        "name": "Llama 3.3 70B (Fireworks)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "fireworks:accounts/fireworks/models/qwen2p5-coder-32b-instruct": {
        "name": "Qwen 2.5 Coder 32B (Fireworks)",
        "max_input_tokens": 32_768,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # Mistral AI
    "mistralai:mistral-large-latest": {
        "name": "Mistral Large",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "mistralai:codestral-latest": {
        "name": "Codestral",
        "max_input_tokens": 256_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "mistralai:pixtral-large-latest": {
        "name": "Pixtral Large",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "mistralai:mistral-small-latest": {
        "name": "Mistral Small",
        "max_input_tokens": 32_768,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "mistralai:ministral-8b-latest": {
        "name": "Ministral 8B",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # Cohere
    "cohere:command-r-plus-08-2024": {
        "name": "Command R+ (08-2024)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "cohere:command-r-plus": {
        "name": "Command R+",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "cohere:command-r-08-2024": {
        "name": "Command R (08-2024)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "cohere:command-r": {
        "name": "Command R",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # Perplexity
    "perplexity:sonar-pro": {
        "name": "Sonar Pro",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "perplexity:sonar": {
        "name": "Sonar",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "perplexity:sonar-reasoning": {
        "name": "Sonar Reasoning",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },

    # xAI
    "xai:grok-2": {
        "name": "Grok 2",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "xai:grok-2-vision-1212": {
        "name": "Grok 2 Vision",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "xai:grok-beta": {
        "name": "Grok Beta",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # Baseten
    "baseten:deepseek-ai/DeepSeek-R1": {
        "name": "DeepSeek R1 (Baseten)",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "baseten:meta-llama/Llama-3.3-70B-Instruct": {
        "name": "Llama 3.3 70B (Baseten)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # Hugging Face
    "huggingface:meta-llama/Llama-3.3-70B-Instruct": {
        "name": "Llama 3.3 70B (HuggingFace)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "huggingface:Qwen/Qwen2.5-Coder-32B-Instruct": {
        "name": "Qwen 2.5 Coder 32B (HuggingFace)",
        "max_input_tokens": 32_768,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # IBM watsonx
    "ibm:ibm/granite-3-8b-instruct": {
        "name": "Granite 3 8B (watsonx)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "ibm:ibm/granite-3-20b-instruct": {
        "name": "Granite 3 20B (watsonx)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "ibm:meta-llama/llama-3-3-70b-instruct": {
        "name": "Llama 3.3 70B (watsonx)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # NVIDIA NIM
    "nvidia:nvidia/llama-3.1-nemotron-70b-instruct": {
        "name": "Nemotron 70B (NVIDIA)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "nvidia:meta/llama-3.3-70b-instruct": {
        "name": "Llama 3.3 70B (NVIDIA)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # LiteLLM
    "litellm:openai/gpt-4o": {
        "name": "GPT-4o (LiteLLM)",
        "max_input_tokens": 128_000,
        "max_output_tokens": 16_384,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "litellm:anthropic/claude-3-5-sonnet": {
        "name": "Claude 3.5 Sonnet (LiteLLM)",
        "max_input_tokens": 200_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "image_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # Meta
    "meta:llama-3.3-70b-instruct": {
        "name": "Llama 3.3 70B",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "meta:llama-3.1-405b-instruct": {
        "name": "Llama 3.1 405B",
        "max_input_tokens": 128_000,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },

    # Ollama
    "ollama:llama3.3": {
        "name": "Llama 3.3",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "ollama:llama3.1": {
        "name": "Llama 3.1",
        "max_input_tokens": 128_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "ollama:qwen2.5-coder": {
        "name": "Qwen 2.5 Coder",
        "max_input_tokens": 32_768,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "ollama:deepseek-r1": {
        "name": "DeepSeek R1",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "ollama:deepseek-r1:14b": {
        "name": "DeepSeek R1 14B",
        "max_input_tokens": 64_000,
        "max_output_tokens": 8_192,
        "text_inputs": True,
        "reasoning_output": True,
        "reasoning_effort_levels": ["low", "medium", "high"],
        "reasoning_effort_default": "medium",
        "tool_calling": True,
        "structured_output": True,
    },
    "ollama:codellama": {
        "name": "Code Llama",
        "max_input_tokens": 16_384,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
    "ollama:mistral": {
        "name": "Mistral 7B",
        "max_input_tokens": 32_768,
        "max_output_tokens": 4_096,
        "text_inputs": True,
        "reasoning_output": False,
        "tool_calling": True,
        "structured_output": True,
    },
}

DEFAULT_PROVIDER_PRIORITY: tuple[str, ...] = (
    "google_genai",
    "openai",
    "anthropic",
    "bedrock",
    "azure_openai",
    "google_vertexai",
    "deepseek",
    "groq",
    "openrouter",
    "mistralai",
    "xai",
    "together",
    "fireworks",
    "cohere",
    "perplexity",
    "baseten",
    "huggingface",
    "ibm",
    "nvidia",
    "meta",
    "litellm",
    "ollama",
    "openai_codex",
)

AVAILABLE_MODELS: dict[str, list[tuple[str, str]]] = {
    "anthropic": [
        ("claude-sonnet-5", "Claude Sonnet 5"),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
        ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
        ("claude-sonnet-4-5-20250929", "Claude Sonnet 4.5 (20250929)"),
        ("claude-opus-5-5", "Claude Opus 5.5"),
        ("claude-opus-5", "Claude Opus 5"),
        ("claude-opus-4-8", "Claude Opus 4.8"),
        ("claude-opus-4-7", "Claude Opus 4.7"),
        ("claude-opus-4-6", "Claude Opus 4.6"),
        ("claude-opus-4-5", "Claude Opus 4.5"),
        ("claude-opus-4-5-20251101", "Claude Opus 4.5 (20251101)"),
        ("claude-haiku-4-5", "Claude Haiku 4.5"),
        ("claude-haiku-4-5-20251001", "Claude Haiku 4.5 (20251001)"),
        ("claude-fable-5-1", "Claude Fable 5.1"),
        ("claude-fable-5", "Claude Fable 5"),
    ],
    "openai": [
        ("gpt-6-astra", "GPT-6 Astra"),
        ("gpt-5.6-luna", "GPT-5.6 Luna"),
        ("gpt-5.6-sol", "GPT-5.6 Sol"),
        ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-5.5", "GPT-5.5"),
        ("gpt-5.4", "GPT-5.4"),
        ("gpt-5.4-mini", "GPT-5.4 Mini"),
        ("gpt-5.2", "GPT-5.2"),
        ("gpt-5.1", "GPT-5.1"),
        ("gpt-5", "GPT-5"),
        ("o3", "o3 (Thinking)"),
        ("o3-pro", "o3 Pro (Thinking)"),
        ("o3-mini", "o3 Mini (Thinking)"),
        ("o4-mini", "o4 Mini"),
        ("gpt-4o", "GPT-4o"),
        ("gpt-4o-mini", "GPT-4o Mini"),
    ],
    "openai_codex": [
        ("gpt-5.6-luna", "GPT-5.6 Luna"),
        ("gpt-5.6-sol", "GPT-5.6 Sol"),
        ("gpt-5.6-terra", "GPT-5.6 Terra"),
        ("gpt-5.5", "GPT-5.5"),
        ("gpt-5.4", "GPT-5.4"),
        ("gpt-5.4-mini", "GPT-5.4 Mini"),
        ("gpt-5.3-codex", "GPT-5.3 Codex"),
        ("gpt-5.2", "GPT-5.2"),
    ],
    "google_genai": [
        ("gemini-3.7-flash", "Gemini 3.7 Flash"),
        ("gemini-3.6-flash", "Gemini 3.6 Flash"),
        ("gemini-3.5-flash", "Gemini 3.5 Flash"),
        ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite"),
        ("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview"),
        ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite"),
        ("gemini-3-flash-preview", "Gemini 3 Flash Preview"),
        ("gemini-2.5-pro", "Gemini 2.5 Pro"),
        ("gemini-2.5-flash", "Gemini 2.5 Flash"),
        ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite"),
        ("gemini-flash-latest", "Gemini Flash (Latest)"),
        ("gemini-flash-lite-latest", "Gemini Flash-Lite (Latest)"),
    ],
    "bedrock": [
        ("us.anthropic.claude-sonnet-5", "Claude Sonnet 5 (Cross-Region)"),
        ("anthropic.claude-sonnet-5", "Claude Sonnet 5"),
        ("us.anthropic.claude-opus-5", "Claude Opus 5 (Cross-Region)"),
        ("anthropic.claude-opus-5", "Claude Opus 5"),
        ("us.anthropic.claude-opus-4-8", "Claude Opus 4.8 (Cross-Region)"),
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "Claude Sonnet 4.5 (Cross-Region)"),
        ("anthropic.claude-sonnet-4-5-20250929-v1:0", "Claude Sonnet 4.5"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "Claude Haiku 4.5 (Cross-Region)"),
        ("global.openai.gpt-6-astra", "GPT-6 Astra (Bedrock)"),
        ("global.openai.gpt-5.6-luna", "GPT-5.6 Luna (Bedrock)"),
        ("global.openai.gpt-5.6-sol", "GPT-5.6 Sol (Bedrock)"),
        ("global.openai.gpt-5.6-terra", "GPT-5.6 Terra (Bedrock)"),
        ("us.amazon.nova-2-lite-v1:0", "Amazon Nova 2 Lite (US)"),
        ("amazon.nova-pro-v1:0", "Amazon Nova Pro"),
        ("amazon.nova-lite-v1:0", "Amazon Nova Lite"),
        ("amazon.nova-micro-v1:0", "Amazon Nova Micro"),
        ("meta.llama4-maverick-17b-instruct-v1:0", "Meta Llama 4 Maverick 17B (Bedrock)"),
        ("meta.llama4-scout-17b-instruct-v1:0", "Meta Llama 4 Scout 17B (Bedrock)"),
        ("meta.llama3-3-70b-instruct-v1:0", "Meta Llama 3.3 70B (Bedrock)"),
        ("deepseek.v3.2", "DeepSeek V3.2 (Bedrock)"),
    ],
    "deepseek": [
        ("deepseek-v4-pro", "DeepSeek V4 Pro"),
        ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),
        ("deepseek-v4-flash", "DeepSeek V4 Flash"),
        ("deepseek-flash", "DeepSeek Flash"),
        ("deepseek-v4-flash-vision-exp", "DeepSeek V4 Flash Vision"),
    ],
    "openrouter": [
        ("anthropic/claude-sonnet-5", "Claude Sonnet 5 (OpenRouter)"),
        ("anthropic/claude-opus-4.8", "Claude Opus 4.8 (OpenRouter)"),
        ("openai/gpt-6-astra", "GPT-6 Astra (OpenRouter)"),
        ("openai/gpt-5.6-luna", "GPT-5.6 Luna (OpenRouter)"),
        ("deepseek/deepseek-v4-pro", "DeepSeek V4 Pro (OpenRouter)"),
        ("deepseek/deepseek-v4.1-flash", "DeepSeek V4.1 Flash (OpenRouter)"),
        ("google/gemini-3.7-flash", "Gemini 3.7 Flash (OpenRouter)"),
        ("moonshotai/kimi-k3", "Kimi K3 (OpenRouter)"),
        ("nvidia/nemotron-3-ultra-550b-a55b", "Nemotron 3 Ultra 550B (OpenRouter)"),
        ("qwen/qwen3.7-plus", "Qwen 3.7 Plus (OpenRouter)"),
        ("z-ai/glm-5.3", "GLM 5.3 (OpenRouter)"),
    ],
    "groq": [
        ("deepseek-r1-distill-llama-70b", "DeepSeek R1 Distill 70B"),
        ("groq/compound", "Groq Compound"),
        ("groq/compound-mini", "Groq Compound Mini"),
        ("llama-3.3-70b-versatile", "Llama 3.3 70B"),
        ("llama-3.1-8b-instant", "Llama 3.1 8B Instant"),
        ("meta-llama/llama-4-scout-17b-16e-instruct", "Llama 4 Scout 17B (Groq)"),
        ("meta-llama/llama-4-maverick-17b-128e-instruct", "Llama 4 Maverick 17B (Groq)"),
        ("mistral-saba-24b", "Mistral Saba 24B"),
        ("gemma2-9b-it", "Gemma 2 9B"),
        ("qwen-qwq-32b", "Qwen QwQ 32B"),
        ("qwen/qwen3-32b", "Qwen3 32B"),
        ("openai/gpt-oss-120b", "GPT-OSS 120B (Groq)"),
        ("openai/gpt-oss-20b", "GPT-OSS 20B (Groq)"),
    ],
    "together": [
        ("deepseek-ai/DeepSeek-V4-Pro", "DeepSeek V4 Pro (Together)"),
        ("deepseek-ai/DeepSeek-V4.1-Flash", "DeepSeek V4.1 Flash (Together)"),
        ("meta-llama/Llama-4-Scout-17B-Instruct", "Llama 4 Scout 17B (Together)"),
        ("meta-llama/Llama-3.3-70B-Instruct-Turbo", "Llama 3.3 70B Turbo (Together)"),
        ("Qwen/Qwen3.7-Plus", "Qwen 3.7 Plus (Together)"),
    ],
    "fireworks": [
        ("accounts/fireworks/models/deepseek-v4-pro", "DeepSeek V4 Pro (Fireworks)"),
        ("accounts/fireworks/models/deepseek-v3p2", "DeepSeek V3.2 (Fireworks)"),
        ("accounts/fireworks/models/deepseek-v3p1", "DeepSeek V3.1 (Fireworks)"),
        ("accounts/fireworks/models/glm-5p1", "GLM 5.1 (Fireworks)"),
        ("accounts/fireworks/models/glm-5", "GLM 5 (Fireworks)"),
        ("accounts/fireworks/models/glm-4p7", "GLM 4.7 (Fireworks)"),
        ("accounts/fireworks/models/glm-4p5", "GLM 4.5 (Fireworks)"),
        ("accounts/fireworks/models/glm-4p5-air", "GLM 4.5 Air (Fireworks)"),
        ("accounts/fireworks/models/kimi-k2-instruct", "Kimi K2 Instruct (Fireworks)"),
        ("accounts/fireworks/models/kimi-k2-thinking", "Kimi K2 Thinking (Fireworks)"),
        ("accounts/fireworks/models/minimax-m2p7", "MiniMax M2.7 (Fireworks)"),
        ("accounts/fireworks/models/minimax-m2p5", "MiniMax M2.5 (Fireworks)"),
        ("accounts/fireworks/models/minimax-m2p1", "MiniMax M2.1 (Fireworks)"),
        ("accounts/fireworks/models/qwen3p6-plus", "Qwen 3.6 Plus (Fireworks)"),
    ],
    "mistralai": [
        ("codestral-latest", "Codestral (Latest)"),
        ("devstral-latest", "Devstral 2 (Latest)"),
        ("devstral-2512", "Devstral 2 (2512)"),
        ("devstral-medium-latest", "Devstral Medium (Latest)"),
        ("devstral-small-2507", "Devstral Small (2507)"),
        ("magistral-medium-latest", "Magistral Medium"),
        ("mistral-large-latest", "Mistral Large"),
        ("mistral-medium-latest", "Mistral Medium"),
        ("mistral-small-latest", "Mistral Small"),
        ("pixtral-large-latest", "Pixtral Large"),
        ("ministral-8b-latest", "Ministral 8B"),
    ],
    "cohere": [
        ("command-r-plus-08-2024", "Command R+ (08-2024)"),
        ("command-r-plus", "Command R+"),
        ("command-r-08-2024", "Command R (08-2024)"),
        ("command-r", "Command R"),
    ],
    "perplexity": [
        ("sonar-pro", "Sonar Pro (Search)"),
        ("sonar", "Sonar (Search)"),
        ("sonar-reasoning", "Sonar Reasoning"),
    ],
    "xai": [
        ("grok-4.5", "Grok 4.5"),
        ("grok-4.3", "Grok 4.3"),
        ("grok-4.20-0309-reasoning", "Grok 4.20 Reasoning"),
        ("grok-4.20-0309-non-reasoning", "Grok 4.20 Non-Reasoning"),
        ("grok-build-0.1", "Grok Build 0.1"),
    ],
    "azure_openai": [
        ("gpt-5.6-luna", "GPT-5.6 Luna (Azure)"),
        ("gpt-5.6-sol", "GPT-5.6 Sol (Azure)"),
        ("gpt-5.4", "GPT-5.4 (Azure)"),
        ("gpt-5.2", "GPT-5.2 (Azure)"),
        ("o3", "o3 (Azure Thinking)"),
        ("o3-mini", "o3 Mini (Azure Thinking)"),
        ("gpt-4o", "GPT-4o (Azure)"),
        ("gpt-4o-mini", "GPT-4o Mini (Azure)"),
    ],
    "google_vertexai": [
        ("gemini-3.7-flash", "Gemini 3.7 Flash (Vertex)"),
        ("gemini-3.5-flash", "Gemini 3.5 Flash (Vertex)"),
        ("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview (Vertex)"),
        ("gemini-2.5-pro", "Gemini 2.5 Pro (Vertex)"),
        ("gemini-2.5-flash", "Gemini 2.5 Flash (Vertex)"),
        ("claude-sonnet-5@20250929", "Claude Sonnet 5 (Vertex)"),
        ("claude-opus-5@20251101", "Claude Opus 5 (Vertex)"),
    ],
    "ollama": [
        ("deepseek-v4-pro:cloud", "DeepSeek V4 Pro"),
        ("deepseek-v4.1-flash:cloud", "DeepSeek V4.1 Flash"),
        ("deepseek-v4-flash:cloud", "DeepSeek V4 Flash"),
        ("glm-5.3:cloud", "GLM 5.3"),
        ("glm-5.3-flash:cloud", "GLM 5.3 Flash"),
        ("minimax-m3:cloud", "MiniMax-M3"),
        ("llama3.3", "Llama 3.3"),
        ("llama3.1", "Llama 3.1"),
        ("qwen2.5-coder", "Qwen 2.5 Coder"),
        ("deepseek-r1", "DeepSeek R1 (Thinking)"),
        ("mistral", "Mistral 7B"),
    ],
    "baseten": [
        ("deepseek-ai/DeepSeek-V4-Pro", "DeepSeek V4 Pro (Baseten)"),
        ("deepseek-ai/DeepSeek-V4.1-Flash", "DeepSeek V4.1 Flash (Baseten)"),
        ("deepseek-ai/DeepSeek-V4-Flash-0731", "DeepSeek V4 Flash 0731 (Baseten)"),
        ("moonshotai/Kimi-K3", "Kimi K3 (Baseten)"),
        ("zai-org/GLM-5.3", "GLM 5.3 (Baseten)"),
        ("zai-org/GLM-5.3-Flash", "GLM 5.3 Flash (Baseten)"),
        ("nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B", "Nemotron 3 Ultra 550B (Baseten)"),
    ],
    "huggingface": [
        ("meta-llama/Llama-4-Scout-17B-Instruct", "Llama 4 Scout 17B (HuggingFace)"),
        ("meta-llama/Llama-3.3-70B-Instruct", "Llama 3.3 70B (HuggingFace)"),
        ("Qwen/Qwen2.5-Coder-32B-Instruct", "Qwen 2.5 Coder 32B (HuggingFace)"),
    ],
    "ibm": [
        ("ibm/granite-3-8b-instruct", "Granite 3 8B (watsonx)"),
        ("ibm/granite-3-20b-instruct", "Granite 3 20B (watsonx)"),
        ("meta-llama/llama-3-3-70b-instruct", "Llama 3.3 70B (watsonx)"),
    ],
    "nvidia": [
        ("nvidia/nemotron-3-ultra-550b-a55b", "Nemotron 3 Ultra 550B (NVIDIA)"),
        ("nvidia/llama-3.1-nemotron-70b-instruct", "Nemotron 70B (NVIDIA)"),
        ("meta/llama-3.3-70b-instruct", "Llama 3.3 70B (NVIDIA)"),
    ],
    "litellm": [
        ("openai/gpt-5.6-luna", "GPT-5.6 Luna (LiteLLM)"),
        ("anthropic/claude-sonnet-5", "Claude Sonnet 5 (LiteLLM)"),
        ("openai/gpt-4o", "GPT-4o (LiteLLM)"),
    ],
    "meta": [
        ("llama4-maverick-17b-instruct", "Llama 4 Maverick 17B"),
        ("llama4-scout-17b-instruct", "Llama 4 Scout 17B"),
        ("muse-spark-1.2", "Muse Spark 1.2"),
        ("llama-3.3-70b-instruct", "Llama 3.3 70B"),
    ],
}

PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "google_genai": "Google GenAI",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "openai_codex": "OpenAI Codex",
    "openrouter": "OpenRouter",
    "fireworks": "Fireworks AI",
    "together": "Together AI",
    "xai": "xAI",
    "mistralai": "Mistral AI",
    "groq": "Groq",
    "deepseek": "DeepSeek",
    "cohere": "Cohere",
    "perplexity": "Perplexity",
    "nvidia": "NVIDIA NIM",
    "baseten": "Baseten",
    "azure_openai": "Azure OpenAI",
    "bedrock": "AWS Bedrock",
    "google_vertexai": "Google Vertex AI",
    "google_anthropic_vertex": "Anthropic (Vertex AI)",
    "ibm": "IBM watsonx",
    "huggingface": "Hugging Face",
    "ollama": "Ollama",
    "meta": "Meta",
    "litellm": "LiteLLM",
    "typesafe": "TypeSafe AI (Jev)",
    "dynamic": "TypeSafe Jev (System 1)",
}


def get_provider_display_name(provider: str) -> str:
    """Return the user-facing display name for a model provider.

    Args:
        provider: Provider identifier.

    Returns:
        Formatted human-readable display name.
    """
    return PROVIDER_DISPLAY_NAMES.get(provider, provider.title())


# ── Dynamic Provider & Model Discovery (dcode aligned) ────

_BUILTIN_PROVIDERS_CACHE: dict[str, Any] | None = None
_PROVIDER_PROFILES_CACHE: dict[str, dict[str, Any]] = {}
_profiles_cache: dict[str, ModelProfileEntry] | None = None
_profiles_override_cache: tuple[int, dict[str, ModelProfileEntry]] | None = None
OLLAMA_DEFAULT_BASE_URL: str = "http://localhost:11434"
_ollama_installed_models_cache: list[str] | None = None


def clear_caches() -> None:
    """Clear all dynamic provider, model, and profile caches."""
    global _BUILTIN_PROVIDERS_CACHE, _profiles_cache, _profiles_override_cache, _ollama_installed_models_cache
    _BUILTIN_PROVIDERS_CACHE = None
    _PROVIDER_PROFILES_CACHE.clear()
    _profiles_cache = None
    _profiles_override_cache = None
    _ollama_installed_models_cache = None



def _get_builtin_providers() -> dict[str, Any]:
    """Return LangChain's built-in provider registry."""
    global _BUILTIN_PROVIDERS_CACHE
    cached = _BUILTIN_PROVIDERS_CACHE
    if cached is not None:
        return cached

    result: dict[str, Any] = {}
    try:
        from langchain.chat_models import base
        registry = getattr(base, "_BUILTIN_PROVIDERS", None)
        if registry is None:
            registry = getattr(base, "_SUPPORTED_PROVIDERS", None)
        if isinstance(registry, dict):
            result = dict(registry)
    except Exception:
        result = {}
    _BUILTIN_PROVIDERS_CACHE = result
    return result


def _get_provider_profile_modules() -> list[tuple[str, str]]:
    """Build a (provider, profile_module) list from langchain's provider registry."""
    providers = _get_builtin_providers()
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for provider_name, (module_path, *_rest) in providers.items():
        package_root = module_path.split(".", maxsplit=1)[0]
        profile_module = f"{package_root}.data._profiles"
        key = (provider_name, profile_module)
        if key not in seen:
            seen.add(key)
            result.append((provider_name, profile_module))
    return result


def _load_provider_profiles_from_package(module_path: str) -> dict[str, Any]:
    """Safely load _PROFILES from a provider data module without importing client code."""
    if module_path in _PROVIDER_PROFILES_CACHE:
        return _PROVIDER_PROFILES_CACHE[module_path]

    parts = module_path.split(".")
    package_root = parts[0]
    spec = importlib.util.find_spec(package_root)
    if spec is None:
        return {}

    if spec.origin:
        package_dir = Path(spec.origin).parent
    elif spec.submodule_search_locations:
        package_dir = Path(next(iter(spec.submodule_search_locations)))
    else:
        return {}

    relative_parts = parts[1:]
    profiles_path = package_dir.joinpath(*relative_parts[:-1], f"{relative_parts[-1]}.py")
    if not profiles_path.exists():
        return {}

    try:
        file_spec = importlib.util.spec_from_file_location(module_path, profiles_path)
        if file_spec and file_spec.loader:
            module = importlib.util.module_from_spec(file_spec)
            file_spec.loader.exec_module(module)
            profiles = getattr(module, "_PROFILES", {})
            _PROVIDER_PROFILES_CACHE[module_path] = profiles
            return profiles
    except Exception:
        logger.debug("Failed to load profiles from %s", module_path, exc_info=True)

    return {}


def _discover_upstream_models() -> dict[str, list[tuple[str, str]]]:
    """Dynamically discover models from installed LangChain packages exposing data._profiles."""
    discovered: dict[str, list[tuple[str, str]]] = {}
    builtin = _get_builtin_providers()
    for provider, (module_path, *_) in builtin.items():
        if provider in ("anthropic_bedrock", "bedrock_converse", "langsmith"):
            continue
        package_root = module_path.split(".", 1)[0]
        profile_module = f"{package_root}.data._profiles"
        profiles = _load_provider_profiles_from_package(profile_module)
        if not profiles:
            continue
        models: list[tuple[str, str]] = []
        for name, prof in profiles.items():
            if (
                prof.get("tool_calling", False)
                and prof.get("text_inputs", True) is not False
                and prof.get("text_outputs", True) is not False
            ):
                display = prof.get("name") or name
                models.append((name, str(display)))
        if models:
            models.sort(key=lambda x: x[0])
            discovered[provider] = models
    return discovered


def _ollama_host_reachable(base_url: str, *, timeout: float = 0.5) -> bool:
    """Preflight TCP check to quickly determine if an Ollama daemon is listening."""
    parsed = urlparse(base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 11434)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (TimeoutError, OSError):
        return False
    except Exception:
        return False


def _fetch_ollama_installed_models(endpoint: str | None = None) -> list[str]:
    """Query local or hosted Ollama daemon /api/tags for installed models."""
    base = (endpoint or OLLAMA_DEFAULT_BASE_URL).rstrip("/")
    if not _ollama_host_reachable(base):
        return []

    try:
        url = f"{base}/api/tags"
        req = Request(url, headers={"User-Agent": "opscloud"})
        with urlopen(req, timeout=1.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("models"), list):
                names = [
                    m.get("name")
                    for m in payload["models"]
                    if isinstance(m, dict) and m.get("name")
                ]
                return sorted(str(n) for n in names if n)
    except Exception:
        logger.debug("Ollama model discovery skipped or failed for %s", base)

    return []


def _get_upstream_profile(provider: str, model_id: str) -> dict[str, Any] | None:
    """Load profile from upstream LangChain package data._profiles if installed."""
    if provider == CODEX_PROVIDER:
        provider = "openai"
    pkg_map = {
        "anthropic": "langchain_anthropic",
        "google_genai": "langchain_google_genai",
        "google_vertexai": "langchain_google_vertexai",
        "google_anthropic_vertex": "langchain_google_vertexai",
        "openai": "langchain_openai",
        "azure_openai": "langchain_openai",
        "fireworks": "langchain_fireworks",
        "together": "langchain_together",
        "groq": "langchain_groq",
        "mistralai": "langchain_mistralai",
        "xai": "langchain_xai",
        "deepseek": "langchain_deepseek",
        "perplexity": "langchain_perplexity",
        "nvidia": "langchain_nvidia_ai_endpoints",
        "baseten": "langchain_baseten",
        "huggingface": "langchain_huggingface",
        "bedrock": "langchain_aws",
        "cohere": "langchain_cohere",
    }
    pkg = pkg_map.get(provider)
    if not pkg:
        return None

    profile_module = f"{pkg}.data._profiles"
    profiles = _load_provider_profiles_from_package(profile_module)
    if model_id in profiles:
        prof = dict(profiles[model_id])
        spec = f"{provider}:{model_id}"
        if spec in MODEL_PROFILES:
            prof.update(MODEL_PROFILES[spec])
        return prof
    # Check date snapshot fallback (e.g. gpt-4o-2024-08-06 -> gpt-4o)
    for k, v in profiles.items():
        if model_id.startswith(f"{k}-"):
            remainder = model_id[len(k) + 1 :]
            if all(c.isdigit() or c in "-.v" for c in remainder):
                prof = dict(v)
                spec = f"{provider}:{k}"
                if spec in MODEL_PROFILES:
                    prof.update(MODEL_PROFILES[spec])
                return prof
    return None


def _build_profile_entry(
    base: Mapping[str, Any],
    overrides: dict[str, Any],
    cli_override: dict[str, Any] | None,
) -> ModelProfileEntry:
    """Build a profile entry by merging base, overrides, and app override."""
    merged = dict(base)
    overridden_keys: set[str] = set()
    if overrides:
        merged.update(overrides)
        overridden_keys.update(overrides.keys())
    if cli_override:
        merged.update(cli_override)
        overridden_keys.update(cli_override.keys())
    return {
        "profile": cast(ModelProfile, merged),
        "overridden_keys": overridden_keys,
    }


def get_model_profiles(
    *, cli_override: dict[str, Any] | None = None
) -> Mapping[str, ModelProfileEntry]:
    """Load upstream profiles merged with configuration overrides and local catalog."""
    global _profiles_cache, _profiles_override_cache
    if cli_override is None and _profiles_cache is not None:
        return _profiles_cache
    if cli_override is not None and _profiles_override_cache is not None:
        cached_id, cached_res = _profiles_override_cache
        if cached_id == id(cli_override):
            return cached_res

    result: dict[str, ModelProfileEntry] = {}
    config = ModelConfig.load()

    # 1. Dynamic upstream discovery from installed provider packages (dcode aligned)
    for provider, module_path in _get_provider_profile_modules():
        if not config.is_provider_enabled(provider):
            continue
        try:
            profiles = _load_provider_profiles_from_package(module_path)
        except Exception:
            continue
        for model_name, upstream_profile in profiles.items():
            spec = f"{provider}:{model_name}"
            overrides = config.get_profile_overrides(provider, model_name=model_name)
            local_prof = MODEL_PROFILES.get(spec, {})
            merged_prof = {**upstream_profile, **local_prof}
            result[spec] = _build_profile_entry(merged_prof, overrides, cli_override)
            if (
                provider == "openai"
                and model_name in CODEX_MODELS
                and config.is_provider_enabled(CODEX_PROVIDER)
            ):
                codex_spec = f"{CODEX_PROVIDER}:{model_name}"
                codex_overrides = config.get_profile_overrides(
                    CODEX_PROVIDER, model_name=model_name
                )
                result[codex_spec] = _build_profile_entry(
                    upstream_profile, codex_overrides, cli_override
                )

    # 2. Local MODEL_PROFILES entries for models not covered upstream
    for spec, prof in MODEL_PROFILES.items():
        if spec not in result:
            provider = detect_provider(spec) or ""
            model_id = spec.split(":", 1)[1] if ":" in spec else spec
            overrides = (
                config.get_profile_overrides(provider, model_name=model_id) if provider else {}
            )
            result[spec] = _build_profile_entry(prof, overrides, cli_override)

    if cli_override is None:
        _profiles_cache = result
    else:
        _profiles_override_cache = (id(cli_override), result)
    return result


def get_model_profile(spec: str) -> ModelProfileEntry | None:
    """Resolve capability and configuration profile for a model specification.

    Args:
        spec: Model specification string (e.g. 'openai:gpt-4o' or 'gpt-4o').

    Returns:
        ModelProfileEntry dictionary containing context limits, features, and defaults.
    """
    if not spec:
        return None
    normalized = normalize_model_spec(spec)
    profiles = get_model_profiles()
    if normalized in profiles:
        return profiles[normalized]
    if spec in profiles:
        return profiles[spec]

    # Check date snapshot fallback (e.g. gpt-4o-2024-08-06 -> gpt-4o)
    for k, v in profiles.items():
        if normalized.startswith(f"{k}-") or spec.startswith(f"{k}-"):
            remainder = (normalized if normalized.startswith(f"{k}-") else spec)[len(k) + 1 :]
            if all(c.isdigit() or c in "-.v" for c in remainder):
                return v

    # Fallback to direct upstream lookup
    if ":" in spec:
        provider, model_id = spec.split(":", 1)
    else:
        provider = detect_provider(spec) or ""
        model_id = spec
    upstream = _get_upstream_profile(provider, model_id)
    if upstream:
        config = ModelConfig.load()
        overrides = config.get_profile_overrides(provider, model_name=model_id)
        return _build_profile_entry(upstream, overrides, None)

    # Check genai-prices snapshot fallback
    try:
        from genai_prices.data_snapshot import get_snapshot

        snap = get_snapshot()
        _prov_lookup = provider or None
        if _prov_lookup:
            _alias_map = {"google_genai": "google", "bedrock": "aws", "azure_openai": "azure"}
            _prov_lookup = _alias_map.get(_prov_lookup, _prov_lookup)
        p, m = snap.find_provider_model(model_id, None, _prov_lookup, None)
        cw = getattr(m, "context_window", None) if m else None
        if isinstance(cw, int) and cw > 0:
            genai_prof = {
                "name": getattr(m, "name", model_id),
                "max_input_tokens": cw,
                "tool_calling": True,
            }
            config = ModelConfig.load()
            overrides = config.get_profile_overrides(provider, model_name=model_id)
            return _build_profile_entry(genai_prof, overrides, None)
    except Exception:
        pass

    return None


def resolve_model_context_limit(spec: str | None) -> int | None:
    """Resolve the context window limit (in tokens) for a given model spec.

    Sources:
    1. Upstream LangChain provider profile or local MODEL_PROFILES.
    2. genai-prices data snapshot `context_window` attribute.
    3. Global settings.model_context_limit fallback.
    """
    if not spec:
        from opscloud.config.settings import settings

        return getattr(settings, "model_context_limit", None)
    prof_entry = get_model_profile(spec)
    if prof_entry:
        limit = prof_entry.get("profile", {}).get("max_input_tokens")
        if isinstance(limit, int) and limit > 0:
            return limit
    from opscloud.config.settings import settings

    return getattr(settings, "model_context_limit", None)


def get_available_models_list() -> list[tuple[str, str, str]]:
    """Return flat list of (spec, display_name, provider) tuples.

    Combines:
    1. Curated frontier catalog across all providers (primary source of truth,
       ordered by DEFAULT_PROVIDER_PRIORITY with curated display names).
    2. Dynamic upstream models discovered from installed LangChain packages
       (for additional uncataloged or preview models).
    3. Locally installed models from a running Ollama daemon (if available).
    4. Custom models declared in config.toml providers config.
    """
    result: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    config = ModelConfig.load()

    # 1. Curated frontier catalog across all providers (primary source of truth)
    for provider in DEFAULT_PROVIDER_PRIORITY:
        if provider not in AVAILABLE_MODELS or not config.is_provider_enabled(provider):
            continue
        for model_id, display_name in AVAILABLE_MODELS[provider]:
            spec = f"{provider}:{model_id}"
            if spec not in seen:
                result.append((spec, display_name, provider))
                seen.add(spec)
    for provider, models in AVAILABLE_MODELS.items():
        if provider in DEFAULT_PROVIDER_PRIORITY or not config.is_provider_enabled(provider):
            continue
        for model_id, display_name in models:
            spec = f"{provider}:{model_id}"
            if spec not in seen:
                result.append((spec, display_name, provider))
                seen.add(spec)

    # 2. Dynamic upstream discovery from installed LangChain packages
    try:
        discovered_upstream = _discover_upstream_models()
        for provider, models in discovered_upstream.items():
            if not config.is_provider_enabled(provider):
                continue
            for model_id, display_name in models:
                spec = f"{provider}:{model_id}"
                if spec not in seen:
                    result.append((spec, display_name, provider))
                    seen.add(spec)
    except Exception:
        logger.debug("Failed upstream model discovery", exc_info=True)

    # 3. Dynamic local Ollama discovery if daemon is running
    if config.is_provider_enabled("ollama"):
        try:
            ollama_models = _fetch_ollama_installed_models()
            for model_id in ollama_models:
                spec = f"ollama:{model_id}"
                if spec not in seen:
                    result.append((spec, model_id, "ollama"))
                    seen.add(spec)
        except Exception:
            logger.debug("Failed Ollama model discovery", exc_info=True)

    # 4. Config-defined custom models
    for provider, pconfig in config.providers.items():
        if not config.is_provider_enabled(provider):
            continue
        models = pconfig.get("models", [])
        for model_id in models:
            spec = f"{provider}:{model_id}"
            if spec not in seen:
                result.append((spec, model_id, provider))
                seen.add(spec)

    return result


def get_curated_models_for_provider(provider: str) -> list[tuple[str, str]]:
    """Return curated list of (model_id, display_name) tuples for a provider from AVAILABLE_MODELS."""
    return list(AVAILABLE_MODELS.get(provider, []))

_BEDROCK_REGION_PREFIXES = ("us.", "eu.", "apac.", "us-gov.")


def is_bedrock_model_id(model_name: str) -> bool:
    """Return whether model_name matches the signature of an AWS Bedrock model ID.

    Bedrock IDs have the shape `[<region>.]<vendor>.<model>[:<version>]`, e.g.
    `anthropic.claude-3-5-sonnet-20241022-v2:0` or `us.anthropic.claude-3-7-sonnet-20250219-v1:0`.
    """
    model_lower = model_name.strip().lower()
    if model_lower.startswith("bedrock:"):
        model_lower = model_lower.removeprefix("bedrock:")
    for region in _BEDROCK_REGION_PREFIXES:
        if model_lower.startswith(region):
            model_lower = model_lower.removeprefix(region)
            break
    vendor, dot, _ = model_lower.partition(".")
    return bool(dot) and vendor.isalnum()


def detect_provider(model_name: str) -> str | None:
    """Infer provider from model name prefixes."""
    name_lower = model_name.strip().lower()
    if name_lower.startswith("bedrock:") or is_bedrock_model_id(name_lower):
        return "bedrock"
    if name_lower.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if name_lower.startswith(("claude-", "claude", "sonnet", "opus", "haiku")):
        return "anthropic"
    if name_lower.startswith("gemini"):
        return "google_genai"
    if name_lower.startswith("deepseek"):
        return "deepseek"
    if name_lower.startswith("grok"):
        return "xai"
    if name_lower.startswith(("mistral", "mixtral", "codestral", "pixtral")):
        return "mistralai"
    if name_lower.startswith("sonar"):
        return "perplexity"
    if name_lower.startswith("command"):
        return "cohere"
    if name_lower.startswith(("nemotron", "nvidia/")):
        return "nvidia"
    if name_lower.startswith("accounts/fireworks/"):
        return "fireworks"
    if name_lower.startswith("groq"):
        return "groq"
    if name_lower.startswith("llama"):
        return "ollama"
    return None


def normalize_model_spec(model_spec: str) -> str:
    """Normalize a model specifier into `provider:model` format if bare."""
    if not model_spec:
        return model_spec
    stripped = model_spec.strip()
    if stripped.lower().startswith("bedrock:"):
        return stripped
    if is_bedrock_model_id(stripped):
        return f"bedrock:{stripped}"
    if ":" in stripped:
        return stripped
    provider = detect_provider(stripped)
    if provider:
        return f"{provider}:{stripped}"
    return stripped


def resolve_model_spec(spec: str | None) -> tuple[str, str]:
    """Resolve a model spec into (provider, model_id) tuple."""
    if not spec:
        return "", ""
    stripped = spec.strip()
    if not stripped:
        return "", ""
    stripped_lower = stripped.lower()
    if stripped_lower.startswith("bedrock:"):
        return "bedrock", stripped[len("bedrock:"):]
    if is_bedrock_model_id(stripped_lower):
        return "bedrock", stripped
    if ":" in stripped:
        provider, model_id = stripped.split(":", 1)
        return provider.lower(), model_id
    provider = detect_provider(stripped) or ""
    return provider, stripped


from opscloud.config.toml_config import (
    clear_default_model,
    load_default_model,
    load_recent_model,
    save_default_model,
    save_recent_model,
)

import importlib.util

_SUPPRESSED_WARNINGS: set[str] = set()


def is_warning_suppressed(key: str) -> bool:
    """Check if a warning key is suppressed."""
    return key in _SUPPRESSED_WARNINGS


def suppress_warning(key: str) -> bool:
    """Suppress a warning key."""
    _SUPPRESSED_WARNINGS.add(key)
    return True


def unsuppress_warning(key: str) -> bool:
    """Unsuppress a warning key."""
    _SUPPRESSED_WARNINGS.discard(key)
    return True


def revoke_provider_credentials(provider: str | None = None, settings: Any | None = None) -> list[str]:
    """Revoke stored credentials for a specified provider, or all providers."""
    from opscloud.config.paths import GLOBAL_ENV_PATH

    target_providers: list[str] = []
    env_keys_to_clear: set[str] = set()

    for p, env_var in PROVIDER_API_KEY_ENV.items():
        if provider is None or provider.lower() in ("all", "*", "") or provider.lower() == p.lower():
            target_providers.append(p)
            if env_var:
                env_keys_to_clear.add(env_var)
                os.environ.pop(env_var, None)

    if GLOBAL_ENV_PATH.exists():
        try:
            lines = GLOBAL_ENV_PATH.read_text(encoding="utf-8").splitlines()
            remaining = [
                line for line in lines
                if not any(line.strip().startswith(f"{k}=") or line.strip().startswith(f"export {k}=") for k in env_keys_to_clear)
            ]
            GLOBAL_ENV_PATH.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to remove credentials from %s: %s", GLOBAL_ENV_PATH, e)

    return target_providers


_PROVIDER_DEPENDENCIES: dict[str, tuple[str, str]] = {
    "anthropic": ("langchain_anthropic", "anthropic"),
    "azure_openai": ("langchain_openai", "openai"),
    "baseten": ("langchain_baseten", "baseten"),
    "bedrock": ("langchain_aws", "bedrock"),
    "cohere": ("langchain_cohere", "cohere"),
    "deepseek": ("langchain_deepseek", "deepseek"),
    "fireworks": ("langchain_fireworks", "fireworks"),
    "google_genai": ("langchain_google_genai", "google-genai"),
    "google_vertexai": ("langchain_google_vertexai", "vertex"),
    "groq": ("langchain_groq", "groq"),
    "huggingface": ("langchain_huggingface", "huggingface"),
    "ibm": ("langchain_ibm", "ibm"),
    "litellm": ("langchain_litellm", "litellm"),
    "meta": ("langchain_meta", "meta"),
    "mistralai": ("langchain_mistralai", "mistralai"),
    "nvidia": ("langchain_nvidia_ai_endpoints", "nvidia"),
    "ollama": ("langchain_ollama", "ollama"),
    "openai": ("langchain_openai", "openai"),
    "openrouter": ("langchain_openrouter", "openrouter"),
    "perplexity": ("langchain_perplexity", "perplexity"),
    "together": ("langchain_together", "together"),
    "typesafe": ("langchain_typesafe", "typesafe"),
    "xai": ("langchain_xai", "xai"),
}


def provider_install_extra(provider: str) -> str | None:
    """Return the extra package name required for provider."""
    dep = _PROVIDER_DEPENDENCIES.get(provider)
    return dep[1] if dep else None


def is_provider_package_installed(provider: str) -> bool:
    """Check if the provider's Python integration package is installed."""
    dep = _PROVIDER_DEPENDENCIES.get(provider)
    if dep is None:
        return True
    try:
        return importlib.util.find_spec(dep[0]) is not None
    except Exception:
        return False


def format_token_count(count: int) -> str:
    """Format token count integer to human-readable string (e.g. 1.0M, 200k)."""
    if count >= 1_000_000:
        val = count / 1_000_000
        return f"{val:.1f}M" if val % 1 != 0 else f"{int(val)}M"
    if count >= 1_000:
        val = count / 1_000
        return f"{val:.1f}k" if val % 1 != 0 else f"{int(val)}k"
    return str(count)


RECOMMENDED_SPECS: frozenset[str] = frozenset(
    {"auto"}
    | {
        f"{prov}:{model_id}"
        for prov, models in AVAILABLE_MODELS.items()
        for model_id, _ in models
    }
)



def load_recent_models() -> list[str]:
    """Load the most-recently-used model specs (most recent first)."""
    from opscloud.config.paths import RECENT_MODELS_PATH
    try:
        if RECENT_MODELS_PATH.exists():
            data = json.loads(RECENT_MODELS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [str(s) for s in data[:10]]
            elif isinstance(data, dict) and isinstance(data.get("models"), list):
                return [str(s) for s in data["models"][:10]]
    except Exception:
        logger.debug("Failed to load recent models", exc_info=True)
    return []


__all__ = [
    "AVAILABLE_MODELS",
    "DEFAULT_PROVIDER_PRIORITY",
    "IMPLICIT_AUTH_PROVIDERS",
    "MODEL_PROFILES",
    "ModelConfig",
    "ModelProfile",
    "ModelProfileEntry",
    "ModelSpec",
    "NO_AUTH_REQUIRED_PROVIDERS",
    "OPTIONAL_AUTH_ENV",
    "PROVIDER_API_KEY_ENV",
    "PROVIDER_BASE_URL_ENV",
    "PROVIDER_DISPLAY_NAMES",
    "PROVIDER_KEY_ALIASES",
    "PROVIDER_SETTINGS_FIELD_MAP",
    "ProviderAuthState",
    "ProviderAuthStatus",
    "RECOMMENDED_SPECS",
    "apply_stored_credentials",
    "clear_default_model",
    "detect_provider",
    "format_token_count",
    "get_available_models_list",
    "get_base_url_env_vars",
    "get_credential_env_var",
    "get_curated_models_for_provider",
    "get_model_profile",
    "get_model_profiles",
    "get_provider_auth_status",
    "get_provider_display_name",
    "has_provider_credentials",
    "is_bedrock_model_id",
    "is_provider_package_installed",
    "is_warning_suppressed",
    "load_default_model",
    "load_recent_model",
    "load_recent_models",
    "normalize_model_spec",
    "provider_install_extra",
    "resolve_model_context_limit",
    "resolve_model_spec",
    "revoke_provider_credentials",
    "save_default_model",
    "save_recent_model",
    "suppress_warning",
    "unsuppress_warning",
]
