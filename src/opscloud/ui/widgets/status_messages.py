"""Status, system, skill, error, turn summary, and rubric message widgets.

Extracted from `opscloud.ui.widgets.messages` (Phase 5 UI modularization).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual import on
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.events import Click
from textual.message import Message
from textual.reactive import var
from textual.widgets import Static

from opscloud.config.settings import get_glyphs, is_ascii_mode
from opscloud.ui import theme
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from rich.console import Console as RichConsole, ConsoleOptions, RenderResult
    from textual.app import ComposeResult

logger = get_logger(__name__)


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter delimited by `---` markers."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text
    end = stripped.find("\n---", 3)
    if end == -1:
        return text
    after = end + 4  # len("\n---")
    return stripped[after:].lstrip("\n")


class _RichMarkdownWrapper:
    """Render Rich markdown for both muted system base and vibrant command output cards."""

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

            def __rich_console__(  # noqa: PLW3201
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

    def __rich_console__(  # noqa: PLW3201
        self, console: RichConsole, options: ConsoleOptions
    ) -> RenderResult:
        from rich.styled import Styled
        from rich.theme import Theme

        overrides = (
            self._COMMAND_THEME_OVERRIDES
            if self._is_command
            else self._MUTED_THEME_OVERRIDES
        )
        md_theme = Theme(overrides, inherit=True)
        try:
            with console.use_theme(md_theme):
                yield from self._markdown.__rich_console__(console, options)
        except Exception:
            logger.warning(
                "Rich markdown rendering failed; falling back to plain text",
                exc_info=True,
            )
            yield from Styled(self._markup, "").__rich_console__(console, options)


_MutedRichMarkdown = _RichMarkdownWrapper

_MARKDOWN_MIN_RENDER_WIDTH = 20
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
                except Exception:
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


class _SkillToggle(Static):
    """Clickable header/hint area for toggling skill body expansion."""


class SkillMessage(Vertical):
    """Widget displaying a skill invocation with collapsible body."""

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
            self._ensure_md_rendered(body)
            self._expanded = True

    def _ensure_md_rendered(self, body: str) -> None:
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
        if not self._stripped_body.strip():
            return
        self._expanded = not self._expanded

    def watch__expanded(self, expanded: bool) -> None:
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
    """A system/command-response info message."""

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
        self.auto_links = False

        is_multiline = "\n" in content
        is_markdown = any(
            tok in content
            for tok in ("```", "**", "__", "#", "- ", "* ", "|", "•", "✓", "⚠", "✗", "1. ")
        )
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
        content = self._raw_content
        if self._show_timestamp:
            content = f"{content}  *[{self._timestamp}]*"

        is_multiline = "\n" in content
        is_markdown = any(
            tok in content
            for tok in ("```", "**", "__", "#", "- ", "* ", "|", "•", "✓", "⚠", "✗", "1. ")
        )
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
    """One-line footer under a completed agent turn."""

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
