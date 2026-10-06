"""Plugin and marketplace configuration for OpsCloud."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Final
import urllib.request

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_GITHUB_URL_PATTERN = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)?([^/]+)/([^/]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True, slots=True)
class DefaultMarketplaceConfig:
    """Specification for an out-of-the-box default marketplace.

    Can be instantiated directly or via `from_github` using a GitHub repository slug/URL.
    """

    name: str
    repository_url: str
    manifest_url: str
    ref: str = "main"
    description: str = ""

    @classmethod
    def from_github(
        cls,
        repo: str,
        *,
        name: str | None = None,
        ref: str = "main",
        manifest_path: str = ".claude-plugin/marketplace.json",
        description: str = "",
    ) -> DefaultMarketplaceConfig:
        """Create a default marketplace configuration from a GitHub repository slug or URL.

        Args:
            repo: GitHub repo in format "owner/repo" or "https://github.com/owner/repo.git".
            name: Optional marketplace identifier. Defaults to "{owner}-{repo}".
            ref: Git ref / branch (default: "main").
            manifest_path: Relative path to marketplace.json in repository.
            description: Optional human-readable description.

        Returns:
            A frozen DefaultMarketplaceConfig instance.
        """
        match = _GITHUB_URL_PATTERN.match(repo.strip())
        if not match:
            msg = f"Invalid GitHub repository specifier: {repo!r}. Expected 'owner/repo' or GitHub URL."
            raise ValueError(msg)
        owner, repo_name = match.group(1), match.group(2)
        resolved_name = name or f"{owner.replace('-ai', '')}-{repo_name}"
        clean_manifest = manifest_path.strip().lstrip("/")
        return cls(
            name=resolved_name,
            repository_url=f"https://github.com/{owner}/{repo_name}.git",
            manifest_url=f"https://raw.githubusercontent.com/{owner}/{repo_name}/{ref}/{clean_manifest}",
            ref=ref,
            description=description,
        )


# Generic list of default marketplace repositories registered out-of-the-box.
# To make any additional marketplace available by default, simply append its configuration here.
DEFAULT_MARKETPLACE_CONFIGS: Final[tuple[DefaultMarketplaceConfig, ...]] = (
    DefaultMarketplaceConfig.from_github(
        "talkops-ai/devops-plugins",
        name="talkops-devops-plugins",
        ref="main",
        description="Official TalkOps marketplace for cloud operations and platform engineering plugins.",
    ),
)


def get_default_marketplace_config(name: str) -> DefaultMarketplaceConfig | None:
    """Find a default marketplace configuration by name."""
    for config in DEFAULT_MARKETPLACE_CONFIGS:
        if config.name == name:
            return config
    return None


def is_default_marketplace(name: str) -> bool:
    """Check if a marketplace name matches any configured default marketplace."""
    return get_default_marketplace_config(name) is not None


def get_primary_default_marketplace_name() -> str:
    """Return the name of the primary default marketplace, or 'marketplace' if none configured."""
    return DEFAULT_MARKETPLACE_CONFIGS[0].name if DEFAULT_MARKETPLACE_CONFIGS else "marketplace"


def get_default_marketplace_names() -> frozenset[str]:
    """Return the set of all configured default marketplace names."""
    return frozenset(config.name for config in DEFAULT_MARKETPLACE_CONFIGS)


def fetch_marketplace_manifest(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Fetch a marketplace manifest JSON directly from a remote URL.

    Args:
        url: Remote URL of the marketplace.json manifest.
        timeout: HTTP request timeout in seconds.

    Returns:
        Parsed JSON dictionary of the marketplace manifest.

    Raises:
        urllib.error.URLError: If the remote catalog cannot be fetched.
        json.JSONDecodeError: If the remote content is not valid JSON.
        ValueError: If the remote response is not a JSON object.
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "opscloud-plugin-manager", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        msg = f"Marketplace manifest from {url} must be a JSON object, got {type(data).__name__}"
        raise ValueError(msg)
    return data
