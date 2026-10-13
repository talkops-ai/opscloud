"""Tool renderers for approval widgets - registry pattern."""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Any

from opscloud.file_ops import (
    _safe_read,
    build_approval_preview,
    compute_unified_diff,
    format_display_path,
    resolve_physical_path,
)
from opscloud.ui.widgets.tool_widgets import (
    EditFileApprovalWidget,
    GenericApprovalWidget,
    TaskApprovalWidget,
    WriteFileApprovalWidget,
    format_display_content,
)

if TYPE_CHECKING:
    from opscloud.ui.widgets.tool_widgets import ToolApprovalWidget


class ToolRendererResult:
    """Legacy compatibility data bag returned by renderers."""

    def __init__(
        self,
        *,
        title: str,
        details: list[str] | None = None,
        diff_lines: list[str] | None = None,
        file_path: str | None = None,
        content_preview: str | None = None,
    ) -> None:
        self.title = title
        self.details = details or []
        self.diff_lines = diff_lines
        self.file_path = file_path
        self.content_preview = content_preview


class ToolRenderer:
    """Strategy for building a tool's HITL approval widget.

    Each renderer maps a tool name to a `(widget_class, data)` pair that
    controls what the user sees in the approval box. Tools not registered
    in `_RENDERER_REGISTRY` fall through to the default, which dumps all
    args as `key: value` lines via `GenericApprovalWidget`.
    """

    @staticmethod
    def get_approval_widget(
        tool_args: dict[str, Any],
        assistant_id: str | None = None,
    ) -> tuple[type[ToolApprovalWidget], dict[str, Any]]:
        """Get the approval widget class and data for this tool."""
        return GenericApprovalWidget, tool_args


class WriteFileRenderer(ToolRenderer):
    """Renderer for write_file / write_to_file tool - shows diff or full content with line numbers."""

    @staticmethod
    def get_approval_widget(
        tool_args: dict[str, Any],
        assistant_id: str | None = None,
    ) -> tuple[type[ToolApprovalWidget], dict[str, Any]]:
        file_path = str(
            tool_args.get("file_path")
            or tool_args.get("path")
            or tool_args.get("TargetFile")
            or ""
        )
        raw_content = (
            tool_args.get("content")
            or tool_args.get("code_content")
            or tool_args.get("CodeContent")
            or ""
        )
        content = format_display_content(raw_content)

        # Check if file exists on disk to compute a real unified diff
        physical_path = resolve_physical_path(file_path, assistant_id)
        if physical_path and physical_path.exists() and physical_path.is_file():
            before = _safe_read(physical_path) or ""
            diff = compute_unified_diff(
                before, content, format_display_path(file_path), max_lines=100
            )
            if diff:
                return EditFileApprovalWidget, {
                    "file_path": file_path,
                    "diff_lines": diff.splitlines(),
                    "old_string": before,
                    "new_string": content,
                }

        file_extension = "text"
        if "." in file_path:
            file_extension = file_path.rsplit(".", 1)[-1]

        data = {
            "file_path": file_path,
            "content": content,
            "file_extension": file_extension,
        }
        return WriteFileApprovalWidget, data


class TaskRenderer(ToolRenderer):
    """Renderer for task tool — interrupt description provides full context."""

    @staticmethod
    def get_approval_widget(
        tool_args: dict[str, Any],
        assistant_id: str | None = None,
    ) -> tuple[type[ToolApprovalWidget], dict[str, Any]]:
        return TaskApprovalWidget, tool_args


class DeleteFileRenderer(ToolRenderer):
    """Renderer for delete / delete_file tool - shows removed file content when available."""

    @staticmethod
    def get_approval_widget(
        tool_args: dict[str, Any],
        assistant_id: str | None = None,
    ) -> tuple[type[ToolApprovalWidget], dict[str, Any]]:
        path = str(tool_args.get("file_path") or tool_args.get("path") or tool_args.get("TargetFile") or "")
        preview = build_approval_preview(
            "delete", {"file_path": path}, assistant_id=assistant_id
        )
        if preview is None:
            return GenericApprovalWidget, tool_args
        if preview.diff:
            return EditFileApprovalWidget, {
                "file_path": format_display_path(path),
                "diff_lines": preview.diff.splitlines(),
                "old_string": "",
                "new_string": "",
            }
        data: dict[str, Any] = {"file_path": format_display_path(path)}
        details = [
            detail for detail in preview.details if not detail.startswith("File:")
        ]
        if details:
            data["details"] = "\n".join(details)
        if preview.error:
            data["error"] = preview.error
        return GenericApprovalWidget, data


class EditFileRenderer(ToolRenderer):
    """Renderer for edit_file / replace_file_content tool - shows unified diff with line numbers."""

    @staticmethod
    def get_approval_widget(
        tool_args: dict[str, Any],
        assistant_id: str | None = None,
    ) -> tuple[type[ToolApprovalWidget], dict[str, Any]]:
        file_path = str(
            tool_args.get("file_path")
            or tool_args.get("path")
            or tool_args.get("TargetFile")
            or ""
        )
        old_string = format_display_content(
            tool_args.get("old_string")
            or tool_args.get("TargetContent")
            or ""
        )
        new_string = format_display_content(
            tool_args.get("new_string")
            or tool_args.get("ReplacementContent")
            or ""
        )
        start_line: int | None = None
        raw_start = tool_args.get("StartLine")
        if raw_start is not None:
            try:
                start_line = int(raw_start)
            except (ValueError, TypeError):
                start_line = None

        chunks = tool_args.get("ReplacementChunks")
        if isinstance(chunks, list) and chunks:
            all_diff_lines: list[str] = []
            for chunk in chunks:
                if isinstance(chunk, dict):
                    c_old = format_display_content(chunk.get("TargetContent", ""))
                    c_new = format_display_content(chunk.get("ReplacementContent", ""))
                    c_start = chunk.get("StartLine")
                    try:
                        c_start_int = int(c_start) if c_start is not None else None
                    except (ValueError, TypeError):
                        c_start_int = None
                    chunk_diff = EditFileRenderer._generate_diff(c_old, c_new, start_line=c_start_int)
                    all_diff_lines.extend(chunk_diff)
            return EditFileApprovalWidget, {
                "file_path": file_path,
                "diff_lines": all_diff_lines,
                "old_string": old_string,
                "new_string": new_string,
            }

        # If start_line is not provided, try to locate old_string in file on disk
        if start_line is None and old_string:
            physical_path = resolve_physical_path(file_path, assistant_id)
            if physical_path and physical_path.exists() and physical_path.is_file():
                file_text = _safe_read(physical_path)
                if file_text and old_string in file_text:
                    preceding = file_text[:file_text.find(old_string)]
                    start_line = preceding.count("\n") + 1

        diff_lines = EditFileRenderer._generate_diff(old_string, new_string, start_line=start_line)

        data = {
            "file_path": file_path,
            "diff_lines": diff_lines,
            "old_string": old_string,
            "new_string": new_string,
        }
        return EditFileApprovalWidget, data

    @staticmethod
    def _generate_diff(
        old_string: str,
        new_string: str,
        start_line: int | None = None,
    ) -> list[str]:
        """Generate unified diff lines from old and new strings with accurate line numbers."""
        if not old_string and not new_string:
            return []

        old_lines = old_string.splitlines() if old_string else []
        new_lines = new_string.splitlines() if new_string else []

        start = start_line if start_line is not None else 1
        old_count = len(old_lines)
        new_count = len(new_lines)

        hunk_header = f"@@ -{start},{old_count} +{start},{new_count} @@"
        diff_lines = [hunk_header]
        diff_lines.extend(f"-{line}" for line in old_lines)
        diff_lines.extend(f"+{line}" for line in new_lines)
        return diff_lines


_RENDERER_REGISTRY: dict[str, type[ToolRenderer]] = {
    "task": TaskRenderer,
    "write_file": WriteFileRenderer,
    "write_to_file": WriteFileRenderer,
    "edit_file": EditFileRenderer,
    "replace_file_content": EditFileRenderer,
    "multi_replace_file_content": EditFileRenderer,
    "delete": DeleteFileRenderer,
    "delete_file": DeleteFileRenderer,
}


def get_renderer(tool_name: str) -> ToolRenderer:
    """Get the renderer for a tool by name."""
    renderer_class = _RENDERER_REGISTRY.get(tool_name, ToolRenderer)
    return renderer_class()


def render_tool_approval(
    tool_name: str,
    args: dict[str, Any],
) -> ToolRendererResult:
    """Legacy helper for backwards compatibility."""
    renderer = get_renderer(tool_name)
    widget_cls, data = renderer.get_approval_widget(args)
    diff_lines = data.get("diff_lines")
    file_path = data.get("file_path") or args.get("file_path", "") or args.get("path", "")
    details = [f"{k}: {v}" for k, v in args.items() if k != "content"]

    title = tool_name
    if "terraform" in tool_name:
        action = "plan" if "plan" in tool_name else "apply"
        dir_path = args.get("dir", ".")
        title = f"Terraform {action.upper()}: {dir_path}"
    elif "kubectl" in tool_name:
        title = f"Kubectl: {tool_name}"
    elif "helm" in tool_name:
        title = f"Helm: {tool_name}"
    elif "ansible" in tool_name:
        title = f"Ansible: {tool_name}"
    elif tool_name in {"execute", "bash", "shell"}:
        title = f"Shell: {tool_name}"
    elif tool_name in {"write_file", "write_to_file"}:
        title = f"Write: {file_path}"
    elif tool_name in {"edit_file", "replace_file_content", "multi_replace_file_content"}:
        title = f"Edit: {file_path}"
    elif tool_name in {"delete", "delete_file"}:
        title = f"Delete: {file_path}"

    return ToolRendererResult(
        title=title,
        details=details,
        diff_lines=diff_lines,
        file_path=file_path,
    )


__all__ = [
    "DeleteFileRenderer",
    "EditFileRenderer",
    "TaskRenderer",
    "ToolRenderer",
    "ToolRendererResult",
    "WriteFileRenderer",
    "get_renderer",
    "render_tool_approval",
]
