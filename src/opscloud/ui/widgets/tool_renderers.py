"""Tool renderers for approval widgets - registry pattern."""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Any

from opscloud.file_ops import build_approval_preview, format_display_path
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
    """Renderer for write_file / write_to_file tool - shows full file content."""

    @staticmethod
    def get_approval_widget(
        tool_args: dict[str, Any],
        assistant_id: str | None = None,
    ) -> tuple[type[ToolApprovalWidget], dict[str, Any]]:
        file_path = tool_args.get("file_path", "") or tool_args.get("path", "") or tool_args.get("TargetFile", "")
        raw_content = tool_args.get("content", "") or tool_args.get("code_content", "") or tool_args.get("CodeContent", "")
        content = format_display_content(raw_content)

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
    """Renderer for edit_file / replace_file_content tool - shows unified diff."""

    @staticmethod
    def get_approval_widget(
        tool_args: dict[str, Any],
        assistant_id: str | None = None,
    ) -> tuple[type[ToolApprovalWidget], dict[str, Any]]:
        file_path = tool_args.get("file_path", "") or tool_args.get("path", "") or tool_args.get("TargetFile", "")
        old_string = format_display_content(tool_args.get("old_string", "") or tool_args.get("TargetContent", ""))
        new_string = format_display_content(tool_args.get("new_string", "") or tool_args.get("ReplacementContent", ""))

        diff_lines = EditFileRenderer._generate_diff(old_string, new_string)

        data = {
            "file_path": file_path,
            "diff_lines": diff_lines,
            "old_string": old_string,
            "new_string": new_string,
        }
        return EditFileApprovalWidget, data

    @staticmethod
    def _generate_diff(old_string: str, new_string: str) -> list[str]:
        """Generate unified diff lines from old and new strings."""
        if not old_string and not new_string:
            return []

        old_lines = old_string.split("\n") if old_string else []
        new_lines = new_string.split("\n") if new_string else []

        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
            n=3,
        )

        diff_list = list(diff)
        return diff_list[2:] if len(diff_list) > 2 else diff_list


_RENDERER_REGISTRY: dict[str, type[ToolRenderer]] = {
    "task": TaskRenderer,
    "write_file": WriteFileRenderer,
    "write_to_file": WriteFileRenderer,
    "edit_file": EditFileRenderer,
    "replace_file_content": EditFileRenderer,
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
    elif tool_name in {"edit_file", "replace_file_content"}:
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
