"""Tool-specific approval widgets for HITL display."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Markdown, Static

from opscloud.ui import theme
from opscloud.file_ops import is_sensitive_file_path

if TYPE_CHECKING:
    from textual.app import ComposeResult

_CREDENTIAL_NOTICE = "Contents hidden — file may contain credentials"

_MAX_VALUE_LEN = 200
_MAX_LINES = 30
_MAX_DIFF_LINES = 50
_MAX_PREVIEW_LINES = 20


def format_display_content(content: object) -> str:
    """Coerce arbitrary tool-arg content into a displayable string."""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, indent=2)
    except (TypeError, ValueError, RecursionError):
        return str(content)


def _format_stats(additions: int, deletions: int) -> Content:
    """Format addition/deletion stats as styled Content."""
    colors = theme.get_theme_colors()
    parts: list[str | tuple[str, str] | Content] = []
    if additions:
        parts.append((f"+{additions}", colors.success))
    if deletions:
        if parts:
            parts.append(" ")
        parts.append((f"-{deletions}", colors.error))
    return Content.assemble(*parts) if parts else Content("")


def _file_header(
    file_path: str, additions: int = 0, deletions: int = 0
) -> ComposeResult:
    """Yield the `File:` path header with optional `+N -M` stats."""
    stats = _format_stats(additions, deletions)
    yield Static(
        Content.assemble(
            Content.from_markup("[bold cyan]File:[/bold cyan] $path  ", path=file_path),
            stats,
        )
    )
    yield Static("")


def _count_diff_stats(
    diff_lines: list[str], old_string: str, new_string: str
) -> tuple[int, int]:
    """Count additions and deletions from diff data."""
    if diff_lines:
        additions = sum(
            1
            for line in diff_lines
            if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1
            for line in diff_lines
            if line.startswith("-") and not line.startswith("---")
        )
    else:
        additions = new_string.count("\n") + 1 if new_string else 0
        deletions = old_string.count("\n") + 1 if old_string else 0
    return additions, deletions


class ToolApprovalWidget(Vertical):
    """Base class for tool approval widgets."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Initialize the tool approval widget with data."""
        super().__init__(classes="tool-approval-widget")
        self.data = data

    def compose(self) -> ComposeResult:
        """Default compose - override in subclasses."""
        yield Static("Tool details not available", classes="approval-description")


class GenericApprovalWidget(ToolApprovalWidget):
    """Generic approval widget for unknown tools."""

    def compose(self) -> ComposeResult:
        """Compose the generic tool display."""
        for key, value in self.data.items():
            if value is None:
                continue
            value_str = str(value)
            if len(value_str) > _MAX_VALUE_LEN:
                hidden = len(value_str) - _MAX_VALUE_LEN
                value_str = value_str[:_MAX_VALUE_LEN] + f"... ({hidden} more chars)"
            yield Static(
                f"{key}: {value_str}", markup=False, classes="approval-description"
            )


class WriteFileApprovalWidget(ToolApprovalWidget):
    """Approval widget for write_file / write_to_file - shows file content with syntax highlighting."""

    def compose(self) -> ComposeResult:
        """Compose the file content display with syntax highlighting."""
        file_path = self.data.get("file_path", "")
        content = format_display_content(self.data.get("content", ""))
        file_extension = self.data.get("file_extension", "text")

        # Never render the contents of credential files (e.g. `.env`).
        if is_sensitive_file_path(file_path):
            yield from _file_header(file_path)
            yield Static(Content.styled(_CREDENTIAL_NOTICE, "dim"))
        else:
            lines = content.split("\n")
            total_lines = len(lines)

            yield from _file_header(file_path, additions=total_lines if content else 0)

            if total_lines > _MAX_LINES:
                shown_lines = lines[:_MAX_LINES]
                remaining = total_lines - _MAX_LINES
                truncated_content = (
                    "\n".join(shown_lines) + f"\n... ({remaining} more lines)"
                )
                yield Markdown(f"```{file_extension}\n{truncated_content}\n```")
            else:
                yield Markdown(f"```{file_extension}\n{content}\n```")


class EditFileApprovalWidget(ToolApprovalWidget):
    """Approval widget for edit_file / replace_file_content - shows clean diff with colors."""

    def compose(self) -> ComposeResult:
        """Compose the diff display with colored additions and deletions."""
        file_path = self.data.get("file_path", "")
        diff_lines = self.data.get("diff_lines", [])
        old_string = format_display_content(self.data.get("old_string", ""))
        new_string = format_display_content(self.data.get("new_string", ""))

        additions, deletions = _count_diff_stats(diff_lines, old_string, new_string)
        yield from _file_header(file_path, additions, deletions)

        # Never render the diff of credential files (e.g. `.env`)
        if is_sensitive_file_path(file_path):
            yield Static(Content.styled(_CREDENTIAL_NOTICE, "dim"))
        elif not diff_lines and not old_string and not new_string:
            yield Static("No changes to display", classes="approval-description")
        elif diff_lines:
            yield from self._render_diff_lines_only(diff_lines)
        else:
            yield from self._render_strings_only(old_string, new_string)

    def _render_diff_lines_only(self, diff_lines: list[str]) -> ComposeResult:
        """Render unified diff lines."""
        lines_shown = 0

        for line in diff_lines:
            if lines_shown >= _MAX_DIFF_LINES:
                yield Static(
                    Content.styled(
                        f"... ({len(diff_lines) - lines_shown} more lines)", "dim"
                    )
                )
                break

            if line.startswith(("@@", "---", "+++")):
                continue

            widget = self._render_diff_line(line)
            if widget:
                yield widget
                lines_shown += 1

    def _render_strings_only(self, old_string: str, new_string: str) -> ComposeResult:
        """Render old/new strings without stats."""
        colors = theme.get_theme_colors()
        if old_string:
            yield Static(Content.styled("Removing:", f"bold {colors.error}"))
            yield from self._render_string_lines(old_string, is_addition=False)
            yield Static("")

        if new_string:
            yield Static(Content.styled("Adding:", f"bold {colors.success}"))
            yield from self._render_string_lines(new_string, is_addition=True)

    @staticmethod
    def _render_diff_line(line: str) -> Static | None:
        """Render a single diff line with appropriate styling."""
        raw = line[1:] if len(line) > 1 else ""

        if line.startswith("-"):
            return Static(
                Content.from_markup("- $text", text=raw), classes="diff-removed"
            )
        if line.startswith("+"):
            return Static(
                Content.from_markup("+ $text", text=raw), classes="diff-added"
            )
        if line.startswith(" "):
            return Static(
                Content.from_markup("  $text", text=raw), classes="diff-context"
            )
        if line.strip():
            return Static(line, markup=False)
        return None

    @staticmethod
    def _render_string_lines(text: str, *, is_addition: bool) -> ComposeResult:
        """Render lines from a string with appropriate styling."""
        lines = text.split("\n")
        sign = "+" if is_addition else "-"
        cls = "diff-added" if is_addition else "diff-removed"

        for line in lines[:_MAX_PREVIEW_LINES]:
            yield Static(Content.from_markup(f"{sign} $text", text=line), classes=cls)

        if len(lines) > _MAX_PREVIEW_LINES:
            remaining = len(lines) - _MAX_PREVIEW_LINES
            yield Static(Content.styled(f"... ({remaining} more lines)", "dim"))


class TaskApprovalWidget(ToolApprovalWidget):
    """Approval widget for task tool call (subagent dispatch)."""

    def compose(self) -> ComposeResult:
        subagent_type = (
            self.data.get("subagent_type")
            or self.data.get("agent_name")
            or self.data.get("subagent")
            or "general-purpose"
        )
        description = (
            self.data.get("description")
            or self.data.get("prompt")
            or self.data.get("task")
            or "N/A"
        )

        warning_msg = "Subagent will have access to file operations and shell commands"
        separator = "─" * 40

        yield Static(
            Content.from_markup("Subagent Type: [bold]$sub[/bold]\n", sub=subagent_type),
            classes="task-subagent-type",
        )
        yield Static(
            Content.styled(f"⚠  {warning_msg}  ⚠\n", "yellow"),
            classes="task-warning",
        )
        yield Static(
            Content.from_markup("Task Instructions:\n[dim]$sep[/dim]", sep=separator),
            classes="task-instructions-header",
        )
        yield Static(description, classes="task-instructions-body")


__all__ = [
    "EditFileApprovalWidget",
    "GenericApprovalWidget",
    "TaskApprovalWidget",
    "ToolApprovalWidget",
    "WriteFileApprovalWidget",
    "format_display_content",
]
