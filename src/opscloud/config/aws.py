"""AWS runtime context, credential discovery, and model SDK configuration for OpsCloud."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from opscloud.config.settings import resolve_env_var, sync_aws_env_aliases

logger = get_logger(__name__)


# ── Canonical AWS Source Definitions (aligned with dcode) ───

AWS_CREDENTIAL_ENV_SOURCES: dict[str, tuple[str, ...]] = {
    "profile_name": ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"),
    "aws_access_key_id": ("AWS_ACCESS_KEY_ID",),
    "aws_secret_access_key": ("AWS_SECRET_ACCESS_KEY",),
    "aws_session_token": ("AWS_SESSION_TOKEN",),
}
"""Canonical AWS credential sources, keyed by the `boto3.Session` argument."""

AWS_REGION_ENV_SOURCES: tuple[str, ...] = ("AWS_REGION", "AWS_DEFAULT_REGION")
"""Canonical AWS region sources, in resolution order."""

AWS_MODEL_SDK_ENV_KWARGS: dict[str, tuple[str, ...]] = {
    "region_name": AWS_REGION_ENV_SOURCES,
    "credentials_profile_name": AWS_CREDENTIAL_ENV_SOURCES["profile_name"],
    "aws_access_key_id": ("AWS_ACCESS_KEY_ID",),
    "aws_secret_access_key": ("AWS_SECRET_ACCESS_KEY",),
    "aws_session_token": ("AWS_SESSION_TOKEN",),
}
"""AWS model environment values accepted directly by LangChain Bedrock constructors."""


# ── Environment & Keyword Mapping ────────────────────────


def resolve_env_kwargs(
    table: Mapping[str, tuple[str, ...]],
    lookup: Callable[[str], str | None] | None = None,
) -> dict[str, str]:
    """Translate an argument/env-name table into explicit constructor kwargs.

    The first name that `lookup` resolves to a non-empty value wins.
    """
    resolver = lookup or resolve_env_var
    resolved: dict[str, str] = {}
    for argument, env_names in table.items():
        value = next((val for name in env_names if (val := resolver(name))), None)
        if value:
            resolved[argument] = value
    return resolved


def validate_aws_credentials(
    lookup: Callable[[str], str | None] | None = None,
) -> tuple[bool, str | None]:
    """Validate completeness of explicit AWS credentials (matches dcode pattern).

    Returns:
        (is_valid, error_message)
    """
    resolver = lookup or (lambda k: os.environ.get(f"OPSCLOUD_{k}") or os.environ.get(k))
    resolved = resolve_env_kwargs(AWS_CREDENTIAL_ENV_SOURCES, resolver)
    key_id = resolved.get("aws_access_key_id")
    secret = resolved.get("aws_secret_access_key")

    if bool(key_id) != bool(secret):
        missing = "AWS_SECRET_ACCESS_KEY" if key_id else "AWS_ACCESS_KEY_ID"
        return (
            False,
            f"AWS credentials configuration is incomplete: {missing} is not set. "
            f"Set both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or unset both "
            f"to use AWS_PROFILE or default IAM credentials.",
        )

    if resolved.get("aws_session_token") and not key_id:
        return (
            False,
            "AWS configuration is incomplete: AWS_SESSION_TOKEN is set without "
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. Set all three together, "
            "or unset AWS_SESSION_TOKEN.",
        )

    return True, None


@dataclass
class AWSIdentity:
    """Represents resolved AWS authentication and caller identity."""

    account_id: str | None = None
    arn: str | None = None
    user_id: str | None = None
    region: str = "us-east-1"
    profile: str = "default"
    is_authenticated: bool = False
    error: str | None = None

    @property
    def authenticated(self) -> bool:
        """Alias for is_authenticated."""
        return self.is_authenticated


def get_boto3_session(*, profile: str | None = None, region: str | None = None) -> Any:
    """Create a boto3.Session respecting environment variables and explicit profiles."""
    import boto3

    sync_aws_env_aliases()

    # Early check for incomplete credential pairs
    is_valid, err = validate_aws_credentials()
    if not is_valid and err:
        logger.warning(err)

    from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region

    effective_profile = profile or get_active_aws_profile()
    effective_region = region or get_active_aws_region() or "us-east-1"

    session_kwargs: dict[str, Any] = {"region_name": effective_region}
    if profile:
        session_kwargs["profile_name"] = profile
    elif effective_profile and effective_profile != "default":
        session_kwargs["profile_name"] = effective_profile

    try:
        return boto3.Session(**session_kwargs)
    except Exception:
        session_kwargs.pop("profile_name", None)
        saved_prof = os.environ.pop("AWS_PROFILE", None)
        saved_def_prof = os.environ.pop("AWS_DEFAULT_PROFILE", None)
        try:
            return boto3.Session(**session_kwargs)
        except Exception:
            return boto3.Session()
        finally:
            if saved_prof is not None:
                os.environ["AWS_PROFILE"] = saved_prof
            if saved_def_prof is not None:
                os.environ["AWS_DEFAULT_PROFILE"] = saved_def_prof


def resolve_aws_identity(*, profile: str | None = None, region: str | None = None) -> AWSIdentity:
    """Query AWS STS get_caller_identity safely via boto3."""
    sync_aws_env_aliases()

    from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region

    effective_profile = profile or get_active_aws_profile()
    effective_region = region or get_active_aws_region() or "us-east-1"

    try:
        session = get_boto3_session(profile=profile, region=region)
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        return AWSIdentity(
            account_id=identity.get("Account"),
            arn=identity.get("Arn"),
            user_id=identity.get("UserId"),
            region=session.region_name or effective_region,
            profile=effective_profile,
            is_authenticated=True,
        )
    except Exception as exc:
        logger.debug("Failed to resolve AWS identity: %s", exc)
        return AWSIdentity(
            region=effective_region,
            profile=effective_profile,
            is_authenticated=False,
            error=str(exc),
        )


def get_aws_credentials_dict(*, profile: str | None = None, region: str | None = None) -> dict[str, str]:
    """Extract resolved AWS credentials as a dictionary for subagent or tool consumption."""
    try:
        session = get_boto3_session(profile=profile, region=region)
        creds = session.get_credentials()
        if not creds:
            return {}
        frozen = creds.get_frozen_credentials()
        result: dict[str, str] = {
            "AWS_ACCESS_KEY_ID": frozen.access_key,
            "AWS_SECRET_ACCESS_KEY": frozen.secret_key,
            "AWS_REGION": session.region_name or "us-east-1",
            "AWS_DEFAULT_REGION": session.region_name or "us-east-1",
        }
        if frozen.token:
            result["AWS_SESSION_TOKEN"] = frozen.token
        return result
    except Exception as exc:
        logger.debug("Could not extract AWS credentials dict: %s", exc)
        return {}


def get_aws_model_kwargs(settings: Any | None = None) -> dict[str, Any]:
    """Extract AWS Bedrock model constructor kwargs from settings and environment."""
    from opscloud.config.settings import get_settings

    s = settings or get_settings()
    resolved = resolve_env_kwargs(AWS_MODEL_SDK_ENV_KWARGS, resolve_env_var)

    kwargs: dict[str, Any] = {}
    region = resolved.get("region_name") or getattr(s, "aws_region", None) or "us-east-1"
    kwargs["region_name"] = region

    profile = resolved.get("credentials_profile_name") or getattr(s, "aws_profile", None)
    if profile and profile != "default":
        kwargs["credentials_profile_name"] = profile

    key_id = resolved.get("aws_access_key_id") or getattr(s, "aws_access_key_id", None)
    secret = resolved.get("aws_secret_access_key") or getattr(s, "aws_secret_access_key", None)
    token = resolved.get("aws_session_token") or getattr(s, "aws_session_token", None)

    if key_id and secret:
        kwargs["aws_access_key_id"] = key_id
        kwargs["aws_secret_access_key"] = secret
        if token:
            kwargs["aws_session_token"] = token

    return kwargs


AWSConfig = AWSIdentity
get_aws_config = resolve_aws_identity
get_aws_context = resolve_aws_identity

__all__ = [
    "AWSConfig",
    "AWSIdentity",
    "AWS_CREDENTIAL_ENV_SOURCES",
    "AWS_MODEL_SDK_ENV_KWARGS",
    "AWS_REGION_ENV_SOURCES",
    "get_aws_config",
    "get_aws_context",
    "get_aws_credentials_dict",
    "get_aws_model_kwargs",
    "get_boto3_session",
    "resolve_aws_identity",
    "resolve_env_kwargs",
    "validate_aws_credentials",
]
