"""Raw JSON configuration import and export for MCP servers.

Supports standard Claude / VS Code `"mcpServers"` JSON specification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def export_raw_mcp_config(
    servers: list[dict[str, Any]] | dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Export server records or mapping to standard Claude/VS Code `"mcpServers"` JSON format."""
    mcp_servers: dict[str, dict[str, Any]] = {}

    if isinstance(servers, dict):
        items = [({"name": k, **v} if "name" not in v else v) for k, v in servers.items()]
    else:
        items = list(servers)

    for srv in items:
        name = srv.get("name")
        if not name:
            continue

        raw: dict[str, Any] = {}
        transport = srv.get("transport") or "stdio"
        url = srv.get("url")
        command = srv.get("command")

        if url or transport in ("http", "sse", "streamable_http"):
            raw["serverUrl"] = url or ""
            if srv.get("headers"):
                raw["headers"] = srv["headers"]
        elif command or transport == "stdio":
            raw["command"] = command or ""
            raw["args"] = srv.get("args", [])
            if srv.get("env"):
                raw["env"] = srv["env"]

        # Tool filtering
        if srv.get("disabled_tools"):
            raw["disabledTools"] = srv["disabled_tools"]
        elif "disabled_tools" in srv:
            raw["disabledTools"] = []

        if srv.get("allowed_tools"):
            raw["allowedTools"] = srv["allowed_tools"]

        # State toggle
        raw["disabled"] = not bool(srv.get("enabled", True))

        mcp_servers[name] = raw

    return {"mcpServers": mcp_servers}


async def import_raw_mcp_config(
    raw_config: dict[str, Any],
    store: Any = None,
    source: str = "user",
    target_file: Path | str | None = None,
) -> list[str]:
    """Validate, normalize, and sync raw JSON configurations into a target file or store."""
    if not isinstance(raw_config, dict):
        msg = f"Expected dict for raw MCP config, got {type(raw_config).__name__}"
        raise ValueError(msg)

    servers_dict = raw_config.get("mcpServers")
    if servers_dict is None:
        servers_dict = raw_config

    if not isinstance(servers_dict, dict):
        msg = f"Expected dict for 'mcpServers', got {type(servers_dict).__name__}"
        raise ValueError(msg)

    synced_servers: list[str] = []
    file_servers: dict[str, Any] = {}

    for name, config in servers_dict.items():
        if not isinstance(config, dict):
            continue

        # Extract url / serverUrl
        url = config.get("serverUrl") or config.get("url")
        command = config.get("command")

        # Resolve transport
        transport = config.get("transport") or config.get("type")
        if not transport:
            transport = ("sse" if "sse" in str(url).lower() else "http") if url else "stdio"

        # Enabled / disabled toggle
        if "disabled" in config:
            enabled = not bool(config["disabled"])
        elif "enabled" in config:
            enabled = bool(config["enabled"])
        else:
            enabled = True

        disabled_tools = config.get("disabledTools") or config.get("disabled_tools") or []
        allowed_tools = config.get("allowedTools") or config.get("allowed_tools") or []

        server_record = {
            "name": name,
            "transport": transport,
            "command": command,
            "args": config.get("args", []),
            "url": url,
            "env": config.get("env", {}),
            "headers": config.get("headers", {}),
            "source": config.get("source", source),
            "auth_token_env_var": config.get("auth_token_env_var"),
            "disabled_tools": disabled_tools if isinstance(disabled_tools, list) else [],
            "allowed_tools": allowed_tools if isinstance(allowed_tools, list) else [],
            "enabled": enabled,
            "trusted": bool(config.get("trusted", False)),
        }

        if store is not None and hasattr(store, "upsert_mcp_server"):
            await store.upsert_mcp_server(server_record)

        file_servers[name] = config
        synced_servers.append(name)

    if target_file is not None:
        target_path = Path(target_file)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        existing_data: dict[str, Any] = {}
        if target_path.is_file():
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
            except Exception:
                existing_data = {}
        existing_servers = existing_data.get("mcpServers", {})
        existing_servers.update(file_servers)
        existing_data["mcpServers"] = existing_servers
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(existing_data, f, indent=2)

    return synced_servers
