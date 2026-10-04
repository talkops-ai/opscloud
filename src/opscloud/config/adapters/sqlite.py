"""SQLite-backed configuration storage adapter.

Implements storage for:
- ``config_store`` — generic key/value configuration settings
- ``model_preferences`` — default model, recent models MRU, effort-by-model, provider configs
- ``mcp_servers`` — MCP server configs (matches OpsCode MCPServerConfig)
- ``plugins`` — installed plugin records with source for redeployment auto-rehydration
- ``skills`` — skill metadata and content for redeployment auto-rehydration
- ``subagents`` — subagent metadata and system prompt for redeployment auto-rehydration
- ``agent_instructions`` — flat file instruction tracking
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import aiosqlite

from opscloud.config.store import ConfigCategory, ConfigEntry, ConfigStorageAdapter
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_SCHEMA_SQL = """
-- Core config key/value store
CREATE TABLE IF NOT EXISTS config_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'system',
    display_name TEXT DEFAULT '',
    description TEXT DEFAULT '',
    is_secret INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Model preferences & catalog overrides
CREATE TABLE IF NOT EXISTS model_preferences (
    id TEXT PRIMARY KEY DEFAULT 'current',
    default_model TEXT,
    recent_models TEXT DEFAULT '[]',
    effort_by_model TEXT DEFAULT '{}',
    provider_configs TEXT DEFAULT '{}',
    updated_at TEXT DEFAULT (datetime('now'))
);

-- MCP server configurations (matches OpsCode MCPServerConfig)
CREATE TABLE IF NOT EXISTS mcp_servers (
    name TEXT PRIMARY KEY,
    transport TEXT NOT NULL DEFAULT 'stdio',
    command TEXT,
    args TEXT DEFAULT '[]',
    url TEXT,
    env TEXT DEFAULT '{}',
    headers TEXT DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'user',
    auth_token_env_var TEXT,
    disabled_tools TEXT DEFAULT '[]',
    allowed_tools TEXT DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    trusted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Marketplace catalog records
CREATE TABLE IF NOT EXISTS marketplaces (
    name TEXT PRIMARY KEY,
    source_type TEXT NOT NULL DEFAULT 'directory',
    source_value TEXT NOT NULL,
    install_location TEXT NOT NULL,
    ref TEXT,
    plugin_count INTEGER DEFAULT 0,
    is_team INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Plugin records with source for redeployment auto-rehydration
CREATE TABLE IF NOT EXISTS plugins (
    plugin_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    marketplace TEXT NOT NULL DEFAULT 'default',
    version TEXT,
    display_name TEXT,
    description TEXT,
    author TEXT,
    skill_count INTEGER DEFAULT 0,
    skill_names TEXT DEFAULT '[]',
    mcp_server_names TEXT DEFAULT '[]',
    install_path TEXT,
    scope TEXT NOT NULL DEFAULT 'user',
    source_type TEXT NOT NULL DEFAULT 'local',
    source_value TEXT,
    enabled INTEGER DEFAULT 1,
    config TEXT DEFAULT '{}',
    installed_at TEXT DEFAULT (datetime('now')),
    last_updated TEXT DEFAULT (datetime('now')),
    git_commit_sha TEXT,
    load_error TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Skill metadata with content for redeployment auto-rehydration
CREATE TABLE IF NOT EXISTS skills (
    name TEXT PRIMARY KEY,
    description TEXT DEFAULT '',
    domain TEXT DEFAULT '',
    path TEXT,
    virtual_path TEXT,
    source TEXT NOT NULL DEFAULT 'built-in',
    content TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Subagent metadata with system_prompt for redeployment auto-rehydration
CREATE TABLE IF NOT EXISTS subagents (
    name TEXT PRIMARY KEY,
    description TEXT DEFAULT '',
    model TEXT,
    instructions_path TEXT,
    system_prompt TEXT DEFAULT '',
    tools TEXT DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'built-in',
    enabled INTEGER DEFAULT 1,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Agent instruction tracking
CREATE TABLE IF NOT EXISTS agent_instructions (
    scope TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    checksum TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
"""


class SqliteConfigAdapter(ConfigStorageAdapter):
    """SQLite-backed config storage with full entity CRUD."""

    def __init__(self, db_path: str | Path) -> None:
        """Initialize the SQLite configuration storage adapter."""
        self._db_path = str(db_path)

    def _connect(self) -> aiosqlite.Connection:
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        return aiosqlite.connect(self._db_path)

    async def initialize(self) -> None:
        """Create all tables if they don't exist."""
        async with self._connect() as conn:
            await conn.executescript(_SCHEMA_SQL)
            # Add columns if migrating an existing DB
            plugin_columns = [
                ("display_name", "TEXT"),
                ("description", "TEXT"),
                ("author", "TEXT"),
                ("skill_count", "INTEGER DEFAULT 0"),
                ("skill_names", "TEXT DEFAULT '[]'"),
                ("mcp_server_names", "TEXT DEFAULT '[]'"),
                ("installed_at", "TEXT DEFAULT (datetime('now'))"),
                ("last_updated", "TEXT DEFAULT (datetime('now'))"),
                ("git_commit_sha", "TEXT"),
                ("load_error", "TEXT"),
            ]
            for col_name, col_type in plugin_columns:
                with contextlib.suppress(Exception):
                    await conn.execute(f"ALTER TABLE plugins ADD COLUMN {col_name} {col_type}")
            with contextlib.suppress(Exception):
                await conn.execute("ALTER TABLE skills ADD COLUMN content TEXT DEFAULT ''")
            with contextlib.suppress(Exception):
                await conn.execute("ALTER TABLE subagents ADD COLUMN system_prompt TEXT DEFAULT ''")
            mcp_columns = [
                ("disabled_tools", "TEXT DEFAULT '[]'"),
                ("allowed_tools", "TEXT DEFAULT '[]'"),
            ]
            for col_name, col_type in mcp_columns:
                with contextlib.suppress(Exception):
                    await conn.execute(f"ALTER TABLE mcp_servers ADD COLUMN {col_name} {col_type}")
            await conn.execute("INSERT OR IGNORE INTO model_preferences (id) VALUES ('current')")
            await conn.commit()
        logger.debug("SqliteConfigAdapter initialized at %s", self._db_path)

    # ── ConfigEntry CRUD ─────────────────────────────────

    async def load_all(self) -> list[ConfigEntry]:
        """Load all configuration entries from the database."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT key, value, category, display_name, description, is_secret FROM config_store ORDER BY key"
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_entry(row) for row in rows]

    async def get(self, key: str) -> ConfigEntry | None:
        """Retrieve a single configuration entry by its key."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT key, value, category, display_name, description, is_secret FROM config_store WHERE key = ?",
                (key,),
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return None
                return self._row_to_entry(row)

    async def set(self, entry: ConfigEntry) -> None:
        """Store or update a configuration entry."""
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO config_store (key, value, category, display_name, description, is_secret, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    category = excluded.category,
                    display_name = excluded.display_name,
                    description = excluded.description,
                    is_secret = excluded.is_secret,
                    updated_at = datetime('now')
                """,
                (
                    entry.key,
                    entry.value,
                    entry.category.value,
                    entry.display_name,
                    entry.description,
                    int(entry.is_secret),
                ),
            )
            await conn.commit()

    async def delete(self, key: str) -> bool:
        """Delete a configuration entry by its key."""
        async with self._connect() as conn:
            result = await conn.execute("DELETE FROM config_store WHERE key = ?", (key,))
            await conn.commit()
            return bool(result.rowcount and result.rowcount > 0)

    async def list_by_category(self, category: ConfigCategory) -> list[ConfigEntry]:
        """Retrieve all configuration entries belonging to a category."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT key, value, category, display_name, description, is_secret "
                "FROM config_store WHERE category = ? ORDER BY key",
                (category.value,),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_entry(row) for row in rows]

    # ── Model Preferences CRUD ────────────────────────────

    async def get_model_preferences(self) -> dict[str, Any]:
        """Retrieve saved model preferences."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT default_model, recent_models, effort_by_model, provider_configs "
                "FROM model_preferences WHERE id = 'current'"
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return {
                        "default_model": None,
                        "recent_models": [],
                        "effort_by_model": {},
                        "provider_configs": {},
                    }
                return {
                    "default_model": row["default_model"],
                    "recent_models": json.loads(row["recent_models"] or "[]"),
                    "effort_by_model": json.loads(row["effort_by_model"] or "{}"),
                    "provider_configs": json.loads(row["provider_configs"] or "{}"),
                }

    async def save_model_preferences(self, prefs: dict[str, Any]) -> None:
        """Save or update model preferences."""
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO model_preferences (id, default_model, recent_models, effort_by_model, provider_configs, updated_at)
                VALUES ('current', ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(id) DO UPDATE SET
                    default_model = COALESCE(excluded.default_model, model_preferences.default_model),
                    recent_models = COALESCE(excluded.recent_models, model_preferences.recent_models),
                    effort_by_model = COALESCE(excluded.effort_by_model, model_preferences.effort_by_model),
                    provider_configs = COALESCE(excluded.provider_configs, model_preferences.provider_configs),
                    updated_at = datetime('now')
                """,
                (
                    prefs.get("default_model"),
                    json.dumps(prefs.get("recent_models", [])) if "recent_models" in prefs else None,
                    json.dumps(prefs.get("effort_by_model", {})) if "effort_by_model" in prefs else None,
                    json.dumps(prefs.get("provider_configs", {})) if "provider_configs" in prefs else None,
                ),
            )
            await conn.commit()

    # ── MCP Server CRUD ──────────────────────────────────

    async def list_mcp_servers(self) -> list[dict[str, Any]]:
        """List all configured MCP servers."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM mcp_servers ORDER BY name") as cursor:
                rows = await cursor.fetchall()
                result = []
                for row in rows:
                    d = dict(row)
                    result.append(
                        {
                            "name": d["name"],
                            "transport": d["transport"],
                            "command": d["command"],
                            "args": json.loads(d["args"] or "[]"),
                            "url": d["url"],
                            "env": json.loads(d["env"] or "{}"),
                            "headers": json.loads(d["headers"] or "{}"),
                            "source": d["source"],
                            "auth_token_env_var": d["auth_token_env_var"],
                            "disabled_tools": json.loads(d["disabled_tools"] or "[]")
                            if d.get("disabled_tools")
                            else [],
                            "allowed_tools": json.loads(d["allowed_tools"] or "[]") if d.get("allowed_tools") else [],
                            "enabled": bool(d["enabled"]),
                            "trusted": bool(d["trusted"]),
                        }
                    )
                return result

    async def get_mcp_server(self, name: str) -> dict[str, Any] | None:
        """Retrieve an MCP server configuration by name."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM mcp_servers WHERE name = ?", (name,)) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return None
                d = dict(row)
                return {
                    "name": d["name"],
                    "transport": d["transport"],
                    "command": d["command"],
                    "args": json.loads(d["args"] or "[]"),
                    "url": d["url"],
                    "env": json.loads(d["env"] or "{}"),
                    "headers": json.loads(d["headers"] or "{}"),
                    "source": d["source"],
                    "auth_token_env_var": d["auth_token_env_var"],
                    "disabled_tools": json.loads(d["disabled_tools"] or "[]") if d.get("disabled_tools") else [],
                    "allowed_tools": json.loads(d["allowed_tools"] or "[]") if d.get("allowed_tools") else [],
                    "enabled": bool(d["enabled"]),
                    "trusted": bool(d["trusted"]),
                }

    async def upsert_mcp_server(self, server: dict[str, Any]) -> None:
        """Create or update an MCP server configuration."""
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO mcp_servers (name, transport, command, args, url, env, headers, source, auth_token_env_var, disabled_tools, allowed_tools, enabled, trusted, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    transport = excluded.transport,
                    command = excluded.command,
                    args = excluded.args,
                    url = excluded.url,
                    env = excluded.env,
                    headers = excluded.headers,
                    source = excluded.source,
                    auth_token_env_var = excluded.auth_token_env_var,
                    disabled_tools = excluded.disabled_tools,
                    allowed_tools = excluded.allowed_tools,
                    enabled = excluded.enabled,
                    trusted = excluded.trusted,
                    updated_at = datetime('now')
                """,
                (
                    server["name"],
                    server.get("transport", "stdio"),
                    server.get("command"),
                    json.dumps(server.get("args", [])),
                    server.get("url"),
                    json.dumps(server.get("env", {})),
                    json.dumps(server.get("headers", {})),
                    server.get("source", "user"),
                    server.get("auth_token_env_var"),
                    json.dumps(server.get("disabled_tools", [])),
                    json.dumps(server.get("allowed_tools", [])),
                    int(server.get("enabled", True)),
                    int(server.get("trusted", False)),
                ),
            )
            await conn.commit()

    async def delete_mcp_server(self, name: str) -> bool:
        """Delete an MCP server configuration by name."""
        async with self._connect() as conn:
            result = await conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
            await conn.commit()
            return bool(result.rowcount and result.rowcount > 0)

    # ── Marketplace Metadata CRUD ─────────────────────────

    async def list_marketplaces(self) -> list[dict[str, Any]]:
        """List all configured plugin marketplaces."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM marketplaces ORDER BY name") as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "name": row["name"],
                        "source_type": row["source_type"],
                        "source_value": row["source_value"],
                        "install_location": row["install_location"],
                        "ref": row["ref"],
                        "plugin_count": row["plugin_count"],
                        "is_team": bool(row["is_team"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ]

    async def get_marketplace(self, name: str) -> dict[str, Any] | None:
        """Retrieve a plugin marketplace by name."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM marketplaces WHERE name = ?", (name,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return {
                    "name": row["name"],
                    "source_type": row["source_type"],
                    "source_value": row["source_value"],
                    "install_location": row["install_location"],
                    "ref": row["ref"],
                    "plugin_count": row["plugin_count"],
                    "is_team": bool(row["is_team"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }

    async def upsert_marketplace(self, marketplace: dict[str, Any]) -> None:
        """Create or update a plugin marketplace record."""
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO marketplaces (name, source_type, source_value, install_location, ref, plugin_count, is_team, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    source_type = excluded.source_type,
                    source_value = excluded.source_value,
                    install_location = excluded.install_location,
                    ref = excluded.ref,
                    plugin_count = excluded.plugin_count,
                    is_team = excluded.is_team,
                    updated_at = datetime('now')
                """,
                (
                    marketplace["name"],
                    marketplace.get("source_type", "directory"),
                    marketplace.get("source_value", ""),
                    marketplace.get("install_location", ""),
                    marketplace.get("ref"),
                    int(marketplace.get("plugin_count", 0)),
                    int(bool(marketplace.get("is_team", False))),
                ),
            )
            await conn.commit()

    async def delete_marketplace(self, name: str) -> bool:
        """Delete a plugin marketplace by name."""
        async with self._connect() as conn:
            result = await conn.execute("DELETE FROM marketplaces WHERE name = ?", (name,))
            await conn.commit()
            return bool(result.rowcount and result.rowcount > 0)

    # ── Plugin Metadata CRUD ──────────────────────────────

    def _row_to_plugin(self, row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        return {
            "plugin_id": d["plugin_id"],
            "name": d["name"],
            "marketplace": d["marketplace"],
            "version": d["version"],
            "display_name": d.get("display_name"),
            "description": d.get("description"),
            "author": d.get("author"),
            "skill_count": d.get("skill_count") or 0,
            "skill_names": json.loads(d["skill_names"] or "[]") if "skill_names" in d else [],
            "mcp_server_names": json.loads(d["mcp_server_names"] or "[]") if "mcp_server_names" in d else [],
            "install_path": d["install_path"],
            "scope": d["scope"],
            "source_type": d["source_type"],
            "source_value": d["source_value"],
            "enabled": bool(d["enabled"]),
            "config": json.loads(d["config"] or "{}"),
            "installed_at": d.get("installed_at"),
            "last_updated": d.get("last_updated"),
            "git_commit_sha": d.get("git_commit_sha"),
            "load_error": d.get("load_error"),
        }

    async def list_plugins(self) -> list[dict[str, Any]]:
        """List all installed plugin records."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM plugins ORDER BY plugin_id") as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_plugin(row) for row in rows]

    async def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        """Retrieve an installed plugin record by ID."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM plugins WHERE plugin_id = ?", (plugin_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return self._row_to_plugin(row)

    async def upsert_plugin(self, plugin: dict[str, Any]) -> None:
        """Create or update an installed plugin record."""
        p_id = plugin.get("plugin_id") or f"{plugin['name']}@{plugin.get('marketplace', 'default')}"
        skill_names = plugin.get("skill_names")
        skill_names_json = json.dumps(list(skill_names)) if isinstance(skill_names, (list, tuple)) else json.dumps([])

        mcp_names = plugin.get("mcp_server_names")
        mcp_names_json = json.dumps(list(mcp_names)) if isinstance(mcp_names, (list, tuple)) else json.dumps([])

        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO plugins (
                    plugin_id, name, marketplace, version, display_name, description, author,
                    skill_count, skill_names, mcp_server_names, install_path, scope,
                    source_type, source_value, enabled, config, installed_at, last_updated,
                    git_commit_sha, load_error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?, ?, datetime('now'))
                ON CONFLICT(plugin_id) DO UPDATE SET
                    name = excluded.name,
                    marketplace = excluded.marketplace,
                    version = excluded.version,
                    display_name = excluded.display_name,
                    description = excluded.description,
                    author = excluded.author,
                    skill_count = excluded.skill_count,
                    skill_names = excluded.skill_names,
                    mcp_server_names = excluded.mcp_server_names,
                    install_path = excluded.install_path,
                    scope = excluded.scope,
                    source_type = excluded.source_type,
                    source_value = excluded.source_value,
                    enabled = excluded.enabled,
                    config = excluded.config,
                    last_updated = datetime('now'),
                    git_commit_sha = excluded.git_commit_sha,
                    load_error = excluded.load_error,
                    updated_at = datetime('now')
                """,
                (
                    p_id,
                    plugin["name"],
                    plugin.get("marketplace", "default"),
                    plugin.get("version"),
                    plugin.get("display_name"),
                    plugin.get("description"),
                    plugin.get("author"),
                    int(plugin.get("skill_count", 0)),
                    skill_names_json,
                    mcp_names_json,
                    plugin.get("install_path"),
                    plugin.get("scope", "user"),
                    plugin.get("source_type", "local"),
                    plugin.get("source_value"),
                    int(plugin.get("enabled", True)),
                    json.dumps(plugin.get("config", {})),
                    plugin.get("git_commit_sha"),
                    plugin.get("load_error"),
                ),
            )
            await conn.commit()

    async def delete_plugin(self, plugin_id: str) -> bool:
        """Delete an installed plugin record by ID."""
        async with self._connect() as conn:
            result = await conn.execute("DELETE FROM plugins WHERE plugin_id = ?", (plugin_id,))
            await conn.commit()
            return bool(result.rowcount and result.rowcount > 0)

    # ── Skill Metadata CRUD ───────────────────────────────

    async def list_skills(self) -> list[dict[str, Any]]:
        """List all stored custom skill records."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM skills ORDER BY name") as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "name": row["name"],
                        "description": row["description"] or "",
                        "domain": row["domain"] or "",
                        "path": row["path"],
                        "virtual_path": row["virtual_path"],
                        "source": row["source"],
                        "content": row["content"] or "",
                        "enabled": bool(row["enabled"]),
                    }
                    for row in rows
                ]

    async def upsert_skill(self, skill: dict[str, Any]) -> None:
        """Create or update a custom skill record."""
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO skills (name, description, domain, path, virtual_path, source, content, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    description = excluded.description,
                    domain = excluded.domain,
                    path = excluded.path,
                    virtual_path = excluded.virtual_path,
                    source = excluded.source,
                    content = CASE WHEN excluded.content != '' THEN excluded.content ELSE skills.content END,
                    enabled = excluded.enabled,
                    updated_at = datetime('now')
                """,
                (
                    skill["name"],
                    skill.get("description", ""),
                    skill.get("domain", ""),
                    skill.get("path"),
                    skill.get("virtual_path"),
                    skill.get("source", "built-in"),
                    skill.get("content", ""),
                    int(skill.get("enabled", True)),
                ),
            )
            await conn.commit()

    async def delete_skill(self, name: str) -> bool:
        """Delete a custom skill record by name."""
        async with self._connect() as conn:
            result = await conn.execute("DELETE FROM skills WHERE name = ?", (name,))
            await conn.commit()
            return bool(result.rowcount and result.rowcount > 0)

    # ── Subagent Metadata CRUD ────────────────────────────

    async def list_subagents(self) -> list[dict[str, Any]]:
        """List all stored custom subagent records."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM subagents ORDER BY name") as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "name": row["name"],
                        "description": row["description"] or "",
                        "model": row["model"],
                        "instructions_path": row["instructions_path"],
                        "system_prompt": row["system_prompt"] or "",
                        "tools": json.loads(row["tools"] or "[]"),
                        "source": row["source"],
                        "enabled": bool(row["enabled"]),
                    }
                    for row in rows
                ]

    async def upsert_subagent(self, subagent: dict[str, Any]) -> None:
        """Create or update a custom subagent record."""
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO subagents (name, description, model, instructions_path, system_prompt, tools, source, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    description = excluded.description,
                    model = excluded.model,
                    instructions_path = excluded.instructions_path,
                    system_prompt = CASE WHEN excluded.system_prompt != '' THEN excluded.system_prompt ELSE subagents.system_prompt END,
                    tools = excluded.tools,
                    source = excluded.source,
                    enabled = excluded.enabled,
                    updated_at = datetime('now')
                """,
                (
                    subagent["name"],
                    subagent.get("description", ""),
                    subagent.get("model"),
                    subagent.get("instructions_path"),
                    subagent.get("system_prompt", ""),
                    json.dumps(subagent.get("tools", [])),
                    subagent.get("source", "built-in"),
                    int(subagent.get("enabled", True)),
                ),
            )
            await conn.commit()

    async def delete_subagent(self, name: str) -> bool:
        """Delete a custom subagent record by name."""
        async with self._connect() as conn:
            result = await conn.execute("DELETE FROM subagents WHERE name = ?", (name,))
            await conn.commit()
            return bool(result.rowcount and result.rowcount > 0)

    # ── Agent Instruction Metadata ────────────────────────

    async def list_agent_instructions(self) -> list[dict[str, Any]]:
        """List custom instructions configured for agents."""
        async with self._connect() as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM agent_instructions ORDER BY scope") as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "scope": row["scope"],
                        "file_path": row["file_path"],
                        "checksum": row["checksum"],
                    }
                    for row in rows
                ]

    async def upsert_agent_instruction(self, instruction: dict[str, Any]) -> None:
        """Save or update custom instructions for an agent."""
        async with self._connect() as conn:
            await conn.execute(
                """
                INSERT INTO agent_instructions (scope, file_path, checksum, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(scope) DO UPDATE SET
                    file_path = excluded.file_path,
                    checksum = excluded.checksum,
                    updated_at = datetime('now')
                """,
                (
                    instruction["scope"],
                    instruction["file_path"],
                    instruction.get("checksum"),
                ),
            )
            await conn.commit()

    # ── Helpers ───────────────────────────────────────────

    @staticmethod
    def _row_to_entry(row: Any) -> ConfigEntry:
        return ConfigEntry(
            key=row["key"],
            value=row["value"],
            category=ConfigCategory(row["category"]),
            display_name=row["display_name"] or "",
            description=row["description"] or "",
            is_secret=bool(row["is_secret"]),
        )
