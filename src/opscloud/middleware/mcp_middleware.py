"""Agent middleware for MCP tools execution, argument normalization, and auth resilience.

Normalizes model-generated arguments before they hit MCP tools (e.g., dropping empty
strings for optional params) and provides clean error surfacing for expired tokens or
unauthenticated MCP sessions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from opscloud.middleware.registry import register_middleware
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

_MCP_AUTH_ERROR_SIGNALS = (
    "unauthorized",
    "not authorized",
    "unauthenticated",
    "authentication failed",
    "token expired",
    "token is expired",
    "is expired",
    "session expired",
    "invalid credentials",
    "access denied",
    "credentials have expired",
    "expiredtoken",
)


def normalize_mcp_arguments(
    arguments: dict[str, Any],
    input_schema: Any = None,
) -> dict[str, Any]:
    """Drop empty-string values for optional parameters to avoid MCP server schema rejections.

    Many MCP servers (AWS MCP, Slack, GitHub, K8s) validate optional string/ID parameters
    strictly and reject empty strings `""` when the parameter should have been omitted.
    """
    if not isinstance(arguments, dict):
        return arguments

    required_set: set[str] = set()
    properties: dict[str, Any] = {}
    if isinstance(input_schema, dict):
        required_set = set(input_schema.get("required") or ())
        properties = input_schema.get("properties") or {}

    cleaned: dict[str, Any] = {}
    for key, value in arguments.items():
        if value != "" or key in required_set:
            cleaned[key] = value
            continue
        # Drop empty strings for non-required fields
        logger.debug("Normalized MCP tool arg: dropped empty-string for key %r", key)

    return cleaned


@register_middleware(name="mcp_tool")
class MCPToolMiddleware(AgentMiddleware):
    """Wraps MCP tool calls to normalize arguments and handle authentication failures."""

    def _is_mcp_call(self, request: ToolCallRequest) -> bool:
        tool_name = (
            request.tool_call.get("name", "")
            if getattr(request, "tool_call", None) and isinstance(request.tool_call, dict)
            else ""
        )
        return ":" in tool_name or tool_name.startswith("mcp__")

    def _preprocess_request(self, request: ToolCallRequest) -> ToolCallRequest:
        if not self._is_mcp_call(request):
            return request

        args = request.tool_call.get("args") or {}
        if isinstance(args, dict):
            input_schema = getattr(request.tool, "args_schema", None)
            schema_dict = input_schema.schema() if input_schema is not None and hasattr(input_schema, "schema") else None
            cleaned = normalize_mcp_arguments(args, schema_dict)
            request.tool_call["args"] = cleaned

        return request

    def _handle_error(self, request: ToolCallRequest, exc: Exception) -> ToolMessage | None:
        err_str = str(exc).lower()
        is_auth_error = any(sig in err_str for sig in _MCP_AUTH_ERROR_SIGNALS) or (
            "expired" in err_str and any(k in err_str for k in ("token", "credential", "auth", "session"))
        )
        if is_auth_error:
            tool_name = request.tool_call.get("name", "mcp_tool")
            tool_id = request.tool_call.get("id", "")
            logger.warning("MCP tool %r encountered authentication error: %s", tool_name, exc)
            return ToolMessage(
                content=(
                    f"MCP tool `{tool_name}` failed due to authentication: {exc}. "
                    "The cloud credentials or session token for this MCP integration may have expired. "
                    "Please verify your AWS / cloud authentication or refresh your credentials."
                ),
                name=tool_name,
                tool_call_id=tool_id,
                status="error",
            )
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        preprocessed = self._preprocess_request(request)
        try:
            return handler(preprocessed)
        except Exception as exc:
            auth_msg = self._handle_error(request, exc)
            if auth_msg is not None:
                return auth_msg
            raise

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        preprocessed = self._preprocess_request(request)
        try:
            return await handler(preprocessed)
        except Exception as exc:
            auth_msg = self._handle_error(request, exc)
            if auth_msg is not None:
                return auth_msg
            raise


__all__ = [
    "MCPToolMiddleware",
    "normalize_mcp_arguments",
]
