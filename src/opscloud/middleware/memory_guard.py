"""Protect machine-managed memory blocks from agent edits while allowing user memory updates."""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
from enum import Enum
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from opscloud.middleware.registry import register_middleware
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

logger = get_logger(__name__)

_GUARDED_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "delete",
        "delete_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "rm",
    }
)

ONBOARDING_NAME_MEMORY_START = "<!-- opscloud:onboarding-name:start -->"
ONBOARDING_NAME_MEMORY_END = "<!-- opscloud:onboarding-name:end -->"

_MANAGED_START_MARKERS = (
    "<!-- opscloud:onboarding-name:start -->",
    "<!-- opscloud:managed-memory:start -->",
    "<!-- k8s_autopilot:onboarding-name:start -->",
    "<!-- k8s_autopilot:managed-memory:start -->",
    "<!-- deepagents:onboarding-name:start -->",
    "<!-- opscode:onboarding-name:start -->",
    "<!-- managed:start -->",
)
_MANAGED_END_MARKERS = (
    "<!-- opscloud:onboarding-name:end -->",
    "<!-- opscloud:managed-memory:end -->",
    "<!-- k8s_autopilot:onboarding-name:end -->",
    "<!-- k8s_autopilot:managed-memory:end -->",
    "<!-- deepagents:onboarding-name:end -->",
    "<!-- opscode:onboarding-name:end -->",
    "<!-- managed:end -->",
)

_REJECTION_MESSAGE = (
    "The region between the `opscloud:onboarding-name:start` and "
    "`opscloud:onboarding-name:end` markers in {path} is machine-managed and "
    "must not be edited. Your other changes to the file were kept, but the "
    "managed block was restored to its previous content. Do not modify content "
    "between those markers."
)

_RESTORE_FAILED_MESSAGE = (
    "The region between the `opscloud:onboarding-name:start` and "
    "`opscloud:onboarding-name:end` markers in {path} is machine-managed and "
    "must not be edited. Your edit changed it and the previous content could "
    "not be restored, so the managed block may now be corrupted. Do not modify "
    "content between those markers, and do not rely on this edit having "
    "succeeded."
)

_DELETE_REJECTION_MESSAGE = (
    "The guarded memory file {path} contains a machine-managed region or is protected "
    "and must not be deleted. Do not delete this file or a parent directory that contains it."
)


class _RestoreOutcome(Enum):
    UNCHANGED = "unchanged"
    RESTORED = "restored"
    FAILED = "failed"


def extract_onboarding_name_block(text: str) -> str | None:
    """Return the managed onboarding name block (markers included) if present."""
    for start_marker, end_marker in zip(_MANAGED_START_MARKERS, _MANAGED_END_MARKERS, strict=False):
        start = text.find(start_marker)
        end = text.find(end_marker)
        if start != -1 and end != -1 and start < end:
            return text[start : end + len(end_marker)]
    return None


def strip_onboarding_name_markers(text: str) -> str:
    """Remove every onboarding-name marker occurrence from text."""
    res = text
    for m in _MANAGED_START_MARKERS + _MANAGED_END_MARKERS:
        res = res.replace(m, "")
    return res


def _upsert_onboarding_name_memory(existing: str, block: str) -> str:
    """Insert or replace the managed onboarding name memory block."""
    for start_marker, end_marker in zip(_MANAGED_START_MARKERS, _MANAGED_END_MARKERS, strict=False):
        start = existing.find(start_marker)
        end = existing.find(end_marker)
        if start != -1 and end != -1 and start < end:
            end += len(end_marker)
            prefix = existing[:start].rstrip()
            suffix = existing[end:].strip()
            parts = [part for part in (prefix, block, suffix) if part]
            return "\n\n".join(parts).rstrip() + "\n"

    base = existing.rstrip()
    if not base:
        return f"## User Preferences\n\n{block}\n"
    if "## User Preferences" in base:
        return f"{base}\n\n{block}\n"
    return f"{base}\n\n## User Preferences\n\n{block}\n"


@register_middleware(name="memory_guard")
class ManagedMemoryGuardMiddleware(AgentMiddleware[Any, Any]):
    """Protects managed onboarding blocks and memory files from deletion while allowing user edits."""

    def __init__(self, guarded_paths: Iterable[str | Path] = ()) -> None:
        super().__init__()
        requested = list(guarded_paths)
        resolved: set[Path] = set()
        for raw in requested:
            try:
                resolved.add(Path(raw).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                logger.warning("Could not resolve guarded memory path %r", raw, exc_info=True)
        self._guarded: frozenset[Path] = frozenset(resolved)
        if requested and not self._guarded:
            logger.warning(
                "ManagedMemoryGuardMiddleware resolved no guarded paths from %r",
                requested,
            )

    def _guarded_path(self, request: ToolCallRequest) -> Path | None:
        tool_call = getattr(request, "tool_call", None)
        if not isinstance(tool_call, dict):
            return None
        tool_name = tool_call.get("name", "")
        if tool_name not in _GUARDED_TOOLS:
            return None
        args = tool_call.get("args") or {}
        file_path = (
            args.get("file_path")
            or args.get("path")
            or args.get("target")
            or args.get("TargetFile")
            or args.get("target_file")
            or args.get("file")
            or ""
        )
        if not isinstance(file_path, str) or not file_path:
            return None
        try:
            resolved = Path(file_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            logger.warning(
                "Could not resolve target path %r for %s",
                file_path,
                tool_name,
                exc_info=True,
            )
            return None
        if tool_name in ("delete", "delete_file", "rm"):
            for guarded in self._guarded:
                try:
                    if guarded.is_relative_to(resolved) or resolved == guarded:
                        return guarded
                except AttributeError:
                    if str(guarded).startswith(str(resolved)):
                        return guarded
            return None
        return resolved if resolved in self._guarded else None

    @staticmethod
    def _read(path: Path) -> str | None:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            logger.warning("Could not read guarded memory file %s", path, exc_info=True)
            return None

    @staticmethod
    def _write(path: Path, content: str) -> None:
        flags = os.O_WRONLY | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)

    @staticmethod
    def _line_range_for_block(before: str, before_block: str) -> tuple[int, int] | None:
        block_start = before.find(before_block)
        if block_start == -1:
            return None
        block_end = block_start + len(before_block)
        start_line: int | None = None
        end_line: int | None = None
        offset = 0
        for line_number, line in enumerate(before.splitlines(keepends=True)):
            line_end = offset + len(line)
            if start_line is None and offset <= block_start < line_end:
                start_line = line_number
            if offset < block_end <= line_end:
                end_line = line_number + 1
                break
            offset = line_end
        if start_line is None or end_line is None:
            return None
        return start_line, end_line

    @staticmethod
    def _without_managed_block_edits(before: str, after: str, before_block: str) -> str | None:
        block_range = ManagedMemoryGuardMiddleware._line_range_for_block(before, before_block)
        if block_range is None:
            return None
        block_start, block_end = block_range
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        ranges: list[tuple[int, int]] = []
        matcher = SequenceMatcher(None, before_lines, after_lines, autojunk=False)
        for (
            tag,
            before_start,
            before_end,
            after_start,
            after_end,
        ) in matcher.get_opcodes():
            overlaps = before_start < block_end and block_start < before_end
            if tag == "insert":
                if block_start < before_start < block_end:
                    ranges.append((after_start, after_end))
                continue
            if not overlaps or tag == "delete":
                continue
            if tag == "equal":
                start = max(before_start, block_start)
                end = min(before_end, block_end)
                ranges.append(
                    (
                        after_start + start - before_start,
                        after_start + end - before_start,
                    )
                )
            else:
                ranges.append((after_start, after_end))

        if not ranges:
            return after
        parts: list[str] = []
        cursor = 0
        for start, end in sorted(ranges):
            range_start = start
            range_end = end
            if range_start < cursor:
                range_end = max(range_end, cursor)
                range_start = cursor
            parts.extend(after_lines[cursor:range_start])
            cursor = range_end
        parts.extend(after_lines[cursor:])
        return "".join(parts)

    def _restore(self, path: Path, before: str, before_block: str) -> _RestoreOutcome:
        after = self._read(path)
        if after is None:
            logger.warning(
                "Guarded memory file %s is unreadable after edit; cannot restore managed block",
                path,
            )
            return _RestoreOutcome.FAILED
        block_after = extract_onboarding_name_block(after)
        if block_after == before_block:
            return _RestoreOutcome.UNCHANGED
        if block_after is not None:
            source = after
        else:
            maybe_source = self._without_managed_block_edits(before, after, before_block)
            if maybe_source is None:
                logger.error(
                    "Could not locate previous managed block in %s; leaving the edited file untouched",
                    path,
                )
                return _RestoreOutcome.FAILED
            source = strip_onboarding_name_markers(maybe_source)
        restored = _upsert_onboarding_name_memory(source, before_block)
        if extract_onboarding_name_block(restored) != before_block:
            logger.error(
                "Restored content for %s did not reproduce the managed block; leaving the edited file untouched",
                path,
            )
            return _RestoreOutcome.FAILED
        try:
            self._write(path, restored)
        except (OSError, UnicodeEncodeError):
            logger.warning("Could not restore managed memory block at %s", path, exc_info=True)
            return _RestoreOutcome.FAILED
        return _RestoreOutcome.RESTORED

    @staticmethod
    def _error(request: ToolCallRequest, path: Path, *, restore_failed: bool) -> ToolMessage:
        template = _RESTORE_FAILED_MESSAGE if restore_failed else _REJECTION_MESSAGE
        tool_call = getattr(request, "tool_call", {})
        return ToolMessage(
            content=template.format(path=path),
            name=tool_call.get("name", "edit_file"),
            tool_call_id=tool_call.get("id", ""),
            status="error",
        )

    @staticmethod
    def _delete_error(request: ToolCallRequest, path: Path) -> ToolMessage:
        tool_call = getattr(request, "tool_call", {})
        return ToolMessage(
            content=_DELETE_REJECTION_MESSAGE.format(path=path),
            name=tool_call.get("name", "delete"),
            tool_call_id=tool_call.get("id", ""),
            status="error",
        )

    @staticmethod
    def _reject_delete(path: Path, before: str | None) -> bool:
        if before is not None:
            return extract_onboarding_name_block(before) is not None or path.exists()
        return path.exists()

    def _result_after_restore(
        self,
        request: ToolCallRequest,
        path: Path,
        before: str,
        before_block: str,
        result: ToolMessage | Command[Any],
    ) -> ToolMessage | Command[Any]:
        outcome = self._restore(path, before, before_block)
        if outcome is _RestoreOutcome.UNCHANGED:
            return result
        return self._error(request, path, restore_failed=outcome is _RestoreOutcome.FAILED)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        path = self._guarded_path(request)
        if path is None:
            return handler(request)
        before = self._read(path)
        tool_name = request.tool_call.get("name", "")
        if tool_name in ("delete", "delete_file", "rm"):
            if self._reject_delete(path, before):
                return self._delete_error(request, path)
            return handler(request)
        before_block = extract_onboarding_name_block(before) if before is not None else None
        if before is None or before_block is None:
            return handler(request)
        result = handler(request)
        return self._result_after_restore(request, path, before, before_block, result)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        path = await asyncio.to_thread(self._guarded_path, request)
        if path is None:
            return await handler(request)
        before = await asyncio.to_thread(self._read, path)
        tool_name = request.tool_call.get("name", "")
        if tool_name in ("delete", "delete_file", "rm"):
            if await asyncio.to_thread(self._reject_delete, path, before):
                return self._delete_error(request, path)
            return await handler(request)
        before_block = extract_onboarding_name_block(before) if before is not None else None
        if before is None or before_block is None:
            return await handler(request)
        result = await handler(request)
        return await asyncio.to_thread(self._result_after_restore, request, path, before, before_block, result)
