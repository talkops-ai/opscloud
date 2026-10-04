"""Status bar widget."""

from __future__ import annotations

import inspect
import os
from contextlib import suppress
from opscloud.utils.logger import get_logger
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from rich.text import Text
from textual import events, on
from textual.containers import Horizontal
from textual.content import Content
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from opscloud.config.env_vars import HIDE_CWD, HIDE_GIT_BRANCH, is_env_truthy
from opscloud.config.settings import get_glyphs
from opscloud.ui.widgets.loading import Spinner
from opscloud.utils.session_stats import format_cost

logger = get_logger(__name__)

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult, RenderResult
    from textual.geometry import Size
    from textual.timer import Timer

PROVIDER_PREFIX_STRIPS: dict[str, tuple[str, ...]] = {}
"""Some providers (e.g. Fireworks) require fully-qualified IDs like
`accounts/fireworks/models/...` or `accounts/fireworks/routers/...` that crowd
out the rest of the status bar; strip the registered prefixes before display."""

ConnectionState = Literal["", "connecting", "reconnecting", "resuming"]
"""Connection states the status bar can display (`''` means cleared)."""

CONNECTION_STATES = frozenset(get_args(ConnectionState))
"""Runtime view of `ConnectionState` for `set_connection`'s defensive guard.

Derived from the `Literal` so the two can never drift."""


class ModelLabel(Widget):
    """A label that displays a model name, right-aligned with smart truncation.

    When the full `provider:model` text doesn't fit, the provider is dropped
    first. If the bare model name still doesn't fit, it is left-truncated
    with a leading ellipsis so the most distinctive tail stays visible.

    When a reasoning effort is set, its label is appended to the model and
    participates in the same ladder: the effort suffix is preserved (with the
    model left-truncated to make room) and is only dropped once even the
    left-truncated model plus effort cannot fit.
    """

    provider: reactive[str] = reactive("", layout=True)
    model: reactive[str] = reactive("", layout=True)
    effort: reactive[str] = reactive("", layout=True)

    def _clean_model(self) -> str:
        """Strip the provider's registered prefix so the status bar stays compact.

        Returns:
            Model name with the provider's registered prefix removed if present,
                otherwise the original name.
        """
        name = self.model
        if not name or not self.provider:
            return name
        # Match on normalized text but slice the original to preserve its casing.
        name_lower = name.lower()
        for prefix in PROVIDER_PREFIX_STRIPS.get(self.provider, ()):
            if name_lower.startswith(prefix):
                return name[len(prefix) :]
        return name

    def _with_effort(self, text: str) -> str:
        """Append the reasoning effort label when one is set.

        Args:
            text: Base model display text.

        Returns:
            Model display text with the effort suffix (a per-session override or
                the provider default) when one is present, else
                `text` unchanged.
        """
        return f"{text} {self.effort}" if self.effort else text

    def get_content_width(self, container: Size, viewport: Size) -> int:  # noqa: ARG002
        """Return the intrinsic width so `width: auto` works.

        Args:
            container: Size of the container.
            viewport: Size of the viewport.

        Returns:
            Character length of the full provider:model string.
        """
        if not self.model:
            return 0
        model = self._clean_model()
        full = f"{self.provider}:{model}" if self.provider else model
        return len(self._with_effort(full))

    def render(self) -> RenderResult:
        """Render the model label with width-aware truncation.

        Returns:
            Text content, truncated from the left when necessary.
        """
        width = self.content_size.width
        if not self.model or width <= 0:
            return ""
        model = self._clean_model()
        full = f"{self.provider}:{model}" if self.provider else model
        full_with_effort = self._with_effort(full)
        model_with_effort = self._with_effort(model)
        if len(full_with_effort) <= width:
            return Content(full_with_effort)
        if len(model_with_effort) <= width:
            return Content(model_with_effort)
        if self.effort and width > len(self.effort) + 2:
            model_width = width - len(self.effort) - 1
            return Content(f"\u2026{model[-(model_width - 1) :]} {self.effort}")
        if len(model) <= width:
            return Content(model)
        if width > 1:
            return Content("\u2026" + model[-(width - 1) :])
        return Content("\u2026")


class BranchLabel(Widget):
    """A label that displays the git branch with glyph-aware truncation.

    Unlike CSS `text-overflow: ellipsis` (which always uses the Unicode
    ellipsis character), this widget truncates manually in :meth:`render` using
    :func:`get_glyphs` so ASCII mode (`OPSCODE_UI_CHARSET_MODE=ascii`)
    gets `"..."` instead of `"…"`.
    """

    branch: reactive[str] = reactive("", layout=True)

    def get_content_width(self, container: Size, viewport: Size) -> int:  # noqa: ARG002
        """Return the intrinsic width so the widget participates in flex layout.

        Args:
            container: Size of the container.
            viewport: Size of the viewport.

        Returns:
            Character length of the full branch string (icon + space + name),
                or `0` when the branch is empty.
        """
        if not self.branch:
            return 0
        icon = get_glyphs().git_branch
        return len(icon) + 1 + len(self.branch)

    def render(self) -> RenderResult:
        """Render the branch label, truncating with the configured glyph.

        Returns:
            Branch text (icon + name) truncated from the right with
                :func:`get_glyphs`'s ellipsis when it overflows the available
                width, or an empty string when no branch is set.
        """
        width = self.content_size.width
        if not self.branch or width <= 0:
            return ""
        icon = get_glyphs().git_branch
        full = f"{icon} {self.branch}"
        if len(full) <= width:
            return full
        ellipsis = get_glyphs().ellipsis
        if width <= len(ellipsis):
            return full[:width]
        return full[: width - len(ellipsis)] + ellipsis


class StatusBar(Horizontal):
    """Status bar showing mode, auto-approve, cwd, git branch, tokens, and model."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        dock: bottom;
        background: $background;
    }

    StatusBar .status-mode {
        width: auto;
        padding: 0 1;
    }

    StatusBar .status-mode.normal {
        display: none;
    }

    StatusBar .status-mode.shell {
        background: $mode-bash;
        color: white;
        text-style: bold;
    }

    StatusBar .status-mode.command {
        background: $mode-command;
        color: white;
    }

    StatusBar .status-mode.shell-incognito {
        background: $mode-incognito;
        color: $background;
        text-style: bold;
    }

    StatusBar .status-auto-approve {
        width: auto;
        padding: 0 1;
    }

    StatusBar .status-auto-approve.smart {
        background: #06b6d4;
        color: #0f172a;
        text-style: bold;
    }

    StatusBar .status-auto-approve.auto {
        background: $success;
        color: $background;
    }

    StatusBar .status-auto-approve.manual {
        background: $warning;
        color: $background;
    }

    StatusBar .status-connection {
        width: auto;
        padding: 0 1;
        color: $warning;
        text-style: bold;
    }

    StatusBar .status-message {
        width: auto;
        padding: 0 1;
        color: $text-muted;
    }

    StatusBar .status-message.thinking {
        color: $warning;
    }

    StatusBar .status-cwd {
        width: auto;
        text-align: right;
        color: $text-muted;
        /* Own both adjacent gaps so they disappear with the cwd: the left gap
           separates it from the auto-approve pill when transient status slots
           are hidden, and the right gap separates it from the branch. */
        padding: 0 1 0 1;
    }

    StatusBar .status-branch {
        width: 1fr;
        min-width: 0;
        overflow-x: hidden;
        text-wrap: nowrap;
    }

    StatusBar .status-left-collapsible {
        width: 1fr;
        min-width: 0;
        height: 1;
        overflow-x: hidden;
    }


    StatusBar .status-tokens {
        width: auto;
        padding: 0 1;
        color: $text-muted;
    }

    StatusBar .status-rubric {
        width: auto;
        padding: 0 1;
        color: $success;
        text-style: bold;
    }

    StatusBar .status-cloud {
        width: auto;
        padding: 0 1;
        color: $text-muted;
    }

    StatusBar .status-cloud:hover {
        text-style: underline;
    }

    StatusBar ModelLabel {
        width: auto;
        padding: 0 2;
        color: $text-muted;
        text-align: right;
    }

    StatusBar BranchLabel {
        color: $text-muted;
        /* No left pad while cwd is visible: the cwd owns that gap. */
        padding: 0 1 0 0;
    }

    StatusBar .status-cwd.after-status {
        padding: 0 1 0 0;
    }

    StatusBar BranchLabel.cwd-hidden {
        /* When cwd is hidden, the branch needs the same left separator that
           cwd normally provides after the auto-approve pill. */
        padding: 0 1 0 1;
    }

    StatusBar BranchLabel.cwd-hidden.after-status {
        padding: 0 1 0 0;
    }
    """
    """Mode badges and auto-approve pills use distinct colors for at-a-glance status."""

    mode: reactive[str] = reactive("normal", init=False)
    status_message: reactive[str] = reactive("", init=False)
    connection_state: reactive[ConnectionState] = reactive("", init=False)
    queued_count: reactive[int] = reactive(0, init=False)
    approval_mode: reactive[str] = reactive(default="manual", init=False)
    cwd: reactive[str] = reactive("", init=False)
    branch: reactive[str] = reactive("", init=False)
    tokens: reactive[int] = reactive(0, init=False)
    context_tokens: reactive[int] = reactive(0, init=False)
    cost_usd: reactive[float] = reactive(0.0, init=False)
    context_limit: reactive[int | None] = reactive(None, init=False)
    rubric_label: reactive[str] = reactive("", init=False)
    cloud_profile: reactive[str] = reactive("", init=False)
    cloud_region: reactive[str] = reactive("", init=False)

    def __init__(self, cwd: str | Path | None = None, **kwargs: Any) -> None:
        """Initialize the status bar.

        Args:
            cwd: Current working directory to display
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(**kwargs)
        # Store initial cwd - will be used in compose()
        self._initial_cwd = str(cwd) if cwd else str(Path.cwd())
        self._hide_cwd = is_env_truthy(HIDE_CWD)
        self._hide_git_branch = is_env_truthy(HIDE_GIT_BRANCH)
        self._spinner = Spinner()
        self._spinner_timer: Timer | None = None
        self._busy_message = ""

    def compose(self) -> ComposeResult:  # noqa: PLR6301 — Textual widget method
        """Compose the status bar layout.

        Yields:
            Widgets for mode, auto-approve, message, cwd, branch, tokens, and
                model display.
        """
        yield Static("", classes="status-mode normal", id="mode-indicator")
        yield Static(
            "MANUAL",
            classes="status-auto-approve manual",
            id="auto-approve-indicator",
        )
        with Horizontal(classes="status-left-collapsible"):
            yield Static("", classes="status-connection", id="connection-indicator")
            yield Static("", classes="status-message", id="status-message")
            yield Static("", classes="status-cwd", id="cwd-display")
            yield BranchLabel(classes="status-branch", id="branch-display")
        yield Static("", classes="status-rubric", id="rubric-display")
        yield Static("", classes="status-cloud", id="cloud-display")
        yield Static("", classes="status-tokens", id="tokens-display")
        yield ModelLabel(id="model-display")

    _CWD_WIDTH_THRESHOLD = 70
    """Hide cwd display below this terminal width."""

    def on_resize(self, event: events.Resize) -> None:
        """Hide the cwd on very narrow terminals.

        The git branch stays visible at any width (unless disabled via
        `HIDE_GIT_BRANCH`) and ellipsizes to fit; only the cwd is dropped
        outright to reclaim space when the terminal gets narrow.
        """
        width = event.size.width
        self._set_cwd_visible(not self._hide_cwd and width >= self._CWD_WIDTH_THRESHOLD)
        self._render_cloud()

    def _set_cwd_visible(self, visible: bool) -> None:
        """Show or hide cwd and keep adjacent branch spacing in sync."""
        with suppress(NoMatches):
            self.query_one("#cwd-display", Static).display = visible
        with suppress(NoMatches):
            branch = self.query_one("#branch-display", BranchLabel)
            if visible:
                branch.remove_class("cwd-hidden")
            else:
                branch.add_class("cwd-hidden")
        self._sync_left_separator()

    def _sync_left_separator(self) -> None:
        """Use one separator between the last transient status item and cwd/branch."""
        preceded_by_status = False
        with suppress(NoMatches):
            preceded_by_status = (
                self.query_one("#connection-indicator", Static).display
                or self.query_one("#status-message", Static).display
            )
        for selector, widget_type in (
            ("#cwd-display", Static),
            ("#branch-display", BranchLabel),
        ):
            with suppress(NoMatches):
                widget = self.query_one(selector, widget_type)
                if preceded_by_status:
                    widget.add_class("after-status")
                else:
                    widget.remove_class("after-status")

    def on_unmount(self) -> None:
        """Stop the spinner timer so it can't tick on a detached widget."""
        self._stop_spinner()

    def on_mount(self) -> None:
        """Set reactive values after mount to trigger watchers safely."""
        from opscloud.config.settings import settings

        self.cwd = self._initial_cwd
        if self._hide_cwd:
            self._set_cwd_visible(False)
        if self._hide_git_branch:
            with suppress(NoMatches):
                self.query_one("#branch-display", BranchLabel).display = False
        else:
            from opscloud.utils.git import read_git_branch_from_filesystem
            try:
                self.branch = read_git_branch_from_filesystem(self.cwd) or ""
            except Exception:
                pass
        # Set initial model display
        label = self.query_one("#model-display", ModelLabel)
        label.provider = settings.model_provider or ""
        label.model = settings.model_name or ""
        with suppress(NoMatches):
            self.query_one("#rubric-display", Static).display = False
        # Reactives are `init=False`, so the connection watcher never fires on
        # mount; render once to hide the empty indicator (and its padding).
        self._render_connection()
        # Same reasoning for the message and token slots: both start empty, so
        # hide them on mount so their padding doesn't reserve a blank gap.
        self.watch_status_message(self.status_message)
        with suppress(NoMatches):
            self.query_one("#tokens-display", Static).display = False
        # Initialize active cloud profile and region display
        try:
            from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region

            self.set_cloud(
                profile=get_active_aws_profile(),
                region=get_active_aws_region() or "",
            )
        except Exception:
            with suppress(NoMatches):
                self.query_one("#cloud-display", Static).display = False
        # Initialize active model context limit
        try:
            from opscloud.config.settings import settings

            self.set_context_limit(settings.model_context_limit)
        except Exception:
            pass

    def watch_context_limit(self, _new_value: int | None) -> None:
        """Update the combined token and cost display when context limit changes."""
        self._render_tokens(self.tokens, approximate=self._approximate)

    def watch_mode(self, mode: str) -> None:
        """Update mode indicator when mode changes."""
        try:
            indicator = self.query_one("#mode-indicator", Static)
        except NoMatches:
            return
        indicator.remove_class("normal", "shell", "command", "shell-incognito")

        if mode == "shell":
            indicator.update("SHELL")
            indicator.add_class("shell")
        elif mode == "shell_incognito":
            indicator.update("SHELL")
            indicator.add_class("shell-incognito")
        elif mode == "command":
            indicator.update("CMD")
            indicator.add_class("command")
        else:
            indicator.update("")
            indicator.add_class("normal")

    def watch_approval_mode(self, new_value: str) -> None:
        """Update the three-state approval indicator (manual, auto, smart)."""
        try:
            indicator = self.query_one("#auto-approve-indicator", Static)
        except NoMatches:
            return
        indicator.remove_class("manual", "auto", "smart")
        mode = new_value if new_value in {"manual", "auto", "smart"} else "manual"
        indicator.update(mode.upper())
        indicator.add_class(mode)

    def watch_cwd(self, new_value: str) -> None:
        """Update cwd display when it changes."""
        try:
            display = self.query_one("#cwd-display", Static)
        except NoMatches:
            return
        display.update(self._format_cwd(new_value))

    def watch_branch(self, new_value: str) -> None:
        """Update branch display when it changes."""
        try:
            display = self.query_one("#branch-display", BranchLabel)
        except NoMatches:
            return
        display.branch = new_value

    def watch_status_message(self, new_value: str) -> None:
        """Update status message display."""
        if self._busy_message:
            # The busy indicator owns the status-message slot while active;
            # defer regular status updates until `set_busy("")` clears it.
            return
        try:
            msg_widget = self.query_one("#status-message", Static)
        except NoMatches:
            return

        msg_widget.remove_class("thinking")
        # Hide when empty so the widget's padding doesn't reserve a blank gap
        # in the footer (mirrors the connection indicator).
        msg_widget.display = bool(new_value)
        if new_value:
            msg_widget.update(new_value)
            if "thinking" in new_value.lower() or "executing" in new_value.lower():
                msg_widget.add_class("thinking")
        else:
            msg_widget.update("")
        self._sync_left_separator()

    def watch_connection_state(self, _new_value: ConnectionState) -> None:
        """Start or stop the spinner and re-render when connection state changes."""
        self._sync_spinner()
        self._render_connection()

    def watch_queued_count(self, _new_value: int) -> None:
        """Re-render the connection indicator when the queued count changes."""
        self._render_connection()

    def _spinner_active(self) -> bool:
        """Whether any indicator (connection or busy) needs the shared spinner.

        Returns:
            `True` when a connection state or a busy message is active.
        """
        return bool(self.connection_state) or bool(self._busy_message)

    def _sync_spinner(self) -> None:
        """Start or stop the shared spinner to match connection/busy state."""
        if self._spinner_active():
            self._start_spinner()
        else:
            self._stop_spinner()

    def _start_spinner(self) -> None:
        """Begin cycling the shared spinner frames.

        No-op when not yet running (e.g. before mount) since `set_interval`
        requires a live event loop, or when an animation is already active.
        """
        if self._spinner_timer is not None or not self._running:
            return
        # 0.1s mirrors LoadingWidget so this spinner ticks in step with the
        # in-thread "Thinking" spinner.
        self._spinner_timer = self.set_interval(0.1, self._tick_spinner)

    def _stop_spinner(self) -> None:
        """Stop the spinner animation and reset to the first frame."""
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._spinner = Spinner()

    def _tick_spinner(self) -> None:
        """Advance the spinner frame and re-render the animated indicators."""
        self._spinner.next_frame()
        self._render_connection()
        self._render_busy()

    def _render_connection(self) -> None:
        """Render the combined connection + queued-count indicator text."""
        try:
            widget = self.query_one("#connection-indicator", Static)
        except NoMatches:
            return

        parts: list[str] = []
        if self.connection_state == "reconnecting":
            parts.append(f"{self._spinner.current_frame()} Reconnecting")
        elif self.connection_state == "resuming":
            parts.append(f"{self._spinner.current_frame()} Resuming")
        elif self.connection_state == "connecting":
            parts.append(f"{self._spinner.current_frame()} Connecting")
        if self.queued_count > 0:
            label = "message" if self.queued_count == 1 else "messages"
            parts.append(f"{self.queued_count} {label} queued")
        separator = f" {get_glyphs().bullet} "
        text = separator.join(parts)
        # Hide the widget entirely when empty so its `padding: 0 1` doesn't
        # leave a 2-column gap between the auto-approve pill and the cwd.
        widget.display = bool(text)
        widget.update(text)
        self._sync_left_separator()

    def _render_busy(self) -> None:
        """Render the animated busy indicator into the status-message slot."""
        if not self._busy_message:
            return
        try:
            widget = self.query_one("#status-message", Static)
        except NoMatches:
            return
        widget.remove_class("thinking")
        widget.display = True
        frame = self._spinner.current_frame()
        widget.update(Content.assemble(frame, " ", Content(self._busy_message)))
        self._sync_left_separator()

    def set_busy(self, message: str) -> None:
        """Show or clear an animated busy indicator in the status-message slot.

        Reuses the shared status-bar spinner so heavier UI operations (e.g. a
        model switch that imports a provider package) show activity instead of
        appearing to hang.

        Args:
            message: Busy text to animate with a spinner, or empty string to
                clear it and restore the regular status message.
        """
        self._busy_message = message
        self._sync_spinner()
        if message:
            self._render_busy()
        else:
            self.watch_status_message(self.status_message)

    def set_connection(self, state: ConnectionState) -> None:
        """Set the connection indicator state.

        Args:
            state: One of `''` (clear), `'connecting'`, `'reconnecting'`, or
                `'resuming'`.

        Raises:
            ValueError: If `state` is not a recognized connection state.
        """
        if state not in CONNECTION_STATES:
            msg = f"Unknown connection state: {state!r}"
            raise ValueError(msg)
        self.connection_state = state

    def set_queued(self, count: int) -> None:
        """Set the number of messages waiting in the queue.

        Args:
            count: Count of queued messages (negative values clamp to `0`).
        """
        self.queued_count = max(count, 0)

    def _format_cwd(self, cwd_path: str = "") -> str:
        """Format the current working directory for display.

        Returns:
            Formatted path string, using ~ for home directory when possible.
        """
        path = Path(cwd_path or self.cwd or self._initial_cwd)
        try:
            # Try to use ~ for home directory
            home = Path.home()
            if path.is_relative_to(home):
                return "~/" + path.relative_to(home).as_posix()
        except (ValueError, RuntimeError):
            pass
        return str(path)

    def set_mode(self, mode: str) -> None:
        """Set the current input mode.

        Args:
            mode: One of "normal", "shell", or "command"
        """
        self.mode = mode

    @property
    def auto_approve(self) -> bool:
        """Whether autonomous approval mode is active."""
        return self.approval_mode in {"auto", "smart"}

    @auto_approve.setter
    def auto_approve(self, enabled: bool) -> None:
        self.set_approval_mode("auto" if enabled else "manual")

    def set_approval_mode(self, mode: str) -> None:
        """Set the approval mode.

        Args:
            mode: `manual`, `auto`, or `smart`.
        """
        self.approval_mode = mode if mode in {"manual", "auto", "smart"} else "manual"

    def set_auto_approve(self, *, enabled: bool) -> None:
        """Set the autonomous approval state.

        Args:
            enabled: Whether autonomous approval mode is enabled.
        """
        self.set_approval_mode("auto" if enabled else "manual")

    def set_status_message(self, message: str) -> None:
        """Set the status message.

        Args:
            message: Status message to display (empty string to clear)
        """
        self.status_message = message

    def set_status(self, status: str) -> None:
        """Backwards compatibility wrapper for app.py."""
        animating = status in {"Thinking...", "Connecting..."} or status.startswith("Running tool:")
        if status in {"Ready", ""}:
            self.set_busy("")
            self.set_status_message("")
        elif animating:
            self.set_busy(status)
        else:
            self.set_busy("")
            self.set_status_message(status)

    _approximate: bool = False
    """Append "+" to the token count to signal that the displayed value is stale.

    (The actual context is larger because the generation was interrupted before
    the model reported final usage.)
    """
    _has_token_count: bool = False
    """Whether the status bar has displayed a real token count this session."""

    _tokens_pending: bool = False
    """Whether the accurate token count for the current turn is still pending.

    A cost update can arrive mid-turn, and it re-renders the shared token/cost
    slot. Without this flag that re-render would replace the ``... tokens``
    placeholder with the *previous* turn's count -- the stale value the
    placeholder exists to hide.
    """

    def watch_tokens(self, new_value: int) -> None:
        """Update the combined token and cost display when tokens change."""
        self._render_tokens(new_value, approximate=self._approximate)

    def watch_context_tokens(self, _new_value: int) -> None:
        """Update the combined token and cost display when active context tokens change."""
        self._render_tokens(self.tokens, approximate=self._approximate)

    def watch_cost_usd(self, _new_value: float) -> None:
        """Update the combined token and cost display when cost changes."""
        self._render_tokens(self.tokens, approximate=self._approximate)

    def watch_rubric_label(self, new_value: str) -> None:
        """Update rubric display when active rubric state changes."""
        try:
            display = self.query_one("#rubric-display", Static)
        except NoMatches:
            return
        display.display = bool(new_value)
        display.update(new_value)

    def watch_cloud_profile(self, _new_value: str) -> None:
        """Update cloud profile indicator when profile changes."""
        self._render_cloud()

    def watch_cloud_region(self, _new_value: str) -> None:
        """Update cloud profile indicator when region changes."""
        self._render_cloud()

    _CLOUD_NARROW_THRESHOLD = 90

    def _render_cloud(self) -> None:
        """Render the active AWS cloud profile and region concisely with responsive truncation."""
        try:
            display = self.query_one("#cloud-display", Static)
        except NoMatches:
            return
        if not self.cloud_profile:
            display.display = False
            display.update("")
            return

        display.display = True
        t = Text()
        t.append("aws:", style="dim")

        try:
            width = self.app.size.width
        except Exception:
            width = 120
        is_narrow = width < self._CLOUD_NARROW_THRESHOLD

        prof_name = self.cloud_profile
        if is_narrow and len(prof_name) > 16:
            prof_name = prof_name[:13] + "…"

        t.append(prof_name, style="bold #7DCFFF")
        if self.cloud_region and not is_narrow:
            t.append(f" ({self.cloud_region})", style="dim")
        display.update(t)

    @on(events.Click, "#cloud-display")
    def _on_cloud_clicked(self, event: events.Click) -> None:
        """Open the cloud profile selector when clicking the cloud profile indicator."""
        event.stop()
        show_cloud = getattr(self.app, "_show_cloud_selector", None)
        if callable(show_cloud):
            res = show_cloud()
            if inspect.isawaitable(res):
                self.app.run_worker(res)

    _CONTEXT_WARNING_PERCENT = 60.0
    """Context usage at which the percentage turns from calm to caution."""

    _CONTEXT_CRITICAL_PERCENT = 80.0
    """Context usage at which the percentage turns to alert."""

    def _percent_color(self, percent: float) -> str:
        """Return the color that encodes how full the context window is."""
        if percent > self._CONTEXT_CRITICAL_PERCENT:
            return "bold red"
        if percent > self._CONTEXT_WARNING_PERCENT:
            return "bold yellow"
        return "dim"

    @staticmethod
    def _compact_tokens(count: int) -> str:
        """Compact representation for token counts (e.g. 13.9K, 1.2M)."""
        if count >= 1_000_000:
            return f"{count / 1_000_000:.1f}M"
        if count >= 1_000:
            return f"{count / 1_000:.1f}K"
        return str(count)

    def _cost_text(self) -> str:
        """Format positive cost, hiding zero and unknown estimates.

        Returns:
            Formatted cost, or an empty string when no positive estimate exists.
        """
        return format_cost(self.cost_usd) if self.cost_usd > 0 else ""

    def _render_tokens(self, count: int, *, approximate: bool = False) -> None:
        """Render context percentage, cumulative tokens, and cumulative cost.

        Format: Context: {percent}% • {compact_count} tokens • ${cost}

        Args:
            count: Total cumulative session token count.
            approximate: Append "+" suffix to indicate the count is stale
                (e.g. after an interrupted generation).
        """
        try:
            display = self.query_one("#tokens-display", Static)
        except NoMatches:
            return

        ctx_count = self.context_tokens if self.context_tokens > 0 else count
        if count <= 0 and ctx_count <= 0 and self.cost_usd <= 0 and not self._tokens_pending:
            display.display = False
            display.update("")
            return

        display.display = True
        suffix = "+" if approximate else ""
        cost = self._cost_text()
        bullet = get_glyphs().bullet

        t = Text()
        if self._tokens_pending:
            t.append("Context: ", style="dim")
            t.append("...", style="dim")
            if count > 0:
                t.append(f" {bullet} ")
                t.append(f"~{self._compact_tokens(count)} tokens...", style="dim")
        else:
            if ctx_count > 0 or count > 0:
                t.append("Context: ", style="dim")
                if self.context_limit is not None and self.context_limit > 0:
                    percent = min(100.0, max(0.0, ctx_count / self.context_limit * 100))
                    color = self._percent_color(percent)
                    t.append(f"{percent:.0f}%", style=color)
                else:
                    t.append("--", style="dim")

            if count > 0:
                count_str = f"{self._compact_tokens(count)}{suffix} tokens"
                if len(t) > 0:
                    t.append(f" {bullet} ")
                t.append(count_str, style="")

        if cost:
            if len(t) > 0:
                t.append(f" {bullet} ")
            t.append(cost)

        display.update(t)

    def set_rubric_label(self, label: str) -> None:
        """Set the rubric status label.

        Args:
            label: Label to display, or empty string to hide the badge.
        """
        self.rubric_label = label

    def set_cloud(self, *, profile: str, region: str = "") -> None:
        """Update the active cloud profile and region shown on the status bar.

        Args:
            profile: Name of active AWS profile (e.g. 'default', 'krayak').
            region: Optional AWS region (e.g. 'ap-south-1', 'us-east-1').
        """
        self.cloud_profile = profile
        self.cloud_region = region
        self._render_cloud()

    def set_context_limit(self, limit: int | None) -> None:
        """Set the active model's context limit."""
        self.context_limit = limit if isinstance(limit, int) and limit > 0 else None
        self._render_tokens(self.tokens, approximate=self._approximate)

    def set_context_tokens(self, count: int) -> None:
        """Set the active turn context window token count."""
        self.context_tokens = max(0, count)
        self._render_tokens(self.tokens, approximate=self._approximate)

    def set_tokens(
        self,
        count: int,
        *,
        approximate: bool = False,
        context_tokens: int | None = None,
    ) -> None:
        """Set the cumulative session token count and optionally active context tokens.

        Forces a display refresh even when the value is unchanged. During
        streaming, ``show_pending_tokens`` replaces the widget text without
        changing the reactive token value, so a later update with the same
        count still needs to re-render the exact count.

        Args:
            count: Total cumulative session token count.
            approximate: Append "+" to indicate the count is stale.
            context_tokens: Active turn context window token count.
        """
        self._approximate = approximate
        self._has_token_count = count > 0 or (context_tokens is not None and context_tokens > 0)
        # The accurate count has arrived, so stop suppressing it.
        self._tokens_pending = False
        if context_tokens is not None and context_tokens >= 0:
            self.context_tokens = context_tokens
        if self.tokens == count:
            # Reactive dedup would skip the watcher — call render directly.
            self._render_tokens(count, approximate=approximate)
        else:
            # Reactive assignment triggers watch_tokens, which reads
            # self._approximate for the suffix.
            self.tokens = count

    def set_cost(self, cost_usd: float) -> None:
        """Set the cumulative thread cost shown beside context tokens.

        Args:
            cost_usd: Cumulative estimated cost in US dollars.
        """
        if self.cost_usd == cost_usd:
            self._render_tokens(self.tokens, approximate=self._approximate)
        else:
            self.cost_usd = cost_usd

    def show_pending_tokens(self) -> None:
        """Show pending tokens while preserving the cumulative cost."""
        if not self._has_token_count:
            return
        # Latch the placeholder so a mid-turn cost refresh keeps it instead of
        # re-rendering the previous turn's count.
        self._tokens_pending = True
        self._render_tokens(self.tokens, approximate=self._approximate)

    def set_model(self, *, provider: str, model: str, effort: str = "") -> None:
        """Update the model display text.

        Args:
            provider: Model provider name (e.g., ``'anthropic'``).
            model: Model name (e.g., ``'claude-sonnet-4-5'``).
            effort: Reasoning effort label to display (per-session override or
                from model config). E.g. ``'low'``, ``'medium'``, ``'high'``.
        """
        try:
            display = self.query_one("#model-display", ModelLabel)
        except NoMatches:
            return

        display.provider = provider
        display.model = model
        display.effort = effort
        display.refresh(layout=True)
