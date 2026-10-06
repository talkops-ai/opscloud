"""Thread selector widget and modal screen for the OpsCloud TUI.

Provides an interactive modal screen to browse, search, resume, create, and delete conversation threads.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from pathlib import Path
from typing import ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

logger = get_logger(__name__)

DELETE_KEYS = ("ctrl+d", "delete", "backspace", "d")


class CustomOptionList(OptionList):
    """Custom OptionList that intercepts mouse click position, hover, and delete keys."""

    last_click_x: int | None = None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.last_click_x = event.x

    async def _on_key(self, event: events.Key) -> None:
        if event.key in DELETE_KEYS:
            try:
                screen = self.screen
                if isinstance(screen, ThreadSelectorScreen):
                    screen.action_delete_thread()
                    event.prevent_default()
                    event.stop()
                    return
            except Exception:
                pass
        await super()._on_key(event)


class SearchInput(Input):
    """Search input that forwards navigation keys (up/down/enter/delete) to OptionList."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key in ("down", "up", "enter", *DELETE_KEYS):
            try:
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
                    elif event.key in DELETE_KEYS:
                        screen.action_delete_thread()
                        event.prevent_default()
                        event.stop()
                        return
            except Exception:
                pass
        await super()._on_key(event)


class ThreadSelectorScreen(ModalScreen[str | None]):
    """Modal dialog for browsing, searching, resuming, and deleting threads."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "select", "Select", show=False, priority=True),
        Binding("ctrl+d", "delete_thread", "Delete", show=False, priority=True),
        Binding("delete", "delete_thread", "Delete", show=False, priority=True),
        Binding("backspace", "delete_thread", "Delete", show=False, priority=True),
        Binding("d", "delete_thread", "Delete", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ThreadSelectorScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    ThreadSelectorScreen > Vertical {
        width: 110;
        max-width: 95%;
        height: 85%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    ThreadSelectorScreen .thread-selector-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    ThreadSelectorScreen SearchInput {
        margin-bottom: 1;
        border: solid $primary-lighten-2;
    }

    ThreadSelectorScreen CustomOptionList {
        height: 1fr;
        background: $background;
        border: solid $accent;
    }

    ThreadSelectorScreen CustomOptionList > .option-list--option-highlighted {
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }

    ThreadSelectorScreen CustomOptionList > .option-list--option-hover {
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }

    ThreadSelectorScreen .thread-selector-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
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

    def compose(self):
        title = f"Select Thread (current: {self._current_thread_id or 'none'})"
        with Vertical():
            yield Static(title, classes="thread-selector-title")
            yield SearchInput(placeholder="Type to search threads...", id="thread-filter")
            yield CustomOptionList(*self._build_options(), id="thread-options")
            yield Static("↑/↓ navigate  •  Enter select  •  Ctrl+D / Delete  •  Esc cancel", classes="thread-selector-help")

    def _build_options(self) -> list[Option]:
        options = [Option("+ Start New Thread", id="__new__")]
        for idx, t in enumerate(self._threads, start=1):
            tid = t.get("thread_id", "unknown")
            msg_cnt = t.get("message_count", 0)
            init_prompt = t.get("initial_prompt") or "(No initial prompt)"

            # Apply filter query
            if self._filter_query:
                q = self._filter_query.lower()
                if q not in tid.lower() and q not in init_prompt.lower():
                    continue

            is_curr = " (active)" if tid == self._current_thread_id else ""

            msg_str = f"{msg_cnt:>3} msgs"
            tid_short = f"{tid[:8]}"
            prompt_trunc = init_prompt if len(init_prompt) <= 45 else init_prompt[:42] + "..."

            prefix = f"{msg_str} │ `{tid_short}` │ {prompt_trunc}{is_curr}"

            # Display right-padded [del] text ONLY on hovered/highlighted row
            if self._highlighted_index == len(options):
                pad_len = max(2, 70 - len(prefix))
                label = f"{prefix}{' ' * pad_len}[bold red][del][/bold red]"
            else:
                label = prefix

            options.append(Option(label, id=tid))
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
            # Check if mouse click was on the delete action on the right (x >= 40)
            if opt_list.last_click_x is not None and opt_list.last_click_x >= 40:
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
            if opt_list.highlighted is not None:
                option = opt_list.get_option_at_index(opt_list.highlighted)
                if option.id and option.id != "__new__":
                    thread_id_to_delete = option.id
                    self.run_worker(self._delete_thread_async(thread_id_to_delete))
        except Exception as exc:
            logger.debug("Failed deleting thread: %s", exc)

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
    "SearchInput",
    "ThreadSelector",
    "ThreadSelectorScreen",
]
