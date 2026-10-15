"""Preload MCP server metadata and eagerly probe tools with diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AsyncExitStack, nullcontext
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any

try:
    from filelock import FileLock
except ImportError:  # pragma: no cover - dependency is declared by the project
    FileLock = None  # type: ignore[assignment,misc]

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


def _config_fingerprint(config: Mapping[str, Any]) -> str:
    """Return a stable digest so same-name config changes invalidate metadata."""
    payload = json.dumps(
        dict(config),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Compose a provider-safe MCP tool name within max length limits."""
    raw_name = f"{server_name}_{tool_name}"
    sanitized = _MCP_TOOL_NAME_RE.sub("_", raw_name).strip("_") or "unnamed"
    if sanitized == raw_name and len(sanitized) <= _MCP_TOOL_NAME_MAX_LENGTH:
        result = sanitized
    else:
        digest = hashlib.sha256(f"{server_name}\0{tool_name}".encode()).hexdigest()[
            :_MCP_TOOL_NAME_HASH_LENGTH
        ]
        server = _MCP_TOOL_NAME_RE.sub("_", server_name).strip("_") or "unnamed"
        tool = _MCP_TOOL_NAME_RE.sub("_", tool_name).strip("_") or "unnamed"
        available = _MCP_TOOL_NAME_MAX_LENGTH - len(digest) - 2
        tool_length = min(len(tool), available // 2)
        server_length = min(len(server), available - tool_length)
        tool_length = min(len(tool), available - server_length)
        result = f"{server[:server_length]}_{tool[:tool_length]}_{digest}"

    try:
        from opscloud.ui.widgets.tool_display import (
            extract_server_scope,
            register_scoped_tool_name,
        )

        scope = extract_server_scope(server_name)
        if scope:
            register_scoped_tool_name(result, tool_name, scope)
    except Exception:
        pass
    return result


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


def _clean_mcp_schema(schema: Any, max_ref_depth: int = 3) -> dict[str, Any]:
    """Clean JSON Schema for compatibility with LLM provider tool schemas (Gemini, Anthropic, OpenAI).

    Safely inlines local `$ref` pointers (such as `#/$defs/...` or `#/definitions/...`)
    up to a bounded ref depth, strips provider-incompatible metadata keys (`$schema`, `$id`,
    `additionalProperties`), and prevents recursive bloat or corrupted empty keys.
    """
    if not isinstance(schema, dict):
        return {}

    defs: dict[str, Any] = {}
    if isinstance(schema.get("$defs"), dict):
        defs.update(schema["$defs"])
    if isinstance(schema.get("definitions"), dict):
        defs.update(schema["definitions"])

    def _resolve(node: Any, ref_depth: int, seen_refs: set[str]) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if (
                    ref_depth >= max_ref_depth
                    or not isinstance(ref, str)
                    or not ref.startswith("#/")
                    or ref in seen_refs
                ):
                    return {"type": "object"}

                parts = ref.lstrip("#/").split("/")
                target = None
                if len(parts) >= 2 and parts[0] in ("$defs", "definitions"):
                    target = defs.get(parts[1])
                elif len(parts) == 1 and parts[0] in defs:
                    target = defs.get(parts[0])

                if isinstance(target, dict):
                    merged = dict(target)
                    for k, v in node.items():
                        if k != "$ref":
                            merged[k] = v
                    return _resolve(merged, ref_depth + 1, seen_refs | {ref})
                return {"type": "object"}

            res = {}
            for k, v in node.items():
                if k in ("$schema", "$id", "additionalProperties", "$defs", "definitions"):
                    continue
                res[k] = _resolve(v, ref_depth, seen_refs)
            return res

        if isinstance(node, list):
            return [_resolve(item, ref_depth, seen_refs) for item in node]

        return node

    result = _resolve(schema, 0, set())
    return result if isinstance(result, dict) else {}


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


def _schema_state_write_enabled() -> bool:
    if os.environ.get("OPSCLOUD_MCP_SCHEMA_DIR_DISABLED") == "1":
        return False
    return not (
        os.environ.get("PYTEST_CURRENT_TEST")
        and not os.environ.get("OPSCLOUD_MCP_SCHEMA_DIR")
    )


def get_mcp_server_state_dir(server_name: str | None = None) -> Path:
    """Return the directory where per-server MCP tool schema JSON files are written.

    Writes schemas under ``~/.opscloud/mcp/<server_name>/<tool_name>.json``.
    """
    override = os.environ.get("OPSCLOUD_MCP_SCHEMA_DIR")
    if override:
        base = Path(override).expanduser()
    else:
        from opscloud.config.paths import DATA_DIR

        base = DATA_DIR / "mcp"
    if server_name:
        safe_server = re.sub(r"[/\\]+", "_", server_name.strip()) or "unnamed"
        return base / safe_server
    return base


def write_mcp_server_state(
    info: MCPServerInfo,
    *,
    state_dir: Path | None = None,
) -> Path | None:
    """Atomically materialize each tool's schema to ``<mcp_dir>/<server>/<tool>.json``.

    Writes one ``<toolName>.json`` per tool and removes stale ``.json`` files from
    previous runs.
    """
    if not info.enabled or info.status != "ok":
        return None
    if state_dir is None and not _schema_state_write_enabled():
        return None

    server_dir = (state_dir / re.sub(r"[/\\]+", "_", info.name.strip())) if state_dir else get_mcp_server_state_dir(info.name)
    try:
        server_dir.mkdir(parents=True, exist_ok=True)
        expected_files: set[str] = set()
        for tool in info.tools:
            raw_name = tool.original_name or (
                tool.name.split(":", 1)[1] if ":" in tool.name else tool.name
            )
            safe_tool = re.sub(r"[/\\]+", "_", str(raw_name).strip()) or "unnamed"
            filename = f"{safe_tool}.json"
            expected_files.add(filename)
            target = server_dir / filename
            schema_dict = dict(tool.input_schema) if isinstance(tool.input_schema, Mapping) else {}
            payload = {
                "name": raw_name,
                "qualified_name": tool.name,
                "server_name": info.name,
                "description": tool.description,
                "inputSchema": schema_dict,
                "parameters": schema_dict,
            }
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=server_dir,
                    prefix=f".{safe_tool}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    temp_path = Path(handle.name)
                os.replace(temp_path, target)
            except OSError:
                if temp_path is not None:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        # Prune stale .json schema files no longer exposed by this server
        for existing in server_dir.glob("*.json"):
            if existing.name not in expected_files:
                try:
                    existing.unlink(missing_ok=True)
                except OSError:
                    pass
        return server_dir
    except OSError as exc:
        logger.debug("Could not write MCP server state for %s: %s", info.name, exc)
        return None


def read_mcp_tool_schema(
    server_name: str,
    tool_name: str,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Read a materialized MCP tool schema from disk if available."""
    safe_tool = re.sub(r"[/\\]+", "_", str(tool_name).strip()) or "unnamed"
    candidates = [server_name]
    if "__" in server_name:
        base_srv = server_name.rsplit("__", 1)[-1]
        if base_srv and base_srv not in candidates:
            candidates.append(base_srv)

    for srv in candidates:
        srv_dir = (
            state_dir / re.sub(r"[/\\]+", "_", srv.strip())
            if state_dir is not None
            else get_mcp_server_state_dir(srv)
        )
        schema_file = srv_dir / f"{safe_tool}.json"
        if schema_file.is_file():
            try:
                data = json.loads(schema_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError, TypeError):
                continue
    return None


def build_mcp_server_info_from_raw_tools(
    name: str,
    srv_config: Mapping[str, Any],
    raw_tools: Any,
) -> MCPServerInfo:
    """Construct an ``MCPServerInfo`` from raw MCP ``list_tools()`` items."""
    transport = _resolve_transport(srv_config)
    source = str(srv_config.get("source", "project"))
    raw_disabled_tools = srv_config.get("disabled_tools") or srv_config.get("disabledTools") or ()
    raw_allowed_tools = srv_config.get("allowed_tools") or srv_config.get("allowedTools") or ()
    disabled_tools_tuple = tuple(raw_disabled_tools) if isinstance(raw_disabled_tools, (list, tuple)) else ()
    allowed_tools_tuple = tuple(raw_allowed_tools) if isinstance(raw_allowed_tools, (list, tuple)) else ()

    tools: list[MCPToolInfo] = []
    for t in raw_tools or ():
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

    # Reuse an already-connected live session in MCPSessionManager if present,
    # avoiding a redundant second subprocess spawn.
    try:
        from opscloud.mcp.session_manager import MCPSessionManager

        active_mgr = MCPSessionManager._instance
        if active_mgr is not None and name in active_mgr._sessions:
            entry = active_mgr._sessions.get(name)
            if entry is not None and getattr(entry, "session", None) is not None:
                async with asyncio.timeout(_PROBE_TIMEOUT):
                    tools_result = await entry.session.list_tools()
                raw_tools = getattr(tools_result, "tools", []) or []
                info = build_mcp_server_info_from_raw_tools(name, srv_config, raw_tools)
                write_mcp_server_state(info)
                return info
    except Exception as exc:
        logger.debug("Live session reuse in probe_one_mcp_server skipped for %s: %s", name, exc)

    exit_stack = AsyncExitStack()
    primary_exc: BaseException | None = None
    close_exc: BaseException | None = None
    captured_stderr: str | None = None
    try:
        from langchain_mcp_adapters.sessions import create_session

        from opscloud.mcp.session_manager import create_mcp_connection

        if transport == "stdio":
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
                    async with asyncio.timeout(_PROBE_TIMEOUT):
                        read_stream, write_stream = await exit_stack.enter_async_context(
                            stdio_client(server_params, errlog=stderr_file)
                        )
                        session = await exit_stack.enter_async_context(
                            ClientSession(read_stream, write_stream)
                        )
                    async with asyncio.timeout(_PROBE_TIMEOUT):
                        await session.initialize()
                    async with asyncio.timeout(_PROBE_TIMEOUT):
                        tools_result = await session.list_tools()
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
            async with asyncio.timeout(_PROBE_TIMEOUT):
                session = await exit_stack.enter_async_context(create_session(conn))
            async with asyncio.timeout(_PROBE_TIMEOUT):
                await session.initialize()
            async with asyncio.timeout(_PROBE_TIMEOUT):
                tools_result = await session.list_tools()

        raw_tools = getattr(tools_result, "tools", []) or []
        info = build_mcp_server_info_from_raw_tools(name, srv_config, raw_tools)
        write_mcp_server_state(info)
        return info
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
_PROBED_CONFIG_FINGERPRINTS: dict[str, str] = {}
_PERSISTED_ENTRIES: dict[str, dict[str, Any]] = {}
_PERSISTENT_CACHE_LOADED = False
_CACHE_LOCK = threading.RLock()
_PERSISTENT_CACHE_VERSION = 1
_DEFAULT_METADATA_CACHE_TTL = 24 * 60 * 60


def _persistent_cache_enabled() -> bool:
    if os.environ.get("OPSCLOUD_MCP_METADATA_CACHE_DISABLED") == "1":
        return False
    return not (
        os.environ.get("PYTEST_CURRENT_TEST")
        and not os.environ.get("OPSCLOUD_MCP_METADATA_CACHE")
    )


def _metadata_cache_path() -> Path:
    override = os.environ.get("OPSCLOUD_MCP_METADATA_CACHE")
    if override:
        return Path(override).expanduser()
    from opscloud.config.paths import STATE_DIR

    return STATE_DIR / "mcp_metadata_cache.json"


def _persistent_file_lock() -> Any:
    """Return an inter-process lock for cache read/merge/write operations."""
    if FileLock is None:
        return nullcontext()
    return FileLock(f"{_metadata_cache_path()}.lock", timeout=5)


def _ensure_persistent_cache_loaded() -> None:
    """Load the schema cache once without ever loading credentials from disk."""
    global _PERSISTENT_CACHE_LOADED
    if not _persistent_cache_enabled():
        return
    if _PERSISTENT_CACHE_LOADED:
        return
    with _CACHE_LOCK:
        if _PERSISTENT_CACHE_LOADED:
            return
        _PERSISTENT_CACHE_LOADED = True
        try:
            with _persistent_file_lock():
                payload = json.loads(_metadata_cache_path().read_text(encoding="utf-8"))
                if payload.get("version") != _PERSISTENT_CACHE_VERSION:
                    return
                entries = payload.get("entries")
                if isinstance(entries, dict):
                    _PERSISTED_ENTRIES.update(
                        (str(name), entry)
                        for name, entry in entries.items()
                        if isinstance(entry, dict)
                    )
        except (OSError, ValueError, TypeError, TimeoutError):
            return


def _server_info_from_persisted(
    name: str,
    config: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> MCPServerInfo | None:
    info = entry.get("info")
    if not isinstance(info, Mapping) or info.get("status") != "ok":
        return None
    raw_tools = info.get("tools")
    tools: list[MCPToolInfo] = []
    if isinstance(raw_tools, list):
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, Mapping) or not raw_tool.get("name"):
                continue
            schema = raw_tool.get("input_schema")
            tools.append(
                MCPToolInfo(
                    name=str(raw_tool["name"]),
                    description=str(raw_tool.get("description") or ""),
                    input_schema=dict(schema) if isinstance(schema, Mapping) else {},
                    original_name=(
                        str(raw_tool["original_name"])
                        if raw_tool.get("original_name") is not None
                        else None
                    ),
                )
            )

    raw_disabled = config.get("disabled_tools") or config.get("disabledTools") or ()
    raw_allowed = config.get("allowed_tools") or config.get("allowedTools") or ()
    return MCPServerInfo(
        name=name,
        transport=_resolve_transport(config),
        status="ok",
        tools=tuple(tools),
        enabled=bool(config.get("enabled", True)),
        source=str(config.get("source", "project")),
        url=config.get("url") or config.get("serverUrl"),
        command=config.get("command"),
        args=tuple(config.get("args") or ()),
        headers=dict(config.get("headers") or {}),
        env=dict(config.get("env") or {}),
        disabled_tools=tuple(raw_disabled) if isinstance(raw_disabled, (list, tuple)) else (),
        allowed_tools=tuple(raw_allowed) if isinstance(raw_allowed, (list, tuple)) else (),
    )


def _record_persisted_entry(
    info: MCPServerInfo,
    fingerprint: str,
) -> None:
    if info.status != "ok":
        return
    _PERSISTED_ENTRIES[info.name] = {
        "fingerprint": fingerprint,
        "cached_at": time.time(),
        "info": {
            "status": info.status,
            "tools": [tool.to_dict() for tool in info.tools],
        },
    }


def _write_persistent_cache() -> None:
    """Atomically persist only fingerprints and non-secret tool schemas."""
    if not _persistent_cache_enabled():
        return
    with _CACHE_LOCK:
        path = _metadata_cache_path()
        temp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _persistent_file_lock():
                disk_entries: dict[str, Any] = {}
                try:
                    disk_payload = json.loads(path.read_text(encoding="utf-8"))
                    raw_entries = disk_payload.get("entries")
                    if isinstance(raw_entries, dict):
                        disk_entries = {
                            str(name): entry
                            for name, entry in raw_entries.items()
                            if isinstance(entry, dict)
                        }
                except (OSError, ValueError, TypeError):
                    pass
                # Entries generated by this process win over stale disk data,
                # while unrelated entries from another process are preserved.
                disk_entries.update(_PERSISTED_ENTRIES)
                payload = {
                    "version": _PERSISTENT_CACHE_VERSION,
                    "entries": disk_entries,
                }
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                    temp_path = Path(handle.name)
                os.replace(temp_path, path)
        except (OSError, TimeoutError) as exc:
            logger.debug("Could not persist MCP metadata cache: %s", exc)
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def get_cached_mcp_server_infos() -> list[MCPServerInfo]:
    """Return all cached/probed MCPServerInfo objects."""
    return list(_PROBED_CACHE.values())


def is_mcp_server_info_cached(
    name: str,
    config: Mapping[str, Any],
) -> bool:
    """Return whether memory or persistent metadata matches this config."""
    fingerprint = _config_fingerprint(config)
    if (
        name in _PROBED_CACHE
        and _PROBED_CONFIG_FINGERPRINTS.get(name) == fingerprint
    ):
        return True

    _ensure_persistent_cache_loaded()
    entry = _PERSISTED_ENTRIES.get(name)
    if not isinstance(entry, Mapping) or entry.get("fingerprint") != fingerprint:
        return False
    try:
        ttl = float(
            os.environ.get(
                "OPSCLOUD_MCP_METADATA_CACHE_TTL",
                str(_DEFAULT_METADATA_CACHE_TTL),
            )
        )
        cached_at = float(entry.get("cached_at", 0.0))
    except (TypeError, ValueError):
        return False
    if ttl >= 0 and time.time() - cached_at > ttl:
        return False

    info = _server_info_from_persisted(name, config, entry)
    if info is None:
        return False
    _PROBED_CACHE[name] = info
    _PROBED_CONFIG_FINGERPRINTS[name] = fingerprint
    return True


def set_cached_mcp_server_info(
    info: MCPServerInfo,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Store or update a probed server and its configuration identity."""
    _PROBED_CACHE[info.name] = info
    write_mcp_server_state(info)
    if config is not None:
        fingerprint = _config_fingerprint(config)
        _PROBED_CONFIG_FINGERPRINTS[info.name] = fingerprint
        with _CACHE_LOCK:
            _record_persisted_entry(info, fingerprint)


def evict_cached_mcp_server_info(name: str) -> None:
    """Evict a cached MCPServerInfo so it can be probed fresh."""
    _PROBED_CACHE.pop(name, None)
    _PROBED_CONFIG_FINGERPRINTS.pop(name, None)
    _PERSISTED_ENTRIES.pop(name, None)


def clear_cached_mcp_server_infos() -> None:
    """Clear process metadata and prevent stale disk rehydration this run."""
    global _PERSISTENT_CACHE_LOADED
    _PROBED_CACHE.clear()
    _PROBED_CONFIG_FINGERPRINTS.clear()
    _PERSISTED_ENTRIES.clear()
    _PERSISTENT_CACHE_LOADED = True


async def preload_mcp_metadata(
    configs: Mapping[str, Mapping[str, Any]],
) -> list[MCPServerInfo]:
    """Probe all configured MCP servers concurrently and return their MCPServerInfo."""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)

    async def _bound_probe(name: str, conf: Mapping[str, Any]) -> MCPServerInfo:
        async with semaphore:
            info = await probe_one_mcp_server(name, conf)
            set_cached_mcp_server_info(info, conf)
            return info

    tasks = [
        _bound_probe(name, conf)
        for name, conf in configs.items()
        if not is_mcp_server_info_cached(name, conf)
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=False)
        _write_persistent_cache()
    return [
        _PROBED_CACHE[name]
        for name in configs
        if name in _PROBED_CACHE
    ]


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
    "build_mcp_server_info_from_raw_tools",
    "clear_cached_mcp_server_infos",
    "evict_cached_mcp_server_info",
    "format_mcp_status_response",
    "get_cached_mcp_server_infos",
    "get_mcp_server_state_dir",
    "is_mcp_server_info_cached",
    "preload_mcp_metadata",
    "preload_mcp_server_info",
    "probe_one_mcp_server",
    "read_mcp_tool_schema",
    "set_cached_mcp_server_info",
    "write_mcp_server_state",
]


