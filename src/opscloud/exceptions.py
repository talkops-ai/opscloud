"""Exception hierarchy for opscloud."""

from __future__ import annotations


class OpsCloudError(Exception):
    """Base exception for all opscloud errors."""


class ServerStartupError(OpsCloudError):
    """Raised when the LangGraph dev server fails to start or become healthy."""


class ModelConfigError(OpsCloudError):
    """Model configuration is invalid or incomplete."""


class MissingCredentialsError(ModelConfigError):
    """Required API credentials are not configured."""

    def __init__(self, message: str, *, provider: str = "", env_var: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.env_var = env_var


class MissingProviderPackageError(ModelConfigError):
    """Required LangChain provider package is not installed."""

    def __init__(self, message: str, *, provider: str = "", package: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.package = package


class NoCredentialsConfiguredError(MissingCredentialsError):
    """No credentials configured for any auto-detectable provider."""

    def __init__(self, message: str) -> None:
        super().__init__(message, provider="", env_var=None)


class SkillExecutionError(OpsCloudError):
    """Raised when skill loading or execution fails."""


class PluginError(OpsCloudError):
    """Raised when plugin resolution or installation fails."""


class DestructiveActionRejectedError(OpsCloudError):
    """Raised when a human rejects a gated destructive cloud action."""


__all__ = [
    "OpsCloudError",
    "ServerStartupError",
    "ModelConfigError",
    "MissingCredentialsError",
    "MissingProviderPackageError",
    "NoCredentialsConfiguredError",
    "SkillExecutionError",
    "PluginError",
    "DestructiveActionRejectedError",
]
