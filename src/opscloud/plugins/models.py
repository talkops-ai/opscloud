"""Data models for plugins and marketplaces in K8s Autopilot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

JsonObject = dict[str, Any]
JsonValue = Any

MarketplaceSourceType = Literal["directory", "file", "github", "git", "url"]
InstallScope = Literal["global", "user", "team", "project", "local"]
ExternalPluginRepositorySourceType = Literal["github", "git-subdir", "url"]
UnsupportedComponent = Literal["hooks"]


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalMarketplaceSource:
    """Local directory or JSON file used as a marketplace source."""

    source_type: Literal["directory", "file"]
    value: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RepositoryMarketplaceSource:
    """GitHub or Git repository used as a marketplace source."""

    source_type: Literal["github", "git"]
    value: str
    ref: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UrlMarketplaceSource:
    """Marketplace manifest downloaded from an HTTP URL."""

    source_type: Literal["url"]
    value: str


MarketplaceSource = LocalMarketplaceSource | RepositoryMarketplaceSource | UrlMarketplaceSource


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginManifest:
    """Parsed plugin manifest."""

    name: str | None
    version: str | None = "1.0.0"
    display_name: str | None = None
    description: str | None = None
    author: str | JsonObject | None = None
    component_paths: dict[str, tuple[Path, ...]] = ()  # type: ignore[assignment]
    inline_mcp: JsonObject = ()  # type: ignore[assignment]
    agents_dir: Path | None = None
    skills_dir: Path | None = None
    mcp_config: JsonObject = ()  # type: ignore[assignment]
    hooks_config: JsonObject = ()  # type: ignore[assignment]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginManifest:
        """Create a PluginManifest from a dictionary."""
        return cls(
            name=data.get("name"),
            version=data.get("version", "1.0.0"),
            display_name=data.get("display_name"),
            description=data.get("description"),
            author=data.get("author"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentInventory:
    """Inventory of supported plugin components."""

    skills: tuple[Path, ...] = ()
    mcp_files: tuple[Path, ...] = ()
    agents: tuple[Path, ...] = ()
    commands: tuple[Path, ...] = ()
    unsupported: tuple[UnsupportedComponent, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def mcp(self) -> tuple[Path, ...]:
        return self.mcp_files


@dataclass(frozen=True, kw_only=True)
class PluginInstance:
    """A discovered plugin ready to feed adapters and agent context.

    Attributes:
        plugin_id: Stable id in `{name}@{marketplace}` form.
        name: Plugin namespace name.
        marketplace: Parent marketplace used for identity and namespacing.
        version: Version declared by the plugin manifest, if any.
        root: Plugin root directory.
        data_dir: Writable data directory for this plugin.
        manifest: Parsed manifest, if any.
        inventory: Component inventory.
    """

    plugin_id: str = ""
    name: str = ""
    marketplace: str = "default"
    version: str | None = None
    root: Path = Path(".")
    data_dir: Path | None = None
    manifest: PluginManifest | None = None
    inventory: ComponentInventory = ComponentInventory()
    enabled: bool = True
    source: InstallScope = "global"

    def __init__(
        self,
        *,
        plugin_id: str = "",
        name: str = "",
        marketplace: str = "default",
        version: str | None = None,
        root: Path | None = None,
        root_dir: Path | None = None,
        data_dir: Path | None = None,
        manifest: PluginManifest | None = None,
        inventory: ComponentInventory | None = None,
        enabled: bool = True,
        source: InstallScope = "global",
    ) -> None:
        """Initialize PluginInstance with optional legacy alias support.

        Args:
            plugin_id: Identifier of the plugin.
            name: Human-readable plugin name.
            marketplace: Marketplace source identifier.
            version: Installed plugin version string.
            root: Root path of the plugin directory.
            root_dir: Alias for root directory path.
            data_dir: Plugin data storage path.
            manifest: Parsed plugin manifest.
            inventory: Inventory of plugin components.
            enabled: Whether plugin is enabled.
            source: Installation scope ("global", "user", "project").
        """
        effective_root = root or root_dir or Path(".")
        effective_name = name or (manifest.name if manifest and manifest.name else "")
        effective_version = version or (manifest.version if manifest and manifest.version else "1.0.0")
        effective_id = plugin_id or (f"{effective_name}@{marketplace}" if effective_name else "")

        if effective_id and "@" in effective_id:
            expected = f"{effective_name}@{marketplace}"
            if effective_id != expected:
                msg = f"Plugin id {effective_id!r} does not match expected {expected!r}"
                raise ValueError(msg)

        object.__setattr__(self, "plugin_id", effective_id)
        object.__setattr__(self, "name", effective_name)
        object.__setattr__(self, "marketplace", marketplace)
        object.__setattr__(self, "version", effective_version)
        object.__setattr__(self, "root", effective_root)
        object.__setattr__(self, "data_dir", data_dir)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "inventory", inventory or ComponentInventory())
        object.__setattr__(self, "enabled", enabled)
        object.__setattr__(self, "source", source)

    @property
    def root_dir(self) -> Path:
        """Alias for root for backward compatibility."""
        return self.root


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalPluginSource:
    """A plugin stored relative to its marketplace."""

    source_type: Literal["local"]
    path: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GithubPluginSource:
    """A plugin sourced from a GitHub repository."""

    source_type: Literal["github"]
    repo: str
    ref: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class GitSubdirectoryPluginSource:
    """A plugin sourced from a subdirectory in a Git repository."""

    source_type: Literal["git-subdir"]
    url: str
    ref: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UrlPluginSource:
    """A plugin sourced from a Git repository URL or archive."""

    source_type: Literal["url"]
    url: str
    ref: str | None = None
    path: str | None = None


PluginSource = LocalPluginSource | GithubPluginSource | GitSubdirectoryPluginSource | UrlPluginSource


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketplacePluginEntry:
    """A catalog entry from a marketplace manifest."""

    name: str
    source: PluginSource
    description: str | None = None
    author: str | JsonObject | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginMarketplace:
    """A parsed marketplace manifest."""

    name: str
    root: Path
    manifest_path: Path
    metadata: JsonObject
    plugins: tuple[MarketplacePluginEntry, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketplaceRecord:
    """Persisted marketplace source record."""

    name: str
    source_type: MarketplaceSourceType
    source: str
    install_location: str
    ref: str | None = None
    plugin_count: int = 0
    is_team: bool = False
    is_project: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledPluginEntry:
    """Install record for a plugin in local state."""

    install_path: str
    version: str | None = None
    scope: InstallScope = "user"
    project_path: str | None = None
    installed_at: str | None = None
    last_updated: str | None = None
    git_commit_sha: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginDiscoveryResult:
    """Result from plugin discovery."""

    plugins: tuple[PluginInstance, ...]
    warnings: tuple[str, ...] = ()


def split_plugin_id(plugin_id: str) -> tuple[str, str]:
    """Split a plugin id in `{plugin}@{marketplace}` form.

    Returns:
        Plugin and marketplace names.

    Raises:
        ValueError: If either part is missing.
    """
    if "@" not in plugin_id:
        msg = f"Invalid plugin id {plugin_id!r}; expected name@marketplace"
        raise ValueError(msg)
    plugin, marketplace = plugin_id.rsplit("@", 1)
    if not plugin or not marketplace:
        msg = f"Invalid plugin id {plugin_id!r}; expected name@marketplace"
        raise ValueError(msg)
    return plugin, marketplace


def namespaced_skill_name(
    namespace: str,
    name: str,
    subfolders: tuple[str, ...] = (),
) -> str:
    """Qualify a skill name under its plugin namespace."""
    return ":".join((namespace, *subfolders, name)).lower()
