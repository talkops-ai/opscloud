"""Runtime compatibility patches for Textual internals.

Applies targeted enhancements and bug fixes for Textual terminal widgets:
1. Keyboard parser modifier preservation (handling ESC sequences and modifier bits).
2. Mouse click-chain word selection and text range resolution.
3. Detached widget hit detection safety.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import re
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual import __version__ as _textual_version
from textual.content import Content
from textual.geometry import Offset
from textual.selection import Selection

if TYPE_CHECKING:
    from collections.abc import Iterable

    from textual.events import Click, Event
    from textual.screen import Screen
    from textual.selection import SelectState
    from textual.widget import Widget

logger = get_logger(__name__)

_ESC_PREFIX_LEN = 2
_DOUBLE_CLICK_CHAIN = 2
_TRIPLE_CLICK_CHAIN = 3
_OPSCODE_WORD_SELECT_ACTIVE = "_opscloud_word_select_active"


# ── 1. Keyboard Parser Patch ──────────────────────────────────


def _install_keyboard_parser_patch() -> None:
    """Install terminal keyboard sequence decoding fixes."""
    try:
        from textual import events
        from textual._ansi_sequences import (  # noqa: PLC2701
            ANSI_SEQUENCES_KEYS,
            IGNORE_SEQUENCE,
        )
        from textual._xterm_parser import XTermParser  # noqa: PLC2701

        original_sequence_to_key_events = XTermParser._sequence_to_key_events
    except (ImportError, AttributeError) as exc:
        logger.warning("Textual keyboard parser patch skipped: %s", exc)
        return

    kitty_lock_codes = frozenset({"57358", "57359", "57360"})
    kitty_lock_names = {
        "57358": "caps_lock",
        "57359": "scroll_lock",
        "57360": "num_lock",
    }
    kitty_key_sequence = re.compile(r"\x1b\[(\d+)[\d;:]*u")
    kitty_subfield_key = re.compile(r"\x1b\[[\d;:]*:[\d;:]*[u~ABCDEFHPQRS]")
    kitty_csi_u = re.compile(
        r"\x1b\[(\d+)(?::\d+)*(?:;(\d+)[\d:]*)?(?:;(\d+)[\d:]*)?u"
    )
    ascii_upper_a = 65
    ascii_upper_z = 90
    real_modifier_mask = 0b111111

    def is_spurious_caps_lock(sequence: str) -> bool:
        match = kitty_csi_u.fullmatch(sequence)
        if match is None:
            return False
        code = int(match.group(1))
        if not ascii_upper_a <= code <= ascii_upper_z:
            return False
        modifier_bits = (int(match.group(2)) - 1) if match.group(2) else 0
        has_text = match.group(3) is not None
        return modifier_bits & real_modifier_mask == 0 and not has_text

    def clean_kitty_subfields(sequence: str) -> str:
        body, terminator = sequence[2:-1], sequence[-1]
        fields = body.split(";")
        fields[:2] = [field.split(":", 1)[0] for field in fields[:2]]
        return f"\x1b[{';'.join(fields)}{terminator}"

    def parse_lock_key_event(sequence: str) -> events.Key | None:
        match = kitty_key_sequence.fullmatch(sequence)
        if match is None or match.group(1) not in kitty_lock_codes:
            return None
        return events.Key(kitty_lock_names[match.group(1)], None)

    def emit_alt_keys(keys: tuple, character: str | None) -> Iterable[events.Key]:
        for key in keys:
            yield events.Key(f"alt+{key.value}", character)

    def sequence_to_key_events_patched(
        parser_self: XTermParser, sequence: str, alt: bool = False
    ) -> Iterable[events.Key]:
        if (lock_event := parse_lock_key_event(sequence)) is not None:
            yield lock_event
            return
        if is_spurious_caps_lock(sequence):
            yield events.Key("caps_lock", None)
            return
        if kitty_subfield_key.fullmatch(sequence):
            sequence = clean_kitty_subfields(sequence)

        # Fast path: \x1b<byte> on first pass
        if not alt and len(sequence) == _ESC_PREFIX_LEN and sequence[0] == "\x1b":
            inner = ANSI_SEQUENCES_KEYS.get(sequence[1])
            if inner is not IGNORE_SEQUENCE and isinstance(inner, tuple):
                yield from emit_alt_keys(inner, None)
                return

        # Preserve `alt` on reissue path for single-byte tuple mappings
        if alt:
            keys = ANSI_SEQUENCES_KEYS.get(sequence)
            if keys is not IGNORE_SEQUENCE and isinstance(keys, tuple):
                character = sequence if len(sequence) == 1 else None
                yield from emit_alt_keys(keys, character)
                return

        yield from original_sequence_to_key_events(parser_self, sequence, alt=alt)

    try:
        setattr(XTermParser, "_sequence_to_key_events", sequence_to_key_events_patched)
    except (AttributeError, TypeError) as exc:
        logger.warning("Textual keyboard parser patch assignment rejected: %s", exc)


# ── 2. Word Selection & Mouse Event Utilities ─────────────────


def _extract_rendered_text(widget: Widget) -> str | None:
    visual = widget._render()
    if isinstance(visual, (Content, Text)):
        return str(visual)
    return None


def _calculate_word_bounds(text: str, offset: Offset) -> tuple[Offset, Offset] | None:
    lines = text.splitlines()
    if not lines:
        return None

    y = min(max(offset.y, 0), len(lines) - 1)
    line = lines[y]
    if not line:
        return None

    x = min(max(offset.x, 0), len(line))
    index = min(x, len(line) - 1)
    if line[index].isspace():
        if x == len(line) and x > 0 and not line[x - 1].isspace():
            index = x - 1
        else:
            return None

    start = index
    while start > 0 and not line[start - 1].isspace():
        start -= 1

    end = index + 1
    while end < len(line) and not line[end].isspace():
        end += 1

    return Offset(start, y), Offset(end, y)


def _compute_word_selection(widget: Widget, selection: Selection) -> Selection | None:
    if selection.start is None or selection.end is None:
        return None

    text = _extract_rendered_text(widget)
    if text is None:
        return None

    start, end = selection.start, selection.end
    if end.transpose < start.transpose:
        start, end = end, start

    start_bounds = _calculate_word_bounds(text, start)
    end_bounds = _calculate_word_bounds(text, end)
    if start_bounds is None and end_bounds is None:
        return None

    return Selection(
        start_bounds[0] if start_bounds is not None else start,
        end_bounds[1] if end_bounds is not None else end,
    )


def _apply_word_selection_at_click(widget: Widget, event: Click) -> bool:
    offset = event.get_content_offset(widget)
    if offset is None:
        return False

    text = _extract_rendered_text(widget)
    if text is None:
        return False

    bounds = _calculate_word_bounds(text, offset)
    if bounds is None:
        widget.screen.clear_selection()
        return True

    widget.screen.selections = {widget: Selection(*bounds)}
    return True


def _install_word_selection_patch() -> None:
    """Install mouse click-chain selection handling."""
    try:
        from textual import events as _events
        from textual.screen import Screen as _Screen
        from textual.widget import Widget as _Widget

        original_forward_event = _Screen._forward_event
        original_watch_select_state = _Screen._watch__select_state
        original_widget_on_click = _Widget._on_click
    except (ImportError, AttributeError) as exc:
        logger.warning(
            "Textual word-selection patch skipped (textual %s): %s",
            _textual_version,
            exc,
        )
        return

    def is_word_select_initiator(screen: Screen, event: Event) -> bool:
        if not isinstance(event, _events.MouseDown) or screen.app.mouse_captured:
            return False

        last_offset = getattr(screen.app, "_click_chain_last_offset", None)
        last_time = getattr(screen.app, "_click_chain_last_time", None)
        if last_offset != event.screen_offset or last_time is None:
            return False

        if event.time - last_time > screen.app.CLICK_CHAIN_TIME_THRESHOLD:
            return False

        select_widget, select_offset = screen.get_widget_and_offset_at(event.x, event.y)
        return (
            select_widget is not None
            and select_widget.allow_select
            and screen.allow_select
            and screen.app.ALLOW_SELECT
            and select_offset is not None
        )

    def forward_event_patched(self: Screen, event: Event) -> None:
        if isinstance(event, _events.MouseDown):
            setattr(
                self,
                _OPSCODE_WORD_SELECT_ACTIVE,
                is_word_select_initiator(self, event),
            )
        try:
            original_forward_event(self, event)
        finally:
            if isinstance(event, _events.MouseUp):
                setattr(self, _OPSCODE_WORD_SELECT_ACTIVE, False)

    async def watch_select_state_patched(
        self: Screen,
        select_state: SelectState | None,
    ) -> None:
        result: Any = original_watch_select_state(self, select_state)
        if isawaitable(result):
            await cast(Any, result)
        if not getattr(self, _OPSCODE_WORD_SELECT_ACTIVE, False):
            return

        selections = dict(self.selections)
        changed = False
        for widget, selection in selections.items():
            word_selection = _compute_word_selection(widget, selection)
            if word_selection is None or word_selection == selection:
                continue
            selections[widget] = word_selection
            changed = True

        if changed:
            self.selections = selections

    async def widget_on_click_patched(self: Widget, event: Click) -> None:
        if (
            event.widget is self
            and self.allow_select
            and self.screen.allow_select
            and self.app.ALLOW_SELECT
        ):
            if event.chain == _DOUBLE_CLICK_CHAIN and _apply_word_selection_at_click(
                self, event
            ):
                await self.broker_event("click", event)
                return
            if event.chain == _TRIPLE_CLICK_CHAIN:
                self.text_select_all()
                await self.broker_event("click", event)
                return

        await original_widget_on_click(self, event)

    try:
        setattr(_Screen, "_forward_event", forward_event_patched)
        setattr(_Screen, "_watch__select_state", watch_select_state_patched)
        setattr(_Widget, "_on_click", widget_on_click_patched)
    except (AttributeError, TypeError) as exc:
        logger.warning(
            "Textual word-selection patch assignment rejected (textual %s): %s",
            _textual_version,
            exc,
        )


# ── 3. Detached Widget Hit Testing Patch ──────────────────────


def _install_detached_hit_patch() -> None:
    """Install guard against hit-testing unattached / unmounted widgets."""
    try:
        from textual.screen import Screen as _HitScreen

        original_get_widget_and_offset_at = _HitScreen.get_widget_and_offset_at
    except (ImportError, AttributeError) as exc:
        logger.warning(
            "Textual detached-hit patch skipped (textual %s): %s",
            _textual_version,
            exc,
        )
        return

    def get_widget_and_offset_at_safe(
        self: Screen,
        x: int,
        y: int,
    ) -> tuple[Widget | None, Offset | None]:
        widget, offset = original_get_widget_and_offset_at(self, x, y)
        if (
            widget is not None
            and not isinstance(widget, _HitScreen)
            and (widget.parent is None or not widget.is_attached)
        ):
            return None, None
        return widget, offset

    try:
        setattr(_HitScreen, "get_widget_and_offset_at", get_widget_and_offset_at_safe)
    except (AttributeError, TypeError) as exc:
        logger.warning(
            "Textual detached-hit patch assignment rejected (textual %s): %s",
            _textual_version,
            exc,
        )


# ── 4. Scroll Hover Recalculation Coalescing Patch ────────────

_OPSCODE_SCROLL_SUPPRESS_HOVER_UNTIL = "_opscloud_scroll_suppress_hover_until"
_OPSCODE_HOVER_CHECK_PENDING = "_opscloud_hover_check_pending"


def _install_scroll_hover_patch() -> None:
    """Suppress redundant post-refresh hover hit-testing while scrolling.

    Textual's ``Screen._compositor_refresh`` calls ``App._update_mouse_over``
    on every rendered frame. During active scrolling, widgets pass beneath a
    stationary mouse cursor on almost every frame, triggering
    ``get_hover_widgets_at`` + ``stylesheet.update_nodes`` + a secondary
    repaint per scroll tick. Deferring hover checks until the scroll gesture
    settles eliminates double-repaints and style cache thrashing during scroll.
    """
    try:
        import time
        from textual.app import App as _App
        from textual.errors import NoWidget

    except (ImportError, AttributeError) as exc:
        logger.warning(
            "Textual scroll-hover patch skipped (textual %s): %s",
            _textual_version,
            exc,
        )
        return

    def update_mouse_over_patched(self: Any, screen: Screen) -> None:
        if self.mouse_over is None or not screen.is_active:
            return
        suppress_until = getattr(self, _OPSCODE_SCROLL_SUPPRESS_HOVER_UNTIL, 0.0)
        if suppress_until and time.monotonic() < suppress_until:
            return
        if getattr(screen, _OPSCODE_HOVER_CHECK_PENDING, False):
            return
        setattr(screen, _OPSCODE_HOVER_CHECK_PENDING, True)

        async def check_mouse() -> None:
            setattr(screen, _OPSCODE_HOVER_CHECK_PENDING, False)
            suppress = getattr(self, _OPSCODE_SCROLL_SUPPRESS_HOVER_UNTIL, 0.0)
            if suppress and time.monotonic() < suppress:
                return
            try:
                hover_widgets = screen.get_hover_widgets_at(*self.mouse_position)
            except NoWidget:
                pass
            else:
                mouse_over, hover_over = hover_widgets.widgets
                if (
                    mouse_over is not self.mouse_over
                    or hover_over is not self.hover_over
                ):
                    self._set_mouse_over(mouse_over, hover_over)

        self.call_after_refresh(check_mouse)

    try:
        setattr(_App, "_update_mouse_over", update_mouse_over_patched)
    except (AttributeError, TypeError) as exc:
        logger.warning(
            "Textual scroll-hover patch assignment rejected (textual %s): %s",
            _textual_version,
            exc,
        )


# ── 5. Virtual-DOM Scrollbar Lifecycle Patch ──────────────────


def _install_scrollbar_lifecycle_patch() -> None:
    """Prevent orphaned ScrollBar message-pump tasks during widget show/hide/teardown.

    Fixes three Textual virtual-DOM lifecycle issues that can leave
    ``ScrollBar`` tasks pending on widget removal:
    1. ``Widget._on_show`` and ``Widget._on_hide`` accessed the lazy
       ``self.horizontal_scrollbar`` / ``self.vertical_scrollbar`` properties
       even when ``self._horizontal_scrollbar`` was ``None``, spawning a
       ``ScrollBar`` task just to deliver ``Show``/``Hide``.
    2. ``App._start_widget`` started ``ScrollBar._task`` even when the parent
       widget was not running or already closing/pruning.
    3. ``Widget._message_loop_exit`` could leave virtual-DOM scrollbar tasks
       pending if a scrollbar was attached during teardown.
    """
    try:
        import contextlib
        from textual.app import App as _App
        from textual.widget import Widget as _Widget

        original_start_widget = _App._start_widget
        original_message_loop_exit = _Widget._message_loop_exit
    except (ImportError, AttributeError) as exc:
        logger.warning(
            "Textual scrollbar-lifecycle patch skipped (textual %s): %s",
            _textual_version,
            exc,
        )
        return

    def start_widget_patched(self: Any, parent: Widget, widget: Widget) -> None:
        if (
            not getattr(parent, "_running", False)
            or getattr(parent, "_closing", False)
            or getattr(parent, "_closed", False)
            or getattr(parent, "_pruning", False)
        ):
            widget._attach(parent)
            widget._closing = True
            widget._closed = True
            return
        original_start_widget(self, parent, widget)

    def widget_on_show_patched(self: Widget, event: Any) -> None:
        if self._horizontal_scrollbar is not None:
            self._horizontal_scrollbar.post_message(event)
        if self._vertical_scrollbar is not None:
            self._vertical_scrollbar.post_message(event)

    def widget_on_hide_patched(self: Widget, event: Any) -> None:
        if self._horizontal_scrollbar is not None:
            self._horizontal_scrollbar.post_message(event)
        if self._vertical_scrollbar is not None:
            self._vertical_scrollbar.post_message(event)
        if self.has_focus:
            self.blur()

    async def message_loop_exit_patched(self: Widget) -> None:
        try:
            await original_message_loop_exit(self)
        finally:
            for vnode in list(self._get_virtual_dom()):
                task = getattr(vnode, "_task", None)
                if task is not None and not task.done():
                    with contextlib.suppress(Exception):
                        await vnode._close_messages()

    try:
        setattr(_App, "_start_widget", start_widget_patched)
        setattr(_Widget, "_on_show", widget_on_show_patched)
        setattr(_Widget, "_on_hide", widget_on_hide_patched)
        setattr(_Widget, "_message_loop_exit", message_loop_exit_patched)
    except (AttributeError, TypeError) as exc:
        logger.warning(
            "Textual scrollbar-lifecycle patch assignment rejected (textual %s): %s",
            _textual_version,
            exc,
        )



def apply_textual_patches() -> None:
    """Apply all Textual compatibility enhancements."""
    _install_keyboard_parser_patch()
    _install_word_selection_patch()
    _install_detached_hit_patch()
    _install_scroll_hover_patch()
    _install_scrollbar_lifecycle_patch()


# Automatically apply patches when module is imported
apply_textual_patches()

