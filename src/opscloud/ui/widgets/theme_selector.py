"""Interactive theme selector modal screen for `/theme` command."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import os
from typing import Any, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from opscloud.ui.theme import (
    get_registry,
    load_terminal_default,
    save_terminal_theme_mapping,
)

logger = get_logger(__name__)


class ThemeSelectorScreen(ModalScreen[str | None]):
    """Modal screen for interactive theme selection with live preview."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("tab", "cursor_down", "Next", show=False, priority=True),
        Binding("shift+tab", "cursor_up", "Previous", show=False, priority=True),
        Binding("n", "toggle_names", "Names", show=False),
        Binding("t", "set_for_terminal", "Set for terminal", show=False),
    ]

    CSS = """
    ThemeSelectorScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    ThemeSelectorScreen > Vertical {
        width: 54;
        max-width: 90%;
        height: auto;
        max-height: 90vh;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    ThemeSelectorScreen .theme-selector-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    ThemeSelectorScreen OptionList {
        height: auto;
        max-height: 16;
        background: $background;
        border: none;
    }

    ThemeSelectorScreen .theme-selector-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
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

    def compose(self):
        options: list[Option] = []
        highlight_index = 0

        registry = get_registry()
        for i, (name, entry) in enumerate(registry.items()):
            options.append(Option(self._format_option(name, entry.label), id=name))
            if name == self._current_theme:
                highlight_index = i

        with Vertical():
            yield Static("Select Theme", classes="theme-selector-title")
            option_list = OptionList(*options, id="theme-options")
            option_list.highlighted = highlight_index
            yield option_list
            nav_line = "↑/↓ or Tab switch • Enter select • Esc cancel"
            action_line = "N labels/keys • T set for this terminal"
            yield Static(f"{nav_line}\n{action_line}", classes="theme-selector-help")

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
        registry = get_registry()
        new_options = [
            Option(self._format_option(name, entry.label), id=name)
            for name, entry in registry.items()
        ]
        option_list.clear_options()
        option_list.add_options(new_options)
        if cursor is not None:
            option_list.highlighted = cursor


__all__ = ["ThemeSelectorScreen"]
