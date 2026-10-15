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


# Visual width of the prompt prefix (glyph + trailing space, e.g. "> ", "$ ").
# Glyphs are single characters, so the prefix is always two columns wide.
_PROMPT_PREFIX_WIDTH = 2


def _strip_prompt_prefix(
    result: tuple[str, str] | None,
    selection: Selection,
) -> tuple[str, str] | None:
    """Drop the leading prompt prefix glyph from a selected range.

    The prefix is only rendered on the first row, so it is stripped only when
    the selection begins there. This keeps triple-click / select-all copies to
    the message body instead of the decorative `"> "` (or mode glyph) prefix.

    Args:
        result: The `(text, ending)` tuple returned by `Static.get_selection`.
        selection: The active selection geometry.

    Returns:
        The selection with the prefix removed from row 0, or `result` unchanged.
    """
    if result is None:
        return None
    text, ending = result
    start = selection.start
    if start is not None and start.y != 0:
        return result
    start_x = 0 if start is None else start.x
    prefix_chars = max(0, _PROMPT_PREFIX_WIDTH - start_x)
    return text[prefix_chars:], ending


def _select_prompt_body(widget: Static) -> None:
    """Select the user message body without its decorative prompt glyph.

    Args:
        widget: User message widget whose body should be selected.
    """
    widget.screen.selections = {  # ty: ignore[invalid-assignment]  # Textual reactive descriptor assignment updates selection watchers; `set_reactive` would skip them.
        widget: Selection(Offset(_PROMPT_PREFIX_WIDTH, 0), None),
    }


def _will_collapse(text: str) -> bool:
    """Return whether `text` exceeds the transcript display threshold.

    Single source of truth for the threshold, shared by `_collapse_user_message`
    and `UserMessage.will_truncate` so the render decision and the expand
    affordance can never disagree.

    Args:
        text: Candidate body text.

    Returns:
        `True` when the body is long enough to collapse.
    """
    return len(text) > _USER_MSG_MAX_DISPLAY_CHARS


@dataclass
class _UserMessageFull:
    text: str

@dataclass
class _UserMessageCollapsed:
    head: str
    tail: str
    hidden_lines: int
    hidden_chars: int

    @property
    def text(self) -> str:
        return f"{self.head}\n\n[... hidden ...]\n\n{self.tail}"

_UserMessageDisplay = _UserMessageFull | _UserMessageCollapsed

def _collapse_user_message(text: str) -> _UserMessageDisplay:
    """Collapse a very long user message for transcript display.

    Keeps the first and last portions and elides the middle for rendering
    performance.

    Args:
        text: Full message content.

    Returns:
        `_UserMessageCollapsed` when the body exceeds the display threshold,
        otherwise `_UserMessageFull` carrying the original text.
    """
    if not _will_collapse(text):
        return _UserMessageFull(text=text)
    hidden_start = _USER_MSG_TRUNCATE_HEAD_CHARS
    hidden_end = len(text) - _USER_MSG_TRUNCATE_TAIL_CHARS
    return _UserMessageCollapsed(
        head=text[:_USER_MSG_TRUNCATE_HEAD_CHARS],
        tail=text[-_USER_MSG_TRUNCATE_TAIL_CHARS:],
        # Counted over a range rather than a slice so a multi-megabyte paste
        # does not allocate a copy of its own middle on every render.
        hidden_lines=text.count("\n", hidden_start, hidden_end),
        hidden_chars=hidden_end - hidden_start,
    )


def _truncate_for_display(text: str) -> str:
    """Truncate very long user message text for display in the conversation.

    Thin string-returning wrapper around `_collapse_user_message` for
    `QueuedUserMessage.render` and tests, which only need the joined text.
    `UserMessage.render` consumes the head/tail fields directly so it can
    interleave the clickable affordance between them.

    Args:
        text: Full message content.

    Returns:
        Truncated text with an elision marker, or the original text when
        it does not exceed the display threshold.
    """
    return _collapse_user_message(text).text






def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter delimited by `---` markers.

    Args:
        text: Raw `SKILL.md` content.

    Returns:
        Body text with frontmatter removed and leading whitespace stripped.
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text
    # Find closing --- (skip the opening line)
    end = stripped.find("\n---", 3)
    if end == -1:
        return text
    # Skip past the closing --- and its trailing newline
    after = end + 4  # len("\n---")
    return stripped[after:].lstrip("\n")








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


# Maps a tool name to the summary category it aggregates under. grep/glob share
# "search" so a mixed run folds into a single "Searched for N patterns" segment.
_TOOL_SUMMARY_CATEGORY: dict[str, str] = {
    "read_file": "read",
    "view_file": "read",
    "write_file": "write",
    "write_to_file": "write",
    "edit_file": "edit",
    "replace_file_content": "edit",
    "multi_replace_file_content": "edit",
    "delete": "delete",
    "delete_file": "delete",
    "ls": "ls",
    "list_dir": "ls",
    "grep": "search",
    "glob": "search",
    "execute": "shell",
    "run_command": "shell",
    "run_script": "script",
    "execute_script": "script",
    "web_search": "web_search",
    "fetch_url": "fetch",
    "js_eval": "js",
    "task": "task",
    "subagent": "task",
    "write_todos": "todos",
}

# category -> (present verb, past verb, singular noun, plural noun).
_TOOL_SUMMARY_PHRASES: dict[str, tuple[str, str, str, str]] = {
    "read": ("Reading", "Read", "file", "files"),
    "write": ("Writing", "Wrote", "file", "files"),
    "edit": ("Editing", "Edited", "file", "files"),
    "delete": ("Deleting", "Deleted", "file", "files"),
    "ls": ("Listing", "Listed", "directory", "directories"),
    "search": ("Searching for", "Searched for", "pattern", "patterns"),
    "shell": ("Running", "Ran", "shell command", "shell commands"),
    "script": ("Running", "Ran", "script", "scripts"),
    "js": ("Running", "Ran", "JS evaluation", "JS evaluations"),
    "fetch": ("Fetching", "Fetched", "URL", "URLs"),
    "task": ("Running", "Ran", "agent", "agents"),
}

_Tense = Literal["present", "past"]


def _summary_segment(category: str, count: int, tool_name: str, tense: _Tense) -> str:
    """Phrase a single count segment, e.g. "Read 2 files" / "Reading 2 files".

    Args:
        category: The summary category the tools were bucketed into.
        count: How many tools fell into this category.
        tool_name: A representative raw tool name, used to phrase categories
            that have no dedicated entry in `_TOOL_SUMMARY_PHRASES`.
        tense: Whether to phrase the segment in the present or past tense.

    Returns:
        The phrased segment for this category, count, and tense.
    """
    from opscloud.ui.widgets.tool_display import parse_scoped_tool_name

    scope_tag = ""
    clean_cat = category
    if "[" in category and category.endswith("]"):
        parts = category.split("[", 1)
        clean_cat = parts[0].strip()
        scope_tag = f" [{parts[1]}"

    if clean_cat == "web_search":
        base = "Searching the web" if tense == "present" else "Searched the web"
        res = base if count == 1 else f"{base} {count} times"
        return f"{res}{scope_tag}"
    if clean_cat == "todos":
        res = "Updating todos" if tense == "present" else "Updated todos"
        return f"{res}{scope_tag}"
    phrase = _TOOL_SUMMARY_PHRASES.get(clean_cat)
    if phrase is None:
        base_tool, _ = parse_scoped_tool_name(tool_name)
        present, past = "Running", "Ran"
        singular, plural = f"{base_tool} call", f"{base_tool} calls"
    else:
        present, past, singular, plural = phrase
    verb = present if tense == "present" else past
    noun = singular if count == 1 else plural
    return f"{verb} {count} {noun}{scope_tag}"


def summarize_tool_group(tool_names: list[str], *, tense: _Tense = "past") -> str:
    """Build a one-line summary of a run of tool calls.

    Aggregates by category in first-appearance order and lowercases the lead
    word of every segment after the first, e.g.
    `["read_file", "read_file", "execute"]` -> "Read 2 files, ran 1 shell command".

    Args:
        tool_names: Raw tool names for the run, in call order.
        tense: Whether to phrase the summary in the present or past tense.

    Returns:
        The aggregated one-line summary string in the requested tense.
    """
    from opscloud.ui.widgets.tool_display import parse_scoped_tool_name

    counts: dict[str, int] = {}
    order: list[str] = []
    rep_name: dict[str, str] = {}
    for name in tool_names:
        base_name, scope = parse_scoped_tool_name(name)
        base_cat = _TOOL_SUMMARY_CATEGORY.get(base_name, _TOOL_SUMMARY_CATEGORY.get(name, base_name))
        category = f"{base_cat} [{scope}]" if scope else base_cat
        if category not in counts:
            counts[category] = 0
            order.append(category)
            rep_name[category] = name
        counts[category] += 1

    segments = [
        _summary_segment(cat, counts[cat], rep_name[cat], tense) for cat in order
    ]
    if not segments:
        return "Running tools" if tense == "present" else "Ran tools"
    return _join_segments(segments)


def _join_segments(segments: list[str]) -> str:
    """Join summary segments, lowercasing the lead word of all but the first.

    Args:
        segments: Pre-phrased segments in display order.

    Returns:
        The segments joined with ", ", e.g. `["Ran 2 files", "Running 1 agent"]`
        -> "Ran 2 files, running 1 agent".
    """
    first, *rest = segments
    lowered = [f"{seg[0].lower()}{seg[1:]}" if seg else seg for seg in rest]
    return ", ".join([first, *lowered])


def summarize_live_tool_group(
    completed_names: list[str], pending_names: list[str]
) -> str:
    """Summarize an in-flight run, mixing past and present tense.

    Completed calls are phrased in the past tense so the work already done in
    the step stays visible, and the still-running calls are phrased in the
    present tense, e.g. `["execute", "execute"]` completed plus `["task"]`
    pending -> "Ran 2 shell commands, running 1 agent".

    Args:
        completed_names: Raw tool names that have finished successfully, in
            call order. Failed/rejected calls are evicted before this runs.
        pending_names: Raw tool names still pending or running, in call order.

    Returns:
        The combined one-line summary. Empty when neither list has members.
    """
    segments: list[str] = []
    if completed_names:
        segments.append(summarize_tool_group(completed_names, tense="past"))
    if pending_names:
        segments.append(summarize_tool_group(pending_names, tense="present"))
    if not segments:
        return ""
    return _join_segments(segments)


_TOOL_GROUP_COLLAPSED_ACCESSORY_CLASS = "-tool-group-collapsed-accessory"
"""Marker class hiding a collapsed group's accessory widgets.

See `_TOOL_AWAITING_APPROVAL_ACCESSORY_CLASS` for why the two hide reasons carry
separate classes and why neither may be replaced by assigning `display`.
"""


class ToolGroupSummary(Static):
    """Collapsed one-line stand-in for an assistant step's tool calls.

    Tools are hidden from the moment they start; this single line shows live
    progress ("Running 1 shell command…") and flips to the fully past-tense
    line ("Ran 1 shell command") once every tool finishes. While the step is
    live, finished calls stay visible in the past tense next to the ones still
    running in the present tense (e.g. "Ran 2 shell commands, running 1 agent…")
    so the work already done in the step doesn't disappear. Failed, rejected,
    and skipped tools are evicted to standalone rows (see `_evict_failed`) so
    errors stay visible. Clicking the line or pressing Ctrl+O expands the
    underlying tool rows (and their diffs).

    Two modes:

    - **live** (streaming): created empty, members added via `add_member` as
      they mount, a spinner timer animates the line and re-renders present/past
      tense, and failed tools are ejected back into view so errors stay visible.
    - **finalized** (`live=False`, used for hydration/resume): a fixed set of
      completed tools rendered straight to the past tense with no timer.

    Purely presentational — never tracked by the message store; it is re-derived
    from the mounted tool widgets on each stream boundary and on hydration.
    """

    DEFAULT_CSS = """
    ToolGroupSummary {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
        color: $text-muted;
        pointer: pointer;
    }

    ToolGroupSummary:hover {
        color: $text;
    }
    """

    _SPINNER_INTERVAL: ClassVar[float] = 0.1

    _collapsed: var[bool] = var(True)

    def __init__(
        self,
        tools: list[ToolCallMessage] | None = None,
        collapsible: list[Widget] | None = None,
        *,
        accessories: dict[Widget, list[Widget]] | None = None,
        live: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize the summary.

        Args:
            tools: Tool widgets the summary aggregates (drives its text). May be
                empty for a live group that grows via `add_member`.
            collapsible: Every widget hidden/shown with the group, including the
                tool widgets and any interleaved diff previews.
            accessories: Decorations (e.g. timestamp footers) keyed by the
                collapsible they trail, hidden and shown with that owner via a
                marker class rather than `display`. Collapsing therefore never
                clears an accessory's own visibility class, so the
                `/timestamps` preference reasserts itself on expand. Keys must
                appear in `collapsible`: `_apply_visibility` iterates
                `collapsible`, so an accessory keyed by a non-member is never
                synced.
            live: When True, animate progress and accept new members until
                `close`. When False, render a finalized past-tense summary.
            **kwargs: Additional arguments passed to `Static`.
        """
        super().__init__("", **kwargs)
        self._tools = list(tools or [])
        self._collapsible = list(collapsible or [])
        self._accessories: dict[Widget, list[Widget]] = {}
        for owner, widgets in (accessories or {}).items():
            self._attach_accessories(owner, widgets)
        self._accepting_members = live
        self._finalized = not live
        self._spinner_pos = 0
        self._timer: Timer | None = None
        # Cached summary phrasing, rebuilt only when membership changes (not on
        # every spinner tick). None means "recompute on next render".
        self._present_text: str | None = None
        self._past_text: str | None = None
        # The (completed, pending) tool-name tuples the cached live line was
        # built from. The line mixes finished (past tense) and running (present
        # tense) members, so it must be rebuilt whenever a member finishes, not
        # just when membership grows.
        self._present_key: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    def on_mount(self) -> None:
        """Apply initial visibility, render, and arm the spinner if live."""
        self._apply_visibility()
        self._render_line()
        self._sync_timer()

    def _attach_accessories(self, owner: Widget, accessories: Iterable[Widget]) -> None:
        """Record `owner`'s accessories and link them to its approval hiding.

        The single registration point for every fold path (constructor,
        `add_member`, `add_collapsible`) so the group's `_accessories` map and a
        tool's own `_visibility_accessories` cannot drift apart — a tool folded
        via one path would otherwise hide its footer on collapse but not while
        an approval prompt replaced it.
        """
        linked = [a for a in accessories if a not in self._accessories.get(owner, ())]
        if not linked:
            return
        self._accessories.setdefault(owner, []).extend(linked)
        if isinstance(owner, ToolCallMessage):
            owner._register_visibility_accessories(*linked)

    def add_member(self, tool: ToolCallMessage, *accessories: Widget) -> None:
        """Add a tool to a live group and link its accessories.

        Args:
            tool: Tool widget folded into the group.
            accessories: Decorations (e.g. the tool's timestamp footer) that
                follow the tool's visibility via a marker class. Not group
                members: they take neither `-grouped` nor a `display` flip, so
                their own visibility class survives the fold. Also linked to
                the tool's approval hiding.
        """
        tool.add_class("-grouped")
        self._tools.append(tool)
        self._collapsible.append(tool)
        self._attach_accessories(tool, accessories)
        self._present_text = self._past_text = self._present_key = None
        self._apply_visibility()
        in_progress = self._sync_lifecycle()
        self._render_line(in_progress=in_progress)

    def add_collapsible(self, widget: Widget, *accessories: Widget) -> None:
        """Attach a non-tool widget (e.g. a diff) and its accessories.

        Args:
            widget: Non-tool widget folded with the group.
            accessories: Decorations (e.g. the widget's timestamp footer) that
                follow the widget's visibility via a marker class. Not group
                members, so their own visibility class survives the fold. No
                approval linkage: only a `ToolCallMessage` can await approval.
        """
        widget.add_class("-grouped")
        self._collapsible.append(widget)
        self._attach_accessories(widget, accessories)
        self._apply_collapsible_visibility(widget, visible=not self._collapsed)

    def close(self) -> None:
        """Stop accepting members and finalize after every tool settles.

        A non-tool stream event can close a group before middleware-generated
        terminal results arrive. Keep the live timer running in that case so a
        later error or rejection is evicted instead of being summarized in the
        past tense as though the tool ran successfully.
        """
        self._accepting_members = False
        self._evict_failed()
        in_progress = self._sync_lifecycle()
        if not self.is_attached:
            return
        if self._tools:
            self._render_line(in_progress=in_progress)
        else:
            # Every tool failed and was ejected — nothing left to summarize.
            # Release whatever is still folded first: once this summary is gone
            # nothing can expand it, so a retained widget (and its accessories)
            # would stay hidden for the rest of the session.
            self._release_all_collapsible()
            self.remove()

    def _release_collapsible(self, widget: Widget) -> None:
        """Drop a widget's group linkage and clear its collapsed accessory class.

        Only the *group's* hide reason is released. A tool's own approval
        linkage (`_visibility_accessories`) intentionally survives, so a
        revealed pending row still hides its footer when an approval prompt
        replaces it — see `_TOOL_AWAITING_APPROVAL_ACCESSORY_CLASS`.
        """
        if widget in self._collapsible:
            self._collapsible.remove(widget)
        widget.remove_class("-grouped")
        for accessory in self._accessories.pop(widget, []):
            accessory.remove_class(_TOOL_GROUP_COLLAPSED_ACCESSORY_CLASS)

    def _release_all_collapsible(self) -> None:
        """Release and reveal every remaining folded widget.

        Must run before this summary is removed: a widget left folded keeps
        `-grouped`, `display = False`, and its accessories' marker class with no
        summary left to expand it, which no later toggle can undo.
        """
        for widget in list(self._collapsible):
            self._release_collapsible(widget)
            if widget.is_attached:
                widget.display = True

    def reveal_pending(self) -> None:
        """Remove unfinished tool calls from the collapsed group."""
        pending = [tool for tool in self._tools if tool.is_pending]
        if not pending:
            return
        for tool in pending:
            self._tools.remove(tool)
            self._release_collapsible(tool)
            if tool.is_attached and not tool._awaiting_approval:
                tool.display = True
        self._present_text = self._past_text = self._present_key = None
        in_progress = self._sync_lifecycle()
        if self._tools:
            self._render_line(in_progress=in_progress)
            return
        self._release_all_collapsible()
        if self.is_attached:
            self.remove()

    @property
    def has_attached_members(self) -> bool:
        """Whether any collapsed widget is still attached to the DOM."""
        return any(widget.is_attached for widget in self._collapsible)

    def toggle(self) -> None:
        """Toggle between collapsed and expanded."""
        self._collapsed = not self._collapsed

    def watch__collapsed(self, _collapsed: bool) -> None:
        """Re-render and re-apply member visibility when the state changes.

        Coalesced into one repaint so expanding a multi-tool group reveals every
        row at once instead of bouncing the transcript per member.
        """
        if not self.is_attached:
            self._apply_visibility()
            self._render_line()
            return
        with self.app.batch_update():
            self._apply_visibility()
            self._render_line()

    def on_click(self, event: Click) -> None:
        """Toggle the group on click."""
        event.stop()
        self.toggle()

    def _in_progress(self) -> bool:
        """Whether any member tool is still pending or running.

        Returns:
            True if at least one member tool has not finished.
        """
        return any(tool.is_pending for tool in self._tools)

    def _sync_lifecycle(self, *, in_progress: bool | None = None) -> bool:
        """Finalize only once a closed group's retained tools have settled.

        Returns:
            Whether any retained tool is still in progress.
        """
        if in_progress is None:
            in_progress = self._in_progress()
        self._finalized = not self._accepting_members and not in_progress
        self._sync_timer()
        return in_progress

    def _evict_failed(self) -> None:
        """Un-fold errored/rejected/skipped tools so non-successes stay visible."""
        failed = [t for t in self._tools if t.is_failed]
        if not failed:
            return
        for tool in failed:
            self._tools.remove(tool)
            self._release_collapsible(tool)
            if tool.is_attached:
                tool.display = True
        self._present_text = self._past_text = self._present_key = None

    def _sync_timer(self) -> None:
        """Run the spinner timer only while live members are in progress."""
        if not self._finalized and self._in_progress():
            if self._timer is None:
                self._timer = self.set_interval(self._SPINNER_INTERVAL, self._tick)
        else:
            self._stop_timer()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def sync_members(self) -> None:
        """Synchronize member status, evict failures, and update the summary line."""
        before = len(self._tools)
        self._evict_failed()
        evicted = len(self._tools) != before
        if self._collapsed:
            # Re-assert hidden state in case a member was shown externally
            # (e.g. ToolCallMessage.clear_awaiting_approval after HITL).
            self._apply_visibility()
        if not self._tools:
            self._sync_lifecycle(in_progress=False)
            # Nothing can expand this summary once it is gone, so release
            # anything still folded before removing it.
            self._release_all_collapsible()
            if self.is_attached:
                self.remove()
            return
        in_progress = self._sync_lifecycle()
        # A bare spinner advance keeps the line height. `_render_line`
        # promotes this to a layout update if the pending summary changed.
        self._render_line(
            in_progress=in_progress, layout=evicted or not in_progress
        )

    def _tick(self) -> None:
        """Advance the spinner, eject failures, and flip to past tense when done."""
        try:
            self._spinner_pos += 1
            self.sync_members()
        except Exception:
            # Fires ~10x/second, so an unhandled raise would propagate out of the
            # interval callback and can crash the app repeatedly. The group is
            # purely presentational; stop animating and log rather than take the
            # transcript down.
            logger.exception("ToolGroupSummary spinner tick failed; stopping timer")
            self._stop_timer()

    def _apply_collapsible_visibility(self, widget: Widget, *, visible: bool) -> None:
        """Apply the group's visibility to a widget and its accessories.

        The owner is driven directly via `display`; accessories are driven by a
        marker class instead, so hiding them leaves their independent visibility
        class intact and it reasserts itself when the group expands. The two
        mechanisms are not interchangeable — see
        `_TOOL_AWAITING_APPROVAL_ACCESSORY_CLASS`.

        Accessories are classed even while detached: `set_class` is safe off-DOM
        and nothing revisits a skipped accessory, so guarding on `is_attached`
        here would leave a late-mounted footer stranded visible over a hidden
        row.
        """
        if widget.is_attached and widget.display != visible:
            widget.display = visible
        for accessory in self._accessories.get(widget, []):
            accessory.set_class(not visible, _TOOL_GROUP_COLLAPSED_ACCESSORY_CLASS)

    def _apply_visibility(self) -> None:
        """Show or hide every folded widget, and its accessories, per collapse."""
        visible = not self._collapsed
        for widget in self._collapsible:
            self._apply_collapsible_visibility(widget, visible=visible)

    def _render_line(
        self, *, in_progress: bool | None = None, layout: bool = True
    ) -> None:
        """Refresh the summary line for the current tense and collapsed state.

        Args:
            in_progress: Pre-computed progress state to avoid re-scanning members
                on the spinner hot path; recomputed when omitted.
            layout: Whether to force a layout update. A changed summary always
                triggers layout; the spinner hot path passes False so a bare
                glyph swap doesn't relayout the whole transcript 10x/second.
        """
        if not self.is_attached:
            return
        if not self._tools:
            self.update(Content(""), layout=layout)
            return
        glyphs = get_glyphs()
        if in_progress is None:
            in_progress = self._in_progress()
        if not self._finalized and in_progress:
            pending = [tool.tool_name for tool in self._tools if tool.is_pending]
            completed = [tool.tool_name for tool in self._tools if not tool.is_pending]
            key = (tuple(completed), tuple(pending))
            summary_changed = self._present_text is None or key != self._present_key
            if summary_changed:
                self._present_text = summarize_live_tool_group(completed, pending)
                self._present_key = key
            frames = glyphs.spinner_frames
            spinner = frames[self._spinner_pos % len(frames)]
            self.update(
                self._live_line(spinner, completed, pending),
                layout=layout or summary_changed,
            )
        else:
            mark = (
                glyphs.disclosure_collapsed
                if self._collapsed
                else glyphs.disclosure_expanded
            )
            if self._past_text is None:
                self._past_text = summarize_tool_group(
                    [tool.tool_name for tool in self._tools], tense="past"
                )
            self.update(
                Content.assemble(
                    (f"{mark} ", "$primary"), *_emphasize_counts(self._past_text)
                ),
                layout=layout,
            )

    @staticmethod
    def _live_line(spinner: str, completed: list[str], pending: list[str]) -> Content:
        """Live line: finished work muted, the in-flight segment in teal.

        e.g. ``⠹ Read 1 file, running 1 agent, running 1 shell command…`` where
        only "running 1 agent, running 1 shell command…" is highlighted, so
        the eye lands on what is happening right now.
        """
        ellipsis = get_glyphs().ellipsis
        pieces: list[tuple[str, str] | str] = [(f"{spinner} ", "$primary")]
        done = summarize_tool_group(completed, tense="past") if completed else ""
        active = summarize_tool_group(pending, tense="present") if pending else ""
        if done:
            pieces.extend(_emphasize_counts(done))
        if active:
            if done:
                pieces.append(", ")
                active = f"{active[0].lower()}{active[1:]}"
            pieces.append((f"{active}{ellipsis}", "$primary"))
        return Content.assemble(*pieces)


_COUNT_RE = re.compile(r"\b\d+\b")


def _emphasize_counts(text: str) -> list[tuple[str, str] | str]:
    """Split ``text`` so bare numbers render in teal ("Read **2** files").

    Everything else inherits the widget colour (muted, brighter on hover).
    """
    pieces: list[tuple[str, str] | str] = []
    last = 0
    for match in _COUNT_RE.finditer(text):
        if match.start() > last:
            pieces.append(text[last : match.start()])
        pieces.append((match.group(), "$primary"))
        last = match.end()
    if last < len(text):
        pieces.append(text[last:])
    return pieces










class _RichMarkdownWrapper:
    """Render Rich markdown for both muted system base and vibrant command output cards.

    When `is_command=True`:
      - Applies OpsCloud theme colors (Teal #2dd4bf for h1/h3, Cyan #7AA2F7 for h2,
        Purple #BB9AF7 for code/h4, Green for success glyphs, subtle dark borders for tables).
      - Body text is rendered with full foreground brightness.
    When `is_command=False`:
      - Maintains classic muted/dimmed system notification style.
    """

    _MUTED_THEME_OVERRIDES: ClassVar[dict[str, str]] = {
        "markdown.h1": "bold underline",
        "markdown.h2": "bold underline",
        "markdown.h3": "bold",
        "markdown.h4": "italic",
        "markdown.table.header": "bold",
        "markdown.table.border": "",
        "markdown.code": "bold",
        "markdown.code_inline": "bold",
    }

    _COMMAND_THEME_OVERRIDES: ClassVar[dict[str, str]] = {
        "markdown.h1": "bold #2dd4bf underline",
        "markdown.h2": "bold #7AA2F7",
        "markdown.h3": "bold #2dd4bf",
        "markdown.h4": "bold #BB9AF7",
        "markdown.table.header": "bold #2dd4bf",
        "markdown.table.border": "#3B3E52",
        "markdown.code": "bold #BB9AF7",
        "markdown.code_inline": "bold #2dd4bf",
        "markdown.bullet": "bold #2dd4bf",
        "markdown.link": "underline #7AA2F7",
        "markdown.hr": "#3B3E52",
    }

    def __init__(self, markup: str, *, is_command: bool = False) -> None:
        from rich.markdown import (
            Markdown as RichMarkdown,
            MarkdownElement,
            TableElement,
        )
        from rich.table import Table

        class _FoldingTableElement(TableElement):
            """Render long Markdown table cells by folding instead of eliding."""

            def __rich_console__(  # noqa: PLW3201  # Rich renderable protocol
                self, console: RichConsole, options: ConsoleOptions
            ) -> RenderResult:
                for renderable in super().__rich_console__(console, options):
                    if isinstance(renderable, Table):
                        for column in renderable.columns:
                            column.overflow = "fold"
                    yield renderable

        class _FoldingMarkdown(RichMarkdown):
            """Rich Markdown variant that never ellipsizes table cells."""

            elements: ClassVar[dict[str, type[MarkdownElement]]] = {
                **RichMarkdown.elements,
                "table_open": _FoldingTableElement,
            }

        self._markdown = _FoldingMarkdown(markup)
        self._markup = markup
        self._is_command = is_command

    def __rich_console__(  # noqa: PLW3201  # Rich renderable protocol
        self, console: RichConsole, options: ConsoleOptions
    ) -> RenderResult:
        from rich.styled import Styled
        from rich.theme import Theme

        overrides = (
            self._COMMAND_THEME_OVERRIDES
            if self._is_command
            else self._MUTED_THEME_OVERRIDES
        )
        theme = Theme(overrides, inherit=True)
        try:
            with console.use_theme(theme):
                if self._is_command:
                    yield from self._markdown.__rich_console__(
                        console, options
                    )
                else:
                    yield from self._markdown.__rich_console__(
                        console, options
                    )
        except Exception:
            logger.warning(
                "Rich markdown rendering failed; falling back to plain text",
                exc_info=True,
            )
            yield from Styled(self._markup, "").__rich_console__(
                console, options
            )


_MutedRichMarkdown = _RichMarkdownWrapper


# Floor for markdown layout width so a not-yet-sized widget still renders a
# readable table instead of collapsing to a single column.
_MARKDOWN_MIN_RENDER_WIDTH = 20

# One-shot flag (mutable holder to avoid a `global` statement) set once the first
# markdown style conversion fails, so a systematic breakage (e.g. a Rich/Textual
# version drift) surfaces at `warning` once instead of staying invisible at
# `debug`, without spamming a line per unconvertible span.
_markdown_style_conversion_warned = [False]


def _markdown_to_content(
    markup: str,
    width: int,
    console: RichConsole | None = None,
    *,
    is_command: bool = False,
) -> Content:
    """Render markdown to selectable `Content` at a fixed width."""
    from rich.console import Console
    from rich.segment import Segment
    from textual.content import Span
    from textual.style import Style

    render_width = max(width, 1)
    if console is None:
        console = Console(width=render_width)
    segments = console.render(
        _RichMarkdownWrapper(markup, is_command=is_command),
        console.options.update_width(render_width),
    )
    content_lines: list[Content] = []
    for line in Segment.split_lines(segments):
        text = "".join(segment.text for segment in line)
        stripped = text.rstrip()
        spans: list[Span] = []
        position = 0
        for segment in line:
            start = position
            position += len(segment.text)
            if start >= len(stripped):
                break
            end = min(position, len(stripped))
            if segment.style is not None and end > start:
                try:
                    style = Style.from_rich_style(segment.style)
                except Exception:  # style conversion is best-effort
                    if not _markdown_style_conversion_warned[0]:
                        _markdown_style_conversion_warned[0] = True
                        logger.warning(
                            "Failed to convert a markdown style; markdown will "
                            "render without some styling (later occurrences log "
                            "at debug)",
                            exc_info=True,
                        )
                    else:
                        logger.debug(
                            "Skipping unconvertible markdown style", exc_info=True
                        )
                else:
                    spans.append(Span(start, end, style))
        content_lines.append(Content(stripped, spans))
    while content_lines and not content_lines[-1].plain:
        content_lines.pop()
    return Content("\n").join(content_lines)





class UserMessage(Static):
    """A user message styled by input mode.

    - **prompt** (normal text): subtle violet strip with a violet ``❯`` rail,
      so the user's voice is distinct from the teal agent.
    - **slash command** (``/plugins``): compact command chip, no strip, so it
      reads as an action taken rather than a question asked.
    - **shell** (``!cmd``): green ``$`` rail; incognito shell is muted.
    """

    DEFAULT_CSS = """
    UserMessage {
        height: auto;
        padding: 0 1;
        margin: 1 0;
        background: $accent 7%;
        border-left: wide $accent;
        pointer: text;
        link-color: $text;
        link-style: not underline;
        link-color-hover: $text;
        link-background-hover: transparent;
        link-style-hover: not bold not underline;
    }

    UserMessage.-mode-command {
        background: transparent;
        border-left: none;
        padding: 0 1 0 2;
    }

    UserMessage.-mode-shell {
        background: $success 6%;
        border-left: wide $mode-bash;
    }

    UserMessage.-mode-shell_incognito {
        background: $panel 50%;
        border-left: wide $mode-incognito;
    }

    UserMessage.-cancelled {
        opacity: 0.6;
    }
    """

    def __init__(self, content: str) -> None:
        from opscloud.ui.widgets.chat_input import detect_input_mode

        self._raw_content = content
        self._timestamp = time.strftime("%H:%M:%S")
        self._show_timestamp = False
        self._mode = detect_input_mode(content)
        # Pass empty string — render() provides the live content on every repaint.
        super().__init__("", classes=f"-mode-{self._mode}")

    @property
    def raw_text(self) -> str:
        """The original, untruncated message text."""
        return self._raw_content

    def set_cancelled(self) -> None:
        """Dim the message to mark its turn as interrupted by the user."""
        self.add_class("-cancelled")

    def set_timestamp_visible(self, visible: bool) -> None:
        self._show_timestamp = visible
        self.refresh(layout=True)

    def _parts(self) -> list[str | tuple[str, str]]:
        """Build the styled segments for the current mode and theme.

        Styles reference Textual theme variables (``$accent``,
        ``$text-muted``) so they resolve against the live theme and the
        terminal's real background rather than precomputed hex values.
        """
        from opscloud.ui.widgets.chat_input import MODE_PREFIXES

        content = self._raw_content
        mode = self._mode

        if mode == "command":
            name, _, args = content.partition(" ")
            parts: list[str | tuple[str, str]] = [
                ("› ", "dim"),
                (name, "bold $accent"),
            ]
            if args.strip():
                parts.append((f"  {args.strip()}", "dim"))
            return parts

        if mode in ("shell", "shell_incognito"):
            body = content[len(MODE_PREFIXES[mode]):]
            if mode == "shell":
                return [("$ ", "bold $mode-bash"), body]
            return [
                ("$ ", "bold $mode-incognito"),
                body,
                ("  incognito", "italic dim"),
            ]

        return [("❯ ", "bold $accent"), content]

    def render(self) -> Content:
        """Render the styled user message with live theme colors.

        Textual calls this on every repaint, so the glyph color and border
        always reflect the currently active theme — even after a ``/theme``
        switch.

        Returns:
            Styled ``Content`` with mode prefix and message body.
        """
        parts = self._parts()
        if self._show_timestamp:
            parts.append((f"  [{self._timestamp}]", "dim italic"))
        return Content.assemble(*parts)

_ASSISTANT_MARKDOWN_CLS: type[Markdown] | None = None
"""Lazily-built ``Markdown`` subclass used for assistant replies."""


def _assistant_markdown_class() -> type[Markdown]:
    """Return the ``Markdown`` subclass used inside ``AssistantMessage``.

    Built lazily so importing this module stays cheap (``textual.widgets``
    markdown machinery is only loaded once a reply is rendered).  The only
    behavioural difference from stock ``Markdown`` is that fenced code
    blocks carry their language as a border title (``bash``, ``hcl`` ...).
    """
    global _ASSISTANT_MARKDOWN_CLS
    if _ASSISTANT_MARKDOWN_CLS is not None:
        return _ASSISTANT_MARKDOWN_CLS

    from textual.widgets import Markdown
    from textual.widgets._markdown import MarkdownFence

    class AssistantFence(MarkdownFence):
        """Code fence that shows its language label and opens file paths on click."""

        DEFAULT_CSS = """
        AssistantFence {
            overflow: hidden hidden;
            scrollbar-size-horizontal: 0;
            scrollbar-size-vertical: 0;
        }
        """

        def __init__(self, markdown: Any, token: Any, code: str) -> None:
            super().__init__(markdown, token, code)
            self._apply_language_title()

        def _get_target_path(self, line_index: int | None = None) -> str | None:
            from opscloud.editor import extract_file_path

            if line_index is not None:
                lines = (self.code or "").splitlines()
                if 0 <= line_index < len(lines):
                    path = extract_file_path(lines[line_index])
                    if path:
                        return path
            return extract_file_path(self.code or "")

        def _apply_language_title(self) -> None:
            lang = (self.lexer or "").strip().split(" ", 1)[0]
            self.border_title = lang or None
            path = self._get_target_path()
            if path:
                self.add_class("-is-file-link")
                self.border_subtitle = "click to open"
                try:
                    from pathlib import Path

                    tip = f"Open file in editor (click): {Path(path).name}"
                    self.tooltip = tip
                    try:
                        self.query_one("#code-content").tooltip = tip
                    except Exception:
                        pass
                except Exception:
                    pass
            else:
                self.remove_class("-is-file-link")
                self.border_subtitle = None
                self.tooltip = None
                try:
                    self.query_one("#code-content").tooltip = None
                except Exception:
                    pass

        def on_mount(self) -> None:
            self._apply_language_title()

        def _copy_context(self, block: Any) -> None:
            super()._copy_context(block)
            self._apply_language_title()

        async def _update_from_block(self, block: Any) -> None:
            await super()._update_from_block(block)
            self._apply_language_title()

        async def on_click(self, event: Any) -> None:
            """Open file if this code fence contains an existing file path."""
            line_idx = getattr(event, "y", None)
            path = self._get_target_path(line_idx) or self._get_target_path()
            if path and self.app is not None:
                event.stop()
                from opscloud.editor import open_file_in_editor

                open_file_in_editor(path, app=self.app)

    class AssistantMarkdown(Markdown):
        """Markdown renderer for assistant replies."""

        BLOCKS = {
            **Markdown.BLOCKS,
            "fence": AssistantFence,
            "code_block": AssistantFence,
        }

    _ASSISTANT_MARKDOWN_CLS = AssistantMarkdown
    return AssistantMarkdown


class AssistantMessage(Vertical):
    """Widget displaying an assistant message with markdown support.

    Uses MarkdownStream for smoother streaming instead of re-rendering
    the full content on each update. Once a stream finishes, the message
    is re-rendered from the complete source via ``Markdown.update()`` to
    ensure correct final rendering.
    """

    _STREAM_FLUSH_INTERVAL: ClassVar[float] = 0.1
    """Seconds between coalesced flushes of streamed text to the markdown widget."""

    DEFAULT_CSS = """
    AssistantMessage {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    AssistantMessage Markdown {
        padding: 0;
        margin: 0;
        pointer: text;
        link-color: $primary;
        link-style: underline;
        link-color-hover: $primary;
        link-background-hover: $primary 15%;
        link-style-hover: bold underline;
    }

    /* First block hugs the marker row.  Note: a lone ``margin-top`` would
       reset the whole margin in Textual, so full margins are spelled out. */
    AssistantMessage Markdown > MarkdownH1:first-child,
    AssistantMessage Markdown > MarkdownH2:first-child,
    AssistantMessage Markdown > MarkdownFence:first-child,
    AssistantMessage Markdown > MarkdownBlockQuote:first-child {
        margin: 0 0 1 0;
    }

    AssistantMessage Markdown > MarkdownH3:first-child,
    AssistantMessage Markdown > MarkdownH4:first-child,
    AssistantMessage Markdown > MarkdownH5:first-child,
    AssistantMessage Markdown > MarkdownH6:first-child {
        margin: 0;
    }

    AssistantMessage Markdown > *:last-child {
        margin-bottom: 0;
    }

    /* Headings: colour + weight carry hierarchy; no underlines or centring */
    AssistantMessage MarkdownH1 {
        content-align: left middle;
        color: $primary;
        background: transparent;
        text-style: bold;
        margin: 1 0 1 0;
    }

    AssistantMessage MarkdownH2 {
        color: $primary;
        background: transparent;
        text-style: bold;
        margin: 1 0 1 0;
    }

    AssistantMessage MarkdownH3 {
        color: $secondary;
        background: transparent;
        text-style: bold;
        margin: 1 0 0 0;
    }

    AssistantMessage MarkdownH4,
    AssistantMessage MarkdownH5,
    AssistantMessage MarkdownH6 {
        color: $foreground;
        background: transparent;
        text-style: bold;
        margin: 1 0 0 0;
    }

    /* Lists: brand-coloured markers */
    AssistantMessage MarkdownBullet {
        color: $primary;
    }

    /* Inline code: amber text on a faint tint of the same hue */
    AssistantMessage Markdown MarkdownBlock > .code_inline {
        color: $text-warning;
        background: $warning 8%;
    }

    /* Code fences: hairline frame with the language as its title */
    AssistantMessage MarkdownFence {
        margin: 1 0;
        padding: 0 1;
        background: transparent;
        border: round $foreground 18%;
        border-title-color: $text-muted;
        border-title-align: left;
        overflow: hidden hidden;
        scrollbar-size-horizontal: 0;
        scrollbar-size-vertical: 0;
    }

    AssistantMessage MarkdownFence.-is-file-link {
        border: round $primary 50%;
        border-title-color: $primary;
        border-subtitle-color: $primary;
        border-subtitle-align: right;
    }

    AssistantMessage MarkdownFence.-is-file-link:hover {
        border: round $primary;
        border-subtitle-color: $primary;
        background: $primary 8%;
    }

    AssistantMessage MarkdownFence > Label {
        padding: 0;
    }

    /* Block quotes: callout rail in the brand colour, no fill */
    AssistantMessage MarkdownBlockQuote {
        background: transparent;
        border-left: outer $primary 60%;
        color: $text-muted;
        text-style: italic;
        margin: 1 0;
        padding: 0 1;
    }

    /* Tables: quiet grid, bold brand-coloured header */
    AssistantMessage MarkdownTableContent {
        keyline: thin $foreground 14%;
    }

    AssistantMessage MarkdownTableContent > .header {
        color: $primary;
        text-style: bold;
    }

    /* Horizontal rule: a hairline, not a full-strength accent bar */
    AssistantMessage MarkdownHorizontalRule {
        border-bottom: solid $foreground 18%;
    }
    """

    def __init__(self, content: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._content_parts: list[str] = [content] if content else []
        self._is_streaming: bool = not bool(content)
        self._markdown: Any = None
        self._stream: Any = None
        self._pending_append: str = ""
        self._flush_timer: Any = None

    @property
    def _content(self) -> str:
        """Full message text, materialized from streamed chunks on access."""
        if len(self._content_parts) > 1:
            self._content_parts = ["".join(self._content_parts)]
        return self._content_parts[0] if self._content_parts else ""

    @_content.setter
    def _content(self, value: str) -> None:
        self._content_parts = [value] if value else []

    def compose(self) -> ComposeResult:
        yield _assistant_markdown_class()("", id="assistant-content", open_links=False)

    def on_mount(self) -> None:
        from textual.widgets import Markdown
        from opscloud.ui.widgets._links import autolink_file_paths

        self._markdown = self.query_one("#assistant-content", Markdown)
        # If constructed with initial content (history replay), render it now.
        if self._content_parts and not self._is_streaming:
            self.run_worker(
                self._markdown.update(autolink_file_paths(self._content)),
                exclusive=True,
                thread=False,
            )

    @property
    def content_text(self) -> str:
        """Raw text content accumulated so far."""
        return self._content

    @property
    def is_streaming(self) -> bool:
        """Whether the message is actively streaming tokens."""
        return self._is_streaming

    def _get_markdown(self) -> Any:
        """Get the markdown widget, querying if not cached."""
        if self._markdown is None:
            from textual.widgets import Markdown
            self._markdown = self.query_one("#assistant-content", Markdown)
        return self._markdown

    def _ensure_stream(self) -> Any:
        """Ensure the markdown stream is initialized."""
        if self._stream is None:
            from textual.widgets import Markdown
            self._stream = Markdown.get_stream(self._get_markdown())
        return self._stream

    def append_token(self, token: str) -> None:
        """Append a streaming text token using MarkdownStream.

        Tokens are buffered and flushed on a throttled timer to avoid
        starving the UI event loop during fast model streaming.
        """
        if not token:
            return
        self._is_streaming = True
        self._content_parts.append(token)
        self._pending_append += token
        if self._flush_timer is None:
            self._flush_timer = self.set_interval(
                self._STREAM_FLUSH_INTERVAL, self._flush_pending_append
            )

    async def _flush_pending_append(self) -> None:
        """Write any buffered streamed text to the markdown stream."""
        if not self._pending_append:
            return
        pending = self._pending_append
        self._pending_append = ""
        try:
            stream = self._ensure_stream()
            await stream.write(pending)
            if isinstance(self.parent, MessageList) and self.parent._auto_scroll_locked:
                self.parent._scroll_to_end()
                self.parent.call_after_refresh(self.parent._scroll_to_end)
        except Exception:
            # Restore buffer on failure so next tick retries.
            self._pending_append = pending + self._pending_append
            logger.exception("Failed to flush streamed markdown fragment")

    def _stop_flush_timer(self) -> None:
        """Cancel the coalescing flush timer if it is running."""
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None

    def update_text(self, text: str) -> None:
        """Replace the entire accumulated text with a new full string."""
        self._is_streaming = True
        self._content = text
        self._stop_flush_timer()
        self._pending_append = ""
        if self._stream is not None:
            try:
                import asyncio
                asyncio.get_event_loop().create_task(self._stream.stop())
            except Exception:
                pass
            self._stream = None
        try:
            from opscloud.ui.widgets._links import autolink_file_paths

            md = self._get_markdown()
            self.run_worker(
                md.update(autolink_file_paths(text)), exclusive=True, thread=False
            )
        except Exception:
            pass

    def finish(self) -> None:
        """Mark the message as complete (final markdown render)."""
        self._is_streaming = False
        self._stop_flush_timer()
        if not self._content_parts or not self._content.strip():
            self.remove()
        else:
            # Flush remaining buffered text and do a final full render if needed.
            async def _finalize() -> None:
                had_stream = self._stream is not None
                await self._flush_pending_append()
                if self._stream is not None:
                    await self._stream.stop()
                    self._stream = None
                from opscloud.ui.widgets._links import autolink_file_paths

                autolinked = autolink_file_paths(self._content)
                md = self._get_markdown()
                if not had_stream or autolinked != self._content or not md.children:
                    await md.update(autolinked)
                if isinstance(self.parent, MessageList) and self.parent._auto_scroll_locked:
                    self.parent._scroll_to_end()
            self.run_worker(_finalize(), exclusive=True, thread=False)

    async def on_markdown_link_clicked(self, event: Any) -> None:
        """Open link in browser or file in user's default editor."""
        event.stop()
        from opscloud.ui.widgets._links import open_link_or_file_async

        href = getattr(event, "href", "")
        if href and self.app is not None:
            await open_link_or_file_async(href, app=self.app, notify_on_success=True)

    async def on_click(self, event: Any) -> None:
        """Handle click anywhere in message that targets an existing file path."""
        from opscloud.editor import extract_file_path, open_file_in_editor

        # 1. Check if clicked inside a code fence
        from textual.widgets._markdown import MarkdownFence

        w = getattr(event, "widget", None)
        while w is not None and w is not self:
            if isinstance(w, MarkdownFence):
                path = extract_file_path(getattr(w, "code", ""))
                if path and self.app is not None:
                    event.stop()
                    open_file_in_editor(path, app=self.app)
                    return
            w = getattr(w, "parent", None)

        # 2. Check text of the clicked widget
        target = getattr(event, "widget", None)
        text = ""
        if hasattr(target, "_content"):
            c = getattr(target, "_content")
            text = c.text if hasattr(c, "text") else str(c)
        elif hasattr(target, "renderable"):
            text = str(getattr(target, "renderable"))
        elif hasattr(target, "code"):
            text = str(getattr(target, "code"))

        path = extract_file_path(text)
        if path and self.app is not None:
            event.stop()
            open_file_in_editor(path, app=self.app)



class DiffMessage(Static):
    """Inline diff display widget."""

    DEFAULT_CSS = """
    DiffMessage {
        padding: 0 1;
        margin: 1 0 0 0;
        background: $background;
        border: solid $panel;
        pointer: text;
    }
    """

    def __init__(self, patch_or_diff: str, file_path: str = "") -> None:
        lines = patch_or_diff.splitlines()
        additions = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
        deletions = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
        header = format_file_diff_header(file_path or "Diff", additions=additions, deletions=deletions)
        body, _ = render_diff_content(patch_or_diff, max_lines=None, show_stats=False)
        super().__init__(Content.assemble(header, "\n", body))

class _SkillToggle(Static):
    """Clickable header/hint area for toggling skill body expansion.

    Referenced by name in `SkillMessage._on_toggle_click`'s `@on(Click)`
    CSS selector — rename with care.
    """


class SkillMessage(Vertical):
    """Widget displaying a skill invocation with collapsible body.

    Shows skill name, source badge, description, and user args as a compact
    header. The full SKILL.md body (frontmatter stripped) is hidden behind a
    preview/expand toggle (click or Ctrl+O). The expanded view renders
    markdown via Rich's `Markdown` inside a single `Static` widget.

    Visibility is driven by a CSS class (`-expanded`) toggled via a Textual
    reactive `var`. Click handlers are scoped to the header and hint widgets
    (`_SkillToggle`) so clicks on the rendered markdown body do not trigger
    expansion toggles (preserving text selection, for instance).
    """

    DEFAULT_CSS = """
    SkillMessage {
        height: auto;
        padding: 0 1 0 0;
        margin: 1 0 1 0;
        background: transparent;
        border-left: wide $skill;
    }

    SkillMessage .skill-header {
        height: auto;
    }

    SkillMessage .skill-description {
        color: $text-muted;
        margin-left: 3;
    }

    SkillMessage .skill-args {
        margin-left: 3;
        margin-top: 0;
    }

    SkillMessage #skill-md {
        margin-left: 3;
        margin-top: 0;
        padding: 0;
        display: none;
    }

    SkillMessage .skill-hint {
        margin-left: 3;
        color: $text-muted;
    }

    SkillMessage.-expanded #skill-md {
        display: block;
    }

    SkillMessage:hover {
        border-left: wide $skill-hover;
    }
    """

    _PREVIEW_LINES = 4
    _PREVIEW_CHARS = 300

    _expanded: var[bool] = var(False, toggle_class="-expanded")

    def __init__(
        self,
        skill_name: str = "",
        description: str = "",
        source: str = "",
        body: str = "",
        args: str = "",
        *,
        name: str = "",
        content: str = "",
        **kwargs: Any,
    ) -> None:
        """Initialize a skill message.

        Args:
            skill_name: Skill identifier.
            description: Short description of the skill.
            source: Origin label (e.g., `'built-in'`, `'user'`).
            body: Full SKILL.md content (frontmatter included).
            args: User-provided arguments.
            name: Legacy/backward-compatible skill name.
            content: Legacy/backward-compatible body content.
            **kwargs: Additional arguments passed to parent.
        """
        super().__init__(**kwargs)
        self._skill_name = skill_name or name or "skill"
        self._description = description
        self._source = source
        self._body = body or content
        self._stripped_body = _strip_frontmatter(self._body)
        self._args = args
        self._md_widget: Static | None = None
        self._hint_widget: _SkillToggle | None = None
        self._deferred_expanded: bool = False
        self._md_rendered: bool = False

    def compose(self) -> ComposeResult:
        """Compose the skill message layout.

        Yields:
            Widgets for header, description, args, and collapsible body.
        """
        from opscloud.skills.invocation import format_skill_slash_name

        colors = theme.get_theme_colors(self)
        source_tag = f" [{self._source}]" if self._source else ""
        clean_name = format_skill_slash_name(self._skill_name) or self._skill_name
        yield _SkillToggle(
            Content.styled(
                f"/{clean_name}{source_tag}",
                f"bold {colors.skill}",
            ),
            classes="skill-header",
        )
        if self._description:
            yield _SkillToggle(
                Content.styled(self._description, "dim"),
                classes="skill-description",
            )
        if self._args:
            yield Static(
                Content.assemble(
                    ("User request: ", "bold"),
                    self._args,
                ),
                classes="skill-args",
            )
        yield Static("", id="skill-md")
        yield _SkillToggle("", classes="skill-hint", id="skill-hint")

    def on_mount(self) -> None:
        """Cache widget references, render initial state."""
        if is_ascii_mode():
            colors = theme.get_theme_colors(self)
            self.styles.border_left = ("ascii", colors.skill)

        self._md_widget = self.query_one("#skill-md", Static)
        self._hint_widget = self.query_one("#skill-hint", _SkillToggle)

        body = self._stripped_body.strip()
        if body:
            self._prepare_body(body)

        if self._deferred_expanded:
            self._expanded = self._deferred_expanded
            self._deferred_expanded = False

    def _prepare_body(self, body: str) -> None:
        """Set initial hint text. Full body render is deferred to first expand."""
        lines = body.split("\n")
        total_lines = len(lines)
        needs_truncation = (
            total_lines > self._PREVIEW_LINES or len(body) > self._PREVIEW_CHARS
        )

        if needs_truncation:
            remaining = total_lines - self._PREVIEW_LINES
            ellipsis = get_glyphs().ellipsis
            if self._hint_widget:
                self._hint_widget.update(
                    Content.styled(
                        f"{ellipsis} {remaining} more lines"
                        " — click or Ctrl+O to expand",
                        "dim italic",
                    )
                )
        else:
            # Short body — show fully rendered, no preview needed.
            self._ensure_md_rendered(body)
            self._expanded = True

    def _ensure_md_rendered(self, body: str) -> None:
        """Render markdown into the Static widget on first call, then no-op."""
        if self._md_rendered or not self._md_widget:
            return
        try:
            from rich.markdown import Markdown as RichMarkdown

            self._md_widget.update(RichMarkdown(body))
        except Exception:
            logger.warning(
                "Failed to render skill body as markdown; falling back to plain text",
                exc_info=True,
            )
            self._md_widget.update(body)
        self._md_rendered = True

    def toggle_body(self) -> None:
        """Toggle between preview and full body display."""
        if not self._stripped_body.strip():
            return
        self._expanded = not self._expanded

    def watch__expanded(self, expanded: bool) -> None:
        """Lazy-render markdown on first expand; update hint text."""
        body = self._stripped_body.strip()
        if not body:
            return

        if expanded:
            self._ensure_md_rendered(body)

        if not self._hint_widget:
            return

        lines = body.split("\n")
        total_lines = len(lines)
        needs_truncation = (
            total_lines > self._PREVIEW_LINES or len(body) > self._PREVIEW_CHARS
        )

        if not needs_truncation:
            # Short body — always fully visible, no hint needed.
            self._hint_widget.display = False
            return

        if expanded:
            self._hint_widget.update(
                Content.styled("click or Ctrl+O to collapse", "dim italic")
            )
        else:
            remaining = total_lines - self._PREVIEW_LINES
            ellipsis = get_glyphs().ellipsis
            self._hint_widget.update(
                Content.styled(
                    f"{ellipsis} {remaining} more lines — click or Ctrl+O to expand",
                    "dim italic",
                )
            )

    @on(Click, "_SkillToggle")
    def _on_toggle_click(self, event: Click) -> None:
        """Toggle expansion when header or hint is clicked."""
        event.stop()
        if self._stripped_body.strip():
            self.toggle_body()

class ErrorMessage(Static):
    """An error message widget."""

    DEFAULT_CSS = """
    ErrorMessage {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        background: $error-muted;
        color: $text;
        border-left: wide $error;
        pointer: text;
    }
    """

    def __init__(self, content: str, **kwargs: Any) -> None:
        self._raw_content = content
        text = Text()
        text.append("Error: ", style="bold red")
        text.append(content, style="default")
        super().__init__(text, **kwargs)

class SystemMessage(Static):
    """A system/command-response info message.

    - Single-line system messages (e.g. notices, queue notifications) render as
      muted notices with a ``● `` bullet prefix.
    - Multi-line, structured markdown, or command responses render in an elevated
      card container with an accent rail (border-left: wide $primary) and theme colors.
    """

    DEFAULT_CSS = """
    SystemMessage {
        padding: 0 1;
        margin: 1 0 0 0;
        height: auto;
        color: $text;
        pointer: text;
    }

    SystemMessage.-card,
    SystemMessage.-command-output,
    CommandOutputMessage {
        height: auto;
        padding: 1 2;
        margin: 1 0 1 0;
        background: $surface 45%;
        border-left: wide $primary;
        color: $foreground;
        pointer: text;
    }
    """

    def __init__(self, content: str, *, command: str | None = None) -> None:
        self._raw_content = content
        self._timestamp = time.strftime("%H:%M:%S")
        self._show_timestamp = False
        self._markdown_cache: tuple[int, Content] | None = None
        self.command = command
        super().__init__("")
        # Disable auto_links: the Reactive setter avoids the flicker loop caused
        # by Style.__add__ generating a fresh random _link_id on every render.
        self.auto_links = False

        is_multiline = "\n" in content
        is_markdown = any(tok in content for tok in ("```", "**", "__", "#", "- ", "* ", "|", "•", "✓", "⚠", "✗", "1. "))
        if is_multiline or is_markdown or command:
            self.add_class("-card")
            self.add_class("-command-output")

    def _is_command_mode(self) -> bool:
        return self.has_class("-command-output") or self.has_class("-card") or bool(self.command)

    def set_timestamp_visible(self, visible: bool) -> None:
        self._show_timestamp = visible
        self._markdown_cache = None
        self.refresh(layout=True)

    def _build_display_text(self) -> str:
        """Return display text, adding bullet prefix for single-line plain messages."""
        content = self._raw_content
        if self._show_timestamp:
            content = f"{content}  *[{self._timestamp}]*"

        is_multiline = "\n" in content
        is_markdown = any(tok in content for tok in ("```", "**", "__", "#", "- ", "* ", "|", "•", "✓", "⚠", "✗", "1. "))
        already_bulleted = content.startswith("● ") or content.startswith("• ")
        if not already_bulleted and not is_multiline and not is_markdown and not self._is_command_mode():
            content = f"● {content}"

        is_structured_markdown = any(
            line.lstrip().startswith(("#", "-", "*", "1.", "|", ">", "•", "✓", "⚠", "✗"))
            for line in content.splitlines()
        )
        if "```" not in content and "\n" in content and not is_structured_markdown:
            content = "\n".join(
                line if line.endswith("  ") or not line else f"{line}  "
                for line in content.splitlines()
            )
        return content

    def render(self) -> Content:
        """Render message content, laying out markdown to the current widget width.

        Results are cached by width — late-bound style tokens (``dim``, ``bold``,
        ANSI colors) are resolved against the active theme at display time, so
        cached ``Content`` re-colors correctly on a ``/theme`` switch.

        Returns:
            Styled ``Content`` for display.
        """
        width = self.content_size.width
        if width <= 0:
            width = self.container_size.width
            if width <= 0:
                try:
                    width = self.app.size.width
                except Exception:
                    width = 80
        text = self._build_display_text()
        if self._markdown_cache is None or self._markdown_cache[0] != width:
            try:
                console = self.app.console
            except Exception:
                console = None
            is_cmd = self._is_command_mode()
            content = _markdown_to_content(text, width, console, is_command=is_cmd)
            self._markdown_cache = (width, content)
        return self._markdown_cache[1]


class CommandOutputMessage(SystemMessage):
    """A formatted command execution result widget matching OpsCloud TUI aesthetics."""

    DEFAULT_CSS = """
    CommandOutputMessage {
        height: auto;
        padding: 1 2;
        margin: 1 0 1 0;
        background: $surface 45%;
        border-left: wide $primary;
        color: $foreground;
        pointer: text;
    }
    """

    def __init__(self, content: str, *, command: str | None = None) -> None:
        super().__init__(content, command=command)
        self.add_class("-card")
        self.add_class("-command-output")

    def _is_command_mode(self) -> bool:
        return True


AppMessage = SystemMessage


def _format_turn_duration(seconds: float) -> str:
    """Format a turn's wall time compactly (``4.2s``, ``2m 05s``, ``1h 02m``)."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m {secs:02d}s"
    hours, rem = divmod(total, 3600)
    return f"{hours}h {rem // 60:02d}m"


class TurnSummary(Static):
    """One-line footer under a completed agent turn.

    Renders e.g. ``✓ Done in 12.4s · 4 tools · 3.2K tokens · $0.02``.
    The check mark uses the success colour (same health semantic as the
    welcome banner); the metrics are dimmed so the footer never competes
    with the reply itself.  Zero-valued metrics are omitted.
    """

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    TurnSummary {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
        background: transparent;
    }
    """

    def __init__(
        self,
        *,
        elapsed_seconds: float,
        tool_count: int = 0,
        tokens: int = 0,
        cost_usd: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.elapsed_seconds = max(0.0, elapsed_seconds)
        self.tool_count = max(0, tool_count)
        self.tokens = max(0, tokens)
        self.cost_usd = max(0.0, cost_usd)

    def summary_text(self) -> str:
        """Plain-text form of the footer (used for rendering and tests)."""
        from opscloud.utils.session_stats import format_cost, format_token_count

        glyphs = get_glyphs()
        parts = [f"Done in {_format_turn_duration(self.elapsed_seconds)}"]
        if self.tool_count:
            parts.append(f"{self.tool_count} tool{'s' if self.tool_count != 1 else ''}")
        if self.tokens:
            parts.append(f"{format_token_count(self.tokens)} tokens")
        if self.cost_usd > 0:
            parts.append(format_cost(self.cost_usd))
        return f" {glyphs.separator} ".join(parts)

    def render(self) -> Content:
        glyphs = get_glyphs()
        return Content.assemble(
            (f"{glyphs.checkmark} ", "bold $success"),
            (self.summary_text(), "dim"),
        )


def compute_wheel_speed(streak: int, viewport_height: int = 0) -> int:
    """Compute sub-linear scroll lines per wheel event.

    Uses ``min(12, 1 + isqrt(streak))`` clamped to ``max(1, viewport_height // 3)``
    when ``viewport_height > 0``.
    """
    import math

    speed = min(12, 1 + math.isqrt(max(0, streak)))
    if viewport_height > 0:
        speed = min(speed, max(1, viewport_height // 3))
    return max(1, speed)


class MessageList(VerticalScroll):
    """Scrollable list of conversation messages with auto-scroll lock controls."""

    DEFAULT_CSS = """
    MessageList {
        height: 1fr;
    }
    """

    _WHEEL_STREAK_WINDOW_S: ClassVar[float] = 0.150
    """Max interval (150ms) between same-direction wheel events to build streak."""

    _SCROLL_COALESCE_INTERVAL_S: ClassVar[float] = 0.016
    """16ms (60fps) frame budget for coalescing burst wheel scroll deltas."""

    _HOVER_SUPPRESS_WINDOW_S: ClassVar[float] = 0.120
    """Suppress post-refresh hover hit-testing while actively scrolling."""

    _STICKY_BOTTOM_THRESHOLD: ClassVar[float] = 2.0
    """Distance from max_scroll_y within which downward scroll re-locks auto-scroll."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._current_assistant: AssistantMessage | None = None
        self._current_thinking: ThinkingMessage | None = None
        self._accumulated_thinking: str = ""
        self._tool_calls: dict[str, ToolCallMessage] = {}
        self._tool_call_total: int = 0
        """Monotonic count of tool-call widgets created (per-turn deltas)."""
        self._active_tool_group: ToolGroupSummary | None = None
        self._auto_scroll_locked = True
        self._scroll_end_pending = False
        self._last_wheel_time: float = 0.0
        self._last_wheel_dir: int = 0
        self._wheel_streak: int = 0
        self._pending_scroll_delta: int = 0
        self._last_scroll_flush_time: float = 0.0
        self._scroll_flush_timer: Any = None
        self._hover_settle_timer: Any = None

    def on_unmount(self) -> None:
        """Stop any pending scroll coalescer or hover settle timers."""
        self._stop_scroll_timers()

    def _stop_scroll_timers(self) -> None:
        if self._scroll_flush_timer is not None:
            self._scroll_flush_timer.stop()
            self._scroll_flush_timer = None
        if self._hover_settle_timer is not None:
            self._hover_settle_timer.stop()
            self._hover_settle_timer = None

    def _mount_child(self, widget: Widget, before: Widget | None = None) -> None:
        """Mount child widget, ensuring it mounts before the bottom loading widget if active."""
        if before is not None:
            self.mount(widget, before=before)
            return
        loading = getattr(self.app, "_loading_widget", None) if self.is_attached and self.app else None
        if (
            loading is not None
            and getattr(loading, "is_attached", False)
            and loading.parent is self
            and widget is not loading
        ):
            self.mount(widget, before=loading)
        else:
            self.mount(widget)

    def on_scroll_up(self) -> None:
        """Suspend auto-scroll when user manually scrolls up."""
        self._auto_scroll_locked = False
        if self._anchored and not self._anchor_released:
            self.release_anchor()

    def _mark_user_scrolling(self) -> None:
        """Suppress post-refresh hover hit-testing while a scroll gesture is active."""
        if not self.is_attached or self.app is None:
            return
        setattr(
            self.app,
            "_opscloud_scroll_suppress_hover_until",
            time.monotonic() + self._HOVER_SUPPRESS_WINDOW_S,
        )
        if self._hover_settle_timer is not None:
            self._hover_settle_timer.stop()
        self._hover_settle_timer = self.set_timer(
            self._HOVER_SUPPRESS_WINDOW_S, self._on_scroll_hover_settled
        )

    def _on_scroll_hover_settled(self) -> None:
        """Restore hover hit-testing once the scroll gesture settles."""
        self._hover_settle_timer = None
        if not self.is_attached or self.app is None:
            return
        setattr(self.app, "_opscloud_scroll_suppress_hover_until", 0.0)
        try:
            self.app._update_mouse_over(self.screen)
        except Exception:
            pass

    def _next_wheel_speed(self, direction: int, now: float | None = None) -> int:
        """Compute accelerated wheel scroll lines for ``direction`` (-1 up, +1 down)."""
        ts = time.monotonic() if now is None else now
        if (
            self._last_wheel_dir == direction
            and (ts - self._last_wheel_time) < self._WHEEL_STREAK_WINDOW_S
        ):
            self._wheel_streak += 1
        else:
            self._wheel_streak = 0
        self._last_wheel_time = ts
        self._last_wheel_dir = direction
        vh = self.scrollable_content_region.height if self.is_attached else 0
        return compute_wheel_speed(self._wheel_streak, vh)

    def _queue_wheel_scroll(self, delta: int, now: float | None = None) -> None:
        """Coalesce wheel deltas within a 16ms frame budget."""
        if delta == 0:
            return
        ts = time.monotonic() if now is None else now
        if delta < 0:
            self.on_scroll_up()
        self._mark_user_scrolling()
        if (self._pending_scroll_delta > 0 and delta < 0) or (
            self._pending_scroll_delta < 0 and delta > 0
        ):
            self._pending_scroll_delta = delta
        else:
            self._pending_scroll_delta += delta

        if not self.is_attached:
            self._flush_pending_scroll()
            return

        if (
            self._scroll_flush_timer is None
            and (ts - self._last_scroll_flush_time) >= self._SCROLL_COALESCE_INTERVAL_S
        ):
            self._flush_pending_scroll()
        elif self._scroll_flush_timer is None:
            self._scroll_flush_timer = self.set_timer(
                self._SCROLL_COALESCE_INTERVAL_S, self._flush_pending_scroll
            )

    def _flush_pending_scroll(self) -> None:
        """Flush accumulated wheel delta in a single immediate compositor scroll."""
        if self._scroll_flush_timer is not None:
            self._scroll_flush_timer.stop()
            self._scroll_flush_timer = None
        delta = self._pending_scroll_delta
        self._pending_scroll_delta = 0
        if delta == 0:
            return
        self._last_scroll_flush_time = time.monotonic()
        target_y = max(0.0, min(float(self.max_scroll_y), float(self.scroll_y) + delta))
        self.scroll_to(y=target_y, animate=False, immediate=True)
        if delta > 0 and (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = True
        elif delta < 0 and (self.max_scroll_y - self.scroll_y) > self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = False

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        """Handle mouse/trackpad scroll up with WheelSpeed + 16ms coalescing."""
        if not self.allow_vertical_scroll:
            return
        self.on_scroll_up()
        if self.scroll_y > 0 or self._pending_scroll_delta > 0:
            speed = self._next_wheel_speed(-1)
            self._queue_wheel_scroll(-speed)
            event.stop()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        """Handle mouse/trackpad scroll down with WheelSpeed + 16ms coalescing."""
        if not self.allow_vertical_scroll:
            return
        if self.scroll_y < self.max_scroll_y or self._pending_scroll_delta < 0:
            speed = self._next_wheel_speed(1)
            self._queue_wheel_scroll(speed)
            event.stop()
        else:
            self._auto_scroll_locked = True

    def action_scroll_up(self) -> None:
        """Scroll up one line immediately and unlock auto-scroll."""
        if not self.allow_vertical_scroll:
            return
        self.on_scroll_up()
        self._mark_user_scrolling()
        self.scroll_up(animate=False, immediate=True)

    def action_scroll_down(self) -> None:
        """Scroll down one line immediately and re-lock if at bottom."""
        if not self.allow_vertical_scroll:
            return
        self._mark_user_scrolling()
        self.scroll_down(animate=False, immediate=True)
        if (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = True

    def action_page_up(self) -> None:
        """Scroll one page up without animation jitter and unlock auto-scroll."""
        if not self.allow_vertical_scroll:
            return
        self.on_scroll_up()
        self._mark_user_scrolling()
        self.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        """Scroll one page down without animation jitter and re-lock at bottom."""
        if not self.allow_vertical_scroll:
            return
        self._mark_user_scrolling()
        self.scroll_page_down(animate=False)
        if (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
            self._auto_scroll_locked = True

    def action_scroll_home(self) -> None:
        """Jump to top of transcript and unlock auto-scroll."""
        if not self._allow_scroll:
            return
        self.on_scroll_up()
        self._mark_user_scrolling()
        self.scroll_home(animate=False, immediate=True, x_axis=False)

    def action_scroll_end(self) -> None:
        """Jump to bottom of transcript and re-lock auto-scroll."""
        if not self._allow_scroll:
            return
        self._auto_scroll_locked = True
        self._mark_user_scrolling()
        self.scroll_end(animate=False, immediate=True, x_axis=False)

    def _on_scroll_up(self, event: Any) -> None:
        """Handle scrollbar track page-up click."""
        if self.allow_vertical_scroll:
            self.on_scroll_up()
            self._mark_user_scrolling()
            self.scroll_page_up(animate=False)
            event.stop()

    def _on_scroll_down(self, event: Any) -> None:
        """Handle scrollbar track page-down click."""
        if self.allow_vertical_scroll:
            self._mark_user_scrolling()
            self.scroll_page_down(animate=False)
            if (self.max_scroll_y - self.scroll_y) <= self._STICKY_BOTTOM_THRESHOLD:
                self._auto_scroll_locked = True
            event.stop()

    def _on_scroll_to(self, message: Any) -> None:
        """Handle scrollbar thumb drag without 0.1s animation lag."""
        if self._allow_scroll:
            self._mark_user_scrolling()
            if message.y is not None:
                if message.y < self.scroll_y:
                    self.on_scroll_up()
                elif (self.max_scroll_y - message.y) <= self._STICKY_BOTTOM_THRESHOLD:
                    self._auto_scroll_locked = True
            self.scroll_to(message.x, message.y, animate=False, immediate=True)
            message.stop()

    async def mount_inline_prompt(self, widget: Widget) -> None:
        """Mount an inline prompt widget into the message transcript area."""
        loading = getattr(self.app, "_loading_widget", None) if self.is_attached and self.app else None
        if (
            loading is not None
            and getattr(loading, "is_attached", False)
            and loading.parent is self
            and widget is not loading
        ):
            await self.mount(widget, before=loading)
        else:
            await self.mount(widget)
        if self._auto_scroll_locked:
            self._scroll_to_end()

    def add_thinking_message(
        self, content: str = "", duration_seconds: float = 0.0, *, live: bool = False
    ) -> ThinkingMessage:
        """Add a thinking message widget to the conversation."""
        self.close_active_tool_group()
        msg = ThinkingMessage(content=content, duration_seconds=duration_seconds, live=live)
        self._mount_child(msg)
        if self._auto_scroll_locked:
            self._scroll_to_end()
        return msg

    def append_thinking_token(self, token: str, duration_seconds: float = 0.0) -> None:
        """Append a token to the current (live) thinking message."""
        if self._current_thinking is None:
            self._current_thinking = self.add_thinking_message("", duration_seconds, live=True)
            self._accumulated_thinking = ""
        self._accumulated_thinking += token
        self._current_thinking.update_thinking(self._accumulated_thinking, duration_seconds)
        if self._auto_scroll_locked:
            self._scroll_to_end()

    def _end_thinking(self) -> None:
        """Collapse the live thinking block, if any.

        Called when reply text, a tool call or the end of the turn arrives,
        so later reasoning in the same turn starts a fresh block below.
        """
        current = self._current_thinking
        self._current_thinking = None
        if current is not None:
            try:
                current.finish()
            except Exception:
                logger.exception("Failed to finish thinking block")

    def close_active_tool_group(self) -> None:
        """Close the active inline tool group so subsequent tool calls form a new group."""
        group = self._active_tool_group
        self._active_tool_group = None
        if group is not None and getattr(group, "is_attached", False):
            try:
                group.close()
            except Exception:
                logger.exception("Failed to close active tool group")

    def reveal_pending_tool_calls(self) -> None:
        """Surface grouped tool calls before asking for approval."""
        group = self._active_tool_group
        self.close_active_tool_group()
        if group is None or not getattr(group, "is_attached", False):
            return
        try:
            group.reveal_pending()
        except Exception:
            logger.exception("Failed to reveal pending tool calls")

    async def regroup_completed_tools(self) -> None:
        """Fold runs of completed tool calls into collapsible group summaries.

        Scans the messages container for maximal runs of consecutive,
        successfully-completed tool calls and inserts a ToolGroupSummary that
        collapses each run into a single dim line.
        """
        self.close_active_tool_group()

        run_tools: list[ToolCallMessage] = []
        run_collapsible: list[Widget] = []
        run_anchor: Widget | None = None
        folded_runs_count = 0

        async def flush() -> None:
            nonlocal run_tools, run_collapsible, run_anchor, folded_runs_count
            if run_tools and run_anchor is not None and getattr(run_anchor, "is_attached", False):
                summary = ToolGroupSummary(
                    tools=list(run_tools),
                    collapsible=list(run_collapsible),
                    live=False,
                )
                for widget in run_collapsible:
                    widget.add_class("-grouped")
                try:
                    await self.mount(summary, before=run_anchor)
                    folded_runs_count += 1
                except Exception:
                    logger.warning("Failed to mount tool group summary", exc_info=True)
            run_tools = []
            run_collapsible = []
            run_anchor = None

        from contextlib import nullcontext
        batch_ctx = self.app.batch_update() if (self.is_attached and self.app is not None) else nullcontext()
        with batch_ctx:
            for child in list(self.children):
                if isinstance(child, ToolGroupSummary):
                    await flush()
                    if not child._finalized:
                        child.close()
                    continue
                if isinstance(child, ToolCallMessage):
                    groupable = (
                        child.tool_name not in _TOOL_GROUP_EXCLUSIONS
                        and child.is_success
                        and not child.has_class("-grouped")
                    )
                    if not groupable:
                        await flush()
                        continue
                    if run_anchor is None:
                        run_anchor = child
                    run_tools.append(child)
                    run_collapsible.append(child)
                    continue
                await flush()
            await flush()

        logger.debug("TUI: MessageList.regroup_completed_tools folded %d runs", folded_runs_count)

    def add_user_message(self, text: str) -> None:
        """Add a user message and reset auto-scroll."""
        self._auto_scroll_locked = True
        self.close_active_tool_group()
        self._mount_child(UserMessage(text))
        self._scroll_to_end()

    def add_queued_user_message(self, text: str) -> QueuedUserMessage:
        """Add a queued user message."""
        msg = QueuedUserMessage(text)
        self._mount_child(msg)
        self._scroll_to_end()
        return msg

    def remove_queued_user_messages(self) -> None:
        """Remove any QueuedUserMessage widgets currently in the list."""
        for msg in self.query(QueuedUserMessage):
            msg.remove()

    def start_assistant_message(self) -> None:
        """Start a streaming assistant message."""
        self.close_active_tool_group()
        msg = AssistantMessage()
        self._current_assistant = msg
        self._mount_child(msg)
        self._scroll_to_end()

    def append_assistant_token(self, token: str) -> None:
        """Append a token to current assistant message."""
        self._end_thinking()  # Collapse thinking when reply text starts
        if self._current_assistant is None:
            self.start_assistant_message()
        if self._current_assistant is not None:
            self._current_assistant.append_token(token)

    def update_assistant_message(self, text: str) -> None:
        """Replace current assistant message text with full state."""
        self._end_thinking()
        if self._current_assistant is None:
            self.start_assistant_message()
        if self._current_assistant is not None:
            self._current_assistant.update_text(text)
            if self._auto_scroll_locked:
                self._scroll_to_end()

    def finish_assistant_message(self) -> None:
        """Finalize assistant message."""
        self._end_thinking()
        if self._current_assistant:
            self._current_assistant.finish()
            self._current_assistant = None
            self._scroll_to_end()

    def add_tool_call(
        self, name: str, call_id: str, args: dict[str, Any], live: bool = True
    ) -> None:
        """Add a tool-call widget."""
        self._end_thinking()
        msg = ToolCallMessage(name, args)
        self._tool_call_total += 1
        if live:
            msg.set_running()
        self._tool_calls[call_id] = msg
        from contextlib import nullcontext
        batch_ctx = self.app.batch_update() if (self.is_attached and self.app is not None) else nullcontext()
        with batch_ctx:
            if live and name not in _TOOL_GROUP_EXCLUSIONS:
                if self._active_tool_group is None or not getattr(self._active_tool_group, "is_attached", False):
                    self._active_tool_group = ToolGroupSummary(live=True)
                    self._mount_child(self._active_tool_group)
                msg.add_class("-grouped")
                msg.display = False
                self._active_tool_group.add_member(msg)
            self._mount_child(msg)
        self._scroll_to_end()

    def update_tool_result(
        self, call_id: str, result: str, success: bool = True, name: str | None = None, live: bool = True
    ) -> None:
        """Update a tool-call widget with its result."""
        if call_id and call_id in self._tool_calls:
            msg = self._tool_calls[call_id]
            if success:
                msg.set_success(result)
            else:
                msg.set_error(result)
            for child in list(self.children):
                if isinstance(child, ToolGroupSummary) and msg in child._tools:
                    child.sync_members()
        else:
            msg_name = name if (name and name != "None") else "tool"
            msg = ToolCallMessage(msg_name, {})
            self._tool_call_total += 1
            if call_id:
                self._tool_calls[call_id] = msg
            from contextlib import nullcontext
            batch_ctx = self.app.batch_update() if (self.is_attached and self.app is not None) else nullcontext()
            with batch_ctx:
                if live and msg_name not in _TOOL_GROUP_EXCLUSIONS:
                    if self._active_tool_group is None or not getattr(self._active_tool_group, "is_attached", False):
                        self._active_tool_group = ToolGroupSummary(live=True)
                        self._mount_child(self._active_tool_group)
                    msg.add_class("-grouped")
                    msg.display = False
                    self._active_tool_group.add_member(msg)
                self._mount_child(msg)
                if success:
                    msg.set_success(result)
                else:
                    msg.set_error(result)
                if self._active_tool_group is not None and msg in self._active_tool_group._tools:
                    self._active_tool_group.sync_members()
        self._scroll_to_end()

    def add_skill_message(self, name: str, content: str) -> None:
        """Add a skill loaded card."""
        self._mount_child(SkillMessage(skill_name=name, body=content))
        self._scroll_to_end()

    def add_diff_message(self, patch: str, file_path: str = "") -> None:
        """Add an inline diff message."""
        self._mount_child(DiffMessage(patch, file_path))
        self._scroll_to_end()

    def add_error_message(self, text: str) -> None:
        """Add an error message."""
        self._mount_child(ErrorMessage(text))
        self._scroll_to_end()

    def add_system_message(self, text: str) -> None:
        """Add a system/info message."""
        self._mount_child(SystemMessage(text))
        self._scroll_to_end()

    def clear(self) -> None:
        """Clear all child widgets."""
        self._stop_scroll_timers()
        self._pending_scroll_delta = 0
        self._scroll_end_pending = False
        self._auto_scroll_locked = True
        self.remove_children()
        self._current_assistant = None
        self._current_thinking = None
        self._tool_calls.clear()
        self._active_tool_group = None

    def set_timestamps_visible(self, visible: bool) -> None:
        """Show or hide timestamp footers across all mounted messages."""
        self._timestamps_visible = visible
        for child in self.children:
            fn = getattr(child, "set_timestamp_visible", None)
            if callable(fn):
                fn(visible)

    def _scroll_to_end(self) -> None:
        """Scroll to the end if auto-scroll is locked, coalesced per refresh frame."""
        if not self._auto_scroll_locked:
            return
        if not self.is_attached:
            self.scroll_end(animate=False, x_axis=False)
            return
        if self._scroll_end_pending:
            return
        self._scroll_end_pending = True

        def _do_scroll_end() -> None:
            self._scroll_end_pending = False
            if self._auto_scroll_locked and self.is_attached:
                self.scroll_end(animate=False, immediate=True, x_axis=False)

        self.call_after_refresh(_do_scroll_end)

_THINKING_HEADING_RE = re.compile(r"^\s*(?:\*\*(.+?)\*\*|#{1,6}\s+(.+?))\s*$")
"""Matches a reasoning-summary heading line (``**Title**`` or ``## Title``)."""


def _thinking_heading(line: str) -> str | None:
    """Return the heading text if ``line`` is a reasoning heading, else None."""
    match = _THINKING_HEADING_RE.match(line)
    if not match:
        return None
    return (match.group(1) or match.group(2) or "").strip() or None


def _clean_thinking_line(line: str) -> str:
    """Strip light markdown from a reasoning line for one-line display."""
    heading = _thinking_heading(line)
    if heading:
        return heading
    return line.strip().replace("**", "").replace("`", "")


class ThinkingMessage(Static):
    """Model reasoning, shown in two states.

    * **Live** (tokens streaming): an animated spinner, ``Thinking · 4s`` and
      a one-line preview of the latest reasoning line.
    * **Done**: a single dim ``▸ Thought for 6s · <topic>`` line, where the
      topic is the first reasoning-summary heading.  Click (or Ctrl+O) to
      expand the full reasoning, indented under the header.

    The widget measures its own duration per thinking block; the adapter's
    turn-scoped timer would otherwise over-count later blocks in a turn.
    """

    DEFAULT_CSS = """
    ThinkingMessage {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
        pointer: pointer;
    }
    ThinkingMessage:hover {
        background: $foreground 4%;
    }
    """

    _TICK_SECONDS: ClassVar[float] = 0.1

    def __init__(
        self,
        content: str = "",
        duration_seconds: float = 0.0,
        *,
        live: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._content = content
        self._duration_seconds = max(0.0, duration_seconds)
        self._expanded = False
        self._live = live
        self._started = time.monotonic()
        self._frame = 0
        self._timer: Any = None

    # ── State ────────────────────────────────────────────

    @property
    def is_live(self) -> bool:
        """Whether reasoning tokens are still streaming into this widget."""
        return self._live

    @property
    def topic(self) -> str | None:
        """First reasoning-summary heading, used as the collapsed topic."""
        for line in self._content.splitlines():
            heading = _thinking_heading(line)
            if heading:
                return heading
        return None

    @property
    def preview(self) -> str:
        """Latest reasoning paragraph, flattened to one line.

        Source text is often hard-wrapped, so the last *line* is usually a
        sentence fragment; the last *paragraph* reads naturally.
        """
        paragraphs = re.split(r"\n\s*\n", self._content.strip())
        for para in reversed(paragraphs):
            words = " ".join(
                _clean_thinking_line(line) for line in para.splitlines() if line.strip()
            ).strip()
            if words:
                return words
        return ""

    def update_thinking(self, content: str, duration_seconds: float | None = None) -> None:
        """Replace the accumulated reasoning text.

        ``duration_seconds`` is honoured only for non-live widgets (history
        replay); live widgets time themselves.
        """
        self._content = content
        if duration_seconds is not None and not self._live:
            self._duration_seconds = max(0.0, duration_seconds)
        if self.is_attached:
            self.refresh(layout=True)

    def finish(self) -> None:
        """End the live state and collapse to ``Thought for Ns``."""
        if not self._live:
            return
        self._live = False
        self._duration_seconds = time.monotonic() - self._started
        self._stop_timer()
        if self.is_attached:
            self.refresh(layout=True)

    def toggle_details(self) -> None:
        """Expand/collapse the full reasoning (bound to click and Ctrl+O)."""
        if self._live or not self._content.strip():
            return
        self._expanded = not self._expanded
        self.refresh(layout=True)

    # ── Lifecycle ────────────────────────────────────────

    def on_mount(self) -> None:
        if self._live:
            self._timer = self.set_interval(self._TICK_SECONDS, self._tick)

    def on_unmount(self) -> None:
        self._stop_timer()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self._frame += 1
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        """Toggle expand/collapse on click."""
        event.stop()
        self.toggle_details()

    # ── Rendering ────────────────────────────────────────

    def _truncate(self, text: str, reserve: int = 0, *, keep_tail: bool = False) -> str:
        width = (self.size.width or 100) - reserve
        width = max(20, width)
        if len(text) <= width:
            return text
        ellipsis = get_glyphs().ellipsis
        if keep_tail:
            tail = text[-(width - 1):]
            head, sep, rest = tail.partition(" ")
            if sep and len(head) < 20:
                tail = rest  # start the ticker on a whole word
            return ellipsis + tail.lstrip()
        return text[: width - 1].rstrip() + ellipsis

    def render(self) -> Content:
        glyphs = get_glyphs()
        sep = glyphs.separator

        if self._live:
            frames = glyphs.spinner_frames
            frame = frames[self._frame % len(frames)]
            elapsed = int(time.monotonic() - self._started)
            parts: list[tuple[str, str] | str] = [
                (f"{frame} ", "$primary"),
                ("Thinking", "bold $primary"),
                (f" {sep} {elapsed}s", "dim"),
            ]
            preview = self.preview
            if preview:
                parts.append("\n")
                parts.append(("  " + self._truncate(preview, reserve=2, keep_tail=True), "dim italic"))
            return Content.assemble(*parts)

        arrow = glyphs.disclosure_expanded if self._expanded else glyphs.disclosure_collapsed
        dur = int(round(self._duration_seconds))
        label = f"Thought for {dur}s" if dur > 0 else "Thought"
        parts = [(f"{arrow} ", "dim"), (label, "dim bold")]
        topic = self.topic
        if topic and not self._expanded:
            reserve = len(label) + len(sep) + 6
            parts.append((f" {sep} {self._truncate(topic, reserve=reserve)}", "dim italic"))

        if self._expanded and self._content.strip():
            parts.append("\n")
            for line in self._content.strip().splitlines():
                heading = _thinking_heading(line)
                if heading:
                    parts.append((f"\n  {heading}", "bold dim"))
                else:
                    parts.append((f"\n  {line.strip()}", "dim"))
        return Content.assemble(*parts)

class QueuedUserMessage(Static):
    """A user message waiting in the queue: dim prompt with an amber tag."""

    DEFAULT_CSS = """
    QueuedUserMessage {
        height: auto;
        padding: 0 1;
        margin: 1 0 0 0;
        background: transparent;
        border-left: wide $warning 45%;
        color: $text-muted;
    }
    """

    def __init__(self, content: str) -> None:
        self._raw_content = content
        super().__init__("")

    def render(self) -> Content:
        """Render the queued message with live theme colors."""
        return Content.assemble(
            ("❯ ", "dim"),
            (self._raw_content, "italic dim"),
            ("  queued", "italic $warning"),
        )


class _RubricResultToggle(Static):
    """Clickable element inside a rubric result widget."""

    can_focus = False


class RubricResultMessage(Vertical):
    """Compact grader result with complete, scrollable details on demand."""

    class ExpansionChanged(Message):
        """Posted when the grader-details expansion state changes."""

        def __init__(self, widget: RubricResultMessage, expanded: bool) -> None:
            super().__init__()
            self.widget = widget
            self.expanded = expanded

    DEFAULT_CSS = """
    RubricResultMessage {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        color: $text-muted;
        border-left: wide $warning;
    }

    RubricResultMessage .rubric-result-summary {
        height: auto;
    }

    RubricResultMessage .rubric-result-details-scroll {
        display: none;
        height: auto;
        max-height: 16;
        margin: 1 0 0 2;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
    }

    RubricResultMessage .rubric-result-details {
        height: auto;
        padding: 0;
    }

    RubricResultMessage .rubric-result-hint {
        height: auto;
        margin-left: 2;
        color: $text-muted;
    }

    RubricResultMessage.-expanded .rubric-result-details-scroll {
        display: block;
    }
    """

    _expanded: var[bool] = var(False, toggle_class="-expanded")

    def __init__(
        self,
        summary: str,
        details: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._summary = summary
        self._details = details
        self._hint_widget: _RubricResultToggle | None = None

    def compose(self) -> ComposeResult:
        yield _RubricResultToggle(
            Content.styled(self._summary, "dim italic"),
            classes="rubric-result-summary",
        )
        with VerticalScroll(classes="rubric-result-details-scroll"):
            yield Static(
                Content(self._details),
                classes="rubric-result-details",
            )
        yield _RubricResultToggle("", classes="rubric-result-hint")

    def on_mount(self) -> None:
        self._hint_widget = self.query_one(
            ".rubric-result-hint",
            _RubricResultToggle,
        )
        if not self._details:
            self._hint_widget.display = False
            return
        self._update_hint()

    def toggle_details(self) -> None:
        if self._details:
            self._expanded = not self._expanded

    def watch__expanded(self, expanded: bool) -> None:
        self._update_hint()
        if self.is_attached:
            self.post_message(self.ExpansionChanged(self, expanded))

    def _update_hint(self) -> None:
        if self._hint_widget is None or not self._details:
            return
        action = "hide" if self._expanded else "show"
        self._hint_widget.update(
            Content.styled(f"click or Ctrl+O to {action} details", "dim italic")
        )

    @on(Click, "_RubricResultToggle")
    def _on_toggle_click(self, event: Click) -> None:
        event.stop()
        self.toggle_details()