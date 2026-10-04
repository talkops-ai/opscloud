"""MCP Session Manager with connection pooling, normalization, and lifecycle management."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
import fnmatch
import os
from typing import Any, cast

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from opscloud.mcp.config import resolve_mcp_server_env, sync_mcp_env_aliases
from opscloud.mcp.mcp_info import MCPServerInfo
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def _is_transient_session_error(exc: BaseException) -> bool:
    """Return True when exc signals the MCP session transport or stream is dead."""
    try:
        import anyio

        anyio_excs = (
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
            anyio.EndOfStream,
        )
    except ImportError:
        anyio_excs = ()
    return isinstance(
        exc,
        (
            *anyio_excs,
            BrokenPipeError,
            ConnectionAbortedError,
            ConnectionResetError,
            EOFError,
            asyncio.IncompleteReadError,
        ),
    )


def _normalize_mcp_arguments(arguments: dict[str, Any], input_schema: Any) -> dict[str, Any]:
    """Normalize MCP tool arguments, stripping empty string values for non-required fields."""
    if not isinstance(input_schema, dict):
        return arguments
    required = set(input_schema.get("required") or ())
    properties = input_schema.get("properties") or {}
    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        if value != "" or key in required:
            cleaned[key] = value
            continue
        prop = properties.get(key)
        prop_type = prop.get("type") if isinstance(prop, dict) else None
        is_string_typed = prop_type == "string" or (isinstance(prop_type, list) and "string" in prop_type)
        if isinstance(prop, dict) and not is_string_typed and prop_type is not None:
            cleaned[key] = value
    return cleaned


def _clean_mcp_schema(schema: Any) -> dict[str, Any]:
    """Clean JSON Schema for compatibility with LLM provider tool schemas (Gemini, Anthropic, OpenAI)."""
    if not isinstance(schema, dict):
        return {}

    cleaned = dict(schema)
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


def _filter_tool_name(
    tool_name: str,
    *,
    allowed_tools: list[str] | tuple[str, ...],
    disabled_tools: list[str] | tuple[str, ...],
) -> bool:
    """Check whether a tool name matches allowed/disabled rules."""
    if disabled_tools:
        for pat in disabled_tools:
            if pat and (pat == tool_name or fnmatch.fnmatch(tool_name, pat)):
                return False

    if allowed_tools:
        return any(pat == tool_name or fnmatch.fnmatch(tool_name, pat) for pat in allowed_tools if pat)

    return True


class _MCPSessionEntry:
    """Internal tracking entry for an active MCP session connection."""

    def __init__(self, session: Any, exit_stack: AsyncExitStack) -> None:
        """Initialize tracking entry for an active MCP session.

        Args:
            session: Active MCP client session instance.
            exit_stack: AsyncExitStack managing connection cleanup.
        """
        self.session = session
        self.exit_stack = exit_stack
        self._cached_tools: list[BaseTool] = []


def _enhance_mcp_error_diagnostics(error_text: str) -> str:
    """Append actionable troubleshooting guidance when encountering cloud, AWS, or connection errors."""
    lower = error_text.lower()

    # AWS Credential / Authentication Errors
    if any(k in lower for k in ("expiredtoken", "token has expired", "credentials has expired", "token is expired")):
        hint = (
            "\n[Diagnostic Hint: AWS credentials have expired. Refresh your AWS SSO session with `aws sso login` "
            "or export refreshed AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN.]"
        )
        if hint not in error_text:
            return f"{error_text}{hint}"

    if any(k in lower for k in ("nocredentialserror", "unable to locate credentials")):
        hint = (
            "\n[Diagnostic Hint: No AWS credentials found. Configure credentials via `aws configure` / `aws sso login`, "
            "set AWS_PROFILE, or export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.]"
        )
        if hint not in error_text:
            return f"{error_text}{hint}"

    if any(k in lower for k in ("accessdenied", "unauthorizedoperation", "is not authorized to perform")):
        hint = (
            "\n[Diagnostic Hint: AWS IAM Access Denied. Verify that the active AWS IAM principal / role "
            "has the required IAM policies and permissions for this operation.]"
        )
        if hint not in error_text:
            return f"{error_text}{hint}"

    if any(k in lower for k in ("endpointconnectionerror", "could not connect to the endpoint url")):
        hint = (
            "\n[Diagnostic Hint: Failed to connect to AWS endpoint URL. Check your network connection, "
            "verify AWS_REGION / AWS_DEFAULT_REGION, and verify any VPC endpoint / proxy configurations.]"
        )
        if hint not in error_text:
            return f"{error_text}{hint}"

    is_cluster_dns = ".svc" in lower or "cluster.local" in lower
    is_name_resolution_failure = any(
        indicator in lower
        for indicator in (
            "errnamenotresolved",
            "nameresolutionerror",
            "nodename nor servname provided",
            "temporary failure in name resolution",
            "name or service not known",
            "cannot connect to",
            "connection refused",
        )
    )
    if is_cluster_dns or (
        is_name_resolution_failure
        and any(svc in lower for svc in ("argocd", "prometheus", "alertmanager", "loki", "tempo", "traefik", "eks"))
    ):
        hint = (
            "\n[Diagnostic Hint: Failed to resolve or connect to target cluster endpoint. "
            "If running opscloud locally outside the cluster/VPC, forward the target service "
            "(e.g., `kubectl port-forward svc/<service-name> -n <namespace> <local_port>:<target_port>`) "
            "or check your VPN/tunnel connection, and configure the URL to point to `http://localhost:<local_port>`.]"
        )
        if hint not in error_text:
            return f"{error_text}{hint}"
    elif "connection closed" in lower or "broken pipe" in lower:
        hint = (
            "\n[Diagnostic Hint: The MCP server process terminated unexpectedly. "
            "Ensure the required CLI command (e.g. uvx, npx, python, docker), dependencies, "
            "and valid credentials are installed and in your system PATH.]"
        )
        if hint not in error_text:
            return f"{error_text}{hint}"
    return error_text


def create_mcp_connection(resolved_config: Mapping[str, Any]) -> Any:
    """Instantiate a LangChain MCP connection from resolved server configuration.

    Consolidates transport resolution (stdio, sse, http/streamable_http) and
    implements ambient environment variable inheritance for StdioConnection processes.
    """
    from langchain_mcp_adapters.sessions import (
        SSEConnection,
        StdioConnection,
        StreamableHttpConnection,
    )

    transport = str(resolved_config.get("type") or resolved_config.get("transport") or "").lower()
    url = str(resolved_config.get("url") or resolved_config.get("serverUrl") or "")
    headers = dict(resolved_config.get("headers") or {})

    if not transport or transport == "stdio":
        transport = ("sse" if "sse" in url.lower() else "http") if url else "stdio"

    if transport in ("http", "streamable_http"):
        return StreamableHttpConnection(  # type: ignore[call-arg]
            transport="streamable_http",
            url=url,
            headers=headers,
        )
    elif transport == "sse":
        return SSEConnection(  # type: ignore[call-arg]
            transport="sse",
            url=url,
            headers=headers,
        )
    else:
        sync_mcp_env_aliases()
        stdio_env = dict(os.environ)
        raw_env = resolved_config.get("env")
        if isinstance(raw_env, dict):
            stdio_env.update(raw_env)
        raw_args = resolved_config.get("args")
        args_list = list(raw_args) if isinstance(raw_args, (list, tuple)) else []
        conn_kwargs: dict[str, Any] = {
            "transport": "stdio",
            "command": str(resolved_config.get("command") or ""),
            "args": args_list,
            "env": stdio_env,
        }
        if resolved_config.get("cwd"):
            conn_kwargs["cwd"] = str(resolved_config["cwd"])
        return cast("StdioConnection", conn_kwargs)



def _build_cached_mcp_tool(
    *,
    mcp_tool: Any,
    server_name: str,
    session_manager: MCPSessionManager,
    name_prefix_sep: str = ":",
) -> BaseTool:
    from langchain_mcp_adapters.tools import (
        _convert_call_tool_result,
    )

    raw_tool_name = getattr(mcp_tool, "name", str(mcp_tool))
    if ":" in raw_tool_name and (not server_name or raw_tool_name.startswith(f"{server_name}:")):
        original_tool_name = raw_tool_name.split(":", 1)[1]
    else:
        original_tool_name = raw_tool_name

    lc_tool_name = f"{server_name}{name_prefix_sep}{original_tool_name}" if server_name else original_tool_name
    description = getattr(mcp_tool, "description", "") or f"MCP tool {original_tool_name} from {server_name}"
    raw_input_schema = getattr(mcp_tool, "inputSchema", None) or getattr(mcp_tool, "input_schema", None) or {}
    input_schema = _clean_mcp_schema(raw_input_schema)

    metadata: dict[str, Any] = {
        "_opscloud_mcp": True,
        "_mcp_server": server_name,
        "_mcp_original_name": original_tool_name,
    }
    from opscloud.mcp.semantic_profiler import MCPSemanticProfiler

    profiler = MCPSemanticProfiler.get_instance()
    profile = profiler.get_profile(server_name, original_tool_name)
    if profile is None:
        profile = profiler.heuristic_profile(mcp_tool, server_name=server_name)
        profiler.register_profile(server_name, original_tool_name, profile)

    metadata["inferred_tier"] = profile.inferred_tier
    metadata["readOnlyHint"] = profile.read_only_hint
    metadata["destructiveHint"] = profile.destructive_hint
    metadata["sensitive_arguments"] = profile.sensitive_arguments
    metadata["justification"] = profile.justification

    def _handle_tool_error(error: ToolException) -> str:
        raw_msg = str(error) or f"{lc_tool_name} failed with no error detail"
        logger.info("MCP tool %r returned error: %s", lc_tool_name, raw_msg)
        return _enhance_mcp_error_diagnostics(raw_msg)

    async def coroutine(
        runtime: Any = None,
        **arguments: Any,
    ) -> Any:
        """Execute the remote MCP tool with safety checks and error handling.

        Args:
            runtime: Optional LangGraph tool runtime context.
            **arguments: Keyword arguments to pass to the MCP tool.

        Returns:
            Any: Tool call result payload.
        """
        import time

        start_time = time.perf_counter()
        logger.info(
            "Invoking MCP tool '%s' on server '%s'",
            original_tool_name,
            server_name,
            extra={"mcp_server": server_name, "tool_name": original_tool_name},
        )
        cleaned_args = _normalize_mcp_arguments(arguments, input_schema)
        session = await session_manager.get_session(server_name)
        try:
            result = await session.call_tool(original_tool_name, cleaned_args)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, ToolException):
            raise
        except Exception as exc:
            if not _is_transient_session_error(exc):
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                logger.error(
                    "MCP tool '%s' execution failed: %s",
                    original_tool_name,
                    exc,
                    exc_info=True,
                    extra={
                        "mcp_server": server_name,
                        "tool_name": original_tool_name,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                msg = f"MCP tool {lc_tool_name!r} failed on server {server_name!r}: {exc}"
                raise ToolException(_enhance_mcp_error_diagnostics(msg)) from None

            # Transient connection failure handling:
            # Consistent with dcode's connection loss safety, do not automatically replay
            # mutating or destructive operations on a severed socket because the operation
            # may have already taken effect remotely on AWS.
            is_read_only = bool(
                profile.read_only_hint
                or (profile.inferred_tier and getattr(profile.inferred_tier, "value", str(profile.inferred_tier)) == "read_only")
            )
            if not is_read_only:
                await session_manager.invalidate(server_name)
                msg = (
                    f"Connection lost while calling MCP tool {lc_tool_name!r}. "
                    "The operation may have already been executed by AWS/cloud provider and was not "
                    "automatically retried to prevent unintended duplicate side effects. "
                    "Please verify the current resource state before trying again."
                )
                logger.warning("MCP connection lost on mutating tool %r; avoiding blind replay", lc_tool_name)
                raise ToolException(_enhance_mcp_error_diagnostics(msg)) from None

            logger.info("MCP session for %r dead; retrying read-only tool once", server_name, extra={"mcp_server": server_name})
            await session_manager.invalidate(server_name)
            retry_session = await session_manager.get_session(server_name)
            try:
                result = await retry_session.call_tool(original_tool_name, cleaned_args)
            except Exception as retry_exc:
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                logger.error(
                    "MCP tool '%s' execution failed: %s",
                    original_tool_name,
                    retry_exc,
                    exc_info=True,
                    extra={
                        "mcp_server": server_name,
                        "tool_name": original_tool_name,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                msg = f"MCP tool {lc_tool_name!r} failed after retry on server {server_name!r}: {retry_exc}"
                raise ToolException(_enhance_mcp_error_diagnostics(msg)) from None

        duration_ms = (time.perf_counter() - start_time) * 1000.0
        logger.info(
            "MCP tool '%s' executed in %.2fms",
            original_tool_name,
            duration_ms,
            extra={
                "mcp_server": server_name,
                "tool_name": original_tool_name,
                "duration_ms": round(duration_ms, 2),
            },
        )
        try:
            converted = _convert_call_tool_result(result)
            if isinstance(converted, tuple) and len(converted) == 2:
                return converted
            return converted, None
        except ToolException:
            # Let ToolException (including _MCPToolExecutionError) propagate to LangChain's handle_tool_error
            raise
        except Exception as exc:
            logger.warning(
                "Fallback formatting for MCP tool '%s' on server '%s': %s",
                original_tool_name,
                server_name,
                exc,
                extra={"mcp_server": server_name, "tool_name": original_tool_name},
            )
            content = getattr(result, "content", None)
            if content is not None:
                return content, None
            return str(result), None

    return StructuredTool(
        name=lc_tool_name,
        description=description,
        args_schema=input_schema if isinstance(input_schema, dict) else {},
        coroutine=coroutine,
        response_format="content_and_artifact",
        metadata=metadata,
        handle_tool_error=cast("Any", _handle_tool_error),
    )


def build_mcp_tools_from_server_infos(
    server_infos: Sequence[MCPServerInfo],
    session_manager: MCPSessionManager,
) -> list[BaseTool]:
    """Build LangChain StructuredTools from preloaded/probed MCPServerInfo list without opening persistent sessions eagerly."""
    tools: list[BaseTool] = []
    for info in server_infos:
        if not info.enabled or info.status != "ok":
            continue
        for t in info.tools:
            tool_obj = _build_cached_mcp_tool(
                mcp_tool=t,
                server_name=info.name,
                session_manager=session_manager,
                name_prefix_sep=":",
            )
            tools.append(tool_obj)
    return tools


class MCPSessionManager:
    """Manages connections to active MCP servers with connection pooling."""

    _instance: MCPSessionManager | None = None

    @classmethod
    def get_instance(cls, mcp_config: dict[str, Any] | None = None, *args: Any, **kwargs: Any) -> MCPSessionManager:
        """Retrieve the singleton instance of MCPSessionManager.

        Args:
            mcp_config: Optional MCP server configuration dictionary.
            *args: Positional arguments for instance creation.
            **kwargs: Keyword arguments for instance creation.

        Returns:
            MCPSessionManager: The singleton session manager instance.
        """
        if cls._instance is None:
            cls._instance = cls(mcp_config)
        elif mcp_config is not None:
            cls._instance.register_servers(mcp_config)
        return cls._instance

    def __init__(self, mcp_config: dict[str, Any] | None = None) -> None:
        """Initialize MCPSessionManager with server configuration.

        Args:
            mcp_config: Optional configuration dictionary containing server settings.
        """
        self._config: dict[str, Any] = mcp_config.get("mcpServers", mcp_config) if mcp_config else {}
        self._sessions: dict[str, _MCPSessionEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        self._errors: dict[str, str] = {}
        self._closed = False
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _get_server_lock(self, server_name: str) -> asyncio.Lock:
        if server_name not in self._locks:
            self._locks[server_name] = asyncio.Lock()
        return self._locks[server_name]

    @property
    def sessions(self) -> dict[str, Any]:
        """Dictionary of active MCP sessions mapped by server name.

        Returns:
            dict[str, Any]: Mapping of server names to active session instances.
        """
        return {name: entry.session for name, entry in self._sessions.items()}

    def register_server(self, name: str, config: Mapping[str, Any]) -> None:
        """Register or update a single server config without affecting other servers."""
        self._config[name] = dict(config)
        self._closed = False

    def register_servers(self, servers: Mapping[str, Any] | dict[str, Any]) -> None:
        """Register or update multiple server configs without affecting other servers."""
        raw_cfg = servers.get("mcpServers", servers) if isinstance(servers, Mapping) else {}
        if isinstance(raw_cfg, dict):
            self._config.update(raw_cfg)
        self._closed = False

    async def get_session(self, server_name: str) -> Any:
        """Return a cached session for `server_name`, creating it if needed."""
        entry = self._sessions.get(server_name)
        if entry is not None:
            return entry.session

        lock = self._get_server_lock(server_name)
        async with lock:
            entry = self._sessions.get(server_name)
            if entry is not None:
                return entry.session

            srv_config = self._config.get(server_name)
            if not srv_config:
                msg = f"No configuration found for MCP server {server_name!r}"
                raise RuntimeError(msg)

            await self.connect_server(server_name, srv_config)
            entry = self._sessions.get(server_name)
            if entry is None:
                err = self._errors.get(server_name, "Unknown error")
                msg = f"Failed to create session for MCP server {server_name!r}: {err}"
                raise RuntimeError(msg)
            return entry.session

    async def invalidate(self, server_name: str) -> None:
        """Close and evict a cached session."""
        lock = self._get_server_lock(server_name)
        async with lock:
            entry = self._sessions.pop(server_name, None)
            if entry is not None:
                try:
                    await entry.exit_stack.aclose()
                except Exception as exc:
                    logger.debug("Error closing invalidated session %s: %s", server_name, exc)

    def remove_server_config(self, server_name: str) -> None:
        """Remove configuration for a disabled or deleted server."""
        self._config.pop(server_name, None)

    async def remove_server(self, server_name: str) -> None:
        """Close any active session and remove configuration for server."""
        await self.invalidate(server_name)
        self.remove_server_config(server_name)

    def sync_active_configs(self, active_configs: dict[str, Any], purge_missing: bool = False) -> list[str]:
        """Synchronize active configurations, returning names of removed servers."""
        raw_cfg = active_configs.get("mcpServers", active_configs)
        removed: list[str] = []
        if purge_missing and isinstance(raw_cfg, dict):
            for name in list(self._config.keys()):
                if name not in raw_cfg:
                    self._config.pop(name, None)
                    removed.append(name)
        if isinstance(raw_cfg, dict):
            self._config.update(raw_cfg)

        for r_name in removed:
            if r_name in self._sessions:
                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(self.invalidate(r_name))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                except RuntimeError:
                    pass
        return removed

    def get_tool(self, name: str) -> BaseTool | None:
        """Find and return a cached LangChain BaseTool matching `name` or suffix."""
        for entry in self._sessions.values():
            for t in entry._cached_tools:
                if t.name == name or t.name.endswith(f":{name}") or t.name.endswith(f"_{name}"):
                    return t
        return None

    async def get_tools(self) -> list[BaseTool]:
        """Return LangChain BaseTool instances from all enabled connected MCP servers."""
        await self.connect_all()
        all_tools: list[BaseTool] = []
        for entry in self._sessions.values():
            all_tools.extend(entry._cached_tools)
        return all_tools

    async def close_all(self) -> None:
        """Close all active sessions and clean up resources with bounded timeout."""
        async with self._lock:
            for name, entry in list(self._sessions.items()):
                try:
                    await asyncio.wait_for(entry.exit_stack.aclose(), timeout=5.0)
                except TimeoutError:
                    logger.warning("MCP session %s teardown timed out after 5.0s", name)
                except Exception as exc:
                    logger.debug("Error closing MCP session %s: %s", name, exc)
            self._sessions.clear()
            self._errors.clear()
            self._closed = True

    async def cleanup(self) -> None:
        """Alias to close_all() for lifecycle shutdown."""
        await self.close_all()

    async def connect_server(self, name: str, config: Mapping[str, Any]) -> bool:
        """Connect to a single MCP server and initialize its tools."""
        enabled = bool(config.get("enabled", True))
        if not enabled:
            return False

        try:
            resolved_config = resolve_mcp_server_env(name, config)
        except Exception as exc:
            self._errors[name] = str(exc)
            logger.warning("Failed to resolve env for MCP server %s: %s", name, exc)
            return False

        transport = (
            resolved_config.get("type")
            or resolved_config.get("transport")
            or ("sse" if "sse" in str(resolved_config.get("url", "")).lower() else "http")
            if resolved_config.get("url")
            else "stdio"
        )
        logger.info(
            "Initializing MCP client session for server '%s'",
            name,
            extra={"mcp_server": name, "transport": transport},
        )

        exit_stack = AsyncExitStack()
        try:
            from langchain_mcp_adapters.sessions import create_session

            conn = create_mcp_connection(resolved_config)

            session = await asyncio.wait_for(
                exit_stack.enter_async_context(create_session(conn)),
                timeout=5.0,
            )
            await asyncio.wait_for(session.initialize(), timeout=5.0)

            # Discover and wrap tools
            tools_result = await asyncio.wait_for(session.list_tools(), timeout=5.0)
            raw_tools = getattr(tools_result, "tools", []) or []
            logger.info(
                "Discovered %d tools from MCP server '%s'",
                len(raw_tools),
                name,
                extra={
                    "mcp_server": name,
                    "tool_count": len(raw_tools),
                    "tools": [getattr(t, "name", str(t)) for t in raw_tools],
                },
            )

            raw_disabled = resolved_config.get("disabled_tools") or resolved_config.get("disabledTools") or ()
            raw_allowed = resolved_config.get("allowed_tools") or resolved_config.get("allowedTools") or ()
            disabled_tools = tuple(raw_disabled) if isinstance(raw_disabled, (list, tuple)) else ()
            allowed_tools = tuple(raw_allowed) if isinstance(raw_allowed, (list, tuple)) else ()

            wrapped_tools: list[BaseTool] = []
            for t in raw_tools:
                raw_name = getattr(t, "name", str(t))
                if not _filter_tool_name(
                    raw_name,
                    allowed_tools=allowed_tools,
                    disabled_tools=disabled_tools,
                ):
                    continue

                tool_obj = _build_cached_mcp_tool(
                    mcp_tool=t,
                    server_name=name,
                    session_manager=self,
                    name_prefix_sep=":",
                )
                wrapped_tools.append(tool_obj)

            entry = _MCPSessionEntry(session, exit_stack)
            entry._cached_tools = wrapped_tools

            self._sessions[name] = entry
            self._errors.pop(name, None)
            return True
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            try:
                await exit_stack.aclose()
            except BaseException as close_exc:
                logger.debug("Error closing exit stack for MCP server %s: %s", name, close_exc)
            self._errors[name] = str(exc)
            logger.warning("Failed to connect to MCP server %s: %s", name, exc)
            return False

    async def connect_all(self) -> None:
        """Connect to all configured enabled MCP servers in parallel."""
        async with self._lock:
            tasks = [
                self.connect_server(name, config)
                for name, config in self._config.items()
                if config.get("enabled", True) and name not in self._sessions
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
