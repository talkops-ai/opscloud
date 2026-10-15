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

_DEFAULT_CONNECT_TIMEOUT = 30.0
_CONNECT_TIMEOUT = float(os.environ.get("OPSCLOUD_MCP_CONNECT_TIMEOUT", str(_DEFAULT_CONNECT_TIMEOUT)))


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

    def __init__(
        self,
        session: Any,
        exit_stack: AsyncExitStack,
        *,
        close_event: asyncio.Event | None = None,
        owner_task: asyncio.Task[None] | None = None,
    ) -> None:
        """Initialize tracking entry for an active MCP session.

        Args:
            session: Active MCP client session instance.
            exit_stack: AsyncExitStack managing connection cleanup.
            close_event: Optional event signalling the owner task to close the session.
            owner_task: Optional background task that entered the session context.
        """
        self.session = session
        self.exit_stack = exit_stack
        self._cached_tools: list[BaseTool] = []
        self._close_event = close_event
        self._owner_task = owner_task

    async def aclose(self) -> None:
        """Close the MCP session within its owning task to satisfy AnyIO cancel-scope invariants."""
        if self._close_event is not None and self._owner_task is not None:
            self._close_event.set()
            try:
                async with asyncio.timeout(5.0):
                    await asyncio.shield(self._owner_task)
            except TimeoutError:
                self._owner_task.cancel()
            except BaseException:
                pass
        else:
            await self.exit_stack.aclose()


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

    orig_name = getattr(mcp_tool, "original_name", None)
    if isinstance(orig_name, str) and orig_name:
        original_tool_name = orig_name
        lc_tool_name = str(getattr(mcp_tool, "name", orig_name))
    else:
        raw_tool_name = getattr(mcp_tool, "name", str(mcp_tool))
        if isinstance(raw_tool_name, str) and ":" in raw_tool_name and (not server_name or raw_tool_name.startswith(f"{server_name}:")):
            original_tool_name = raw_tool_name.split(":", 1)[1]
        else:
            original_tool_name = raw_tool_name
        lc_tool_name = f"{server_name}{name_prefix_sep}{original_tool_name}" if server_name else original_tool_name
    description = getattr(mcp_tool, "description", "") or f"MCP tool {original_tool_name} from {server_name}"
    raw_input_schema = getattr(mcp_tool, "inputSchema", None) or getattr(mcp_tool, "input_schema", None) or {}
    input_schema = _clean_mcp_schema(raw_input_schema)

    base_server = server_name.rsplit("__", 1)[-1] if "__" in server_name else server_name
    subagent_owner = (
        server_name[10:].split("__", 1)[0]
        if server_name.startswith("subagent__") and "__" in server_name[10:]
        else None
    )

    try:
        from opscloud.ui.widgets.tool_display import (
            extract_server_scope,
            register_scoped_tool_name,
        )

        scope = extract_server_scope(server_name)
        if scope:
            register_scoped_tool_name(lc_tool_name, original_tool_name, scope)
    except Exception:
        pass

    metadata: dict[str, Any] = {
        "_opscloud_mcp": True,
        "_mcp_server": server_name,
        "_mcp_base_server": base_server,
        "_mcp_original_name": original_tool_name,
        "_mcp_subagent_owner": subagent_owner,
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


def _lazy_mcp_tools_enabled() -> bool:
    """Return True when global lazy MCP tool loading is enabled."""
    raw = (
        os.environ.get("OPSCLOUD_MCP_LAZY_TOOLS")
        or os.environ.get("OPSCLOUD_LAZY_MCP_TOOLS")
        or "0"
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def split_enabled_tools_by_eager_lazy(
    info: MCPServerInfo,
    config: Mapping[str, Any] | None = None,
) -> tuple[list[Any], list[Any]]:
    """Partition an MCP server's enabled tools into ``(eager_tools, lazy_tools)``.

    Resolution rules:
    - Tools are eager by default so plugin subagents and skills can invoke their
      declared ``<server>:<tool>`` tools directly in one turn, or lazy by default
      when ``OPSCLOUD_MCP_LAZY_TOOLS=1`` / ``lazy_load: true`` / ``eager_tools`` is set.
    - Server-level ``eager_load`` / ``eagerLoad: true`` marks all tools on the server eager
      unless overridden per tool in ``tool_config`` / ``toolConfig`` or ``lazy_tools``.
    - Server-level ``eager_tools`` / ``eagerTools`` list marks matching tools eager (and others lazy).
    - Server-level ``lazy_tools`` / ``lazyTools`` list marks matching tools lazy.
    - Per-tool ``tool_config[<tool_name>].eager_load`` / ``eagerLoad`` overrides the server default.
    """
    all_tools = list(getattr(info, "tools", ()) or ())
    global_lazy = _lazy_mcp_tools_enabled()

    tool_cfg_map: Mapping[str, Any] = {}
    eager_tools_list: tuple[str, ...] = ()
    lazy_tools_list: tuple[str, ...] = ()
    has_server_lazy_cfg = False
    server_eager = not global_lazy

    if isinstance(config, Mapping):
        raw_tc = config.get("tool_config") or config.get("toolConfig")
        if isinstance(raw_tc, Mapping) and raw_tc:
            tool_cfg_map = raw_tc
            has_server_lazy_cfg = True
        raw_et = config.get("eager_tools") or config.get("eagerTools")
        if isinstance(raw_et, (list, tuple, set)):
            eager_tools_list = tuple(str(x) for x in raw_et)
            has_server_lazy_cfg = True
        raw_lt = config.get("lazy_tools") or config.get("lazyTools")
        if isinstance(raw_lt, (list, tuple, set)):
            lazy_tools_list = tuple(str(x) for x in raw_lt)
            has_server_lazy_cfg = True

        if "lazy_load" in config or "lazyLoad" in config:
            has_server_lazy_cfg = True
            server_eager = not bool(config.get("lazy_load") or config.get("lazyLoad"))
        elif "eager_load" in config or "eagerLoad" in config:
            has_server_lazy_cfg = True
            server_eager = bool(config.get("eager_load") or config.get("eagerLoad"))
        elif eager_tools_list and not lazy_tools_list:
            server_eager = False

    if not global_lazy and not has_server_lazy_cfg:
        return all_tools, []

    eager: list[Any] = []
    lazy: list[Any] = []
    for tool in all_tools:
        raw_name = getattr(tool, "original_name", None) or getattr(tool, "name", str(tool))
        if isinstance(raw_name, str) and ":" in raw_name:
            raw_name = raw_name.split(":", 1)[1]
        full_name = str(getattr(tool, "name", raw_name))

        is_eager = server_eager
        if not is_eager and eager_tools_list:
            if any(
                pat in (str(raw_name), full_name)
                or fnmatch.fnmatch(str(raw_name), pat)
                or fnmatch.fnmatch(full_name, pat)
                for pat in eager_tools_list
            ):
                is_eager = True
        if is_eager and lazy_tools_list:
            if any(
                pat in (str(raw_name), full_name)
                or fnmatch.fnmatch(str(raw_name), pat)
                or fnmatch.fnmatch(full_name, pat)
                for pat in lazy_tools_list
            ):
                is_eager = False
        per_tool = tool_cfg_map.get(str(raw_name)) or tool_cfg_map.get(full_name)
        if isinstance(per_tool, Mapping):
            if "eager_load" in per_tool:
                is_eager = bool(per_tool["eager_load"])
            elif "eagerLoad" in per_tool:
                is_eager = bool(per_tool["eagerLoad"])
            elif "lazy_load" in per_tool:
                is_eager = not bool(per_tool["lazy_load"])
            elif "lazyLoad" in per_tool:
                is_eager = not bool(per_tool["lazyLoad"])
        elif isinstance(per_tool, bool):
            is_eager = per_tool

        if is_eager:
            eager.append(tool)
        else:
            lazy.append(tool)

    return eager, lazy


def build_call_mcp_tool(
    session_manager: MCPSessionManager,
    *,
    allowed_servers: Sequence[str] | set[str] | None = None,
    server_infos: Sequence[MCPServerInfo] | None = None,
) -> BaseTool:
    """Build the ``call_mcp_tool`` dispatcher for lazy-loaded MCP tools.

    Accepts ``server_name``, ``tool_name``, and ``arguments``, resolves the tool's
    cached/on-disk schema for argument normalization and safety profiling, and
    invokes the tool on the pooled ``MCPSessionManager`` session.
    """
    from langchain_mcp_adapters.tools import _convert_call_tool_result
    from opscloud.mcp.preload import get_cached_mcp_server_infos, read_mcp_tool_schema
    from opscloud.mcp.semantic_profiler import MCPSemanticProfiler

    allowed_set: set[str] | None = set(allowed_servers) if allowed_servers is not None else None
    info_by_name: dict[str, MCPServerInfo] = {
        info.name: info for info in (server_infos or ()) if getattr(info, "name", None)
    }

    def _resolve_server_name(requested: str) -> str:
        req = requested.strip()
        if not req:
            raise ToolException("server_name is required for call_mcp_tool.")

        candidates = (
            allowed_set
            if allowed_set is not None
            else (set(info_by_name.keys()) | set(session_manager._config.keys()) | set(session_manager._sessions.keys()))
        )
        if req in candidates:
            return req

        # Match scoped subagent/plugin server names by base server suffix
        req_lower = req.lower()
        for cand in sorted(candidates, key=len, reverse=True):
            cand_lower = cand.lower()
            if (
                cand_lower == req_lower
                or cand_lower.endswith(f"__{req_lower}")
                or cand_lower.endswith(f"_{req_lower}")
            ):
                return cand

        if allowed_set is not None:
            allowed_display = ", ".join(sorted(allowed_set)) or "none"
            raise ToolException(
                f"MCP server {req!r} is not permitted or not configured for this agent. "
                f"Allowed servers: [{allowed_display}]."
            )
        return req

    def _resolve_tool_metadata(resolved_server: str, requested_tool: str) -> tuple[str, dict[str, Any], Any]:
        req_tool = requested_tool.strip()
        if ":" in req_tool:
            prefix, rest = req_tool.split(":", 1)
            if prefix == resolved_server or resolved_server.endswith(f"__{prefix}"):
                req_tool = rest

        info = info_by_name.get(resolved_server)
        if info is None:
            for cached in get_cached_mcp_server_infos():
                if cached.name == resolved_server:
                    info = cached
                    break

        if info is not None:
            for t in info.tools:
                orig = t.original_name or (t.name.split(":", 1)[1] if ":" in t.name else t.name)
                if req_tool in (orig, t.name) or (
                    t.name.endswith(f"_{req_tool}") or t.name.endswith(f":{req_tool}")
                ):
                    schema = t.input_schema if isinstance(t.input_schema, dict) else {}
                    return str(orig), _clean_mcp_schema(schema), t

        disk_schema = read_mcp_tool_schema(resolved_server, req_tool)
        if isinstance(disk_schema, dict):
            raw_input = disk_schema.get("inputSchema")
            orig_name = str(disk_schema.get("name") or req_tool)
            return orig_name, _clean_mcp_schema(raw_input), disk_schema

        return req_tool, {}, None

    def _handle_tool_error(error: ToolException) -> str:
        raw_msg = str(error) or "call_mcp_tool failed with no error detail"
        logger.info("MCP dispatcher call_mcp_tool returned error: %s", raw_msg)
        return _enhance_mcp_error_diagnostics(raw_msg)

    async def coroutine(
        server_name: str = "",
        tool_name: str = "",
        arguments: dict[str, Any] | None = None,
        *,
        ServerName: str = "",
        ToolName: str = "",
        Arguments: dict[str, Any] | None = None,
        runtime: Any = None,
        **extra_kwargs: Any,
    ) -> Any:
        import time

        raw_server = server_name or ServerName or str(extra_kwargs.pop("server", "") or "")
        raw_tool = tool_name or ToolName or str(extra_kwargs.pop("tool", "") or "")
        raw_args = arguments if arguments is not None else Arguments
        if raw_args is None:
            raw_args = extra_kwargs if extra_kwargs else {}
        elif not isinstance(raw_args, dict):
            raise ToolException(f"Invalid arguments for MCP tool {raw_tool!r}: expected an object/dict.")

        resolved_server = _resolve_server_name(raw_server)
        actual_tool_name, input_schema, tool_ref = _resolve_tool_metadata(resolved_server, raw_tool)
        if not actual_tool_name:
            raise ToolException("tool_name is required for call_mcp_tool.")

        lc_tool_name = f"{resolved_server}:{actual_tool_name}"
        profiler = MCPSemanticProfiler.get_instance()
        profile = profiler.get_profile(resolved_server, actual_tool_name)
        if profile is None:
            target_for_profile = tool_ref if tool_ref is not None else actual_tool_name
            profile = profiler.heuristic_profile(target_for_profile, server_name=resolved_server)
            profiler.register_profile(resolved_server, actual_tool_name, profile)

        start_time = time.perf_counter()
        logger.info(
            "Invoking lazy MCP tool '%s' on server '%s' via call_mcp_tool",
            actual_tool_name,
            resolved_server,
            extra={"mcp_server": resolved_server, "tool_name": actual_tool_name},
        )
        cleaned_args = _normalize_mcp_arguments(dict(raw_args), input_schema)
        try:
            session = await session_manager.get_session(resolved_server)
            result = await session.call_tool(actual_tool_name, cleaned_args)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, ToolException):
            raise
        except Exception as exc:
            if not _is_transient_session_error(exc):
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                logger.error(
                    "Lazy MCP tool '%s' execution failed: %s",
                    actual_tool_name,
                    exc,
                    exc_info=True,
                    extra={
                        "mcp_server": resolved_server,
                        "tool_name": actual_tool_name,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                msg = f"MCP tool {lc_tool_name!r} failed on server {resolved_server!r}: {exc}"
                raise ToolException(_enhance_mcp_error_diagnostics(msg)) from None

            is_read_only = bool(
                profile.read_only_hint
                or (
                    profile.inferred_tier
                    and getattr(profile.inferred_tier, "value", str(profile.inferred_tier)) == "read_only"
                )
            )
            if not is_read_only:
                await session_manager.invalidate(resolved_server)
                msg = (
                    f"Connection lost while calling MCP tool {lc_tool_name!r}. "
                    "The operation may have already been executed by AWS/cloud provider and was not "
                    "automatically retried to prevent unintended duplicate side effects. "
                    "Please verify the current resource state before trying again."
                )
                logger.warning("MCP connection lost on mutating lazy tool %r; avoiding blind replay", lc_tool_name)
                raise ToolException(_enhance_mcp_error_diagnostics(msg)) from None

            logger.info(
                "MCP session for %r dead; retrying read-only lazy tool once",
                resolved_server,
                extra={"mcp_server": resolved_server},
            )
            await session_manager.invalidate(resolved_server)
            try:
                retry_session = await session_manager.get_session(resolved_server)
                result = await retry_session.call_tool(actual_tool_name, cleaned_args)
            except Exception as retry_exc:
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                logger.error(
                    "Lazy MCP tool '%s' execution failed after retry: %s",
                    actual_tool_name,
                    retry_exc,
                    exc_info=True,
                    extra={
                        "mcp_server": resolved_server,
                        "tool_name": actual_tool_name,
                        "duration_ms": round(duration_ms, 2),
                    },
                )
                msg = f"MCP tool {lc_tool_name!r} failed after retry on server {resolved_server!r}: {retry_exc}"
                raise ToolException(_enhance_mcp_error_diagnostics(msg)) from None

        duration_ms = (time.perf_counter() - start_time) * 1000.0
        logger.info(
            "Lazy MCP tool '%s' executed in %.2fms",
            actual_tool_name,
            duration_ms,
            extra={
                "mcp_server": resolved_server,
                "tool_name": actual_tool_name,
                "duration_ms": round(duration_ms, 2),
            },
        )
        try:
            converted = _convert_call_tool_result(result)
            if isinstance(converted, tuple) and len(converted) == 2:
                return converted
            return converted, None
        except ToolException:
            raise
        except Exception as exc:
            logger.warning(
                "Fallback formatting for lazy MCP tool '%s' on server '%s': %s",
                actual_tool_name,
                resolved_server,
                exc,
            )
            content = getattr(result, "content", None)
            if content is not None:
                return content, None
            return str(result), None

    args_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "server_name": {
                "type": "string",
                "description": "Name of the MCP server to call.",
            },
            "tool_name": {
                "type": "string",
                "description": "Original name of the MCP tool on that server.",
            },
            "arguments": {
                "type": "object",
                "description": "Dictionary of arguments matching the tool's JSON schema.",
            },
        },
        "required": ["server_name", "tool_name"],
    }
    metadata: dict[str, Any] = {
        "_opscloud_mcp": True,
        "_opscloud_mcp_dispatcher": True,
        "_mcp_allowed_servers": tuple(sorted(allowed_set)) if allowed_set is not None else (),
        "readOnlyHint": True,
        "destructiveHint": False,
    }
    return StructuredTool(
        name="call_mcp_tool",
        description=(
            "Call a lazy-loaded MCP tool. Read the tool's schema file (<toolName>.json) "
            "in the MCP server's schema directory to understand its arguments and usage."
        ),
        args_schema=args_schema,
        coroutine=coroutine,
        response_format="content_and_artifact",
        metadata=metadata,
        handle_tool_error=cast("Any", _handle_tool_error),
    )


def build_mcp_tools_from_server_infos(
    server_infos: Sequence[MCPServerInfo],
    session_manager: MCPSessionManager,
    *,
    configs: Mapping[str, Any] | None = None,
    include_dispatcher: bool = False,
) -> list[BaseTool]:
    """Build LangChain StructuredTools from preloaded/probed MCPServerInfo list.

    Eager/Lazy tool partitioning:
    - Materializes every active server's tool schemas to ``~/.opscloud/mcp/<server>/<tool>.json``.
    - Eager tools are bound directly as individual ``StructuredTool`` objects.
    - Lazy tools are invoked on-demand through a single shared ``call_mcp_tool`` dispatcher,
      keeping thousands of lines of raw JSON schemas out of every LLM request payload.
    """
    from opscloud.mcp.preload import write_mcp_server_state

    tools: list[BaseTool] = []
    has_lazy_tools = False
    active_infos: list[MCPServerInfo] = []
    cfg_lookup = configs if isinstance(configs, Mapping) else getattr(session_manager, "_config", {})

    for info in server_infos:
        if not info.enabled or info.status != "ok":
            continue
        active_infos.append(info)
        write_mcp_server_state(info)
        srv_cfg = cfg_lookup.get(info.name) if isinstance(cfg_lookup, Mapping) else None
        eager_tools, lazy_tools = split_enabled_tools_by_eager_lazy(info, srv_cfg)
        if lazy_tools:
            has_lazy_tools = True
        for t in eager_tools:
            tool_obj = _build_cached_mcp_tool(
                mcp_tool=t,
                server_name=info.name,
                session_manager=session_manager,
                name_prefix_sep=":",
            )
            tools.append(tool_obj)

    if has_lazy_tools or (include_dispatcher and active_infos):
        allowed_servers = [i.name for i in active_infos]
        if not allowed_servers and isinstance(cfg_lookup, Mapping):
            allowed_servers = [
                str(k)
                for k, v in cfg_lookup.items()
                if not isinstance(v, Mapping) or v.get("enabled", True)
            ]
        tools.append(
            build_call_mcp_tool(
                session_manager,
                allowed_servers=allowed_servers or None,
                server_infos=active_infos,
            )
        )
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
                    if hasattr(entry, "aclose") and callable(entry.aclose):
                        await entry.aclose()
                    else:
                        await entry.exit_stack.aclose()
                except Exception as exc:
                    logger.debug("Error closing invalidated session %s: %s", server_name, exc)

    async def close_subagent_sessions(self, agent_name: str | None = None) -> int:
        """Close and evict active MCP stdio/HTTP sessions owned by a subagent or `/agent` session.

        Args:
            agent_name: Optional subagent name or ID (e.g. ``aws-finops-agent@talkops-devops-plugins``).
                When ``None``, closes all active ``subagent__*`` sessions.

        Returns:
            Number of closed subagent MCP sessions.
        """
        if agent_name:
            from opscloud.plugins.adapters.mcp import _safe_mcp_name_part

            short = agent_name.split("@", 1)[0].rsplit(":", 1)[-1].strip()
            safe_full = _safe_mcp_name_part(agent_name)
            safe_short = _safe_mcp_name_part(short)
            prefixes: tuple[str, ...] = (
                f"subagent__{safe_full}__",
                f"subagent__{safe_short}__",
                f"subagent__{short}__",
                f"subagent__{short}_",
                f"subagent__{agent_name}__",
            )
        else:
            prefixes = ("subagent__",)

        target_names = [
            srv_name
            for srv_name in list(self._sessions.keys())
            if srv_name.startswith(prefixes)
        ]
        for srv_name in target_names:
            await self.invalidate(srv_name)
        if target_names:
            logger.info(
                "Closed %d subagent MCP session(s): %s",
                len(target_names),
                ", ".join(target_names),
            )
        return len(target_names)

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
                    if hasattr(entry, "aclose") and callable(entry.aclose):
                        await entry.aclose()
                    else:
                        async with asyncio.timeout(5.0):
                            await entry.exit_stack.aclose()
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
        """Connect to a single MCP server and initialize its tools inside a dedicated owner task."""
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

        loop = asyncio.get_running_loop()
        ready_future: asyncio.Future[tuple[Any, Any, AsyncExitStack]] = loop.create_future()
        close_event = asyncio.Event()

        async def _session_owner() -> None:
            exit_stack = AsyncExitStack()
            try:
                conn = create_mcp_connection(resolved_config)
                async with asyncio.timeout(_CONNECT_TIMEOUT):
                    if conn.get("transport") == "stdio":
                        from mcp import ClientSession
                        from mcp.client.stdio import StdioServerParameters, stdio_client
                        from opscloud.utils.logger import DEFAULT_LOG_DIR

                        try:
                            DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
                            errlog_file = exit_stack.enter_context(
                                open(DEFAULT_LOG_DIR / "mcp_stderr.log", "a", encoding="utf-8")
                            )
                        except Exception:
                            errlog_file = exit_stack.enter_context(
                                open(os.devnull, "w", encoding="utf-8")
                            )
                        server_params = StdioServerParameters(
                            command=str(conn.get("command") or ""),
                            args=list(conn.get("args") or []),
                            env=conn.get("env"),
                            cwd=conn.get("cwd"),
                        )
                        read_stream, write_stream = await exit_stack.enter_async_context(
                            stdio_client(server_params, errlog=errlog_file)
                        )
                        session = await exit_stack.enter_async_context(
                            ClientSession(read_stream, write_stream)
                        )
                    else:
                        from langchain_mcp_adapters.sessions import create_session

                        session = await exit_stack.enter_async_context(create_session(conn))
                async with asyncio.timeout(_CONNECT_TIMEOUT):
                    await session.initialize()
                async with asyncio.timeout(_CONNECT_TIMEOUT):
                    tools_result = await session.list_tools()

                if not ready_future.done():
                    ready_future.set_result((session, tools_result, exit_stack))
                    await close_event.wait()
            except BaseException as exc:
                if not ready_future.done():
                    ready_future.set_exception(exc)
            finally:
                try:
                    async with asyncio.timeout(5.0):
                        await exit_stack.aclose()
                except BaseException as close_exc:
                    logger.debug("Error closing exit stack for MCP server %s: %s", name, close_exc)

        owner_task = loop.create_task(_session_owner(), name=f"mcp-owner-{name}")
        self._background_tasks.add(owner_task)
        owner_task.add_done_callback(self._background_tasks.discard)

        try:
            session, tools_result, exit_stack = await ready_future
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

            try:
                from opscloud.mcp.preload import (
                    build_mcp_server_info_from_raw_tools,
                    set_cached_mcp_server_info,
                )

                info = build_mcp_server_info_from_raw_tools(name, resolved_config, raw_tools)
                set_cached_mcp_server_info(info, config)
            except Exception as cache_exc:
                logger.debug("Failed to cache MCPServerInfo for '%s': %s", name, cache_exc)

            entry = _MCPSessionEntry(
                session,
                exit_stack,
                close_event=close_event,
                owner_task=owner_task,
            )
            entry._cached_tools = wrapped_tools

            def _watch_owner(_task: asyncio.Task[Any]) -> None:
                # If the owner task exits unexpectedly while this entry is still
                # registered, evict it so the next tool call transparently reconnects.
                if self._sessions.get(name) is entry and not close_event.is_set():
                    self._sessions.pop(name, None)

            owner_task.add_done_callback(_watch_owner)

            self._sessions[name] = entry
            self._errors.pop(name, None)
            return True
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            close_event.set()
            if not owner_task.done():
                owner_task.cancel()
            raise
        except BaseException as exc:
            close_event.set()
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

