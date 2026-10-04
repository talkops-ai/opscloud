"""DB-backed configuration store with manifest-aware typed resolution.

The ``ConfigStore`` facade provides:
- **Typed resolution**: ``resolve(option)`` → DB → env → manifest default
- **Entity management**: MCP servers, plugins, skills, subagents, model preferences
- **In-memory cache**: Warm cache avoids DB round-trips for hot paths
- **Adapter pattern**: ``ConfigStorageAdapter`` ABC for SQLite/Postgres backends
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


class ConfigCategory(StrEnum):
    """Categories for grouping configuration entries."""

    CREDENTIALS = "credentials"
    LLM = "llm"
    MODELS = "models"
    AWS = "aws"
    SECURITY = "security"
    SANDBOX = "sandbox"
    MCP = "mcp"
    MCP_SERVERS = "mcp_servers"
    COMPACTION = "compaction"
    RUBRIC = "rubric"
    INTEGRATIONS = "integrations"
    A2A = "a2a"
    SYSTEM = "system"
    PROJECT = "project"
    STORAGE = "storage"
    TRACING = "tracing"
    SEARCH = "search"
    MONITORING = "monitoring"


_GROUP_TO_CATEGORY: dict[str, ConfigCategory] = {
    "Credentials": ConfigCategory.CREDENTIALS,
    "Models": ConfigCategory.MODELS,
    "AWS": ConfigCategory.AWS,
    "Security": ConfigCategory.SECURITY,
    "Sandbox": ConfigCategory.SANDBOX,
    "Storage": ConfigCategory.STORAGE,
    "MCP": ConfigCategory.MCP,
    "MCP Servers": ConfigCategory.MCP_SERVERS,
    "Compaction": ConfigCategory.COMPACTION,
    "Rubric": ConfigCategory.RUBRIC,
    "Integrations": ConfigCategory.INTEGRATIONS,
    "A2A": ConfigCategory.A2A,
    "System": ConfigCategory.SYSTEM,
    "Project": ConfigCategory.PROJECT,
    "Tracing": ConfigCategory.TRACING,
    "Search": ConfigCategory.SEARCH,
}


def category_from_group(group: str) -> ConfigCategory:
    """Map a manifest group name to a ConfigCategory."""
    return _GROUP_TO_CATEGORY.get(group, ConfigCategory.SYSTEM)


@dataclass
class ConfigEntry:
    """A single configuration entry stored in the DB."""

    key: str
    value: str
    category: ConfigCategory = ConfigCategory.SYSTEM
    display_name: str = ""
    description: str = ""
    is_secret: bool = False

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.key.replace("_", " ").title()


ConfigValue = ConfigEntry


class ConfigStorageAdapter(ABC):
    """Abstract base class for config storage backends."""

    @abstractmethod
    async def initialize(self) -> None:
        """Create tables/schema if needed."""

    # ── Config Entry CRUD ─────────────────────────────────
    @abstractmethod
    async def load_all(self) -> list[ConfigEntry]:
        """Load all configuration entries."""

    @abstractmethod
    async def get(self, key: str) -> ConfigEntry | None:
        """Get a single config entry by key."""

    @abstractmethod
    async def set(self, entry: ConfigEntry) -> None:
        """Set/upsert a configuration entry."""

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete a config entry."""

    @abstractmethod
    async def list_by_category(self, category: ConfigCategory) -> list[ConfigEntry]:
        """List all entries in a category."""

    # ── Model Preferences CRUD ────────────────────────────
    @abstractmethod
    async def get_model_preferences(self) -> dict[str, Any]:
        """Get default model, recent models, effort-by-model, and provider configs."""

    @abstractmethod
    async def save_model_preferences(self, prefs: dict[str, Any]) -> None:
        """Save model preferences."""

    # ── MCP Server CRUD ──────────────────────────────────
    @abstractmethod
    async def list_mcp_servers(self) -> list[dict[str, Any]]:
        """List all MCP server configurations."""

    @abstractmethod
    async def get_mcp_server(self, name: str) -> dict[str, Any] | None:
        """Get a single MCP server configuration."""

    @abstractmethod
    async def upsert_mcp_server(self, server: dict[str, Any]) -> None:
        """Upsert an MCP server configuration."""

    @abstractmethod
    async def delete_mcp_server(self, name: str) -> bool:
        """Delete an MCP server by name."""

    # ── Marketplace Metadata CRUD ─────────────────────────
    @abstractmethod
    async def list_marketplaces(self) -> list[dict[str, Any]]:
        """List all marketplace records."""

    @abstractmethod
    async def get_marketplace(self, name: str) -> dict[str, Any] | None:
        """Get a single marketplace record by name."""

    @abstractmethod
    async def upsert_marketplace(self, marketplace: dict[str, Any]) -> None:
        """Upsert a marketplace record."""

    @abstractmethod
    async def delete_marketplace(self, name: str) -> bool:
        """Delete a marketplace record."""

    # ── Plugin Metadata CRUD ──────────────────────────────
    @abstractmethod
    async def list_plugins(self) -> list[dict[str, Any]]:
        """List all installed plugin records."""

    @abstractmethod
    async def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        """Get a single plugin record by plugin_id."""

    @abstractmethod
    async def upsert_plugin(self, plugin: dict[str, Any]) -> None:
        """Upsert a plugin record."""

    @abstractmethod
    async def delete_plugin(self, plugin_id: str) -> bool:
        """Delete a plugin record."""

    # ── Skill Metadata CRUD ───────────────────────────────
    @abstractmethod
    async def list_skills(self) -> list[dict[str, Any]]:
        """List all skill metadata records."""

    @abstractmethod
    async def upsert_skill(self, skill: dict[str, Any]) -> None:
        """Upsert a skill metadata record."""

    @abstractmethod
    async def delete_skill(self, name: str) -> bool:
        """Delete a skill record."""

    # ── Subagent Metadata CRUD ────────────────────────────
    @abstractmethod
    async def list_subagents(self) -> list[dict[str, Any]]:
        """List all subagent metadata records."""

    @abstractmethod
    async def upsert_subagent(self, subagent: dict[str, Any]) -> None:
        """Upsert a subagent metadata record."""

    @abstractmethod
    async def delete_subagent(self, name: str) -> bool:
        """Delete a subagent record."""

    # ── Agent Instruction Metadata ────────────────────────
    @abstractmethod
    async def list_agent_instructions(self) -> list[dict[str, Any]]:
        """List all agent instruction metadata."""

    @abstractmethod
    async def upsert_agent_instruction(self, instruction: dict[str, Any]) -> None:
        """Upsert an agent instruction metadata entry."""


class ConfigStore:
    """Facade for configuration with manifest-aware typed resolution & entity management."""

    def __init__(self, adapter: ConfigStorageAdapter) -> None:
        """Initialize the ConfigStore with a storage adapter."""
        self._adapter = adapter
        self._cache: dict[str, ConfigEntry] = {}
        self._initialized = False

    @property
    def adapter(self) -> ConfigStorageAdapter:
        """Return the underlying configuration storage adapter."""
        return self._adapter

    async def initialize(self) -> None:
        """Initialize storage backend and warm cache from DB."""
        if self._initialized:
            return
        await self._adapter.initialize()
        entries = await self._adapter.load_all()
        for entry in entries:
            self._cache[entry.key] = entry
        self._initialized = True
        logger.info("ConfigStore initialized with %d entries", len(self._cache))

    # ── Typed Resolution ─────────────────────────────────

    async def resolve(
        self,
        option: Any,  # ConfigOption
    ) -> tuple[Any, str]:
        """Resolve an option through the chain: DB → env → TOML → manifest default."""
        from opscloud.config.manifest import (
            _coerce_toml,
            _toml_lookup,
            coerce_str_value,
            load_config_toml,
            resolve_from_env,
        )

        # 1. Check DB
        db_key = getattr(option, "db_key", None) or getattr(option, "key", None)
        if db_key:
            db_value = await self.get(db_key)
            if db_value is not None and db_value != "":
                coerced = coerce_str_value(option.kind, db_value)
                if coerced is not None:
                    return coerced, "db"

        # 2. Check environment variables
        env_result = resolve_from_env(option)
        if env_result is not None:
            return env_result

        # 3. Check TOML config file
        toml_keys = getattr(option, "toml_keys", None)
        if toml_keys:
            toml_data = load_config_toml()
            found, raw = _toml_lookup(toml_data, toml_keys)
            if found:
                coerced = _coerce_toml(option, raw)
                if coerced is not None:
                    return coerced, "config.toml"

        # 4. Manifest default
        return option.default, "default"

    async def get_typed(
        self,
        option: Any,  # ConfigOption
    ) -> Any:
        """Resolve and return just the typed value."""
        value, _ = await self.resolve(option)
        return value

    async def set_typed(
        self,
        option: Any,  # ConfigOption
        value: Any,
    ) -> None:
        """Set a config value with manifest-aware serialization."""
        from opscloud.config.manifest import serialize_typed_value

        str_value = serialize_typed_value(option.kind, value)
        await self.set(
            key=option.db_key,
            value=str_value,
            category=category_from_group(option.group),
            display_name=option.summary,
            description="",
            is_secret=option.redacted,
        )

    # ── Raw String Operations ────────────────────────────

    async def get_entry(self, key: str) -> ConfigEntry | None:
        """Get a full config entry by key (cache → adapter)."""
        if key in self._cache:
            return self._cache[key]

        entry = await self._adapter.get(key)
        if entry:
            self._cache[key] = entry
            return entry

        return None

    async def get(self, key: str, default: str | None = None) -> str | None:
        """Get a raw config value by key (cache → adapter → default)."""
        entry = await self.get_entry(key)
        if entry is not None:
            return entry.value
        return default

    async def set(
        self,
        key: str,
        value: str,
        *,
        category: ConfigCategory = ConfigCategory.SYSTEM,
        display_name: str = "",
        description: str = "",
        is_secret: bool = False,
    ) -> None:
        """Set/upsert a config value in the DB and cache."""
        entry = ConfigEntry(
            key=key,
            value=value,
            category=category,
            display_name=display_name,
            description=description,
            is_secret=is_secret,
        )
        await self._adapter.set(entry)
        self._cache[key] = entry

    async def delete(self, key: str) -> bool:
        """Delete a config entry from DB and cache."""
        result = await self._adapter.delete(key)
        self._cache.pop(key, None)
        return result

    async def list_by_category(self, category: ConfigCategory) -> list[ConfigEntry]:
        """List all entries in a category."""
        return await self._adapter.list_by_category(category)

    async def list_all(self) -> list[ConfigEntry]:
        """List all entries from the adapter."""
        return await self._adapter.load_all()

    async def invalidate_cache(self, key: str | None = None) -> None:
        """Invalidate cache for a specific key or all keys."""
        if key:
            self._cache.pop(key, None)
        else:
            self._cache.clear()
            entries = await self._adapter.load_all()
            for entry in entries:
                self._cache[entry.key] = entry

    # ── Entity Convenience Passthroughs ──────────────────

    async def get_model_preferences(self) -> dict[str, Any]:
        """Retrieve saved model preferences."""
        return await self._adapter.get_model_preferences()

    async def save_model_preferences(self, prefs: dict[str, Any]) -> None:
        """Save or update model preferences."""
        await self._adapter.save_model_preferences(prefs)

    async def list_mcp_servers(self) -> list[dict[str, Any]]:
        """List all configured MCP servers."""
        return await self._adapter.list_mcp_servers()

    async def get_mcp_server(self, name: str) -> dict[str, Any] | None:
        """Retrieve an MCP server configuration by name."""
        return await self._adapter.get_mcp_server(name)

    async def upsert_mcp_server(self, server: dict[str, Any]) -> None:
        """Create or update an MCP server configuration."""
        await self._adapter.upsert_mcp_server(server)

    async def delete_mcp_server(self, name: str) -> bool:
        """Delete an MCP server configuration by name."""
        return await self._adapter.delete_mcp_server(name)

    async def list_marketplaces(self) -> list[dict[str, Any]]:
        """List all configured plugin marketplaces."""
        return await self._adapter.list_marketplaces()

    async def get_marketplace(self, name: str) -> dict[str, Any] | None:
        """Retrieve a plugin marketplace by name."""
        return await self._adapter.get_marketplace(name)

    async def upsert_marketplace(self, marketplace: dict[str, Any]) -> None:
        """Create or update a plugin marketplace record."""
        await self._adapter.upsert_marketplace(marketplace)

    async def delete_marketplace(self, name: str) -> bool:
        """Delete a plugin marketplace by name."""
        return await self._adapter.delete_marketplace(name)

    async def list_plugins(self) -> list[dict[str, Any]]:
        """List all installed plugin records."""
        return await self._adapter.list_plugins()

    async def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        """Retrieve an installed plugin record by ID."""
        return await self._adapter.get_plugin(plugin_id)

    async def upsert_plugin(self, plugin: dict[str, Any]) -> None:
        """Create or update an installed plugin record."""
        await self._adapter.upsert_plugin(plugin)

    async def delete_plugin(self, plugin_id: str) -> bool:
        """Delete an installed plugin record by ID."""
        return await self._adapter.delete_plugin(plugin_id)

    async def list_skills(self) -> list[dict[str, Any]]:
        """List all stored custom skill records."""
        return await self._adapter.list_skills()

    async def upsert_skill(self, skill: dict[str, Any]) -> None:
        """Create or update a custom skill record."""
        await self._adapter.upsert_skill(skill)

    async def delete_skill(self, name: str) -> bool:
        """Delete a custom skill record by name."""
        return await self._adapter.delete_skill(name)

    async def list_subagents(self) -> list[dict[str, Any]]:
        """List all stored custom subagent records."""
        return await self._adapter.list_subagents()

    async def upsert_subagent(self, subagent: dict[str, Any]) -> None:
        """Create or update a custom subagent record."""
        await self._adapter.upsert_subagent(subagent)

    async def delete_subagent(self, name: str) -> bool:
        """Delete a custom subagent record by name."""
        return await self._adapter.delete_subagent(name)
