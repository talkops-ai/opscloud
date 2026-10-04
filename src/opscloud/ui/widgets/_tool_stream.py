"""Shared streaming tool-call buffering and hook-payload construction.

Both execution surfaces reassemble the same streamed tool-call state and fire
the same `tool.use` / `tool.result` / `tool.error` hook payloads:

- the interactive Textual TUI (`opscloud.ui.textual_adapter`), and
- the headless runner (`opscloud.client.non_interactive`).

This module holds the single implementation of the buffering, argument parsing,
and payload-shape logic, so the two surfaces cannot drift.
"""

from __future__ import annotations

import json
from opscloud.utils.logger import get_logger
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypedDict

from opscloud.integrations.hooks import HOOK_TOOL_OUTPUT_LIMIT

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = get_logger(__name__)

ToolStatus = Literal["success", "error"]
"""Terminal status of a tool call, mirroring `ToolMessage.status`."""

ToolCallBufferKey = int | str
"""Key for buffering an in-progress streamed tool call."""

ProviderToolArgs = dict[str, Any] | list[Any] | str | int | float | bool | None
"""A whole tool-call arguments value delivered by a provider in one chunk."""

TOOL_OUTPUT_TRUNCATION_MARKER = "…[output truncated]"
"""Suffix appended to a `tool.result` `tool_output` that hit HOOK_TOOL_OUTPUT_LIMIT."""

UNRENDERABLE_TOOL_OUTPUT = "<tool output could not be rendered>"
"""Sentinel `tool_output` used when formatting/coercing a tool result raises."""

MAX_JSON_CONTAINER_DEPTH = sys.getrecursionlimit()
"""Maximum JSON container nesting accepted for streamed tool-call args."""


def normalize_tool_status(raw_status: object, tool_name: str) -> ToolStatus:
    """Map a raw `ToolMessage.status` to the two-value hook domain, fail-closed."""
    if raw_status == "error":
        return "error"
    if raw_status == "success":
        return "success"
    logger.warning(
        "Unexpected ToolMessage.status %r for tool %s; treating as error",
        raw_status,
        tool_name,
    )
    return "error"


class ToolUsePayload(TypedDict):
    """`tool.use` hook payload (schema documented in `hooks`)."""

    tool_name: str
    tool_id: str
    tool_args: dict[str, Any]


class ToolErrorPayload(TypedDict):
    """`tool.error` hook payload (schema documented in `hooks`)."""

    tool_names: list[str]


class ToolResultPayload(TypedDict):
    """`tool.result` hook payload (schema documented in `hooks`)."""

    tool_name: str
    tool_id: str | None
    tool_args: dict[str, Any]
    tool_status: ToolStatus
    tool_output: str


def tool_call_buffer_key(
    index: int | str | None, tool_id: str | None, count: int
) -> ToolCallBufferKey:
    """Compute a stable key for buffering an in-progress streamed tool call."""
    if index is not None:
        return index
    if tool_id is not None:
        return tool_id
    return f"unknown-{count}"


def _exceeds_json_container_depth(s: str) -> bool:
    """Return whether `s` exceeds the safe nesting depth for JSON args."""
    depth = 0
    in_string = False
    escaped = False
    for ch in s:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            if depth > MAX_JSON_CONTAINER_DEPTH:
                return True
        elif ch in "}]":
            depth -= 1
    return False


def _looks_structurally_complete(s: str) -> bool:
    """Return whether `s` is a balanced JSON container, string-state aware."""
    depth = 0
    in_string = False
    escaped = False
    for ch in s:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth < 0:
                return True
    return depth == 0 and not in_string


@dataclass
class ToolCallBuffer:
    """In-progress state for a single streamed tool call."""

    name: str | None = None
    tool_id: str | None = None
    args: ProviderToolArgs = None
    args_parts: list[str] = field(default_factory=list)
    displayed: bool = False
    warned: bool = False

    def __post_init__(self) -> None:
        if self.args is not None and self.args_parts:
            msg = "ToolCallBuffer cannot hold both args and args_parts"
            raise ValueError(msg)

    def _reset_for_new_call(self) -> None:
        self.name = None
        self.tool_id = None
        self.args = None
        self.args_parts = []
        self.displayed = False
        self.warned = False

    def ingest(
        self,
        *,
        name: str | None,
        tool_id: str | None,
        args: Any,
    ) -> None:
        """Fold one streamed tool-call chunk's fields into the buffer."""
        new_id_reuses_index = (
            tool_id is not None and self.tool_id is not None and tool_id != self.tool_id
        )
        delayed_id_reuses_index = (
            tool_id is None
            and self.tool_id is not None
            and name is not None
            and self.name is not None
        )
        if new_id_reuses_index or delayed_id_reuses_index:
            self._reset_for_new_call()

        if name:
            self.name = name
        if tool_id:
            self.tool_id = tool_id

        if isinstance(args, dict):
            self.args = args
            self.args_parts = []
        elif isinstance(args, str):
            if args:
                self.args = None
                self.args_parts.append(args)
        elif args is not None:
            self.args = args
            self.args_parts = []

    def parse_args(self) -> dict[str, Any] | None:
        """Return the tool-call args once enough data has arrived, else `None`."""
        if self.args is not None and self.args_parts:
            msg = "ToolCallBuffer cannot hold both args and args_parts"
            raise ValueError(msg)
        if isinstance(self.args, dict):
            return self.args
        if self.args is not None:
            return {"value": self.args}

        if not self.args_parts:
            return None
        joined = "".join(self.args_parts)
        stripped = joined.strip()
        if not stripped:
            return None
        if stripped[0] in "{[":
            if not stripped.endswith(("}", "]")):
                return None
            if _exceeds_json_container_depth(stripped):
                if not self.warned:
                    self.warned = True
                    logger.warning(
                        "Tool-call args look complete but failed to parse: %r",
                        joined[:200],
                    )
                return None
        try:
            parsed = json.loads(joined)
        except (json.JSONDecodeError, RecursionError):
            if (
                stripped[0] in "{["
                and _looks_structurally_complete(stripped)
                and not self.warned
            ):
                self.warned = True
                logger.warning(
                    "Tool-call args look complete but failed to parse: %r",
                    joined[:200],
                )
            return None
        if not isinstance(parsed, dict):
            return {"value": parsed}
        return parsed


class UnemittedToolCalls(NamedTuple):
    """Counts of buffered tool calls that never emitted a `tool.use`."""

    unparsed: int
    idless_parsed: int


def count_unemitted_tool_calls(buffers: Iterable[ToolCallBuffer]) -> UnemittedToolCalls:
    """Classify buffered tool calls that never emitted a `tool.use`."""
    unparsed = 0
    idless_parsed = 0
    for buffer in buffers:
        if buffer.name is None:
            continue
        if buffer.parse_args() is None:
            unparsed += 1
        elif buffer.tool_id is None:
            idless_parsed += 1
    return UnemittedToolCalls(unparsed, idless_parsed)


def build_tool_use_payload(
    tool_name: str, tool_id: str, tool_args: dict[str, Any]
) -> ToolUsePayload:
    """Build the `tool.use` hook payload (schema documented in `hooks`)."""
    return {
        "tool_name": tool_name,
        "tool_id": tool_id,
        "tool_args": tool_args,
    }


def build_tool_error_payload(tool_name: str) -> ToolErrorPayload:
    """Build the `tool.error` hook payload (schema documented in `hooks`)."""
    return {"tool_names": [tool_name]}


def build_tool_result_payload(
    tool_name: str,
    tool_id: str | None,
    tool_args: dict[str, Any],
    tool_status: ToolStatus,
    tool_output: str,
) -> ToolResultPayload:
    """Build the `tool.result` hook payload (schema documented in `hooks`)."""
    if len(tool_output) > HOOK_TOOL_OUTPUT_LIMIT:
        keep = max(HOOK_TOOL_OUTPUT_LIMIT - len(TOOL_OUTPUT_TRUNCATION_MARKER), 0)
        capped_output = tool_output[:keep] + TOOL_OUTPUT_TRUNCATION_MARKER
    else:
        capped_output = tool_output
    return {
        "tool_name": tool_name,
        "tool_id": tool_id,
        "tool_args": tool_args,
        "tool_status": tool_status,
        "tool_output": capped_output,
    }
