"""PostgreSQL-backed configuration storage adapter.

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
from typing import Any, cast

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow, dict_row

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
    is_secret BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Model preferences & catalog overrides
CREATE TABLE IF NOT EXISTS model_preferences (
    id TEXT PRIMARY KEY DEFAULT 'current',
    default_model TEXT,
    recent_models TEXT DEFAULT '[]',
    effort_by_model TEXT DEFAULT '{}',
    provider_configs TEXT DEFAULT '{}',
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
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
    enabled BOOLEAN DEFAULT TRUE,
    trusted BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Marketplace catalog records
CREATE TABLE IF NOT EXISTS marketplaces (
    name TEXT PRIMARY KEY,
    source_type TEXT NOT NULL DEFAULT 'directory',
    source_value TEXT NOT NULL,
    install_location TEXT NOT NULL,
    ref TEXT,
    plugin_count INTEGER DEFAULT 0,
    is_team BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
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
    enabled BOOLEAN DEFAULT TRUE,
    config TEXT DEFAULT '{}',
    installed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    git_commit_sha TEXT,
    load_error TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
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
    enabled BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
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
    enabled BOOLEAN DEFAULT TRUE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Agent instruction tracking
CREATE TABLE IF NOT EXISTS agent_instructions (
    scope TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    checksum TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
"""

_CONFIG_CHANGE_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION notify_config_change()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('config_change', NEW.key);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS config_change_trigger ON config_store;
CREATE TRIGGER config_change_trigger
    AFTER INSERT OR UPDATE ON config_store
    FOR EACH ROW EXECUTE FUNCTION notify_config_change();
"""


class PostgresConfigAdapter(ConfigStorageAdapter):
    """PostgreSQL-backed config storage with pg_notify support."""

    def __init__(self, connection_uri: str) -> None:
        """Initialize the PostgreSQL configuration storage adapter."""
        self._uri = connection_uri

    def _connect(self) -> Connection[DictRow]:
        return cast(
            Connection[DictRow],
            psycopg.connect(self._uri, row_factory=cast(Any, dict_row)),
        )

    async def initialize(self) -> None:
        """Create all tables and notification triggers."""
        with self._connect() as conn:
            conn.execute(_SCHEMA_SQL)
            conn.execute(_CONFIG_CHANGE_TRIGGER_SQL)
            # Add columns if migrating an existing DB
            plugin_migration_stmts = (
                "ALTER TABLE plugins ADD COLUMN IF NOT EXISTS display_name TEXT",
                "ALTER TABLE plugins ADD COLUMN IF NOT EXISTS description TEXT",
                "ALTER TABLE plugins ADD COLUMN IF NOT EXISTS author TEXT",
                "ALTER TABLE plugins ADD COLUMN IF NOT EXISTS skill_count INTEGER DEFAULT 0",
                "ALTER TABLE plugins ADD COLUMN IF NOT EXISTS skill_names TEXT DEFAULT '[]'",
                "ALTER TABLE plugins ADD COLUMN IF NOT EXISTS mcp_server_names TEXT DEFAULT '[]'",
                "ALTER TABLE plugins ADD COLUMN IF NOT EXISTS installed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()",
                "ALTER TABLE plugins ADD COLUMN IF NOT EXISTS last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW()",
                "ALTER TABLE plugins ADD COLUMN IF NOT EXISTS git_commit_sha TEXT",
                "ALTER TABLE plugins ADD COLUMN IF NOT EXISTS load_error TEXT",
            )
            for stmt in plugin_migration_stmts:
                with contextlib.suppress(Exception):
                    conn.execute(stmt)
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE skills ADD COLUMN IF NOT EXISTS content TEXT DEFAULT ''")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE subagents ADD COLUMN IF NOT EXISTS system_prompt TEXT DEFAULT ''")
            mcp_migration_stmts = (
                "ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS disabled_tools TEXT DEFAULT '[]'",
                "ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS allowed_tools TEXT DEFAULT '[]'",
            )
            for stmt in mcp_migration_stmts:
                with contextlib.suppress(Exception):
                    conn.execute(stmt)
            conn.execute("INSERT INTO model_preferences (id) VALUES ('current') ON CONFLICT (id) DO NOTHING")
            conn.commit()
        logger.debug("PostgresConfigAdapter initialized")

    # ── ConfigEntry CRUD ─────────────────────────────────

    async def load_all(self) -> list[ConfigEntry]:
        """Load all configuration entries from the database."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, category, display_name, description, is_secret FROM config_store ORDER BY key"
            ).fetchall()
            return [self._row_to_entry(row) for row in rows]

    async def get(self, key: str) -> ConfigEntry | None:
        """Retrieve a single configuration entry by its key."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key, value, category, display_name, description, is_secret FROM config_store WHERE key = %s",
                (key,),
            ).fetchone()
            return self._row_to_entry(row) if row else None

    async def set(self, entry: ConfigEntry) -> None:
        """Store or update a configuration entry."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO config_store (key, value, category, display_name, description, is_secret, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(key) DO UPDATE SET
                    value = EXCLUDED.value,
                    category = EXCLUDED.category,
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    is_secret = EXCLUDED.is_secret,
                    updated_at = NOW()
                """,
                (
                    entry.key,
                    entry.value,
                    entry.category.value,
                    entry.display_name,
                    entry.description,
                    entry.is_secret,
                ),
            )
            conn.commit()

    async def delete(self, key: str) -> bool:
        """Delete a configuration entry by its key."""
        with self._connect() as conn:
            result = conn.execute("DELETE FROM config_store WHERE key = %s", (key,))
            conn.commit()
            return result.rowcount > 0

    async def list_by_category(self, category: ConfigCategory) -> list[ConfigEntry]:
        """Retrieve all configuration entries belonging to a category."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value, category, display_name, description, is_secret "
                "FROM config_store WHERE category = %s ORDER BY key",
                (category.value,),
            ).fetchall()
            return [self._row_to_entry(row) for row in rows]

    # ── Model Preferences CRUD ────────────────────

    async def get_model_preferences(self) -> dict[str, Any]:
        """Retrieve saved model preferences."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT default_model, recent_models, effort_by_model, provider_configs "
                "FROM model_preferences WHERE id = 'current'"
            ).fetchone()
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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO model_preferences (id, default_model, recent_models, effort_by_model, provider_configs, updated_at)
                VALUES ('current', %s, %s, %s, %s, NOW())
                ON CONFLICT(id) DO UPDATE SET
                    default_model = COALESCE(EXCLUDED.default_model, model_preferences.default_model),
                    recent_models = COALESCE(EXCLUDED.recent_models, model_preferences.recent_models),
                    effort_by_model = COALESCE(EXCLUDED.effort_by_model, model_preferences.effort_by_model),
                    provider_configs = COALESCE(EXCLUDED.provider_configs, model_preferences.provider_configs),
                    updated_at = NOW()
                """,
                (
                    prefs.get("default_model"),
                    json.dumps(prefs.get("recent_models", [])) if "recent_models" in prefs else None,
                    json.dumps(prefs.get("effort_by_model", {})) if "effort_by_model" in prefs else None,
                    json.dumps(prefs.get("provider_configs", {})) if "provider_configs" in prefs else None,
                ),
            )
            conn.commit()

    # ── MCP Server CRUD ──────────────────────────────────

    async def list_mcp_servers(self) -> list[dict[str, Any]]:
        """List all configured MCP servers."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM mcp_servers ORDER BY name").fetchall()
            return [
                {
                    "name": row["name"],
                    "transport": row["transport"],
                    "command": row["command"],
                    "args": json.loads(row["args"] or "[]") if isinstance(row["args"], str) else (row["args"] or []),
                    "url": row["url"],
                    "env": json.loads(row["env"] or "{}") if isinstance(row["env"], str) else (row["env"] or {}),
                    "headers": json.loads(row["headers"] or "{}")
                    if isinstance(row["headers"], str)
                    else (row["headers"] or {}),
                    "source": row["source"],
                    "auth_token_env_var": row["auth_token_env_var"],
                    "disabled_tools": json.loads(row["disabled_tools"] or "[]")
                    if isinstance(row.get("disabled_tools"), str)
                    else (row.get("disabled_tools") or []),
                    "allowed_tools": json.loads(row["allowed_tools"] or "[]")
                    if isinstance(row.get("allowed_tools"), str)
                    else (row.get("allowed_tools") or []),
                    "enabled": bool(row["enabled"]),
                    "trusted": bool(row["trusted"]),
                }
                for row in rows
            ]

    async def get_mcp_server(self, name: str) -> dict[str, Any] | None:
        """Retrieve an MCP server configuration by name."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM mcp_servers WHERE name = %s", (name,)).fetchone()
            if row is None:
                return None
            return {
                "name": row["name"],
                "transport": row["transport"],
                "command": row["command"],
                "args": json.loads(row["args"] or "[]") if isinstance(row["args"], str) else (row["args"] or []),
                "url": row["url"],
                "env": json.loads(row["env"] or "{}") if isinstance(row["env"], str) else (row["env"] or {}),
                "headers": json.loads(row["headers"] or "{}")
                if isinstance(row["headers"], str)
                else (row["headers"] or {}),
                "source": row["source"],
                "auth_token_env_var": row["auth_token_env_var"],
                "disabled_tools": json.loads(row["disabled_tools"] or "[]")
                if isinstance(row.get("disabled_tools"), str)
                else (row.get("disabled_tools") or []),
                "allowed_tools": json.loads(row["allowed_tools"] or "[]")
                if isinstance(row.get("allowed_tools"), str)
                else (row.get("allowed_tools") or []),
                "enabled": bool(row["enabled"]),
                "trusted": bool(row["trusted"]),
            }

    async def upsert_mcp_server(self, server: dict[str, Any]) -> None:
        """Create or update an MCP server configuration."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_servers (name, transport, command, args, url, env, headers, source, auth_token_env_var, disabled_tools, allowed_tools, enabled, trusted, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(name) DO UPDATE SET
                    transport = EXCLUDED.transport,
                    command = EXCLUDED.command,
                    args = EXCLUDED.args,
                    url = EXCLUDED.url,
                    env = EXCLUDED.env,
                    headers = EXCLUDED.headers,
                    source = EXCLUDED.source,
                    auth_token_env_var = EXCLUDED.auth_token_env_var,
                    disabled_tools = EXCLUDED.disabled_tools,
                    allowed_tools = EXCLUDED.allowed_tools,
                    enabled = EXCLUDED.enabled,
                    trusted = EXCLUDED.trusted,
                    updated_at = NOW()
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
                    server.get("enabled", True),
                    server.get("trusted", False),
                ),
            )
            conn.commit()

    async def delete_mcp_server(self, name: str) -> bool:
        """Delete an MCP server configuration by name."""
        with self._connect() as conn:
            result = conn.execute("DELETE FROM mcp_servers WHERE name = %s", (name,))
            conn.commit()
            return result.rowcount > 0

    # ── Marketplace Metadata CRUD ─────────────────────────

    async def list_marketplaces(self) -> list[dict[str, Any]]:
        """List all configured plugin marketplaces."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM marketplaces ORDER BY name").fetchall()
            return [
                {
                    "name": row["name"],
                    "source_type": row["source_type"],
                    "source_value": row["source_value"],
                    "install_location": row["install_location"],
                    "ref": row["ref"],
                    "plugin_count": row["plugin_count"],
                    "is_team": bool(row["is_team"]),
                    "created_at": str(row["created_at"]) if row["created_at"] else None,
                    "updated_at": str(row["updated_at"]) if row["updated_at"] else None,
                }
                for row in rows
            ]

    async def get_marketplace(self, name: str) -> dict[str, Any] | None:
        """Retrieve a plugin marketplace by name."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM marketplaces WHERE name = %s",
                (name,),
            ).fetchone()
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
                "created_at": str(row["created_at"]) if row["created_at"] else None,
                "updated_at": str(row["updated_at"]) if row["updated_at"] else None,
            }

    async def upsert_marketplace(self, marketplace: dict[str, Any]) -> None:
        """Create or update a plugin marketplace record."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO marketplaces (name, source_type, source_value, install_location, ref, plugin_count, is_team, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(name) DO UPDATE SET
                    source_type = EXCLUDED.source_type,
                    source_value = EXCLUDED.source_value,
                    install_location = EXCLUDED.install_location,
                    ref = EXCLUDED.ref,
                    plugin_count = EXCLUDED.plugin_count,
                    is_team = EXCLUDED.is_team,
                    updated_at = NOW()
                """,
                (
                    marketplace["name"],
                    marketplace.get("source_type", "directory"),
                    marketplace.get("source_value", ""),
                    marketplace.get("install_location", ""),
                    marketplace.get("ref"),
                    int(marketplace.get("plugin_count", 0)),
                    bool(marketplace.get("is_team", False)),
                ),
            )
            conn.commit()

    async def delete_marketplace(self, name: str) -> bool:
        """Delete a plugin marketplace by name."""
        with self._connect() as conn:
            result = conn.execute("DELETE FROM marketplaces WHERE name = %s", (name,))
            conn.commit()
            return result.rowcount > 0

    # ── Plugin Metadata CRUD ──────────────────────────────

    def _row_to_plugin(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "plugin_id": row["plugin_id"],
            "name": row["name"],
            "marketplace": row["marketplace"],
            "version": row["version"],
            "display_name": row.get("display_name"),
            "description": row.get("description"),
            "author": row.get("author"),
            "skill_count": row.get("skill_count") or 0,
            "skill_names": json.loads(row["skill_names"] or "[]")
            if isinstance(row.get("skill_names"), str)
            else (row.get("skill_names") or []),
            "mcp_server_names": json.loads(row["mcp_server_names"] or "[]")
            if isinstance(row.get("mcp_server_names"), str)
            else (row.get("mcp_server_names") or []),
            "install_path": row["install_path"],
            "scope": row["scope"],
            "source_type": row["source_type"],
            "source_value": row["source_value"],
            "enabled": bool(row["enabled"]),
            "config": json.loads(row["config"] or "{}")
            if isinstance(row.get("config"), str)
            else (row.get("config") or {}),
            "installed_at": str(row["installed_at"]) if row.get("installed_at") else None,
            "last_updated": str(row["last_updated"]) if row.get("last_updated") else None,
            "git_commit_sha": row.get("git_commit_sha"),
            "load_error": row.get("load_error"),
        }

    async def list_plugins(self) -> list[dict[str, Any]]:
        """List all installed plugin records."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM plugins ORDER BY plugin_id").fetchall()
            return [self._row_to_plugin(row) for row in rows]

    async def get_plugin(self, plugin_id: str) -> dict[str, Any] | None:
        """Retrieve an installed plugin record by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plugins WHERE plugin_id = %s",
                (plugin_id,),
            ).fetchone()
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

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plugins (
                    plugin_id, name, marketplace, version, display_name, description, author,
                    skill_count, skill_names, mcp_server_names, install_path, scope,
                    source_type, source_value, enabled, config, installed_at, last_updated,
                    git_commit_sha, load_error, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), %s, %s, NOW())
                ON CONFLICT(plugin_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    marketplace = EXCLUDED.marketplace,
                    version = EXCLUDED.version,
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    author = EXCLUDED.author,
                    skill_count = EXCLUDED.skill_count,
                    skill_names = EXCLUDED.skill_names,
                    mcp_server_names = EXCLUDED.mcp_server_names,
                    install_path = EXCLUDED.install_path,
                    scope = EXCLUDED.scope,
                    source_type = EXCLUDED.source_type,
                    source_value = EXCLUDED.source_value,
                    enabled = EXCLUDED.enabled,
                    config = EXCLUDED.config,
                    last_updated = NOW(),
                    git_commit_sha = EXCLUDED.git_commit_sha,
                    load_error = EXCLUDED.load_error,
                    updated_at = NOW()
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
                    bool(plugin.get("enabled", True)),
                    json.dumps(plugin.get("config", {})),
                    plugin.get("git_commit_sha"),
                    plugin.get("load_error"),
                ),
            )
            conn.commit()

    async def delete_plugin(self, plugin_id: str) -> bool:
        """Delete an installed plugin record by ID."""
        with self._connect() as conn:
            result = conn.execute("DELETE FROM plugins WHERE plugin_id = %s", (plugin_id,))
            conn.commit()
            return result.rowcount > 0

    # ── Skill Metadata CRUD ───────────────────────────────

    async def list_skills(self) -> list[dict[str, Any]]:
        """List all stored custom skill records."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM skills ORDER BY name").fetchall()
            return [
                {
                    "name": row["name"],
                    "description": row["description"] or "",
                    "domain": row["domain"] or "",
                    "path": row["path"],
                    "virtual_path": row["virtual_path"],
                    "source": row["source"],
                    "content": row.get("content") or "",
                    "enabled": bool(row["enabled"]),
                }
                for row in rows
            ]

    async def upsert_skill(self, skill: dict[str, Any]) -> None:
        """Create or update a custom skill record."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO skills (name, description, domain, path, virtual_path, source, content, enabled, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(name) DO UPDATE SET
                    description = EXCLUDED.description,
                    domain = EXCLUDED.domain,
                    path = EXCLUDED.path,
                    virtual_path = EXCLUDED.virtual_path,
                    source = EXCLUDED.source,
                    content = CASE WHEN EXCLUDED.content != '' THEN EXCLUDED.content ELSE skills.content END,
                    enabled = EXCLUDED.enabled,
                    updated_at = NOW()
                """,
                (
                    skill["name"],
                    skill.get("description", ""),
                    skill.get("domain", ""),
                    skill.get("path"),
                    skill.get("virtual_path"),
                    skill.get("source", "built-in"),
                    skill.get("content", ""),
                    skill.get("enabled", True),
                ),
            )
            conn.commit()

    async def delete_skill(self, name: str) -> bool:
        """Delete a custom skill record by name."""
        with self._connect() as conn:
            result = conn.execute("DELETE FROM skills WHERE name = %s", (name,))
            conn.commit()
            return result.rowcount > 0

    # ── Subagent Metadata CRUD ────────────────────────────

    async def list_subagents(self) -> list[dict[str, Any]]:
        """List all stored custom subagent records."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM subagents ORDER BY name").fetchall()
            return [
                {
                    "name": row["name"],
                    "description": row["description"] or "",
                    "model": row["model"],
                    "instructions_path": row["instructions_path"],
                    "system_prompt": row.get("system_prompt") or "",
                    "tools": json.loads(row["tools"] or "[]"),
                    "source": row["source"],
                    "enabled": bool(row["enabled"]),
                }
                for row in rows
            ]

    async def upsert_subagent(self, subagent: dict[str, Any]) -> None:
        """Create or update a custom subagent record."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subagents (name, description, model, instructions_path, system_prompt, tools, source, enabled, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT(name) DO UPDATE SET
                    description = EXCLUDED.description,
                    model = EXCLUDED.model,
                    instructions_path = EXCLUDED.instructions_path,
                    system_prompt = CASE WHEN EXCLUDED.system_prompt != '' THEN EXCLUDED.system_prompt ELSE subagents.system_prompt END,
                    tools = EXCLUDED.tools,
                    source = EXCLUDED.source,
                    enabled = EXCLUDED.enabled,
                    updated_at = NOW()
                """,
                (
                    subagent["name"],
                    subagent.get("description", ""),
                    subagent.get("model"),
                    subagent.get("instructions_path"),
                    subagent.get("system_prompt", ""),
                    json.dumps(subagent.get("tools", [])),
                    subagent.get("source", "built-in"),
                    subagent.get("enabled", True),
                ),
            )
            conn.commit()

    async def delete_subagent(self, name: str) -> bool:
        """Delete a custom subagent record by name."""
        with self._connect() as conn:
            result = conn.execute("DELETE FROM subagents WHERE name = %s", (name,))
            conn.commit()
            return result.rowcount > 0

    # ── Agent Instruction Metadata ────────────────────────

    async def list_agent_instructions(self) -> list[dict[str, Any]]:
        """List custom instructions configured for agents."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM agent_instructions ORDER BY scope").fetchall()
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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_instructions (scope, file_path, checksum, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT(scope) DO UPDATE SET
                    file_path = EXCLUDED.file_path,
                    checksum = EXCLUDED.checksum,
                    updated_at = NOW()
                """,
                (
                    instruction["scope"],
                    instruction["file_path"],
                    instruction.get("checksum"),
                ),
            )
            conn.commit()

    # ── Helpers ───────────────────────────────────────────

    @staticmethod
    def _row_to_entry(row: dict[str, Any]) -> ConfigEntry:
        return ConfigEntry(
            key=row["key"],
            value=row["value"],
            category=ConfigCategory(row["category"]),
            display_name=row.get("display_name") or "",
            description=row.get("description") or "",
            is_secret=bool(row.get("is_secret")),
        )
