"""Thread selector widget and modal screen for the OpsCloud TUI.

Provides an interactive modal screen to browse, search, resume, create, and delete conversation threads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from rich.cells import cell_len
from rich.markup import escape

from opscloud.config.settings import get_glyphs
from opscloud.ui.widgets.modal_base import OpsModalScreen
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

# Safe: deletion is an explicit command, never normal text editing.
DELETE_KEYS = ("ctrl+d",)


def _format_relative_time(timestamp: Any) -> str:
    """Format timestamp into human-readable relative time."""
    if not timestamp:
        return ""
    try:
        now = datetime.now(timezone.utc)
        if isinstance(timestamp, (int, float)):
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        elif isinstance(timestamp, str):
            clean_str = timestamp.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        elif isinstance(timestamp, datetime):
            dt = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        else:
            return str(timestamp)

        diff = (now - dt).total_seconds()
        if diff < 60:
            return "just now"
        elif diff < 3600:
            mins = int(diff / 60)
            return f"{mins}m ago"
        elif diff < 86400:
            hours = int(diff / 3600)
            return f"{hours}h ago"
        elif diff < 172800:
            return "yesterday"
        else:
            days = int(diff / 86400)
            return f"{days}d ago"
    except Exception:
        return ""


class CustomOptionList(OptionList):
    """Thread result list."""

    last_click_x: int | None = None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.last_click_x = event.x


class SearchInput(Input):
    """Search input that forwards result navigation, not editing keys."""

    async def _on_key(self, event: events.Key) -> None:
        screen = self.screen
        if isinstance(screen, ThreadSelectorScreen):
            if event.key == "down":
                screen.action_cursor_down()
                event.prevent_default()
                event.stop()
                return
            elif event.key == "up":
                screen.action_cursor_up()
                event.prevent_default()
                event.stop()
                return
            elif event.key == "enter":
                screen.action_select()
                event.prevent_default()
                event.stop()
                return

        # Textual handles characters, Backspace, Delete, Home, End, and arrows.
        await super()._on_key(event)


class DeleteThreadConfirmScreen(OpsModalScreen[bool]):
    """Confirmation modal before deleting a conversation thread."""

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    DEFAULT_CSS = """
    DeleteThreadConfirmScreen .confirm-message {
        height: auto;
        padding: 1 0;
        text-align: center;
    }

    DeleteThreadConfirmScreen .confirm-actions {
        height: auto;
        align: center middle;
        padding-top: 1;
    }

    DeleteThreadConfirmScreen .confirm-actions Button {
        margin: 0 1;
    }
    """

    def __init__(self, target_thread_id: str) -> None:
        super().__init__()
        self.target_thread_id = target_thread_id

    @property
    def thread_id(self) -> str:
        return self.target_thread_id

    def compose_modal(self) -> ComposeResult:
        yield Static(
            f"Delete thread {self.target_thread_id[:8]}? This cannot be undone.",
            classes="confirm-message",
        )
        with Horizontal(classes="confirm-actions"):
            yield Button("Cancel", id="cancel", variant="default")
            yield Button("Delete", id="confirm", variant="error")

    def on_mount(self) -> None:
        super().on_mount()
        self.set_modal_title("Delete Thread")
        self.set_modal_subtitle([("enter", "choose"), ("esc", "cancel")])
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class ThreadSelectorScreen(OpsModalScreen[str | None]):
    """Modal dialog for browsing, searching, resuming, and deleting threads."""

    DIALOG_CLASSES: ClassVar[str] = "-workspace"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "select", "Select", show=False, priority=True),
        Binding("ctrl+d", "delete_thread", "Delete", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ThreadSelectorScreen .thread-selector-title {
        display: none;
    }

    ThreadSelectorScreen SearchInput {
        margin-bottom: 1;
        border: solid $primary 40%;
    }

    ThreadSelectorScreen SearchInput:focus {
        border: solid $primary;
    }

    ThreadSelectorScreen CustomOptionList {
        height: 1fr;
        background: $background;
        border: solid $primary 20%;
    }

    ThreadSelectorScreen CustomOptionList > .option-list--option-highlighted {
        background: $primary 18%;
        color: $text;
        text-style: bold;
    }

    ThreadSelectorScreen CustomOptionList > .option-list--option-hover {
        background: $primary 14%;
        color: $text;
        text-style: bold;
    }

    ThreadSelectorScreen .thread-selector-help {
        display: none;
    }
    """

    def __init__(
        self,
        threads: list[dict] | None = None,
        current_thread_id: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._threads = threads or []
        self._current_thread_id = current_thread_id
        self._filter_query = ""
        self._highlighted_index: int = 0
        self._last_row_width: int | None = None

    def compose_modal(self):
        title = f"Select Thread (current: {self._current_thread_id or 'none'})"
        yield Static(title, classes="thread-selector-title")
        yield SearchInput(placeholder="Type to search threads...", id="thread-filter")
        yield CustomOptionList(*self._build_options(), id="thread-options")
        yield Static(
            "↑/↓ navigate  •  Enter select  •  Ctrl+D / Delete  •  Esc cancel",
            classes="thread-selector-help",
        )

    def on_mount(self) -> None:
        super().on_mount()
        self._update_modal_chrome()
        self.call_after_refresh(self._refresh_options, preserve_highlight=True)
        try:
            self.query_one("#thread-filter", SearchInput).focus()
        except Exception:
            pass

    def on_resize(self) -> None:
        width = self._get_row_width()
        if width != self._last_row_width:
            self._last_row_width = width
            self.call_after_refresh(self._refresh_options, preserve_highlight=True)

    def _update_modal_chrome(self) -> None:
        glyphs = get_glyphs()
        active_str = f"active: {self._current_thread_id[:8]}" if self._current_thread_id else "no active thread"
        self.set_modal_title("Conversation Threads", counter=f"{len(self._threads)} threads · {active_str}")
        self.set_modal_subtitle([
            (f"{glyphs.arrow_up}{glyphs.arrow_down}", "navigate"),
            ("enter", "resume"),
            ("ctrl+d", "delete"),
            ("esc", "cancel"),
        ])

    def _get_row_width(self) -> int:
        try:
            opt_list = self.query_one("#thread-options", CustomOptionList)
            w = opt_list.scrollable_content_region.width
            if w > 20:
                return w
        except Exception:
            pass
        return 82

    def _build_options(self) -> list[Option]:
        width = self._get_row_width()
        options = [Option("+ Start New Thread", id="__new__")]
        for idx, t in enumerate(self._threads, start=1):
            tid = t.get("thread_id", "unknown")
            msg_cnt = t.get("message_count", 0)
            init_prompt = (t.get("initial_prompt") or "(No initial prompt)").replace("\n", " ")

            # Apply filter query
            if self._filter_query:
                q = self._filter_query.lower()
                if q not in tid.lower() and q not in init_prompt.lower():
                    continue

            is_curr = " [bold $success](active)[/bold $success]" if tid == self._current_thread_id else ""
            is_highlighted = (self._highlighted_index == len(options))

            short_id = tid[:8]
            rel_time = _format_relative_time(t.get("updated_at") or t.get("created_at"))

            # Build right-aligned part: [del] on highlighted row, relative time otherwise
            if is_highlighted:
                right_markup = " [bold red][del][/bold red]"
                right_plain = " [del]"
            else:
                right_markup = f" [dim]{rel_time}[/dim]" if rel_time else ""
                right_plain = f" {rel_time}" if rel_time else ""

            left_markup = f"[dim]{msg_cnt:>3} msgs[/dim] │ `{short_id}` │ "
            left_plain = f"{msg_cnt:>3} msgs │ `{short_id}` │ "

            left_cells = cell_len(left_plain)
            right_cells = cell_len(right_plain)
            active_cells = cell_len(" (active)") if is_curr else 0

            # Available budget for the prompt
            budget = max(10, width - left_cells - right_cells - active_cells - 3)
            if cell_len(init_prompt) > budget:
                prompt_trunc = init_prompt[:max(0, budget - 3)] + "..."
            else:
                prompt_trunc = init_prompt

            safe_prompt = escape(prompt_trunc)
            core_markup = f"{left_markup}{safe_prompt}{is_curr}"
            core_plain = f"{left_plain}{prompt_trunc}{' (active)' if is_curr else ''}"

            gap = max(1, width - cell_len(core_plain) - right_cells - 2)
            row_markup = f"{core_markup}{' ' * gap}{right_markup}"

            options.append(Option(row_markup, id=tid))
        return options

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "thread-filter":
            self._filter_query = event.value
            self._refresh_options()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._highlighted_index != event.option_index:
            self._highlighted_index = event.option_index
            self._refresh_options(preserve_highlight=True)

    def _refresh_options(self, preserve_highlight: bool = False) -> None:
        try:
            opt_list = self.query_one("#thread-options", CustomOptionList)
            current_hl = opt_list.highlighted if preserve_highlight else self._highlighted_index
            opt_list.clear_options()
            for opt in self._build_options():
                opt_list.add_option(opt)
            if current_hl is not None and current_hl < opt_list.option_count:
                opt_list.highlighted = current_hl
        except Exception:
            pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        try:
            opt_list = self.query_one("#thread-options", CustomOptionList)
            width = self._get_row_width()
            # If mouse click was on the [del] action on the far right (within 10 cells of the right edge)
            if opt_list.last_click_x is not None and opt_list.last_click_x >= (width - 10):
                opt_list.last_click_x = None
                self.action_delete_thread()
                return
            opt_list.last_click_x = None
        except Exception:
            pass

        if event.option and event.option.id:
            self.dismiss(event.option.id)

    def action_select(self) -> None:
        try:
            opt_list = self.query_one("#thread-options", CustomOptionList)
            if opt_list.highlighted is not None:
                option = opt_list.get_option_at_index(opt_list.highlighted)
                if option.id:
                    self.dismiss(option.id)
        except Exception:
            pass

    def action_delete_thread(self) -> None:
        try:
            opt_list = self.query_one("#thread-options", CustomOptionList)
            if opt_list.highlighted is None:
                return

            option = opt_list.get_option_at_index(opt_list.highlighted)
            thread_id = option.id
            if not thread_id or thread_id == "__new__":
                return

            def after_confirm(confirmed: bool | None) -> None:
                if confirmed:
                    self.run_worker(self._delete_thread_async(thread_id))

            self.app.push_screen(DeleteThreadConfirmScreen(thread_id), after_confirm)
        except Exception as exc:
            logger.debug("Failed initiating thread deletion: %s", exc)

    async def _delete_thread_async(self, thread_id: str) -> None:
        try:
            from opscloud.state.session import SessionManager
            from opscloud.state.session import get_db_path
            db_path = get_db_path()
            if db_path.exists():
                sm = SessionManager(db_path)
                await sm.delete_thread(thread_id)
            self._threads = [t for t in self._threads if t.get("thread_id") != thread_id]
            self._refresh_options()
            self.notify(f"Deleted thread `{thread_id[:8]}`", severity="information")
        except Exception as exc:
            logger.warning("Error deleting thread %s: %s", thread_id, exc)

    def action_cursor_up(self) -> None:
        try:
            opt_list = self.query_one("#thread-options", CustomOptionList)
            opt_list.action_cursor_up()
        except Exception:
            pass

    def action_cursor_down(self) -> None:
        try:
            opt_list = self.query_one("#thread-options", CustomOptionList)
            opt_list.action_cursor_down()
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.dismiss(None)


class ThreadSelector(Widget):
    """Overlay widget to manage conversation threads.

    Posts ``ThreadSelector.Selected`` when the user picks a thread.
    """

    DEFAULT_CSS = """
    ThreadSelector {
        layer: overlay;
        dock: right;
        width: 50;
        height: 100%;
        background: $surface;
        border-left: tall $accent;
        padding: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
    ]

    class Selected(Message):
        """Fired when a thread is selected."""

        def __init__(self, thread_id: str) -> None:
            self.thread_id = thread_id
            super().__init__()

    class NewThread(Message):
        """Fired when the user requests a new thread."""

    def __init__(self, threads: list[dict] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._threads = threads or []

    def compose(self):
        yield Label("Threads", id="thread-title")
        yield Button("+ New Thread", id="new-thread", variant="success")

        with VerticalScroll():
            if not self._threads:
                yield Static("No threads yet", classes="muted")
            else:
                for t in self._threads:
                    tid = t.get("thread_id", "unknown")
                    label = tid[:12] + "..."
                    yield Button(label, id=f"thread-{tid}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "new-thread":
            self.post_message(self.NewThread())
            self.remove()
        elif btn_id.startswith("thread-"):
            thread_id = btn_id.removeprefix("thread-")
            self.post_message(self.Selected(thread_id))
            self.remove()

    def action_dismiss(self) -> None:
        self.remove()


__all__ = [
    "CustomOptionList",
    "DeleteThreadConfirmScreen",
    "SearchInput",
    "ThreadSelector",
    "ThreadSelectorScreen",
]
