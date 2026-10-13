"""Interactive reasoning effort selector modal for `/effort`."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.binding import Binding, BindingType
from textual.css.query import NoMatches
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from opscloud.config.settings import get_glyphs
from opscloud.ui.widgets.modal_base import OpsModalScreen, format_modal_option_row


class EffortSelectorScreen(OpsModalScreen[str | None]):
    """Modal dialog for selecting a reasoning effort level."""

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("tab", "cursor_down", "Next", show=False, priority=True),
        Binding("shift+tab", "cursor_up", "Previous", show=False, priority=True),
    ]

    _EFFORT_DESCRIPTIONS: ClassVar[dict[str, str]] = {
        "low": "Fast responses, minimal thinking tokens",
        "medium": "Balanced reasoning depth and speed",
        "high": "Maximum thinking budget and depth",
        "off": "Disable explicit reasoning effort",
    }

    DEFAULT_CSS = """
    EffortSelectorScreen .effort-selector-title {
        display: none;
    }

    EffortSelectorScreen .effort-selector-subtitle {
        display: none;
    }

    EffortSelectorScreen OptionList {
        height: auto;
        max-height: 14;
        background: $background;
        border: solid $primary 20%;
    }

    EffortSelectorScreen OptionList > .option-list--option-highlighted {
        background: $primary 18%;
        color: $text;
        text-style: bold;
    }

    EffortSelectorScreen .effort-selector-help {
        display: none;
    }
    """

    def __init__(
        self,
        *,
        model_spec: str,
        efforts: tuple[str, ...],
        current_effort: str | None = None,
        default_effort: str | None = None,
    ) -> None:
        super().__init__()
        self._model_spec = model_spec
        self._efforts = efforts or ("low", "medium", "high")
        self._current_effort = current_effort
        self._default_effort = default_effort or "high"
        self._dismissed: bool = False
        self._last_row_width: int | None = None

    def _format_label(self, effort: str) -> str:
        markers = []
        if effort == self._current_effort:
            markers.append("current")
        if effort == self._default_effort:
            markers.append("default")
        if markers:
            suffix = ", ".join(markers)
            return f"{effort} ({suffix})"
        return effort

    def _build_options(self, width: int | None = None) -> list[Option]:
        options: list[Option] = []
        w = width if width is not None else self._option_content_width("#effort-options", fallback=50)
        for effort in self._efforts:
            badges: list[tuple[str, str]] = []
            if effort == self._current_effort:
                badges.append(("ACTIVE", "bold $success"))
            if effort == self._default_effort:
                badges.append(("DEFAULT", "bold $primary"))

            desc = self._EFFORT_DESCRIPTIONS.get(
                effort.lower(), f"{effort.capitalize()} reasoning effort"
            )
            content = format_modal_option_row(
                label=effort.upper(),
                description=desc,
                badges=badges if badges else None,
                width=w,
            )
            options.append(Option(content, id=effort))
        return options

    def compose_modal(self):
        highlighted_effort = self._current_effort or self._default_effort
        try:
            highlighted = self._efforts.index(highlighted_effort)
        except ValueError:
            highlighted = 0

        yield Static("Select Reasoning Effort", classes="effort-selector-title")
        yield Static(self._model_spec, classes="effort-selector-subtitle")
        option_list = OptionList(*self._build_options(), id="effort-options")
        option_list.highlighted = highlighted
        yield option_list
        help_text = "↑/↓ or Tab switch  •  Enter select  •  Esc cancel"
        yield Static(help_text, classes="effort-selector-help")

    def on_mount(self) -> None:
        super().on_mount()
        glyphs = get_glyphs()
        self.set_modal_title("Reasoning Effort", counter=self._model_spec)
        self.set_modal_subtitle([
            (f"{glyphs.arrow_up}{glyphs.arrow_down}", "navigate"),
            ("enter", "select"),
            ("esc", "cancel"),
        ])
        try:
            self.query_one("#effort-options", OptionList).focus()
        except NoMatches:
            pass
        self.call_after_refresh(self._reformat_options)

    def on_resize(self) -> None:
        width = self._option_content_width("#effort-options", fallback=50)
        if width != self._last_row_width:
            self._last_row_width = width
            self.call_after_refresh(self._reformat_options)

    def _reformat_options(self) -> None:
        try:
            ol = self.query_one("#effort-options", OptionList)
        except NoMatches:
            return
        highlighted = ol.highlighted if ol.highlighted is not None else 0
        w = self._option_content_width("#effort-options", fallback=50)
        ol.clear_options()
        ol.add_options(self._build_options(width=w))
        if 0 <= highlighted < len(self._efforts):
            ol.highlighted = highlighted

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
        if getattr(self, "_dismissed", False):
            return
        effort = event.option.id
        if effort is not None:
            self.dismiss(effort)

    def action_cancel(self) -> None:
        if getattr(self, "_dismissed", False):
            return
        self.dismiss(None)

    def action_cursor_down(self) -> None:
        try:
            self.query_one("#effort-options", OptionList).action_cursor_down()
        except NoMatches:
            pass

    def action_cursor_up(self) -> None:
        try:
            self.query_one("#effort-options", OptionList).action_cursor_up()
        except NoMatches:
            pass


__all__ = ["EffortSelectorScreen"]
