"""Model creation and configuration package for opscloud."""

from opscloud.model.config import (
    MODEL_PROFILES,
    PROVIDER_API_KEY_ENV,
    PROVIDER_BASE_URL_ENV,
    ModelConfig,
    ModelSpec,
    apply_stored_credentials,
    clear_default_model,
    detect_provider,
    load_default_model,
    normalize_model_spec,
    save_default_model,
)
from opscloud.model.factory import (
    ModelResult,
    clear_model_cache,
    create_model,
)

__all__ = [
    "ModelConfig",
    "ModelSpec",
    "ModelResult",
    "apply_stored_credentials",
    "clear_default_model",
    "clear_model_cache",
    "create_model",
    "detect_provider",
    "load_default_model",
    "normalize_model_spec",
    "save_default_model",
    "PROVIDER_API_KEY_ENV",
    "PROVIDER_BASE_URL_ENV",
    "MODEL_PROFILES",
]
