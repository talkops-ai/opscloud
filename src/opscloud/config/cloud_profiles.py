"""AWS profile discovery, inspection, and switching for OpsCloud."""

from __future__ import annotations

import configparser
from opscloud.utils.logger import get_logger
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opscloud.config.settings import get_settings, sync_aws_env_aliases
from opscloud.config.toml_config import (
    clear_aws_region,
    load_aws_profile,
    load_aws_region,
    load_recent_aws_profiles,
    save_aws_profile,
    save_aws_region,
    save_recent_aws_profile,
)

logger = get_logger(__name__)


@dataclass
class AWSProfileInfo:
    """Metadata for an AWS named profile."""

    name: str
    region: str | None = None
    role_arn: str | None = None
    source_profile: str | None = None
    account_id: str | None = None
    is_sso: bool = False
    has_keys: bool = False

    @property
    def display_details(self) -> str:
        """Formatted description for UI cards and selectors."""
        parts: list[str] = []
        if self.region:
            parts.append(f"region: {self.region}")
        if self.account_id:
            parts.append(f"acct: {self.account_id}")
        elif self.role_arn:
            parts.append("role assumed")
        elif self.is_sso:
            parts.append("SSO")
        elif self.has_keys:
            parts.append("IAM keys")
        return ", ".join(parts) if parts else "configured"


_cached_profiles: list[AWSProfileInfo] | None = None
_cached_mtimes: tuple[int, int] = (-1, -1)
_profile_info_cache: dict[str, tuple[tuple[int, int], AWSProfileInfo]] = {}


def _get_aws_mtimes() -> tuple[int, int]:
    """Return mtimes of ~/.aws/config and ~/.aws/credentials for cache validation."""
    aws_dir = Path.home() / ".aws"
    config_file = aws_dir / "config"
    creds_file = aws_dir / "credentials"
    cfg_m = config_file.stat().st_mtime_ns if config_file.exists() else 0
    cred_m = creds_file.stat().st_mtime_ns if creds_file.exists() else 0
    return (cfg_m, cred_m)


def invalidate_aws_profile_cache() -> None:
    """Clear cached AWS profile discoveries and metadata."""
    global _cached_profiles, _cached_mtimes, _profile_info_cache
    _cached_profiles = None
    _cached_mtimes = (-1, -1)
    _profile_info_cache.clear()


def list_aws_profiles(*, force_refresh: bool = False) -> list[AWSProfileInfo]:
    """Discover all AWS named profiles on the system with in-memory caching.

    Discovers profiles using:
    1. `aws configure list-profiles` via AWS CLI if available.
    2. Parsing `~/.aws/config` and `~/.aws/credentials`.
    3. `botocore.session.Session().available_profiles` if botocore is available.

    Returns a deduplicated list of AWSProfileInfo objects with 'default' first
    if present.
    """
    global _cached_profiles, _cached_mtimes
    mtimes = _get_aws_mtimes()
    if not force_refresh and _cached_profiles is not None and _cached_mtimes == mtimes:
        return list(_cached_profiles)

    discovered_names: list[str] = []

    # 1. AWS CLI list-profiles
    try:
        proc = subprocess.run(
            ["aws", "configure", "list-profiles"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            for line in proc.stdout.splitlines():
                name = line.strip()
                if name and name not in discovered_names:
                    discovered_names.append(name)
    except Exception as exc:
        logger.debug("Failed running 'aws configure list-profiles': %s", exc)

    # 2. Local ~/.aws/config & ~/.aws/credentials
    aws_dir = Path.home() / ".aws"
    config_file = aws_dir / "config"
    creds_file = aws_dir / "credentials"

    cfg_parser = configparser.RawConfigParser()
    if config_file.exists():
        try:
            cfg_parser.read(config_file)
            for section in cfg_parser.sections():
                sec_clean = section.strip()
                if sec_clean == "default":
                    if "default" not in discovered_names:
                        discovered_names.append("default")
                elif sec_clean.startswith("profile "):
                    pname = sec_clean[len("profile ") :].strip()
                    if pname and pname not in discovered_names:
                        discovered_names.append(pname)
                elif not sec_clean.startswith("sso-session "):
                    if sec_clean and sec_clean not in discovered_names:
                        discovered_names.append(sec_clean)
        except Exception as exc:
            logger.debug("Error reading ~/.aws/config: %s", exc)

    cred_parser = configparser.RawConfigParser()
    if creds_file.exists():
        try:
            cred_parser.read(creds_file)
            for section in cred_parser.sections():
                pname = section.strip()
                if pname and pname not in discovered_names:
                    discovered_names.append(pname)
        except Exception as exc:
            logger.debug("Error reading ~/.aws/credentials: %s", exc)

    # 3. Fallback to botocore available profiles
    if not discovered_names:
        try:
            import botocore.session

            sess = botocore.session.Session()
            for pname in sess.available_profiles:
                if pname not in discovered_names:
                    discovered_names.append(pname)
        except Exception as exc:
            logger.debug("botocore session profile discovery skipped: %s", exc)

    # Ensure "default" is always present if no profiles found at all
    if not discovered_names:
        discovered_names = ["default"]

    # Normalize order: put "default" first if it exists
    if "default" in discovered_names and discovered_names[0] != "default":
        discovered_names.remove("default")
        discovered_names.insert(0, "default")

    # Build rich profile metadata
    results: list[AWSProfileInfo] = []
    for name in discovered_names:
        info = _inspect_profile_metadata(name, cfg_parser, cred_parser)
        results.append(info)
        _profile_info_cache[info.name] = (mtimes, info)

    _cached_profiles = list(results)
    _cached_mtimes = mtimes
    return results


def _inspect_profile_metadata(
    name: str,
    cfg_parser: configparser.RawConfigParser,
    cred_parser: configparser.RawConfigParser,
) -> AWSProfileInfo:
    """Extract metadata (region, role_arn, account_id, SSO) for a single profile."""
    # Check both "profile <name>" and bare "<name>" section headers in config
    sec_candidates = [f"profile {name}", name] if name != "default" else ["default"]
    sec_name = next((s for s in sec_candidates if cfg_parser.has_section(s)), sec_candidates[0])

    region: str | None = None
    role_arn: str | None = None
    source_profile: str | None = None
    account_id: str | None = None
    is_sso = False
    has_keys = False

    if cfg_parser.has_section(sec_name):
        region = cfg_parser.get(sec_name, "region", fallback=None)
        role_arn = cfg_parser.get(sec_name, "role_arn", fallback=None)
        source_profile = cfg_parser.get(sec_name, "source_profile", fallback=None)
        if cfg_parser.has_option(sec_name, "sso_session") or cfg_parser.has_option(sec_name, "sso_start_url"):
            is_sso = True
        sso_acct = cfg_parser.get(sec_name, "sso_account_id", fallback=None)
        if sso_acct:
            account_id = sso_acct

    # If region is not directly defined on this profile, check source_profile chain
    if not region and source_profile:
        curr_source = source_profile
        visited = {name, curr_source}
        for _ in range(5):  # prevent infinite loop in cyclic configs
            src_candidates = [f"profile {curr_source}", curr_source] if curr_source != "default" else ["default"]
            src_sec = next((s for s in src_candidates if cfg_parser.has_section(s)), None)
            if src_sec:
                src_region = cfg_parser.get(src_sec, "region", fallback=None)
                if src_region and src_region.strip():
                    region = src_region.strip()
                    break
                next_source = cfg_parser.get(src_sec, "source_profile", fallback=None)
                if next_source and next_source not in visited:
                    visited.add(next_source)
                    curr_source = next_source
                else:
                    break
            else:
                break

    # Check credentials file for static keys or region fallback
    cred_sec = name
    if cred_parser.has_section(cred_sec):
        if cred_parser.has_option(cred_sec, "aws_access_key_id"):
            has_keys = True
        if not region and cred_parser.has_option(cred_sec, "region"):
            region = cred_parser.get(cred_sec, "region", fallback=None)

    # Parse account ID from role_arn if present (arn:aws:iam::123456789012:role/...)
    if role_arn and not account_id:
        match = re.search(r"arn:aws(?:-[a-z]+)?:iam::(\d{12}):role/", role_arn)
        if match:
            account_id = match.group(1)

    return AWSProfileInfo(
        name=name,
        region=region.strip() if region else None,
        role_arn=role_arn,
        source_profile=source_profile,
        account_id=account_id,
        is_sso=is_sso,
        has_keys=has_keys,
    )


def get_aws_profile_info(name: str, *, force_refresh: bool = False) -> AWSProfileInfo:
    """Read ~/.aws/config and ~/.aws/credentials to extract metadata for a single profile."""
    clean_name = name.strip()
    mtimes = _get_aws_mtimes()

    if not force_refresh and clean_name in _profile_info_cache:
        cached_mtimes, cached_info = _profile_info_cache[clean_name]
        if cached_mtimes == mtimes:
            return cached_info

    aws_dir = Path.home() / ".aws"
    config_file = aws_dir / "config"
    creds_file = aws_dir / "credentials"

    cfg_parser = configparser.RawConfigParser()
    if config_file.exists():
        try:
            cfg_parser.read(config_file)
        except Exception:
            pass

    cred_parser = configparser.RawConfigParser()
    if creds_file.exists():
        try:
            cred_parser.read(creds_file)
        except Exception:
            pass

    info = _inspect_profile_metadata(clean_name, cfg_parser, cred_parser)
    _profile_info_cache[clean_name] = (mtimes, info)
    return info


def get_active_aws_profile() -> str:
    """Return the currently active AWS profile name.

    Resolution order:
    1. AWS_PROFILE or AWS_DEFAULT_PROFILE environment variable.
    2. settings.aws_profile (if configured).
    3. Persisted profile in config.toml via load_aws_profile().
    4. Fallback to 'default'.
    """
    env_prof = os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE")
    if env_prof and env_prof.strip():
        return env_prof.strip()

    persisted = load_aws_profile()
    if persisted and persisted.strip():
        return persisted.strip()

    try:
        s = get_settings()
        if getattr(s, "aws_profile", None) and s.aws_profile != "default":
            return s.aws_profile
    except Exception:
        pass

    return "default"


def set_active_aws_profile(
    profile: str,
    *,
    region: str | None = None,
    persist: bool = True,
) -> None:
    """Activate an AWS named profile for the current process and future sessions.

    Sets environment variables, synchronizes aliases, updates runtime settings,
    and optionally persists to config.toml so it stays active across sessions.
    """
    clean_profile = profile.strip()
    if not clean_profile:
        return

    os.environ["AWS_PROFILE"] = clean_profile
    os.environ["AWS_DEFAULT_PROFILE"] = clean_profile

    # Determine region:
    # 1. Explicit region arg passed in (e.g. from modal selection or CLI)
    # 2. Metadata from ~/.aws/config / ~/.aws/credentials (including source_profile chain)
    # 3. For 'default' profile only, check persisted config.toml
    effective_region = region.strip() if region and region.strip() else None
    if not effective_region:
        info = get_aws_profile_info(clean_profile)
        if info and info.region:
            effective_region = info.region

    if not effective_region and clean_profile == "default":
        effective_region = load_aws_region()

    if effective_region:
        os.environ["AWS_REGION"] = effective_region
        os.environ["AWS_DEFAULT_REGION"] = effective_region
    else:
        # Crucial: do NOT leave stale region from previous profile in environment
        os.environ.pop("AWS_REGION", None)
        os.environ.pop("AWS_DEFAULT_REGION", None)

    sync_aws_env_aliases()

    try:
        s = get_settings()
        s.aws_profile = clean_profile
        s.aws_region = effective_region or "us-east-1"
    except Exception:
        pass

    if persist:
        save_aws_profile(clean_profile)
        save_recent_aws_profile(clean_profile)
        if effective_region:
            save_aws_region(effective_region)
        else:
            clear_aws_region()

    invalidate_aws_profile_cache()
    logger.info("Activated AWS profile: %s (region=%s, persist=%s)", clean_profile, effective_region, persist)


def get_active_aws_region() -> str | None:
    """Return the active AWS region for the currently active profile.

    Resolution order:
    - For named profile (non-default):
      1. Profile's configured region in ~/.aws/config or credentials.
      2. Persisted region in config.toml via load_aws_region().
      3. AWS_REGION or AWS_DEFAULT_REGION environment variable.
      4. settings.aws_region (if explicitly configured and not default "us-east-1").
    - For 'default' profile:
      1. AWS_REGION or AWS_DEFAULT_REGION environment variable.
      2. Persisted region in config.toml via load_aws_region().
      3. [default] region from ~/.aws/config.
      4. settings.aws_region (if explicitly configured and not default "us-east-1").
    """
    active_prof = get_active_aws_profile()

    if active_prof != "default":
        # Named profile: profile's own configuration is primary
        try:
            info = get_aws_profile_info(active_prof)
            if info and info.region:
                return info.region
        except Exception:
            pass

    # For default profile, environment variable override takes highest priority
    env_reg = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if env_reg and env_reg.strip():
        return env_reg.strip()

    persisted = load_aws_region()
    if persisted and persisted.strip():
        return persisted.strip()

    # Fallback to [default] profile inspection
    try:
        info_default = get_aws_profile_info("default")
        if info_default and info_default.region:
            return info_default.region
    except Exception:
        pass

    try:
        s = get_settings()
        if getattr(s, "aws_region", None) and s.aws_region.strip() and s.aws_region != "us-east-1":
            return s.aws_region.strip()
    except Exception:
        pass

    return None


__all__ = [
    "AWSProfileInfo",
    "get_active_aws_profile",
    "get_active_aws_region",
    "get_aws_profile_info",
    "invalidate_aws_profile_cache",
    "list_aws_profiles",
    "set_active_aws_profile",
]
