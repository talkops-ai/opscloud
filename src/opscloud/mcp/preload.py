"""Preload MCP server metadata and eagerly probe tools with diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AsyncExitStack
import fnmatch
import hashlib
import os
import re
from typing import Any

from opscloud.mcp.config import resolve_mcp_server_env, sync_mcp_env_aliases
from opscloud.mcp.mcp_info import MCPServerInfo, MCPServerStatus, MCPToolInfo
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_PROBE_TIMEOUT = 30.0
_PROBE_TIMEOUT = float(os.environ.get("OPSCLOUD_MCP_PROBE_TIMEOUT", str(_DEFAULT_PROBE_TIMEOUT)))
_MAX_CONCURRENT_PROBES = 8

_MCP_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MCP_TOOL_NAME_MAX_LENGTH = 64
_MCP_TOOL_NAME_HASH_LENGTH = 12


def _mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Compose a provider-safe MCP tool name within max length limits."""
    raw_name = f"{server_name}_{tool_name}"
    sanitized = _MCP_TOOL_NAME_RE.sub("_", raw_name).strip("_") or "unnamed"
    if sanitized == raw_name and len(sanitized) <= _MCP_TOOL_NAME_MAX_LENGTH:
        return sanitized
    digest = hashlib.sha256(f"{server_name}\0{tool_name}".encode()).hexdigest()[
        :_MCP_TOOL_NAME_HASH_LENGTH
    ]
    server = _MCP_TOOL_NAME_RE.sub("_", server_name).strip("_") or "unnamed"
    tool = _MCP_TOOL_NAME_RE.sub("_", tool_name).strip("_") or "unnamed"
    available = _MCP_TOOL_NAME_MAX_LENGTH - len(digest) - 2
    tool_length = min(len(tool), available // 2)
    server_length = min(len(server), available - tool_length)
    tool_length = min(len(tool), available - server_length)
    return f"{server[:server_length]}_{tool[:tool_length]}_{digest}"


def _resolve_transport(config: Mapping[str, Any]) -> str:
    """Determine the transport type from a server config dict."""
    transport = config.get("type") or config.get("transport")
    if transport:
        return str(transport)
    url = config.get("url") or config.get("serverUrl") or ""
    if url:
        if "sse" in str(url).lower():
            return "sse"
        return "http"
    return "stdio"


def _filter_tool_names(
    tool_name: str,
    *,
    allowed_tools: list[str] | tuple[str, ...],
    disabled_tools: list[str] | tuple[str, ...],
) -> bool:
    """Check whether a tool name is permitted by allowed/disabled filters."""
    # Check disabled patterns first
    if disabled_tools:
        for pat in disabled_tools:
            if pat and (pat == tool_name or fnmatch.fnmatch(tool_name, pat)):
                return False

    # If allowed list specified, tool must match at least one pattern
    if allowed_tools:
        return any(pat == tool_name or fnmatch.fnmatch(tool_name, pat) for pat in allowed_tools if pat)

    return True


def _clean_mcp_schema(schema: Any) -> dict[str, Any]:
    """Clean JSON Schema for compatibility with LLM provider tool schemas (Gemini, Anthropic, OpenAI).

    Dereferences all `$ref` pointers (such as `#/$defs/...` or `#/definitions/...`)
    using LangChain's `dereference_refs` so that referenced models (like PricingFilter)
    are inlined directly into properties before removing metadata keys.
    """
    if not isinstance(schema, dict):
        return {}

    from langchain_core.utils.json_schema import dereference_refs

    try:
        resolved = dereference_refs(schema)
    except Exception as exc:
        logger.debug("Failed to dereference MCP schema refs: %s", exc)
        resolved = schema

    cleaned = dict(resolved) if isinstance(resolved, dict) else dict(schema)
    for key in ("$schema", "$id", "$defs", "definitions", "additionalProperties"):
        cleaned.pop(key, None)

    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        new_props = {}
        for prop_name, prop_val in properties.items():
            if isinstance(prop_val, dict):
                p_clean = dict(prop_val)
                p_clean.pop("additionalProperties", None)
                p_clean.pop("$schema", None)
                new_props[prop_name] = p_clean
            else:
                new_props[prop_name] = prop_val
        cleaned["properties"] = new_props

    return cleaned


def _clean_stderr_diagnostic(stderr_text: str | None) -> str | None:
    """Extract a concise and meaningful diagnostic message from captured process stderr."""
    if not stderr_text:
        return None
    lines = [line.strip() for line in stderr_text.strip().splitlines() if line.strip()]
    if not lines:
        return None
    # Prefer lines with explicit error descriptions, AWS credentials/permissions, or exception details
    for line in reversed(lines):
        if (
            line.startswith("Traceback")
            or line.startswith("File ")
            or set(line) <= {"─", "│", "╭", "╮", "╰", "╯", " ", "█", "▀", "▄"}
        ):
            continue
        if any(
            keyword in line
            for keyword in (
                "ExpiredToken",
                "NoCredentialsError",
                "AccessDenied",
                "UnauthorizedOperation",
                "Unable to locate credentials",
                "CRD not found",
                "not found",
                "Error:",
                "Exception:",
                "failed",
                "Error",
                "Exception",
            )
        ):
            if ": " in line and ("Error" in line.split(": ")[0] or "Exception" in line.split(": ")[0]):
                return line.split(": ", 1)[1].strip() or line
            return line
    for line in reversed(lines):
        if not (
            line.startswith("Traceback")
            or line.startswith("File ")
            or set(line) <= {"─", "│", "╭", "╮", "╰", "╯", " ", "█", "▀", "▄"}
        ):
            return line
    return lines[-1]



def _extract_root_mcp_error(
    exc: BaseException | None,
    close_exc: BaseException | None,
    stderr_output: str | None = None,
) -> tuple[MCPServerStatus, str]:
    """Extract root cause error message and status code ('error' or 'unauthenticated') from probe exceptions."""
    all_excs: list[BaseException] = []

    def _collect(e: BaseException | None) -> None:
        if e is None:
            return
        if hasattr(e, "exceptions"):
            for sub in getattr(e, "exceptions", ()):
                _collect(sub)
        if getattr(e, "__cause__", None):
            _collect(e.__cause__)
        if getattr(e, "__context__", None) and e.__context__ is not e:
            _collect(e.__context__)
        all_excs.append(e)

    _collect(exc)
    _collect(close_exc)

    for e in all_excs:
        msg = str(e)
        if "401" in msg or "unauthorized" in msg.lower() or "oauth" in msg.lower():
            return "unauthenticated", f"Authentication required: {msg}"
        if "403" in msg or "forbidden" in msg.lower():
            return "unauthenticated", f"Access forbidden: {msg}"
        if "404" in msg or "not found" in msg.lower():
            return "error", f"Endpoint not found: {msg}"
        if "connecterror" in type(e).__name__.lower() or "connection refused" in msg.lower():
            return "error", f"Connection refused: {msg}"
        if isinstance(e, asyncio.TimeoutError):
            return "error", f"Connection timed out after {_PROBE_TIMEOUT:.1f}s"

    stderr_diag = _clean_stderr_diagnostic(stderr_output)
    if stderr_diag:
        return "error", stderr_diag

    for e in all_excs:
        msg = str(e)
        if (
            msg
            and not msg.startswith("Cancelled via cancel scope")
            and not msg.startswith("unhandled errors in a TaskGroup")
        ):
            return "error", msg

    if isinstance(exc, (asyncio.CancelledError, asyncio.TimeoutError)):
        return "error", "Connection timed out or failed to initialize"

    return "error", str(exc or close_exc or "Unknown connection failure")


async def probe_one_mcp_server(
    name: str,
    srv_config: Mapping[str, Any],
) -> MCPServerInfo:
    """Open a throwaway session to one MCP server and list its tools."""
    transport = _resolve_transport(srv_config)
    enabled = bool(srv_config.get("enabled", True))
    source = srv_config.get("source", "project")
    raw_disabled_tools = srv_config.get("disabled_tools") or srv_config.get("disabledTools") or ()
    raw_allowed_tools = srv_config.get("allowed_tools") or srv_config.get("allowedTools") or ()

    disabled_tools_tuple = tuple(raw_disabled_tools) if isinstance(raw_disabled_tools, (list, tuple)) else ()
    allowed_tools_tuple = tuple(raw_allowed_tools) if isinstance(raw_allowed_tools, (list, tuple)) else ()

    if not enabled:
        return MCPServerInfo(
            name=name,
            transport=transport,
            status="disabled",
            tools=(),
            enabled=False,
            url=srv_config.get("url") or srv_config.get("serverUrl"),
            command=srv_config.get("command"),
            args=tuple(srv_config.get("args") or ()),
            headers=dict(srv_config.get("headers") or {}),
            env=dict(srv_config.get("env") or {}),
            disabled_tools=disabled_tools_tuple,
            allowed_tools=allowed_tools_tuple,
            source=source,
        )

    # Resolve environment variables
    try:
        resolved_config = resolve_mcp_server_env(name, srv_config)
    except Exception as exc:
        logger.warning("MCP server '%s' env resolution failed: %s", name, exc)
        return MCPServerInfo(
            name=name,
            transport=transport,
            status="error",
            error=f"Environment variable error: {exc}",
            tools=(),
            enabled=enabled,
            url=srv_config.get("url") or srv_config.get("serverUrl"),
            command=srv_config.get("command"),
            args=tuple(srv_config.get("args") or ()),
            headers=dict(srv_config.get("headers") or {}),
            env=dict(srv_config.get("env") or {}),
            disabled_tools=disabled_tools_tuple,
            allowed_tools=allowed_tools_tuple,
            source=source,
        )

    exit_stack = AsyncExitStack()
    primary_exc: BaseException | None = None
    close_exc: BaseException | None = None
    captured_stderr: str | None = None
    try:
        from langchain_mcp_adapters.sessions import create_session

        from opscloud.mcp.session_manager import create_mcp_connection

        if transport == "stdio":
            import tempfile

            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            sync_mcp_env_aliases()
            stdio_env = dict(os.environ)
            raw_env = resolved_config.get("env")
            if isinstance(raw_env, dict):
                stdio_env.update(raw_env)
            raw_args = resolved_config.get("args")
            args_list = list(raw_args) if isinstance(raw_args, (list, tuple)) else []
            server_params = StdioServerParameters(
                command=str(resolved_config.get("command") or ""),
                args=args_list,
                env=stdio_env,
                cwd=resolved_config.get("cwd"),
            )
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
                try:
                    read_stream, write_stream = await asyncio.wait_for(
                        exit_stack.enter_async_context(stdio_client(server_params, errlog=stderr_file)),
                        timeout=_PROBE_TIMEOUT,
                    )
                    session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
                    await asyncio.wait_for(session.initialize(), timeout=_PROBE_TIMEOUT)
                    tools_result = await asyncio.wait_for(session.list_tools(), timeout=_PROBE_TIMEOUT)
                except BaseException as exc:
                    primary_exc = exc
                    try:
                        stderr_file.seek(0)
                        captured_stderr = stderr_file.read()
                    except Exception:
                        pass
                    raise
        else:
            conn = create_mcp_connection(resolved_config)
            session = await asyncio.wait_for(
                exit_stack.enter_async_context(create_session(conn)),
                timeout=_PROBE_TIMEOUT,
            )
            await asyncio.wait_for(session.initialize(), timeout=_PROBE_TIMEOUT)
            tools_result = await asyncio.wait_for(session.list_tools(), timeout=_PROBE_TIMEOUT)

        tools: list[MCPToolInfo] = []
        raw_tools = getattr(tools_result, "tools", []) or []
        for t in raw_tools:
            raw_name = getattr(t, "name", str(t))
            if not _filter_tool_names(
                raw_name,
                allowed_tools=allowed_tools_tuple,
                disabled_tools=disabled_tools_tuple,
            ):
                continue

            clean_schema = _clean_mcp_schema(getattr(t, "inputSchema", None))
            tool_name = _mcp_tool_name(name, raw_name)
            tools.append(
                MCPToolInfo(
                    name=tool_name,
                    description=getattr(t, "description", "") or "",
                    input_schema=clean_schema,
                    original_name=raw_name,
                )
            )

        return MCPServerInfo(
            name=name,
            transport=transport,
            status="ok",
            tools=tuple(tools),
            enabled=True,
            url=srv_config.get("url") or srv_config.get("serverUrl"),
            command=srv_config.get("command"),
            args=tuple(srv_config.get("args") or ()),
            headers=dict(srv_config.get("headers") or {}),
            env=dict(srv_config.get("env") or {}),
            disabled_tools=disabled_tools_tuple,
            allowed_tools=allowed_tools_tuple,
            source=source,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        primary_exc = exc
    finally:
        try:
            await exit_stack.aclose()
        except BaseException as ce:
            close_exc = ce

    status, err_msg = _extract_root_mcp_error(primary_exc, close_exc, captured_stderr)
    logger.warning("MCP server '%s' probe failed (%s): %s", name, status, err_msg)
    return MCPServerInfo(
        name=name,
        transport=transport,
        status=status,
        error=err_msg,
        tools=(),
        enabled=enabled,
        url=srv_config.get("url") or srv_config.get("serverUrl"),
        command=srv_config.get("command"),
        args=tuple(srv_config.get("args") or ()),
        headers=dict(srv_config.get("headers") or {}),
        env=dict(srv_config.get("env") or {}),
        disabled_tools=disabled_tools_tuple,
        allowed_tools=allowed_tools_tuple,
        source=source,
    )


_PROBED_CACHE: dict[str, MCPServerInfo] = {}


def get_cached_mcp_server_infos() -> list[MCPServerInfo]:
    """Return all cached/probed MCPServerInfo objects."""
    return list(_PROBED_CACHE.values())


def set_cached_mcp_server_info(info: MCPServerInfo) -> None:
    """Store or update a probed MCPServerInfo in the cache."""
    _PROBED_CACHE[info.name] = info


def evict_cached_mcp_server_info(name: str) -> None:
    """Evict a cached MCPServerInfo so it can be probed fresh."""
    _PROBED_CACHE.pop(name, None)


def clear_cached_mcp_server_infos() -> None:
    """Clear all cached MCPServerInfo entries."""
    _PROBED_CACHE.clear()


async def preload_mcp_metadata(
    configs: Mapping[str, Mapping[str, Any]],
) -> list[MCPServerInfo]:
    """Probe all configured MCP servers concurrently and return their MCPServerInfo."""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)

    async def _bound_probe(name: str, conf: Mapping[str, Any]) -> MCPServerInfo:
        async with semaphore:
            info = await probe_one_mcp_server(name, conf)
            set_cached_mcp_server_info(info)
            return info

    tasks = [_bound_probe(name, conf) for name, conf in configs.items()]
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return list(results)


def format_mcp_status_response(server_infos: list[MCPServerInfo]) -> dict[str, Any]:
    """Format probed server infos into the API status payload."""
    total_tools = sum(s.tool_count for s in server_infos)
    return {
        "total_tools": total_tools,
        "servers": [s.to_dict() for s in server_infos],
    }


async def preload_mcp_server_info(
    *,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool | None = None,
) -> list[MCPServerInfo]:
    """Discover MCP servers, probe them concurrently, and cache metadata.

    Args:
        mcp_config_path: Optional explicit path to an MCP config JSON.
        no_mcp: When True, skip all MCP loading.
        trust_project_mcp: Optional whole-config trust override.

    Returns:
        List of MCPServerInfo entries for discovered servers.
    """
    if no_mcp:
        return []

    import json
    from pathlib import Path
    from opscloud.mcp.discovery import MCPDiscovery

    discovery = MCPDiscovery()
    try:
        config: dict[str, Any] = dict(await discovery.discover_and_sync_async())
    except Exception as exc:
        logger.debug("Async discovery failed, falling back to sync: %s", exc)
        config = dict(discovery.discover())

    if mcp_config_path:
        try:
            path = Path(mcp_config_path)
            if path.is_file():
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                servers = data.get("mcpServers")
                if isinstance(servers, dict):
                    for name, srv_config in servers.items():
                        if isinstance(srv_config, dict):
                            config[name] = srv_config
        except Exception as e:
            logger.warning("Failed to load explicit MCP config %s: %s", mcp_config_path, e)

    if not config:
        return []

    return await preload_mcp_metadata(config)


__all__ = [
    "_mcp_tool_name",
    "clear_cached_mcp_server_infos",
    "evict_cached_mcp_server_info",
    "format_mcp_status_response",
    "get_cached_mcp_server_infos",
    "preload_mcp_metadata",
    "preload_mcp_server_info",
    "probe_one_mcp_server",
    "set_cached_mcp_server_info",
]

