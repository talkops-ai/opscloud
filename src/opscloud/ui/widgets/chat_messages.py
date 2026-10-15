"""Conversational message widgets: UserMessage, QueuedUserMessage, AssistantMessage, DiffMessage, ThinkingMessage.

Extracted from `opscloud.ui.widgets.messages` (Phase 5 UI modularization).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from textual import events
from textual.containers import Vertical
from textual.content import Content
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Static

from opscloud.config.settings import get_glyphs
from opscloud.ui import theme
from opscloud.ui.widgets.diff import format_file_diff_header, render_diff_content
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widgets import Markdown

logger = get_logger(__name__)

_USER_MSG_MAX_DISPLAY_CHARS = 10_000
_USER_MSG_TRUNCATE_HEAD_CHARS = 2_500
_USER_MSG_TRUNCATE_TAIL_CHARS = 2_500
_PROMPT_PREFIX_WIDTH = 2


def _mode_color(mode: str | None, widget_or_app: object | None = None) -> str:
    """Return the hex color string for a mode, falling back to primary."""
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


def _strip_prompt_prefix(
    result: tuple[str, str] | None,
    selection: Selection,
) -> tuple[str, str] | None:
    """Drop the leading prompt prefix glyph from a selected range."""
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
    """Select the user message body without its decorative prompt glyph."""
    widget.screen.selections = {  # ty: ignore[invalid-assignment]
        widget: Selection(Offset(_PROMPT_PREFIX_WIDTH, 0), None),
    }


def _will_collapse(text: str) -> bool:
    """Return whether `text` exceeds the transcript display threshold."""
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
    """Collapse a very long user message for transcript display."""
    if not _will_collapse(text):
        return _UserMessageFull(text=text)
    hidden_start = _USER_MSG_TRUNCATE_HEAD_CHARS
    hidden_end = len(text) - _USER_MSG_TRUNCATE_TAIL_CHARS
    return _UserMessageCollapsed(
        head=text[:_USER_MSG_TRUNCATE_HEAD_CHARS],
        tail=text[-_USER_MSG_TRUNCATE_TAIL_CHARS:],
        hidden_lines=text.count("\n", hidden_start, hidden_end),
        hidden_chars=hidden_end - hidden_start,
    )


def _truncate_for_display(text: str) -> str:
    """Truncate very long user message text for display in the conversation."""
    return _collapse_user_message(text).text


class UserMessage(Static):
    """A user message styled by input mode."""

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
        """Build the styled segments for the current mode and theme."""
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
            body = content[len(MODE_PREFIXES[mode]) :]
            if mode == "shell":
                return [("$ ", "bold $mode-bash"), body]
            return [
                ("$ ", "bold $mode-incognito"),
                body,
                ("  incognito", "italic dim"),
            ]

        return [("❯ ", "bold $accent"), content]

    def render(self) -> Content:
        """Render the styled user message with live theme colors."""
        parts = self._parts()
        if self._show_timestamp:
            parts.append((f"  [{self._timestamp}]", "dim italic"))
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


_ASSISTANT_MARKDOWN_CLS: type[Markdown] | None = None
"""Lazily-built ``Markdown`` subclass used for assistant replies."""


def _assistant_markdown_class() -> type[Markdown]:
    """Return the ``Markdown`` subclass used inside ``AssistantMessage``."""
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
    """Widget displaying an assistant message with markdown support."""

    _STREAM_FLUSH_INTERVAL: ClassVar[float] = 0.1

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

    AssistantMessage MarkdownBullet {
        color: $primary;
    }

    AssistantMessage Markdown MarkdownBlock > .code_inline {
        color: $text-warning;
        background: $warning 8%;
    }

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

    AssistantMessage MarkdownBlockQuote {
        background: transparent;
        border-left: outer $primary 60%;
        color: $text-muted;
        text-style: italic;
        margin: 1 0;
        padding: 0 1;
    }

    AssistantMessage MarkdownTableContent {
        keyline: thin $foreground 14%;
    }

    AssistantMessage MarkdownTableContent > .header {
        color: $primary;
        text-style: bold;
    }

    AssistantMessage MarkdownHorizontalRule {
        border-bottom: solid $foreground 18%;
    }
    """

    def __init__(self, content: str = "", **kwargs: Any) -> None:
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
        if self._markdown is None:
            from textual.widgets import Markdown

            self._markdown = self.query_one("#assistant-content", Markdown)
        return self._markdown

    def _ensure_stream(self) -> Any:
        if self._stream is None:
            from textual.widgets import Markdown

            self._stream = Markdown.get_stream(self._get_markdown())
        return self._stream

    def append_token(self, token: str) -> None:
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
        if not self._pending_append:
            return
        pending = self._pending_append
        self._pending_append = ""
        try:
            stream = self._ensure_stream()
            await stream.write(pending)
            parent = self.parent
            if parent is not None and getattr(parent, "_auto_scroll_locked", False):
                scroll_end = getattr(parent, "_scroll_to_end", None)
                if callable(scroll_end):
                    scroll_end()
                    parent.call_after_refresh(scroll_end)
        except Exception:
            self._pending_append = pending + self._pending_append
            logger.exception("Failed to flush streamed markdown fragment")

    def _stop_flush_timer(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None

    def update_text(self, text: str) -> None:
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
        self._is_streaming = False
        self._stop_flush_timer()
        if not self._content_parts or not self._content.strip():
            self.remove()
        else:
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
                parent = self.parent
                if parent is not None and getattr(parent, "_auto_scroll_locked", False):
                    scroll_end = getattr(parent, "_scroll_to_end", None)
                    if callable(scroll_end):
                        scroll_end()

            self.run_worker(_finalize(), exclusive=True, thread=False)

    async def on_markdown_link_clicked(self, event: Any) -> None:
        event.stop()
        from opscloud.ui.widgets._links import open_link_or_file_async

        href = getattr(event, "href", "")
        if href and self.app is not None:
            await open_link_or_file_async(href, app=self.app, notify_on_success=True)

    async def on_click(self, event: Any) -> None:
        from opscloud.editor import extract_file_path, open_file_in_editor
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
        additions = sum(
            1 for ln in lines if ln.startswith("+") and not ln.startswith("+++")
        )
        deletions = sum(
            1 for ln in lines if ln.startswith("-") and not ln.startswith("---")
        )
        header = format_file_diff_header(
            file_path or "Diff", additions=additions, deletions=deletions
        )
        body, _ = render_diff_content(patch_or_diff, max_lines=None, show_stats=False)
        super().__init__(Content.assemble(header, "\n", body))


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
    """Model reasoning widget supporting live streaming and collapsed summary states."""

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

    @property
    def is_live(self) -> bool:
        return self._live

    @property
    def topic(self) -> str | None:
        for line in self._content.splitlines():
            heading = _thinking_heading(line)
            if heading:
                return heading
        return None

    @property
    def preview(self) -> str:
        paragraphs = re.split(r"\n\s*\n", self._content.strip())
        for para in reversed(paragraphs):
            words = " ".join(
                _clean_thinking_line(line) for line in para.splitlines() if line.strip()
            ).strip()
            if words:
                return words
        return ""

    def update_thinking(
        self, content: str, duration_seconds: float | None = None
    ) -> None:
        self._content = content
        if duration_seconds is not None and not self._live:
            self._duration_seconds = max(0.0, duration_seconds)
        if self.is_attached:
            self.refresh(layout=True)

    def finish(self) -> None:
        if not self._live:
            return
        self._live = False
        self._duration_seconds = time.monotonic() - self._started
        self._stop_timer()
        if self.is_attached:
            self.refresh(layout=True)

    def toggle_details(self) -> None:
        if self._live or not self._content.strip():
            return
        self._expanded = not self._expanded
        self.refresh(layout=True)

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
        event.stop()
        self.toggle_details()

    def _truncate(
        self, text: str, reserve: int = 0, *, keep_tail: bool = False
    ) -> str:
        width = (self.size.width or 100) - reserve
        width = max(20, width)
        if len(text) <= width:
            return text
        ellipsis = get_glyphs().ellipsis
        if keep_tail:
            tail = text[-(width - 1) :]
            head, sep, rest = tail.partition(" ")
            if sep and len(head) < 20:
                tail = rest
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
                parts.append(
                    ("  " + self._truncate(preview, reserve=2, keep_tail=True), "dim italic")
                )
            return Content.assemble(*parts)

        arrow = (
            glyphs.disclosure_expanded
            if self._expanded
            else glyphs.disclosure_collapsed
        )
        dur = int(round(self._duration_seconds))
        label = f"Thought for {dur}s" if dur > 0 else "Thought"
        parts = [(f"{arrow} ", "dim"), (label, "dim bold")]
        topic = self.topic
        if topic and not self._expanded:
            reserve = len(label) + len(sep) + 6
            parts.append(
                (f" {sep} {self._truncate(topic, reserve=reserve)}", "dim italic")
            )

        if self._expanded and self._content.strip():
            parts.append("\n")
            for line in self._content.strip().splitlines():
                heading = _thinking_heading(line)
                if heading:
                    parts.append((f"\n  {heading}", "bold dim"))
                else:
                    parts.append((f"\n  {line.strip()}", "dim"))
        return Content.assemble(*parts)
