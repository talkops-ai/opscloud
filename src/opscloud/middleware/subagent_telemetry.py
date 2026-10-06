"""Subagent telemetry middleware for real-time progress and intermediate tool streaming."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import re
import time
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from opscloud.middleware.registry import register_middleware
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


def _format_tool_activity(name: str, tool_args: Mapping[str, Any] | None) -> str:
    """Format human-readable action description for live telemetry."""
    args = tool_args or {}
    if name in {"execute", "bash", "run_command", "sh", "terminal"}:
        cmd = str(args.get("command") or args.get("cmd") or args.get("CommandLine") or "").strip()
        first_line = cmd.splitlines()[0] if cmd else "command"
        if len(first_line) > 65:
            first_line = first_line[:62] + "..."
        if first_line.startswith("aws ") or first_line.startswith("kubectl ") or first_line.startswith("terraform "):
            return first_line
        return f"sh: {first_line}" if not first_line.startswith("sh:") else first_line

    if name in {"read_file", "edit_file", "write_file", "view_file"}:
        path = str(args.get("file_path") or args.get("path") or args.get("TargetFile") or args.get("AbsolutePath") or "").strip()
        return f"{name}: {path}" if path else name

    if name in {"grep_search", "file_search", "glob"}:
        pattern = str(args.get("query") or args.get("pattern") or args.get("Query") or "").strip()
        if len(pattern) > 30:
            pattern = pattern[:27] + "..."
        return f"{name}: {pattern}" if pattern else name

    # MCP tool parsing
    server_scope: str | None = None
    tool_action = name
    if "___" in name:
        server_raw, _, tool_action = name.partition("___")
        server_scope = server_raw.split("_")[-1] or "aws"
    elif "__" in name:
        parts = name.split("__")
        if len(parts) >= 2:
            server_scope = parts[1].split("_")[0]
            tool_action = parts[-1]

    if server_scope:
        if "aws" in server_scope:
            server_scope = "aws"
        elif "k8s" in server_scope or "kube" in server_scope:
            server_scope = "k8s"
        elif "gcp" in server_scope:
            server_scope = "gcp"
        elif "azure" in server_scope:
            server_scope = "azure"

    # If tool executes a script or command (e.g. aws___run_script)
    if "run_script" in tool_action or "script" in tool_action or "exec" in tool_action:
        desc = str(args.get("description") or args.get("summary") or args.get("comment") or args.get("title") or "").strip()
        if desc:
            return f"{server_scope}: {desc}" if server_scope else desc

        cmd = str(args.get("command") or args.get("cmd") or "").strip()
        if cmd:
            first_cmd = cmd.splitlines()[0].strip()
            if len(first_cmd) > 50:
                first_cmd = first_cmd[:47] + "..."
            return f"{server_scope}: {first_cmd}" if server_scope and not first_cmd.startswith(server_scope) else first_cmd

        script = str(args.get("script") or args.get("code") or "").strip()
        if script:
            lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
            for line in lines:
                if line.startswith("#"):
                    comment = line.lstrip("#").strip()
                    if comment:
                        if len(comment) > 50:
                            comment = comment[:47] + "..."
                        return f"{server_scope}: {comment}" if server_scope else comment
                if "client(" in line or "boto3." in line:
                    match = re.search(r"client\(['\"]([^'\"]+)['\"]\)", line)
                    if match:
                        svc = match.group(1)
                        return f"{server_scope or 'aws'}: {svc} query"
            op = str(args.get("operation") or args.get("action") or args.get("service") or "").strip()
            if op:
                return f"{server_scope or 'aws'}: {op}"
            return f"{server_scope or 'aws'}: Running script"

    op = str(args.get("operation") or args.get("action") or "").strip()
    if op:
        return f"{server_scope}: {op}" if server_scope else op

    readable_action = tool_action.replace("_", " ")
    if server_scope:
        return f"{server_scope}: {readable_action}"

    if any(prefix in name for prefix in ("billing", "aws", "k8s", "azure", "gcp")):
        return name.replace("_", " ")
    return name


@register_middleware(name="subagent_telemetry")
class SubagentTelemetryMiddleware(AgentMiddleware[Any, Any, Any]):
    """Dispatches live progress and tool telemetry events during subagent execution."""

    def __init__(self, subagent_name: str = "subagent") -> None:
        super().__init__()
        self.subagent_name = subagent_name

    def _emit_progress(
        self,
        action: str,
        *,
        tool_name: str | None = None,
        runtime: Any = None,
    ) -> None:
        payload = {
            "type": "subagent_progress",
            "subagent_name": self.subagent_name,
            "action": action,
            "activity": action,
            "tool_name": tool_name,
            "timestamp": time.time(),
        }

        # 1. Direct runtime stream_writer if provided
        writer = getattr(runtime, "stream_writer", None)
        if callable(writer):
            try:
                writer(payload)
            except Exception:
                pass

        # 2. LangGraph stream writer for cross-subgraph stream mode 'custom'
        try:
            from langgraph.config import get_stream_writer

            sw = get_stream_writer()
            sw(payload)
        except Exception:
            pass

        # 3. LangChain callback manager custom event dispatch
        try:
            from langchain_core.callbacks.manager import dispatch_custom_event

            config = getattr(runtime, "config", None) if runtime is not None else None
            dispatch_custom_event(
                "subagent_progress",
                payload,
                config=config,
            )
        except Exception:
            pass

    async def _aemit_progress(
        self,
        action: str,
        *,
        tool_name: str | None = None,
        runtime: Any = None,
    ) -> None:
        payload = {
            "type": "subagent_progress",
            "subagent_name": self.subagent_name,
            "action": action,
            "activity": action,
            "tool_name": tool_name,
            "timestamp": time.time(),
        }

        # 1. Direct runtime stream_writer if provided
        writer = getattr(runtime, "stream_writer", None)
        if callable(writer):
            try:
                writer(payload)
            except Exception:
                pass

        # 2. LangGraph stream writer for cross-subgraph stream mode 'custom'
        try:
            from langgraph.config import get_stream_writer

            sw = get_stream_writer()
            sw(payload)
        except Exception:
            pass

        # 3. LangChain callback manager custom event dispatch
        try:
            from langchain_core.callbacks.manager import adispatch_custom_event

            config = getattr(runtime, "config", None) if runtime is not None else None
            await adispatch_custom_event(
                "subagent_progress",
                payload,
                config=config,
            )
        except Exception:
            pass

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        tool_name = request.tool_call.get("name", "tool")
        tool_args = request.tool_call.get("args") or {}
        action = _format_tool_activity(tool_name, tool_args)
        self._emit_progress(action, tool_name=tool_name, runtime=request.runtime)
        try:
            return handler(request)
        finally:
            self._emit_progress("Thinking...", tool_name=None, runtime=request.runtime)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        tool_name = request.tool_call.get("name", "tool")
        tool_args = request.tool_call.get("args") or {}
        action = _format_tool_activity(tool_name, tool_args)
        await self._aemit_progress(action, tool_name=tool_name, runtime=request.runtime)
        try:
            return await handler(request)
        finally:
            await self._aemit_progress("Thinking...", tool_name=None, runtime=request.runtime)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse | ExtendedModelResponse:
        self._emit_progress("Thinking...", tool_name=None, runtime=request.runtime)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        await self._aemit_progress("Thinking...", tool_name=None, runtime=request.runtime)
        return await handler(request)


__all__ = ["SubagentTelemetryMiddleware", "_format_tool_activity"]
