"""Chat input widget for OpsCloud TUI.

A multi-line text input area with input mode detection (normal/command/shell),
prompt history, paste collapse preview, slash-command autocomplete,
and mode-reactive prompt glyph (❯ / / / $) with visual feedback.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
from opscloud.utils.logger import get_logger
from pathlib import Path
from typing import Any, ClassVar, Literal, Self

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static, TextArea
from textual.widgets.text_area import EditResult
from textual.events import Click
from textual.containers import VerticalScroll
from textual.content import Content

from opscloud.config.settings import get_glyphs
from opscloud.ui.widgets.autocomplete import (
    CompletionResult,
    FuzzyFileController,
    MultiCompletionManager,
    SlashCommandController,
)

logger = get_logger(__name__)

# ── Input Mode Types ────────────────────────────────────

InputMode = Literal["normal", "command", "shell", "shell_incognito"]

MODE_PREFIXES: dict[str, str] = {
    "shell_incognito": "!!",
    "shell": "!",
    "command": "/",
}

MODE_DISPLAY_GLYPHS: dict[str, str] = {
    "normal": "❯",
    "command": "/",
    "shell": "$",
    "shell_incognito": "$",
}


def detect_input_mode(text: str) -> InputMode:
    """Detect the input mode from the first characters of *text*."""
    if text.startswith("!!"):
        return "shell_incognito"
    if text.startswith("!"):
        return "shell"
    if text.startswith("/"):
        return "command"
    return "normal"


# Dim style used for inline argument hint ghost text.
_HINT_STYLE = Style(color="grey50", italic=True)


# Border title per non-normal mode (normal mode stays untitled).
_MODE_TITLES: dict[str, str] = {
    "command": "command",
    "shell": "shell",
    "shell_incognito": "private shell",
}

# Keys that insert a newline instead of submitting. Plain Enter submits;
# which of these a terminal can send varies (Option+Enter arrives as
# alt+enter on macOS, Ctrl+J everywhere, Shift+Enter with kitty protocol).
_NEWLINE_KEYS: frozenset[str] = frozenset(
    {"shift+enter", "alt+enter", "ctrl+enter", "meta+enter", "super+enter", "ctrl+j"}
)


def _newline_key_label() -> str:
    """Short keycap label for the platform newline shortcut."""
    from opscloud.config.settings import newline_shortcut

    return {"Option+Enter": "opt+enter", "Ctrl+J": "ctrl+j"}.get(
        newline_shortcut(), newline_shortcut().lower()
    )


def input_hints(mode: str) -> list[tuple[str, str]]:
    """Keycap hints for the input's bottom border, most important first.

    Args:
        mode: Current input mode.

    Returns:
        ``(key, action)`` pairs; callers drop trailing pairs to fit.
    """
    if mode == "command":
        return [("enter", "run"), ("tab", "complete"), ("↑↓", "select"), ("esc", "exit")]
    if mode == "shell":
        return [("enter", "run"), ("!", "private"), ("esc", "exit")]
    if mode == "shell_incognito":
        return [("enter", "run"), ("esc", "exit")]
    return [
        ("enter", "send"),
        (_newline_key_label(), "newline"),
        ("/", "commands"),
        ("!", "shell"),
        ("@", "files"),
    ]


def format_input_hints(hints: list[tuple[str, str]], max_width: int) -> Content:
    """Render ``key action · key action`` keycaps, trimmed to ``max_width``.

    Keys are bold, actions and separators dim. Trailing hints are dropped
    (never cut mid-word) when the border is too narrow.
    """
    sep = " · "
    kept: list[tuple[str, str]] = []
    width = 0
    for key, action in hints:
        extra = len(key) + 1 + len(action) + (len(sep) if kept else 0)
        if width + extra > max_width:
            break
        kept.append((key, action))
        width += extra
    pieces: list[tuple[str, str]] = []
    for i, (key, action) in enumerate(kept):
        if i:
            pieces.append((sep, "dim"))
        pieces.append((key, "bold"))
        pieces.append((f" {action}", "dim"))
    if pieces:
        pieces.insert(0, (" ", ""))
        pieces.append((" ", ""))
    return Content.assemble(*pieces)


# ── Completion rows ─────────────────────────────────────────────

CompletionKind = Literal["command", "file"]

_LABEL_COLUMN_MAX = 48
"""Widest the command label column grows before labels get an ellipsis."""

_HINT_COLUMN_MAX = 22
"""Widest a short command label grows from inline argument hints alone."""

_ROW_PREFIX_CELLS = 2
"""Selection bar plus one space, in front of every row."""

_COLUMN_GAP = 2
"""Cells between the label column and the description."""

_MIN_DESC_CELLS = 24
"""Description room below which argument hints stop widening the label column."""

_MATCH_STYLE = "bold $primary"
"""Style for the characters of a label that the query matched."""


def completion_kind(suggestions: list[tuple[str, str]]) -> CompletionKind:
    """Classify a suggestion list: file controller labels start with ``@``."""
    if suggestions and suggestions[0][0].startswith("@"):
        return "file"
    return "command"


def _fold(text: str) -> str:
    """Lower-case *text* one character at a time so indices stay aligned."""
    return "".join(lc if len(lc := ch.lower()) == 1 else ch for ch in text)


def _run_match(query: str, text: str) -> list[int] | None:
    """Match *query* in *text* taking the longest contiguous run each step."""
    positions: list[int] = []
    pos = 0
    qi = 0
    while qi < len(query):
        for size in range(len(query) - qi, 0, -1):
            at = text.find(query[qi : qi + size], pos)
            if at >= 0:
                positions.extend(range(at, at + size))
                pos = at + size
                qi += size
                break
        else:
            return None
    return positions


def _leftmost_match(query: str, text: str) -> list[int] | None:
    """Plain leftmost subsequence match of *query* in *text*."""
    positions: list[int] = []
    pos = 0
    for ch in query:
        at = text.find(ch, pos)
        if at < 0:
            return None
        positions.append(at)
        pos = at + 1
    return positions


def _match_spans(query: str, text: str) -> list[int]:
    """Indices of *text* matched by *query*, for highlighting only.

    Case-insensitive subsequence match that prefers contiguous runs (so
    ``pl`` lights up the ``pl`` in ``plugins``, not ``p`` … ``l``). If the
    run-first pass strands a later character, a plain leftmost subsequence
    is used instead.

    Returns:
        Sorted indices into *text*; empty when *query* is empty or is not a
        subsequence (e.g. the controller matched on the description).
    """
    if not query:
        return []
    q, t = _fold(query), _fold(text)
    positions = _run_match(q, t)
    if positions is None:
        positions = _leftmost_match(q, t)
    return positions or []


def _clip_range(text: str, width: int, *, keep_end: bool = False) -> tuple[int, int]:
    """Slice ``[start, end)`` of *text* that fits *width* cells with an ellipsis.

    Returns the full range when *text* already fits. Otherwise room is left
    for ``get_glyphs().ellipsis`` on the cut side (the start when
    *keep_end*, else the end).
    """
    if cell_len(text) <= width:
        return 0, len(text)
    budget = width - cell_len(get_glyphs().ellipsis)
    used = 0
    if keep_end:
        start = len(text)
        while start > 0 and used + cell_len(text[start - 1]) <= budget:
            start -= 1
            used += cell_len(text[start])
        return start, len(text)
    end = 0
    while end < len(text) and used + cell_len(text[end]) <= budget:
        used += cell_len(text[end])
        end += 1
    return 0, end


def _clipped_pieces(
    text: str,
    width: int,
    styles: list[str],
    *,
    keep_end: bool = False,
    ellipsis_style: str = "dim",
) -> list[tuple[str, str]]:
    """Fit *text* into *width* cells, grouping runs of equal per-char *styles*."""
    if width <= 0 or not text:
        return []
    start, end = _clip_range(text, width, keep_end=keep_end)
    ellipsis = get_glyphs().ellipsis
    pieces: list[tuple[str, str]] = []
    if (start, end) != (0, len(text)) and start == end:
        return [(ellipsis[:width], ellipsis_style)]
    if start > 0:
        pieces.append((ellipsis, ellipsis_style))
    run_start = start
    for i in range(start + 1, end + 1):
        if i == end or styles[i] != styles[run_start]:
            pieces.append((text[run_start:i], styles[run_start]))
            run_start = i
    if end < len(text):
        pieces.append((ellipsis, ellipsis_style))
    return pieces


def _completion_name(label: str, kind: CompletionKind) -> str:
    """Visible label text: commands drop ``/``, files drop ``@``."""
    return label.removeprefix("@") if kind == "file" else label.removeprefix("/")


def completion_label_width(
    suggestions: list[tuple[str, str]],
    argument_hints: list[str] | None = None,
    *,
    kind: CompletionKind,
    width: int,
) -> int:
    """Width of the label column for one popup rebuild.

    Commands: the longest ``name`` (up to ``_LABEL_COLUMN_MAX`` so namespaced
    plugin skills like ``chrome-devtools-plugin:memory-leak-debugging`` fit
    in full), while inline argument hints widen short command names up to
    ``_HINT_COLUMN_MAX``.
    Hints are dropped from the sizing when they would leave descriptions
    fewer than ``_MIN_DESC_CELLS``.
    Files: the longest path, allowed to use whatever the (short) type
    column leaves free.

    Args:
        suggestions: ``(label, description)`` pairs being shown.
        argument_hints: Per-row argument hint (commands only), or ``None``.
        kind: ``"command"`` or ``"file"``.
        width: Row content width in cells; ``0`` when not laid out yet.
    """
    if not suggestions:
        return 0
    hints = argument_hints or [""] * len(suggestions)
    names = [cell_len(_completion_name(label, kind)) for label, _desc in suggestions]
    with_hints = [
        min(cell_len(f"{_completion_name(label, kind)} {hint}"), max(n, _HINT_COLUMN_MAX))
        if hint and kind == "command"
        else n
        for (label, _desc), hint, n in zip(suggestions, hints, names, strict=False)
    ]
    cap = _LABEL_COLUMN_MAX
    if kind == "file" and width > 0:
        widest_desc = max(cell_len(desc) for _label, desc in suggestions)
        cap = max(cap, width - _ROW_PREFIX_CELLS - _COLUMN_GAP - widest_desc)
    column = min(max(with_hints), cap)
    room = width - _ROW_PREFIX_CELLS - column - _COLUMN_GAP
    if kind == "command" and width > 0 and room < _MIN_DESC_CELLS:
        name_cap = max(_HINT_COLUMN_MAX, width - _ROW_PREFIX_CELLS - _COLUMN_GAP - _MIN_DESC_CELLS)
        column = min(max(names), min(cap, name_cap))
    if width > 0:
        column = min(column, width - _ROW_PREFIX_CELLS)
    return max(column, 0)


def format_completion_row(
    label: str,
    description: str,
    *,
    query: str,
    label_width: int,
    width: int,
    selected: bool,
    kind: CompletionKind = "command",
    argument_hint: str = "",
) -> Content:
    """Render one completion row as a single line.

    Layout: ``▌ name [hint]…padded…  description``. The bar is drawn only
    on the selected row (``>`` in ASCII mode); the label column is padded
    to *label_width* so descriptions line up; anything too long gets an
    ellipsis instead of wrapping.

    Args:
        label: Raw suggestion label (``/plugins`` or ``@src/app.py``).
        description: Suggestion description; whitespace is collapsed.
        query: Text typed after the trigger, used for match highlighting.
        label_width: Label column width from `completion_label_width`.
        width: Total row width in cells; ``0`` means unbounded.
        selected: Whether this is the highlighted row.
        kind: ``"command"`` or ``"file"`` (dims the directory part).
        argument_hint: Command argument hint shown dim after the name.
    """
    bar = get_glyphs().selection_bar
    pieces: list[tuple[str, str]] = [
        (bar if selected else " " * cell_len(bar), "$primary"),
        (" ", ""),
    ]
    name = _completion_name(label, kind)
    column = label_width if label_width > 0 else cell_len(name)
    if width > 0:
        column = min(column, width - _ROW_PREFIX_CELLS)
    if column <= 0:
        return Content.assemble(*pieces)

    matches = set(_match_spans(query, name))
    base = "bold" if selected else ""
    if kind == "file":
        trimmed = name.rstrip("/")
        dir_end = trimmed.rfind("/") + 1
        styles = [
            _MATCH_STYLE if i in matches else ("dim" if i < dir_end else base)
            for i in range(len(name))
        ]
    else:
        styles = [_MATCH_STYLE if i in matches else base for i in range(len(name))]
    label_pieces = _clipped_pieces(name, column, styles, keep_end=kind == "file")
    used = sum(cell_len(text) for text, _ in label_pieces)

    hint_room = column - used
    hint = f" {argument_hint}" if argument_hint and kind == "command" else ""
    name_whole = used == cell_len(name)
    if hint and name_whole and (cell_len(hint) <= hint_room or hint_room >= 8):
        label_pieces += _clipped_pieces(hint, hint_room, ["dim"] * len(hint))
        used = sum(cell_len(text) for text, _ in label_pieces)
    pieces += label_pieces
    if column > used:
        pieces.append((" " * (column - used), ""))

    desc = " ".join(description.split())
    if desc:
        room = (width - _ROW_PREFIX_CELLS - column - _COLUMN_GAP) if width > 0 else cell_len(desc)
        desc_pieces = _clipped_pieces(desc, room, ["dim"] * len(desc))
        if desc_pieces:
            pieces.append((" " * _COLUMN_GAP, ""))
            pieces += desc_pieces
    return Content.assemble(*pieces)


@dataclass(frozen=True)
class CompletionRowLayout:
    """Layout shared by every row of one popup rebuild."""

    query: str = ""
    label_width: int = 0
    width: int = 0
    kind: CompletionKind = "command"


class CompletionOption(Static):
    """A clickable completion option in the autocomplete popup."""

    # Selection is an accent bar (drawn in the row content) on a soft tint;
    # no colour inversion, so it reads the same on light and dark themes.
    DEFAULT_CSS = """
    CompletionOption {
        height: 1;
        padding: 0;
    }

    CompletionOption:hover {
        background: $primary 6%;
    }

    CompletionOption.completion-option-selected {
        background: $primary 12%;
    }
    """

    class Clicked(Message):
        """Message sent when a completion option is clicked."""

        def __init__(self, index: int) -> None:
            """Initialize with the clicked option index."""
            super().__init__()
            self.index = index

    def __init__(
        self,
        label: str,
        description: str,
        index: int,
        is_selected: bool = False,
        *,
        layout: CompletionRowLayout | None = None,
        argument_hint: str = "",
        **kwargs: Any,
    ) -> None:
        """Initialize the completion option."""
        super().__init__(**kwargs)
        self._label = label
        self._description = description
        self._index = index
        self._is_selected = is_selected
        self._layout = layout or CompletionRowLayout()
        self._argument_hint = argument_hint

    def on_mount(self) -> None:
        """Set up the option display on mount."""
        self._update_display()

    def _update_display(self) -> None:
        """Update the display text and styling."""
        layout = self._layout
        content = format_completion_row(
            self._label,
            self._description,
            query=layout.query,
            label_width=layout.label_width,
            width=layout.width,
            selected=self._is_selected,
            kind=layout.kind,
            argument_hint=self._argument_hint,
        )

        self.update(content)

        if self._is_selected:
            self.add_class("completion-option-selected")
        else:
            self.remove_class("completion-option-selected")

    def set_selected(self, *, selected: bool) -> None:
        """Update the selected state of this option."""
        if self._is_selected != selected:
            self._is_selected = selected
            self._update_display()

    def set_layout(self, layout: CompletionRowLayout) -> None:
        """Re-render with a new shared layout (e.g. after a resize)."""
        if layout != self._layout:
            self._layout = layout
            self._update_display()

    def set_content(
        self,
        label: str,
        description: str,
        index: int,
        *,
        is_selected: bool,
        layout: CompletionRowLayout | None = None,
        argument_hint: str = "",
    ) -> None:
        """Replace label, description, index, and selection in-place."""
        self._label = label
        self._description = description
        self._index = index
        self._is_selected = is_selected
        if layout is not None:
            self._layout = layout
        self._argument_hint = argument_hint
        self._update_display()

    def on_click(self, event: Click) -> None:
        """Handle click on this option."""
        event.stop()
        self.post_message(self.Clicked(self._index))


class CompletionPopup(VerticalScroll):
    """Popup widget that displays completion suggestions as clickable options."""

    # Inset one cell so rows line up under the prompt glyph; a faint rule
    # separates the list from the input row.
    DEFAULT_CSS = """
    CompletionPopup {
        display: none;
        height: auto;
        max-height: 12;
        margin: 0 1;
        border-top: solid $primary 20%;
    }
    """

    class OptionClicked(Message):
        """Message sent when a completion option is clicked."""

        def __init__(self, index: int) -> None:
            """Initialize with the clicked option index."""
            super().__init__()
            self.index = index

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the completion popup."""
        super().__init__(**kwargs)
        self.can_focus = False
        self._options: list[CompletionOption] = []
        self._selected_index = 0
        self._pending_suggestions: list[tuple[str, str]] = []
        self._pending_selected: int = 0
        self._rebuild_generation: int = 0
        self._shown_suggestions: list[tuple[str, str]] = []
        self._shown_hints: list[str] = []
        self._layout = CompletionRowLayout()

    def _chat_input(self) -> ChatInput | None:
        """Walk up the ancestor chain to the owning `ChatInput`."""
        node = self.parent
        while node is not None:
            if isinstance(node, ChatInput):
                return node
            node = node.parent
        return None

    def _row_width(self) -> int:
        """Cells available to one row; falls back to the box while hidden."""
        width = self.scrollable_content_region.width
        if width <= 0 and isinstance(self.parent, Widget):
            # Hidden popups have no region yet; use the box minus our margin.
            width = self.parent.content_region.width - 2
        return max(width, 0)

    def _compute_layout(
        self, suggestions: list[tuple[str, str]]
    ) -> tuple[CompletionRowLayout, list[str]]:
        """Shared row layout plus per-row argument hints for *suggestions*."""
        kind = completion_kind(suggestions)
        owner = self._chat_input()
        query = owner.completion_query(kind) if owner is not None else ""
        hint_map = owner._argument_hints if owner is not None and kind == "command" else {}
        hints = [hint_map.get(label.removeprefix("/"), "") for label, _ in suggestions]
        width = self._row_width()
        label_width = completion_label_width(suggestions, hints, kind=kind, width=width)
        layout = CompletionRowLayout(
            query=query, label_width=label_width, width=width, kind=kind
        )
        return layout, hints

    def on_resize(self, event: events.Resize) -> None:
        """Re-fit rows to the new width (cheap: at most a dozen rows)."""
        if not self._options or not self._shown_suggestions:
            return
        width = self._row_width()
        label_width = completion_label_width(
            self._shown_suggestions, self._shown_hints, kind=self._layout.kind, width=width
        )
        self._layout = replace(self._layout, width=width, label_width=label_width)
        for option in self._options:
            option.set_layout(self._layout)

    def update_suggestions(
        self, suggestions: list[tuple[str, str]], selected_index: int
    ) -> None:
        """Update the popup with new suggestions."""
        if not suggestions:
            self.hide()
            return

        self._selected_index = selected_index
        self._pending_suggestions = suggestions
        self._pending_selected = selected_index
        self._rebuild_generation += 1
        gen = self._rebuild_generation
        self.call_next(lambda: self._rebuild_options(gen))

    async def _rebuild_options(self, generation: int) -> None:
        """Rebuild option widgets from pending suggestions."""
        if generation != self._rebuild_generation:
            return

        suggestions = self._pending_suggestions
        selected_index = self._pending_selected

        if not suggestions:
            self.hide()
            return

        layout, hints = self._compute_layout(suggestions)
        self._layout = layout
        self._shown_suggestions = list(suggestions)
        self._shown_hints = hints

        existing = len(self._options)
        needed = len(suggestions)

        for i in range(min(existing, needed)):
            label, desc = suggestions[i]
            self._options[i].set_content(
                label,
                desc,
                i,
                is_selected=(i == selected_index),
                layout=layout,
                argument_hint=hints[i],
            )

        try:
            if existing > needed:
                for option in self._options[needed:]:
                    await option.remove()
                del self._options[needed:]

            if needed > existing:
                new_widgets: list[CompletionOption] = []
                for idx in range(existing, needed):
                    label, desc = suggestions[idx]
                    option = CompletionOption(
                        label=label,
                        description=desc,
                        index=idx,
                        is_selected=(idx == selected_index),
                        layout=layout,
                        argument_hint=hints[idx],
                    )
                    new_widgets.append(option)
                self._options.extend(new_widgets)
                await self.mount(*new_widgets)
        except Exception:
            logger.exception("Failed to rebuild completion popup; hiding to recover")
            self._options = []
            with contextlib.suppress(Exception):
                await self.remove_children()
            self.hide()
            return

        if generation != self._rebuild_generation:
            return

        self.show()

        if 0 <= selected_index < len(self._options):
            self._options[selected_index].scroll_visible()

    def update_selection(self, selected_index: int) -> None:
        """Update which option is selected without rebuilding the list."""
        self._pending_selected = selected_index

        if self._selected_index == selected_index:
            return

        if 0 <= self._selected_index < len(self._options):
            self._options[self._selected_index].set_selected(selected=False)

        self._selected_index = selected_index
        if 0 <= selected_index < len(self._options):
            self._options[selected_index].set_selected(selected=True)
            self._options[selected_index].scroll_visible()

    def on_completion_option_clicked(self, event: CompletionOption.Clicked) -> None:
        """Handle click on a completion option."""
        event.stop()
        self.post_message(self.OptionClicked(event.index))

    def hide(self) -> None:
        """Hide the popup."""
        self._pending_suggestions = []
        self._rebuild_generation += 1
        self.styles.display = "none"

    def show(self) -> None:
        """Show the popup."""
        self.styles.display = "block"


class _CompletionViewAdapter:
    """Translate completion-space replacements to text-area coordinates."""

    def __init__(self, chat_input: ChatInput) -> None:
        """Initialize adapter with its owning `ChatInput`."""
        self._chat_input = chat_input

    def render_completion_suggestions(
        self, suggestions: list[tuple[str, str]], selected_index: int
    ) -> None:
        self._chat_input.render_completion_suggestions(suggestions, selected_index)

    def clear_completion_suggestions(self) -> None:
        self._chat_input.clear_completion_suggestions()

    def replace_completion_range(self, start: int, end: int, replacement: str) -> None:
        prefix = MODE_PREFIXES.get(self._chat_input.mode, "")
        if prefix and replacement.startswith(prefix):
            replacement = replacement[len(prefix) :]
        self._chat_input.replace_completion_range(
            self._chat_input._completion_index_to_text_index(start),
            self._chat_input._completion_index_to_text_index(end),
            replacement,
        )


class ChatTextArea(TextArea):
    """Internal TextArea for ChatInput widget."""

    argument_hint: reactive[str] = reactive("")
    """Ghost text shown after cursor when a slash command expects arguments."""

    def __init__(self, **kwargs) -> None:
        super().__init__(
            language=None,
            show_line_numbers=False,
            **kwargs,
        )

    def clear(self) -> EditResult:
        """Clear document text and reset any argument hint ghost text."""
        self.argument_hint = ""
        return super().clear()

    def render_line(self, y: int) -> Strip:
        """Render a single line, appending argument hint ghost text at EOL."""
        strip = super().render_line(y)
        if not self.argument_hint:
            return strip

        _scroll_x, scroll_y = self.scroll_offset
        absolute_y = scroll_y + y

        last_doc_line = self.document.line_count - 1
        if absolute_y != last_doc_line:
            return strip

        line_text = self.document.get_line(absolute_y)
        if not line_text.strip():
            return strip
        content_cells = cell_len(line_text)

        if content_cells >= strip.cell_length:
            return strip

        prefix = strip.crop(0, content_cells)
        suffix = strip.crop(content_cells, strip.cell_length)
        suffix_width = suffix.cell_length

        cursor_style = suffix._segments[0].style if suffix._segments else None
        hint_text = self.argument_hint
        
        if cursor_style and self.has_focus:
            first_char_style = _HINT_STYLE + cursor_style
            segments = [
                Segment(hint_text[0], first_char_style),
                Segment(hint_text[1:], _HINT_STYLE)
            ]
            hint_strip = Strip(segments, cell_length=cell_len(hint_text))
            if suffix_width > 0:
                suffix = suffix.crop(1, suffix_width)
        else:
            hint_segment = Segment(hint_text, _HINT_STYLE)
            hint_strip = Strip([hint_segment], cell_length=cell_len(hint_text))

        tail = Strip.join([hint_strip, suffix]).crop(0, suffix_width)
        return Strip.join([prefix, tail])

    def _find_chat_input(self) -> ChatInput | None:
        """Walk up the ancestor chain to find the ChatInput widget."""
        node = self.parent
        while node is not None:
            if isinstance(node, ChatInput):
                return node
            node = node.parent
        return None

    async def _on_key(self, event: events.Key) -> None:
        chat_input = self._find_chat_input()
        if (
            event.is_printable
            and event.character is not None
            and self.cursor_location == (0, 0)
            and self.selection.is_empty
            and not self.text
            and chat_input is not None
            and chat_input.handle_mode_prefix_keystroke(event.character)
        ):
            event.prevent_default()
            event.stop()
            return

        if chat_input is not None:
            handled = await chat_input._handle_key_event(event)
            if handled:
                return
        await super()._on_key(event)

    def on_paste(self, event: events.Paste) -> None:
        chat_input = self._find_chat_input()
        if chat_input is not None:
            chat_input.on_paste(event)


class ChatInput(Widget):
    """Multi-line chat input with prompt glyph (❯), mode detection, history,
    paste collapse, and inline slash command autocomplete.
    """

    DEFAULT_CSS = """
    ChatInput {
        height: auto;
    }

    /* Rounded box whose colour follows the input mode: teal prompt, violet
       slash command, green shell, muted private shell. Dimmer while the
       input is not focused. Mode name sits in the top border, keycap hints
       in the bottom border. */
    ChatInput #input-box {
        height: auto;
        min-height: 3;
        max-height: 20;
        border: round $primary 45%;
        border-title-color: $primary;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
        border-subtitle-align: left;
        background: $background;
        color: $foreground;
        padding: 0;
    }

    ChatInput:focus-within #input-box {
        border: round $primary;
    }

    ChatInput.mode-command #input-box {
        border: round $mode-command;
        border-title-color: $mode-command;
    }

    ChatInput.mode-shell #input-box {
        border: round $mode-bash;
        border-title-color: $mode-bash;
    }

    ChatInput.mode-shell-incognito #input-box {
        border: round $mode-incognito 70%;
        border-title-color: $mode-incognito;
    }

    ChatInput .input-row {
        height: auto;
        width: 100%;
        layout: horizontal;
        padding: 0 1;
    }

    ChatInput .input-prompt {
        width: 2;
        height: 1;
        color: $primary;
        text-style: bold;
    }

    ChatInput.mode-command .input-prompt {
        color: $mode-command;
    }

    ChatInput.mode-shell .input-prompt {
        color: $mode-bash;
    }

    ChatInput.mode-shell-incognito .input-prompt {
        color: $mode-incognito;
    }

    ChatInput ChatTextArea {
        width: 1fr;
        height: auto;
        min-height: 1;
        max-height: 10;
        border: none;
        background: transparent;
        padding: 0;
    }

    /* TextArea ships its own focus border/tint; the outer box shows focus. */
    ChatInput ChatTextArea:focus {
        border: none;
        background: transparent;
        background-tint: transparent;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = []

    mode: reactive[str] = reactive("normal")
    _app: Any = None

    # ── Messages ────────────────────────────────────────

    class Submitted(Message):
        """Fired when the user submits prompt/command."""

        def __init__(self, value: str, mode: str = "normal") -> None:
            super().__init__()
            self.value = value
            self.mode = mode

    class ModeChanged(Message):
        """Fired when the input mode changes (for status bar updates)."""

        def __init__(self, mode: str) -> None:
            super().__init__()
            self.mode = mode

    class Typing(Message):
        """Fired on printable keystrokes."""

    class SlashCommandStarted(Message):
        """Fired when user begins typing a slash command."""

        def __init__(self, query: str) -> None:
            super().__init__()
            self.query = query

    class SlashCommandEnded(Message):
        """Fired when slash command context is exited."""

    class CancelRequested(Message):
        """Fired when user presses Ctrl+C on empty input."""

    # ── Constructor ─────────────────────────────────────

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._app: Any = None
        self._prompt_widget = Static("❯", id="input-prompt", classes="input-prompt")
        self._text_area = ChatTextArea(id="chat-text-area")
        self._autocomplete = CompletionPopup(id="completion-popup")
        self._history: list[str] = self._load_persisted_history()
        self._history_index: int = -1
        self._saved_draft: str = ""
        self._pasted_full_text: str | None = None
        self._cwd = Path.cwd()
        self._argument_hints: dict[str, str] = {}
        self._current_suggestions: list[tuple[str, str]] = []
        self._current_selected_index: int = 0
        self._completion_prefix_len: int = 0
        self._input_box: Widget | None = None
        self._rebuild_argument_hints()

    def on_mount(self) -> None:
        self._completion_view = _CompletionViewAdapter(self)
        from opscloud.ui.command_registry import get_all_entries
        self._file_controller = FuzzyFileController(self._completion_view, cwd=self._cwd)
        self._slash_controller = SlashCommandController(get_all_entries(), self._completion_view)
        self._completion_manager = MultiCompletionManager([
            self._slash_controller,
            self._file_controller,
        ])
        
        self.run_worker(
            self._file_controller.warm_cache(force=False),
            exclusive=False,
            group="file_cache",
            exit_on_error=False,
        )
        self._update_chrome()

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal, Vertical
        with Vertical(id="input-box") as box:
            self._input_box = box
            with Horizontal(classes="input-row"):
                yield self._prompt_widget
                yield self._text_area
            yield self._autocomplete

    # ── Chrome (border title, keycap hints) ────────────────

    def on_resize(self, event: events.Resize) -> None:
        """Re-fit the bottom-border hints to the new width."""
        self._update_chrome()

    def _update_chrome(self) -> None:
        """Sync border title and keycap hints with the mode."""
        box = self._input_box
        if box is None:
            return
        mode = self.mode
        title = _MODE_TITLES.get(mode)
        box.border_title = f" {title} " if title else None
        # Leave room for the rounded corners plus a little breathing space.
        max_width = max(0, (box.size.width or self.size.width or 80) - 6)
        box.border_subtitle = format_input_hints(input_hints(mode), max_width)

    # ── Properties & Delegation ─────────────────────────

    @property
    def text(self) -> str:
        return self._text_area.text

    @text.setter
    def text(self, value: str) -> None:
        mode = detect_input_mode(value)
        prefix = MODE_PREFIXES.get(mode, "")
        if prefix and value.startswith(prefix):
            self.mode = mode
            self._text_area.text = value[len(prefix) :]
        else:
            self.mode = "normal"
            self._text_area.text = value
            self._sync_mode()
        self._update_argument_hint()

    @property
    def value(self) -> str:
        """The logical content of the input text area."""
        if self._text_area:
            return self._text_area.text
        return ""

    @value.setter
    def value(self, val: str) -> None:
        self.text = val

    def set_value_at_end(self, val: str) -> bool:
        """Set the input value and place cursor at the end."""
        if not self._text_area:
            return False
        self.text = val
        lines = self._text_area.text.split("\n")
        self._text_area.cursor_location = (len(lines) - 1, len(lines[-1])) if lines else (0, 0)
        return True

    def discard_text(self) -> bool:
        """Clear the input text draft."""
        if self._text_area is None:
            return False
        had_text = bool(self._text_area.text)
        self.clear()
        return had_text

    def dismiss_completion(self) -> bool:
        """Dismiss completion popup if active."""
        if not getattr(self, "_current_suggestions", None):
            return False
        if self._completion_manager:
            self._completion_manager.reset()
        self.clear_completion_suggestions()
        return True

    def exit_mode(self) -> bool:
        """Exit the current input mode (command/shell) back to normal."""
        if self.mode == "normal":
            return False
        self.mode = "normal"
        if getattr(self, "_text_area", None) is not None:
            self._text_area.argument_hint = ""
        if self._completion_manager:
            self._completion_manager.reset()
        self.clear_completion_suggestions()
        return True

    def clear(self) -> None:
        if getattr(self, "_text_area", None) is not None:
            self._text_area.argument_hint = ""
            self._text_area.clear()
        self._pasted_full_text = None
        self._completion_manager.reset()
        self.mode = "normal"

    def focus(self, scroll_visible: bool = True) -> Self:
        self._text_area.focus(scroll_visible=scroll_visible)
        return self

    def focus_input(self) -> None:
        self.focus()

    # ── Mode Watcher ────────────────────────────────────

    def watch_mode(self, mode: str) -> None:
        """Update CSS classes and prompt glyph when mode changes."""
        self.remove_class("mode-command", "mode-shell", "mode-shell-incognito")
        glyph = MODE_DISPLAY_GLYPHS.get(mode, "❯")
        self._prompt_widget.update(glyph)
        if mode == "command":
            self.add_class("mode-command")
        elif mode == "shell":
            self.add_class("mode-shell")
        elif mode == "shell_incognito":
            self.add_class("mode-shell-incognito")
        if mode != "command" and getattr(self, "_text_area", None) is not None:
            self._text_area.argument_hint = ""
        self._update_chrome()
        self.post_message(self.ModeChanged(mode))

    # ── Event Handlers ──────────────────────────────────

    def on_paste(self, event: events.Paste | Any) -> None:
        """Handle paste events with collapsing for large multi-line payloads."""
        text = event.text
        lines = text.splitlines()
        if len(lines) > 5 or len(text) > 500:
            self._pasted_full_text = text
            preview = f"[Pasted {len(lines)} lines ({len(text)} chars) — press Enter to send]"
            self.text = preview
            event.prevent_default()
            event.stop()

    def handle_mode_prefix_keystroke(self, char: str) -> bool:
        """Switch input mode for a mode trigger typed at the start of empty input.

        Handles the switch before TextArea inserts the character so the trigger
        (/, !, !!) is consumed cleanly without appearing inside the text area.
        """
        if char == "/" and self.mode == "command":
            return False

        if char == "/":
            detected = "command"
        elif char == "!":
            if self.mode == "shell":
                detected = "shell_incognito"
            elif self.mode == "shell_incognito":
                return False
            else:
                detected = "shell"
        else:
            return False

        if self.mode != detected:
            self.mode = detected

        self._update_argument_hint()
        if hasattr(self, "_completion_manager") and self._completion_manager:
            vtext, vcursor = self._completion_text_and_cursor()
            self._completion_manager.on_text_changed(vtext, vcursor)
        return True

    def _get_cursor_offset(self) -> int:
        """Get cursor position as character offset from start of text."""
        if not self._text_area:
            return 0
        text = self._text_area.text
        row, col = self._text_area.cursor_location
        if not text:
            return 0
        lines = text.split("\n")
        row = max(0, min(row, len(lines) - 1))
        col = max(0, col)
        return sum(len(line) + 1 for line in lines[:row]) + min(col, len(lines[row]))

    def _completion_text_and_cursor(self) -> tuple[str, int]:
        """Return controller-facing text/cursor in completion space."""
        if not self._text_area:
            self._completion_prefix_len = 0
            return "", 0

        text = self._text_area.text
        cursor = self._get_cursor_offset()
        prefix = MODE_PREFIXES.get(self.mode, "")
        self._completion_prefix_len = len(prefix)

        if prefix:
            return prefix + text, cursor + len(prefix)
        return text, cursor

    def _completion_index_to_text_index(self, index: int) -> int:
        """Translate completion-space index into text-area index."""
        if not self._text_area:
            return 0
        if 0 <= index <= self._completion_prefix_len:
            return 0
        mapped = index - self._completion_prefix_len
        text_len = len(self._text_area.text)
        return max(0, min(mapped, text_len))

    def render_completion_suggestions(
        self, suggestions: list[tuple[str, str]], selected_index: int
    ) -> None:
        """Update popup with completion suggestions and track current list."""
        self._current_suggestions = suggestions
        self._current_selected_index = selected_index
        self._autocomplete.update_suggestions(suggestions, selected_index)

    def completion_query(self, kind: CompletionKind) -> str:
        """Text typed after the completion trigger, for match highlighting.

        Args:
            kind: ``"command"`` (after the leading ``/``) or ``"file"``
                (after the last ``@`` before the cursor).
        """
        text, cursor = self._completion_text_and_cursor()
        before = text[:cursor]
        if kind == "file":
            at = before.rfind("@")
            return before[at + 1 :] if at >= 0 else ""
        return before.removeprefix("/")

    def clear_completion_suggestions(self) -> None:
        """Clear and hide completion popup."""
        self._current_suggestions = []
        self._current_selected_index = 0
        self._autocomplete.hide()

    def replace_completion_range(self, start: int, end: int, replacement: str) -> None:
        """Replace text in input field and append trailing space for completed tokens."""
        if not self._text_area:
            return

        text = self._text_area.text
        start = max(0, min(start, len(text)))
        end = max(start, min(end, len(text)))

        prefix = text[:start]
        suffix = text[end:]

        if replacement.endswith("/"):
            insertion = replacement
        else:
            insertion = replacement + " " if not suffix.startswith(" ") else replacement

        new_text = f"{prefix}{insertion}{suffix}"
        self._text_area.text = new_text

        new_offset = start + len(insertion)
        lines = new_text.split("\n")
        remaining = new_offset
        for row, line in enumerate(lines):
            if remaining <= len(line):
                self._text_area.cursor_location = (row, remaining)
                break
            remaining -= len(line) + 1
        else:
            self._text_area.cursor_location = (len(lines) - 1, len(lines[-1])) if lines else (0, 0)

        self._update_argument_hint()

    def on_completion_popup_option_clicked(
        self, event: CompletionPopup.OptionClicked
    ) -> None:
        """Handle click on a completion option."""
        if not self._current_suggestions or not self._text_area:
            return

        index = event.index
        if index < 0 or index >= len(self._current_suggestions):
            return

        if self._completion_manager is None:
            return
        text, cursor = self._completion_text_and_cursor()
        self._completion_manager.apply_selection(index, text, cursor)
        with contextlib.suppress(Exception):
            self._text_area.focus()

    def on_click(self, event: Click) -> None:
        """Focus the text area when clicking anywhere on the chat input box."""
        with contextlib.suppress(Exception):
            self.focus()

    async def _handle_key_event(self, event: events.Key) -> bool:
        """Handle key presses — intercept submit/history, delegate rest to TextArea."""
        key = event.key

        try:
            app = getattr(self, "_app", None) or self.app
        except Exception:
            app = getattr(self, "_app", None)
        prompts = getattr(app, "_prompts", None) if app is not None else None
        if prompts is not None and prompts.route_key(event):
            return True

        pending_approval = (
            getattr(app, "_pending_approval_widget", None) if app is not None else None
        )
        if (
            pending_approval is not None
            and getattr(pending_approval, "is_mounted", False)
            and getattr(pending_approval, "display", True)
        ):
            from opscloud.ui.controllers.prompt_controller import PromptController

            handled = (
                bool(pending_approval.handle_scoped_key(key))
                if hasattr(pending_approval, "handle_scoped_key")
                else PromptController._dispatch_fallback_approval_key(
                    pending_approval, key
                )
            )
            if handled:
                event.prevent_default()
                event.stop()
                return True

        # Backspace at start of mode prompt exits the current mode
        if (
            key == "backspace"
            and self.mode != "normal"
            and self._get_cursor_offset() == 0
            and not self.text
        ):
            self.mode = "normal"
            self._completion_manager.reset()
            self._update_argument_hint()
            event.prevent_default()
            event.stop()
            return True

        # Newline shortcuts (Option+Enter / Ctrl+J / Shift+Enter) insert a
        # line break; plain Enter submits below.
        if key in _NEWLINE_KEYS:
            event.prevent_default()
            event.stop()
            self._text_area.insert("\n")
            self.call_after_refresh(self._after_key_sync)
            return True

        vtext, vcursor = self._completion_text_and_cursor()
        result = self._completion_manager.on_key(event, vtext, vcursor)
        if result == CompletionResult.HANDLED:
            event.prevent_default()
            event.stop()
            return True
        elif result == CompletionResult.SUBMIT:
            event.prevent_default()
            event.stop()
            self._submit_value()
            return True

        # When `/btw` card is open and the input is empty in normal mode,
        # route Enter (dismiss/cancel), Tab (expand/collapse), and Left/Right (history).
        app_any: Any = app
        if (
            app_any is not None
            and hasattr(app_any, "is_btw_open")
            and app_any.is_btw_open()
            and self.mode == "normal"
            and not self.text.strip()
        ):
            if key == "enter":
                event.prevent_default()
                event.stop()
                app_any.dismiss_btw_response()
                return True
            if key == "tab":
                event.prevent_default()
                event.stop()
                app_any.toggle_btw_expanded()
                return True
            if key == "left":
                event.prevent_default()
                event.stop()
                app_any.navigate_btw(-1)
                return True
            if key == "right":
                event.prevent_default()
                event.stop()
                app_any.navigate_btw(1)
                return True

        if key == "enter":
            event.prevent_default()
            event.stop()
            self._submit_value()
            return True

        if key in ("ctrl+up", "ctrl+p"):
            event.prevent_default()
            event.stop()
            self.action_history_prev()
            return True

        if key in ("ctrl+down", "ctrl+n"):
            event.prevent_default()
            event.stop()
            self.action_history_next()
            return True

        if key == "up" and self._history:
            row, _col = self._text_area.cursor_location if self._text_area else (0, 0)
            last_row = max(0, self._text_area.document.line_count - 1) if self._text_area else 0
            if row == 0 or (self._history_index != -1 and row >= last_row):
                event.prevent_default()
                event.stop()
                self.action_history_prev()
                return True

        if key == "down" and self._history_index != -1:
            row, _col = self._text_area.cursor_location if self._text_area else (0, 0)
            last_row = max(0, self._text_area.document.line_count - 1) if self._text_area else 0
            if row >= last_row or row == 0:
                event.prevent_default()
                event.stop()
                self.action_history_next()
                return True

        if key == "ctrl+c" and not self.text.strip():
            event.prevent_default()
            event.stop()
            self.post_message(self.CancelRequested())
            return True

        self.call_after_refresh(self._after_key_sync)
        if event.is_printable or key == "backspace":
            self.post_message(self.Typing())

        return False

    def _after_key_sync(self) -> None:
        self._sync_mode()
        vtext, vcursor = self._completion_text_and_cursor()
        self._completion_manager.on_text_changed(vtext, vcursor)
        self._update_argument_hint()

    # ── Mode Detection ──────────────────────────────────

    def _sync_mode(self) -> None:
        """Detect and update the input mode from current text content, stripping prefix if pasted."""
        text = self._text_area.text
        if self.mode == "normal":
            for mode, prefix in (("shell_incognito", "!!"), ("shell", "!"), ("command", "/")):
                if text.startswith(prefix):
                    self.mode = mode
                    self._text_area.text = text[len(prefix) :]
                    row, col = self._text_area.cursor_location
                    self._text_area.cursor_location = (row, max(0, col - len(prefix)))
                    return

    # ── Argument Hint Ghost Text ────────────────────────

    def update_slash_commands(self, commands: list[Any]) -> None:
        """Update slash command autocomplete entries and argument hints."""
        if hasattr(self, "_slash_controller") and self._slash_controller:
            self._slash_controller.update_commands(commands)
        self._rebuild_argument_hints()

    def _rebuild_argument_hints(self) -> None:
        """Rebuild the command-name -> argument-hint lookup from registry."""
        from opscloud.ui.command_registry import get_all_entries

        self._argument_hints = {
            entry.name.removeprefix("/"): entry.argument_hint
            for entry in get_all_entries()
            if entry.argument_hint
        }

    def _update_argument_hint(self) -> None:
        """Show or clear inline ghost text for slash-command argument hints."""
        if not self._text_area:
            return

        if self.mode == "command":
            text = self._text_area.text
            if text.endswith(" ") and text.count(" ") == 1:
                command = text[:-1]
                hint = self._argument_hints.get(command, "")
                if hint:
                    self._text_area.argument_hint = hint
                    return

        self._text_area.argument_hint = ""

    # ── Submit ──────────────────────────────────────────

    def _submit_value(self) -> None:
        """Submit the current text with detected mode."""
        content = self._pasted_full_text if self._pasted_full_text else self.text.strip()
        self._pasted_full_text = None

        if not content:
            return

        mode = self.mode
        prefix = MODE_PREFIXES.get(mode, "")
        if prefix and not content.startswith(prefix):
            content = prefix + content

        self.add_history_entry(content, persist=True)
        self._history_index = -1
        self._saved_draft = ""

        self.clear()
        self.mode = "normal"

        self.post_message(self.SlashCommandEnded())
        self.post_message(self.Submitted(content, mode))

    # ── History ─────────────────────────────────────────

    @staticmethod
    def _load_persisted_history(max_entries: int = 200) -> list[str]:
        """Load persisted prompt history from ~/.opscloud/.state/history.jsonl."""
        import json
        import os

        if os.environ.get("PYTEST_CURRENT_TEST"):
            return []
        try:
            from opscloud.config.paths import HISTORY_PATH

            if not HISTORY_PATH.is_file():
                return []
            entries: list[str] = []
            for raw_line in HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-max_entries:]:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    text = data.get("text") if isinstance(data, dict) else None
                except Exception:
                    text = line
                if isinstance(text, str) and text.strip():
                    if not entries or entries[-1] != text:
                        entries.append(text)
            return entries[-max_entries:]
        except Exception:
            return []

    def add_history_entry(self, content: str, *, persist: bool = True) -> None:
        """Append a prompt to in-memory history and optionally persist to disk."""
        import json
        import os

        clean = content.strip()
        if not clean:
            return
        if not self._history or self._history[-1] != clean:
            self._history.append(clean)
            if persist and not os.environ.get("PYTEST_CURRENT_TEST"):
                try:
                    from opscloud.config.paths import HISTORY_PATH

                    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
                    with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"text": clean}) + "\n")
                except Exception:
                    pass

    def action_history_prev(self) -> None:
        """Navigate backward in history and place cursor at the end."""
        if not self._history:
            return
        if self._history_index == -1:
            self._saved_draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1

        self.set_value_at_end(self._history[self._history_index])

    def action_history_next(self) -> None:
        """Navigate forward in history and place cursor at the end."""
        if self._history_index == -1:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.set_value_at_end(self._history[self._history_index])
        else:
            self._history_index = -1
            self.set_value_at_end(self._saved_draft)

    def action_dismiss_or_clear(self) -> None:
        """Clear input text and end slash command mode."""
        self.clear()
