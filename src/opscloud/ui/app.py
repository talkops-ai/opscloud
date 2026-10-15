"""OpsCloud Textual TUI application orchestrator.

Composes widget tree, manages theme registration, input routing via InputMode,
message queueing with bypass tiers, event routing via TextualAdapter, shell
command execution, and modal overlays.

Architecture:
    Input flow: ``on_chat_input_submitted`` → ``_submit_input(value, mode)``
    → ``_process_message(value, mode)`` which routes to:
      - ``_handle_command(value)`` for slash commands (synchronous)
      - ``_handle_user_message(value)`` for agent prompts (spawns ONE worker)
      - ``_handle_shell_command(cmd)`` for ``!`` prefixed shell commands

    Only ``_run_agent_task`` and ``_run_shell_task`` spawn workers.
    ``_cleanup_agent_task`` always drains the queue in its ``finally`` block.

Server startup is deferred: the TUI renders immediately with a "Connecting..."
status, then a background worker starts the LangGraph server subprocess.  Once
the server is healthy the ``ServerReady`` message fires and the app binds the
client to the adapter, enabling agent prompts.
"""

from __future__ import annotations

try:
    import opscloud._textual_patches  # noqa: F401
except ImportError:
    pass

import asyncio
import inspect
import math
import sys
import os
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from pathlib import Path
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, cast

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container
from textual.css.query import NoMatches
from textual.message import Message
from textual.theme import Theme
from textual.worker import Worker

from opscloud.approval_mode import ApprovalMode
from opscloud.ui.widgets.approval import (
    ApprovalDecided,
    ApprovalMenu,
    ApprovalModalScreen,
    assess_tool_risk,
)
from opscloud.ui.widgets.goal_review import GoalReviewMenu
from opscloud.ui.widgets.autocomplete import AutocompletePopup
from opscloud.ui.widgets.btw_card import BtwCardPanel, BtwExchange
from opscloud.ui.widgets.chat_input import ChatInput, InputMode
from opscloud.ui.command_registry import (
    ALWAYS_IMMEDIATE,
    IMMEDIATE_CMDS,
    IMMEDIATE_UI_CMDS,
    BypassTier,
    get_command,
)
from opscloud.mcp.mcp_info import MCPServerInfo
from opscloud.ui.widgets.infra_panel import InfraStatePanel
from opscloud.ui.widgets.messages import (
    AssistantMessage,
    ErrorMessage,
    MessageList,
    QueuedUserMessage,
    SystemMessage,
    UserMessage,
)
from opscloud.ui.widgets.ask_user import AskUserMenu
from opscloud.ui.widgets.notification_center import NotificationCenter
from opscloud.ui.widgets.status import StatusBar
from opscloud.ui.widgets.subagent_panel import SubagentPanel
from opscloud.ui.textual_adapter import TextualAdapter
from opscloud.ui.theme import (
    DARK_COLORS,
    LIGHT_COLORS,
    get_css_variable_defaults,
    get_registry,
    load_theme_preference,
    register_app_themes,
    save_theme_preference,
)
from opscloud.ui.widgets.theme_selector import ThemeSelectorScreen
from opscloud.ui.widgets.toast import show_toast
from opscloud.ui.widgets.welcome import WelcomeBanner

from opscloud.ui.permission_store import PermissionStore, load_permission_store
from opscloud.utils.git import (
    read_git_branch_from_filesystem,
    read_git_branch_via_subprocess,
)



from opscloud.utils.logger import bind_logging_to_thread, get_logger

logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────

_TYPING_IDLE_THRESHOLD_SECONDS = 2.0
"""Approval widgets wait this long after the last keystroke before showing."""

_SHELL_TIMEOUT_SECONDS = 60
"""Maximum time a shell command is allowed to run before being killed."""


def _monotonic() -> float:
    """Monotonic clock shortcut."""
    return time.monotonic()


def _format_error_detail(err: Exception) -> str:
    """Extract clean error text from Exception objects or dict representations."""
    if err.args and isinstance(err.args[0], dict):
        d = err.args[0]
        msg = d.get("message") or d.get("error")
        err_type = d.get("error") if d.get("message") else None
        if msg:
            return f"{err_type}: {msg}" if err_type and err_type != str(msg) else str(msg)

    err_str = str(err)
    if "RemoteException(" in err_str or (err_str.startswith("{") and "error" in err_str):
        import ast
        try:
            inner = err_str.split("RemoteException(")[-1].rstrip(")") if "RemoteException(" in err_str else err_str
            parsed = ast.literal_eval(inner)
            if isinstance(parsed, dict):
                msg = parsed.get("message") or parsed.get("error")
                err_type = parsed.get("error") if parsed.get("message") else None
                if msg:
                    return f"{err_type}: {msg}" if err_type and err_type != str(msg) else str(msg)
        except Exception:
            pass
    return err_str


def _split_model_spec(spec: str) -> tuple[str, str]:
    """Split ``'provider:model'`` into ``(provider, model)``."""
    if not spec:
        return "", ""
    if spec in ("auto", "dynamic"):
        return "typesafe", "jev-router"
    if ":" in spec:
        provider, _, model = spec.partition(":")
        return provider.strip(), model.strip()
    from opscloud.model.config import detect_provider
    provider = detect_provider(spec) or ""
    return provider, spec


# ── Domain Controllers & MCP Helpers ────────────────────

from opscloud.ui.controllers import (
    BtwController,
    GoalController,
    ModalController,
    PromptController,
    ServerLifecycleController,
    ShellController,
    ThreadController,
)
from opscloud.ui.controllers.server_controller import (
    _McpRefreshStatus,
    _ServerRespawnResult,
    _format_mcp_server_changes,
)


def _btw_ctrl(app: Any) -> BtwController:
    ctrl = getattr(app, "__dict__", {}).get("_btw")
    return ctrl if isinstance(ctrl, BtwController) else BtwController(app)


def _shell_ctrl(app: Any) -> ShellController:
    ctrl = getattr(app, "__dict__", {}).get("_shell")
    return ctrl if isinstance(ctrl, ShellController) else ShellController(app)


def _goals_ctrl(app: Any) -> GoalController:
    ctrl = getattr(app, "__dict__", {}).get("_goals")
    return ctrl if isinstance(ctrl, GoalController) else GoalController(app)


def _threads_ctrl(app: Any) -> ThreadController:
    ctrl = getattr(app, "__dict__", {}).get("_threads")
    return ctrl if isinstance(ctrl, ThreadController) else ThreadController(app)


def _server_ctrl(app: Any) -> ServerLifecycleController:
    ctrl = getattr(app, "__dict__", {}).get("_server_lifecycle")
    return ctrl if isinstance(ctrl, ServerLifecycleController) else ServerLifecycleController(app)


def _modals_ctrl(app: Any) -> ModalController:
    ctrl = getattr(app, "__dict__", {}).get("_modals")
    return ctrl if isinstance(ctrl, ModalController) else ModalController(app)


# ── Queued Message ───────────────────────────────────────

@dataclass
class QueuedMessage:
    """A message queued for processing when the app is busy."""

    text: str
    mode: InputMode = "normal"


# ── App ──────────────────────────────────────────────────


class OpsCloudApp(App):
    """OpsCloud interactive TUI — DevOps coding agent orchestrator."""

    CSS_PATH = ["app.tcss", "modal.tcss"]
    ENABLE_COMMAND_PALETTE = False

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "interrupt", "Interrupt", show=False, priority=True),
        Binding(
            "ctrl+c",
            "quit_or_interrupt",
            "Quit/Interrupt",
            show=False,
            priority=True,
        ),
        Binding("ctrl+d", "quit_app", "Quit", show=False),
        Binding("ctrl+l", "clear_chat", "Clear", show=False),
        Binding("shift+tab", "toggle_auto_approve", "Auto-approve", show=False),
        Binding("ctrl+\\", "toggle_debug_console", "Debug", show=False),
        Binding("ctrl+x", "open_editor", "External Editor", show=False),
        Binding("ctrl+n", "open_notifications", "Notifications", show=False),
        Binding("ctrl+o", "toggle_tool_output", "Toggle Output", show=False, priority=True),
    ]

    # ── App-level Lifecycle Messages ─────────────────────

    class ServerReady(Message):
        """Posted by ``_start_server_background`` when the server is healthy."""

        def __init__(
            self,
            client: Any,
            server_proc: Any,
            mcp_server_info: list | None = None,
        ) -> None:
            super().__init__()
            self.client = client
            self.server_proc = server_proc
            self.mcp_server_info = mcp_server_info

    class ServerStartFailed(Message):
        """Posted by ``_start_server_background`` on startup failure."""

        def __init__(self, error: Exception) -> None:
            super().__init__()
            self.error = error

    # ── Constructor ──────────────────────────────────────

    def __init__(
        self,
        *,
        assistant_id: str = "opscloud",
        client: Any = None,
        server: Any = None,
        server_proc: Any = None,
        server_url: str | None = None,
        model: str | None = None,
        auto_approve: bool = False,
        approval_mode: Any = None,
        resume_thread: str | None = None,
        thread_id: str | None = None,
        goal: str | None = None,
        initial_prompt: str | None = None,
        initial_skill: str | None = None,
        startup_cmd: str | None = None,
        cwd: str | Path | None = None,
        check_workspace_trust: bool | None = None,
        defer_server_start: bool = False,
        server_kwargs: dict[str, Any] | None = None,
        mcp_preload_kwargs: dict[str, Any] | None = None,
        mcp_server_info: list | None = None,
        **extra_kwargs: Any,
    ) -> None:
        super().__init__()
        self._assistant_id = assistant_id
        if server_url and client is None:
            from opscloud.ui.remote_client import RemoteAgent
            client = RemoteAgent(url=server_url, graph_name="agent")
        self._client = client
        self._server = server
        self._server_proc = server_proc if server_proc is not None else server
        self._server_url = server_url

        if thread_id and not resume_thread:
            resume_thread = thread_id

        if not model and not defer_server_start and not client:
            try:
                from opscloud.model.factory import _get_default_model_spec
                model = _get_default_model_spec()
            except Exception:
                model = ""
                defer_server_start = True
        self._model = model or ""
        self._manual_model: str | None = self._model or None
        self._smart_routed_active: bool = False
        self._server_startup_deferred = defer_server_start and (client is None)
        from opscloud.config.settings import get_settings
        self._settings = get_settings()
        self._reasoning_effort: str | None = getattr(self._settings, "reasoning_effort", None)

        # Sync model metadata to settings early so /config can display it
        # before the agent is created (apply_to_settings only runs on first turn)
        if model and not self._settings.model_name:
            if ":" in model:
                provider_part, model_part = model.split(":", 1)
                self._settings.model_provider = provider_part
                self._settings.model_name = model_part
            else:
                self._settings.model_name = model

        from opscloud.approval_mode import ApprovalMode, coerce_approval_mode
        if approval_mode is not None:
            try:
                self._approval_mode = coerce_approval_mode(
                    approval_mode,
                    allow_yolo=False,
                )
            except Exception:
                self._approval_mode = ApprovalMode.MANUAL
        elif auto_approve:
            self._approval_mode = ApprovalMode.AUTO
        else:
            self._approval_mode = ApprovalMode.MANUAL

        self._auto_approve = False
        self._resume_thread = resume_thread
        self._goal = goal
        self._initial_prompt = initial_prompt
        self._initial_skill = initial_skill
        self._startup_cmd = startup_cmd
        self._agent_thread_id: str | None = None
        self._previous_thread_id: str | None = None
        self._message_timestamps_visible: bool = False
        self._auto_mode_notice_pending: bool = False
        self._active_goal: str | None = goal
        self._goal_status: str | None = "active" if goal else None
        self._active_rubric: str | None = None
        self._goal_status_note: str | None = None
        self._pending_goal_completion_note: str | None = None
        self._pending_goal_objective: str | None = None
        self._pending_goal_rubric: str | None = None
        self._pending_goal_kind: str | None = None
        self._pending_goal_request_id: str | None = None
        from opscloud.config.cli_settings import resolve_path_no_slash

        raw_cwd = cwd or (server_kwargs.get("cwd") if isinstance(server_kwargs, dict) else None) or str(Path.cwd())
        self._cwd: str = resolve_path_no_slash(raw_cwd) or str(Path.cwd())
        if check_workspace_trust is None:
            env_trust = os.environ.get("OPSCLOUD_CHECK_WORKSPACE_TRUST", "").strip().lower()
            if env_trust in {"1", "true", "yes", "on"}:
                self._check_workspace_trust = True
            elif env_trust in {"0", "false", "no", "off"}:
                self._check_workspace_trust = False
            else:
                self._check_workspace_trust = not bool(os.environ.get("PYTEST_CURRENT_TEST"))
        else:
            self._check_workspace_trust = bool(check_workspace_trust)
        self._workspace_trusted: bool = not self._check_workspace_trust
        self._status_bar: StatusBar | None = None
        self._git_branch_refresh_task: asyncio.Task[None] | None = None
        self._session_cost_usd: float = 0.0
        self._context_tokens: int = 0
        self._cumulative_session_tokens: int = 0

        # ── State Flags ──────────────────────────────────
        self._agent_running = False
        self._shell_running = False
        self._connecting = server_kwargs is not None and not self._server_startup_deferred and client is None
        self._startup_sequence_running = False
        self._server_startup_error: Exception | None = None
        self._exit = False
        
        self._debug_console_cleared_upto = -1
        self._debug_console_click_to_copy = False

        # ── Goal State Synchronization & Domain Controllers ──
        self._goal_state_lock = asyncio.Lock()
        self._goal_state_mutating = False
        self._agent_reconciling = False
        from opscloud.ui.controllers import (
            BtwController,
            GoalController,
            ModalController,
            PromptController,
            ServerLifecycleController,
            ShellController,
            ThreadController,
        )

        self._prompts = PromptController(self)
        self._btw = BtwController(self)
        self._shell = ShellController(self)
        self._goals = GoalController(self)
        self._threads = ThreadController(self)
        self._server_lifecycle = ServerLifecycleController(self)
        self._modals = ModalController(self)

        # ── Worker Handles ───────────────────────────────
        self._agent_worker: Worker[None] | None = None
        self._agent_turn_started: bool = False
        self._active_user_message: Any | None = None
        self._active_turn_visible_output_started: bool = False
        self._turn_metrics_start: dict[str, Any] | None = None
        """Counters captured at turn start; consumed by ``_mount_turn_summary``."""
        self._clear_input_pending: bool = False
        self._quit_pending: bool = False
        self._shell_worker: Any | None = None
        self._shell_process: asyncio.subprocess.Process | None = None

        # ── Ephemeral /btw Side-Question State ───────────
        self._btw_items: list[BtwExchange] = []
        self._btw_index: int = -1
        self._btw_worker: Worker[None] | None = None
        self._btw_cancel_event: asyncio.Event = asyncio.Event()

        # ── Queue ────────────────────────────────────────
        self._pending_messages: deque[QueuedMessage] = deque()
        self._queued_widgets: deque[QueuedUserMessage] = deque()
        self._pending_shell_messages: list[Any] = []
        self._processing_pending = False

        # ── Adapter & Widgets ────────────────────────────
        self._adapter: TextualAdapter | None = None
        self._chat_input: ChatInput | None = None
        self._input_dock: Any | None = None
        # Autocomplete is now managed inline by ChatInput
        self._loading_widget: Any | None = None

        # ── Typing Detection ─────────────────────────────
        self._last_typed_at: float | None = None

        # ── Reload & Restart Concurrency Control ──────────
        self._environment_mutation_lock = asyncio.Lock()
        self._reloading: bool = False
        self._reconnecting: bool = False
        self._reload_task: asyncio.Task[None] | None = None
        self._restart_requested_during_reload: bool = False
        self._restart_respawn_task: asyncio.Task[bool] | None = None
        self._server_restart_tasks: set[asyncio.Task[Any]] = set()
        self._discovered_skills: list[dict[str, Any]] = []
        self._last_reload_snapshot: dict[str, Any] | None = None

        # Deferred server startup
        self._server_kwargs: dict[str, Any] | None = server_kwargs
        if self._server_kwargs is None and getattr(self, "_server_proc", None) is not None:
            self._server_kwargs = {
                "assistant_id": self._assistant_id,
                "model_name": self._model,
            }
        self._mcp_preload_kwargs = mcp_preload_kwargs if mcp_preload_kwargs is not None else {}
        self._server_proc = getattr(self, "_server_proc", None)
        self._mcp_session_manager: Any | None = None
        self._mcp_server_info = mcp_server_info or []

        # Register themes
        self._init_themes()

        # Session-scoped permission grants (Phase 3)
        # Full permission store with allow/ask/deny rules, persisted to config.toml
        self._permission_store: PermissionStore = load_permission_store()

        # ── Command Router ────────────────────────────────
        from opscloud.commands import CommandRouter
        self._command_router = CommandRouter()
        self._command_router.auto_discover()

    @property
    def ui_phase(self) -> Any:
        """Return the current deterministic `UIPhase`."""
        from opscloud.ui.state.ui_phase import derive_ui_phase

        return derive_ui_phase(self)

    @property
    def ui_state(self) -> Any:
        """Return the current immutable `UIState` snapshot."""
        from opscloud.ui.state.ui_phase import derive_ui_state

        return derive_ui_state(self)

    @property
    def _pending_approval_widget(self) -> Any | None:
        return self._prompts.pending_approval_widget

    @_pending_approval_widget.setter
    def _pending_approval_widget(self, value: Any | None) -> None:
        self._prompts.pending_approval_widget = value

    @property
    def _approval_placeholder(self) -> Any | None:
        return self._prompts.approval_placeholder

    @_approval_placeholder.setter
    def _approval_placeholder(self, value: Any | None) -> None:
        self._prompts.approval_placeholder = value

    @property
    def _pending_ask_user_widget(self) -> AskUserMenu | None:
        return self._prompts.pending_ask_user_widget

    @_pending_ask_user_widget.setter
    def _pending_ask_user_widget(self, value: AskUserMenu | None) -> None:
        self._prompts.pending_ask_user_widget = value

    @property
    def _pending_goal_review_widget(self) -> Any | None:
        return self._prompts.pending_goal_review_widget

    @_pending_goal_review_widget.setter
    def _pending_goal_review_widget(self, value: Any | None) -> None:
        self._prompts.pending_goal_review_widget = value

    @property
    def _pending_goal_review_future(self) -> Any | None:
        return self._prompts.pending_goal_review_future

    @_pending_goal_review_future.setter
    def _pending_goal_review_future(self, value: Any | None) -> None:
        self._prompts.pending_goal_review_future = value

    @property
    def settings(self) -> Any:
        """Return the active runtime settings instance."""
        return getattr(self, "_settings", None)

    def _init_themes(self) -> None:
        """Register OpsCloud brand and custom themes and apply initial preference."""
        try:
            register_app_themes(self)
            initial_theme = load_theme_preference()
            self.theme = initial_theme if initial_theme in get_registry() else "opscloud-dark"
        except Exception:
            logger.exception("Failed to register themes")
            self.theme = "opscloud-dark"

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Return custom CSS variable defaults for the current theme.

        Most styling uses Textual's built-in variables (``$primary``,
        ``$text-muted``, ``$error-muted``, etc.).  This override injects the
        app-specific variables (``$mode-bash``, ``$mode-command``,
        ``$mode-incognito``, ``$skill``, ``$skill-hover``, ``$tool``,
        ``$tool-hover``, and the DevOps-specific tokens) that have no Textual
        equivalent.

        Returns:
            Dict of CSS variable names to hex color values.
        """
        from opscloud.ui.theme import get_css_variable_defaults, get_theme_colors

        colors = get_theme_colors(self)
        return get_css_variable_defaults(colors=colors)


    # ── Layout ───────────────────────────────────────────

    def compose(self) -> ComposeResult:
        from opscloud.ui.widgets.input_dock import InputDock

        yield MessageList(id="messages")
        self._input_dock = InputDock(id="bottom-container")
        yield self._input_dock
        yield StatusBar(cwd=self._cwd, id="status-bar")

    # ── Lifecycle ────────────────────────────────────────

    async def on_mount(self) -> None:
        """Initialize TUI and optionally start server in the background."""
        logger.debug("TUI: on_mount start")

        messages_widget = self.query_one("#messages", MessageList)
        status_bar = self.query_one("#status-bar", StatusBar)
        self._status_bar = status_bar
        self._schedule_git_branch_refresh()
        eff = getattr(self, "_reasoning_effort", None) or (getattr(getattr(self, "settings", None), "reasoning_effort", None))
        if not eff and self._model:
            from opscloud.model.reasoning import default_effort_for_model
            eff = default_effort_for_model(self._model)
        _provider, _model = _split_model_spec(self._model or "")
        status_bar.set_model(provider=_provider, model=_model, effort=eff or "")
        from opscloud.model.config import resolve_model_context_limit

        _ctx_limit = resolve_model_context_limit(self._model)
        if _ctx_limit:
            status_bar.set_context_limit(_ctx_limit)
            if hasattr(self, "_settings") and self._settings:
                self._settings.model_context_limit = _ctx_limit
        mode_val = self._approval_mode.value if hasattr(self._approval_mode, "value") else str(self._approval_mode)
        status_bar.set_approval_mode(mode_val)
        self._reset_thread_usage(0.0, 0)

        # Create adapter early with client=None (will be bound on ServerReady)
        self._adapter = TextualAdapter(
            client=self._client,
            assistant_id=self._assistant_id,
            messages_widget=messages_widget,
            status_bar=status_bar,
            auto_approve=self._auto_approve,
            set_spinner=self._set_spinner,
            on_subagent_event=self._on_subagent_event,
            app=self,
            request_approval=self._request_approval,
            request_ask_user=self._request_ask_user,
            on_user_visible_output_started=self._on_user_visible_output_started,
        )

        self._chat_input = self.query_one("#input-area", ChatInput)
        self._chat_input.focus()

        messages_widget.mount(
            WelcomeBanner(
                model_spec=self._model or "",
                effort=eff or "",
                mcp_servers=self._mcp_server_info or None,
                mcp_disabled=bool(
                    (getattr(self, "_mcp_preload_kwargs", None) or {}).get("no_mcp")
                ),
            )
        )
        self._sync_status_cloud()

        # Always refocus input after mounting widgets that may steal focus
        self._chat_input.focus()

        if self._check_workspace_trust:
            await self._evaluate_startup_trust_and_settings()
        else:
            self._workspace_trusted = True
            await self._continue_post_trust_startup()

    async def _evaluate_startup_trust_and_settings(self) -> None:
        await _server_ctrl(self).evaluate_startup_trust_and_settings()

    async def _prompt_workspace_trust_if_needed(self) -> None:
        await _server_ctrl(self).prompt_workspace_trust_if_needed()

    async def _continue_post_trust_startup(self) -> None:
        await _server_ctrl(self).continue_post_trust_startup()

    def _sync_banner_model(self, model_spec: str, effort: str = "") -> None:
        """Mirror the active model / effort onto the welcome banner, if present."""
        try:
            self.query_one(WelcomeBanner).update_model(model_spec, effort)
        except Exception:
            pass

    def _sync_banner_mcp(self) -> None:
        """Mirror probed MCP server metadata onto the welcome banner, if present."""
        try:
            self.query_one(WelcomeBanner).update_mcp(self._mcp_server_info or [])
        except Exception:
            pass

    async def _set_spinner(self, status: str | None) -> None:
        """Show, update, or hide the loading spinner in the message list."""
        from opscloud.ui.widgets.loading import LoadingWidget

        if status is None:
            if self._loading_widget is not None:
                try:
                    await self._loading_widget.remove()
                except Exception:
                    pass
                self._loading_widget = None
            return

        try:
            messages = self.query_one("#messages", MessageList)
        except Exception:
            return

        if self._loading_widget is None or not getattr(self._loading_widget, "is_attached", False):
            turn_start = (self._turn_metrics_start or {}).get("started")
            self._loading_widget = LoadingWidget(status, started_at=turn_start)
            await messages.mount(self._loading_widget)
            messages._scroll_to_end()
        else:
            if hasattr(self._loading_widget, "resume"):
                self._loading_widget.resume()
            if hasattr(self._loading_widget, "set_status"):
                self._loading_widget.set_status(status)
            messages._scroll_to_end()

    def _pause_loading_spinner_for_approval(self) -> None:
        """Pause the global spinner timer and subagent panel timer while approval is visible."""
        if self._loading_widget is not None and hasattr(self._loading_widget, "pause"):
            self._loading_widget.pause()
        subagent_panel = self._get_subagent_panel()
        if subagent_panel is not None and hasattr(subagent_panel, "pause"):
            subagent_panel.pause()

    def _resume_loading_spinner_after_approval(
        self,
        _future: asyncio.Future[Any] | None = None,
    ) -> None:
        """Resume the global spinner timer and subagent panel timer after approval decision."""
        if self._loading_widget is not None and hasattr(self._loading_widget, "resume"):
            self._loading_widget.resume()
        subagent_panel = self._get_subagent_panel()
        if subagent_panel is not None and hasattr(subagent_panel, "resume"):
            subagent_panel.resume()

    # ── Widget Mount Helper ──────────────────────────────

    async def _mount_message(self, widget: Any) -> None:
        """Mount a message widget in the message list."""
        try:
            messages = self.query_one("#messages", MessageList)
            if (
                self._loading_widget is not None
                and getattr(self._loading_widget, "is_attached", False)
                and self._loading_widget.parent is messages
            ):
                await messages.mount(widget, before=self._loading_widget)
            else:
                await messages.mount(widget)
            messages._scroll_to_end()
        except Exception:
            logger.debug("Could not mount message (app closing?)", exc_info=True)

    # ── Deferred Server Startup (ServerLifecycleController) ──

    async def maybe_start_deferred_server(self) -> bool:
        return await _server_ctrl(self).maybe_start_deferred_server()

    async def _start_server_background(self) -> None:
        await _server_ctrl(self).start_server_background()

    async def _preload_mcp_metadata_background(self) -> None:
        await _server_ctrl(self).preload_mcp_metadata_background()

    @on(ServerReady)
    async def _on_server_ready(self, event: ServerReady) -> None:
        await _server_ctrl(self).on_server_ready(event)

    @on(ServerStartFailed)
    async def _on_server_start_failed(self, event: ServerStartFailed) -> None:
        await _server_ctrl(self).on_server_start_failed(event)

    async def _finalize_connection(self, client: Any) -> None:
        await _server_ctrl(self).finalize_connection(client)

    async def _send_goal(self) -> None:
        await _server_ctrl(self).send_goal()


    # ── Autocomplete Event Handlers ──────────────────────

    @on(ChatInput.SlashCommandStarted)
    def _on_slash_started(self, event: ChatInput.SlashCommandStarted) -> None:
        """Autocomplete is now managed inline by ChatInput — no app-level action needed."""

    @on(ChatInput.SlashCommandEnded)
    def _on_slash_ended(self, event: ChatInput.SlashCommandEnded) -> None:
        """Autocomplete is now managed inline by ChatInput — no app-level action needed."""

    @on(ChatInput.ModeChanged)
    def _on_chat_input_mode_changed(self, event: ChatInput.ModeChanged) -> None:
        """Update status bar when input mode changes."""
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.set_mode(event.mode)
        except Exception:
            pass

    @on(AutocompletePopup.CommandSelected)
    def _on_command_selected(self, event: AutocompletePopup.CommandSelected) -> None:
        """Insert selected command into chat input and submit if zero-arg or submitted via Enter."""
        if not self._chat_input:
            return

        # Hide the inline popup
        self._chat_input._autocomplete.hide()

        cmd = get_command(event.command_name)
        has_args = bool(cmd and cmd.argument_hint)

        if event.by_enter or not has_args:
            # Execute command immediately (e.g. /theme opens modal popup instantly on single Enter)
            self._chat_input.text = event.command_name
            self._chat_input._submit_value()
        else:
            # Insert command prefix with space for argument input (e.g. /effort [level])
            self._chat_input.text = f"{event.command_name} "
            self._chat_input.focus_input()

    def set_chat_input(self, text: str) -> None:
        """Insert text into the chat input area and focus it."""
        if self._chat_input is not None:
            self._chat_input.text = text
            self._chat_input.focus_input()

    @on(ChatInput.Typing)
    def _on_typing(self, event: ChatInput.Typing) -> None:
        """Record the most recent keystroke time for typing-aware approval deferral."""
        self._last_typed_at = _monotonic()

    def _is_user_typing(self) -> bool:
        """Return whether the user typed recently (within idle threshold)."""
        if self._last_typed_at is None:
            return False
        return (_monotonic() - self._last_typed_at) < _TYPING_IDLE_THRESHOLD_SECONDS

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """Copy selection to clipboard after click-chain selection updates."""
        from opscloud.ui.clipboard import copy_selection_to_clipboard
        self.call_after_refresh(copy_selection_to_clipboard, self)

    def on_click(self, event: events.Click) -> None:
        """Handle clicks anywhere in the terminal to restore focus to chat input."""
        if not self._chat_input:
            return
        from textual.screen import ModalScreen
        if isinstance(self.screen, ModalScreen):
            return
        # Don't steal focus from active approval or interactive prompt widgets.
        pending_approval = getattr(self, "_pending_approval_widget", None)
        if pending_approval is not None and getattr(pending_approval, "is_mounted", False) and getattr(pending_approval, "display", True):
            return
        pending_ask_user = getattr(self, "_pending_ask_user_widget", None)
        if pending_ask_user is not None and getattr(pending_ask_user, "is_mounted", False) and getattr(pending_ask_user, "display", True):
            return
        pending_goal = getattr(self, "_pending_goal_review_widget", None)
        if pending_goal is not None and getattr(pending_goal, "is_mounted", False) and getattr(pending_goal, "display", True):
            return
        self.call_after_refresh(self._chat_input.focus_input)

    # ── Input Submission Pipeline ────────────────────────

    @on(ChatInput.Submitted)
    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        """Handle submitted input from ChatInput widget.

        This is the SINGLE entry point for user input.  It feeds into
        ``_submit_input`` which handles queueing, bypass tiers, and
        routing — never spawns a worker directly.
        """
        value = event.value
        mode: InputMode = event.mode  # type: ignore[assignment]
        await self._submit_input(value, mode)

    def _can_bypass_queue(self, value: str) -> bool:
        """Check if a command can bypass the busy-state queue.

        Args:
            value: Lowered, stripped command text.

        Returns:
            ``True`` if the command should bypass the queue.
        """
        cmd = value.split(maxsplit=1)[0] if value else ""

        if cmd in IMMEDIATE_CMDS:
            return True

        if cmd in IMMEDIATE_UI_CMDS:
            # Only bare form (no args) bypasses — e.g. /model opens selector,
            # /model <name> does a direct switch that shouldn't race with agent.
            return value == cmd

        return False

    async def _submit_input(
        self,
        value: str,
        mode: InputMode,
        *,
        force_bypass: bool = False,
    ) -> None:
        """Submit input, fast-pathing always-immediate commands.

        For commands in ``ALWAYS_IMMEDIATE`` (or whenever ``force_bypass``
        is set), the value is processed directly.  Otherwise the standard
        queue and per-tier bypass policy applies.

        Args:
            value: Raw text submitted by the user.
            mode: Input routing mode.
            force_bypass: Skip queueing and process immediately.
        """
        # Normalize leading "/ " for command mode (e.g. "/ goal" -> "/goal")
        if mode == "command":
            trimmed = value.strip()
            if trimmed.startswith("/") and len(trimmed) > 1 and trimmed[1].isspace():
                value = "/" + trimmed[1:].lstrip()

        # Always-immediate commands (e.g. /quit, /force-clear)
        if force_bypass or (
            mode == "command" and value.lower().strip().split(maxsplit=1)[0] in ALWAYS_IMMEDIATE
        ):
            await self._process_message(value, mode)
            return

        # If the app is busy, enqueue instead of processing
        if (
            self._agent_running
            or self._shell_running
            or self._connecting
            or self._startup_sequence_running
            or self._server_startup_error is not None
        ):
            # Check if this command can bypass the queue
            if mode == "command" and self._can_bypass_queue(value.lower().strip()):
                await self._process_message(value, mode)
                return

            # Enqueue
            self._pending_messages.append(QueuedMessage(text=value, mode=mode))
            queued_widget = QueuedUserMessage(value)
            self._queued_widgets.append(queued_widget)
            await self._mount_message(queued_widget)
            self._sync_status_queued()

            if self._connecting:
                self.notify(
                    "Server is connecting. Your message is queued.",
                    severity="information",
                    timeout=3,
                )
            return

        await self._process_message(value, mode)

    async def _process_message(self, value: str, mode: InputMode) -> None:
        """Route a message to the appropriate handler based on mode.

        Args:
            value: The message text to process.
            mode: The input mode that determines message routing.
        """
        if mode == "shell_incognito":
            cmd = value.removeprefix("!!").removeprefix("!")
            await self._handle_shell_command(cmd, incognito=True)
        elif mode == "shell":
            cmd = value.removeprefix("!")
            await self._handle_shell_command(cmd)
        elif mode == "command":
            await self._handle_command(value)
        elif mode == "normal":
            await self._handle_user_message(value)
        else:
            logger.error(
                "Unrecognized input mode %r; refusing to forward to agent",
                mode,
            )
            await self._mount_message(
                ErrorMessage(
                    f"Internal error: unknown input mode {mode!r}. "
                    "Message was not sent."
                )
            )

    def _sync_status_queued(self) -> None:
        """Update status bar with queue depth."""
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            count = len(self._pending_messages)
            if count > 0:
                status_bar.set_status(f"{count} queued")
            elif not (self._agent_running or self._shell_running):
                status_bar.set_status("Ready")
        except Exception:
            pass

    # ── Ephemeral `/btw` Side-Question Manager (BtwController) ──

    def _refresh_btw_card(self) -> None:
        _btw_ctrl(self).refresh_btw_card()

    def is_btw_active(self) -> bool:
        return _btw_ctrl(self).is_btw_active()

    def is_btw_open(self) -> bool:
        return _btw_ctrl(self).is_btw_open()

    def show_btw_history(self) -> bool:
        return _btw_ctrl(self).show_btw_history()

    def dismiss_btw_response(self) -> None:
        _btw_ctrl(self).dismiss_btw_response()

    def clear_active_btw(self) -> None:
        _btw_ctrl(self).clear_active_btw()

    def toggle_btw_expanded(self) -> None:
        _btw_ctrl(self).toggle_btw_expanded()

    def navigate_btw(self, delta: int) -> None:
        _btw_ctrl(self).navigate_btw(delta)

    def get_btw_copy_status(self) -> tuple[str | None, str | None]:
        return _btw_ctrl(self).get_btw_copy_status()

    async def _send_ephemeral_message(self, question: str) -> None:
        await _btw_ctrl(self).send_ephemeral_message(question)

    async def _run_btw_task(self, item_index: int, question: str) -> None:
        await _btw_ctrl(self).run_btw_task(item_index, question)


    # ── Agent Turn Lifecycle ─────────────────────────────

    async def _handle_user_message(self, message: str) -> None:
        """Handle a user message to send to the agent.

        Mounts the user message widget and dispatches to the agent
        via a single worker.

        Args:
            message: The user's message.
        """
        user_msg = UserMessage(message)
        self._active_user_message = user_msg
        self._active_turn_visible_output_started = False
        await self._mount_message(user_msg)
        if await self._maybe_continue_agent_session(message):
            return
        await self._send_to_agent(message)

    async def send_agent_message(self, message: str, **kwargs: Any) -> None:
        """Send a message to the agent and start execution.

        Public interface for skill invocation and command handlers.
        """
        await self._send_to_agent(message, message_kwargs=kwargs.get("message_kwargs"))

    async def _send_to_agent(
        self,
        message: str,
        *,
        message_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Send a message to the agent and start execution.

        This is the low-level send path.  The caller mounts the visual
        representation first (``UserMessage``, ``SkillMessage``, etc.).

        Args:
            message: The prompt to send to the agent.
            message_kwargs: Optional metadata kwargs to attach to the HumanMessage.
        """
        # Anchor scroll so streaming response stays visible
        with suppress(NoMatches):
            messages = self.query_one("#messages", MessageList)
            messages._auto_scroll_locked = True
            messages._scroll_to_end()

        # Check if agent is available
        if self._adapter and self._adapter.connected and self._agent_thread_id:
            await self._flush_pending_shell_messages()
            self._agent_turn_started = False
            self._agent_running = True
            await self._set_spinner("Thinking")

            if self._chat_input:
                # Visual hint that input is busy
                pass

            # Spawn a single worker for the agent task
            self._agent_worker = self.run_worker(
                self._run_agent_task(message, message_kwargs=message_kwargs),
                exclusive=False,
            )
        elif self._server_startup_error is not None:
            await self._mount_message(
                ErrorMessage(
                    "Cannot send prompt: Agent server failed to start. "
                    "Check your API key and relaunch, or use `/model` to reconfigure."
                )
            )
        elif not self._adapter or not self._adapter.connected:
            await self._mount_message(
                ErrorMessage(
                    "Agent not connected. Server may still be starting — "
                    "try again in a moment."
                )
            )

    async def _run_agent_task(
        self,
        message: str,
        *,
        graph_input: dict[str, Any] | None = None,
        message_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Run the agent task in a background worker.

        This runs in a Textual worker so the main event loop stays responsive.
        ``_cleanup_agent_task`` always runs in the ``finally`` block.

        Args:
            message: The prompt to send to the agent.
            graph_input: Prepared non-conversation input for a server operation
                (e.g. goal criteria generation).  When set, ``message`` is
                ignored by ``stream_turn``.
            message_kwargs: Optional metadata kwargs to attach to the HumanMessage.
        """
        if self._adapter is None:
            return

        self._agent_turn_started = True

        # Track criteria request correlation for cleanup (reference: app.py L15685-L15691).
        criteria_request_id: str | None = None
        if graph_input is not None:
            criteria_request = graph_input.get("goal_criteria_request")
            if isinstance(criteria_request, dict):
                raw_request_id = criteria_request.get("request_id")
                if isinstance(raw_request_id, str):
                    criteria_request_id = raw_request_id
        agent_session_turn = graph_input is not None and isinstance(graph_input.get("agent_session_request"), dict)
        # Goal-criteria generation is a background server operation, not a
        # conversational turn, so it gets no footer.
        self._turn_metrics_start = (
            None if criteria_request_id is not None else self._snapshot_turn_metrics()
        )

        task_succeeded = False
        try:
            logger.info(
                "TUI: Starting agent turn | thread_id=%s prompt=%r",
                self._agent_thread_id,
                message[:80] + ("..." if len(message) > 80 else ""),
            )
            from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region

            active_aws_prof = get_active_aws_profile()
            active_aws_reg = get_active_aws_region()
            context: dict[str, Any] = {}
            if self._model:
                context["model"] = self._model
            if active_aws_prof:
                context["aws_profile"] = active_aws_prof
            if active_aws_reg:
                context["aws_region"] = active_aws_reg
            mode_val = self._approval_mode.value if hasattr(self._approval_mode, "value") else str(self._approval_mode)
            context["approval_mode"] = mode_val
            context["smart"] = (mode_val.lower() == "smart")
            panel = self._get_subagent_panel()
            if panel is not None:
                spec = self._model or ""
                display_label = spec.split(":")[-1] if ":" in spec else spec
                panel.prepare_turn(model_label=display_label)

            stream_kwargs: dict[str, Any] = {
                "prompt": message,
                "thread_id": self._agent_thread_id or "local_session",
                "context": context if context else None,
                "graph_input": graph_input,
            }
            if message_kwargs is not None:
                stream_kwargs["message_kwargs"] = message_kwargs
            if (
                criteria_request_id is None
                and not agent_session_turn
                and self._agent_thread_id
                and message
                and message.strip()
            ):
                from opscloud.state.conversation_title import schedule_conversation_title_generation

                schedule_conversation_title_generation(
                    self._agent_thread_id,
                    message,
                    model_spec=self._model,
                )
            await self._adapter.stream_turn(**stream_kwargs)
            logger.info("TUI: Agent turn completed successfully | thread_id=%s", self._agent_thread_id)
            task_succeeded = True
        except asyncio.CancelledError:
            logger.debug("Agent task cancelled")
        except Exception as e:
            logger.exception("Agent execution failed: %s", e)
            body = _format_error_detail(e)
            # Criteria generation: surface an actionable message
            # (reference: app.py L15900-L15904).
            if graph_input is not None and graph_input.get("goal_criteria_request"):
                body = (
                    "Could not generate acceptance criteria for this goal. "
                    "Run `/goal` again to retry."
                )
            await self._mount_message(ErrorMessage(body))

        finally:
            if self._turn_metrics_start is not None:
                self._turn_metrics_start["elapsed"] = (
                    time.monotonic() - self._turn_metrics_start["started"]
                )
                self._turn_metrics_start["ok"] = task_succeeded and bool(
                    getattr(self._adapter, "last_turn_ok", True)
                )
            panel = self._get_subagent_panel()
            if panel is not None:
                panel.finalize_running()
            self._close_active_tool_group()
            await self._cleanup_agent_task(
                force_goal_sync=graph_input is not None and not agent_session_turn,
                goal_criteria_request_id=criteria_request_id,
                goal_criteria_succeeded=(
                    criteria_request_id is None or task_succeeded
                ),
            )

    async def _cleanup_agent_task(
        self,
        *,
        force_goal_sync: bool = False,
        goal_criteria_request_id: str | None = None,
        goal_criteria_succeeded: bool = True,
    ) -> None:
        """Tear down after a turn completes or is cancelled.

        Resets spinner/cursor, syncs goal state from the checkpoint, drains
        the message queue, then releases the reconciliation flag.

        Args:
            force_goal_sync: Read goal state even when no local goal fields
                are set (e.g. after a criteria generation turn).
            goal_criteria_request_id: Terminal criteria request to correlate
                when restoring a newly generated proposal.
            goal_criteria_succeeded: Whether criteria generation completed
                without failure or cancellation.
        """
        self._agent_reconciling = True
        self._agent_running = False
        self._agent_worker = None
        self._agent_turn_started = False
        self._active_user_message = None
        self._active_turn_visible_output_started = False

        try:
            try:
                # Dismiss the spinner (reference: app.py L16067).
                if self._adapter and self._adapter._set_spinner:
                    try:
                        await self._adapter._set_spinner(None)
                    except Exception:
                        pass

                # ── Goal state synchronization from checkpoint ──
                await self._sync_goal_state_from_checkpoint(
                    force=force_goal_sync,
                    proposal_request_id=goal_criteria_request_id,
                    allow_pending_proposal=goal_criteria_succeeded,
                )

                # ── Session cost & token state synchronization from checkpoint ──
                try:
                    await self._sync_session_cost_from_checkpoint()
                except Exception:
                    logger.exception("Error during agent cleanup session cost sync")

            except Exception:
                logger.exception("Error during agent cleanup goal sync")

            # Regroup completed tools at turn boundary
            try:
                await self._regroup_completed_tools()
            except Exception:
                logger.exception("Error during agent turn regrouping")

            # Turn footer (after cost sync so tokens/cost are reconciled,
            # before the queue drains so it sits under *this* turn).
            try:
                await self._mount_turn_summary()
            except Exception:
                logger.exception("Error mounting turn summary")

            # Process next message from queue
            if not self._startup_sequence_running:
                await self._process_next_from_queue()
        finally:
            self._agent_reconciling = False
            self._focus_chat_input_after_refresh()

    # ── Turn Summary Footer ──────────────────────────────

    def _snapshot_turn_metrics(self) -> dict[str, Any]:
        """Capture the counters a turn footer is computed from (as deltas)."""
        tool_total = 0
        with suppress(NoMatches):
            tool_total = getattr(self.query_one("#messages", MessageList), "_tool_call_total", 0)
        return {
            "started": time.monotonic(),
            "tools": tool_total,
            "tokens": self._cumulative_session_tokens,
            "cost": self._session_cost_usd,
        }

    async def _mount_turn_summary(self) -> None:
        """Mount the ``✓ Done in … · N tools · tokens · cost`` footer.

        Only shown for turns that completed cleanly; errors and interrupts
        already leave their own message in the transcript.  The snapshot is
        consumed so cleanup paths that run without a turn never mount one.
        """
        start = self._turn_metrics_start
        self._turn_metrics_start = None
        if not start or not start.get("ok"):
            return
        from opscloud.ui.widgets.messages import TurnSummary

        tool_total = start["tools"]
        with suppress(NoMatches):
            tool_total = getattr(self.query_one("#messages", MessageList), "_tool_call_total", tool_total)
        await self._mount_message(
            TurnSummary(
                elapsed_seconds=float(start.get("elapsed", 0.0)),
                tool_count=max(0, tool_total - start["tools"]),
                tokens=max(0, self._cumulative_session_tokens - start["tokens"]),
                cost_usd=max(0.0, self._session_cost_usd - start["cost"]),
            )
        )

    # ── Goal State Management ────────────────────────────
    #
    # Goal infrastructure: mutation boundary, checkpoint persistence, state
    # sync from thread, criteria request submission, and accept flow.

    @asynccontextmanager
    async def _goal_state_mutation_boundary(self) -> AsyncGenerator[None, None]:
        async with _goals_ctrl(self).goal_state_mutation_boundary():
            yield

    async def _aupdate_thread_state(self, update: dict[str, Any]) -> None:
        await _goals_ctrl(self).aupdate_thread_state(update)

    def _goal_state_update(self) -> dict[str, Any]:
        return _goals_ctrl(self).goal_state_update()

    async def _persist_goal_rubric_state(
        self,
        *,
        notice: Any | None = None,
        state_update: dict[str, Any] | None = None,
    ) -> bool:
        return await _goals_ctrl(self).persist_goal_rubric_state(
            notice=notice,
            state_update=state_update,
        )

    async def _get_thread_state_values(self, thread_id: str | None = None) -> dict[str, Any]:
        return await _goals_ctrl(self).get_thread_state_values(thread_id)

    async def _reconstruct_state_from_local_db(
        self,
        thread_id: str,
    ) -> dict[str, Any]:
        return await _goals_ctrl(self).reconstruct_state_from_local_db(thread_id)

    @staticmethod
    def _reduce_message_writes(
        rows: list[tuple[str, bytes]],
        serde: Any,
    ) -> list[Any]:
        from opscloud.ui.controllers.goal_controller import GoalController

        return GoalController.reduce_message_writes(rows, serde)

    async def _sync_goal_state_from_checkpoint(
        self,
        *,
        force: bool = False,
        proposal_request_id: str | None = None,
        allow_pending_proposal: bool = True,
    ) -> bool:
        return await _goals_ctrl(self).sync_goal_state_from_checkpoint(
            force=force,
            proposal_request_id=proposal_request_id,
            allow_pending_proposal=allow_pending_proposal,
        )

    async def _sync_session_cost_from_checkpoint(self) -> None:
        await _goals_ctrl(self).sync_session_cost_from_checkpoint()

    async def _run_goal_criteria_request(
        self,
        request: dict[str, Any],
    ) -> None:
        await _goals_ctrl(self).run_goal_criteria_request(request)

    async def _accept_goal_rubric(
        self,
        rubric: str,
    ) -> bool:
        return await _goals_ctrl(self).accept_goal_rubric(rubric)

    async def _continue_goal_work(
        self,
        transition: str,
        *,
        objective: str | None = None,
        persisted: bool = True,
    ) -> None:
        await _goals_ctrl(self).continue_goal_work(
            transition,
            objective=objective,
            persisted=persisted,
        )

    # ── Shell Command Execution (ShellController) ────────

    async def _handle_shell_command(
        self,
        command: str,
        *,
        incognito: bool = False,
    ) -> None:
        await _shell_ctrl(self).handle_shell_command(command, incognito=incognito)

    async def _run_shell_task(
        self,
        command: str,
        *,
        incognito: bool = False,
    ) -> None:
        await _shell_ctrl(self).run_shell_task(command, incognito=incognito)

    async def _kill_shell_process(self) -> None:
        await _shell_ctrl(self).kill_shell_process()


    # ── Queue Processing ─────────────────────────────────

    async def _process_next_from_queue(self) -> None:
        """Process the next message from the queue if any exist.

        Uses ``_processing_pending`` to prevent re-entrant execution.
        """
        if self._processing_pending or not self._pending_messages or self._exit:
            return

        self._processing_pending = True
        msg: QueuedMessage | None = None
        try:
            msg = self._pending_messages.popleft()
            self._sync_status_queued()

            # Remove the ephemeral queued-message widget
            if self._queued_widgets:
                widget = self._queued_widgets.popleft()
                try:
                    await widget.remove()
                except Exception:
                    pass

            await self._process_message(msg.text, msg.mode)
        except Exception:
            logger.exception("Failed to process queued message")
            err_snippet = msg.text[:60] if msg else "unknown message"
            await self._mount_message(
                ErrorMessage(f"Failed to process queued message: {err_snippet}")
            )
        finally:
            self._processing_pending = False


        # Command-mode messages complete synchronously; continue draining
        busy = self._agent_running or self._shell_running
        if not busy and self._pending_messages:
            await self._process_next_from_queue()

    # ── Slash Command Registry Dispatch ──────────────────

    async def _handle_command(self, text: str) -> None:
        """Dispatch slash command via command_registry.

        Commands execute synchronously (no worker).  Only DevOps commands
        that need agent interaction re-enter via ``_send_to_agent``.

        Args:
            text: Full command text including ``/`` prefix.
        """
        # Normalize leading "/ " e.g. "/ goal" -> "/goal"
        normalized = text.strip()
        if normalized.startswith("/") and len(normalized) > 1 and normalized[1].isspace():
            normalized = "/" + normalized[1:].lstrip()
            text = normalized

        parts = normalized.split(maxsplit=1)
        cmd_name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # ── Router Dispatch Check ────────────────────────
        handler, resolved_cmd_name, resolved_arg = self._command_router.resolve_command(normalized)
        if handler is None and get_command(cmd_name) is None:
            from opscloud.skills.invocation import match_discovered_skill

            skills = self._discovered_skills or self.get_discovered_skills()
            if match_discovered_skill(skills, cmd_name.lstrip("/")) is not None:
                handler = self._command_router.get_handler("/skill:")

        if handler is not None:
            if self._agent_running and handler.bypass_tier == BypassTier.QUEUED:
                self._pending_messages.append(QueuedMessage(text=text, mode="command"))
                await self._mount_message(
                    SystemMessage(f"Command `{resolved_cmd_name}` queued...")
                )
                return

            is_skill_invoke = (
                resolved_cmd_name.startswith("/skill:")
                or getattr(handler, "name", "") == "/skill:"
            )
            if resolved_cmd_name not in {"/exit", "/quit", "/q", "/clear", "/force-clear", "/remember", "/skill-creator", "/btw", "/aside", "/fork", "/branch"} and not is_skill_invoke:
                await self._mount_message(UserMessage(text))

            from opscloud.commands import CommandContext
            from opscloud.config.settings import settings as global_settings
            ctx = CommandContext(
                app=self,
                session=getattr(self, "_session_state", None),
                agent=getattr(self, "_agent", None),
                settings=getattr(self, "_settings", None) or global_settings,
                raw_command=text,
                args=resolved_arg,
                thread_id=self._agent_thread_id,
                model_spec=self._model,
            )
            res = await self._command_router.dispatch(text, ctx)
            if res.mount_as_app_message and res.message:
                from opscloud.ui.widgets.messages import CommandOutputMessage
                await self._mount_message(CommandOutputMessage(res.message, command=resolved_cmd_name))
            if res.notify:
                self.notify(res.notify, severity=res.notify_severity)
            if res.push_screen:
                self.push_screen(res.push_screen)
            return

        command = get_command(cmd_name)

        if not command:
            await self._mount_message(
                SystemMessage(f"Unknown command: `{cmd_name}`. Try `/help`.")
            )
            return

        # Check queueing for QUEUED tier when busy
        if self._agent_running and command.bypass_tier == BypassTier.QUEUED:
            self._pending_messages.append(QueuedMessage(text=text, mode="command"))
            await self._mount_message(
                SystemMessage(f"Command `{cmd_name}` queued...")
            )
            return

        if cmd_name not in {"/exit", "/quit", "/q", "/clear", "/force-clear", "/remember", "/skill-creator", "/btw", "/aside", "/fork", "/branch"} and not cmd_name.startswith("/skill:"):
            await self._mount_message(UserMessage(text))

        # ── Dispatch (Fallback for unmigrated inline commands) ──────

        if cmd_name == "/theme":
            if arg:
                await self._switch_theme(arg)
            else:
                await self._show_theme_selector()

        elif cmd_name in {"/agents", "/agent"}:
            from opscloud.commands import CommandContext
            from opscloud.commands.power.agents import AgentsHandler
            from opscloud.config.settings import settings as global_settings

            ctx = CommandContext(
                app=self,
                session=getattr(self, "_session_state", None),
                agent=getattr(self, "_agent", None),
                settings=getattr(self, "_settings", None) or global_settings,
                raw_command=text,
                args=arg,
                thread_id=self._agent_thread_id,
                model_spec=self._model,
            )
            res = await AgentsHandler().execute(ctx)
            if res.mount_as_app_message and res.message:
                from opscloud.ui.widgets.messages import CommandOutputMessage
                await self._mount_message(CommandOutputMessage(res.message, command=cmd_name))

        elif cmd_name in {"/manual", "/auto", "/smart"}:
            target_mode = ApprovalMode(cmd_name.lstrip("/"))
            await self._set_approval_mode(target_mode)

        elif cmd_name == "/goal":
            if arg:
                self._goal = arg
                await self._mount_message(
                    SystemMessage(f"Persistent Goal Set: `{arg}`")
                )
            else:
                await self._mount_message(
                    SystemMessage(f"Current Goal: `{self._goal or 'None'}`")
                )



    # ── Runtime Command Handlers (/reload, /restart, /install) ──

    async def _mount_transient_app_message(self, content: str) -> SystemMessage | None:
        """Mount a transient status message (e.g. 'Restarting server...')."""
        widget = SystemMessage(content)
        await self._mount_message(widget)
        return widget

    def _sync_status_connection(self) -> None:
        """Sync connection status to the status bar."""
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            if self._connecting or self._reconnecting:
                status_bar.set_status("Connecting...")
            elif not (self._agent_running or self._shell_running):
                status_bar.set_status("Ready")
        except Exception:
            pass

    def _discard_queue(self) -> None:
        """Drop all queued messages and remove their placeholder widgets."""
        self._pending_messages.clear()
        while self._queued_widgets:
            widget = self._queued_widgets.popleft()
            try:
                widget.remove()
            except Exception:
                pass
        self._sync_status_queued()

    async def _restore_queue_to_input(
        self,
        notice: str,
        *,
        empty_notice: str,
    ) -> None:
        """Return every queued prompt to the chat input and drop the queue."""
        has_queued = bool(self._pending_messages or self._queued_widgets)
        msg = notice if has_queued else empty_notice
        await self._mount_message(SystemMessage(msg))
        if has_queued and self._chat_input:
            texts = [m.text for m in self._pending_messages]
            self._discard_queue()
            if texts:
                existing = self._chat_input.text
                new_text = "\n\n".join(texts)
                self._chat_input.text = f"{existing}\n\n{new_text}" if existing else new_text

    # ── /restart, /reload, /install & /update (ServerLifecycleController) ──

    async def _handle_restart_command(self, command: str, *, echo: bool = False) -> None:
        await _server_ctrl(self).handle_restart_command(command, echo=echo)

    def _schedule_restart_command(
        self,
        *,
        preserve_queue: bool = False,
        propagate_errors: bool = False,
        mutation_lock_held: bool = False,
    ) -> asyncio.Task[bool]:
        return _server_ctrl(self).schedule_restart_command(
            preserve_queue=preserve_queue,
            propagate_errors=propagate_errors,
            mutation_lock_held=mutation_lock_held,
        )

    def _track_server_restart_task(self, task: asyncio.Task[Any]) -> None:
        _server_ctrl(self).track_server_restart_task(task)

    def _finish_restart(self, task: asyncio.Task[Any]) -> None:
        _server_ctrl(self).finish_restart(task)

    async def _run_restart_command_detached(
        self,
        *,
        preserve_queue: bool = False,
        propagate_errors: bool = False,
        mutation_lock_held: bool = False,
    ) -> bool:
        return await _server_ctrl(self).run_restart_command_detached(
            preserve_queue=preserve_queue,
            propagate_errors=propagate_errors,
            mutation_lock_held=mutation_lock_held,
        )

    async def _run_restart_command(
        self,
        *,
        preserve_queue: bool = False,
        propagate_errors: bool = False,
    ) -> bool:
        return await _server_ctrl(self).run_restart_command(
            preserve_queue=preserve_queue,
            propagate_errors=propagate_errors,
        )

    async def _reload_configuration_for_restart(self) -> bool:
        return await _server_ctrl(self).reload_configuration_for_restart()

    async def _run_restart_respawn(self, *, propagate_errors: bool = False) -> bool:
        return await _server_ctrl(self).run_restart_respawn(propagate_errors=propagate_errors)

    async def _restart_server_manual(self) -> bool:
        return await _server_ctrl(self).restart_server_manual()

    async def _restart_server_manual_result(self) -> _ServerRespawnResult:
        return await _server_ctrl(self).restart_server_manual_result()

    async def _respawn_server(
        self,
        *,
        log_message: str = "Server respawn failed",
        mcp_failure_log: str = "MCP preload after restart failed",
        mcp_failure_toast: str = "MCP metadata could not be refreshed",
        restart_timeout: float = 60.0,
    ) -> _ServerRespawnResult:
        return await _server_ctrl(self).respawn_server(
            log_message=log_message,
            mcp_failure_log=mcp_failure_log,
            mcp_failure_toast=mcp_failure_toast,
            restart_timeout=restart_timeout,
        )

    def _take_reload_snapshot(self) -> dict[str, Any]:
        return _server_ctrl(self).take_reload_snapshot()

    def _schedule_reload(self) -> asyncio.Task[None]:
        return _server_ctrl(self).schedule_reload()

    def _finish_reload(self, task: asyncio.Task[None]) -> None:
        _server_ctrl(self).finish_reload(task)

    async def _run_reload_sequence(self) -> None:
        await _server_ctrl(self).run_reload_sequence()

    async def _run_reload(self) -> None:
        await _server_ctrl(self).run_reload()

    async def _run_reload_unlocked(self) -> None:
        await _server_ctrl(self).run_reload_unlocked()

    async def _invoke_reload(self, *, command: str | None = None) -> None:
        await _server_ctrl(self).invoke_reload(command=command)

    async def _handle_install_command(self, command: str) -> None:
        await _server_ctrl(self).handle_install_command(command)

    async def _handle_update_command(self, command: str = "/update") -> None:
        await _server_ctrl(self).handle_update_command(command)


    # ── Phase 2 Helper Methods ────────────────────────────

    def prepare_exit(self) -> None:
        """Clear message queues and cancel active adapter workers prior to termination."""
        self._pending_messages.clear()
        self._queued_widgets.clear()
        if self._adapter:
            self._adapter.cancel()

    def get_latest_assistant_message(self) -> str | None:
        """Retrieve latest assistant message content for clipboard copy."""
        try:
            from opscloud.ui.widgets.messages import AssistantMessage
            msgs = list(self.query(AssistantMessage))
            for latest in reversed(msgs):
                msg_text = getattr(latest, "content_text", None) or "".join(getattr(latest, "_fragments", []))
                if not msg_text or not msg_text.strip():
                    continue
                if getattr(latest, "is_streaming", False):
                    continue
                return msg_text
        except Exception:
            pass
        return None

    def _goal_rubric_payload_from_state(
        self,
        state_values: dict[str, Any],
        raw_messages: list[Any] | None = None,
    ) -> dict[str, Any]:
        return _goals_ctrl(self).goal_rubric_payload_from_state(state_values, raw_messages)

    def _sync_status_rubric(self) -> None:
        _goals_ctrl(self).sync_status_rubric()

    def _restore_goal_rubric_state(self, payload: dict[str, Any]) -> None:
        _goals_ctrl(self).restore_goal_rubric_state(payload)

    async def _load_thread_history(self, thread_id: str) -> None:
        await _threads_ctrl(self).load_thread_history(thread_id)

    def _close_active_tool_group(self) -> None:
        _threads_ctrl(self).close_active_tool_group()

    async def _regroup_completed_tools(self) -> None:
        await _threads_ctrl(self).regroup_completed_tools()

    async def resume_thread(self, thread_id: str, *, is_new: bool = False) -> None:
        await _threads_ctrl(self).resume_thread(thread_id, is_new=is_new)

    async def fork_thread(self, *, source_thread_id: str | None = None) -> tuple[str, str]:
        return await _threads_ctrl(self).fork_thread(source_thread_id=source_thread_id)

    async def invoke_compact_conversation(self, thread_id: str | None = None, force: bool = True) -> None:
        await _threads_ctrl(self).invoke_compact_conversation(thread_id=thread_id, force=force)

    def toggle_timestamps(self, visible: bool | None = None) -> bool:
        return _threads_ctrl(self).toggle_timestamps(visible=visible)

    def get_context_tokens(self) -> int:
        return _threads_ctrl(self).get_context_tokens()

    async def _get_conversation_token_count(self) -> int | None:
        return await _threads_ctrl(self).get_conversation_token_count()

    async def _has_conversation_messages(self) -> bool:
        return await _threads_ctrl(self).has_conversation_messages()

    async def get_conversation_token_count(self) -> int | None:
        return await self._get_conversation_token_count()

    def get_thread_messages(self) -> list:
        return _threads_ctrl(self).get_thread_messages()


    def get_active_tools(self) -> list[dict[str, str]]:
        """Get list of active core tools with names and descriptions."""
        return [
            {"name": "read_file", "description": "Read file contents with line range filtering."},
            {"name": "write_file", "description": "Create new files or overwrite existing files."},
            {"name": "edit_file", "description": "Perform precise contiguous block edits on a file."},
            {"name": "run_command", "description": "Execute terminal shell commands on system."},
            {"name": "grep_search", "description": "Fast regex and exact string pattern searching."},
            {"name": "list_dir", "description": "List contents and metadata of workspace directories."},
            {"name": "view_file", "description": "View text and binary files (images, pdfs)."},
        ]

    def get_mcp_servers(self) -> list:
        """Return MCP server metadata for /mcp, /skills, and /doctor commands."""
        servers = getattr(self, "_mcp_server_info", [])
        if not servers:
            try:
                from opscloud.mcp.preload import get_cached_mcp_server_infos

                cached = get_cached_mcp_server_infos()
                if cached:
                    self._mcp_server_info = cached
                    return cached
            except Exception:
                pass
        return servers

    def get_discovered_skills(self) -> list[dict[str, Any]]:
        """Get list of discovered skills (project, user, plugin, built-in)."""
        from pathlib import Path
        from opscloud.config.settings import settings
        from opscloud.skills.loader import list_skills

        built_in_dir = Path(__file__).parent.parent / "built_in_skills"
        user_skills_dir = settings.get_user_skills_dir("opscloud")
        ws_trusted = getattr(self, "_workspace_trusted", True)
        project_skills_dir = settings.get_project_skills_dir() if ws_trusted else None
        project_root = (
            (
                getattr(self._settings, "project_root", None)
                if hasattr(self, "_settings") and self._settings
                else settings.project_root
            )
            if ws_trusted
            else None
        )

        skills = list_skills(
            built_in_skills_dir=built_in_dir,
            user_skills_dir=user_skills_dir,
            project_skills_dir=project_skills_dir,
            include_plugins=True,
            project_root=project_root,
        )
        return [dict(s) for s in skills]

    def get_discovered_plugin_agents(self) -> list[dict[str, Any]]:
        """Get list of discovered plugin subagents."""
        from opscloud.config.settings import settings
        from opscloud.plugins.adapters.agents import discover_plugin_subagents

        ws_trusted = getattr(self, "_workspace_trusted", True)
        project_root = (
            (
                getattr(self._settings, "project_root", None)
                if hasattr(self, "_settings") and self._settings
                else settings.project_root
            )
            if ws_trusted
            else None
        )
        agents = discover_plugin_subagents(project_root=project_root)
        return [dict(a) for a in agents]

    async def _discover_startup_skills(self) -> None:
        """Discover skills, custom commands, and plugin agents in the background on startup and populate autocomplete."""
        try:
            skills = await asyncio.to_thread(self.get_discovered_skills)
            self._discovered_skills = skills
            plugin_agents = await asyncio.to_thread(self.get_discovered_plugin_agents)
            self._discovered_plugin_agents = plugin_agents
            if hasattr(self, "_chat_input") and self._chat_input:
                from opscloud.ui.command_registry import (
                    build_agent_commands,
                    build_custom_commands,
                    build_skill_commands,
                    get_all_entries,
                )

                skill_commands = build_skill_commands(skills or [])
                skill_commands.extend(build_agent_commands(plugin_agents or []))
                skill_commands.extend(build_custom_commands())
                self._chat_input.update_slash_commands(get_all_entries(skill_commands))
        except Exception as exc:
            logger.debug("Failed startup skill discovery: %s", exc)

    async def _run_custom_command_turn(
        self,
        prompt: str,
        payload: dict[str, Any],
    ) -> None:
        """Submit an expanded custom command turn with active command permissions/model/effort/fork context."""
        if not self._adapter or not self._adapter.connected or not self._agent_thread_id:
            await self._send_to_agent(prompt)
            return

        await self._flush_pending_shell_messages()
        self._agent_turn_started = False
        self._agent_running = True
        cmd_label = payload.get("command_name") or "command"
        spinner = (
            f"Running /{cmd_label} (forked)"
            if payload.get("execution_context") == "fork"
            else f"Thinking (/{cmd_label})"
        )
        await self._set_spinner(spinner)
        from langchain_core.messages import HumanMessage

        self._agent_worker = self.run_worker(
            self._run_agent_task(
                prompt,
                graph_input={
                    "messages": [HumanMessage(content=prompt)],
                    "custom_command_request": payload,
                },
            ),
            exclusive=False,
        )

    async def switch_model(self, model_name: str, extra_kwargs: dict | None = None) -> None:
        """Switch active LLM model."""
        self.run_worker(
            self._switch_model(model_name),
            exclusive=False,
            group="model-switch",
        )

    async def set_default_model(self, model_name: str) -> None:
        """Set default model in settings."""
        self._model = model_name
        self._manual_model = model_name
        from opscloud.approval_mode import ApprovalMode
        if getattr(self, "_approval_mode", ApprovalMode.MANUAL) is ApprovalMode.MANUAL:
            await self._restore_manual_default_model()

    async def clear_default_model(self) -> None:
        """Clear default model setting."""
        pass

    def save_previous_model(self, model: str | None, effort: str | None) -> None:
        """Save previous model and reasoning effort before fast mode toggle."""
        self._prev_model = model
        self._prev_effort = effort

    def get_previous_model(self) -> str | None:
        """Get previously saved model name."""
        return getattr(self, "_prev_model", None)

    def get_previous_effort(self) -> str | None:
        """Get previously saved reasoning effort."""
        return getattr(self, "_prev_effort", None)

    def _cancel_git_branch_refresh_task(self) -> None:
        """Cancel and clear any in-flight background branch refresh task."""
        prior_task = self._git_branch_refresh_task
        if prior_task is not None and not prior_task.done():
            prior_task.cancel()
        self._git_branch_refresh_task = None

    def _reset_thread_usage(
        self,
        cost_usd: float = 0.0,
        context_tokens: int = 0,
        cumulative_tokens: int = 0,
    ) -> None:
        """Reset active thread token and cost metrics in the app state and status bar."""
        from opscloud.utils.session_stats import SessionStats

        self._session_cost_usd = max(0.0, cost_usd)
        self._context_tokens = max(0, context_tokens)
        self._cumulative_session_tokens = max(0, cumulative_tokens or context_tokens)
        if self._adapter:
            self._adapter._stats = SessionStats()
            if cost_usd > 0:
                self._adapter._stats.total_cost_usd = cost_usd
        if self._status_bar:
            display_tokens = self._cumulative_session_tokens or self._context_tokens
            self._status_bar.set_tokens(display_tokens, context_tokens=self._context_tokens)
            self._status_bar.set_cost(self._session_cost_usd)

    async def _refresh_git_branch(self) -> None:
        """Refresh the git branch display on the status bar."""
        try:
            cwd = getattr(self, "_cwd", str(Path.cwd()))
            branch = read_git_branch_from_filesystem(cwd)
            if branch is None:
                branch = await asyncio.to_thread(read_git_branch_via_subprocess, cwd)
            if self._status_bar:
                self._status_bar.branch = branch
        except Exception:
            logger.warning("Git branch resolution failed", exc_info=True)

    async def _refresh_git_branch_subprocess_fallback(self, cwd: str) -> None:
        """Run the `git rev-parse` fallback off-thread for unusual repo layouts."""
        try:
            branch = await asyncio.to_thread(read_git_branch_via_subprocess, cwd)
        except Exception:
            logger.warning("Git branch subprocess fallback failed", exc_info=True)
            return
        if self._status_bar:
            self._status_bar.branch = branch

    def _schedule_git_branch_refresh(self) -> None:
        """Refresh the git branch, inline when possible."""
        if getattr(self, "_exit", False):
            return

        cwd = getattr(self, "_cwd", str(Path.cwd()))
        try:
            branch = read_git_branch_from_filesystem(cwd)
        except Exception:
            logger.warning("Git branch filesystem probe failed", exc_info=True)
            return

        if branch is not None:
            if self._status_bar:
                self._status_bar.branch = branch
            self._cancel_git_branch_refresh_task()
            return

        self._cancel_git_branch_refresh_task()
        refresh_task = asyncio.create_task(
            self._refresh_git_branch_subprocess_fallback(cwd),
        )
        self._git_branch_refresh_task = refresh_task

        def _finalize_git_branch_refresh(task: asyncio.Task[None]) -> None:
            if self._git_branch_refresh_task is task:
                self._git_branch_refresh_task = None
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "Background git branch refresh failed unexpectedly",
                    exc_info=True,
                )

        refresh_task.add_done_callback(_finalize_git_branch_refresh)

    def _update_auto_mode_status_bar(self, mode: str) -> None:
        """Update TUI status bar indicator badge for auto-mode."""
        try:
            status_bar = self._status_bar or self.query_one("#status-bar", StatusBar)
            status_bar.set_approval_mode(mode)
        except Exception:
            pass

    def _force_interrupt_active_work(self) -> None:
        """Force-interrupt any active agent or shell work."""
        if self._adapter:
            self._adapter.cancel()
        if self._agent_worker:
            self._agent_worker.cancel()
        if self._shell_worker:
            self._shell_worker.cancel()
        self._agent_running = False
        self._shell_running = False

    # ── TextualAdapter Event Handlers ────────────────────

    @on(TextualAdapter.TokenStreamed)
    def _on_adapter_token(self, event: TextualAdapter.TokenStreamed) -> None:
        pass

    @on(TextualAdapter.ToolCallStarted)
    def _on_tool_started(self, event: TextualAdapter.ToolCallStarted) -> None:
        # Tool activity is shown inline via ToolCallMessage widgets.
        # The reference does NOT put tool names in the status bar.
        pass

    @on(TextualAdapter.ToolCallCompleted)
    def _on_tool_completed(self, event: TextualAdapter.ToolCallCompleted) -> None:
        # Tool completion is shown inline via ToolCallMessage.set_success().
        pass

    async def _on_smart_approve_enabled(self) -> bool:
        """Enable Smart mode when selected in approval menu."""
        from opscloud.approval_mode import ApprovalMode
        return await self._set_approval_mode(ApprovalMode.SMART)

    async def _on_auto_approve_enabled(self) -> bool:
        """Alias for enabling Smart mode when selected in approval menu."""
        return await self._on_smart_approve_enabled()

    async def _request_approval(
        self,
        action_requests: Any,
        assistant_id: str | None = None,
    ) -> asyncio.Future[Any]:
        """Delegate HITL tool approval request to `PromptController`."""
        return await self._prompts.request_approval(action_requests, assistant_id)

    async def _mount_approval_widget(
        self,
        menu: ApprovalMenu,
        result_future: asyncio.Future[dict[str, str]],
    ) -> None:
        """Mount the approval menu widget inline in the messages area."""
        await self._prompts.mount_approval_widget(menu, result_future)

    async def _deferred_show_approval(
        self,
        placeholder: Any,
        menu: ApprovalMenu,
        result_future: asyncio.Future[dict[str, str]],
    ) -> None:
        """Wait until user is idle, then swap placeholder for real menu."""
        await self._prompts.deferred_show_approval(placeholder, menu, result_future)

    @on(ApprovalMenu.Decided)
    async def _on_approval_menu_decided(self, event: ApprovalMenu.Decided) -> None:
        await self._prompts.on_approval_menu_decided(event)

    @on(TextualAdapter.InterruptRaised)
    async def _on_interrupt_raised(self, event: TextualAdapter.InterruptRaised) -> None:
        """Legacy event handler for InterruptRaised."""
        await self._prompts.on_interrupt_raised(event)

    # ── Ask User Interrupt Handling ──────────────────────

    async def _request_ask_user(
        self,
        questions: list[Any],
    ) -> asyncio.Future[Any]:
        """Delegate interactive `AskUserMenu` prompt to `PromptController`."""
        return await self._prompts.request_ask_user(questions)

    @on(AskUserMenu.Answered)
    async def _on_ask_user_menu_answered(self, event: AskUserMenu.Answered) -> None:
        """Handle ask_user menu answers - remove widget and refocus input."""
        await self._prompts.finish_ask_user_prompt(context="answered")

    @on(AskUserMenu.Cancelled)
    async def _on_ask_user_menu_cancelled(self, event: AskUserMenu.Cancelled) -> None:
        """Handle ask_user menu cancellation - remove widget and refocus input."""
        await self._prompts.finish_ask_user_prompt(context="cancelled")

    async def _finish_ask_user_prompt(self, *, context: str = "finished") -> None:
        """Remove the AskUserMenu widget and return focus to chat input."""
        await self._prompts.finish_ask_user_prompt(context=context)

    async def _cancel_pending_ask_user(self, *, context: str = "cleanup") -> None:
        """Cancel and remove any mounted pending ask_user prompt."""
        await self._prompts.cancel_pending_ask_user(context=context)

    def _is_input_focused(self) -> bool:
        if not hasattr(self, "_chat_input") or not self._chat_input:
            return False
        focused = self.focused
        if focused is None:
            return False
        return focused.id == "chat-input" or focused in self._chat_input.walk_children()

    @on(ApprovalDecided)
    def _on_approval_decided(self, event: ApprovalDecided) -> None:
        self._prompts.on_approval_decided(event)

    def _get_subagent_panel(self) -> SubagentPanel | None:
        """Return the subagent fan-out panel, or None if not yet mounted."""
        try:
            return self.query_one("#subagent-panel", SubagentPanel)
        except Exception:
            return None

    def _on_subagent_event(self, event: dict[str, Any]) -> None:
        """Forward a validated subagent custom-stream event to the panel."""
        panel = self._get_subagent_panel()
        if panel is not None:
            panel.on_subagent_event(event)

    def _on_user_visible_output_started(self) -> None:
        """Record that the current turn has rendered model text or a tool call."""
        self._active_turn_visible_output_started = True

    def action_toggle_subagent_panel(self) -> None:
        """Expand or collapse the subagent fan-out panel."""
        panel = self._get_subagent_panel()
        if panel is not None:
            panel.toggle()

    @on(TextualAdapter.SubagentSpawned)
    def _on_subagent_spawned(self, event: TextualAdapter.SubagentSpawned) -> None:
        panel = self._get_subagent_panel()
        if panel is not None:
            panel.spawn_subagent(event.agent_name, event.task)

    @on(TextualAdapter.SubagentUpdate)
    def _on_subagent_update(self, event: TextualAdapter.SubagentUpdate) -> None:
        panel = self._get_subagent_panel()
        if panel is not None:
            panel.append_token(event.agent_name, event.token)

    @on(TextualAdapter.StreamFinished)
    async def _on_stream_finished(self, event: TextualAdapter.StreamFinished) -> None:
        panel = self._get_subagent_panel()
        if panel is not None:
            panel.finalize_running()
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            display_tokens = self._cumulative_session_tokens or self._context_tokens
            if display_tokens > 0:
                status_bar.set_tokens(display_tokens, context_tokens=self._context_tokens)
            cost_to_display = max(event.stats.total_cost_usd, self._session_cost_usd)
            if cost_to_display > 0:
                self._session_cost_usd = cost_to_display
                status_bar.set_cost(cost_to_display)
            status_bar.set_status("Ready")
        except NoMatches:
            pass
        # Note: queue draining is handled by _cleanup_agent_task, not here

    @on(TextualAdapter.StreamError)
    async def _on_stream_error(self, event: TextualAdapter.StreamError) -> None:
        show_toast(self, event.error, title="Stream Error", severity="error")
        try:
            messages = self.query_one("#messages", MessageList)
            await self._mount_message(ErrorMessage(event.error))
        except NoMatches:
            pass

    async def on_markdown_link_clicked(self, event: Any) -> None:
        """Handle link clicks from any Markdown widget across the application."""
        event.stop()
        from opscloud.ui.widgets._links import open_link_or_file_async

        href = getattr(event, "href", "")
        if href:
            await open_link_or_file_async(href, app=self, notify_on_success=True)

    # ── Global Actions ───────────────────────────────────

    def _cancel_worker(
        self, worker: Worker[None] | None, *, abort_pending_reconnect: bool = True
    ) -> None:
        """Discard the message queue and cancel an active worker."""
        self._discard_queue()
        if worker is not None:
            worker.cancel()
            self._recover_unstarted_agent_worker(worker)

    def _recover_unstarted_agent_worker(self, worker: Worker[None]) -> None:
        """Release the turn when a cancelled agent worker never started."""
        if worker is not self._agent_worker or getattr(self, "_agent_turn_started", False):
            return

        async def _release() -> None:
            if (
                worker is not self._agent_worker
                or getattr(self, "_agent_turn_started", False)
                or not self._agent_running
            ):
                return
            logger.warning(
                "Agent worker was cancelled before it started; releasing the turn"
            )
            try:
                await self._cleanup_agent_task()
            except Exception:
                logger.exception("Cleanup failed while releasing an unstarted turn")

        if not self.call_after_refresh(_release):
            logger.warning(
                "Could not schedule unstarted-turn recovery; message pump is closing"
            )

    def _restore_interrupted_message_to_input(self, message: Any) -> None:
        """Return an interrupted prompt to the chat input when it is empty and no output appeared."""
        if getattr(self, "_active_turn_visible_output_started", False):
            return
        chat_input = self._chat_input
        if chat_input is None:
            return
        if chat_input.value.strip():
            return
        raw_text = getattr(message, "raw_text", getattr(message, "_raw_content", ""))
        chat_input.set_value_at_end(raw_text)

    def _pop_last_queued_message(self) -> None:
        """Remove the most recently queued message (LIFO)."""
        if not self._pending_messages:
            return
        msg = self._pending_messages.pop()
        self._sync_status_queued()
        if self._queued_widgets:
            widget = self._queued_widgets.pop()
            try:
                widget.remove()
            except Exception:
                pass
        msg_text = msg.text if hasattr(msg, "text") else str(msg)
        if not self._chat_input:
            self.notify("Queued message discarded", timeout=2)
            return

        if self._chat_input.value.strip():
            self.notify("Queued message discarded (input not empty)", timeout=3)
        elif self._chat_input.set_value_at_end(msg_text):
            self.notify("Queued message moved to input", timeout=2)
        else:
            self.notify("Queued message discarded", timeout=2)

    def _handle_clear_input_escape(self) -> None:
        """Clear the chat input draft on double Esc press."""
        chat_input = self._chat_input
        if chat_input is None or not chat_input.value:
            self._clear_input_pending = False
            return
        if getattr(self, "_clear_input_pending", False):
            self._clear_input_pending = False
            if chat_input.discard_text():
                self.notify("Input cleared", timeout=3)
        else:
            self._clear_input_pending = True
            self.notify("Press Esc again to clear input", timeout=2)
            self.set_timer(2.0, lambda: setattr(self, "_clear_input_pending", False))

    def action_interrupt(self) -> None:
        """Handle escape key or interrupt request.

        Priority order (aligned with dcode):
        1. If modal screen is active, dismiss it
        2. If completion popup is open, dismiss it
        3. If input is in command/shell mode, exit to normal mode
        4. If shell command is running, kill it
        5. If approval menu is active, reject it
        6. If ask-user menu is active, cancel it
        7. If queued messages exist, pop the last one (LIFO)
        8. If agent is running, interrupt it (restoring the interrupted prompt
           to the chat input when it is empty and no user-visible output
           has appeared yet for the turn)
        9. Otherwise, a second Esc clears the chat input draft (undoable)
        """
        from textual.screen import ModalScreen

        clear_was_pending = getattr(self, "_clear_input_pending", False)
        self._clear_input_pending = False

        # 1. Dismiss modal screen if active
        try:
            screen = self.screen
        except Exception:
            screen = None
        if screen is not None and isinstance(screen, ModalScreen):
            cancel = getattr(screen, "action_cancel", None)
            if cancel is not None:
                cancel()
            else:
                screen.dismiss(None)
            return

        # 2 & 3. Close completion popup or exit slash/shell command mode
        if self._chat_input:
            if hasattr(self._chat_input, "dismiss_completion") and self._chat_input.dismiss_completion():
                return
            if hasattr(self._chat_input, "exit_mode") and self._chat_input.exit_mode():
                return

        # 3a. If `/btw` overlay card is open, dismiss/cancel it without interrupting main agent
        if self.is_btw_open():
            self.dismiss_btw_response()
            return

        # 4. If shell command is running, cancel the worker
        if self._shell_running and self._shell_worker:
            self._cancel_worker(self._shell_worker)
            return

        # 5 & 6. If approval or ask_user prompt is active, reject/cancel it
        if self._prompts.interrupt_active_prompt():
            return

        # 7. If queued messages exist, pop the last one (LIFO)
        if self._pending_messages:
            self._pop_last_queued_message()
            return

        # 8. If agent is running, interrupt it and discard queued messages
        if self._agent_running and self._agent_worker:
            if self._active_user_message is not None:
                if hasattr(self._active_user_message, "set_cancelled"):
                    self._active_user_message.set_cancelled()
                self._restore_interrupted_message_to_input(self._active_user_message)
            if self._adapter:
                self._adapter.cancel(thread_id=self._agent_thread_id)
            self._cancel_worker(self._agent_worker)
            try:
                self.query_one("#status-bar", StatusBar).set_status("Interrupted")
            except Exception:
                pass
            return

        # 9. Double-Esc clears chat input
        self._clear_input_pending = clear_was_pending
        self._handle_clear_input_escape()

    def action_quit_or_interrupt(self) -> None:
        """Handle Ctrl+C: interrupt running work or quit on second press."""
        if self._shell_running and self._shell_worker:
            self._cancel_worker(self._shell_worker)
            self._quit_pending = False
            return

        if self._prompts.interrupt_active_prompt():
            self._quit_pending = False
            return

        if self._agent_running and self._agent_worker:
            if self._active_user_message is not None:
                if hasattr(self._active_user_message, "set_cancelled"):
                    self._active_user_message.set_cancelled()
            if self._adapter:
                self._adapter.cancel(thread_id=self._agent_thread_id)
            self._cancel_worker(self._agent_worker)
            try:
                self.query_one("#status-bar", StatusBar).set_status("Interrupted")
            except Exception:
                pass
            self._quit_pending = False
            return

        if getattr(self, "_quit_pending", False):
            self.exit()
            return

        self._quit_pending = True
        self.notify("Press Ctrl+C again to quit", timeout=2)
        self.set_timer(2.0, lambda: setattr(self, "_quit_pending", False))

    def action_clear_chat(self) -> None:
        """Clear chat and start fresh (Ctrl+L)."""
        self.clear_active_btw()
        self._pending_messages.clear()
        self._queued_widgets.clear()
        self._reset_ephemeral_mode_states()
        self._restore_goal_rubric_state({})
        self._reset_thread_usage(0.0, 0)
        try:
            messages = self.query_one("#messages", MessageList)
            messages.clear()
        except NoMatches:
            pass

    async def action_toggle_auto_approve(self) -> None:
        await _modals_ctrl(self).action_toggle_auto_approve()

    async def _write_live_approval_mode(self, mode: ApprovalMode | None = None) -> bool:
        return await _modals_ctrl(self).write_live_approval_mode(mode)

    async def _set_approval_mode(self, target: ApprovalMode) -> bool:
        return await _modals_ctrl(self).set_approval_mode(target)

    def _resolve_default_model_spec(self) -> str | None:
        return _modals_ctrl(self).resolve_default_model_spec()

    async def _restore_manual_default_model(self) -> bool:
        return await _modals_ctrl(self).restore_manual_default_model()

    def _notify_auto_mode_enabled_once(self) -> None:
        _modals_ctrl(self).notify_auto_mode_enabled_once()

    def action_toggle_tool_output(self) -> None:
        _modals_ctrl(self).action_toggle_tool_output()

    def action_toggle_debug_console(self) -> None:
        _modals_ctrl(self).action_toggle_debug_console()

    async def action_open_editor(self) -> None:
        await _modals_ctrl(self).action_open_editor()

    async def _open_text_area_in_editor(
        self,
        text_area: Any,
        current_text: str,
        *,
        allow_empty: bool,
        raise_editor_errors: bool,
        restore_focus: Callable[[], object],
        reset_after_edit: Callable[[], None] | None = None,
    ) -> None:
        await _modals_ctrl(self).open_text_area_in_editor(
            text_area,
            current_text,
            allow_empty=allow_empty,
            raise_editor_errors=raise_editor_errors,
            restore_focus=restore_focus,
            reset_after_edit=reset_after_edit,
        )

    def action_open_notifications(self) -> None:
        _modals_ctrl(self).action_open_notifications()

    def _open_notification_center(self) -> None:
        _modals_ctrl(self).open_notification_center()

    def _buffer_shell_for_model_context(
        self, command: str, output: str, returncode: int | None
    ) -> None:
        _shell_ctrl(self).buffer_shell_for_model_context(command, output, returncode)

    async def _flush_pending_shell_messages(self) -> None:
        await _shell_ctrl(self).flush_pending_shell_messages()

    def _open_debug_console(self) -> None:
        _modals_ctrl(self).open_debug_console()

    def _persist_debug_console_click_to_copy(self, enabled: bool) -> None:
        _modals_ctrl(self).persist_debug_console_click_to_copy(enabled)

    def _build_debug_snapshot(self) -> list[Any]:
        return _modals_ctrl(self).build_debug_snapshot()

    # ── Theme, Thread & Cloud Selector Integration ───────

    async def _show_theme_selector(self) -> None:
        await _modals_ctrl(self).show_theme_selector()

    async def _switch_theme(self, name: str) -> None:
        await _modals_ctrl(self).switch_theme(name)

    async def _show_thread_selector(self) -> None:
        await _threads_ctrl(self).show_thread_selector()

    async def get_recent_threads(self, limit: int = 20) -> list:
        return await _threads_ctrl(self).get_recent_threads(limit=limit)

    def _sync_status_cloud(self) -> None:
        _modals_ctrl(self).sync_status_cloud()

    async def _show_cloud_selector(self) -> None:
        await _modals_ctrl(self).show_cloud_selector()

    async def _switch_cloud_profile(self, profile: str, region: str | None = None) -> None:
        await _modals_ctrl(self).switch_cloud_profile(profile, region=region)

    # ── Screen Stack Safety ───────────────────────────────

    def pop_screen(self) -> Any:
        """Pop current screen, guarding against ScreenStackError if only default screen remains."""
        screen_stack = getattr(self, "_screen_stack", None)
        if screen_stack is not None and len(screen_stack) <= 1:
            logger.warning(
                "Attempted to pop screen when only default screen remains on stack (%r)",
                screen_stack,
            )
            from textual.await_complete import AwaitComplete

            async def _noop() -> None:
                pass

            return AwaitComplete(_noop()).call_next(self)
        return super().pop_screen()

    # ── Model, Effort, Pool, Agent, Plugin & Skill Selectors (ModalController) ──

    async def _show_model_selector(self) -> None:
        await _modals_ctrl(self).show_model_selector()

    async def _install_extra_then_switch(self, extra: str, spec: str, effort: str | None = None) -> None:
        await _modals_ctrl(self).install_extra_then_switch(extra, spec, effort=effort)

    async def _prompt_model_auth_if_needed(self, spec: str) -> bool:
        return await _modals_ctrl(self).prompt_model_auth_if_needed(spec)

    async def _switch_model(self, spec: str, effort: str | None = None) -> None:
        await _modals_ctrl(self).switch_model(spec, effort=effort)

    async def _finalize_model_switch(self, spec: str, effort: str | None = None) -> None:
        await _modals_ctrl(self).finalize_model_switch(spec, effort=effort)

    async def _show_effort_selector(self) -> None:
        await _modals_ctrl(self).show_effort_selector()

    async def _show_pool_selector(self) -> None:
        await _modals_ctrl(self).show_pool_selector()

    async def _set_effort_override(self, effort: str) -> None:
        await _modals_ctrl(self).set_effort_override(effort)

    async def _show_agent_selector(self) -> None:
        await _modals_ctrl(self).show_agent_selector()

    async def _activate_selected_agent(self, agent_name: str, prompt: str = "") -> None:
        await _modals_ctrl(self).activate_selected_agent(agent_name, prompt=prompt)

    def _switch_agent(self, agent_name: str) -> None:
        _modals_ctrl(self).switch_agent(agent_name)

    def _snapshot_plugin_state(self) -> tuple[frozenset[str], dict[str, Any]]:
        return _modals_ctrl(self).snapshot_plugin_state()

    async def _check_plugin_manager_changes(
        self,
        baseline_enabled: frozenset[str],
        baseline_meta: dict[str, Any],
    ) -> bool | None:
        return await _modals_ctrl(self).check_plugin_manager_changes(baseline_enabled, baseline_meta)

    async def _show_plugin_manager(self) -> None:
        await _modals_ctrl(self).show_plugin_manager()

    def _open_skills_viewer(self) -> None:
        _modals_ctrl(self).open_skills_viewer()

    # ── Goal Review & Worker Focus ───────────────────────

    def _focus_chat_input_after_refresh(self) -> None:
        self._prompts.focus_chat_input_after_refresh()

    @on(Worker.StateChanged)
    def _on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        from textual.worker import WorkerState

        worker = event.worker
        if worker is self._agent_worker and event.state in {
            WorkerState.SUCCESS,
            WorkerState.CANCELLED,
            WorkerState.ERROR,
        }:
            self._agent_worker = None
            self._agent_running = False
            self._focus_chat_input_after_refresh()

    async def _cancel_pending_goal_review(self, *, context: str = "cleanup") -> None:
        await self._prompts.cancel_pending_goal_review(context=context)

    def _open_goal_review(self, objective: str, rubric: str, amendment: bool = False) -> None:
        self._prompts.open_goal_review(objective, rubric, amendment=amendment)

    @on(GoalReviewMenu.Decided)
    async def _on_goal_review_decided(self, event: GoalReviewMenu.Decided) -> None:
        await self._prompts.on_goal_review_decided(event)

    def _handle_rubric_evaluation_end(self, rubric_msg: dict[str, Any]) -> None:
        _goals_ctrl(self).handle_rubric_evaluation_end(rubric_msg)

    # ── Agent Persona Session (/agent) & Skill Command ───

    def _reset_ephemeral_mode_states(self) -> None:
        _modals_ctrl(self).reset_ephemeral_mode_states()

    async def _sync_agent_session_state_from_checkpoint(self) -> None:
        await _modals_ctrl(self).sync_agent_session_state_from_checkpoint()

    async def _run_agent_session_request(self, request: dict[str, Any]) -> None:
        await _modals_ctrl(self).run_agent_session_request(request)

    async def _maybe_continue_agent_session(self, message: str) -> bool:
        return await _modals_ctrl(self).maybe_continue_agent_session(message)

    async def _handle_skill_command(self, command: str) -> None:
        await _modals_ctrl(self).handle_skill_command(command)

    async def _invoke_skill(
        self,
        skill_name: str,
        args: str = "",
        *,
        command: str | None = None,
    ) -> None:
        await _modals_ctrl(self).invoke_skill(skill_name, args, command=command)

    # ── Exit ─────────────────────────────────────────────


    def exit(
        self,
        result: Any = None,
        return_code: int = 0,
        message: Any = None,
    ) -> None:
        """Exit the app after cleaning up background resources."""
        self._exit = True
        self._pending_messages.clear()
        self._queued_widgets.clear()
        # Cancel any active restart tasks so they cannot spawn servers post-exit (IA-07)
        for task in list(self._server_restart_tasks):
            if not task.done():
                task.cancel()
        # Cancel background workers (including server-startup)
        try:
            self.workers.cancel_all()
        except Exception:
            pass
        if self._adapter:
            self._adapter.cancel()
        if self._server_proc is not None:
            try:
                self._server_proc.stop()
            except Exception:
                logger.debug("Failed to stop server process during exit", exc_info=True)
        # Return thread_id so the CLI can print a resume hint
        if result is None:
            result = self._agent_thread_id
        super().exit(result=result, return_code=return_code, message=message)

    async def on_unmount(self) -> None:
        """Asynchronous shutdown coordinator ensuring lifecycle tasks and processes are drained (DV-05, DV-10)."""
        self._exit = True
        # 1. Drain and await restart tasks
        pending_restart = [t for t in list(self._server_restart_tasks) if not t.done()]
        for t in pending_restart:
            t.cancel()
        if pending_restart:
            await asyncio.gather(*pending_restart, return_exceptions=True)
        self._server_restart_tasks.clear()

        # 2. Drain adapter maintenance and cancel in-flight stream
        if self._adapter:
            try:
                self._adapter.cancel()
                await self._adapter.drain_maintenance(timeout=1.5)
            except Exception:
                pass

        # 3. Explicitly close remote client connection pool (DV-10)
        if self._client and hasattr(self._client, "aclose"):
            try:
                await self._client.aclose()
            except Exception:
                pass

        # 4. Stop server process
        if self._server_proc is not None:
            try:
                self._server_proc.stop()
            except Exception:
                pass

    def action_quit_app(self) -> None:
        self.exit()
