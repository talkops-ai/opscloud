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
_MAX_LINES = 33
_MAX_DIFF_LINES = 33
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
    from opscloud.ui.widgets.diff import format_diff_stats

    return format_diff_stats(additions, deletions)


def _file_header(
    file_path: str, additions: int = 0, deletions: int = 0
) -> ComposeResult:
    """Yield the file path header with ``+N -M`` stats."""
    from opscloud.ui.widgets.diff import format_file_diff_header

    yield Static(
        format_file_diff_header(file_path, additions=additions, deletions=deletions),
        classes="diff-line-header",
    )


def _auto_approve_file_edits_hint() -> Static:
    """Build the ``shift+tab to auto-approve file edits`` footer hint."""
    return Static(
        Content.assemble(
            ("shift+tab", "bold"),
            (" to auto-approve file edits", "dim"),
        ),
        classes="diff-auto-approve-hint",
    )


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
        from opscloud.ui.widgets.diff import compute_line_diff

        computed = compute_line_diff(old_string, new_string)
        additions = sum(
            1
            for line in computed
            if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1
            for line in computed
            if line.startswith("-") and not line.startswith("---")
        )
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
    """Approval widget for write_file / write_to_file - shows file content with diff and line highlights."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self._full_diff = False
        self._diff_body: Static | None = None

    def _build_diff_text(self) -> str:
        diff_lines = self.data.get("diff_lines", [])
        if diff_lines:
            return "\n".join(diff_lines)
        content = format_display_content(self.data.get("content", ""))
        lines = content.splitlines()
        total_lines = len(lines)
        preview_diff = [f"@@ -0,0 +1,{total_lines} @@"] + [f"+{line}" for line in lines]
        return "\n".join(preview_diff)

    @property
    def has_expandable_diff(self) -> bool:
        file_path = self.data.get("file_path", "")
        if is_sensitive_file_path(file_path):
            return False
        from opscloud.ui.widgets.diff import render_diff_content

        _, remaining = render_diff_content(
            self._build_diff_text(),
            max_lines=_MAX_LINES,
            show_stats=False,
        )
        return remaining > 0

    def toggle_full_diff(self) -> bool:
        """Toggle between preview and full diff view."""
        if not self.has_expandable_diff and not self._full_diff:
            return False
        self._full_diff = not self._full_diff
        if self._diff_body is not None:
            from opscloud.ui.widgets.diff import render_diff_content

            cap = None if self._full_diff else _MAX_LINES
            rendered, _ = render_diff_content(
                self._build_diff_text(),
                max_lines=cap,
                show_stats=False,
                expand_hint="f for full diff",
            )
            self._diff_body.update(rendered)
        return True

    def compose(self) -> ComposeResult:
        """Compose the file content display with diff and line highlights."""
        file_path = self.data.get("file_path", "")
        diff_lines = self.data.get("diff_lines", [])
        content = format_display_content(self.data.get("content", ""))

        # Never render the contents of credential files (e.g. `.env`).
        if is_sensitive_file_path(file_path):
            yield from _file_header(file_path)
            yield Static(Content.styled(_CREDENTIAL_NOTICE, "dim"))
            return

        from opscloud.ui.widgets.diff import render_diff_content

        if diff_lines:
            additions, deletions = _count_diff_stats(diff_lines, "", "")
        else:
            lines = content.splitlines()
            additions, deletions = (len(lines) if content else 0), 0

        yield from _file_header(file_path, additions=additions, deletions=deletions)
        cap = None if self._full_diff else _MAX_LINES
        rendered, _ = render_diff_content(
            self._build_diff_text(),
            max_lines=cap,
            show_stats=False,
            expand_hint="f for full diff",
        )
        self._diff_body = Static(rendered, classes="diff-block-body")
        yield self._diff_body
        yield Static("")
        yield _auto_approve_file_edits_hint()


class EditFileApprovalWidget(ToolApprovalWidget):
    """Approval widget for edit_file / replace_file_content - shows clean diff with colors and line numbers."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self._full_diff = False
        self._diff_body: Static | None = None

    def _resolved_diff_lines(self) -> list[str]:
        diff_lines = self.data.get("diff_lines", [])
        if diff_lines:
            return list(diff_lines)
        old_string = format_display_content(self.data.get("old_string", ""))
        new_string = format_display_content(self.data.get("new_string", ""))
        if not old_string and not new_string:
            return []
        from opscloud.ui.widgets.diff import compute_line_diff

        return compute_line_diff(old_string, new_string)

    @property
    def has_expandable_diff(self) -> bool:
        file_path = self.data.get("file_path", "")
        if is_sensitive_file_path(file_path):
            return False
        diff_lines = self._resolved_diff_lines()
        if not diff_lines:
            return False
        from opscloud.ui.widgets.diff import render_diff_content

        _, remaining = render_diff_content(
            "\n".join(diff_lines),
            max_lines=_MAX_DIFF_LINES,
            show_stats=False,
        )
        return remaining > 0

    def toggle_full_diff(self) -> bool:
        """Toggle between truncated inline preview and full diff."""
        if not self.has_expandable_diff and not self._full_diff:
            return False
        self._full_diff = not self._full_diff
        if self._diff_body is not None:
            from opscloud.ui.widgets.diff import render_diff_content

            cap = None if self._full_diff else _MAX_DIFF_LINES
            rendered, _ = render_diff_content(
                "\n".join(self._resolved_diff_lines()),
                max_lines=cap,
                show_stats=False,
                expand_hint="f for full diff",
            )
            self._diff_body.update(rendered)
        return True

    def compose(self) -> ComposeResult:
        """Compose the diff display with colored additions and deletions."""
        file_path = self.data.get("file_path", "")
        diff_lines = self._resolved_diff_lines()
        old_string = format_display_content(self.data.get("old_string", ""))
        new_string = format_display_content(self.data.get("new_string", ""))

        additions, deletions = _count_diff_stats(diff_lines, old_string, new_string)
        yield from _file_header(file_path, additions, deletions)

        # Never render the diff of credential files (e.g. `.env`)
        if is_sensitive_file_path(file_path):
            yield Static(Content.styled(_CREDENTIAL_NOTICE, "dim"))
        elif not diff_lines and not old_string and not new_string:
            yield Static("No changes to display", classes="approval-description")
        else:
            from opscloud.ui.widgets.diff import render_diff_content

            cap = None if self._full_diff else _MAX_DIFF_LINES
            rendered, _ = render_diff_content(
                "\n".join(diff_lines),
                max_lines=cap,
                show_stats=False,
                expand_hint="f for full diff",
            )
            self._diff_body = Static(rendered, classes="diff-block-body")
            yield self._diff_body
            yield Static("")
            yield _auto_approve_file_edits_hint()

    def _render_diff_lines_only(self, diff_lines: list[str]) -> ComposeResult:
        """Render unified diff lines."""
        from opscloud.ui.widgets.diff import compose_diff_lines

        yield from compose_diff_lines(
            "\n".join(diff_lines),
            max_lines=_MAX_DIFF_LINES,
            show_stats=False,
        )

    def _render_strings_only(self, old_string: str, new_string: str) -> ComposeResult:
        """Render old/new strings via unified diff lines."""
        from opscloud.ui.widgets.diff import compose_diff_lines, compute_line_diff

        diff_lines = compute_line_diff(old_string, new_string)
        yield from compose_diff_lines(
            "\n".join(diff_lines),
            max_lines=_MAX_DIFF_LINES,
            show_stats=False,
        )

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
