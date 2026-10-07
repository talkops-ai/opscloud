"""LLM Model construction factory and provider detection.

Builds dynamic chat model instances for LangChain agents based on the
active model spec (``provider:model``) and reasoning effort configuration.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import importlib
import importlib.util
import json
import os
import re
import threading
from typing import Any, cast

# Avoid duplicate protobuf descriptor registration between google-genai, google-cloud-aiplatform, and fireworks-ai
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
try:
    import google.longrunning.operations_proto_pb2  # noqa: F401
except ImportError:
    pass

from langchain_core.language_models import BaseChatModel

from opscloud.config.settings import get_settings, resolve_env_var
from opscloud.exceptions import (
    MissingCredentialsError,
    MissingProviderPackageError,
    ModelConfigError,
    NoCredentialsConfiguredError,
)
from opscloud.model.config import (
    IMPLICIT_AUTH_PROVIDERS,
    ModelConfig,
    ModelSpec,
    apply_stored_credentials,
    detect_provider,
    get_credential_env_var,
    get_model_profile,
    has_provider_credentials,
    normalize_model_spec,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ModelResult:
    """Chat model instance with associated metadata."""

    model: BaseChatModel
    model_name: str
    provider: str
    context_limit: int | None = None
    unsupported_modalities: frozenset[str] = frozenset()

    def apply_to_settings(self) -> None:
        """Commit this result's metadata to the global settings singleton."""
        s = get_settings()
        s.model = self.model_name
        s.model_name = self.model_name
        s.model_provider = self.provider
        s.model_context_limit = self.context_limit
        s.model_unsupported_modalities = self.unsupported_modalities


def _get_default_model_spec() -> str:
    """Resolve the default model spec to use based on settings or configured credentials."""
    from opscloud.config.settings import _load_dotenv

    _load_dotenv(refresh_loaded=True)
    config = ModelConfig.load()
    settings = get_settings()

    def _has_creds(spec: str) -> bool:
        provider = spec.split(":", 1)[0] if ":" in spec else detect_provider(spec)
        return bool(provider and has_provider_credentials(provider))

    # 1. If explicit model set in settings and has credentials
    if settings.model_name and _has_creds(settings.model_name):
        return normalize_model_spec(settings.model_name)
    if settings.model and _has_creds(settings.model):
        return normalize_model_spec(settings.model)

    # 2. If default / recent model set in config and has credentials
    if config.default_model and _has_creds(config.default_model):
        return config.default_model
    if config.recent_model and _has_creds(config.recent_model):
        return config.recent_model

    # 3. Auto-detect from available credentials in environment
    for prov, default_model in (
        ("anthropic", "anthropic:claude-sonnet-4-5"),
        ("openai", "openai:gpt-5.4"),
        ("openai_codex", "openai_codex:gpt-5.4"),
        ("bedrock", "bedrock:anthropic.claude-sonnet-4-5-20250929-v1:0"),
        ("google_genai", "google_genai:gemini-2.5-flash"),
        ("groq", "groq:llama-3.3-70b-versatile"),
        ("deepseek", "deepseek:deepseek-v4-flash"),
        ("openrouter", "openrouter:anthropic/claude-sonnet-5"),
    ):
        if has_provider_credentials(prov) is True:
            return default_model

    # 4. If explicit model set without credentials (e.g. testing / local provider)
    if settings.model_name:
        return normalize_model_spec(settings.model_name)
    if settings.model:
        return normalize_model_spec(settings.model)

    raise NoCredentialsConfiguredError(
        "No model is configured yet. Run /model to choose one."
    )


def _get_provider_kwargs(provider: str, *, model_name: str | None = None) -> dict[str, Any]:
    """Retrieve all configuration arguments for the provider/model."""
    config = ModelConfig.load()
    result = config.get_kwargs(provider, model_name=model_name)

    base_url = config.get_base_url(provider)
    if base_url:
        if provider == "azure_openai":
            result["azure_endpoint"] = base_url
            api_version = (
                resolve_env_var("AZURE_OPENAI_API_VERSION")
                or resolve_env_var("OPENAI_API_VERSION")
                or "2024-08-01-preview"
            )
            result.setdefault("api_version", api_version)
        elif provider == "perplexity":
            # ChatPerplexity does not accept base_url directly in kwargs
            pass
        elif provider == "anthropic":
            anthropic_base = base_url[:-3] if base_url.endswith("/v1") else base_url
            result["anthropic_api_url"] = anthropic_base
            result["base_url"] = anthropic_base
        else:
            result["base_url"] = base_url
            if provider == "mistralai":
                result["endpoint"] = base_url
            elif provider == "deepseek":
                result["api_base"] = base_url
            elif provider == "groq":
                result["groq_api_base"] = base_url
            elif provider == "fireworks":
                result["fireworks_api_base"] = base_url
            elif provider == "xai":
                result["xai_api_base"] = base_url

    settings = get_settings()
    env_var = get_credential_env_var(provider)
    api_key = None
    if env_var:
        api_key = resolve_env_var(env_var)

    if not api_key:
        field_map = {
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
            "typesafe": "typesafe_api_key",
            "ibm": "watsonx_apikey",
        }
        field = field_map.get(provider)
        if field and hasattr(settings, field):
            api_key = getattr(settings, field)

    if not api_key and provider in ("google_genai", "google", "google_vertexai"):
        api_key = resolve_env_var("GEMINI_API_KEY") or getattr(settings, "google_api_key", None)

    if api_key:
        if provider not in (
            "google_vertexai",
            "google_anthropic_vertex",
            "bedrock",
            "bedrock_converse",
            "anthropic_bedrock",
        ):
            result["api_key"] = api_key
        if provider in ("google_genai", "google"):
            result["google_api_key"] = api_key
        elif provider == "huggingface":
            result["huggingfacehub_api_token"] = api_key
        elif provider == "ibm":
            result["apikey"] = api_key
        elif provider == "anthropic":
            result["anthropic_api_key"] = api_key
            result.setdefault("default_headers", {})["Authorization"] = f"Bearer {api_key}"
        if env_var:
            os.environ[env_var] = str(api_key)

    # AWS Bedrock handling
    if provider in ("bedrock", "bedrock_converse", "anthropic_bedrock"):
        from opscloud.config.aws import get_aws_model_kwargs, validate_aws_credentials

        is_valid, err = validate_aws_credentials()
        if not is_valid and err:
            logger.warning(err)
        aws_kwargs = get_aws_model_kwargs(settings)
        result.update(aws_kwargs)
        result.pop("api_key", None)

    # IBM watsonx handling
    if provider == "ibm":
        project_id = resolve_env_var("WATSONX_PROJECT_ID") or getattr(settings, "watsonx_project_id", None)
        if project_id:
            result["project_id"] = project_id
        space_id = resolve_env_var("WATSONX_SPACE_ID") or getattr(settings, "watsonx_space_id", None)
        if space_id:
            result["space_id"] = space_id
        watsonx_url = (
            config.get_base_url("ibm")
            or resolve_env_var("WATSONX_URL")
            or getattr(settings, "watsonx_url", None)
        )
        if watsonx_url:
            result["url"] = watsonx_url

    # Vertex AI handling
    if provider in ("google_genai", "google"):
        use_vertex_env = resolve_env_var("GOOGLE_GENAI_USE_VERTEXAI") or str(
            getattr(settings, "google_genai_use_vertexai", False)
        )
        is_vertex = use_vertex_env.strip().lower() in ("true", "1", "yes")
        if is_vertex:
            result["vertexai"] = True
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
        project_env = resolve_env_var("GOOGLE_CLOUD_PROJECT") or getattr(settings, "google_cloud_project", None)
        if project_env:
            result["project"] = project_env
            os.environ["GOOGLE_CLOUD_PROJECT"] = str(project_env)
        location_env = resolve_env_var("GOOGLE_CLOUD_LOCATION") or getattr(settings, "google_cloud_location", None)
        if location_env:
            result["location"] = location_env
            os.environ["GOOGLE_CLOUD_LOCATION"] = str(location_env)
    elif provider in ("google_vertexai", "google_anthropic_vertex"):
        project_env = resolve_env_var("GOOGLE_CLOUD_PROJECT") or getattr(settings, "google_cloud_project", None)
        if project_env:
            result["project"] = project_env
            os.environ["GOOGLE_CLOUD_PROJECT"] = str(project_env)
        location_env = resolve_env_var("GOOGLE_CLOUD_LOCATION") or getattr(settings, "google_cloud_location", None)
        if location_env:
            result["location"] = location_env
            os.environ["GOOGLE_CLOUD_LOCATION"] = str(location_env)

    # Ollama bearer token injection for remote / hosted Ollama instances (matches dcode)
    if provider == "ollama":
        result.pop("api_key", None)
        ollama_key = resolve_env_var("OLLAMA_API_KEY")
        if ollama_key:
            client_kwargs = result.get("client_kwargs")
            if isinstance(client_kwargs, dict) or client_kwargs is None:
                client_kwargs = dict(client_kwargs) if client_kwargs else {}
                headers = client_kwargs.get("headers")
                headers = dict(headers) if isinstance(headers, dict) else {}
                if not any(isinstance(k, str) and k.lower() == "authorization" for k in headers):
                    headers["Authorization"] = f"Bearer {ollama_key}"
                    client_kwargs["headers"] = headers
                    result["client_kwargs"] = client_kwargs

    # Reasoning effort injection
    from opscloud.model.reasoning import is_effort_supported_for_model, with_effort_model_params

    effort = getattr(settings, "reasoning_effort", None)
    if effort:
        spec = f"{provider}:{model_name}" if model_name else provider
        if is_effort_supported_for_model(spec, str(effort)):
            result = with_effort_model_params(spec, result, str(effort))

    return result


def _compose_openai_reasoning_effort(
    provider: str,
    kwargs: dict[str, Any],
    effort_override: object = None,
    reasoning_override: object = None,
) -> dict[str, Any]:
    """Compose a session effort override with an OpenAI reasoning mapping.

    Args:
        provider: Resolved model provider.
        kwargs: Layered model constructor parameters.
        effort_override: High-priority `reasoning_effort` from session params.
        reasoning_override: High-priority native `reasoning` from session params.

    Returns:
        Constructor parameters with one native `reasoning` mapping when
        composition is needed.
    """
    if provider not in {"openai", "openai_codex", "azure_openai"}:
        return kwargs
    effort = effort_override if isinstance(effort_override, str) else kwargs.get("reasoning_effort")
    if not isinstance(effort, str):
        return kwargs
    reasoning = kwargs.get("reasoning")
    if not isinstance(reasoning, dict):
        return kwargs
    composed = dict(kwargs)
    if isinstance(reasoning_override, dict) and "effort" in reasoning_override:
        composed.pop("reasoning_effort", None)
        return composed
    composed["reasoning"] = {**reasoning, "effort": effort}
    composed.pop("reasoning_effort", None)
    return composed


def _create_model_from_class(
    class_path: str,
    model_name: str,
    provider: str,
    kwargs: dict[str, Any],
) -> BaseChatModel:
    """Instantiate a chat model dynamically via class reflection."""
    if ":" not in class_path:
        raise ModelConfigError(f"Invalid class_path '{class_path}': must be module:Class format")

    module_path, class_name = class_path.rsplit(":", 1)
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ModelConfigError(f"Module '{module_path}' not found for class_path '{class_path}': {e}") from e

    cls = getattr(module, class_name, None)
    if cls is None:
        raise ModelConfigError(f"Class '{class_name}' not found in module '{module_path}'")

    if not (isinstance(cls, type) and issubclass(cls, BaseChatModel)):
        raise ModelConfigError(f"'{class_path}' is not a BaseChatModel subclass")

    cls_kwargs = dict(kwargs)

    cls_any = cast(Any, cls)
    try:
        return cls_any(model=model_name, **cls_kwargs)
    except TypeError:
        return cls_any(model_name=model_name, **cls_kwargs)


def _create_model_via_init(
    model_name: str,
    provider: str,
    kwargs: dict[str, Any],
) -> BaseChatModel:
    """Delegate to langchain's dynamic init_chat_model function."""
    from langchain.chat_models import init_chat_model

    init_kwargs = dict(kwargs)

    target_provider = provider
    if provider in ("openai_codex", "typesafe"):
        target_provider = "openai"

    if target_provider == "huggingface":
        try:
            from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint

            hf_kwargs = dict(init_kwargs)
            token = hf_kwargs.pop("huggingfacehub_api_token", None) or hf_kwargs.pop("api_key", None)
            endpoint_url = hf_kwargs.pop("endpoint_url", None) or hf_kwargs.pop("base_url", None)
            ep_args: dict[str, Any] = {}
            if token:
                ep_args["huggingfacehub_api_token"] = token
            if endpoint_url:
                ep_args["endpoint_url"] = endpoint_url
            else:
                ep_args["repo_id"] = model_name
            endpoint = HuggingFaceEndpoint(**ep_args, **hf_kwargs)
            return ChatHuggingFace(llm=endpoint)
        except Exception as hf_err:
            logger.debug("HuggingFaceEndpoint initialization skipped (%s); trying standard init", hf_err)

    try:
        if target_provider:
            return init_chat_model(model_name, model_provider=target_provider, **init_kwargs)
        return init_chat_model(model_name, **init_kwargs)
    except ImportError as e:
        package_map = {
            "anthropic": "langchain-anthropic",
            "openai": "langchain-openai",
            "openai_codex": "langchain-openai",
            "azure_openai": "langchain-openai",
            "google_genai": "langchain-google-genai",
            "google_vertexai": "langchain-google-vertexai",
            "google_anthropic_vertex": "langchain-google-vertexai",
            "bedrock": "langchain-aws",
            "bedrock_converse": "langchain-aws",
            "anthropic_bedrock": "langchain-aws",
            "groq": "langchain-groq",
            "deepseek": "langchain-deepseek",
            "mistralai": "langchain-mistralai",
            "cohere": "langchain-cohere",
            "fireworks": "langchain-fireworks",
            "together": "langchain-together",
            "xai": "langchain-xai",
            "ollama": "langchain-ollama",
            "openrouter": "langchain-openrouter",
            "perplexity": "langchain-perplexity",
            "nvidia": "langchain-nvidia-ai-endpoints",
            "huggingface": "langchain-huggingface",
            "baseten": "langchain-baseten",
            "ibm": "langchain-ibm",
            "litellm": "langchain-litellm",
            "meta": "langchain-meta",
            "typesafe": "langchain-typesafe",
        }
        package = package_map.get(provider, f"langchain-{provider}")
        module_name = package.replace("-", "_")
        try:
            spec_found = importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError):
            spec_found = False
        if spec_found:
            raise ModelConfigError(
                f"Provider package '{package}' is installed but failed to import for provider '{provider}': {e}"
            ) from e
        raise MissingProviderPackageError(
            f"Missing package for '{provider}'. Install: pip install {package}",
            provider=provider,
            package=package,
        ) from e
    except (ValueError, TypeError) as e:
        raise ModelConfigError(f"Invalid configuration for '{provider}:{model_name}': {e}") from e


_MODEL_CACHE: dict[str, ModelResult] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def clear_model_cache() -> None:
    """Clear cached chat model instances."""
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.clear()


def create_model(
    model_spec: str | None = None,
    *,
    extra_kwargs: dict[str, Any] | None = None,
    profile_overrides: dict[str, Any] | None = None,
) -> ModelResult:
    """Factory function to build a BaseChatModel and return ModelResult."""
    if not model_spec:
        model_spec = _get_default_model_spec()

    cache_key = json.dumps(
        {
            "spec": model_spec,
            "extra": extra_kwargs or {},
            "profile": profile_overrides or {},
        },
        sort_keys=True,
        default=str,
    )
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached

    inferred_provider = detect_provider(model_spec)
    parsed = ModelSpec.try_parse(model_spec)
    config = ModelConfig.load()
    if parsed and parsed.provider in config.providers:
        provider = parsed.provider
        model_name = parsed.model
    elif inferred_provider == "bedrock":
        provider = inferred_provider
        model_name = parsed.model if (parsed and parsed.provider == "bedrock") else model_spec
    elif parsed:
        provider = parsed.provider
        model_name = parsed.model
    else:
        model_name = model_spec
        provider = inferred_provider or ""

    if provider == "google_vertexai" and model_name and model_name.lower().startswith("claude-"):
        raise ModelConfigError(
            f"Claude model '{model_name}' on Google Vertex AI requires provider 'google_anthropic_vertex' instead of 'google_vertexai' (e.g. google_anthropic_vertex:{model_name})."
        )

    if provider:
        apply_stored_credentials(provider)

    if provider and provider not in IMPLICIT_AUTH_PROVIDERS:
        cred_status = has_provider_credentials(provider)
        if cred_status is False:
            env_var = get_credential_env_var(provider)
            raise MissingCredentialsError(
                f"No credentials configured for provider '{provider}'. Set environment variable {env_var} or configure it in Settings.",
                provider=provider,
                env_var=env_var,
            )

    kwargs = _get_provider_kwargs(provider, model_name=model_name)
    if provider:
        from deepagents.profiles.provider import apply_provider_profile

        spec = f"{provider}:{model_name}" if model_name else provider
        try:
            kwargs = apply_provider_profile(spec, kwargs)
        except Exception as exc:
            logger.debug("ProviderProfile resolution for %r failed: %s", spec, exc)

    reasoning_effort_override: object = None
    reasoning_override: object = None
    if extra_kwargs:
        extra_kwargs = dict(extra_kwargs)
        reasoning_effort_override = extra_kwargs.pop("reasoning_effort", None)
        reasoning_override = extra_kwargs.pop("reasoning", None)
        eff = reasoning_effort_override
        if eff is not None:
            from opscloud.model.reasoning import is_effort_supported_for_model, with_effort_model_params

            spec = f"{provider}:{model_name}" if model_name else (model_name or "")
            if is_effort_supported_for_model(spec, str(eff)):
                kwargs = with_effort_model_params(spec, kwargs, str(eff))
        kwargs.update(extra_kwargs)

    kwargs = _compose_openai_reasoning_effort(
        provider,
        kwargs,
        reasoning_effort_override,
        reasoning_override,
    )

    if provider == "anthropic":
        # Ensure fixed-budget 'enabled' thinking is never sent for Claude 5+ models
        thinking = kwargs.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            match = re.match(r"^claude-[a-z]+-(\d+)", model_name.lower())
            if match is not None and int(match.group(1)) >= 5:
                kwargs.pop("thinking", None)

    # Sanitize provider-native models that disallow non-literal reasoning_effort strings (e.g. 'off')
    if kwargs.get("reasoning_effort") in ("off", "none", "clear", "0", "reset"):
        kwargs.pop("reasoning_effort", None)
    if provider in ("google_genai", "google", "google_vertexai"):
        spec = f"{provider}:{model_name}" if model_name else (provider or "")
        from opscloud.model.reasoning import supported_efforts_for_model
        if not supported_efforts_for_model(spec) or not kwargs.get("reasoning_effort"):
            kwargs.pop("reasoning_effort", None)
            kwargs.pop("thinking_level", None)
            kwargs.pop("thinking_budget", None)
            kwargs.pop("include_thoughts", None)

    config = ModelConfig.load()
    class_path = config.get_class_path(provider) if provider else None

    logger.info(
        "Instantiating ChatModel '%s' (provider=%s)",
        model_name,
        provider,
        extra={
            "model": model_name,
            "provider": provider,
            "reasoning_effort": kwargs.get("reasoning_effort"),
            "temperature": kwargs.get("temperature", 0.0),
        },
    )

    if class_path:
        model = _create_model_from_class(class_path, model_name, provider, kwargs)
    else:
        model = _create_model_via_init(model_name, provider, kwargs)

    # Resolve model profile & context limits
    spec_key = f"{provider}:{model_name}" if provider else model_name
    prof_entry = get_model_profile(spec_key)
    profile: dict[str, Any] = dict(prof_entry["profile"]) if prof_entry else {}
    if profile_overrides:
        profile.update(cast(dict[str, Any], profile_overrides))

    context_limit = profile.get("max_input_tokens")
    unsupported_modalities = frozenset(profile.get("unsupported_modalities", []))

    if profile:
        try:
            setattr(model, "profile", dict(profile))
        except Exception:
            with contextlib.suppress(Exception):
                setattr(type(model), "profile", property(lambda self: dict(profile)))

    result = ModelResult(
        model=model,
        model_name=model_name,
        provider=provider or getattr(model, "_model_provider", ""),
        context_limit=context_limit,
        unsupported_modalities=unsupported_modalities,
    )
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE[cache_key] = result
    return result
