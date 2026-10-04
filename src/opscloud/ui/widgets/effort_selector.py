"""Interactive reasoning effort selector modal for `/effort`."""

from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class EffortSelectorScreen(ModalScreen[str | None]):
    """Modal dialog for selecting a reasoning effort level."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("tab", "cursor_down", "Next", show=False, priority=True),
        Binding("shift+tab", "cursor_up", "Previous", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    EffortSelectorScreen {
        align: center middle;
        background: transparent;
    }

    EffortSelectorScreen > Vertical {
        width: 54;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    EffortSelectorScreen .effort-selector-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    EffortSelectorScreen .effort-selector-subtitle {
        height: auto;
        color: $text-muted;
        text-align: center;
        margin-bottom: 1;
    }

    EffortSelectorScreen OptionList {
        height: auto;
        max-height: 10;
        background: $background;
        border: solid $accent;
    }

    EffortSelectorScreen .effort-selector-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
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

    def compose(self):
        options = [
            Option(self._format_label(effort), id=effort) for effort in self._efforts
        ]
        highlighted_effort = self._current_effort or self._default_effort
        try:
            highlighted = self._efforts.index(highlighted_effort)
        except ValueError:
            highlighted = 0

        help_text = "↑/↓ or Tab switch  •  Enter select  •  Esc cancel"

        with Vertical():
            yield Static("Select Reasoning Effort", classes="effort-selector-title")
            yield Static(self._model_spec, classes="effort-selector-subtitle")
            option_list = OptionList(*options, id="effort-options")
            option_list.highlighted = highlighted
            yield option_list
            yield Static(help_text, classes="effort-selector-help")

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

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        effort = event.option.id
        if effort is not None:
            self.dismiss(effort)

    def action_cancel(self) -> None:
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
