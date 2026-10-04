"""SHA-256 trust store for project-level MCP configs.

Before loading a project-level ``.mcp.json``, the trust store checks
whether the file's SHA-256 fingerprint has been previously approved.
This prevents untrusted configs from spawning processes.

Usage::

    from opscloud.mcp.trust import MCPTrustStore

    trust = MCPTrustStore()
    if trust.is_trusted(config_path):
        # load and use
    else:
        # prompt user to trust
        trust.trust(config_path)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from opscloud.config import paths
from opscloud.utils.logger import AgentLogger

logger = AgentLogger("MCPTrust")


class MCPTrustStore:
    """SHA-256 trust gating for project-level MCP configuration files.

    Trust state is persisted as a JSON file at
    ``~/.opscloud/.state/mcp_trust.json``.
    """

    TRUST_FILE = paths.MCP_TRUST_PATH

    def __init__(self, trust_file: Path | None = None) -> None:
        """Initialize MCPTrustStore with trust persistence file.

        Args:
            trust_file: Optional explicit path to the JSON trust store file.
        """
        self._trust_file = trust_file or self.TRUST_FILE
        self._trusted: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        """Load trusted fingerprints from disk."""
        if self._trust_file.exists():
            try:
                data = json.loads(self._trust_file.read_text(encoding="utf-8"))
                return data.get("trusted", {})
            except Exception as e:
                logger.warning(f"Failed to load MCP trust store at {self._trust_file}: {e}")
        return {}

    def _save(self) -> None:
        """Persist trusted fingerprints to disk."""
        self._trust_file.parent.mkdir(parents=True, exist_ok=True)
        self._trust_file.write_text(
            json.dumps({"trusted": self._trusted}, indent=2),
            encoding="utf-8",
        )

    def _fingerprint(self, config_path: Path) -> str:
        """Compute SHA-256 of a config file."""
        return hashlib.sha256(config_path.read_bytes()).hexdigest()

    def is_trusted(self, config_path: Path) -> bool:
        """Check if a config file's current content is trusted.

        Returns True if the file's SHA-256 matches a previously
        trusted fingerprint.
        """
        if not config_path.exists():
            return False

        key = str(config_path.resolve())
        current = self._fingerprint(config_path)
        return self._trusted.get(key) == current

    def trust(self, config_path: Path) -> None:
        """Mark a config file's current content as trusted.

        Stores the SHA-256 fingerprint so future ``is_trusted()``
        checks pass.
        """
        if not config_path.exists():
            logger.warning(f"Cannot trust non-existent file: {config_path}")
            return

        key = str(config_path.resolve())
        self._trusted[key] = self._fingerprint(config_path)
        self._save()
        logger.info(f"Trusted MCP config: {config_path}")

    def revoke(self, config_path: Path) -> None:
        """Revoke trust for a config file."""
        key = str(config_path.resolve())
        if key in self._trusted:
            del self._trusted[key]
            self._save()
            logger.info(f"Revoked trust for MCP config: {config_path}")
