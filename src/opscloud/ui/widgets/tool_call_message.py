from __future__ import annotations
import time
from typing import Any, ClassVar
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from rich.console import Console as RichConsole, ConsoleOptions, RenderResult
from rich.styled import Styled
from rich.theme import Theme
from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll, Vertical
from textual.widgets import Static
from textual.content import Content, Span
from textual.style import Style
from opscloud.ui.widgets.diff import compose_diff_lines
from opscloud.ui.widgets.tool_display import (
    format_tool_display,
    format_tool_result_summary,
)

"""Message widgets."""



import ast
import json
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from opscloud.utils.logger import get_logger

from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.events import Click
from textual.geometry import Offset
from textual.message import Message
from textual._context import NoActiveAppError
from textual.reactive import var
from textual.selection import Selection
from textual.style import Style as TStyle
from textual.widgets import Static

from opscloud.ui import theme
ASK_USER_ANSWERED_SUMMARY = "answered"
ASK_USER_FAILED_SUMMARY = "failed"
AskUserRowSummary = str
from opscloud.config.settings import (
    get_glyphs,
    is_ascii_mode,
)
from opscloud.ui.widgets.loading import format_duration
from opscloud.ui.widgets.tool_display import (
    format_tool_display,
    format_tool_parts,
)
EXECUTE_HEADER_MAX_LENGTH = 100

_FILE_DIFF_TOOLS: frozenset[str] = frozenset(
    {
        "edit_file",
        "write_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "delete",
        "delete_file",
    }
)
"""Tools that modify, write, or delete files and display a line-numbered diff preview."""

_TOOL_GROUP_EXCLUSIONS = frozenset(
    {
        "ask_user",
        "write_todos",
    }
)
"""Tools that stay expanded instead of collapsing into step summaries."""
from opscloud.ui.widgets.diff import (
    compose_diff_lines,
    format_file_diff_header,
    render_diff_content,
)
from opscloud.ui.widgets.tool_renderers import get_renderer
from opscloud.ui.widgets.tool_widgets import is_sensitive_file_path

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rich.console import (
        Console as RichConsole,
        ConsoleOptions,
        RenderResult,
    )
    from textual.app import ComposeResult
    from textual.events import MouseMove
    from textual.timer import Timer
    from textual.widget import Widget
    from textual.widgets import Markdown
    from textual.widgets._markdown import MarkdownStream

    from opscloud.ui.theme import ThemeColors

logger = get_logger(__name__)


def _mode_color(mode: str | None, widget_or_app: object | None = None) -> str:
    """Return the hex color string for a mode, falling back to primary.

    Args:
        mode: Mode name (e.g. `'shell'`, `'command'`) or `None`.
        widget_or_app: Textual widget or `App` for theme-aware lookup.

    Returns:
        Color string from the active theme's `ThemeColors`.
    """
    colors = theme.get_theme_colors(widget_or_app)
    if not mode:
        return colors.primary
    if mode == "shell_incognito":
        return colors.mode_incognito
    if mode == "shell":
        return colors.mode_bash
    if mode == "command":
        return colors.mode_command
    logger.warning("Missing color for mode '%s'; falling back to primary.", mode)
    return colors.primary


@dataclass(frozen=True, slots=True)
class FormattedOutput:
    """Result of formatting tool output for display."""

    content: Content
    """Styled `Content` for the formatted output."""

    truncation: str | None = None
    """Description of truncated content (e.g., "10 more lines"), or None if no
    truncation occurred."""


# Maximum number of tool arguments to display inline
_MAX_INLINE_ARGS = 3

# Truncation limits for display
_MAX_TODO_CONTENT_LEN = 70
_DEFAULT_TODO_WRAP_WIDTH = 80
_TODO_WRAP_GUARD_COLUMNS = 4
_MAX_WEB_CONTENT_LEN = 100

# User message display truncation — when content exceeds this many characters,
# only the head and tail are rendered with an elision marker in between.
# This keeps very large pastes from flooding the conversation scrollback.
_USER_MSG_MAX_DISPLAY_CHARS = 10_000
_USER_MSG_TRUNCATE_HEAD_CHARS = 2_500
_USER_MSG_TRUNCATE_TAIL_CHARS = 2_500

# Tools that have their key info already in the header (no need for args line)
_TOOLS_WITH_HEADER_INFO: set[str] = {
    # Filesystem tools
    "ls",
    "read_file",
    "view_file",
    "write_file",
    "write_to_file",
    "edit_file",
    "replace_file_content",
    "multi_replace_file_content",
    "delete",
    "delete_file",
    "glob",
    "grep",
    "execute",  # sandbox shell
    # Web tools
    "web_search",
    "fetch_url",
    "ask_user",
    # Agent tools
    "task",
    "write_todos",
}


# Tools whose key info (file path / search pattern) is already in the header, so
# their output body is collapsed entirely by default — an expand affordance
# replaces the inline preview. `read_file` echoes the file; grep/glob echo the
# matches for a pattern the header already names.
_COLLAPSE_OUTPUT_BY_DEFAULT: set[str] = {
    "read_file",
    "view_file",
    "list_dir",
    "grep",
    "glob",
}


# Tools whose collapsed body is always the formatter's compact preview, no
# matter how short the raw output is, and whose expandability is therefore
# decided by the formatter rather than by the raw size thresholds. `write_todos`
# renders a per-item summary; `ask_user` renders a one-line summary so a
# two-line transcript still keeps its answers behind an expand click.
_ALWAYS_PREVIEW_TOOLS: frozenset[str] = frozenset(
    {
        "write_todos",
        "ask_user",
        "edit_file",
        "write_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "delete",
        "delete_file",
    }
)


# An `ask_user` row whose recorded output is exactly one of these holds only a
# fallback summary — no `ToolMessage` transcript ever arrived — so there is
# nothing for an expand click to reveal. Recognized by value rather than by the
# `_deferred_success_settled` flag so the suppression also holds for a row rebuilt
# from the message store, where that flag is not persisted (a rehydrated row is
# always already terminal). A real transcript always begins `Q: `, so it can never
# collide with these.
_ASK_USER_ROW_SUMMARIES: frozenset[str] = frozenset(
    {ASK_USER_ANSWERED_SUMMARY, ASK_USER_FAILED_SUMMARY}
)


# Long-running tools whose completed status row reports how long they ran
# ("Took <duration>") when a run was timed, instead of being hidden. `execute`
# shells and `task` subagent dispatches can both run for a while, so the elapsed
# time is useful.
_TIMED_SUCCESS_TOOLS: set[str] = {
    "execute",
    "task",
}


# CSS classes applied to a `ToolCallMessage` to tint the whole row by terminal
# outcome (see its `DEFAULT_CSS`). Running/pending states carry none of these.
_STATUS_CLASSES: frozenset[str] = frozenset(
    {"-status-success", "-status-error", "-status-rejected", "-status-skipped"}
)


_SUCCESS_EXIT_RE = re.compile(r"\n?\[Command succeeded with exit code 0\]\s*$")
"""Strip the SDK's `[Command succeeded with exit code 0]` trailer from tool output."""


_READ_FILE_GUTTER_RE = re.compile(r"^ *(\d+(?:\.\d+)?)(?:  |\t)(.*)$")
"""Match a `read_file` gutter row into (marker, source).

The marker is a bare `N` or `N.M` (the latter a wrapped-line continuation) —
both sides of the dot required, so a stray `.5` head is not a gutter. The
separator is exactly two spaces (current format) or a single tab (legacy
`cat -n`). Only the separator is consumed and leading padding is spaces-only, so
source indentation — including leading tabs — after the gutter stays put.
See `ToolCallMessage._compact_line_gutter`.
"""


def _strip_success_exit_line(text: str) -> str:
    """Remove the `[Command succeeded with exit code 0]` trailer.

    Non-zero exit codes are left intact (they come through `set_error`).

    Args:
        text: Raw tool output string.

    Returns:
        Text with the success exit-code trailer removed, if present.
    """
    return _SUCCESS_EXIT_RE.sub("", text)


def _arg_shown_in_header(value: object, shown: str) -> bool:
    """Whether ``value`` is the argument already rendered on the tool header.

    Args:
        value: A raw tool-call argument value.
        shown: The header's argument summary (`ToolDisplayParts.detail`),
            possibly truncated with a trailing ellipsis.

    Returns:
        True when the header already displays this value, in full or as its
        truncated prefix.
    """
    if not shown or isinstance(value, (dict, list, tuple, set)):
        return False
    text = str(value).strip()
    if not text:
        return False
    if text == shown:
        return True
    # `truncate_value` always appends a literal "…" regardless of ASCII mode.
    return shown.endswith("\u2026") and text.startswith(shown[:-1])




_ToolStatus = Literal["pending", "running", "success", "error", "rejected", "skipped"]
"""The full set of lifecycle states a tool call can hold.

Kept as a closed `Literal` so `ty` flags typos at the assignment sites and so
the grouping predicates (`is_success`/`is_failed`/`is_pending`) partition a
known universe.
"""

_TOOL_AWAITING_APPROVAL_ACCESSORY_CLASS = "-tool-awaiting-approval-accessory"
"""Marker class hiding a tool's accessories while an approval prompt replaces it.

Deliberately distinct from `_TOOL_GROUP_COLLAPSED_ACCESSORY_CLASS`: a footer can
be hidden for both reasons at once, and releasing one reason must not un-hide a
footer still hidden by the other. Merging the two into a single class would make
`ToolGroupSummary._release_collapsible` reveal a footer whose tool is still
hidden behind an approval prompt.

Applied with `set_class` rather than by assigning `display`. An inline `display`
permanently outranks the CSS cascade, so assigning it here would strand the
footer against the user's `/timestamps` preference forever. Styled in
`app.tcss`, which relies on rule order to win the specificity tie against that
preference's own class.
"""


class ToolCallMessage(Vertical):
    """Widget displaying a tool call with collapsible output.

    Tool outputs are shown as a 3-line preview by default.
    Press Ctrl+O to expand/collapse the full output.
    Shows an animated "Running..." indicator while the tool is executing.
    """

    DEFAULT_CSS = """
    ToolCallMessage {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
        background: transparent;
    }

    ToolCallMessage .tool-header {
        height: auto;
    }

    ToolCallMessage .tool-task-desc {
        color: $text-muted;
        margin-left: 2;
        text-style: italic;
    }

    ToolCallMessage .tool-args {
        color: $text-muted;
        margin-left: 2;
    }

    ToolCallMessage .tool-status {
        margin-left: 2;
    }

    ToolCallMessage .tool-status.pending {
        color: $warning;
    }

    ToolCallMessage .tool-status.success {
        color: $success;
    }

    ToolCallMessage .tool-status.error {
        color: $error;
    }

    ToolCallMessage .tool-status.rejected {
        color: $warning;
    }

    ToolCallMessage .tool-reject-reason {
        margin: 0 0 0 2;
        height: auto;
        color: $text-muted;
    }

    ToolCallMessage .tool-output-row {
        layout: horizontal;
        height: auto;
        width: 1fr;
        margin: 0 0 0 2;
    }

    /* Fixed gutter holds the output glyph so soft-wrapped content lines stay
       aligned to a single hanging indent instead of falling under the glyph. */
    ToolCallMessage .tool-output-gutter {
        width: 2;
        height: 1;
        color: $text-muted;
    }

    ToolCallMessage .tool-output {
        margin: 0;
        padding: 0;
        height: auto;
        width: 1fr;
    }

    ToolCallMessage .tool-output-preview {
        margin: 0;
        width: 1fr;
    }

    ToolCallMessage .tool-output-hint {
        margin: 0 0 0 4;
        color: $text-muted;
    }

    /* Expanded group members nest under their summary line. */
    ToolCallMessage.-grouped {
        margin: 0;
        padding: 0 1 0 3;
    }

    /* Success and running rows stay unframed: the coloured status glyph on
       the header carries the outcome. Failures get a bar so they are hard to
       miss when scrolling back: red error, amber rejected/skipped. */
    ToolCallMessage.-status-error {
        border-left: wide $error;
        background: transparent;
    }

    ToolCallMessage.-status-rejected,
    ToolCallMessage.-status-skipped {
        border-left: wide $warning;
        background: transparent;
    }
    """
    """Header glyph tracks tool lifecycle; failures add a coloured bar."""

    _PREVIEW_LINES = 6
    """Maximum number of lines to show in preview mode."""

    _PREVIEW_CHARS = 400
    """Maximum number of characters to show in preview mode."""

    _TASK_DESC_MAX_LENGTH = 120
    """Maximum `task` description length shown before it is truncated.

    A longer description collapses to at most this many characters (trailing
    whitespace trimmed) with a trailing ellipsis and becomes expandable via
    click or Ctrl+O.
    """

    _RUNNING_TIMER_THRESHOLD_SECS = 10
    """Seconds a tool must run before the elapsed-time counter appears.

    Short tool calls finish well under this threshold, so the timer would only
    flicker on briefly; suppressing it until the tool is genuinely slow keeps
    the "Running..." row quiet for the common case.
    """

    def __init__(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a tool call message.

        Args:
            tool_name: Name of the tool being called
            args: Tool arguments (optional)
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(**kwargs)
        self._tool_name = "tool" if tool_name in (None, "None", "") else tool_name
        self._args = args or {}
        self._status: _ToolStatus = "pending"  # Waiting for approval or auto-approve
        self._output: str = ""
        self._expanded: bool = False
        self._args_expanded: bool = False
        self._task_desc_expanded: bool = False
        # User-provided reason attached to a HITL reject decision (if any).
        self._reject_reason: str | None = None
        # Widget references (set in on_mount)
        self._status_widget: Static | None = None
        self._header_widget: Static | None = None
        self._task_desc_widget: Static | None = None
        self._task_desc_hint_widget: Static | None = None
        self._args_widget: Static | None = None
        self._args_hint_widget: Static | None = None
        self._preview_widget: Static | None = None
        self._preview_row: Horizontal | None = None
        self._hint_widget: Static | None = None
        self._full_widget: Static | None = None
        self._full_row: Horizontal | None = None
        self._reject_reason_widget: Static | None = None
        # Animation state
        self._spinner_position = 0
        self._start_time: float | None = None
        self._duration: float | None = None
        self._animation_timer: Timer | None = None
        # Terminal success this row earned but has not rendered. See
        # `defer_success`; `_deferred_success_settled` separates "still awaiting
        # the richer result" from "already fell back to the summary".
        self._deferred_success_output: str | None = None
        self._deferred_success_settled: bool = False
        # One-shot guard so `_format_ask_user_output` reports unusable `questions`
        # args once per widget rather than on every re-render.
        self._ask_user_args_warned: bool = False
        # Deferred state for hydration (set by MessageData.to_widget)
        self._deferred_status: str | None = None
        self._deferred_output: str | None = None
        self._deferred_duration: float | None = None
        self._deferred_expanded: bool = False
        self._deferred_reject_reason: str | None = None
        # Whether the widget is currently hidden because an approval prompt
        # is rendering the same content (see `set_awaiting_approval`).
        self._awaiting_approval: bool = False
        # Transcript decorations that must follow approval visibility without
        # losing their independent user-controlled visibility state.
        self._visibility_accessories: list[Widget] = []
        self._cached_file_diff: tuple[list[str], int, int] | None = None
        if self._tool_name in _FILE_DIFF_TOOLS and self._args:
            self._cached_file_diff = self._compute_file_diff_lines()

    def _compute_file_diff_lines(self) -> tuple[list[str], int, int]:
        """Compute diff lines and (+added, -removed) counts for file edit/write tools."""
        if self._tool_name not in _FILE_DIFF_TOOLS or not self._args:
            return [], 0, 0
        file_path = str(
            self._args.get("file_path")
            or self._args.get("TargetFile")
            or self._args.get("path")
            or ""
        )
        if is_sensitive_file_path(file_path):
            return [], 0, 0
        try:
            renderer = get_renderer(self._tool_name)
            _widget_cls, data = renderer.get_approval_widget(self._args)
        except Exception:
            return [], 0, 0

        diff_lines = data.get("diff_lines")
        if isinstance(diff_lines, list) and diff_lines:
            added = int(data.get("stats_add", 0))
            removed = int(data.get("stats_del", 0))
            if added == 0 and removed == 0:
                added = sum(
                    1
                    for ln in diff_lines
                    if isinstance(ln, str) and ln.startswith("+") and not ln.startswith("+++")
                )
                removed = sum(
                    1
                    for ln in diff_lines
                    if isinstance(ln, str) and ln.startswith("-") and not ln.startswith("---")
                )
            return [str(ln) for ln in diff_lines], added, removed

        content = (
            data.get("content")
            or self._args.get("content")
            or self._args.get("CodeContent")
        )
        if isinstance(content, str) and content:
            raw_lines = content.splitlines()
            synth_diff = [
                f"@@ -0,0 +1,{len(raw_lines)} @@",
                *[f"+{ln}" for ln in raw_lines],
            ]
            return synth_diff, len(raw_lines), 0

        return [], 0, 0

    def _build_file_diff_lines(self) -> tuple[list[str], int, int]:
        """Return cached or freshly computed diff lines and (+added, -removed) stats."""
        if self._cached_file_diff is None:
            self._cached_file_diff = self._compute_file_diff_lines()
        return self._cached_file_diff

    def compose(self) -> ComposeResult:
        """Compose the tool call message layout.

        Yields:
            Widgets for header, arguments, status, and output display.
        """
        self._header_widget = Static(
            self._header_content(), classes="tool-header", id="tool-header"
        )
        yield self._header_widget
        # Task: dedicated description line (dim, truncated). A long description
        # collapses to a truncated preview that expands on click or Ctrl+O.
        if self._tool_name == "task":
            if self._task_description():
                self._task_desc_widget = Static(
                    self._task_desc_content(),
                    classes="tool-task-desc",
                    id="task-desc",
                )
                yield self._task_desc_widget
                self._task_desc_hint_widget = Static("", classes="tool-output-hint", id="task-desc-hint")
                yield self._task_desc_hint_widget
        else:
            self._task_desc_widget = None
            self._task_desc_hint_widget = None

        # Only show args for tools where header doesn't capture the key info
        if self._tool_name not in _TOOLS_WITH_HEADER_INFO:
            args = self._filtered_args()
            # The header already shows one argument (e.g. `ap-south-1`);
            # don't repeat it in the dim args line below.
            shown = format_tool_parts(self._tool_name, self._args).detail
            if args:
                inline_items = {}
                for k, v in list(args.items())[:_MAX_INLINE_ARGS]:
                    if k in {"code", "script"} or (isinstance(v, str) and "\n" in v):
                        continue
                    if _arg_shown_in_header(v, shown):
                        continue
                    inline_items[k] = v
                remaining = sum(
                    1 for v in args.values() if not _arg_shown_in_header(v, shown)
                )
                if inline_items:
                    args_str = ", ".join(
                        f"{k}={v!r}" for k, v in inline_items.items()
                    )
                    if remaining > len(inline_items):
                        args_str += ", ..."
                    yield Static(
                        Content.from_markup("[dim]($args)[/dim]", args=args_str),
                        classes="tool-args",
                    )
        # Collapsed argument detail for tools whose args are too noisy inline.
        # Mounted for every tool but only populated when `has_expandable_args` is True.
        self._args_widget = Static("", classes="tool-args", id="args-full")
        yield self._args_widget
        self._args_hint_widget = Static("", classes="tool-output-hint", id="args-hint")
        yield self._args_hint_widget
        # Status - shows running animation while pending, then final status
        self._status_widget = Static("", classes="tool-status", id="status")
        yield self._status_widget
        # Optional HITL reject reason (only shown when user rejected with a message)
        self._reject_reason_widget = Static("", classes="tool-reject-reason", id="reject-reason")
        yield self._reject_reason_widget
        # Output area - hidden initially, shown when output is set. The glyph
        # lives in a fixed-width gutter so wrapped content aligns to a single
        # hanging indent rather than wrapping back under the glyph.
        output_prefix = get_glyphs().output_prefix
        
        self._preview_widget = Static("", classes="tool-output-preview", id="output-preview")
        self._preview_row = Horizontal(
            Static(output_prefix, classes="tool-output-gutter"),
            self._preview_widget,
            classes="tool-output-row",
            id="output-preview-row",
        )
        yield self._preview_row
        
        self._full_widget = Static("", classes="tool-output", id="output-full")
        self._full_row = Horizontal(
            Static(output_prefix, classes="tool-output-gutter"),
            self._full_widget,
            classes="tool-output-row",
            id="output-full-row",
        )
        yield self._full_row
        
        self._hint_widget = Static("", classes="tool-output-hint", id="output-hint")
        yield self._hint_widget

    def on_mount(self) -> None:
        """Cache widget references and hide all status/output areas initially."""
        if is_ascii_mode():
            self.add_class("-ascii")

        # Hide everything initially - status only shown when running or on error/reject
        if self._status_widget:
            self._status_widget.display = False
        if self._args_widget:
            self._args_widget.display = False
        if self._args_hint_widget:
            self._args_hint_widget.display = False
        if self._preview_row:
            self._preview_row.display = False
        if self._hint_widget:
            self._hint_widget.display = False
        if self._full_row:
            self._full_row.display = False
        if self._reject_reason_widget:
            self._reject_reason_widget.display = False
        self._update_args_display()
        self._update_task_desc_display()

        # Restore deferred state if this widget was hydrated from data
        self._restore_deferred_state()

    def _restore_deferred_state(self) -> None:
        """Restore state from deferred values (used when hydrating from data)."""
        if self._deferred_status is None:
            return

        status = self._deferred_status
        output = self._deferred_output or ""
        duration = self._deferred_duration
        self._expanded = self._deferred_expanded
        if self._deferred_reject_reason:
            self._reject_reason = self._deferred_reject_reason

        # Clear deferred values
        self._deferred_status = None
        self._deferred_output = None
        self._deferred_duration = None
        self._deferred_expanded = False
        self._deferred_reject_reason = None

        # Restore based on status (don't restart animations for running tools)
        match status:
            case "success":
                self._status = "success"
                self._output = output
                self._duration = duration
                self._apply_status_class("success")
                if self._tool_name in _TIMED_SUCCESS_TOOLS and duration is not None:
                    self._show_timed_success_status(duration)
                else:
                    self._show_success_status()
                self._update_output_display()
            case "error":
                self._status = "error"
                self._output = output
                self._apply_status_class("error")
                if self._status_widget:
                    self._status_widget.display = False
                self._update_output_display()
            case "rejected":
                self._status = "rejected"
                self._apply_status_class("rejected")
                self._show_terminal_label("Rejected", "rejected")
                self._update_reject_reason_display()
            case "skipped":
                self._status = "skipped"
                self._apply_status_class("skipped")
                self._show_terminal_label("Skipped", "rejected")
            case "running":
                # For running tools, show a static spinner frame without
                # animation (animations shouldn't be restored for archived tools)
                self._status = "running"
            case _:
                # pending or unknown - leave as default
                pass
        self._refresh_header()

    def set_running(self) -> None:
        """Mark the tool as running (approved and executing).

        Call this when approval is granted to start the running animation.
        """
        if self._status == "running":
            return  # Already running

        self._status = "running"
        self._duration = None
        self._start_time = time.time()
        # The spinner and elapsed time live on the header line; the separate
        # status line stays hidden while running.
        self._update_running_animation()
        self._animation_timer = self.set_interval(0.1, self._update_running_animation)

    def _update_running_animation(self) -> None:
        """Advance the header spinner (and elapsed time once the tool is slow)."""
        if self._status != "running":
            return
        frames = get_glyphs().spinner_frames
        self._spinner_position = (self._spinner_position + 1) % len(frames)
        if not self.display:
            return
        self._refresh_header(layout=False)

    def pause_running(self) -> None:
        """Pause the running spinner while the tool awaits a user decision.

        Reverts the row to its pending appearance (status hidden) and stops the
        animation so a tool blocked on HITL approval or `ask_user` input does
        not misleadingly display "Running...". Resume with `set_running`, which
        restarts the elapsed timer from the moment execution actually begins.
        """
        if self._status != "running":
            return
        self._stop_animation()
        self._status = "pending"
        self._start_time = None
        if self._status_widget:
            self._status_widget.remove_class("pending")
            self._status_widget.display = False
        self._refresh_header()

    def _stop_animation(self) -> None:
        """Stop the running animation."""
        if self._animation_timer is not None:
            self._animation_timer.stop()
            self._animation_timer = None

    def _apply_status_class(self, status: str) -> None:
        """Tint the whole row to match a terminal outcome.

        Swaps the `-status-*` CSS class so the row border and background
        reflect success/error/rejected/skipped. Running and pending states keep
        the default `$tool` accent, so they clear any prior status class.

        Args:
            status: Terminal status name (`success`, `error`, `rejected`,
                `skipped`); any other value clears the tint.
        """
        for name in _STATUS_CLASSES:
            self.remove_class(name)
        class_name = f"-status-{status}"
        if class_name in _STATUS_CLASSES:
            self.add_class(class_name)

    _SUBAGENT_LABEL: ClassVar[str] = "Subagent"
    """Header label for `task` dispatches (the raw display name is `task`)."""

    def _header_content(self) -> Content:
        """Build the styled one-line header for the current lifecycle state.

        ``<status glyph> <Name> <scope>  <argument>  · <elapsed>``

        The glyph carries the outcome so the row needs no separate status
        line: a teal spinner while running, green check on success, red cross
        on error, amber cross when rejected and an amber hollow circle while
        pending (awaiting approval). Errors also turn the tool name red.
        """
        glyphs = get_glyphs()
        status = self._status
        if status == "running":
            frames = glyphs.spinner_frames
            mark, mark_style = frames[self._spinner_position % len(frames)], "$primary"
        elif status == "success":
            mark, mark_style = glyphs.checkmark, "bold $success"
        elif status == "error":
            mark, mark_style = glyphs.error, "bold $error"
        elif status == "rejected":
            mark, mark_style = glyphs.error, "bold $warning"
        elif status == "skipped":
            mark, mark_style = glyphs.circle_empty, "dim"
        else:
            mark, mark_style = glyphs.circle_empty, "$warning"

        parts = format_tool_parts(self._tool_name, self._args)
        name = self._SUBAGENT_LABEL if parts.kind == "task" else parts.name
        name_style = "bold $error" if status == "error" else "bold"
        pieces: list[tuple[str, str] | str] = [(f"{mark} ", mark_style), (name, name_style)]
        if parts.scope:
            pieces.append((f" {parts.scope}", "dim"))
        if parts.detail:
            pieces.append("  ")
            if parts.kind == "command":
                pieces.append(("$ ", "dim"))
            elif parts.kind == "task":
                pieces.append((parts.detail, "$secondary"))
            if parts.kind != "task":
                pieces.append(parts.detail)

        if self._tool_name in _FILE_DIFF_TOOLS:
            _diff_lines, added, removed = self._build_file_diff_lines()
            if added > 0 or removed > 0:
                pieces.append("  ")
                if added > 0:
                    pieces.append((f"+{added}", "bold #4ec969"))
                if removed > 0:
                    if added > 0:
                        pieces.append(" ")
                    pieces.append((f"-{removed}", "bold #f85149"))

        meta = self._header_meta()
        if meta:
            pieces.append((f"  {glyphs.separator} {meta}", "dim"))
        return Content.assemble(*pieces)

    def _header_meta(self) -> str:
        """Elapsed time for slow running tools, or run time for timed ones."""
        if self._status == "running" and self._start_time is not None:
            elapsed = int(time.time() - self._start_time)
            if elapsed >= self._RUNNING_TIMER_THRESHOLD_SECS:
                return format_duration(elapsed)
        elif self._status == "success" and self._duration is not None:
            if self._duration >= 1:
                return format_duration(int(self._duration))
        return ""

    def _refresh_header(self, *, layout: bool = True) -> None:
        """Re-render the header line for the current state."""
        if self._header_widget is not None:
            self._header_widget.update(self._header_content(), layout=layout)

    def _show_terminal_label(self, label: str, css_class: str) -> None:
        """Show a short terminal label (e.g. ``Rejected``) under the header."""
        if self._status_widget is None:
            return
        self._status_widget.remove_class("pending")
        self._status_widget.add_class(css_class)
        style = "dim" if label == "Skipped" else "$warning"
        self._status_widget.update(Content.styled(label, style))
        self._status_widget.display = True

    def defer_success(self, output: AskUserRowSummary) -> None:
        """Record a terminal success this row earned but has not yet rendered.

        An answered `ask_user` deliberately stays in `_current_tool_messages` so
        the streamed `ToolMessage` can settle it with the full Q&A transcript.
        That leaves the row non-terminal in the meantime, and every teardown
        sweep treats a still-tracked row as a failure — so without this the row
        renders as rejected or as an agent error, and its `tool.result` reports
        `tool_status="error"`, for a question the user answered normally.

        Args:
            output: Summary to settle with if the `ToolMessage` never arrives.
                Narrowed to `AskUserRowSummary` because `_format_ask_user_output`
                recognizes exactly those values as "no transcript behind this row"
                and suppresses the expand affordance for them. Passing the
                transcript here would strand it unreadable on the row.
        """
        self._deferred_success_output = output
        self._deferred_success_settled = False

    @property
    def deferred_success_output(self) -> str | None:
        """Terminal output for a row that earned a success it did not render.

        Set while the row awaits its richer result and deliberately kept after a
        fallback settle, because a settled row can still be tracked in
        `_current_tool_messages` and swept again later (`textual_adapter`'s
        `finally` backstop). `_dispatch_terminal_tool_result_hooks` reads this as
        the "this row already succeeded" flag, so clearing it on settle would make
        that later sweep report a fabricated failure.
        """
        return self._deferred_success_output

    @property
    def is_awaiting_deferred_result(self) -> bool:
        """Whether this row still expects a richer result to replace its summary.

        Distinct from `deferred_success_output`, which stays set after a fallback
        settle. Callers that must not act on an already-settled row — recovering
        an interrupted turn's `tool_calls`, or imposing a terminal failure — ask
        this instead.
        """
        return self._deferred_success_output is not None and (
            not self._deferred_success_settled
        )

    def clear_deferred_success(self) -> None:
        """Drop the deferred outcome once an authoritative result supersedes it.

        Called when the streamed `ToolMessage` settles the row, so its real
        status wins — including an error, which `set_error` would otherwise
        redirect back to the deferred success.
        """
        self._deferred_success_output = None
        self._deferred_success_settled = False

    def settle_deferred_success(self) -> bool:
        """Settle this row with its deferred success, if it is awaiting one.

        Idempotent: a row that already fell back returns False rather than
        re-rendering, so callers need no `is_awaiting_deferred_result` guard of
        their own. Records that the fallback fired but keeps the output — see
        `deferred_success_output` for why a later sweep still needs to read it.

        Returns:
            True if the row was settled. False if it had no deferred outcome, has
                already settled, or is rejected/skipped so `set_success` would
                ignore it — in each case the caller should record its own terminal
                state.
        """
        output = self._deferred_success_output
        if output is None or self._deferred_success_settled:
            # Mirrors `is_awaiting_deferred_result`, spelled out so the type
            # checker can narrow `output` to `str`.
            return False
        if self._status in {"rejected", "skipped"}:
            return False
        # Before `set_success`, which re-renders synchronously. Nothing in that
        # render path reads this flag today (`_format_ask_user_output` derives
        # "no transcript" from the output value instead, so the suppression also
        # survives rehydration), but ordering the flag first keeps the object
        # consistent for anything the render does reach.
        self._deferred_success_settled = True
        self.set_success(output)
        return True

    def set_success(self, result: str = "") -> None:
        """Mark the tool call as successful.

        For long-running tools (`execute`, `task`) that actually ran (a start
        time was recorded via `set_running`), the elapsed run time is shown via
        `_show_timed_success_status`; every other case routes through
        `_show_success_status`.

        Args:
            result: Tool output/result to display
        """
        if self._status in {"rejected", "skipped"}:
            # A rejected tool (or one skipped due to a sibling rejection) never
            # legitimately becomes successful. A resumed turn can still stream a
            # synthetic ToolMessage for such a tool (see the reasoned-reject path
            # in `textual_adapter`); ignore it so the row keeps its terminal
            # rejected/skipped state instead of flipping.
            return
        elapsed = time.time() - self._start_time if self._start_time is not None else None
        self._stop_animation()
        self._status = "success"
        self._duration = (
            elapsed
            if self._tool_name in _TIMED_SUCCESS_TOOLS and elapsed is not None
            else None
        )
        # Strip redundant command success trailers — the UI already conveys
        # success. `ask_user` output is a user-authored Q&A transcript, though,
        # so text that resembles a command trailer must remain verbatim.
        self._output = (
            result
            if self._tool_name == "ask_user"
            else _strip_success_exit_line(result)
        )
        self._apply_status_class("success")
        if self._duration is not None:
            self._show_timed_success_status(self._duration)
        else:
            self._show_success_status()
        self._update_output_display()

    def _show_timed_success_status(self, duration: float) -> None:
        """Render the preserved duration for a completed timed tool call.

        The run time now sits on the header line (``✓ Execute  ...  · 4s``),
        so the separate status line is hidden.

        Args:
            duration: Elapsed tool run time in seconds.
        """
        self._duration = duration
        if self._status_widget is not None:
            self._status_widget.remove_class("pending")
            self._status_widget.display = False
        self._refresh_header()

    def _show_success_status(self) -> None:
        """Render the status marker for a completed successful call.

        The green check on the header line is the outcome indicator, so a
        completed call (e.g. `edit_file`) is never left without one and the
        separate status line stays hidden.
        """
        if self._status_widget is not None:
            self._status_widget.remove_class("pending")
            self._status_widget.remove_class("success")
            self._status_widget.display = False
        self._refresh_header()

    def set_error(self, error: str) -> None:
        """Mark the tool call as failed.

        Args:
            error: Error message
        """
        if self._status in {"rejected", "skipped"}:
            # A rejected/skipped tool never legitimately errors. A resumed turn
            # can stream a synthetic error ToolMessage for a reasoned-reject tool
            # (see `textual_adapter`); ignore it so the row keeps its rejected
            # state rather than flipping to "Error" (which also left the stale
            # `rejected` CSS class alongside `error`).
            return
        if self.settle_deferred_success():
            # A teardown sweep imposing a generic failure on a row that already
            # succeeded (an answered `ask_user` awaiting its transcript). The
            # authoritative `ToolMessage` calls `clear_deferred_success` first, so
            # a *real* tool error still lands below. `settle_deferred_success` is
            # idempotent, so the redirect fires once: a row that already fell back
            # keeps no immunity against a later genuine error.
            #
            # INFO, not DEBUG: turning a failure into a success is the single
            # highest-stakes decision on this path, and the always-on debug ring
            # buffer that backs the in-app console only captures INFO and above.
            logger.info(
                "Suppressed error on tool row with a deferred success: %s", error
            )
            return
        self._stop_animation()
        self._status = "error"
        self._apply_status_class("error")
        # For shell commands, prepend the full command so users can see what
        # failed -- unless the header already shows it in full (single line,
        # not truncated), in which case the echo would just repeat it.
        command = self._args.get("command") if self._tool_name == "execute" else None
        if command and isinstance(command, str) and command.strip():
            shown = format_tool_parts(self._tool_name, self._args).detail
            if shown == command.strip():
                self._output = error
            else:
                self._output = f"$ {command}\n\n{error}"
        else:
            self._output = error
        if self._status_widget:
            # The red cross and red tool name on the header carry the outcome.
            self._status_widget.remove_class("pending")
            self._status_widget.add_class("error")
            self._status_widget.display = False
        self._refresh_header()
        # Always show full error - errors should be visible
        self._expanded = True
        self._update_output_display()

    def set_rejected(self, *, reason: str | None = None) -> None:
        """Mark the tool call as rejected by user.

        Args:
            reason: Optional free-text reason supplied via the HITL reject
                widget; rendered as a dim line beneath the status.
        """
        if self.settle_deferred_success():
            # A turn-cancel sweep rejecting every tracked row; an answered
            # `ask_user` among them still succeeded, so it keeps its own outcome.
            # (Interrupt rejections leave these rows tracked instead — see
            # `_pop_rows_not_awaiting_deferred_result`.) INFO for the same reason
            # as the redirect in `set_error`.
            logger.info(
                "Suppressed rejection on tool row with a deferred success: %s", reason
            )
            return
        self._stop_animation()
        self._status = "rejected"
        self._apply_status_class("rejected")
        if reason and reason.strip():
            self._reject_reason = reason.strip()
        self._show_terminal_label("Rejected", "rejected")
        self._refresh_header()
        self._update_reject_reason_display()

    def _update_reject_reason_display(self) -> None:
        """Render the rejection reason, folded onto the ``Rejected`` line.

        ``Rejected · not during business hours`` reads as one decision, so the
        reason joins the status line instead of taking a row of its own.
        """
        if self._reject_reason_widget is None:
            return
        self._reject_reason_widget.display = False
        if self._reject_reason and self._status_widget is not None:
            sep = get_glyphs().separator
            self._status_widget.update(
                Content.assemble(
                    ("Rejected", "$warning"),
                    (f" {sep} {self._reject_reason}", "dim"),
                )
            )
            self._status_widget.display = True

    def set_skipped(self) -> None:
        """Mark the tool call as skipped (due to another rejection)."""
        self._stop_animation()
        self._status = "skipped"
        self._apply_status_class("skipped")
        self._show_terminal_label("Skipped", "rejected")  # Same styling as rejected
        self._refresh_header()

    def set_awaiting_approval(self) -> None:
        """Hide the tool call while an approval prompt mirrors its content.

        Used to avoid showing the same shell command in both the streamed tool
        call header and the HITL approval dialog at the same time. The widget
        is restored via `clear_awaiting_approval` once the user decides.
        """
        self._awaiting_approval = True
        self.display = False
        self._sync_approval_accessories()

    def clear_awaiting_approval(self) -> None:
        """Restore the tool call after `set_awaiting_approval`.

        No-op if `set_awaiting_approval` was not previously called, so the
        method is safe to call unconditionally from a `finally` block.
        """
        if not self._awaiting_approval:
            return
        self._awaiting_approval = False
        self.display = True
        self._sync_approval_accessories()

    def _register_visibility_accessories(self, *accessories: Widget) -> None:
        """Link transcript decorations whose visibility follows this tool.

        Idempotent: `Widget` uses identity equality, so re-registering the same
        accessory (a regroup folding an already-folded tool) cannot double-add.
        """
        for accessory in accessories:
            if accessory not in self._visibility_accessories:
                self._visibility_accessories.append(accessory)
        self._sync_approval_accessories()

    def _sync_approval_accessories(self) -> None:
        """Mirror this row's approval hiding onto its linked decorations.

        Drives both directions, so it is safe to call unconditionally from
        `set_awaiting_approval` and `clear_awaiting_approval`. Uses `set_class`
        rather than `display` so an accessory keeps its own visibility class
        (e.g. the `/timestamps` preference) and returns to it afterwards.
        """
        for accessory in self._visibility_accessories:
            accessory.set_class(
                self._awaiting_approval,
                _TOOL_AWAITING_APPROVAL_ACCESSORY_CLASS,
            )

    def _has_diff_preview(self) -> bool:
        """Return whether this row is a file diff tool with renderable diff lines."""
        return (
            self._tool_name in _FILE_DIFF_TOOLS
            and self._status not in {"error", "rejected", "skipped"}
            and bool(self._build_file_diff_lines()[0])
        )

    def toggle_output(self) -> None:
        """Toggle expansion of the tool's preview/full output."""
        if not self._output and not self._has_diff_preview():
            return
        # No-op in both directions when nothing is hidden: the collapsed and
        # expanded forms are identical, so toggling only flickers the hint.
        # This also covers force-expanded errors (see `set_error`).
        if not self._has_expandable_output():
            return
        self._expanded = not self._expanded
        self._update_output_display()

    def toggle_args(self) -> None:
        """Toggle display of collapsed tool arguments."""
        if not self.has_expandable_args:
            return
        self._args_expanded = not self._args_expanded
        self._update_args_display()

    def toggle_task_desc(self) -> None:
        """Toggle between the truncated and full `task` description."""
        if not self.has_expandable_task_desc:
            return
        self._task_desc_expanded = not self._task_desc_expanded
        self._update_task_desc_display()

    def on_click(self, event: Click) -> None:
        """Toggle output/argument/description expansion.

        A click on the header/args region (the truncated command or code line
        and its hint) toggles the collapsible args/code block directly, so an
        `execute` command or `js_eval` program can be expanded even when the
        output below it is *also* expandable. A `task` row routes clicks on its
        description region to the description toggle for the same reason.
        Otherwise prefer toggling output, falling through to the args/code block
        only when the output can't expand — `js_eval` commonly has a short,
        unexpandable result sitting below a multi-line, collapsible code block,
        and the old "output wins whenever it exists" rule left that code block
        stuck.
        """
        event.stop()  # Prevent click from bubbling up and scrolling
        if self.has_expandable_task_desc and self._click_targets_task_desc_region(
            event.widget
        ):
            self.toggle_task_desc()
        elif self.has_expandable_args and self._click_targets_args_region(event.widget):
            self.toggle_args()
        elif (self._output or self._has_diff_preview()) and self.has_expandable_output:
            self.toggle_output()
        elif self.has_expandable_args:
            self.toggle_args()
        elif self.has_expandable_task_desc:
            self.toggle_task_desc()

    def _click_targets_args_region(self, widget: object) -> bool:
        """Whether a click landed on the header/args block (not the output).

        Walks up from the clicked widget to `self`, matching the cached
        header, collapsed-args, and args-hint widgets. The walk is bounded so a
        mock or detached node (which never reaches `self` via `.parent`) returns
        `False` instead of looping, preserving the generic "prefer output"
        routing for those cases.

        Returns:
            `True` if the click landed on the header/args region.
        """
        targets = tuple(
            target
            for target in (
                self._header_widget,
                self._args_widget,
                self._args_hint_widget,
            )
            if target is not None
        )
        if not targets:
            # A click can only arrive post-mount, where these refs are always
            # cached, so an empty tuple means a regression nulled them out. Log
            # it rather than silently routing every click to output (mirrors
            # `_update_args_display`).
            logger.debug("_click_targets_args_region: header/args refs not cached")
            return False
        # The header/args/hint widgets are direct children of `self` (a click on
        # rendered text reports a descendant, so the real match depth is 0-1).
        # 8 is generous headroom that also bounds the walk for a detached or mock
        # node, whose `.parent` chain never reaches `self`.
        node = widget
        for _ in range(8):
            if node is None or node is self:
                return False
            if any(node is target for target in targets):
                return True
            node = getattr(node, "parent", None)
        return False

    def _click_targets_task_desc_region(self, widget: object) -> bool:
        """Whether a click landed on the `task` header/description block.

        Mirrors `_click_targets_args_region` but matches the cached header,
        description, and description-hint widgets so a `task` row expands its
        description when clicked, even when its output below is also expandable.

        Returns:
            `True` if the click landed on the header/description region.
        """
        targets = tuple(
            target
            for target in (
                self._header_widget,
                self._task_desc_widget,
                self._task_desc_hint_widget,
            )
            if target is not None
        )
        if not targets:
            logger.debug("_click_targets_task_desc_region: header/desc refs not cached")
            return False
        node = widget
        for _ in range(8):
            if node is None or node is self:
                return False
            if any(node is target for target in targets):
                return True
            node = getattr(node, "parent", None)
        return False

    def _format_output(
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Format tool output based on tool type for nicer display.

        Args:
            output: Raw output string
            is_preview: Whether this is for preview (truncated) display

        Returns:
            FormattedOutput with content and optional truncation info.
        """
        # Trim surrounding blank lines and trailing whitespace, but preserve the
        # command's own leading indentation on the first content line. A bare
        # `strip()` would lstrip the first line only — continuation lines keep
        # their indent — so output that indents every row (e.g. `git branch -r`,
        # which prefixes each branch with two spaces) renders with line 0 flush
        # and the rest indented beside the fixed glyph gutter.
        output = output.rstrip().lstrip("\n")
        if not output and not self._has_diff_preview():
            return FormattedOutput(content=Content(""))

        # Tool-specific formatting using dispatch table
        formatters = {
            "write_todos": self._format_todos_output,
            "ls": self._format_ls_output,
            "read_file": self._format_file_output,
            "view_file": self._format_file_output,
            "write_file": self._format_edit_file_output,
            "write_to_file": self._format_edit_file_output,
            "edit_file": self._format_edit_file_output,
            "replace_file_content": self._format_edit_file_output,
            "multi_replace_file_content": self._format_edit_file_output,
            "delete": self._format_edit_file_output,
            "delete_file": self._format_edit_file_output,
            "grep": self._format_search_output,
            "glob": self._format_search_output,
            "execute": self._format_shell_output,
            "web_search": self._format_web_output,
            "fetch_url": self._format_web_output,
            "task": self._format_task_output,
            "ask_user": self._format_ask_user_output,
        }

        formatter = formatters.get(self._tool_name)
        if formatter:
            return formatter(output, is_preview=is_preview)

        return self._format_generic_output(output, is_preview=is_preview)

    def _format_generic_output(
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Format output using generic size-based truncation.

        Used for tools with no dedicated formatter, and by a dedicated formatter
        that cannot parse its input and so must still cap an arbitrarily long
        body rather than dumping it into the collapsed row.

        Args:
            output: Tool output. `_format_output` has stripped trailing whitespace
                and leading newlines, but deliberately preserves the first line's
                leading indentation — do not assume it is fully trimmed.
            is_preview: Whether to truncate for the collapsed row.

        Returns:
            FormattedOutput, carrying truncation info only when `is_preview` and
                the body exceeds the line or character threshold.
        """
        if is_preview:
            lines = output.split("\n")
            if len(lines) > self._PREVIEW_LINES:
                return self._format_lines_output(lines, is_preview=True)
            if len(output) > self._PREVIEW_CHARS:
                truncated = output[: self._PREVIEW_CHARS]
                truncation = f"{len(output) - self._PREVIEW_CHARS} more chars"
                return FormattedOutput(
                    content=Content(truncated), truncation=truncation
                )

        # Default: plain text (Content treats input as literal)
        return FormattedOutput(content=Content(output))

    @property
    def has_expandable_output(self) -> bool:
        """Whether collapsed output has hidden content worth a toggle.

        Public wrapper around `_has_expandable_output` so toggle routing (click
        and Ctrl+O) can tell "has output" apart from "has output that can
        actually expand/collapse". `js_eval` results are frequently short and
        unexpandable while the code block above them *is* collapsible, so the
        routing must fall through to args when output cannot toggle.
        """
        return self._has_expandable_output()

    def _is_search_no_result_output(self, output: str) -> bool:
        """Return whether search output is a terminal no-result message."""
        if self._tool_name == "grep":
            return output.strip() == "No matches found"
        if self._tool_name == "glob":
            return output.strip() == "No files found"
        return False

    def _has_expandable_output(self) -> bool:
        """Return whether collapsed output has hidden content to expand."""
        output = self._output.strip()
        if (not output and not self._has_diff_preview()) or self._is_search_no_result_output(output):
            return False

        if self._tool_name in _COLLAPSE_OUTPUT_BY_DEFAULT and self._status != "error":
            formatted = self._format_output(output, is_preview=False)
            return bool(formatted.content.plain.strip())

        # See `_ALWAYS_PREVIEW_TOOLS`: the formatter decides whether these have
        # anything left to reveal, rather than the raw size thresholds below.
        # (A formatter that cannot parse its input may delegate back to them.)
        if self._tool_name in _ALWAYS_PREVIEW_TOOLS:
            return self._format_output(output, is_preview=True).truncation is not None

        lines = output.split("\n")
        if len(lines) > self._PREVIEW_LINES or len(output) > self._PREVIEW_CHARS:
            return self._format_output(output, is_preview=True).truncation is not None

        return False

    def _format_todos_output(
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Format write_todos output as a checklist.

        Returns:
            FormattedOutput with checklist content and optional truncation info.
        """
        items = self._parse_todo_items(output)
        if items is None:
            return FormattedOutput(content=Content(output))

        if not items:
            return FormattedOutput(content=Content.styled("No todos", "dim"))

        lines: list[Content] = []
        max_items = 4 if is_preview else len(items)

        # Build stats header
        stats = self._build_todo_stats(items)
        if stats:
            lines.extend([stats, Content("")])

        # Format each item
        lines.extend(
            self._format_single_todo(item, is_preview=is_preview)
            for item in items[:max_items]
        )

        truncation = None
        if is_preview:
            hidden_items = len(items) - max_items
            if hidden_items > 0:
                truncation = f"{hidden_items} more"
            elif any(
                len(self._todo_text(item)) > _MAX_TODO_CONTENT_LEN
                for item in items[:max_items]
            ):
                truncation = "full todo text"

        return FormattedOutput(content=Content("\n").join(lines), truncation=truncation)

    @staticmethod
    def _todo_text(item: dict | str) -> str:
        """Return display text for a todo item.

        Args:
            item: Todo item dictionary or plain string.

        Returns:
            Todo content text.
        """
        if isinstance(item, dict):
            return str(item.get("content", str(item)))
        return item

    def _parse_todo_items(self, output: str) -> list | None:  # noqa: PLR6301  # Grouped as method for widget cohesion
        """Parse todo items from output.

        Returns:
            List of todo items, or None if parsing fails.
        """
        list_match = re.search(r"\[(\{.*\})\]", output.replace("\n", " "), re.DOTALL)
        if list_match:
            try:
                return ast.literal_eval("[" + list_match.group(1) + "]")
            except (ValueError, SyntaxError):
                return None
        try:
            items = ast.literal_eval(output)
            return items if isinstance(items, list) else None
        except (ValueError, SyntaxError):
            return None

    def _build_todo_stats(self, items: list) -> Content:
        """Build stats content for todo list.

        Returns:
            Styled `Content` showing active, pending, and completed counts.
        """
        colors = theme.get_theme_colors(self)
        completed = sum(
            1 for i in items if isinstance(i, dict) and i.get("status") == "completed"
        )
        active = sum(
            1 for i in items if isinstance(i, dict) and i.get("status") == "in_progress"
        )
        pending = len(items) - completed - active

        parts: list[Content] = []
        if active:
            parts.append(Content.styled(f"{active} active", colors.warning))
        if pending:
            parts.append(Content.styled(f"{pending} pending", "dim"))
        if completed:
            parts.append(Content.styled(f"{completed} done", colors.success))
        return Content.styled(" | ", "dim").join(parts) if parts else Content("")

    def _todo_content_width(self, indent_width: int) -> int:
        """Return the todo content wrap width for the current widget size.

        Args:
            indent_width: Display width before todo content starts.

        Returns:
            Width available for todo content wrapping.
        """
        display_width = 0
        for widget in (self._full_widget, self._preview_widget, self):
            if widget and widget.is_mounted and widget.size.width > 0:
                display_width = widget.size.width
                break

        if not display_width:
            try:
                display_width = self.app.size.width
            except NoActiveAppError:
                display_width = _DEFAULT_TODO_WRAP_WIDTH

        # The content widgets measured above live inside the gutter row, so
        # their width already excludes the output glyph column; the guard
        # columns absorb the gutter offset for the self/app fallback width.
        available = display_width - indent_width - _TODO_WRAP_GUARD_COLUMNS
        return max(20, available)

    def _format_todo_line(
        self,
        prefix: Content,
        text: str,
        *,
        is_preview: bool,
        text_style: str | None = None,
    ) -> Content:
        """Format a todo row, wrapping expanded content under the text column.

        Args:
            prefix: Styled status prefix before todo content.
            text: Todo text to render.
            is_preview: Whether the compact preview is being rendered.
            text_style: Optional style for todo content.

        Returns:
            Styled `Content` for one todo row.
        """
        if is_preview and len(text) > _MAX_TODO_CONTENT_LEN:
            text = text[: _MAX_TODO_CONTENT_LEN - 3] + "..."

        if is_preview:
            content = Content.styled(text, text_style) if text_style else Content(text)
            return Content.assemble(prefix, content)

        indent = " " * len(prefix.plain)
        wrapped = textwrap.wrap(
            text,
            width=self._todo_content_width(len(prefix.plain)),
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        parts: list[Content] = [prefix]
        for index, line in enumerate(wrapped):
            if index:
                parts.append(Content("\n" + indent))
            content = Content.styled(line, text_style) if text_style else Content(line)
            parts.append(content)
        return Content.assemble(*parts)

    def _format_single_todo(self, item: dict | str, *, is_preview: bool) -> Content:
        """Format a single todo item.

        Args:
            item: Todo item dictionary or plain string.
            is_preview: Whether the compact preview is being rendered.

        Returns:
            Styled `Content` with checkbox and status styling.
        """
        colors = theme.get_theme_colors(self)
        if isinstance(item, dict):
            text = self._todo_text(item)
            status = item.get("status", "pending")
        else:
            text = self._todo_text(item)
            status = "pending"

        glyphs = get_glyphs()
        if status == "completed":
            return self._format_todo_line(
                Content.styled(f"{glyphs.checkmark} done   ", colors.success),
                text,
                is_preview=is_preview,
                text_style="dim",
            )
        if status == "in_progress":
            return self._format_todo_line(
                Content.styled(f"{glyphs.circle_filled} active ", colors.warning),
                text,
                is_preview=is_preview,
            )
        return self._format_todo_line(
            Content.styled(f"{glyphs.circle_empty} todo   ", "dim"),
            text,
            is_preview=is_preview,
        )

    def _format_ls_output(  # noqa: PLR6301  # Grouped as method for widget cohesion
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Format ls output as a clean directory listing.

        Returns:
            FormattedOutput with directory listing and optional truncation info.
        """
        # Try to parse as a Python list (common format)
        try:
            items = ast.literal_eval(output)
            if isinstance(items, list):
                lines: list[Content] = []
                max_items = 5 if is_preview else len(items)
                for item in items[:max_items]:
                    path = Path(str(item))
                    name = path.name
                    if path.suffix in {".py", ".pyx"}:
                        lines.append(Content.styled(name, "blue"))
                    elif path.suffix in {".json", ".yaml", ".yml", ".toml"}:
                        lines.append(Content.styled(name, "yellow"))
                    elif not path.suffix:
                        lines.append(Content.styled(f"{name}/", "bold cyan"))
                    else:
                        lines.append(Content(name))

                truncation = None
                if is_preview and len(items) > max_items:
                    truncation = f"{len(items) - max_items} more"

                return FormattedOutput(
                    content=Content("\n").join(lines), truncation=truncation
                )
        except (ValueError, SyntaxError):
            pass

        # Fallback: plain text
        return FormattedOutput(content=Content(output))

    @staticmethod
    def _compact_line_gutter(output: str) -> str:
        r"""Tighten `read_file`'s line-number gutter for display.

        `read_file` prefixes each row with a right-justified line marker — `N`,
        or `N.M` for a wrapped-line continuation — then two spaces, then the
        original source content. The model needs the raw gutter for edits, but the
        TUI re-justifies markers to the widest marker actually present, then two
        spaces, mirroring how grep/glob results sit flush left. Source
        indentation after the gutter is preserved untouched.

        The gutter shape is `_READ_FILE_GUTTER_RE`. Lines that don't match a
        gutter shape (e.g. test fixtures or non-numbered output) are passed
        through unchanged.

        Returns:
            The output with compacted gutters, or the original string if no
                line-numbered content was found.
        """
        lines = output.split("\n")
        parsed: list[tuple[str, str] | None] = []
        width = 0
        for line in lines:
            match = _READ_FILE_GUTTER_RE.match(line)
            if match:
                marker, source = match.groups()
                parsed.append((marker, source))
                width = max(width, len(marker))
            else:
                parsed.append(None)

        if width == 0:
            return output

        compacted: list[str] = []
        for line, row in zip(lines, parsed, strict=True):
            if row is None:
                compacted.append(line)
            else:
                marker, source = row
                compacted.append(f"{marker:>{width}}  {source}")
        return "\n".join(compacted)

    def _format_edit_file_output(
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Render file edit/write output as a line-numbered, background-tinted diff block."""
        if self._status != "error":
            diff_lines, _added, _removed = self._build_file_diff_lines()
            if diff_lines:
                width = 88
                if self.is_attached and self.size.width > 16:
                    width = max(40, self.size.width - 8)
                max_preview_lines = 33
                content, remaining = render_diff_content(
                    "\n".join(diff_lines),
                    max_lines=max_preview_lines if is_preview else None,
                    show_stats=False,
                    expand_hint="",
                    line_width=width,
                )
                truncation = f"{remaining} more lines" if (is_preview and remaining > 0) else None
                return FormattedOutput(content=content, truncation=truncation)
            if self._status == "success" and is_preview:
                return FormattedOutput(content=Content(""))
        return self._format_file_output(output, is_preview=is_preview)

    def _format_file_output(
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Format file read/write output.

        Preview mode caps both line count and total characters so that files
        with very long lines (minified HTML/JS/CSS) don't wrap and overflow
        the widget.

        Returns:
            FormattedOutput with file content and optional truncation info.
        """
        output = self._compact_line_gutter(output)
        lines = output.split("\n")
        # Files conventionally end in "\n"; the trailing empty element isn't a
        # real line and would inflate truncation counts.
        had_trailing_newline = bool(lines) and not lines[-1]
        if had_trailing_newline:
            lines = lines[:-1]
        max_lines = 4 if is_preview else len(lines)
        char_budget = self._PREVIEW_CHARS if is_preview else None

        shown, chars_used, char_truncated = self._truncate_to_budget(
            lines, max_lines=max_lines, char_budget=char_budget
        )
        parts = [Content(line) for line in shown]
        content = Content("\n").join(parts) if parts else Content("")

        truncation = self._build_truncation_hint(
            output=output,
            lines=lines,
            parts_count=len(parts),
            chars_used=chars_used,
            char_truncated=char_truncated,
            had_trailing_newline=had_trailing_newline,
            is_preview=is_preview,
        )

        return FormattedOutput(content=content, truncation=truncation)

    @staticmethod
    def _truncate_to_budget(
        lines: list[str], *, max_lines: int, char_budget: int | None
    ) -> tuple[list[str], int, bool]:
        """Apply line- and character-count caps to a list of display lines.

        Shared by the file, shell, and search formatters so preview truncation
        stays identical across tool outputs. When `char_budget` is `None` (the
        expanded, non-preview view) only the line cap applies.

        Args:
            lines: Candidate display lines, already cleaned by the caller.
            max_lines: Maximum number of lines to emit.
            char_budget: Maximum characters to emit across all lines, counting
                the newline separators between them, or `None` for no cap.

        Returns:
            The lines to show, the characters consumed (including separators),
            and whether the character budget forced truncation.
        """
        shown: list[str] = []
        chars_used = 0
        char_truncated = False
        for line in lines[:max_lines]:
            display_line = line
            if char_budget is not None:
                separator_cost = 1 if shown else 0
                remaining = char_budget - chars_used - separator_cost
                if remaining <= 0:
                    char_truncated = True
                    break
                if len(line) > remaining:
                    display_line = line[:remaining]
                    char_truncated = True
                chars_used += separator_cost + len(display_line)
            shown.append(display_line)
            if char_truncated:
                break
        return shown, chars_used, char_truncated

    @staticmethod
    def _build_truncation_hint(
        *,
        output: str,
        lines: list[str],
        parts_count: int,
        chars_used: int,
        char_truncated: bool,
        had_trailing_newline: bool,
        is_preview: bool,
        line_unit: Literal["files", "lines"] = "lines",
    ) -> str | None:
        """Compose the truncation hint, preferring line counts over char counts.

        When both the line cap and the char cap were hit, hidden-line count is
        the more useful signal for the user — char counts dominate the hint
        for big files where what they really want to know is "how many more
        lines am I missing?". `line_unit` names the hidden-row noun ("lines"
        for text output, "files" for glob path lists).

        Returns:
            Hint string for the UI, or `None` if nothing was truncated.
        """
        if not is_preview:
            return None
        hidden_lines = len(lines) - parts_count
        if hidden_lines > 0:
            return f"{hidden_lines} more {line_unit}"
        if char_truncated:
            effective_output_len = len(output) - (1 if had_trailing_newline else 0)
            hidden_chars = effective_output_len - chars_used
            return f"{hidden_chars} more chars"
        return None

    def _format_search_output(
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Format grep/glob search output.

        Returns:
            FormattedOutput with search results and optional truncation info.
        """
        # Try to parse as a Python list (glob returns list of paths). The
        # except is scoped to detection only — formatting runs outside it so a
        # bug in `_format_search_lines` can't silently reroute to the fallback.
        try:
            items = ast.literal_eval(output.strip())
        except (ValueError, SyntaxError):
            items = None

        if isinstance(items, list):
            paths: list[str] = []
            for item in items:
                path = Path(str(item))
                try:
                    display = str(path.relative_to(Path.cwd()))
                except ValueError:
                    display = path.name
                paths.append(display)
            return self._format_search_lines(
                paths, is_preview=is_preview, line_unit="files"
            )

        # Fallback: line-based output (grep results)
        lines = [
            raw_line.strip() for raw_line in output.split("\n") if raw_line.strip()
        ]
        return self._format_search_lines(
            lines, is_preview=is_preview, line_unit="lines"
        )

    def _format_search_lines(
        self,
        lines: list[str],
        *,
        is_preview: bool,
        line_unit: Literal["files", "lines"],
    ) -> FormattedOutput:
        """Format search result rows with line and character preview caps.

        `line_unit` names the hidden-row noun for the hint — "files" for glob
        path lists, "lines" for grep matches.

        Returns:
            FormattedOutput with search rows and optional truncation info.
        """
        # Search rows are denser than file/shell output, so the preview shows
        # one extra row (5) before truncating.
        max_lines = 5 if is_preview else len(lines)
        char_budget = self._PREVIEW_CHARS if is_preview else None

        shown, chars_used, char_truncated = self._truncate_to_budget(
            lines, max_lines=max_lines, char_budget=char_budget
        )
        parts = [Content(line) for line in shown]
        content = Content("\n").join(parts) if parts else Content("")

        # The cleaned `lines` carry no trailing-newline element, so the joined
        # length is the full preview-able content length.
        truncation = self._build_truncation_hint(
            output="\n".join(lines),
            lines=lines,
            parts_count=len(parts),
            chars_used=chars_used,
            char_truncated=char_truncated,
            had_trailing_newline=False,
            is_preview=is_preview,
            line_unit=line_unit,
        )

        return FormattedOutput(content=content, truncation=truncation)

    def _format_shell_output(
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Format shell command output with DevOps-aware highlighting.

        Returns:
            FormattedOutput with shell output and optional truncation info.
        """
        lines = output.split("\n")
        had_trailing_newline = bool(lines) and not lines[-1]
        if had_trailing_newline:
            lines = lines[:-1]
        max_lines = 4 if is_preview else len(lines)
        char_budget = self._PREVIEW_CHARS if is_preview else None

        shown, chars_used, char_truncated = self._truncate_to_budget(
            lines, max_lines=max_lines, char_budget=char_budget
        )

        command = (
            self._args.get("command")
            or self._args.get("cmd")
            or self._args.get("command_line")
            or self._args.get("CommandLine")
        )
        command_str = command.strip() if isinstance(command, str) else ""
        if not command_str and lines and lines[0].startswith("$ "):
            command_str = lines[0][2:].strip()

        from opscloud.ui.widgets.devops_renderers import detect_devops_renderer

        renderer = detect_devops_renderer(output, command=command_str)
        colors = (
            theme.get_theme_colors(self)
            if self.is_attached
            else theme.get_theme_colors(None)
        )

        # Dim the leading `$ command` echo; render subsequent lines with DevOps renderer if matched.
        parts: list[Content] = []
        for index, line in enumerate(shown):
            if index == 0 and line.startswith("$ "):
                parts.append(Content.styled(line, "dim"))
            elif renderer is not None:
                try:
                    parts.append(renderer.render_line(line, colors=colors))
                except Exception:
                    parts.append(Content(line))
            else:
                parts.append(Content(line))

        content = Content("\n").join(parts) if parts else Content("")

        truncation = self._build_truncation_hint(
            output=output,
            lines=lines,
            parts_count=len(parts),
            chars_used=chars_used,
            char_truncated=char_truncated,
            had_trailing_newline=had_trailing_newline,
            is_preview=is_preview,
        )

        return FormattedOutput(content=content, truncation=truncation)

    def _format_web_output(
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Format web_search/fetch_url output.

        Returns:
            FormattedOutput with web response and optional truncation info.
        """
        data = self._try_parse_web_data(output)
        if isinstance(data, dict):
            return self._format_web_dict(data, is_preview=is_preview)

        # Fallback: plain text
        return self._format_lines_output(output.split("\n"), is_preview=is_preview)

    @staticmethod
    def _try_parse_web_data(output: str) -> dict | None:
        """Try to parse web output as JSON or dict.

        Returns:
            Parsed dict if successful, None otherwise.
        """
        try:
            if output.strip().startswith("{"):
                return json.loads(output)
            return ast.literal_eval(output)
        except (ValueError, SyntaxError, json.JSONDecodeError):
            return None

    def _format_web_dict(self, data: dict, *, is_preview: bool) -> FormattedOutput:
        """Format a parsed web response dict.

        Returns:
            FormattedOutput with web response content and optional truncation info.
        """
        # Handle web_search results
        if "results" in data:
            return self._format_web_search_results(
                data.get("results", []), is_preview=is_preview
            )

        # Handle fetch_url response
        if "markdown_content" in data:
            lines = data["markdown_content"].split("\n")
            return self._format_lines_output(lines, is_preview=is_preview)

        # Generic dict - show key fields
        parts: list[Content] = []
        max_keys = 3 if is_preview else len(data)
        for k, v in list(data.items())[:max_keys]:
            v_str = str(v)
            if is_preview and len(v_str) > _MAX_WEB_CONTENT_LEN:
                v_str = v_str[:_MAX_WEB_CONTENT_LEN] + "..."
            parts.append(Content(f"  {k}: {v_str}"))
        truncation = None
        if is_preview and len(data) > max_keys:
            truncation = f"{len(data) - max_keys} more"
        return FormattedOutput(
            content=Content("\n").join(parts) if parts else Content(""),
            truncation=truncation,
        )

    def _format_web_search_results(  # noqa: PLR6301  # Grouped as method for widget cohesion
        self, results: list, *, is_preview: bool
    ) -> FormattedOutput:
        """Format web search results.

        Returns:
            FormattedOutput with search results and optional truncation info.
        """
        if not results:
            return FormattedOutput(content=Content.styled("No results", "dim"))
        parts: list[Content] = []
        max_results = 3 if is_preview else len(results)
        for r in results[:max_results]:
            title = r.get("title", "")
            url = r.get("url", "")
            parts.extend(
                [
                    Content.styled(f"  {title}", "bold"),
                    Content.styled(f"  {url}", "dim"),
                ]
            )
        truncation = None
        if is_preview and len(results) > max_results:
            truncation = f"{len(results) - max_results} more results"
        return FormattedOutput(content=Content("\n").join(parts), truncation=truncation)

    def _format_lines_output(  # noqa: PLR6301  # Grouped as method for widget cohesion
        self, lines: list[str], *, is_preview: bool
    ) -> FormattedOutput:
        """Format a list of lines with optional preview truncation.

        Returns:
            FormattedOutput with lines content and optional truncation info.
        """
        max_lines = 4 if is_preview else len(lines)
        parts = [Content(line) for line in lines[:max_lines]]
        content = Content("\n").join(parts) if parts else Content("")
        truncation = None
        if is_preview and len(lines) > max_lines:
            truncation = f"{len(lines) - max_lines} more lines"
        return FormattedOutput(content=content, truncation=truncation)

    def _format_task_output(  # noqa: PLR6301  # Grouped as method for widget cohesion
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Format task (subagent) output.

        Returns:
            FormattedOutput with task output and optional truncation info.
        """
        lines = output.split("\n")
        max_lines = 4 if is_preview else len(lines)

        parts = [Content(line) for line in lines[:max_lines]]
        content = Content("\n").join(parts) if parts else Content("")

        truncation = None
        if is_preview and len(lines) > max_lines:
            truncation = f"{len(lines) - max_lines} more lines"

        return FormattedOutput(content=content, truncation=truncation)

    def _ask_user_question_count(self) -> int:
        """Return the number of valid question objects in this tool call.

        The count comes from the structured tool arguments rather than parsing
        the free-form transcript. This keeps arbitrary answer text opaque while
        still supporting the collapsed `N answers` affordance.

        Returns:
            The question count, or zero unless `questions` is a non-empty list of
                dicts each carrying non-blank `question` text. Deliberately looser
                than `ask_user._validate_questions` — it accepts payloads that
                rejects, such as an unknown `type` or a `choices`/`type` mismatch —
                because it only needs to guard the fields the count reads. Of the
                three paths that populate `_args`, only the `ask_user` interrupt
                (validated in `textual_adapter` via `ask_user_adapter`) is checked;
                the streamed tool call and the persisted store
                (`message_store.to_widget`) are not, so malformed shapes do reach
                here and must degrade rather than raise.
        """
        questions = self._args.get("questions")
        if not isinstance(questions, list) or not questions:
            return 0
        if not all(
            isinstance(question, dict)
            and isinstance(question.get("question"), str)
            and bool(question["question"].strip())
            for question in questions
        ):
            return 0
        return len(questions)

    def _format_ask_user_output(
        self, output: str, *, is_preview: bool = False
    ) -> FormattedOutput:
        """Format an `ask_user` result for the collapsed or expanded row.

        The inline question widget is unmounted once answered, so this row is the
        only place the answers stay visible in the live session — the thread's
        own `ToolMessage` is what a reload re-renders from. Collapsed, the row
        keeps a one-line summary; expanded, it shows what was sent back.

        The summary is derived from the recorded status, never from the answer
        text (the question count only labels the expand affordance). The
        placeholders are in-band, so a user who types `(cancelled)` or
        `(error: ...)` must not have their answer read as control state. The cost
        is that a cancelled prompt resumed by a non-TUI client — which `ask_user`
        records as `status="success"` with `(cancelled)` placeholders — reads as
        answered until expanded.

        Returns:
            FormattedOutput with the status-derived summary when `is_preview`, or
                the output rendered literally when expanded. A row holding only a
                fallback summary advertises no expansion. Falls back to generic
                formatting when the structured question args are unavailable.
        """
        question_count = self._ask_user_question_count()
        if question_count == 0:
            # Route through the generic path rather than returning the body bare:
            # `ask_user` is in `_ALWAYS_PREVIEW_TOOLS`, so the size thresholds in
            # `_has_expandable_output`/`_update_output_display` no longer gate it
            # and an arbitrarily long body would otherwise fill the collapsed row
            # with no expand affordance. `_format_generic_output` reapplies them.
            if not self._ask_user_args_warned:
                # Once per widget: this runs on every re-render, and the
                # condition cannot change without a new `_args`.
                self._ask_user_args_warned = True
                logger.warning(
                    "ask_user row has no usable `questions` args (got %r); the "
                    "collapsed row will show the transcript instead of a summary",
                    self._args.get("questions"),
                )
            return self._format_generic_output(output, is_preview=is_preview)

        if output in _ASK_USER_ROW_SUMMARIES:
            # No authoritative ToolMessage arrived, so this row holds only the
            # fallback summary. There is no transcript for expansion to reveal;
            # advertising the question count would create a dead affordance.
            return FormattedOutput(content=Content.styled(output, "dim"))

        if not is_preview:
            return FormattedOutput(content=Content(output))

        if self._status == "error":
            # The transcript holds `(error: ...)` placeholders, not answers, so
            # count the questions instead of promising answers.
            summary = ASK_USER_FAILED_SUMMARY
            noun = "question" if question_count == 1 else "questions"
        else:
            summary = ASK_USER_ANSWERED_SUMMARY
            noun = "answer" if question_count == 1 else "answers"
        return FormattedOutput(
            content=Content.styled(summary, "dim"),
            truncation=f"{question_count} {noun}",
        )

    def _update_output_display(self) -> None:
        """Update the output display based on expanded state."""
        # Guard: all widgets must be initialized before updating display state
        if (
            (not self._output and not self._has_diff_preview())
            or not self._preview_widget
            or not self._preview_row
            or not self._full_widget
            or not self._full_row
            or not self._hint_widget
        ):
            return

        output_stripped = self._output.strip()
        lines = output_stripped.split("\n")
        total_lines = len(lines)
        total_chars = len(output_stripped)

        # Truncate if too many lines OR too many characters
        needs_truncation = (
            total_lines > self._PREVIEW_LINES or total_chars > self._PREVIEW_CHARS
        )

        full = self._format_output(self._output, is_preview=False)
        if not full.content.plain.strip():
            self._preview_row.display = False
            self._full_row.display = False
            self._hint_widget.display = False
            return

        if self._expanded:
            # Show full output with formatting
            self._preview_row.display = False
            self._full_widget.update(full.content)
            self._full_row.display = True
            if self._has_expandable_output():
                self._hint_widget.update(
                    Content.styled(
                        f"{self._output_hint_keys()} to collapse", "dim"
                    )
                )
                self._hint_widget.display = True
            else:
                self._hint_widget.display = False
        else:
            # Show collapsed preview
            self._full_row.display = False
            if not self._is_search_no_result_output(self._output) and (
                self._tool_name in _COLLAPSE_OUTPUT_BY_DEFAULT
            ):
                sep = get_glyphs().separator
                summary = format_tool_result_summary(self._tool_name, self._output)
                summary = summary.removeprefix("⎿").strip()
                self._preview_widget.update(
                    Content.assemble(
                        (summary, "dim") if summary else "",
                        (f" {sep} " if summary else "", "dim"),
                        (f"{self._output_hint_keys()} to expand", "dim"),
                    )
                )
                self._preview_row.display = True
                self._hint_widget.display = False
                return
            # Truncate the preview only when the output is large enough to
            # warrant it; `_ALWAYS_PREVIEW_TOOLS` use their compact preview
            # regardless of size.
            is_preview = needs_truncation or self._tool_name in _ALWAYS_PREVIEW_TOOLS
            result = self._format_output(self._output, is_preview=is_preview)
            self._preview_widget.update(result.content)
            self._preview_row.display = True

            # Offer expansion only when the formatter actually hid content.
            if result.truncation:
                ellipsis = get_glyphs().ellipsis
                sep = get_glyphs().separator
                self._hint_widget.update(
                    Content.styled(
                        f"{ellipsis} {result.truncation} {sep} "
                        f"{self._output_hint_keys()} to expand",
                        "dim",
                    )
                )
                self._hint_widget.display = True
            else:
                self._hint_widget.display = False

    def _output_hint_keys(self) -> str:
        """Affordances to advertise in the output expand/collapse hint.

        Returns:
            `"click"` when an expandable command/code block or `task`
                description owns Ctrl+O, otherwise `"click or Ctrl+O"`.
        """
        if self.has_expandable_args or self.has_expandable_task_desc:
            return "click"
        return "click or Ctrl+O"

    @property
    def has_output(self) -> bool:
        """Check if this tool message has output to display.

        Returns:
            True if there is output content, False otherwise.
        """
        return bool(self._output) or self._has_diff_preview()

    @property
    def tool_name(self) -> str:
        """Public read-only accessor for the underlying tool name."""
        return self._tool_name

    @property
    def args(self) -> dict[str, Any]:
        """Public read-only accessor for the parsed tool-call arguments.

        Returns a shallow copy so a consumer (e.g. a hook payload built from
        `args`) cannot rebind the widget's top-level keys by reference. Nested
        mutable values are shared, not deep-copied, so callers must treat them as
        read-only and must not deep-mutate a returned nested value.
        """
        return dict(self._args)

    @property
    def is_success(self) -> bool:
        """Whether the tool completed successfully."""
        return self._status == "success"

    @property
    def is_failed(self) -> bool:
        """Whether the tool did not succeed and should stay visible.

        Covers errored, rejected, and skipped tools. `skipped` is included so a
        reject-cascade (one tool rejected, the rest skipped) keeps the skipped
        rows visible and out of the group's success count, matching how
        `_regroup_completed_tools` treats a hydrated transcript.
        """
        return self._status in {"error", "rejected", "skipped"}

    @property
    def is_pending(self) -> bool:
        """Whether the tool has not finished (awaiting approval or running)."""
        return self._status in {"pending", "running"}

    @property
    def has_expandable_args(self) -> bool:
        """Whether the tool's args are large enough to deserve a collapsible block.

        - `ask_user`: its `questions` payload is too noisy to render inline.
        - `execute` / `run_command`: the header truncates the shell command at
            `EXECUTE_HEADER_MAX_LENGTH`, so the full command is offered as a
            collapsible block when the command is longer than `EXECUTE_HEADER_MAX_LENGTH`.
        - `js_eval`, `run_script`, or script/code args: full source code block.
        """
        if self._tool_name in _FILE_DIFF_TOOLS:
            return False
        if self._tool_name == "ask_user":
            return bool(self._args)
        if self._tool_name in {"execute", "run_command"}:
            command = (
                self._args.get("command")
                or self._args.get("cmd")
                or self._args.get("command_line")
                or self._args.get("CommandLine")
            )
            if isinstance(command, str) and command.strip():
                return len(command.strip()) > EXECUTE_HEADER_MAX_LENGTH
        if (
            "code" in self._args
            or "script" in self._args
            or self._tool_name.endswith("run_script")
            or "run_script" in self._tool_name
        ):
            code = self._args.get("code") or self._args.get("script")
            if isinstance(code, str) and ("\n" in code or len(code) > 40):
                return True
        for val in self._args.values():
            if isinstance(val, str) and ("\n" in val or len(val) > 80):
                return True
        return False

    @property
    def has_expandable_task_desc(self) -> bool:
        """Whether the `task` description is long enough to be truncated.

        A `task` row renders its description on a dedicated dim line, truncated
        at `_TASK_DESC_MAX_LENGTH`. When the full description exceeds that, the
        truncated preview becomes expandable via click or Ctrl+O.
        """
        return len(self._task_description()) > self._TASK_DESC_MAX_LENGTH

    def _task_description(self) -> str:
        """Return the `task` call's description string, or empty when absent.

        A non-string `description` (schema-typed as a string) is coerced to
        `""` so downstream length/slice logic stays safe; the anomaly is logged.
        """
        if self._tool_name != "task":
            return ""
        desc = self._args.get("description", "")
        if isinstance(desc, str):
            return desc
        if desc is not None:
            logger.debug("task description is not a string: %r", type(desc))
        return ""

    def _task_desc_content(self) -> Content:
        """Render the `task` description, truncated unless expanded.

        Returns:
            Dim `Content`: the full description when expanded or when it already
            fits within `_TASK_DESC_MAX_LENGTH`; otherwise the preview truncated
            to that length (trailing whitespace trimmed) with a trailing
            ellipsis.
        """
        desc = self._task_description()
        if self._task_desc_expanded or len(desc) <= self._TASK_DESC_MAX_LENGTH:
            text = desc
        else:
            ellipsis = get_glyphs().ellipsis
            text = desc[: self._TASK_DESC_MAX_LENGTH].rstrip() + ellipsis
        return Content.styled(text, "dim")

    def _update_task_desc_display(self) -> None:
        """Update the truncated/expanded `task` description and its hint."""
        if self._task_desc_widget is None or self._task_desc_hint_widget is None:
            # Refs are legitimately None for non-`task` rows (never mounted). Log
            # only when a `task` row that carries a description is missing them,
            # so a regression that nulls them post-mount isn't a silent no-op.
            if self._task_description():
                logger.debug("_update_task_desc_display: task-desc refs not cached")
            return
        if not self._task_description():
            self._task_desc_widget.display = False
            self._task_desc_hint_widget.display = False
            return
        self._task_desc_widget.update(self._task_desc_content())
        self._task_desc_widget.display = True
        if not self.has_expandable_task_desc:
            self._task_desc_hint_widget.display = False
            return
        verb = "collapse" if self._task_desc_expanded else "expand"
        self._task_desc_hint_widget.update(
            Content.styled(f"click or Ctrl+O to {verb}", "dim italic")
        )
        self._task_desc_hint_widget.display = True

    def _format_code_detail(self) -> Content:
        """Render the `js_eval` program for the collapsible code block.

        The code is shown verbatim and left-aligned (its own indentation is the
        only indentation), as plain uncolored `Content`. Blank lines of
        top/bottom padding add breathing room between the `js_eval` header above
        and the "show/hide code" hint below.

        Returns:
            A plain `Content` renderable with a blank line of padding on
                top and bottom.
        """
        code = self._args.get("code") or self._args.get("script")
        code_str = code.strip("\n") if isinstance(code, str) else str(code)

        # Blank lines of top/bottom padding separate the block from the header
        # line above and the "show/hide code" hint below.
        return Content("\n").join((Content(""), Content(code_str), Content("")))

    def _format_command_detail(self) -> Content:
        """Render the full `execute` command for the collapsible block.

        The command is shown verbatim and left-aligned, as plain uncolored
        `Content`, mirroring `_format_code_detail`. Hidden/deceptive Unicode is
        rendered as visible markers so a truncated header can't conceal it.

        Returns:
            A plain `Content` renderable with a blank line of padding on
                top and bottom.
        """
        command = (
            self._args.get("command")
            or self._args.get("cmd")
            or self._args.get("command_line")
            or self._args.get("CommandLine")
        )
        command_str = command.strip("\n") if isinstance(command, str) else str(command)
        return Content("\n").join((Content(""), Content(command_str), Content("")))

    def _format_args_detail(self) -> Content:
        """Render tool arguments as an indented `Content` block.

        Renders JSON-pretty-printed args, falling back to `str(self._args)`
        (with a visible marker) when JSON serialization fails — `default=str`
        already handles most non-serializable values, so reaching the fallback
        indicates a deeper issue worth logging. `js_eval` code is handled
        separately by `_format_code_detail`.

        Returns:
            Indented `Content` containing JSON-pretty-printed arguments, or a
            marked fallback rendering on serialization failure.
        """
        try:
            text = json.dumps(self._args, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "ask_user args not JSON-serializable; using repr fallback: %r", exc
            )
            text = f"# (fallback rendering)\n{self._args!s}"
        lines = Content(text).split("\n")
        return Content("\n").join(Content.assemble("  ", line) for line in lines)

    def _update_args_display(self) -> None:
        """Update the collapsed/expanded argument display."""
        if self._args_widget is None or self._args_hint_widget is None:
            # Toggle invoked before on_mount cached the refs; log so a regression
            # that nulls them out post-mount doesn't appear as a silent no-op.
            logger.debug("_update_args_display called before widget refs are cached")
            return

        if not self.has_expandable_args:
            self._args_widget.display = False
            self._args_hint_widget.display = False
            return

        if (
            self._tool_name == "js_eval"
            or "code" in self._args
            or "script" in self._args
            or self._tool_name.endswith("run_script")
            or "run_script" in self._tool_name
        ):
            noun, detail_fn = "code", self._format_code_detail
        elif self._tool_name in {"execute", "run_command"}:
            noun, detail_fn = "command", self._format_command_detail
        else:
            noun, detail_fn = "arguments", self._format_args_detail
        if self._args_expanded:
            self._args_widget.update(detail_fn())
            self._args_widget.display = True
            self._args_hint_widget.update(
                Content.styled(f"click or Ctrl+O to hide {noun}", "dim italic")
            )
        else:
            self._args_widget.display = False
            self._args_hint_widget.update(
                Content.styled(f"click or Ctrl+O to show {noun}", "dim italic")
            )
        self._args_hint_widget.display = True

    def _filtered_args(self) -> dict[str, Any]:
        """Filter large tool args for display.

        Returns:
            Filtered args dict with only display-relevant keys for write/edit tools.
        """
        if self._tool_name not in _FILE_DIFF_TOOLS:
            return self._args

        filtered: dict[str, Any] = {}
        for key in ("file_path", "TargetFile", "path", "replace_all"):
            if key in self._args:
                filtered[key] = self._args[key]
        return filtered

