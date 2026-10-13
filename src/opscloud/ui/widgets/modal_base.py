"""Modal foundation primitives and base container for OpsCloud TUI.

Provides OpsModalScreen, ModalDialog, PillTabs, ModalSearchInput, and
modal option row formatting helpers for all command modals.
"""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar

from rich.cells import cell_len
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static

from opscloud.config.settings import get_glyphs, is_ascii_mode

T = TypeVar("T")

_CSS_PATH = Path(__file__).parents[1] / "modal.tcss"


# ── Formatters ──────────────────────────────────────────────


def format_modal_hints(
    hints: Sequence[tuple[str, str]],
    max_width: int | None = None,
) -> Content:
    """Format keycap hints for the modal bottom border.

    Args:
        hints: Sequence of ``(key, action)`` pairs, e.g. ``[("↑↓", "navigate"), ("esc", "close")]``.
        max_width: Optional max cells. Trailing hints are dropped to fit.
    """
    sep = f" {get_glyphs().separator} "
    kept: list[tuple[str, str]] = []
    width = 0
    for key, action in hints:
        extra = len(key) + 1 + len(action) + (len(sep) if kept else 0)
        if max_width is not None and width + extra > max_width:
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


def truncate_cells(value: str, width: int, suffix: str | None = None) -> str:
    """Truncate text to terminal cells without splitting wide characters."""
    if width <= 0:
        return ""
    if cell_len(value) <= width:
        return value
    glyphs = get_glyphs()
    sfx = suffix if suffix is not None else glyphs.ellipsis
    sfx_len = cell_len(sfx)
    if width < sfx_len:
        used = 0
        end = 0
        for ch in value:
            cl = cell_len(ch)
            if used + cl > width:
                break
            used += cl
            end += 1
        return value[:end]
    budget = width - sfx_len
    used = 0
    end = 0
    for ch in value:
        cl = cell_len(ch)
        if used + cl > budget:
            break
        used += cl
        end += 1
    return value[:end] + sfx


def format_modal_option_row(
    label: str,
    description: str = "",
    *,
    badges: Sequence[str | tuple[str, str]] | None = None,
    meta: str | Content | None = None,
    status: str | tuple[str, str] | None = None,
    width: int = 76,
    is_selected: bool = False,
) -> Content:
    """Format a two-tier option row for modal OptionList or custom views.

    Line 1: selection bar + bold label + badges ... right-aligned (status + meta).
    Line 2: 2-space indent + dim description (truncated with ellipsis if too wide).

    Args:
        label: Primary name or title (rendered bold).
        description: Secondary description or summary (rendered dim).
        badges: Optional badges as strings or ``(text, style)`` tuples.
        meta: Right-aligned metadata/version string.
        status: Optional status tag (e.g. ``("✓ Enabled", "bold $success")``).
        width: Available row width in cells.
        is_selected: Whether this row currently holds the selection.
    """
    glyphs = get_glyphs()

    # Selection indicator
    if is_selected:
        bar_text = f"{glyphs.selection_bar} "
        bar = (bar_text, "bold $primary")
    else:
        bar = ("  ", "")

    line1_pieces: list[tuple[str, str]] = [bar, (label, "bold $foreground")]
    left_cells = cell_len(bar[0]) + cell_len(label)

    # Badges
    if badges:
        for b in badges:
            if isinstance(b, tuple):
                b_text, b_style = b
            else:
                b_text, b_style = b, "dim"
            badge_str = f" [{b_text}]"
            line1_pieces.append((badge_str, b_style))
            left_cells += cell_len(badge_str)

    # Right side: status + meta
    right_pieces: list[tuple[str, str]] = []
    right_cells = 0
    if status:
        if isinstance(status, tuple):
            s_text, s_style = status
        else:
            s_text, s_style = status, "bold"
        right_pieces.append((s_text, s_style))
        right_cells += cell_len(s_text)

    if meta:
        if isinstance(meta, Content):
            meta_str = meta.plain
            m_style = "dim"
        else:
            meta_str = str(meta)
            m_style = "dim"
        if right_pieces:
            sep = f" {glyphs.separator} "
            right_pieces.append((sep, "dim"))
            right_cells += cell_len(sep)
        right_pieces.append((meta_str, m_style))
        right_cells += cell_len(meta_str)

    # Spacing between left and right parts
    if right_cells > 0:
        gap = max(1, width - left_cells - right_cells)
        line1_pieces.append((" " * gap, ""))
        line1_pieces.extend(right_pieces)

    # Line 2: description
    if description:
        desc_single = " ".join(description.split())
        max_desc_width = max(10, width - 2)
        desc_text = truncate_cells(desc_single, max_desc_width)

        full_pieces = list(line1_pieces)
        full_pieces.append(("\n  ", ""))
        full_pieces.append((desc_text, "dim"))
        return Content.assemble(*full_pieces)

    return Content.assemble(*line1_pieces)


@dataclass
class ModalOptionRow:
    """Helper data structure representing a structured modal option row."""

    label: str
    description: str = ""
    badges: list[str | tuple[str, str]] = field(default_factory=list)
    meta: str | Content | None = None
    status: str | tuple[str, str] | None = None
    is_selected: bool = False

    def render(self, width: int = 76) -> Content:
        """Render this row as a formatted Content instance."""
        return format_modal_option_row(
            self.label,
            self.description,
            badges=self.badges,
            meta=self.meta,
            status=self.status,
            width=width,
            is_selected=self.is_selected,
        )


# ── Pill Tabs ───────────────────────────────────────────────


class PillTab(Widget):
    """An individual pill button inside PillTabs."""

    label: reactive[str] = reactive("")
    count: reactive[int | None] = reactive(None)
    active: reactive[bool] = reactive(False)

    def __init__(
        self,
        label: str,
        *,
        id: str | None = None,
        count: int | None = None,
        active: bool = False,
    ) -> None:
        super().__init__(id=id)
        self.label = label
        self.count = count
        self.active = active

    @property
    def tab_id(self) -> str:
        return self.id or self.label.lower().replace(" ", "-")

    def render(self) -> Content:
        if self.count is not None:
            count_repr = f"[{self.count}]" if is_ascii_mode() else f"{self.count}"
            return Content.assemble(
                self.label,
                " ",
                Content.styled(count_repr, "dim"),
            )
        return Content(self.label)

    def watch_active(self, active: bool) -> None:
        self.set_class(active, "pill-tab--active")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if self.parent and isinstance(self.parent, PillTabs):
            self.parent.activate_tab(self.tab_id)


class PillTabs(Widget):
    """Horizontal bar of cohesive pill buttons for switching modal categories."""

    active_tab: reactive[str] = reactive("")

    class TabChanged(Message):
        """Posted when the active tab is switched."""

        def __init__(self, tab_id: str, tab_index: int) -> None:
            super().__init__()
            self.tab_id = tab_id
            self.tab_index = tab_index

    def __init__(
        self,
        tabs: Sequence[tuple[str, str] | tuple[str, str, int] | PillTab] | None = None,
        *,
        id: str | None = None,
        initial_tab: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._initial_tabs = list(tabs or [])
        self._initial_active = initial_tab

    def compose(self) -> ComposeResult:
        for item in self._initial_tabs:
            if isinstance(item, PillTab):
                yield item
            elif len(item) == 3:
                tab_id, label, count = item  # type: ignore[misc]
                yield PillTab(label, id=tab_id, count=count)
            else:
                tab_id, label = item  # type: ignore[misc]
                yield PillTab(label, id=tab_id)

    def on_mount(self) -> None:
        tabs = self.tabs
        if not tabs:
            return
        target = self._initial_active or tabs[0].tab_id
        self.activate_tab(target)

    @property
    def tabs(self) -> list[PillTab]:
        return list(self.query(PillTab))

    @property
    def active_index(self) -> int:
        for idx, tab in enumerate(self.tabs):
            if tab.tab_id == self.active_tab:
                return idx
        return 0

    def activate_tab(self, tab_id: str) -> None:
        """Activate the tab with ID *tab_id*."""
        tabs = self.tabs
        match_idx = -1
        for idx, tab in enumerate(tabs):
            is_match = (tab.tab_id == tab_id)
            tab.active = is_match
            if is_match:
                match_idx = idx

        if match_idx >= 0 and self.active_tab != tab_id:
            self.active_tab = tab_id
            self.post_message(self.TabChanged(tab_id, match_idx))

    def activate_index(self, index: int) -> None:
        """Activate tab by index with wrap-around."""
        tabs = self.tabs
        if not tabs:
            return
        target = tabs[index % len(tabs)]
        self.activate_tab(target.tab_id)

    def next_tab(self) -> None:
        """Switch to the next tab in sequence."""
        self.activate_index(self.active_index + 1)

    def previous_tab(self) -> None:
        """Switch to the previous tab in sequence."""
        self.activate_index(self.active_index - 1)

    def set_tab_count(self, tab_id: str, count: int | None) -> None:
        """Update the numeric badge count on a specific tab."""
        for tab in self.tabs:
            if tab.tab_id == tab_id:
                tab.count = count
                break

    def watch_active_tab(self, active_tab: str) -> None:
        for tab in self.tabs:
            tab.active = (tab.tab_id == active_tab)


# ── Modal Search Input ─────────────────────────────────────


class ModalSearchInput(Widget):
    """Minimal search input for modals with leading glyph and auto-list navigation."""

    class Changed(Message):
        """Posted when search query changes."""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class Submitted(Message):
        """Posted when search query is submitted."""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(
        self,
        placeholder: str = "Search...",
        *,
        id: str | None = None,
        input_id: str | None = None,
        target_list: Widget | None = None,
    ) -> None:
        super().__init__(id=id)
        self._placeholder = placeholder
        self._input_id = input_id
        self._target_list = target_list
        self._icon: Static | None = None
        self._input: Input | None = None

    def compose(self) -> ComposeResult:
        glyph = get_glyphs().search
        self._icon = Static(glyph, classes="modal-search-icon")
        self._input = Input(
            placeholder=self._placeholder,
            id=self._input_id or "modal-search-field",
        )
        yield self._icon
        yield self._input

    @property
    def input_widget(self) -> Input:
        if self._input is None:
            return self.query_one(Input)
        return self._input

    @property
    def value(self) -> str:
        return self.input_widget.value

    @value.setter
    def value(self, val: str) -> None:
        self.input_widget.value = val

    @property
    def placeholder(self) -> str:
        return self.input_widget.placeholder

    @placeholder.setter
    def placeholder(self, val: str) -> None:
        self.input_widget.placeholder = val

    @property
    def is_input_focused(self) -> bool:
        """Return True if the inner input widget has focus."""
        return self._input is not None and self._input.has_focus

    def focus(self, scroll_visible: bool = True) -> Self:  # type: ignore[override]
        self.input_widget.focus(scroll_visible=scroll_visible)
        return self

    def on_input_changed(self, event: Input.Changed) -> None:
        self.post_message(self.Changed(event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.post_message(self.Submitted(event.value))
        self._focus_target_list()

    def on_key(self, event: events.Key) -> None:
        if event.key == "down":
            event.prevent_default()
            event.stop()
            self._focus_target_list()

    def _focus_target_list(self) -> None:
        """Transfer focus to the linked or screen OptionList."""
        if self._target_list is not None and self._target_list.is_mounted:
            self._target_list.focus()
            return

        with contextlib.suppress(Exception):
            target = self.screen.query(OptionList).first()
            if target is not None:
                target.focus()
                if target.option_count > 0 and target.highlighted is None:
                    target.highlighted = 0


# ── Modal Dialog Container ──────────────────────────────────


class ModalDialog(Vertical):
    """Floating dialog container with round border and native title/subtitle."""

    pass


# ── OpsModalScreen ──────────────────────────────────────────


class OpsModalScreen(ModalScreen[T], Generic[T]):
    """Unified base screen for all OpsCloud command modals.

    Provides centered dialog presentation, native border titles/subtitles,
    clean Escape dismissal, tab switching shortcuts, and search focus bindings.
    """

    DIALOG_CLASSES: ClassVar[str] = ""

    DEFAULT_CSS = (
        _CSS_PATH.read_text(encoding="utf-8") if _CSS_PATH.exists() else ""
    )

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("ctrl+tab", "next_tab", "Next tab", show=False),
        Binding("ctrl+shift+tab", "previous_tab", "Previous tab", show=False),
        Binding("slash", "focus_search", "Search", show=False),
    ]

    def __init__(
        self,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.dialog: ModalDialog | None = None
        self._modal_title_text: str = ""
        self._modal_counter: int | str | None = None

    def compose(self) -> ComposeResult:
        """Compose the screen wrapping compose_modal() inside ModalDialog."""
        classes = f"ops-modal-dialog {self.DIALOG_CLASSES}".strip()
        with ModalDialog(id="modal-dialog", classes=classes) as dialog:
            self.dialog = dialog
            yield from self.compose_modal()

    def compose_modal(self) -> ComposeResult:
        """Override in subclasses to provide the inner dialog contents."""
        yield from ()

    def on_mount(self) -> Any:
        if self.dialog is None:
            with contextlib.suppress(NoMatches):
                self.dialog = self.query_one(ModalDialog)

        if self.dialog is not None:
            if not self.dialog.border_subtitle:
                glyphs = get_glyphs()
                default_hints = [
                    (f"{glyphs.arrow_up}{glyphs.arrow_down}", "navigate"),
                    (glyphs.newline, "select"),
                    ("esc", "close"),
                ]
                self.set_modal_subtitle(default_hints)

    def set_modal_title(
        self,
        title: str,
        counter: int | str | None = None,
    ) -> None:
        """Set the dialog's top border title with optional counter."""
        self._modal_title_text = title
        self._modal_counter = counter
        if counter is not None:
            text = f" {title} ({counter}) "
        else:
            text = f" {title} " if title else ""

        if self.dialog is not None:
            self.dialog.border_title = text

    def set_modal_subtitle(
        self,
        hints: Sequence[tuple[str, str]] | str | None = None,
    ) -> None:
        """Set the dialog's bottom border subtitle with keyboard shortcuts."""
        if hints is None:
            text: str | Content = ""
        elif isinstance(hints, str):
            text = f" {hints} "
        else:
            max_width = (self.dialog.size.width or 88) - 6 if self.dialog else 80
            text = format_modal_hints(hints, max_width=max_width)

        if self.dialog is not None:
            self.dialog.border_subtitle = text

    def _option_content_width(self, list_id: str, fallback: int = 72) -> int:
        """Calculate responsive row width for an OptionList inside the modal dialog."""
        try:
            option_list = self.query_one(list_id, OptionList)
            w = option_list.scrollable_content_region.width
            if w > 0:
                return max(20, w - 3)
        except (NoMatches, RuntimeError):
            pass
        if self.dialog is not None and self.dialog.region.width > 0:
            return max(20, self.dialog.region.width - 6)
        return fallback

    def check_action(
        self,
        action: str,
        parameters: tuple[object, ...],
    ) -> bool | None:
        """Prevent priority bindings from stealing keystrokes when typing in Input."""
        focused = self.focused
        if isinstance(focused, Input):
            if action in {"previous_tab", "next_tab"} and bool(focused.value):
                return False
            if action in {"focus_search", "focus_filter"}:
                return False
        return True

    def action_cancel(self) -> None:
        """Dismiss the modal cleanly with None."""
        self.dismiss(None)

    def action_next_tab(self) -> None:
        """Switch to the next tab in PillTabs if available."""
        with contextlib.suppress(NoMatches):
            tabs = self.query_one(PillTabs)
            tabs.next_tab()

    def action_previous_tab(self) -> None:
        """Switch to the previous tab in PillTabs if available."""
        with contextlib.suppress(NoMatches):
            tabs = self.query_one(PillTabs)
            tabs.previous_tab()

    def action_focus_search(self) -> None:
        """Focus the modal search input."""
        with contextlib.suppress(NoMatches):
            search = self.query_one(ModalSearchInput)
            search.focus()
            return
        with contextlib.suppress(NoMatches):
            inp = self.query_one(Input)
            inp.focus()
