"""User identity resolution and cloud RBAC authorization mapping.

Resolves terminal operators and external callers to internal identities,
AWS IAM/cloud authorization roles, and scoped resource boundaries.

Identity mappings are loaded from:

1. ``IDENTITY_MAPPING_FILE`` env var — path to a JSON file.
2. ``IDENTITY_MAPPING`` env var — inline JSON string.
3. Default: unmapped callers receive the ``"viewer"`` role with read-only access.

Example mapping file::

    {
        "terminal:local": {
            "internal_id": "alice@company.com",
            "display_name": "Alice",
            "rbac_role": "admin",
            "accounts": ["123456789012"],
            "regions": ["us-east-1", "us-west-2"],
            "services": ["*"],
            "operations": ["read", "deploy", "delete", "scale"]
        },
        "event_bus:ci-pipeline": {
            "internal_id": "ci-runner@company.com",
            "display_name": "CI Runner",
            "rbac_role": "operator",
            "accounts": ["123456789012"],
            "regions": ["us-east-1"],
            "services": ["ec2", "s3"],
            "operations": ["read", "deploy"]
        }
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
import getpass
import json
import os
from typing import Any

from opscloud.utils.logger import AgentLogger

logger = AgentLogger("IdentityIntegration")


@dataclass
class UserIdentity:
    """Resolved user identity with cloud authorization and boundary context.

    Attributes:
        platform: Source platform or interface (e.g. ``"terminal"``, ``"event_bus"``).
        platform_user_id: Caller identifier on that platform.
        internal_user_id: Internal corporate/IAM user identifier or email.
        display_name: Human-readable display name.
        rbac_role: Cloud authorization role (``"admin"``, ``"operator"``, ``"viewer"``).
        allowed_accounts: Permitted cloud account IDs (``["*"]`` for all).
        allowed_regions: Permitted cloud regions (``["*"]`` for all).
        allowed_services: Permitted cloud services (e.g. ``["ec2", "s3"]`` or ``["*"]``).
        allowed_operations: Permitted operations: ``"read"``, ``"deploy"``, ``"delete"``, etc.
        allowed_namespaces: Permitted Kubernetes namespaces if cluster operations are used.
    """

    platform: str = "terminal"
    platform_user_id: str = "local"
    internal_user_id: str = "local"
    display_name: str = "Terminal Operator"
    rbac_role: str = "viewer"
    allowed_accounts: list[str] = field(default_factory=lambda: ["*"])
    allowed_regions: list[str] = field(default_factory=lambda: ["*"])
    allowed_services: list[str] = field(default_factory=lambda: ["*"])
    allowed_operations: list[str] = field(default_factory=lambda: ["read"])
    allowed_namespaces: list[str] = field(default_factory=lambda: ["*"])


class IdentityMapper:
    """Resolves platform users to :class:`UserIdentity` with cloud RBAC context.

    Uses composite keys of the form ``"platform:user_id"`` (e.g. ``"terminal:local"``).
    """

    def __init__(self) -> None:
        """Initialize IdentityMapper and load configured user mappings."""
        self._mapping: dict[str, dict[str, Any]] = {}
        self._load_mapping()

    # ── Loading ───────────────────────────────────────────────────────

    def _load_mapping(self) -> None:
        """Load identity mapping from env var sources."""
        # Priority 1: JSON file
        mapping_file = os.getenv("IDENTITY_MAPPING_FILE")
        if mapping_file:
            try:
                with open(mapping_file, encoding="utf-8") as fh:
                    self._mapping = json.load(fh)
                    logger.info(f"Loaded identity mapping from file: {mapping_file} ({len(self._mapping):d} entries)")
                    return
            except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
                logger.warning(f"Failed to load identity mapping file {mapping_file}: {exc}")

        # Priority 2: Inline JSON string
        mapping_str = os.getenv("IDENTITY_MAPPING")
        if mapping_str:
            try:
                self._mapping = json.loads(mapping_str)
                logger.info(f"Loaded identity mapping from IDENTITY_MAPPING env ({len(self._mapping):d} entries)")
                return
            except json.JSONDecodeError as exc:
                logger.warning(f"Failed to parse IDENTITY_MAPPING env: {exc}")

        logger.info("No identity mapping configured — using default viewer role")

    # ── Resolution ────────────────────────────────────────────────────

    def resolve(
        self,
        platform: str = "terminal",
        platform_user_id: str | None = None,
    ) -> UserIdentity:
        """Resolve a caller identity to a :class:`UserIdentity`.

        Unmapped callers receive the ``"viewer"`` role with read-only access.

        Args:
            platform: Platform key (e.g. ``"terminal"``, ``"event_bus"``).
            platform_user_id: Platform-native or local user identifier.
                Defaults to current operating system user when platform is "terminal".

        Returns:
            Resolved :class:`UserIdentity` with cloud authorization context.
        """
        if platform_user_id is None:
            if platform == "terminal":
                try:
                    platform_user_id = getpass.getuser()
                except Exception:
                    platform_user_id = "local"
            else:
                platform_user_id = "anonymous"

        key = f"{platform}:{platform_user_id}"
        user_data = self._mapping.get(key, {})

        accounts = user_data.get("accounts") or user_data.get("allowed_accounts") or ["*"]
        regions = user_data.get("regions") or user_data.get("allowed_regions") or ["*"]
        services = user_data.get("services") or user_data.get("allowed_services") or ["*"]
        namespaces = user_data.get("namespaces") or user_data.get("allowed_namespaces") or ["*"]
        operations = user_data.get("operations") or user_data.get("allowed_operations") or ["read"]

        return UserIdentity(
            platform=platform,
            platform_user_id=platform_user_id,
            internal_user_id=user_data.get("internal_id", platform_user_id),
            display_name=user_data.get("display_name", platform_user_id.title()),
            rbac_role=user_data.get("rbac_role", "viewer"),
            allowed_accounts=list(accounts),
            allowed_regions=list(regions),
            allowed_services=list(services),
            allowed_operations=list(operations),
            allowed_namespaces=list(namespaces),
        )

    # ── Utilities ─────────────────────────────────────────────────────

    def is_mapped(self, platform: str, platform_user_id: str) -> bool:
        """Check if a user has an explicit mapping configured.

        Args:
            platform: Platform key.
            platform_user_id: User identifier.

        Returns:
            True if user is explicitly present in the mapping.
        """
        return f"{platform}:{platform_user_id}" in self._mapping


__all__ = ["IdentityMapper", "UserIdentity"]
