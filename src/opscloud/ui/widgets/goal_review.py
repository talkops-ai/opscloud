"""Goal acceptance-criteria review widget."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypedDict

from textual import on
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Markdown, Static

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult

from opscloud.utils.logger import get_logger
import uuid
from opscloud.config.settings import get_glyphs
from opscloud.ui.widgets._inline_prompt import (
    InlinePromptCompletion,
    InlinePromptOption,
    InlinePromptTextArea,
    apply_inline_prompt_border,
    newline_hint,
    stop_inline_prompt_blur,
)

logger = get_logger(__name__)

_OPTIONS: tuple[tuple[str, str], ...] = (
    ("1. Accept proposed criteria (y)", "accept"),
    ("2. Edit criteria (e)", "edit"),
    ("3. Reject with message (r)", "reject_with_message"),
    ("4. Cancel (n)", "cancel"),
)


class GoalReviewAccepted(TypedDict):
    """Widget result when the generated criteria are accepted unchanged."""

    type: Literal["accepted"]


class GoalReviewEdited(TypedDict):
    """Widget result when the user submits revised criteria."""

    type: Literal["edited"]
    criteria: str


class GoalReviewRejected(TypedDict):
    """Widget result when the user rejects criteria with feedback."""

    type: Literal["rejected"]
    message: str


class GoalReviewCancelled(TypedDict):
    """Widget result when the user cancels the proposal."""

    type: Literal["cancelled"]


GoalReviewResult = (
    GoalReviewAccepted | GoalReviewEdited | GoalReviewRejected | GoalReviewCancelled
)


class GoalReviewTextArea(InlinePromptTextArea):
    """Text input that keeps goal-review edit keystrokes inside the editor."""

    class Submitted(InlinePromptTextArea.Submitted):
        """Posted when the user presses Enter to submit goal-review text."""

    class CancelEdit(Message):
        """Posted when Escape should leave goal criteria edit mode."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._escape_pending_time: float | None = None
        self._escape_timer: Any = None

    async def _on_key(self, event: events.Key) -> None:
        import time
        now = time.monotonic()

        # Terminal-emitted Option+Return (Alt+Enter) sends Escape followed rapidly by Enter.
        if event.key == "enter" and self._escape_pending_time is not None:
            gap = now - self._escape_pending_time
            if gap <= 0.08:
                logger.debug("Option+Return detected (gap=%.3fs); inserting newline", gap)
                self._escape_pending_time = None
                if self._escape_timer is not None:
                    try:
                        self._escape_timer.stop()
                    except Exception:
                        pass
                    self._escape_timer = None
                self.action_insert_newline()
                event.prevent_default()
                event.stop()
                return
            self._escape_pending_time = None

        if event.key == "escape":
            logger.debug("Escape key received in GoalReviewTextArea; buffering 50ms for Option+Return check")
            event.prevent_default()
            event.stop()
            self._escape_pending_time = now
            if self._escape_timer is not None:
                try:
                    self._escape_timer.stop()
                except Exception:
                    pass
            self._escape_timer = self.set_timer(0.05, self._flush_pending_escape)
            return

        await super()._on_key(event)

    def _flush_pending_escape(self) -> None:
        if self._escape_pending_time is not None:
            logger.debug("Flushing standalone Escape in GoalReviewTextArea; posting CancelEdit")
            self._escape_pending_time = None
            self._escape_timer = None
            self.post_message(self.CancelEdit())



class GoalReviewMenu(Container):
    """Inline review widget for generated goal acceptance criteria."""

    can_focus = True
    can_focus_children = True

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False),
        Binding("k", "move_up", "Up", show=False),
        Binding("down", "move_down", "Down", show=False),
        Binding("j", "move_down", "Down", show=False),
        Binding("enter", "select", "Select", show=False),
        Binding("1", "accept", "Accept", show=False),
        Binding("y", "accept", "Accept", show=False),
        Binding("2", "edit", "Edit", show=False),
        Binding("e", "edit", "Edit", show=False),
        Binding("3", "reject_with_message", "Reject with message", show=False),
        Binding("r", "reject_with_message", "Reject with message", show=False),
        Binding("4", "cancel", "Cancel", show=False),
        Binding("n", "cancel", "Cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    GoalReviewMenu {
        height: auto;
        margin: 1 0;
        padding: 0 1;
        background: $surface;
        border: solid $success;
    }
    GoalReviewMenu .goal-review-title {
        text-style: bold;
        color: $success;
        margin-bottom: 0;
    }
    GoalReviewMenu .goal-review-content {
        height: auto;
        max-height: 12;
        padding: 0 1;
        overflow-y: auto;
    }
    GoalReviewMenu .goal-review-body {
        height: auto;
    }
    GoalReviewMenu .goal-review-markdown {
        margin: 0 0 1 0;
        padding: 0;
    }
    GoalReviewMenu .goal-review-options-container {
        height: auto;
        background: $surface;
        padding: 0 1;
        margin-top: 0;
    }
    GoalReviewMenu .goal-review-option {
        height: 1;
        padding: 0 1;
    }
    GoalReviewMenu .goal-review-option-selected {
        background: $primary;
        text-style: bold;
    }
    GoalReviewMenu .goal-review-edit-input {
        margin: 1 1 0 1;
        height: auto;
        min-height: 4;
        max-height: 12;
        background: transparent;
    }
    GoalReviewMenu .goal-review-help {
        color: $text-muted;
        text-style: italic;
        margin-top: 0;
        margin-bottom: 0;
    }
    """

    class Decided(Message):
        """Message sent when the user accepts, edits, or cancels."""

        def __init__(
            self,
            result: GoalReviewResult,
            widget: GoalReviewMenu | None = None,
        ) -> None:
            super().__init__()
            self.result = result
            self.widget = widget

    def __init__(
        self,
        objective: str,
        criteria: str,
        *,
        amendment: bool = False,
        id: str | None = None,
    ) -> None:
        menu_id = id if (id and id != "goal-review-menu") else f"goal-review-menu-{uuid.uuid4().hex[:8]}"
        super().__init__(
            id=menu_id,
            classes="inline-prompt goal-review-menu",
        )
        self._objective = objective
        self._criteria = criteria
        self._amendment = amendment
        self._selected = 0
        self._option_widgets: list[InlinePromptOption] = []
        self._help_widget: Static | None = None
        self._edit_input: GoalReviewTextArea = GoalReviewTextArea(classes="goal-review-edit-input")
        self._edit_input.text = self._criteria
        self._edit_input.display = False
        self._input_mode: Literal["edit", "reject"] | None = None
        self._last_input_mode_exit_time: float = 0.0
        self._completion: InlinePromptCompletion[GoalReviewResult] = (
            InlinePromptCompletion()
        )

    def set_future(self, future: asyncio.Future[GoalReviewResult]) -> None:
        """Set the future to resolve when the user decides."""
        self._completion.set_future(future)

    def compose(self) -> ComposeResult:
        """Compose the review widget."""
        glyphs = get_glyphs()
        title = "Review goal amendment" if self._amendment else "Review goal criteria"
        yield Static(
            Content.from_markup("$cursor $title", cursor=glyphs.cursor, title=title),
            classes="inline-prompt-title goal-review-title",
        )
        with (
            VerticalScroll(classes="goal-review-content"),
            Vertical(classes="goal-review-body"),
        ):
            source = f"**Proposed criteria**\n\n{self._criteria}"
            if self._amendment:
                source = (
                    f"**Proposed objective**\n\n{self._objective}\n\n"
                    f"**Proposed criteria**\n\n{self._criteria}"
                )
            yield Markdown(source, classes="goal-review-markdown")
        with Container(classes="goal-review-options-container"):
            for i, (label, _) in enumerate(_OPTIONS):
                widget = InlinePromptOption(
                    label,
                    i,
                    selected=i == self._selected,
                    selected_class="goal-review-option-selected",
                    classes="goal-review-option",
                )
                self._option_widgets.append(widget)
                yield widget
        yield self._edit_input
        self._help_widget = Static(
            "",
            classes="inline-prompt-help goal-review-help",
        )
        yield self._help_widget

    async def on_mount(self) -> None:
        """Focus the menu and render options after mount."""
        apply_inline_prompt_border(self)
        self._update_options()
        self.focus()

    def focus_active(self) -> None:
        """Focus the active control."""
        if not self.is_mounted:
            return
        if self._input_mode is not None and self._edit_input is not None:
            self._edit_input.focus()
            return
        self.focus()

    def action_move_up(self) -> None:
        """Move selection up."""
        if self._input_mode is not None:
            return
        self._selected = (self._selected - 1) % len(_OPTIONS)
        self._update_options()

    def action_move_down(self) -> None:
        """Move selection down."""
        if self._input_mode is not None:
            return
        self._selected = (self._selected + 1) % len(_OPTIONS)
        self._update_options()

    def action_select(self) -> None:
        """Select the highlighted option."""
        import time
        if self._input_mode is not None:
            return
        gap = time.monotonic() - self._last_input_mode_exit_time
        if gap < 0.150:
            logger.debug("action_select suppressed due to exit cool-off (gap=%.3fs)", gap)
            return
        action_name = _OPTIONS[self._selected][1]
        logger.debug("GoalReviewMenu select triggered: option %d -> %s", self._selected, action_name)
        getattr(self, f"action_{action_name}")()

    def action_accept(self) -> None:
        """Accept the proposed criteria unchanged."""
        import time
        if self._input_mode is not None:
            return
        gap = time.monotonic() - self._last_input_mode_exit_time
        if gap < 0.150:
            logger.debug("action_accept suppressed due to exit cool-off (gap=%.3fs)", gap)
            return
        logger.debug("GoalReviewMenu accepted proposed criteria")
        self._submit({"type": "accepted"})

    def action_edit(self) -> None:
        """Open the inline editor for revised criteria."""
        if self._completion.resolved or self._input_mode is not None:
            return
        logger.debug("GoalReviewMenu opening inline editor for criteria")
        self._input_mode = "edit"
        if self._edit_input is not None:
            self._edit_input.text = self._criteria
            self._edit_input.display = True
            if self.is_mounted:
                self._edit_input.focus()
        self._update_options()

    def action_reject_with_message(self) -> None:
        """Open the inline feedback input for regenerating criteria."""
        if self._completion.resolved or self._input_mode is not None:
            return
        logger.debug("GoalReviewMenu opening feedback editor for rejection")
        self._input_mode = "reject"
        if self._edit_input is not None:
            self._edit_input.text = ""
            self._edit_input.display = True
            if self.is_mounted:
                self._edit_input.focus()
        self._update_options()

    def action_cancel(self) -> None:
        """Cancel editing or cancel the whole proposal."""
        if self._completion.resolved:
            return
        if self._input_mode is not None:
            import time
            self._last_input_mode_exit_time = time.monotonic()
            logger.debug("GoalReviewMenu exiting input_mode %s (cool-off set)", self._input_mode)
            self._input_mode = None
            if self._edit_input is not None:
                self._edit_input.display = False
            self._update_options()
            if self.is_mounted:
                self.focus()
            return
        logger.debug("GoalReviewMenu cancelling proposal")
        self._submit({"type": "cancelled"})

    def on_goal_review_text_area_submitted(
        self,
        event: GoalReviewTextArea.Submitted,
    ) -> None:
        """Submit edited criteria when Enter is pressed in the editor."""
        if event.text_area is not self._edit_input:
            return
        event.stop()
        if self._input_mode == "edit":
            self._submit_edit()
            return
        if self._input_mode == "reject":
            self._submit_rejection()

    def on_goal_review_text_area_cancel_edit(
        self,
        event: GoalReviewTextArea.CancelEdit,
    ) -> None:
        """Return from edit mode when Escape is pressed in the editor."""
        event.stop()
        self.action_cancel()

    def on_blur(self, event: events.Blur) -> None:
        """Prevent blur from dismissing the review prompt."""
        stop_inline_prompt_blur(event)

    def _submit_edit(self) -> None:
        """Submit the current editor text as revised criteria."""
        if self._edit_input is None:
            return
        criteria = self._edit_input.submitted_value.strip()
        if not criteria:
            self._hint_empty_submission("criteria")
            return
        self._submit({"type": "edited", "criteria": criteria})

    def _submit_rejection(self) -> None:
        """Submit the current editor text as regeneration feedback."""
        if self._edit_input is None:
            return
        message = self._edit_input.submitted_value.strip()
        if not message:
            self._hint_empty_submission("feedback")
            return
        self._submit({"type": "rejected", "message": message})

    def _hint_empty_submission(self, what: str) -> None:
        """Explain why an empty editor submission did nothing."""
        if self._help_widget is None:
            return
        glyphs = get_glyphs()
        self._help_widget.update(
            f"Enter some {what}, or press Esc to go back {glyphs.bullet} "
            f"{newline_hint()}"
        )

    def _submit(self, result: GoalReviewResult) -> None:
        """Resolve the result future and post the decision message."""
        if self._completion.resolved:
            return
        self.display = False
        if self._completion.resolve(result):
            self.post_message(self.Decided(result, self))

    def _update_options(self) -> None:
        """Render option labels and help text."""
        for i, widget in enumerate(self._option_widgets):
            widget.set_state(
                cursor=i == self._selected,
                highlighted=i == self._selected and self._input_mode is None,
            )

        if self._help_widget is None:
            return
        glyphs = get_glyphs()
        if self._input_mode == "edit":
            self._help_widget.update(
                f"Enter save edits {glyphs.bullet} "
                f"{newline_hint()} {glyphs.bullet} Esc back"
            )
            return
        if self._input_mode == "reject":
            self._help_widget.update(
                f"Enter regenerate {glyphs.bullet} "
                f"{newline_hint()} {glyphs.bullet} Esc back"
            )
            return
        self._help_widget.update(
            f"{glyphs.arrow_up}/{glyphs.arrow_down} navigate {glyphs.bullet} "
            f"Enter select {glyphs.bullet} y/e/r/n quick keys {glyphs.bullet} "
            "Esc cancel"
        )


class GoalReviewScreen(ModalScreen[str]):
    """Legacy Modal for reviewing a proposed goal and rubric.

    Dismiss values:
      - ``"accept"``  — accept the proposed rubric
      - ``"reject"``  — reject and regenerate
      - ``""``        — cancelled (Escape)
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=True),
        Binding("enter", "accept", "Accept", show=True),
    ]

    DEFAULT_CSS = """
    GoalReviewScreen {
        align: center middle;
    }

    GoalReviewScreen > #goal-review-container {
        width: 80;
        max-height: 80%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    GoalReviewScreen > #goal-review-container > #goal-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    GoalReviewScreen > #goal-review-container > #goal-objective {
        color: $text-muted;
        margin-bottom: 1;
        padding: 0 1;
    }

    GoalReviewScreen > #goal-review-container > #rubric-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    GoalReviewScreen > #goal-review-container > #rubric-content {
        color: $text;
        margin-bottom: 1;
        padding: 0 1;
        max-height: 20;
        overflow-y: auto;
    }

    GoalReviewScreen > #goal-review-container > #goal-button-bar {
        height: 3;
        align: center middle;
        margin-top: 1;
    }

    GoalReviewScreen > #goal-review-container > #goal-button-bar > Button {
        margin: 0 1;
    }

    GoalReviewScreen > #goal-review-container > #goal-button-bar > #btn-accept {
        background: $success;
    }

    GoalReviewScreen > #goal-review-container > #goal-button-bar > #btn-reject {
        background: $error;
    }

    GoalReviewScreen > #goal-review-container > #goal-button-bar > #btn-cancel {
        background: $surface-darken-2;
    }
    """

    def __init__(
        self,
        objective: str,
        rubric: str,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._objective = objective
        self._rubric = rubric

    def compose(self) -> ComposeResult:
        with Vertical(id="goal-review-container"):
            yield Label("🎯 Goal Review", id="goal-title")
            yield Static(self._objective, id="goal-objective")
            yield Label("Acceptance Criteria:", id="rubric-title")
            yield Static(self._rubric, id="rubric-content")
            with Horizontal(id="goal-button-bar"):
                yield Button("✓ Accept", id="btn-accept", variant="success")
                yield Button("✗ Reject", id="btn-reject", variant="error")
                yield Button("Cancel", id="btn-cancel", variant="default")

    @on(Button.Pressed, "#btn-accept")
    def _on_accept(self, event: Button.Pressed) -> None:
        self.dismiss("accept")

    @on(Button.Pressed, "#btn-reject")
    def _on_reject(self, event: Button.Pressed) -> None:
        self.dismiss("reject")

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        self.dismiss("")

    def action_cancel(self) -> None:
        self.dismiss("")

    def action_accept(self) -> None:
        self.dismiss("accept")


__all__ = [
    "GoalReviewAccepted",
    "GoalReviewCancelled",
    "GoalReviewEdited",
    "GoalReviewMenu",
    "GoalReviewRejected",
    "GoalReviewResult",
    "GoalReviewScreen",
    "GoalReviewTextArea",
]
