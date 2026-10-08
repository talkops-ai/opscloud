"""Unit tests for ChatInput responsiveness, mode prefixes, and autocomplete."""

from unittest.mock import MagicMock
import pytest
from textual.events import Click, Key

from opscloud.ui.command_registry import get_all_entries
from opscloud.ui.widgets.autocomplete import (
    CompletionResult,
    MultiCompletionManager,
    SlashCommandController,
)
from opscloud.ui.widgets.chat_input import ChatInput, CompletionPopup, _CompletionViewAdapter


class DummyApp:
    def __init__(self):
        self._pending_approval_widget = None
        self._pending_ask_user_widget = None
        self._pending_goal_review_widget = None


@pytest.fixture
def chat_input():
    widget = ChatInput()
    widget._app = DummyApp()
    widget._completion_view = _CompletionViewAdapter(widget)
    widget._slash_controller = SlashCommandController(get_all_entries(), widget._completion_view)
    widget._completion_manager = MultiCompletionManager([widget._slash_controller])
    return widget


def test_mode_prefix_keystroke_consumption(chat_input):
    """Typing '/' at start of empty input enters command mode without inserting '/' into TextArea."""
    assert chat_input.mode == "normal"
    assert "❯" in str(chat_input._prompt_widget.render())

    consumed = chat_input.handle_mode_prefix_keystroke("/")
    assert consumed is True
    assert chat_input.mode == "command"
    assert "/" in str(chat_input._prompt_widget.render())
    assert chat_input.text == ""

    # Virtual completion text should be "/"
    vtext, vcursor = chat_input._completion_text_and_cursor()
    assert vtext == "/"
    assert vcursor == 1


def test_backspace_exits_command_mode(chat_input):
    """Pressing backspace on empty input in command mode exits back to normal mode."""
    chat_input.handle_mode_prefix_keystroke("/")
    assert chat_input.mode == "command"
    assert "/" in str(chat_input._prompt_widget.render())

    event = Key(key="backspace", character=None)
    # Simulate _handle_key_event
    import asyncio
    handled = asyncio.run(chat_input._handle_key_event(event))
    assert handled is True
    assert chat_input.mode == "normal"
    assert "❯" in str(chat_input._prompt_widget.render())
    assert chat_input.text == ""


def test_space_completes_with_trailing_space_and_argument_hint(chat_input):
    """Selecting /pool via space appends a trailing space and immediately renders argument hint."""
    chat_input.handle_mode_prefix_keystroke("/")
    chat_input._text_area.text = "poo"
    chat_input._text_area.cursor_location = (0, 3)

    vtext, vcursor = chat_input._completion_text_and_cursor()
    assert vtext == "/poo"
    assert vcursor == 4

    chat_input._completion_manager.on_text_changed(vtext, vcursor)
    assert len(chat_input._current_suggestions) > 0
    # First suggestion is /pool
    assert chat_input._current_suggestions[0][0] in ("/pool", "pool")

    # Press space on the completion
    space_event = Key(key="space", character=" ")
    import asyncio
    handled = asyncio.run(chat_input._handle_key_event(space_event))
    assert handled is True

    # The text area should now have "pool " (with trailing space)
    assert chat_input.text == "pool "
    # The cursor should be after the space
    assert chat_input._text_area.cursor_location == (0, 5)
    # The argument hint should immediately be populated
    assert chat_input._text_area.argument_hint == "[status | clear]"


def test_mouse_click_on_completion_option(chat_input):
    """Clicking an autocomplete popup option applies the completion and adds a trailing space."""
    chat_input.handle_mode_prefix_keystroke("/")
    chat_input._text_area.text = "poo"
    chat_input._text_area.cursor_location = (0, 3)

    vtext, vcursor = chat_input._completion_text_and_cursor()
    chat_input._completion_manager.on_text_changed(vtext, vcursor)

    # Click first suggestion
    option_event = CompletionPopup.OptionClicked(0)
    chat_input.on_completion_popup_option_clicked(option_event)

    assert chat_input.text == "pool "
    assert chat_input._text_area.cursor_location == (0, 5)
    assert chat_input._text_area.argument_hint == "[status | clear]"


def test_submit_value_prepends_mode_prefix(chat_input):
    """Submitting 'pool status' in command mode posts '/pool status'."""
    chat_input.mode = "command"
    chat_input._text_area.text = "pool status"

    posted_messages = []
    chat_input.post_message = lambda msg: posted_messages.append(msg)

    chat_input._submit_value()

    submitted = next((m for m in posted_messages if isinstance(m, ChatInput.Submitted)), None)
    assert submitted is not None
    assert submitted.value == "/pool status"
    assert submitted.mode == "command"
    # Mode should reset to normal
    assert chat_input.mode == "normal"
    assert chat_input.text == ""


def test_click_on_chat_input_focuses_text_area(chat_input):
    """Clicking anywhere on ChatInput focuses the internal text area."""
    focused = False

    def mock_focus(*args, **kwargs):
        nonlocal focused
        focused = True

    chat_input._text_area.focus = mock_focus
    event = MagicMock(spec=Click)
    chat_input.on_click(event)
    assert focused is True
