"""Chat input widget for OpsCloud TUI.

A multi-line text input area with input mode detection (normal/command/shell),
prompt history, paste collapse preview, slash-command autocomplete,
and mode-reactive prompt glyph (❯ / / / $) with visual feedback.
"""

from __future__ import annotations

import contextlib
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
from textual.events import Click
from textual.containers import VerticalScroll
from textual.content import Content

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


class CompletionOption(Static):
    """A clickable completion option in the autocomplete popup."""

    DEFAULT_CSS = """
    CompletionOption {
        height: 1;
        padding: 0 1;
    }

    CompletionOption:hover {
        background: $surface-lighten-1;
    }

    CompletionOption.completion-option-selected {
        background: $primary;
        color: $background;
        text-style: bold;
    }

    CompletionOption.completion-option-selected:hover {
        background: $primary-lighten-1;
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
        **kwargs: Any,
    ) -> None:
        """Initialize the completion option."""
        super().__init__(**kwargs)
        self._label = label
        self._description = description
        self._index = index
        self._is_selected = is_selected

    def on_mount(self) -> None:
        """Set up the option display on mount."""
        self._update_display()

    def _update_display(self) -> None:
        """Update the display text and styling."""
        display_label = self._label.removeprefix("/")
        if self._description:
            content = Content.from_markup(
                "[bold]$label[/bold]  [dim]$desc[/dim]",
                label=display_label,
                desc=self._description,
            )
        else:
            content = Content.from_markup("[bold]$label[/bold]", label=display_label)

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

    def set_content(
        self, label: str, description: str, index: int, *, is_selected: bool
    ) -> None:
        """Replace label, description, index, and selection in-place."""
        self._label = label
        self._description = description
        self._index = index
        self._is_selected = is_selected
        self._update_display()

    def on_click(self, event: Click) -> None:
        """Handle click on this option."""
        event.stop()
        self.post_message(self.Clicked(self._index))


class CompletionPopup(VerticalScroll):
    """Popup widget that displays completion suggestions as clickable options."""

    DEFAULT_CSS = """
    CompletionPopup {
        display: none;
        height: auto;
        max-height: 12;
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

        existing = len(self._options)
        needed = len(suggestions)

        for i in range(min(existing, needed)):
            label, desc = suggestions[i]
            self._options[i].set_content(
                label, desc, i, is_selected=(i == selected_index)
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

    ChatInput #input-box {
        height: auto;
        min-height: 3;
        max-height: 20;
        border: solid $primary;
        background: $background;
        color: $foreground;
        padding: 0;
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

    ChatInput ChatTextArea {
        width: 1fr;
        height: auto;
        min-height: 1;
        max-height: 10;
        border: none;
        background: transparent;
        padding: 0;
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
        self._history: list[str] = []
        self._history_index: int = -1
        self._saved_draft: str = ""
        self._pasted_full_text: str | None = None
        self._cwd = Path.cwd()
        self._argument_hints: dict[str, str] = {}
        self._current_suggestions: list[tuple[str, str]] = []
        self._current_selected_index: int = 0
        self._completion_prefix_len: int = 0
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

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal, Vertical
        with Vertical(id="input-box"):
            with Horizontal(classes="input-row"):
                yield self._prompt_widget
                yield self._text_area
            yield self._autocomplete

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
            self._text_area.text = value
            self._sync_mode()

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
        if self._completion_manager:
            self._completion_manager.reset()
        self.clear_completion_suggestions()
        return True

    def clear(self) -> None:
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
        pending_approval = getattr(app, "_pending_approval_widget", None) if app is not None else None
        if pending_approval is not None and getattr(pending_approval, "is_mounted", False) and getattr(pending_approval, "display", True):
            if key in ("up", "k"):
                event.prevent_default()
                event.stop()
                pending_approval.action_move_up()
                return True
            if key in ("down", "j"):
                event.prevent_default()
                event.stop()
                pending_approval.action_move_down()
                return True
            if key == "enter":
                event.prevent_default()
                event.stop()
                pending_approval.action_select()
                return True
            if key == "y":
                event.prevent_default()
                event.stop()
                pending_approval.action_select_approve()
                return True
            if key in ("s", "a"):
                event.prevent_default()
                event.stop()
                if hasattr(pending_approval, "action_select_smart"):
                    pending_approval.action_select_smart()
                else:
                    pending_approval.action_select_auto()
                return True
            if key in ("n", "escape"):
                event.prevent_default()
                event.stop()
                pending_approval.action_select_reject()
                return True
            if key == "1":
                event.prevent_default()
                event.stop()
                pending_approval.action_select_position(0)
                return True
            if key == "2":
                event.prevent_default()
                event.stop()
                pending_approval.action_select_position(1)
                return True
            if key == "3":
                event.prevent_default()
                event.stop()
                pending_approval.action_select_position(2)
                return True
            if key == "e":
                event.prevent_default()
                event.stop()
                pending_approval.action_toggle_expand()
                return True
            if key == "tab":
                event.prevent_default()
                event.stop()
                pending_approval.action_reject_with_reason()
                return True
            if not getattr(pending_approval, "_reason_input_active", False):
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

        if not self._history or self._history[-1] != content:
            self._history.append(content)
        self._history_index = -1
        self._saved_draft = ""

        self.clear()
        self.mode = "normal"

        self.post_message(self.SlashCommandEnded())
        self.post_message(self.Submitted(content, mode))

    # ── History ─────────────────────────────────────────

    def action_history_prev(self) -> None:
        """Navigate backward in history."""
        if not self._history:
            return
        if self._history_index == -1:
            self._saved_draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1

        self.text = self._history[self._history_index]

    def action_history_next(self) -> None:
        """Navigate forward in history."""
        if self._history_index == -1:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.text = self._history[self._history_index]
        else:
            self._history_index = -1
            self.text = self._saved_draft

    def action_dismiss_or_clear(self) -> None:
        """Clear input text and end slash command mode."""
        self.clear()
