"""Interactive theme selector modal screen for `/theme` command."""

from __future__ import annotations

import os
from typing import Any, ClassVar

from textual.binding import Binding, BindingType
from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from opscloud.config.settings import get_glyphs
from opscloud.ui.theme import (
    ThemeEntry,
    get_registry,
    load_terminal_default,
    save_terminal_theme_mapping,
)
from opscloud.ui.widgets.modal_base import OpsModalScreen, cell_len
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


class ThemeSelectorScreen(OpsModalScreen[str | None]):
    """Modal screen for interactive theme selection with live preview and swatches."""

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("tab", "cursor_down", "Next", show=False, priority=True),
        Binding("shift+tab", "cursor_up", "Previous", show=False, priority=True),
        Binding("n", "toggle_names", "Names", show=False),
        Binding("t", "set_for_terminal", "Set for terminal", show=False),
    ]

    DEFAULT_CSS = """
    ThemeSelectorScreen .theme-selector-title {
        display: none;
    }

    ThemeSelectorScreen OptionList {
        height: auto;
        max-height: 18;
        background: $background;
        border: solid $primary 20%;
    }

    ThemeSelectorScreen OptionList > .option-list--option-highlighted {
        background: $primary 18%;
        color: $text;
        text-style: bold;
    }

    ThemeSelectorScreen .theme-selector-help {
        display: none;
    }
    """

    def __init__(self, current_theme: str, terminal_default: str | None = None) -> None:
        super().__init__()
        self._current_theme = current_theme
        self._original_theme = current_theme
        self._terminal_default = terminal_default or load_terminal_default()
        self._session_terminal_default: str | None = None
        self._show_keys = False
        self._dismissed: bool = False
        self._last_row_width: int | None = None

    def _format_option_content(
        self, name: str, entry: ThemeEntry, width: int = 58
    ) -> Content:
        display_name = name if self._show_keys else entry.label
        badges: list[tuple[str, str]] = []
        if name == self._current_theme:
            badges.append(("ACTIVE", "bold $success"))
        if name == self._terminal_default:
            badges.append(("DEFAULT", "bold $primary"))
        badges.append(("DARK" if entry.dark else "LIGHT", "dim"))

        pieces: list[tuple[str, str]] = [
            ("  ", ""),
            (display_name, "bold $foreground"),
            (" ", ""),
        ]
        left_len = 2 + cell_len(display_name) + 1
        for b_text, b_style in badges:
            badge_chunk = f"[{b_text}] "
            pieces.append((badge_chunk, b_style))
            left_len += cell_len(badge_chunk)

        c = entry.colors
        gap = max(1, width - left_len - 8)
        pieces.append((" " * gap, ""))
        pieces.extend([
            ("■ ", c.primary),
            ("■ ", c.secondary),
            ("■ ", c.accent),
            ("■", c.surface),
        ])
        return Content.assemble(*pieces)

    def _format_option(self, name: str, label: str) -> str:
        text = name if self._show_keys else label
        suffixes: list[str] = []
        if name == self._current_theme:
            suffixes.append("current")
        if name == self._terminal_default:
            suffixes.append("default")
        if suffixes:
            text = f"{text} ({', '.join(suffixes)})"
        return text

    def _current_index(self) -> int:
        registry = get_registry()
        for i, name in enumerate(registry.keys()):
            if name == self._current_theme:
                return i
        return 0

    def _build_options(self, width: int | None = None) -> list[Option]:
        registry = get_registry()
        options: list[Option] = []
        w = width if width is not None else self._option_content_width("#theme-options", fallback=58)
        for name, entry in registry.items():
            content = self._format_option_content(name, entry, width=w)
            options.append(Option(content, id=name))
        return options

    def compose_modal(self):
        yield Static("Select Theme", classes="theme-selector-title")
        option_list = OptionList(*self._build_options(), id="theme-options")
        option_list.highlighted = self._current_index()
        yield option_list
        nav_line = "↑/↓ or Tab switch • Enter select • Esc cancel"
        action_line = "N labels/keys • T set for this terminal"
        yield Static(f"{nav_line}\n{action_line}", classes="theme-selector-help")

    def on_mount(self) -> None:
        super().on_mount()
        glyphs = get_glyphs()
        registry = get_registry()
        self.set_modal_title("Select Theme", counter=f"{len(registry)} available")
        self.set_modal_subtitle([
            (f"{glyphs.arrow_up}{glyphs.arrow_down}", "preview"),
            ("enter", "select"),
            ("n", "keys"),
            ("t", "save default"),
            ("esc", "cancel"),
        ])
        try:
            self.query_one("#theme-options", OptionList).focus()
        except NoMatches:
            pass
        self.call_after_refresh(self._reformat_options)

    def on_resize(self) -> None:
        width = self._option_content_width("#theme-options", fallback=58)
        if width != self._last_row_width:
            self._last_row_width = width
            self.call_after_refresh(self._reformat_options)

    def _reformat_options(self) -> None:
        try:
            ol = self.query_one("#theme-options", OptionList)
        except NoMatches:
            return
        highlighted = ol.highlighted if ol.highlighted is not None else 0
        w = self._option_content_width("#theme-options", fallback=58)
        ol.clear_options()
        ol.add_options(self._build_options(width=w))
        if 0 <= highlighted < len(get_registry()):
            ol.highlighted = highlighted

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        """Live-preview highlighted theme."""
        name = event.option.id
        registry = get_registry()
        if name is not None and name in registry:
            try:
                self.app.theme = name
                stack = self.app.screen_stack
                if len(stack) > 1:
                    stack[-2].refresh(layout=True)
            except Exception:
                logger.warning("Failed to preview theme '%s'", name, exc_info=True)
                try:
                    self.app.theme = self._original_theme
                except Exception:
                    pass

    def dismiss(self, result: str | None = None) -> Any:
        """Safely dismiss the modal screen with idempotency protection."""
        if getattr(self, "_dismissed", False):
            return None
        self._dismissed = True

        try:
            app = self.app
        except Exception:
            app = None

        if app is not None:
            try:
                screen_stack = app._screen_stack
            except Exception:
                screen_stack = None
            if screen_stack is not None:
                if len(screen_stack) <= 1 or self not in screen_stack:
                    return None

        try:
            return super().dismiss(result)
        except Exception:
            return None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Commit selected theme."""
        if getattr(self, "_dismissed", False):
            return
        name = event.option.id
        registry = get_registry()
        if name is not None and name in registry:
            self.dismiss(name)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        """Dismiss screen and revert to original or terminal-default theme."""
        if getattr(self, "_dismissed", False):
            return
        target = self._session_terminal_default or self._original_theme
        try:
            self.app.theme = target
        except Exception:
            pass
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        self.query_one(OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(OptionList).action_cursor_up()

    def action_toggle_names(self) -> None:
        self._show_keys = not self._show_keys
        self._rerender_options()

    def action_set_for_terminal(self) -> None:
        term_program = os.environ.get("TERM_PROGRAM", "").strip()
        if not term_program:
            self.app.notify(
                "TERM_PROGRAM is unset; cannot set per-terminal default.",
                severity="warning",
            )
            return

        option_list = self.query_one(OptionList)
        if option_list.highlighted is None:
            return
        option = option_list.get_option_at_index(option_list.highlighted)
        name = option.id
        if name is None or name not in get_registry():
            return

        self._session_terminal_default = name
        saved = save_terminal_theme_mapping(term_program, name)
        if saved:
            self._terminal_default = name
            self._rerender_options()
            self.app.notify(
                f"Set '{name}' as default for {term_program}.",
                severity="information",
            )
        else:
            self.app.notify("Failed to save terminal theme mapping.", severity="error")

    def _rerender_options(self) -> None:
        option_list = self.query_one(OptionList)
        cursor = option_list.highlighted
        new_options = self._build_options()
        option_list.clear_options()
        option_list.add_options(new_options)
        if cursor is not None:
            option_list.highlighted = cursor


__all__ = ["ThemeSelectorScreen"]
