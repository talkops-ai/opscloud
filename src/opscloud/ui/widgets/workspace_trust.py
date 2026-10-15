"""Startup Workspace Trust and Settings Error modal screens for OpsCloud.

Startup Workspace Trust Prompt Layout::

    Accessing workspace:

    /Users/structbinary/Downloads

    Do you trust the contents of this project?

    OpsCloud CLI requires permission to read, edit, and execute files here.

    > Yes, I trust this folder
      No, exit

      ↑/↓ Navigate · enter Confirm
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import events
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from opscloud.config.cli_settings import resolve_path_no_slash
from opscloud.config.settings import get_glyphs
from opscloud.ui.theme import get_theme_colors

if TYPE_CHECKING:
    from textual.app import ComposeResult

WORKSPACE_TRUST_OPTIONS: tuple[str, str] = (
    "Yes, I trust this folder",
    "No, exit",
)

SETTINGS_ERROR_OPTIONS: tuple[str, str] = (
    "Yes, quarantine and reset to defaults",
    "No, exit",
)

DEFAULT_PRODUCT_NAME = "OpsCloud CLI"


class WorkspaceTrustScreen(ModalScreen[bool]):
    """Full-screen startup prompt asking whether the user trusts the current workspace folder.

    Dismisses with ``True`` when the user selects ``"Yes, I trust this folder"``
    and ``False`` when the user selects ``"No, exit"`` or presses ``Escape``.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "Navigate Up", show=False, priority=True),
        Binding("k", "cursor_up", "Navigate Up", show=False, priority=True),
        Binding("down", "cursor_down", "Navigate Down", show=False, priority=True),
        Binding("j", "cursor_down", "Navigate Down", show=False, priority=True),
        Binding("enter", "confirm", "Confirm", show=False, priority=True),
        Binding("escape", "cancel", "Exit", show=False, priority=True),
        Binding("q", "cancel", "Exit", show=False, priority=True),
        Binding("y", "select_yes", "Yes", show=False, priority=True),
        Binding("n", "select_no", "No", show=False, priority=True),
        Binding("1", "select_yes", "First option", show=False, priority=True),
        Binding("2", "select_no", "Second option", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    WorkspaceTrustScreen {
        align: left top;
        background: $background;
        padding: 1 2;
    }

    WorkspaceTrustScreen #wt-container {
        width: 100%;
        height: auto;
        background: $background;
    }

    WorkspaceTrustScreen .wt-muted {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }

    WorkspaceTrustScreen .wt-path {
        color: $text;
        text-style: bold;
        height: auto;
        margin-bottom: 1;
    }

    WorkspaceTrustScreen .wt-question {
        color: $text;
        height: auto;
        margin-bottom: 1;
    }

    WorkspaceTrustScreen .wt-description {
        color: $text;
        height: auto;
        margin-bottom: 1;
    }

    WorkspaceTrustScreen #wt-options {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }

    WorkspaceTrustScreen .wt-option {
        width: auto;
        height: 1;
    }

    WorkspaceTrustScreen .wt-hint {
        color: $text-muted;
        height: auto;
    }
    """

    can_focus = True
    selected: reactive[int] = reactive(0)

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        product_name: str = DEFAULT_PRODUCT_NAME,
    ) -> None:
        super().__init__()
        self._workspace_dir = resolve_path_no_slash(workspace_dir) or str(workspace_dir)
        self._product_name = product_name

    @property
    def workspace_dir(self) -> str:
        """Return the normalized workspace path displayed on screen."""
        return self._workspace_dir

    @property
    def options(self) -> tuple[str, str]:
        """Return the selectable options tuple."""
        return WORKSPACE_TRUST_OPTIONS

    def compose(self) -> ComposeResult:
        glyphs = get_glyphs()
        hint_text = (
            f"  {glyphs.arrow_up}/{glyphs.arrow_down} Navigate "
            f"{glyphs.separator} enter Confirm"
        )
        with Vertical(id="wt-container"):
            yield Static(
                "Accessing workspace:",
                id="wt-header",
                classes="wt-muted",
                markup=False,
            )
            yield Static(
                self._workspace_dir,
                id="wt-workspace-path",
                classes="wt-path",
                markup=False,
            )
            yield Static(
                "Do you trust the contents of this project?",
                id="wt-question",
                classes="wt-question",
                markup=False,
            )
            yield Static(
                f"{self._product_name} requires permission to read, edit, and execute files here.",
                id="wt-description",
                classes="wt-description",
                markup=False,
            )
            with Vertical(id="wt-options"):
                for idx, label in enumerate(WORKSPACE_TRUST_OPTIONS):
                    yield Static(
                        self._render_option(idx, label),
                        id=f"wt-option-{idx}",
                        classes="wt-option",
                    )
            yield Static(
                hint_text,
                id="wt-footer",
                classes="wt-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self._refresh_options()
        self.focus()

    def _render_option(self, index: int, label: str) -> Text:
        colors = get_theme_colors(self)
        is_selected = index == self.selected
        prefix = "> " if is_selected else "  "
        text = Text()
        if is_selected:
            text.append(prefix, style=f"bold {colors.primary}")
            text.append(label, style=f"bold {colors.primary}")
        else:
            text.append(prefix, style=colors.muted)
            text.append(label, style=colors.foreground)
        return text

    def _refresh_options(self) -> None:
        for idx, label in enumerate(WORKSPACE_TRUST_OPTIONS):
            try:
                widget = self.query_one(f"#wt-option-{idx}", Static)
                widget.update(self._render_option(idx, label))
            except Exception:
                pass

    def watch_selected(self, _old_value: int, _new_value: int) -> None:
        if self.is_mounted:
            self._refresh_options()

    def action_cursor_up(self) -> None:
        """Move selection up (wrapping modulo 2)."""
        self.selected = (self.selected - 1) % len(WORKSPACE_TRUST_OPTIONS)

    def action_cursor_down(self) -> None:
        """Move selection down (wrapping modulo 2)."""
        self.selected = (self.selected + 1) % len(WORKSPACE_TRUST_OPTIONS)

    def action_confirm(self) -> None:
        """Confirm the highlighted option."""
        self.dismiss(self.selected == 0)

    def action_cancel(self) -> None:
        """Exit without trusting the workspace."""
        self.dismiss(False)

    def action_select_yes(self) -> None:
        """Select 'Yes, I trust this folder' and confirm."""
        self.selected = 0
        self.dismiss(True)

    def action_select_no(self) -> None:
        """Select 'No, exit' and confirm."""
        self.selected = 1
        self.dismiss(False)

    def on_click(self, event: events.Click) -> None:
        """Allow clicking an option to select and confirm it."""
        widget = event.widget
        if widget is not None and widget.id in {"wt-option-0", "wt-option-1"}:
            idx = int(widget.id.rsplit("-", 1)[-1])
            self.selected = idx
            self.dismiss(idx == 0)

    def render_plain_text(self) -> str:
        """Render the plain-text representation of the prompt (for testing/inspection)."""
        glyphs = get_glyphs()
        lines = [
            "Accessing workspace:",
            "",
            self._workspace_dir,
            "",
            "Do you trust the contents of this project?",
            "",
            f"{self._product_name} requires permission to read, edit, and execute files here.",
            "",
        ]
        for idx, label in enumerate(WORKSPACE_TRUST_OPTIONS):
            prefix = "> " if idx == self.selected else "  "
            lines.append(f"{prefix}{label}")
        lines.append("")
        lines.append(
            f"  {glyphs.arrow_up}/{glyphs.arrow_down} Navigate {glyphs.separator} enter Confirm"
        )
        return "\n".join(lines)


class SettingsErrorScreen(ModalScreen[bool]):
    """Full-screen startup recovery prompt when ``settings.json`` is malformed or invalid.

    Offers to quarantine the broken ``settings.json`` to
    ``settings.json.corrupted.<unix_ms>`` and reset to defaults, or exit.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "Navigate Up", show=False, priority=True),
        Binding("k", "cursor_up", "Navigate Up", show=False, priority=True),
        Binding("down", "cursor_down", "Navigate Down", show=False, priority=True),
        Binding("j", "cursor_down", "Navigate Down", show=False, priority=True),
        Binding("enter", "confirm", "Confirm", show=False, priority=True),
        Binding("escape", "cancel", "Exit", show=False, priority=True),
        Binding("q", "cancel", "Exit", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    SettingsErrorScreen {
        align: left top;
        background: $background;
        padding: 1 2;
    }

    SettingsErrorScreen #se-container {
        width: 100%;
        height: auto;
        background: $background;
    }

    SettingsErrorScreen .se-header {
        color: $error;
        text-style: bold;
        height: auto;
        margin-bottom: 1;
    }

    SettingsErrorScreen .se-path {
        color: $text;
        text-style: bold;
        height: auto;
        margin-bottom: 1;
    }

    SettingsErrorScreen .se-error {
        color: $warning;
        height: auto;
        margin-bottom: 1;
    }

    SettingsErrorScreen .se-question {
        color: $text;
        height: auto;
        margin-bottom: 1;
    }

    SettingsErrorScreen #se-options {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }

    SettingsErrorScreen .se-option {
        width: auto;
        height: 1;
    }

    SettingsErrorScreen .se-hint {
        color: $text-muted;
        height: auto;
    }
    """

    can_focus = True
    selected: reactive[int] = reactive(0)

    def __init__(self, settings_path: str | Path, error: Exception | str) -> None:
        super().__init__()
        self._settings_path = str(settings_path)
        self._error_text = str(error)

    def compose(self) -> ComposeResult:
        glyphs = get_glyphs()
        hint_text = (
            f"  {glyphs.arrow_up}/{glyphs.arrow_down} Navigate "
            f"{glyphs.separator} enter Confirm"
        )
        with Vertical(id="se-container"):
            yield Static(
                "Invalid settings file:",
                id="se-header",
                classes="se-header",
                markup=False,
            )
            yield Static(
                self._settings_path,
                id="se-path",
                classes="se-path",
                markup=False,
            )
            yield Static(
                self._error_text,
                id="se-error",
                classes="se-error",
                markup=False,
            )
            yield Static(
                "Quarantine the corrupted file and reset settings to defaults?",
                id="se-question",
                classes="se-question",
                markup=False,
            )
            with Vertical(id="se-options"):
                for idx, label in enumerate(SETTINGS_ERROR_OPTIONS):
                    yield Static(
                        self._render_option(idx, label),
                        id=f"se-option-{idx}",
                        classes="se-option",
                    )
            yield Static(
                hint_text,
                id="se-footer",
                classes="se-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self._refresh_options()
        self.focus()

    def _render_option(self, index: int, label: str) -> Text:
        colors = get_theme_colors(self)
        is_selected = index == self.selected
        prefix = "> " if is_selected else "  "
        text = Text()
        if is_selected:
            text.append(prefix, style=f"bold {colors.primary}")
            text.append(label, style=f"bold {colors.primary}")
        else:
            text.append(prefix, style=colors.muted)
            text.append(label, style=colors.foreground)
        return text

    def _refresh_options(self) -> None:
        for idx, label in enumerate(SETTINGS_ERROR_OPTIONS):
            try:
                widget = self.query_one(f"#se-option-{idx}", Static)
                widget.update(self._render_option(idx, label))
            except Exception:
                pass

    def watch_selected(self, _old_value: int, _new_value: int) -> None:
        if self.is_mounted:
            self._refresh_options()

    def action_cursor_up(self) -> None:
        self.selected = (self.selected - 1) % len(SETTINGS_ERROR_OPTIONS)

    def action_cursor_down(self) -> None:
        self.selected = (self.selected + 1) % len(SETTINGS_ERROR_OPTIONS)

    def action_confirm(self) -> None:
        self.dismiss(self.selected == 0)

    def action_cancel(self) -> None:
        self.dismiss(False)


__all__ = [
    "DEFAULT_PRODUCT_NAME",
    "SETTINGS_ERROR_OPTIONS",
    "WORKSPACE_TRUST_OPTIONS",
    "SettingsErrorScreen",
    "WorkspaceTrustScreen",
]
