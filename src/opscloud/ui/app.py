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
from opscloud.ui.widgets.chat_input import ChatInput, InputMode
from opscloud.ui.command_registry import (
    ALWAYS_IMMEDIATE,
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


# ── MCP Reload / Respawn Types and Helpers ──────────────

_McpRefreshStatus = Literal["fresh", "disabled", "unavailable"]


@dataclass(frozen=True)
class _ServerRespawnResult:
    """Result of restarting the app-owned server."""

    restarted: bool
    mcp_server_info: list[MCPServerInfo] | None = None
    mcp_status: _McpRefreshStatus = "unavailable"
    mcp_error: str | None = None


def _format_mcp_server_changes(
    previous: list[MCPServerInfo] | None,
    current: list[MCPServerInfo] | None,
    error: str | None = None,
) -> str:
    """Format MCP server changes for the /reload report."""
    from opscloud.config.settings import get_glyphs

    glyphs = get_glyphs()
    if current is None:
        detail = f" ({error})" if error else ""
        return f"MCP server changes couldn't be determined{detail}; use /mcp to check."

    servers = {s.name: s for s in current if s.transport != "config"}
    config_errors = sorted(s.name for s in current if s.transport == "config")

    if previous is None:
        usable = sorted(name for name, s in servers.items() if s.status == "ok")
        unusable = sorted(
            f"{name} ({s.status})" for name, s in servers.items() if s.status != "ok"
        )
        parts = [f"currently loaded: {', '.join(usable) or 'none'}"]
        if unusable:
            parts.append(f"unavailable: {', '.join(unusable)}")
        if config_errors:
            parts.append(f"config errors: {', '.join(config_errors)}")
        return (
            "MCP server changes couldn't be determined (no baseline metadata); "
            f"{'; '.join(parts)}."
        )

    before = {s.name: s for s in previous if s.transport != "config"}
    before_config_errors = {s.name for s in previous if s.transport == "config"}

    loaded = sorted(
        name for name in servers.keys() - before.keys() if servers[name].status == "ok"
    )
    removed = sorted(before.keys() - servers.keys())
    failing = sorted(
        name
        for name, server in servers.items()
        if server.status == "error"
        and (name not in before or before[name].status != "error")
    )
    unavailable = sorted(
        name
        for name, server in servers.items()
        if name not in before and server.status not in {"ok", "error"}
    )
    recovered = sorted(
        name
        for name, server in servers.items()
        if server.status == "ok" and before.get(name, server).status != "ok"
    )
    status_changed = sorted(
        name
        for name, server in servers.items()
        if (
            name in before
            and before[name].status != server.status
            and name not in failing
            and name not in recovered
        )
    )
    reconfigured = sorted(
        name
        for name, server in servers.items()
        if name in before
        and before[name].status == server.status
        and (
            before[name].transport != server.transport
            or len(before[name].tools) != len(server.tools)
        )
    )
    new_config_errors = [
        name for name in config_errors if name not in before_config_errors
    ]
    resolved_config_errors = sorted(before_config_errors - set(config_errors))

    reported = (
        set(loaded)
        | set(failing)
        | set(unavailable)
        | set(recovered)
        | set(status_changed)
        | set(reconfigured)
    )
    stuck = sorted(
        [
            name
            for name, server in servers.items()
            if name not in reported
            and (server.status == "error" or server.needs_attention())
        ]
        + list(set(config_errors) & before_config_errors),
    )

    if not (
        loaded
        or removed
        or failing
        or unavailable
        or recovered
        or status_changed
        or reconfigured
        or new_config_errors
        or resolved_config_errors
    ):
        if stuck:
            return (
                "MCP server changes: no changes detected "
                f"(still needs attention: {', '.join(stuck)}; use /mcp)."
            )
        return "MCP server changes: no changes detected."

    lines = ["MCP server changes:"]
    if loaded:
        lines.append(f"  - Loaded: {', '.join(loaded)}")
    if removed:
        lines.append(f"  - Removed: {', '.join(removed)}")
    if failing:
        lines.append(f"  - Failed to load: {', '.join(failing)} (use /mcp for details)")
    if unavailable:
        statuses = ", ".join(f"{name} ({servers[name].status})" for name in unavailable)
        lines.append(f"  - Unavailable: {statuses} (use /mcp for details)")
    if recovered:
        lines.append(f"  - Recovered: {', '.join(recovered)}")
    if status_changed:
        transitions = ", ".join(
            f"{name} ({before[name].status} {glyphs.arrow_right} {servers[name].status})"
            for name in status_changed
        )
        lines.append(f"  - Status changed: {transitions}")
    if reconfigured:
        edits = []
        for name in reconfigured:
            was, now = before[name], servers[name]
            if was.transport != now.transport:
                edits.append(
                    f"{name} ({was.transport} {glyphs.arrow_right} {now.transport})"
                )
            else:
                edits.append(
                    f"{name} ({len(was.tools)} {glyphs.arrow_right} {len(now.tools)} tools)"
                )
        lines.append(f"  - Reconfigured: {', '.join(edits)}")
    if new_config_errors:
        lines.append(f"  - Config errors: {', '.join(new_config_errors)}")
    if resolved_config_errors:
        lines.append(f"  - Resolved config errors: {', '.join(resolved_config_errors)}")
    return "\n".join(lines)


# ── Queued Message ───────────────────────────────────────

@dataclass
class QueuedMessage:
    """A message queued for processing when the app is busy."""

    text: str
    mode: InputMode = "normal"


# ── App ──────────────────────────────────────────────────


class OpsCloudApp(App):
    """OpsCloud interactive TUI — DevOps coding agent orchestrator."""

    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "interrupt", "Cancel", show=False),
        Binding("ctrl+d", "quit_app", "Quit", show=False),
        Binding("ctrl+l", "clear_chat", "Clear", show=False),
        Binding("shift+tab", "toggle_auto_approve", "Auto-approve", show=False),
        Binding("ctrl+\\", "toggle_debug_console", "Debug", show=False),
        Binding("ctrl+x", "open_editor", "External Editor", show=False),
        Binding("ctrl+n", "open_notifications", "Notifications", show=False),
        Binding("ctrl+o", "toggle_tool_output", "Toggle Output", show=False, priority=True),
        # Approval menu keys (handled at App level for reliability)
        Binding("up", "approval_up", "Up", show=False),
        Binding("k", "approval_up", "Up", show=False),
        Binding("down", "approval_down", "Down", show=False),
        Binding("j", "approval_down", "Down", show=False),
        Binding("enter", "approval_select", "Select", show=False),
        Binding("y", "approval_yes", "Yes", show=False),
        Binding("1", "approval_position(0)", "Select first", show=False),
        Binding("2", "approval_position(1)", "Select second", show=False),
        Binding("3", "approval_position(2)", "Select third", show=False),
        Binding("s", "approval_smart", "Smart", show=False),
        Binding("a", "approval_smart", "Auto", show=False),
        Binding("n", "approval_no", "No", show=False),
        Binding("escape", "approval_escape", "Esc", show=False),
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
        self._server_startup_deferred = defer_server_start and (client is None)
        from opscloud.config.settings import settings
        self._settings = settings
        self._reasoning_effort: str | None = getattr(settings, "reasoning_effort", None)

        # Sync model metadata to settings early so /config can display it
        # before the agent is created (apply_to_settings only runs on first turn)
        if model and not settings.model_name:
            if ":" in model:
                provider_part, model_part = model.split(":", 1)
                settings.model_provider = provider_part
                settings.model_name = model_part
            else:
                settings.model_name = model

        from opscloud.approval_mode import ApprovalMode
        if approval_mode is not None:
            if isinstance(approval_mode, str):
                try:
                    self._approval_mode = ApprovalMode.from_str(approval_mode)
                except Exception:
                    self._approval_mode = ApprovalMode.MANUAL
            else:
                self._approval_mode = approval_mode
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
        self._cwd: str = str(Path.cwd())
        self._status_bar: StatusBar | None = None
        self._git_branch_refresh_task: asyncio.Task[None] | None = None
        self._session_cost_usd: float = 0.0
        self._context_tokens: int = 0
        self._cumulative_session_tokens: int = 0

        # ── State Flags ──────────────────────────────────
        self._agent_running = False
        self._shell_running = False
        self._connecting = server_kwargs is not None and client is None
        self._startup_sequence_running = False
        self._server_startup_error: Exception | None = None
        self._exit = False
        
        self._debug_console_cleared_upto = -1
        self._debug_console_click_to_copy = False

        # ── Goal State Synchronization ────────────────────
        self._goal_state_lock = asyncio.Lock()
        self._goal_state_mutating = False
        self._agent_reconciling = False
        self._pending_goal_review_widget: Any | None = None
        self._pending_goal_review_future: Any | None = None
        self._pending_approval_widget: Any | None = None
        self._pending_ask_user_widget: AskUserMenu | None = None
        self._approval_placeholder: Any | None = None

        # ── Worker Handles ───────────────────────────────
        self._agent_worker: Worker[None] | None = None
        self._shell_worker: Any | None = None
        self._shell_process: asyncio.subprocess.Process | None = None

        # ── Queue ────────────────────────────────────────
        self._pending_messages: deque[QueuedMessage] = deque()
        self._queued_widgets: deque[QueuedUserMessage] = deque()
        self._pending_shell_messages: list[Any] = []
        self._processing_pending = False

        # ── Adapter & Widgets ────────────────────────────
        self._adapter: TextualAdapter | None = None
        self._chat_input: ChatInput | None = None
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
        from opscloud.ui.widgets.goal_status import GoalStatusPanel
        from opscloud.ui.widgets.subagent_panel import SubagentPanel
        yield MessageList(id="messages")
        with Container(id="bottom-container"):
            yield SubagentPanel(id="subagent-panel")
            yield GoalStatusPanel(id="goal-status-panel")
            yield ChatInput(id="input-area")
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
        )

        self._chat_input = self.query_one("#input-area", ChatInput)
        self._chat_input.focus()

        messages_widget.mount(WelcomeBanner())
        self._sync_status_cloud()

        # Always refocus input after mounting widgets that may steal focus
        self._chat_input.focus()

        # Discover skills in background to populate autocomplete
        self.run_worker(self._discover_startup_skills(), group="skill-discovery")

        if self._server_startup_deferred:
            status_bar.set_status("No model configured")
            await self._mount_message(
                SystemMessage(
                    "No model is configured yet. Run `/model` to choose one.\n\n"
                    "OpsCloud will ask for credentials for the selected provider."
                )
            )
        elif self._connecting:
            # Deferred server startup — TUI is visible immediately
            status_bar.set_status("Connecting...")
            self._startup_sequence_running = True
            self.run_worker(self._start_server_background(), group="server-startup")
        elif self._client:
            # Eagerly-provided client (e.g. tests or pre-started server)
            if self._adapter:
                self._adapter.set_client(self._client)
            await self._finalize_connection(self._client)
            try:
                banner = self.query_one(WelcomeBanner)
                banner.set_connected()
            except Exception:
                pass
        else:
            # No server at all — offline/widget-only mode
            self._agent_thread_id = "local_session"
            await self._mount_message(
                SystemMessage(
                    "🚀 **OpsCloud TUI Ready** (offline) — "
                    "Type `/help` for slash commands."
                )
            )

        if self._goal:
            self.call_later(self._send_goal)

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
            self._loading_widget = LoadingWidget(status)
            await messages.mount(self._loading_widget)
            messages.scroll_end(animate=False)
        else:
            if hasattr(self._loading_widget, "resume"):
                self._loading_widget.resume()
            if hasattr(self._loading_widget, "set_status"):
                self._loading_widget.set_status(status)
            messages.scroll_end(animate=False)

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
        """Mount a message widget in the message list.

        Centralizes mount logic so scroll anchoring and error handling
        are in one place.

        Args:
            widget: Message widget to mount.
        """
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
            messages.scroll_end(animate=False)
        except Exception:
            logger.debug("Could not mount message (app closing?)", exc_info=True)

    # ── Deferred Server Startup ──────────────────────────

    async def maybe_start_deferred_server(self) -> bool:
        """Start the background server if startup was deferred due to missing credentials."""
        if not self._server_startup_deferred:
            return False

        from opscloud.exceptions import ModelConfigError, NoCredentialsConfiguredError
        from opscloud.model.factory import _get_default_model_spec

        try:
            model_spec = _get_default_model_spec()
        except (NoCredentialsConfiguredError, ModelConfigError):
            return False

        self._server_startup_deferred = False
        self._model = model_spec
        self._connecting = True
        self._server_kwargs = {
            "assistant_id": self._assistant_id,
            "model_name": model_spec,
            "auto_approve": self._auto_approve,
        }

        try:
            status_bar = self.query_one(StatusBar)
            status_bar.set_status("Connecting...")
        except Exception:
            pass

        await self._mount_message(
            SystemMessage(
                f"⏳ **Credentials configured!** Starting agent server for model `{model_spec}`..."
            )
        )
        self.run_worker(self._start_server_background(), group="server-startup")
        return True

    async def _start_server_background(self) -> None:
        """Background worker: start the LangGraph server subprocess and preload MCP metadata.

        On success → posts ``ServerReady(client, server_proc, mcp_server_info)``
        On failure → posts ``ServerStartFailed(error)``
        """
        from opscloud.cli.server_manager import start_server_and_get_agent

        try:
            kwargs: dict[str, Any] = dict(self._server_kwargs or {})
            assistant_id = str(kwargs.pop("assistant_id", self._assistant_id))

            coros: list[Any] = [
                start_server_and_get_agent(
                    assistant_id=assistant_id,
                    **kwargs,
                )
            ]

            if self._mcp_preload_kwargs is not None and not self._mcp_preload_kwargs.get("no_mcp"):
                from opscloud.mcp.preload import preload_mcp_server_info

                coros.append(preload_mcp_server_info(**self._mcp_preload_kwargs))

            results = await asyncio.gather(*coros, return_exceptions=True)

            server_result = results[0]
            if isinstance(server_result, BaseException):
                raise server_result

            client, server_proc = server_result
            self._server_proc = server_proc

            mcp_info = None
            if len(results) > 1:
                mcp_res = results[1]
                if isinstance(mcp_res, list):
                    mcp_info = mcp_res
                elif isinstance(mcp_res, BaseException):
                    logger.warning("MCP metadata preload background failed: %s", mcp_res)

            self.post_message(
                self.ServerReady(
                    client=client,
                    server_proc=server_proc,
                    mcp_server_info=mcp_info,
                )
            )
        except Exception as exc:
            logger.exception("Server startup failed: %s", exc)
            self.post_message(self.ServerStartFailed(error=exc))

    @on(ServerReady)
    async def _on_server_ready(self, event: ServerReady) -> None:
        """Handle successful background server startup."""
        self._client = event.client
        self._server_proc = event.server_proc
        if event.mcp_server_info is not None:
            self._mcp_server_info = event.mcp_server_info

        # Bind the now-live client to the adapter
        if self._adapter:
            self._adapter.set_client(event.client)

        await self._finalize_connection(event.client)
        self._connecting = False
        self._startup_sequence_running = False

        logger.debug("TUI: server ready, client bound to adapter")

        # Transition the welcome banner status indicator to green.
        try:
            banner = self.query_one(WelcomeBanner)
            banner.set_connected()
        except Exception:
            pass  # Banner may have been dismissed already

        # Restore focus to input after async server startup
        if self._chat_input:
            self._chat_input.focus()

        # Drain any messages queued during connection
        await self._process_next_from_queue()

    @on(ServerStartFailed)
    async def _on_server_start_failed(self, event: ServerStartFailed) -> None:
        """Handle server startup failure — show error and allow recovery."""
        self._connecting = False
        self._startup_sequence_running = False
        self._server_startup_error = event.error

        # Clear any queued messages
        self._pending_messages.clear()
        while self._queued_widgets:
            widget = self._queued_widgets.popleft()
            try:
                await widget.remove()
            except Exception:
                pass

        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.set_status("Server Error")
        except NoMatches:
            pass

        try:
            banner = self.query_one(WelcomeBanner)
            banner.set_error()
        except Exception:
            pass

        await self._mount_message(
            ErrorMessage(
                f"Server startup failed: {event.error}\n\n"
                "Check your API key (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`) "
                "and relaunch, or use `/model` to reconfigure."
            )
        )
        logger.error("Server startup failed: %s", event.error)

        # Keep input focusable for slash commands even after failure
        if self._chat_input:
            self._chat_input.focus()

    async def _finalize_connection(self, client: Any) -> None:
        """Create thread and mark the app as ready for agent turns."""
        if self._resume_thread:
            self._agent_thread_id = self._resume_thread
            session_state = getattr(self, "_session_state", None)
            if session_state:
                session_state.thread_id = self._resume_thread
            await self._mount_message(
                SystemMessage(f"🔄 Resumed thread: `{self._resume_thread}`")
            )
            await self._load_thread_history(self._resume_thread)
        else:
            import uuid
            self._agent_thread_id = str(uuid.uuid4())

        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.set_status("Ready")
        except NoMatches:
            pass
        if self._agent_thread_id:
            bind_logging_to_thread(self._agent_thread_id)
        logger.debug("TUI: connection finalized, thread_id=%s", self._agent_thread_id)

        if self._startup_cmd:
            await self._mount_message(SystemMessage(f"⚡ Running startup command: `{self._startup_cmd}`"))
            await self._submit_input(self._startup_cmd, "shell")

        if self._initial_skill:
            await self._submit_input(f"/skill {self._initial_skill}", "normal")

        if self._initial_prompt:
            await self._submit_input(self._initial_prompt, "normal")

        if self._goal:
            await self._send_goal()

    async def _send_goal(self) -> None:
        """Submit the initial goal objective if present."""
        if self._goal:
            await self._submit_input(self._goal, "normal")

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

    # ── Agent Turn Lifecycle ─────────────────────────────

    async def _handle_user_message(self, message: str) -> None:
        """Handle a user message to send to the agent.

        Mounts the user message widget and dispatches to the agent
        via a single worker.

        Args:
            message: The user's message.
        """
        await self._mount_message(UserMessage(message))
        await self._send_to_agent(message)

    async def send_agent_message(self, message: str, **kwargs: Any) -> None:
        """Send a message to the agent and start execution.

        Public interface for skill invocation and command handlers.
        """
        await self._send_to_agent(message)

    async def _send_to_agent(self, message: str) -> None:
        """Send a message to the agent and start execution.

        This is the low-level send path.  The caller mounts the visual
        representation first (``UserMessage``, ``SkillMessage``, etc.).

        Args:
            message: The prompt to send to the agent.
        """
        # Anchor scroll so streaming response stays visible
        with suppress(NoMatches):
            self.query_one("#messages", MessageList).scroll_end(animate=False)

        # Check if agent is available
        if self._adapter and self._adapter.connected and self._agent_thread_id:
            await self._flush_pending_shell_messages()
            self._agent_running = True
            await self._set_spinner("Thinking")

            if self._chat_input:
                # Visual hint that input is busy
                pass

            # Spawn a single worker for the agent task
            self._agent_worker = self.run_worker(
                self._run_agent_task(message),
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
    ) -> None:
        """Run the agent task in a background worker.

        This runs in a Textual worker so the main event loop stays responsive.
        ``_cleanup_agent_task`` always runs in the ``finally`` block.

        Args:
            message: The prompt to send to the agent.
            graph_input: Prepared non-conversation input for a server operation
                (e.g. goal criteria generation).  When set, ``message`` is
                ignored by ``stream_turn``.
        """
        if self._adapter is None:
            return

        # Track criteria request correlation for cleanup (reference: app.py L15685-L15691).
        criteria_request_id: str | None = None
        if graph_input is not None:
            criteria_request = graph_input.get("goal_criteria_request")
            if isinstance(criteria_request, dict):
                raw_request_id = criteria_request.get("request_id")
                if isinstance(raw_request_id, str):
                    criteria_request_id = raw_request_id

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

            await self._adapter.stream_turn(
                prompt=message,
                thread_id=self._agent_thread_id or "local_session",
                context=context if context else None,
                graph_input=graph_input,
            )
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
            await self._cleanup_agent_task(
                force_goal_sync=graph_input is not None,
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

            # Process next message from queue
            if not self._startup_sequence_running:
                await self._process_next_from_queue()
        finally:
            self._agent_reconciling = False
            self._focus_chat_input_after_refresh()

    # ── Goal State Management ────────────────────────────
    #
    # Goal infrastructure: mutation boundary, checkpoint persistence, state
    # sync from thread, criteria request submission, and accept flow.

    @asynccontextmanager
    async def _goal_state_mutation_boundary(self) -> AsyncGenerator[None, None]:
        """Serialize an out-of-run goal mutation with graph checkpoints."""
        async with self._goal_state_lock:
            self._goal_state_mutating = True
            try:
                # Wait for agent quiescence.
                while self._agent_running or self._agent_reconciling:
                    await asyncio.sleep(0.05)
                yield
            finally:
                self._goal_state_mutating = False

    async def _aupdate_thread_state(self, update: dict[str, Any]) -> None:
        """Write one state update through the graph client.

        """
        if not self._adapter or not self._adapter.connected or not self._agent_thread_id:
            return
        config: dict[str, Any] = {"configurable": {"thread_id": self._agent_thread_id}}
        client = self._adapter._client

        if hasattr(client, "aensure_thread"):
            await client.aensure_thread(config)
            await client.aupdate_state(config, update, as_node="model")
        else:
            await client.aupdate_state(config, update)

    def _goal_state_update(self) -> dict[str, Any]:
        """Build checkpoint state for goal/rubric metadata managed by the TUI.

        """
        from opscloud.commands.power.goal import get_goal_state

        state = get_goal_state(self)
        return {
            "rubric": (
                None
                if state.objective and state.status in {"paused", "complete"}
                else state.rubric
            ),
            "_sticky_rubric": state.rubric,
            "_goal_objective": state.objective,
            "_goal_status": state.status if state.objective else None,
            "_goal_rubric": state.rubric if state.objective else None,
            "_goal_status_note": state.status_note if state.objective else None,
            "_pending_goal_objective": state.pending_objective,
            "_pending_goal_rubric": state.pending_rubric,
            "_pending_goal_kind": (
                state.pending_kind if state.pending_objective else None
            ),
        }

    async def _persist_goal_rubric_state(
        self,
        *,
        notice: Any | None = None,
        state_update: dict[str, Any] | None = None,
    ) -> bool:
        """Persist TUI-owned goal state and an optional notice atomically.


        Returns:
            ``True`` when the state was written or there is no thread to write
            to yet; ``False`` when a write was attempted and failed.
        """
        if not self._adapter or not self._adapter.connected or not self._agent_thread_id:
            return True
        update = dict(state_update or self._goal_state_update())
        if notice is not None:
            if hasattr(notice, "content") and hasattr(notice, "type"):
                update["messages"] = [{"role": getattr(notice, "type", "system"), "content": str(notice.content)}]
            elif isinstance(notice, dict):
                update["messages"] = [notice]
            else:
                update["messages"] = [str(notice)]
        try:
            await self._aupdate_thread_state(update)
        except Exception:
            logger.warning("Failed to persist goal/rubric state", exc_info=True)
            self.notify(
                "Could not persist goal/rubric state for this thread.",
                severity="warning",
                markup=False,
            )
            return False
        return True

    async def _get_thread_state_values(self, thread_id: str | None = None) -> dict[str, Any]:
        """Fetch thread state values from the checkpoint.

        In server mode the LangGraph dev server starts with an empty in-memory
        thread store, so ``aget_state`` returns empty state for any thread that
        was not registered in the current server session.  Calling
        ``aensure_thread`` first registers the thread idempotently so the
        subsequent ``aget_state`` call can read from the checkpointer correctly,
        including proper reconstruction of delta channels.

        When the remote path returns empty (server restarting, delta-channel
        messages not yet replayed), a **local checkpointer fallback** reads the
        same SQLite database and reconstructs messages by replaying the writes
        table through LangChain's ``add_messages`` reducer. The fallback
        filters ``checkpoint_ns = ''`` (parent graph only) so sub-graph messages never leak.

        Args:
            thread_id: Explicit thread ID to fetch.  Defaults to the current
                active thread (``self._agent_thread_id``).
        """
        tid = thread_id or self._agent_thread_id
        if not tid:
            return {}

        config: dict[str, Any] = {"configurable": {"thread_id": tid}}

        # ── Path 1: Remote agent ──────────────────────────────────
        if self._adapter and self._adapter.connected:
            client = self._adapter._client
            if hasattr(client, "aensure_thread"):
                try:
                    await client.aensure_thread(config)
                except Exception:
                    logger.debug("aensure_thread failed for %s, proceeding anyway", tid)

            try:
                state = await client.aget_state(config)
                if state and state.values:
                    res = dict(state.values)
                    # Merge goal channels from local DB if missing or empty in remote state
                    if not res.get("_goal_objective"):
                        local_state = await self._reconstruct_state_from_local_db(tid)
                        for k, v in local_state.items():
                            if (k not in res or res[k] is None) and v is not None:
                                res[k] = v
                    return res
            except Exception:
                logger.debug(
                    "Remote aget_state failed for %s; trying local checkpointer",
                    tid,
                    exc_info=True,
                )

        # ── Path 2: Local checkpointer fallback ───────────────────
        # Reconstruct messages from the SQLite writes table through the
        # add_messages reducer (with checkpoint_ns='').
        return await self._reconstruct_state_from_local_db(tid)

    async def _reconstruct_state_from_local_db(
        self,
        thread_id: str,
    ) -> dict[str, Any]:
        """Reconstruct thread state from the local SQLite checkpoint database.

        This is the fallback when ``RemoteGraph.aget_state`` returns empty
        (e.g. after a server restart before the thread is fully rehydrated).

        The method reads message-channel writes from the ``writes`` table —
        filtered to ``checkpoint_ns = ''`` (parent graph only) — and replays them through
        LangChain's ``add_messages`` reducer.  This handles deduplication by
        message ID, ``RemoveMessage`` deletions, and ``Overwrite`` resets,
        producing the same result as ``Pregel.aget_state``'s
        ``DeltaChannel.replay_writes``.

        Returns:
            State-values dict with a ``messages`` key.  Empty dict when the
            database is missing or the thread has no writes.
        """
        import asyncio

        from opscloud.state.session import get_db_path

        db_path = get_db_path()
        if not db_path.exists():
            return {}

        try:
            import aiosqlite
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

            serde = JsonPlusSerializer()

            async with aiosqlite.connect(db_path) as conn:
                # Read all message-channel writes for the parent graph only.
                # Ordered by (checkpoint_id, task_id, idx) to replay oldest →
                # newest, matching LangGraph's application order.
                async with conn.execute(
                    """
                    SELECT type, value
                    FROM writes
                    WHERE thread_id = ?
                      AND checkpoint_ns = ''
                      AND channel = 'messages'
                    ORDER BY checkpoint_id ASC, task_id ASC, idx ASC
                    """,
                    (thread_id,),
                ) as cursor:
                    rows: list[tuple[str, bytes]] = [
                        (r[0], r[1]) for r in await cursor.fetchall()
                    ]

            if not rows:
                return {}

            # Decode + reduce in a worker thread (CPU-bound for large histories).
            loop = asyncio.get_running_loop()
            messages = await loop.run_in_executor(
                None, self._reduce_message_writes, rows, serde
            )
            if messages:
                return {"messages": messages}
        except Exception:
            logger.warning(
                "Local DB reconstruction failed for thread %s",
                thread_id,
                exc_info=True,
            )

        return {}

    @staticmethod
    def _reduce_message_writes(
        rows: list[tuple[str, bytes]],
        serde: Any,
    ) -> list[Any]:
        """Replay message-channel write rows through ``add_messages``.

        Runs synchronously in a worker thread.
        """
        from langgraph.graph.message import add_messages

        accumulated: list[Any] = []

        for type_str, value_blob in rows:
            if not type_str or not value_blob:
                continue
            try:
                delta = serde.loads_typed((type_str, value_blob))
            except Exception:
                logger.warning(
                    "Failed to decode a message write row; skipping",
                    exc_info=True,
                )
                continue

            # Each delta is either a single message or a list of messages.
            # add_messages handles dedup by ID, RemoveMessage, etc.
            # Cast to list — add_messages returns a Messages union type but
            # always produces a list at runtime.
            if isinstance(delta, list):
                accumulated = list(add_messages(accumulated, delta))
            else:
                accumulated = list(add_messages(accumulated, [delta]))

        return accumulated


    async def _sync_goal_state_from_checkpoint(
        self,
        *,
        force: bool = False,
        proposal_request_id: str | None = None,
        allow_pending_proposal: bool = True,
    ) -> bool:
        """Refresh goal/rubric metadata from the active checkpoint.

        grading correlation, one-shot rubric bookkeeping, or auto-accept).

        Args:
            force: Read the checkpoint even when no goal fields are set locally.
            proposal_request_id: When set, restore a pending proposal only if
                it originated from this criteria request.
            allow_pending_proposal: Whether a proposal from this turn may be
                restored.  Failed/cancelled criteria turns set ``False``.

        Returns:
            Whether state was successfully reconciled.
        """
        from opscloud.commands.power.goal import GoalHandler, get_goal_state
        from opscloud.ui.widgets.messages import AppMessage, ErrorMessage

        goal_state = get_goal_state(self)
        if not force and not (
            goal_state.objective
            or goal_state.rubric
            or goal_state.next_rubric
            or goal_state.status_note
            or goal_state.pending_objective
            or goal_state.pending_rubric
        ):
            return True

        try:
            state_values = await self._get_thread_state_values()
        except Exception:
            logger.warning("Failed to refresh goal/rubric state", exc_info=True)
            if force:
                # Criteria were generated but couldn't be loaded
                # (reference: app.py L11840-L11845).
                if goal_state.pending_objective and goal_state.pending_rubric:
                    self._open_goal_review(
                        goal_state.pending_objective,
                        goal_state.pending_rubric,
                    )
                else:
                    await self._mount_message(
                        ErrorMessage(
                            "Acceptance criteria were generated but could not be "
                            "loaded from the thread. Run `/goal` again to retry."
                        )
                    )
            return False

        if not state_values:
            return True

        # ── Extract checkpoint goal fields ────────────────
        pending_obj = None
        pending_rubric = None
        pending_kind = None
        pending_request_id = None

        raw_pending_obj = state_values.get("_pending_goal_objective")
        if isinstance(raw_pending_obj, str) and raw_pending_obj.strip():
            pending_obj = raw_pending_obj.strip()
        raw_pending_rubric = state_values.get("_pending_goal_rubric")
        if isinstance(raw_pending_rubric, str) and raw_pending_rubric.strip():
            pending_rubric = raw_pending_rubric.strip()
        raw_pending_kind = state_values.get("_pending_goal_kind")
        if isinstance(raw_pending_kind, str) and raw_pending_kind.strip():
            pending_kind = raw_pending_kind.strip()
        raw_pending_request_id = state_values.get("_pending_goal_request_id")
        if isinstance(raw_pending_request_id, str):
            pending_request_id = raw_pending_request_id

        # ── Active goal fields from checkpoint ───────────
        raw_obj = state_values.get("_goal_objective")
        if isinstance(raw_obj, str) and raw_obj.strip():
            goal_state.objective = raw_obj.strip()
        raw_status = state_values.get("_goal_status")
        if isinstance(raw_status, str) and raw_status in {
            "active", "blocked", "complete", "paused",
        }:
            goal_state.status = raw_status  # type: ignore[assignment]
        raw_note = state_values.get("_goal_status_note")
        if isinstance(raw_note, str) and raw_note.strip():
            goal_state.status_note = raw_note.strip()
        raw_rubric = state_values.get("_goal_rubric") or state_values.get("_sticky_rubric")
        if isinstance(raw_rubric, str) and raw_rubric.strip():
            goal_state.rubric = raw_rubric.strip()

        # ── Resolve goal completion from rubric evaluation ──
        rubric_status = state_values.get("_rubric_status")
        pending_completion_note = state_values.get("_pending_goal_completion_note")
        previous_status = raw_status or goal_state.status

        if (
            (rubric_status == "satisfied" or goal_state.status == "complete")
            and goal_state.objective
            and (raw_status == "active" or goal_state.status == "complete")
        ):
            note = pending_completion_note or goal_state.status_note or "Acceptance criteria satisfied."
            goal_state.status = "complete"
            goal_state.status_note = note
            goal_state.pending_objective = None
            goal_state.pending_rubric = None
            goal_state.pending_kind = None

            from opscloud.middleware.goal_state_notice import build_goal_state_notice
            state_update = self._goal_state_update()
            state_update["_pending_goal_completion_note"] = None
            state_update["_rubric_status"] = None
            persisted = await self._persist_goal_rubric_state(
                notice=build_goal_state_notice(state_update),
                state_update=state_update,
            )
            if persisted and previous_status != "complete":
                await self._mount_message(AppMessage(f"Goal marked complete.\n\n{note}"))
        elif rubric_status == "max_iterations_reached" and pending_completion_note:
            await self._mount_message(
                ErrorMessage(
                    "Goal completion was not recorded: the iteration limit was reached "
                    "with unmet criteria. The goal remains active for resume, "
                    "amendment, retry, or clearing."
                )
            )

        # ── Gate pending proposal on request correlation ──
        discard = False
        if not allow_pending_proposal and pending_obj is not None:
            if proposal_request_id is None or pending_request_id == proposal_request_id:
                discard = True
        if (
            proposal_request_id is not None
            and pending_request_id != proposal_request_id
        ):
            discard = True

        if discard:
            pending_obj = None
            pending_rubric = None
            pending_kind = None
            pending_request_id = None
            goal_state.pending_objective = None
            goal_state.pending_rubric = None
            goal_state.pending_kind = None
        else:
            goal_state.pending_objective = pending_obj
            goal_state.pending_rubric = pending_rubric
            goal_state.pending_kind = pending_kind

        GoalHandler._sync_status_rubric(self, goal_state)

        # ── Mount review if pending proposal exists ──────
        if pending_obj and pending_rubric:
            self._open_goal_review(pending_obj, pending_rubric)

        return True

    async def _sync_session_cost_from_checkpoint(self) -> None:
        """Reconcile displayed context tokens, cumulative cost, and session tokens from the checkpoint.

        The streamed totals already track spend during a turn; this settles the
        display at the end of one from the authoritative SQLite checkpointer.
        """
        if not self._agent_thread_id:
            return
        try:
            state_values = await self._get_thread_state_values(self._agent_thread_id)
        except Exception:
            logger.debug(
                "Could not load thread state while reconciling cost and token state",
                exc_info=True,
            )
            return

        cost_val = state_values.get("_session_cost_usd")
        if isinstance(cost_val, (int, float)) and math.isfinite(cost_val) and cost_val >= 0:
            self._session_cost_usd = float(cost_val)
            if self._status_bar:
                self._status_bar.set_cost(self._session_cost_usd)

        total_tokens = state_values.get("_session_total_tokens")
        if isinstance(total_tokens, int) and total_tokens >= 0:
            self._cumulative_session_tokens = total_tokens

        context_tokens = state_values.get("_context_tokens")
        if isinstance(context_tokens, int) and context_tokens >= 0:
            self._context_tokens = context_tokens
            if self._status_bar:
                display_tokens = self._cumulative_session_tokens or self._context_tokens
                if display_tokens > 0:
                    self._status_bar.set_tokens(display_tokens, context_tokens=self._context_tokens)

    async def _run_goal_criteria_request(
        self,
        request: dict[str, Any],
    ) -> None:
        """Submit one typed criteria request through the normal agent stream.

        """
        if not self._adapter or not self._adapter.connected:
            await self._mount_message(
                ErrorMessage(
                    "Goal criteria generation requires a connected server."
                )
            )
            return
        self._agent_running = True
        if self._chat_input:
            # Disable input while criteria run.
            pass
        self._agent_worker = self.run_worker(
            self._run_agent_task(
                "",
                graph_input={
                    "messages": [],
                    "goal_criteria_request": request,
                },
            ),
            exclusive=False,
        )

    async def _accept_goal_rubric(
        self,
        rubric: str,
    ) -> bool:
        """Apply accepted criteria: persist to checkpoint and start work.


        Returns:
            Whether the proposal was accepted.
        """
        from opscloud.commands.power.goal import GoalHandler, get_goal_state

        goal_state = get_goal_state(self)
        objective = goal_state.pending_objective
        if not objective:
            await self._mount_message(
                SystemMessage("No pending goal to accept.")
            )
            return False
        rubric = rubric.strip()
        if not rubric:
            await self._mount_message(
                SystemMessage("Cannot accept empty goal criteria.")
            )
            return False

        kind = goal_state.pending_kind or "create"
        is_amendment = kind == "amend"

        if is_amendment and (not goal_state.objective or goal_state.status == "complete"):
            await self._mount_message(
                SystemMessage(
                    "The goal is no longer active. Start a new goal with "
                    "`/goal <objective>`."
                )
            )
            return False

        # ── Persist accepted state ───────────────────────
        goal_state.objective = objective
        goal_state.rubric = rubric
        goal_state.next_rubric = None
        if not is_amendment or goal_state.status == "blocked":
            goal_state.status = "active"
            goal_state.status_note = None
        goal_state.pending_objective = None
        goal_state.pending_rubric = None
        goal_state.pending_kind = None
        GoalHandler._sync_status_rubric(self, goal_state)

        from langchain_core.messages import SystemMessage as LCSystemMessage

        notice = LCSystemMessage(content=f"Goal {'amended' if is_amendment else 'accepted'}.")
        persisted = await self._persist_goal_rubric_state(notice=notice)

        # ── User feedback message ────────────────────────
        if is_amendment:
            await self._mount_message(
                SystemMessage("Goal amended." + ("" if persisted else
                    " (could not be saved to thread)"))
            )
        else:
            await self._mount_message(
                SystemMessage(
                    "Goal accepted. It will stay active for this thread until paused, "
                    "completed, blocked, or cleared.\nUse /goal show to inspect it or "
                    "/goal clear to remove it."
                )
            )
            if not persisted:
                await self._mount_message(
                    ErrorMessage(
                        "Goal accepted for this session, but it could not be saved "
                        "to the thread."
                    )
                )

        # ── Continuation: start working toward the goal ──
        if goal_state.status == "paused":
            return True
        if not self._agent_running:
            await self._continue_goal_work(
                "amended" if is_amendment else "created",
                objective=objective,
                persisted=persisted,
            )
        return True

    async def _continue_goal_work(
        self,
        transition: str,
        *,
        objective: str | None = None,
        persisted: bool = True,
    ) -> None:
        """Send one hidden command that resumes work after a goal transition.

        """
        from opscloud.middleware.goal_state_notice import build_goal_continuation

        continuation = build_goal_continuation(
            transition,  # type: ignore[arg-type]
            unsaved_objective=None if persisted else objective,
        )
        await self._send_to_agent(cast("str", continuation.content))

    # ── Shell Command Execution ──────────────────────────

    async def _handle_shell_command(
        self,
        command: str,
        *,
        incognito: bool = False,
    ) -> None:
        """Handle a shell command (``!`` prefix).

        Mounts the user message and spawns a worker so the event loop
        stays free for key events (Esc/Ctrl+C).

        Args:
            command: The shell command to execute.
            incognito: Whether command/output should remain local-only.
        """
        if not incognito:
            await self._mount_message(UserMessage(f"!{command}"))
        self._shell_running = True

        self._shell_worker = self.run_worker(
            self._run_shell_task(command, incognito=incognito),
            exclusive=False,
        )

    async def _run_shell_task(
        self,
        command: str,
        *,
        incognito: bool = False,
    ) -> None:
        """Run a shell command in a background worker.

        Args:
            command: The shell command to execute.
            incognito: Whether command/output should remain local-only.
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(sys.platform != "win32"),
            )
            self._shell_process = proc

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_SHELL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                await self._kill_shell_process()
                await self._mount_message(
                    ErrorMessage(f"Command timed out ({_SHELL_TIMEOUT_SECONDS}s limit)")
                )
                return
            except asyncio.CancelledError:
                await self._kill_shell_process()
                raise

            output = (stdout_bytes or b"").decode(errors="replace").strip()
            stderr_text = (stderr_bytes or b"").decode(errors="replace").strip()
            if stderr_text:
                output += f"\n[stderr]\n{stderr_text}"

            if output:
                if incognito:
                    await self._mount_message(
                        SystemMessage(f"```text\n{output}\n```")
                    )
                else:
                    await self._mount_message(
                        AssistantMessage(f"```text\n{output}\n```")
                    )
            else:
                await self._mount_message(
                    SystemMessage("Command completed (no output)")
                )

            if proc.returncode and proc.returncode != 0:
                await self._mount_message(
                    ErrorMessage(f"Exit code: {proc.returncode}")
                )

            if not incognito:
                self._buffer_shell_for_model_context(command, output, proc.returncode)

        except asyncio.CancelledError:
            logger.debug("Shell task cancelled")
        except Exception as e:
            logger.exception("Shell command failed: %s", e)
            await self._mount_message(ErrorMessage(f"Shell error: {e!r}"))
        finally:
            self._shell_running = False
            self._shell_process = None
            self._shell_worker = None
            # Drain queue after shell completes
            await self._process_next_from_queue()

    async def _kill_shell_process(self) -> None:
        """Terminate the active shell process."""
        proc = self._shell_process
        if proc is None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, ProcessLookupError):
            with suppress(ProcessLookupError):
                proc.kill()

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
        handler = self._command_router.get_handler(cmd_name)
        if handler is not None:
            if self._agent_running and handler.bypass_tier == BypassTier.QUEUED:
                self._pending_messages.append(QueuedMessage(text=text, mode="command"))
                await self._mount_message(
                    SystemMessage(f"⏳ Command `{cmd_name}` queued...")
                )
                return

            if cmd_name not in {"/exit", "/quit", "/q", "/clear", "/force-clear", "/remember", "/skill-creator"} and not cmd_name.startswith("/skill:"):
                await self._mount_message(UserMessage(text))

            from opscloud.commands import CommandContext
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
            res = await self._command_router.dispatch(text, ctx)
            if res.mount_as_app_message and res.message:
                await self._mount_message(SystemMessage(res.message))
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
                SystemMessage(f"⏳ Command `{cmd_name}` queued...")
            )
            return

        if cmd_name not in {"/exit", "/quit", "/q", "/clear", "/force-clear", "/remember", "/skill-creator"} and not cmd_name.startswith("/skill:"):
            await self._mount_message(UserMessage(text))

        # ── Dispatch (Fallback for unmigrated inline commands) ──────

        if cmd_name == "/theme":
            if arg:
                await self._switch_theme(arg)
            else:
                await self._show_theme_selector()

        elif cmd_name == "/agents":
            if arg:
                # Direct switch: /agents <name>
                self._switch_agent(arg)
            else:
                # Open interactive picker
                await self._show_agent_selector()

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

    # ── /restart Implementation ──────────────────────────

    async def _handle_restart_command(self, command: str, *, echo: bool = False) -> None:
        """Drive the `/restart` slash command.

        Superset of `/reload`: re-reads .env/environment, clears configuration
        caches, then respawns the app-owned LangGraph server subprocess.
        """
        reload_task = self._reload_task
        defer_restart = (
            self._reloading and reload_task is not None and not reload_task.done()
        )
        if defer_restart:
            self._restart_requested_during_reload = True

        if echo:
            await self._mount_message(UserMessage(command))

        if defer_restart:
            await self._mount_message(
                SystemMessage("Reload already in progress; server restart requested.")
            )
            return

        restart_task = self._restart_respawn_task
        if restart_task is not None and not restart_task.done():
            await self._mount_message(
                SystemMessage(
                    "A server restart is already in progress. Queued prompts "
                    "will be sent once it finishes."
                )
            )
            return
        self._schedule_restart_command()

    def _schedule_restart_command(
        self,
        *,
        preserve_queue: bool = False,
        propagate_errors: bool = False,
        mutation_lock_held: bool = False,
    ) -> asyncio.Task[bool]:
        """Run config refresh and server respawn off the Textual message pump."""
        active = self._restart_respawn_task
        if active is not None and not active.done():
            return cast("asyncio.Task[bool]", active)

        task = asyncio.create_task(
            self._run_restart_command_detached(
                preserve_queue=preserve_queue,
                propagate_errors=propagate_errors,
                mutation_lock_held=mutation_lock_held,
            ),
            name="restart",
        )
        self._restart_respawn_task = task
        self._track_server_restart_task(task)
        task.add_done_callback(self._finish_restart)
        return task

    def _track_server_restart_task(self, task: asyncio.Task[Any]) -> None:
        self._server_restart_tasks.add(task)
        task.add_done_callback(self._server_restart_tasks.discard)

    def _finish_restart(self, task: asyncio.Task[Any]) -> None:
        """Resume prompts whose server-ready drain raced restart completion."""
        if task is not self._restart_respawn_task:
            return
        if self._pending_messages and not self._agent_running and not self._exit:
            drain = asyncio.create_task(
                self._process_next_from_queue(),
                name="drain-after-restart",
            )
            drain.add_done_callback(lambda t: None)

    async def _run_restart_command_detached(
        self,
        *,
        preserve_queue: bool = False,
        propagate_errors: bool = False,
        mutation_lock_held: bool = False,
    ) -> bool:
        """Serialize and safely report a detached /restart continuation."""
        try:
            if mutation_lock_held:
                return await self._run_restart_command(
                    preserve_queue=preserve_queue,
                    propagate_errors=propagate_errors,
                )
            async with self._environment_mutation_lock:
                return await self._run_restart_command(
                    preserve_queue=preserve_queue,
                    propagate_errors=propagate_errors,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if propagate_errors:
                raise
            logger.exception("Manual /restart failed unexpectedly")
            await self._mount_message(
                ErrorMessage(f"Restart failed: {type(exc).__name__}: {exc}")
            )
            return False

    async def _run_restart_command(
        self,
        *,
        preserve_queue: bool = False,
        propagate_errors: bool = False,
    ) -> bool:
        """Validate and schedule a restart."""
        if self._connecting and self._reconnecting:
            await self._mount_message(
                SystemMessage(
                    "A server restart is already in progress. Queued prompts "
                    "will be sent once it finishes."
                )
            )
            return False

        # Sever in-flight work bound to the dying subprocess
        preserved = None
        if self._agent_running and self._agent_worker:
            preserved = list(self._pending_messages) if preserve_queue else None
            self._agent_worker.cancel()
            self._agent_running = False
        elif not preserve_queue:
            self._discard_queue()

        if preserved is not None:
            self._pending_messages.extendleft(reversed(preserved))
            self._sync_status_queued()

        if not await self._reload_configuration_for_restart():
            await self._restore_queue_to_input(
                "Configuration reload failed; queued prompts were returned to "
                "the input. Re-submit them or run `/restart` again after fixing "
                "the configuration.",
                empty_notice="Configuration reload failed. Fix the configuration, then run `/restart` again.",
            )
            return False

        if self._server_proc is None and self._server_kwargs is None:
            await self._restore_queue_to_input(
                "Cannot restart: this app is connected to a remote LangGraph "
                "server (no owned subprocess). Configuration was reloaded; "
                "queued prompts were returned to the input. Re-submit them to "
                "send on the current session, or relaunch opscloud to fully restart.",
                empty_notice=(
                    "Cannot restart: this app is connected to a remote LangGraph "
                    "server (no owned subprocess). Configuration was reloaded; "
                    "relaunch opscloud to fully restart."
                ),
            )
            return False

        self._connecting = True
        self._reconnecting = True
        self._sync_status_connection()
        return await self._run_restart_respawn(propagate_errors=propagate_errors)

    async def _reload_configuration_for_restart(self) -> bool:
        """Refresh configuration and caches for restart."""
        try:
            if hasattr(self._settings, "reload_from_environment"):
                await asyncio.to_thread(self._settings.reload_from_environment)
            from opscloud.model.config import clear_caches
            clear_caches()
            return True
        except Exception as exc:
            logger.warning("Configuration reload for restart error: %s", exc)
            return False

    async def _run_restart_respawn(self, *, propagate_errors: bool = False) -> bool:
        """Respawn the server for `/restart`, detached from the message pump."""
        restarting = None
        restarted = False
        failure: Exception | None = None
        try:
            restarting = await self._mount_transient_app_message("Restarting server...")
            restarted = await self._restart_server_manual()
        except Exception as exc:
            if propagate_errors:
                failure = exc
            else:
                logger.exception("Manual /restart of server raised unexpectedly")
                await self._mount_message(
                    ErrorMessage(f"Restart failed: {type(exc).__name__}: {exc}")
                )
        finally:
            if not restarted:
                self._connecting = False
                self._reconnecting = False
                self._sync_status_connection()
            if restarting is not None:
                try:
                    await restarting.remove()
                except Exception:
                    pass

        if restarted:
            await self._mount_message(SystemMessage("Restart complete."))
        else:
            await self._restore_queue_to_input(
                "Restart did not complete; queued prompts were returned to the "
                "input. Re-submit them to send on the current session.",
                empty_notice="Restart did not complete. Run `/restart` to try again.",
            )
        if failure is not None:
            raise failure
        return restarted

    async def _restart_server_manual(self) -> bool:
        """Respawn the app-owned LangGraph server for a user-initiated restart."""
        return (await self._restart_server_manual_result()).restarted

    async def _restart_server_manual_result(self) -> _ServerRespawnResult:
        """Respawn the app-owned server for a user-initiated restart."""
        return await self._respawn_server(
            log_message="User-initiated server restart failed",
            mcp_failure_log="MCP metadata preload after user-initiated restart failed",
            mcp_failure_toast="MCP tool metadata could not be refreshed. Use /mcp to check.",
        )

    async def _respawn_server(
        self,
        *,
        log_message: str = "Server respawn failed",
        mcp_failure_log: str = "MCP preload after restart failed",
        mcp_failure_toast: str = "MCP metadata could not be refreshed",
        restart_timeout: float = 30.0,
    ) -> _ServerRespawnResult:
        """Stop the app-owned server subprocess and rebuild the agent."""
        server_proc = self._server_proc
        if server_proc is None:
            return _ServerRespawnResult(restarted=False)

        try:
            self._connecting = True
            self._reconnecting = True
            self._sync_status_connection()

            if hasattr(server_proc, "restart"):
                await server_proc.restart(timeout=restart_timeout)
            else:
                await asyncio.to_thread(server_proc.stop)
                await asyncio.to_thread(server_proc.start)

            from opscloud.mcp.preload import preload_mcp_server_info
            from opscloud.ui.remote_client import RemoteAgent

            mcp_info = None
            mcp_error = None
            mcp_status: _McpRefreshStatus = "unavailable"
            preload_kwargs = dict(getattr(self, "_mcp_preload_kwargs", {}) or {})

            if preload_kwargs.get("no_mcp"):
                mcp_status = "disabled"
            else:
                try:
                    mcp_info = await preload_mcp_server_info(**preload_kwargs)
                    mcp_status = "fresh" if mcp_info is not None else "unavailable"
                except Exception as exc:
                    logger.exception(mcp_failure_log)
                    mcp_error = f"{type(exc).__name__}: {exc}"
                    self.notify(f"{mcp_failure_toast} ({mcp_error})", severity="warning")

            ready_mcp_info = mcp_info if mcp_status != "unavailable" else self._mcp_server_info

            self._client = RemoteAgent(url=server_proc.url, graph_name="agent")
            if self._adapter:
                self._adapter.set_client(self._client)

            self.post_message(
                self.ServerReady(
                    client=self._client,
                    server_proc=server_proc,
                    mcp_server_info=ready_mcp_info,
                )
            )
            self._reconnecting = False
            return _ServerRespawnResult(
                restarted=True,
                mcp_server_info=mcp_info,
                mcp_status=mcp_status,
                mcp_error=mcp_error,
            )
        except Exception as exc:
            self._connecting = False
            self._reconnecting = False
            self._sync_status_connection()
            logger.exception(log_message)
            self.post_message(self.ServerStartFailed(error=exc))
            return _ServerRespawnResult(restarted=False)

    # ── /reload Implementation ───────────────────────────

    def _schedule_reload(self) -> asyncio.Task[None]:
        """Run `/reload` off the Textual message pump."""
        reload_task = self._reload_task
        if self._reloading and reload_task is not None and not reload_task.done():
            self.notify("Reload already in progress.", severity="information")
            return reload_task

        self._reloading = True
        task = asyncio.create_task(self._run_reload_sequence(), name="reload")
        task.add_done_callback(self._finish_reload)
        self._reload_task = task
        return task

    def _finish_reload(self, task: asyncio.Task[None]) -> None:
        """Release the reload guard and resume prompts queued during reload."""
        if task is not self._reload_task:
            return
        self._reloading = False
        if self._pending_messages and not self._agent_running and not self._exit:
            self.call_after_refresh(
                lambda: asyncio.create_task(self._process_next_from_queue())
            )

    async def _run_reload_sequence(self) -> None:
        """Run reload work, then honor restart requests it did not satisfy."""
        try:
            await self._run_reload()
        except asyncio.CancelledError:
            self._restart_requested_during_reload = False
            raise

        if self._exit:
            self._restart_requested_during_reload = False
            return
        if self._restart_requested_during_reload:
            self._restart_requested_during_reload = False
            self._schedule_restart_command(preserve_queue=True)

    async def _run_reload(self) -> None:
        """Refresh config, themes, skills, plugins, and hooks, then report."""
        async with self._environment_mutation_lock:
            await self._run_reload_unlocked()

    async def _run_reload_unlocked(self) -> None:
        """Run `/reload` while the environment mutation lock is held."""
        from opscloud.plugins.adapters.mcp import plugin_mcp_configs
        from opscloud.plugins.discovery import discover_plugins

        try:
            old_skill_names = {
                str(s.get("name", "")) for s in (self._discovered_skills or []) if s.get("name")
            }
            old_mcp_server_info = self._mcp_server_info

            # 1. Reload configuration & .env
            try:
                changes = await asyncio.to_thread(self._settings.reload_from_environment)
                from opscloud.model.config import clear_caches
                clear_caches()
            except (OSError, ValueError, Exception):
                logger.exception("Failed to reload configuration")
                await self._mount_message(
                    SystemMessage(
                        "Failed to reload configuration. Check your .env "
                        "file and environment variables for syntax errors, "
                        "then try again."
                    )
                )
                return

            # 2. Reload themes
            theme_reload_ok = True
            try:
                from opscloud.ui import theme
                theme.reload_registry()
                theme.register_app_themes(self)
            except Exception:
                theme_reload_ok = False
                logger.warning("Failed to reload themes", exc_info=True)

            # 3. Re-discover skills
            self._discovered_skills = await asyncio.to_thread(self.get_discovered_skills)
            new_skill_names = {
                str(s.get("name", "")) for s in (self._discovered_skills or []) if s.get("name")
            }
            added_skills = sorted(new_skill_names - old_skill_names)
            removed_skills = sorted(old_skill_names - new_skill_names)

            if hasattr(self, "_chat_input") and self._chat_input:
                from opscloud.ui.command_registry import build_skill_commands, get_all_entries

                skill_commands = build_skill_commands(self._discovered_skills)
                self._chat_input.update_slash_commands(get_all_entries(skill_commands))

            # 4. Compose initial report lines
            if changes:
                report = "Configuration reloaded. Changes:\n" + "\n".join(
                    f"  - {change}" for change in changes
                )
            else:
                report = "Configuration reloaded. No changes detected."
            report += "\nModel config caches cleared."
            if theme_reload_ok:
                report += "\nTheme registry reloaded."
            else:
                report += "\nTheme registry reload failed."

            if added_skills or removed_skills:
                skill_lines = []
                if added_skills:
                    skill_lines.append(f"  - Added: {', '.join(added_skills)}")
                if removed_skills:
                    skill_lines.append(f"  - Removed: {', '.join(removed_skills)}")
                report += "\nSkills updated:\n" + "\n".join(skill_lines)

            # 5. Discover plugins
            try:
                plugin_result = await asyncio.to_thread(discover_plugins)
                plugins = plugin_result.plugins
            except Exception:
                logger.exception("Failed to discover plugins during /reload")
                plugins = ()
                report += "\nCouldn't read plugin state; run /reload to be safe."

            plugin_count = len(plugins)
            mcp_configs = plugin_mcp_configs(plugins)
            mcp_count = sum(
                len(servers)
                for config in mcp_configs
                if isinstance((servers := config.get("mcpServers", config)), dict)
            )
            plugin_skill_count = sum(1 for name in new_skill_names if ":" in name)
            hook_count = sum(len(getattr(p.manifest, "hooks", ()) or ()) for p in plugins)
            separator = " · "
            report += (
                f"\nPlugins: {plugin_count} plugin"
                f"{'s' if plugin_count != 1 else ''}{separator}"
                f"{plugin_skill_count} skill"
                f"{'s' if plugin_skill_count != 1 else ''}{separator}"
                f"{mcp_count} plugin MCP server"
                f"{'s' if mcp_count != 1 else ''}{separator}"
                f"{hook_count} hook{'s' if hook_count != 1 else ''}"
            )

            # 6. Restart owned server for plugin MCP
            if self._server_proc is not None:
                preserved = list(self._pending_messages)
                if self._agent_running and self._agent_worker:
                    self._agent_worker.cancel()
                    self._agent_running = False
                else:
                    self._discard_queue()

                try:
                    restart_result = await self._restart_server_manual_result()
                finally:
                    self._restart_requested_during_reload = False
                    if preserved:
                        self._pending_messages.extendleft(reversed(preserved))
                        self._sync_status_queued()

                if restart_result.restarted:
                    report += "\nAgent server restarted for plugin MCP."
                    if restart_result.mcp_status == "disabled":
                        report += (
                            "\nMCP is disabled for this session (--no-mcp); "
                            "no servers were loaded."
                        )
                    else:
                        report += "\n" + _format_mcp_server_changes(
                            old_mcp_server_info,
                            restart_result.mcp_server_info,
                            restart_result.mcp_error,
                        )
                else:
                    report += (
                        "\nAgent server was not restarted; plugin MCP may be stale."
                    )
            elif self._server_kwargs is None:
                report += (
                    "\nMCP servers unchanged; this session uses a remote "
                    "server and cannot reload MCP config."
                )

            await self._mount_message(SystemMessage(report))
        except Exception as exc:
            logger.exception("Detached /reload failed unexpectedly")
            await self._mount_message(
                ErrorMessage(f"Reload failed: {type(exc).__name__}: {exc}")
            )

    async def _invoke_reload(self, *, command: str | None = None) -> None:
        """Perform hot-reload of config, themes, skills, and plugins."""
        if command is not None:
            await self._mount_message(UserMessage(command))
        self._schedule_reload()

    async def _handle_install_command(self, command: str) -> None:
        """Execute in-app package installation and auto-reload."""
        await self._mount_message(UserMessage(command))
        parts = command.strip().split()
        if len(parts) < 2:
            await self._mount_message(
                SystemMessage(
                    "Usage: /install <extra> [--force]\n"
                    "       /install <package> --package [--force]\n\n"
                    "Example: /install quickjs\n"
                    "         /install daytona"
                )
            )
            return

        target = parts[1]
        is_pkg = "--package" in parts[1:]
        force = "--force" in parts[1:]

        await self._mount_message(SystemMessage(f"Installing `{target}`..."))

        import shutil
        installer = "uv" if shutil.which("uv") else "pip"
        if installer == "uv" and not is_pkg:
            cmd_args = ["uv", "tool", "install", "--reinstall", "-U", f"opscloud[{target}]"]
        elif installer == "uv" and is_pkg:
            cmd_args = ["uv", "pip", "install", target]
        else:
            cmd_args = ["pip", "install", target]
        if force:
            cmd_args.append("--force")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                await self._mount_message(
                    SystemMessage(f"Successfully installed `{target}`. Triggering reload...")
                )
                await self._invoke_reload()
            else:
                err_text = stderr.decode().strip() or stdout.decode().strip()
                await self._mount_message(ErrorMessage(f"Failed to install `{target}`:\n{err_text}"))
        except Exception as exc:
            await self._mount_message(ErrorMessage(f"Error running installer for `{target}`: {exc}"))

    async def _handle_update_command(self, command: str = "/update") -> None:
        """Check for and apply OpsCloud software updates."""
        await self._mount_message(UserMessage(command))
        parts = command.strip().split()
        allowed = {"--prerelease", "--deps"}
        unknown = [opt for opt in parts[1:] if opt not in allowed]
        if unknown:
            await self._mount_message(
                SystemMessage(
                    f"Unknown option(s) for /update: {' '.join(unknown)}. Usage: /update [--deps] [--prerelease]"
                )
            )
            return

        prerelease = "--prerelease" in parts[1:]
        try:
            from opscloud import __version__ as cli_version
        except ImportError:
            cli_version = "unknown"

        await self._mount_message(SystemMessage(f"Checking updates for OpsCloud (current: v{cli_version})..."))
        from opscloud.commands.power.runtime import _check_pypi_version, _perform_upgrade

        latest = await asyncio.to_thread(_check_pypi_version, prerelease=prerelease)
        if latest is None:
            await self._mount_message(
                SystemMessage(f"Could not determine latest version. Currently on v{cli_version}.")
            )
            return

        if latest == cli_version:
            await self._mount_message(
                SystemMessage(f"OpsCloud v{cli_version} is on the latest version.")
            )
            return

        await self._mount_message(SystemMessage(f"Updating OpsCloud v{cli_version} → v{latest}..."))
        success, output = await asyncio.to_thread(_perform_upgrade, target_version=latest, prerelease=prerelease)
        if success:
            await self._mount_message(SystemMessage(f"✅ Updated OpsCloud to v{latest}. Run `/restart` to apply."))
        else:
            await self._mount_message(ErrorMessage(f"Failed to update OpsCloud:\n{output}"))

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
        """Extract goal and rubric metadata from raw checkpoint channel values.

        """
        def _as_str(value: object) -> str | None:
            return value if isinstance(value, str) else None

        def _as_nonblank_str(value: object) -> str | None:
            return value if isinstance(value, str) and value.strip() else None

        goal_obj = _as_str(state_values.get("_goal_objective"))
        goal_status = _as_str(state_values.get("_goal_status"))

        # Fallback: extract objective and status from get_goal tool outputs if missing in state_values
        if not goal_obj and raw_messages:
            for m in raw_messages:
                name = getattr(m, "name", None)
                mtype = getattr(m, "type", None)
                if name == "get_goal" or mtype == "tool":
                    c = getattr(m, "content", "")
                    if isinstance(c, str) and '"objective"' in c:
                        try:
                            import json
                            parsed = json.loads(c)
                            if isinstance(parsed, dict):
                                obj = parsed.get("objective")
                                stat = parsed.get("status")
                                if isinstance(obj, str) and obj.strip():
                                    goal_obj = obj.strip()
                                if isinstance(stat, str) and stat.strip():
                                    goal_status = stat.strip()
                                break
                        except Exception:
                            pass

        return {
            "rubric": _as_str(state_values.get("rubric")),
            "sticky_rubric": _as_str(state_values.get("_sticky_rubric")),
            "sticky_rubric_recorded": "_sticky_rubric" in state_values,
            "goal_objective": goal_obj or _as_str(state_values.get("_pending_goal_objective")),
            "goal_status": goal_status or ("active" if goal_obj else None),
            "goal_rubric": _as_str(state_values.get("_goal_rubric")),
            "goal_status_note": _as_str(state_values.get("_goal_status_note")),
            "pending_goal_completion_note": _as_str(state_values.get("_pending_goal_completion_note")),
            "rubric_status": _as_str(state_values.get("_rubric_status")),
            "rubric_grading_run_id": _as_nonblank_str(state_values.get("_current_grading_run_id")),
            "pending_goal_objective": _as_str(state_values.get("_pending_goal_objective")),
            "pending_goal_rubric": _as_str(state_values.get("_pending_goal_rubric")),
            "pending_goal_kind": _as_str(state_values.get("_pending_goal_kind")),
            "pending_goal_request_id": _as_nonblank_str(state_values.get("_pending_goal_request_id")),
        }

    def _sync_status_rubric(self) -> None:
        """Sync active goal and rubric fields to local GoalState and GoalStatusPanel.

        """
        from opscloud.commands.power.goal import get_goal_state
        goal_state = get_goal_state(self)
        goal_state.objective = self._active_goal
        goal_state.status = self._goal_status or ("active" if self._active_goal else None)
        goal_state.rubric = self._active_rubric
        goal_state.status_note = self._goal_status_note
        goal_state.pending_objective = self._pending_goal_objective
        goal_state.pending_rubric = self._pending_goal_rubric
        goal_state.pending_kind = self._pending_goal_kind

        try:
            from opscloud.ui.widgets.goal_status import GoalStatusPanel
            panel = self.query_one(GoalStatusPanel)
            panel.set_goal(
                self._active_goal or self._pending_goal_objective,
                self._goal_status or ("active" if self._active_goal else None),
                self._goal_status_note or self._pending_goal_completion_note,
            )
        except Exception:
            pass

        try:
            from opscloud.commands.power.goal import GoalHandler
            GoalHandler._sync_status_rubric(self, goal_state)
        except Exception:
            pass

    def _restore_goal_rubric_state(self, payload: dict[str, Any]) -> None:
        """Restore active goal and rubric state from thread payload.

        """
        self._active_goal = payload.get("goal_objective")
        self._goal_status = payload.get("goal_status")
        self._goal_status_note = payload.get("goal_status_note")
        self._pending_goal_completion_note = payload.get("pending_goal_completion_note")
        if payload.get("goal_rubric"):
            self._active_rubric = payload.get("goal_rubric")
        elif payload.get("sticky_rubric_recorded"):
            self._active_rubric = payload.get("sticky_rubric")
        else:
            self._active_rubric = payload.get("rubric")
        self._pending_goal_objective = payload.get("pending_goal_objective")
        self._pending_goal_rubric = payload.get("pending_goal_rubric")
        self._pending_goal_kind = payload.get("pending_goal_kind")
        self._pending_goal_request_id = payload.get("pending_goal_request_id")
        self._sync_status_rubric()

    async def _load_thread_history(self, thread_id: str) -> None:
        """Fetch and mount stored message history for a thread."""
        import re
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from opscloud.middleware.goal_state_notice import is_internal_message
        from opscloud.ui.widgets.messages import (
            AssistantMessage,
            MessageList,
            ToolCallMessage,
            UserMessage,
        )

        self._reset_thread_usage(0.0, 0)
        state_values = await self._get_thread_state_values(thread_id)
        raw_messages = state_values.get("messages", [])
        payload = self._goal_rubric_payload_from_state(state_values, raw_messages)
        self._restore_goal_rubric_state(payload)
        if not raw_messages:
            return

        if any(isinstance(m, dict) for m in raw_messages):
            from langchain_core.messages.utils import convert_to_messages
            raw_messages = convert_to_messages(raw_messages)

        pending_tools: dict[str, dict] = {}

        try:
            messages_container = self.query_one("#messages", MessageList)
        except NoMatches:
            return

        restored_goal_prompt = False

        for idx, msg in enumerate(raw_messages):
            msg_type = getattr(msg, "type", type(msg).__name__.lower().replace("message", ""))
            content_raw = getattr(msg, "content", None)
            is_internal = is_internal_message(msg)
            msg_name = getattr(msg, "name", None) or ""

            logger.info(
                "TUI: _load_thread_history msg[%d/%d] type=%s cls=%s name=%s is_internal=%s content=%r",
                idx,
                len(raw_messages),
                msg_type,
                type(msg).__name__,
                msg_name,
                is_internal,
                str(content_raw)[:80] if content_raw else "",
            )

            # Gate 1: skip named sub-graph messages
            if msg_name in {"rubric_grader", "goal_criteria"}:
                continue

            # Gate 2: skip LangChain system messages
            if msg_type == "system":
                continue

            # Normalise content → plain string
            if isinstance(content_raw, list):
                parts = []
                for block in content_raw:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "".join(parts).strip()
            elif content_raw is not None:
                text = str(content_raw).strip()
            else:
                text = ""

            if isinstance(msg, HumanMessage) or msg_type == "human":
                if text.startswith("[SYSTEM]") or is_internal:
                    if not restored_goal_prompt and idx <= 3:
                        goal_obj = payload.get("goal_objective") or state_values.get("_goal_objective")
                        if goal_obj and isinstance(goal_obj, str) and goal_obj.strip():
                            text = f"/goal {goal_obj.strip()}"
                            restored_goal_prompt = True
                            logger.info("TUI: Restored goal objective prompt for msg[%d]: %r", idx, text)
                        else:
                            logger.info("TUI: Skipping internal human msg[%d] (no goal objective found)", idx)
                            continue
                    else:
                        logger.info("TUI: Skipping internal human msg[%d]", idx)
                        continue
                elif is_internal:
                    continue

                if not text:
                    continue

                # Skip grader evaluation prompts leaked from sub-graph writes
                if text.startswith("This is grader iteration "):
                    continue

                # Skip system prompt echoes
                if text.startswith("System Prompt"):
                    continue

                # ── Extract user intent from goal <operation> XML ─────
                if text.startswith("<operation>"):
                    if text.startswith("<operation>draft</operation>"):
                        match = re.search(r"<goal>\s*(.*?)\s*</goal>", text, re.DOTALL)
                        if match:
                            text = f"/goal {match.group(1).strip()}"
                        else:
                            continue  # Unextractable draft — skip
                    elif text.startswith("<operation>amend</operation>"):
                        match = re.search(r"<user_feedback>\s*(.*?)\s*</user_feedback>", text, re.DOTALL)
                        if match:
                            text = f"/goal amend {match.group(1).strip()}"
                        else:
                            text = "/goal amend"
                    else:
                        continue  # Unknown operation type — skip

                await self._mount_message(UserMessage(text))

            elif isinstance(msg, AIMessage) or msg_type == "ai":
                # Extract text and thinking from AIMessage
                from opscloud.ui.textual_adapter import _extract_text_and_thinking
                ai_text, thinking = _extract_text_and_thinking(
                    getattr(msg, "content", ""),
                    getattr(msg, "additional_kwargs", None),
                    getattr(msg, "response_metadata", None),
                    msg_obj=msg
                )
                if ai_text or thinking:
                    if thinking:
                        messages_container.add_thinking_message(thinking)
                    if ai_text:
                        assistant_msg = AssistantMessage(ai_text)
                        await self._mount_message(assistant_msg)

                # Mount tool-call widgets for each tool_call in this AI message.
                # Guard against malformed entries: SQLite rehydration can produce
                # lists or other non-dict shapes inside tool_calls.
                for tc in getattr(msg, "tool_calls", []) or []:
                    if not isinstance(tc, dict):
                        logger.debug(
                            "Skipping malformed tool_call entry (type=%s) "
                            "during history load for thread %s",
                            type(tc).__name__, thread_id,
                        )
                        continue
                    tc_id = tc.get("id", "")
                    name = tc.get("name") or "tool"
                    args = tc.get("args") or {}
                    if not isinstance(args, dict):
                        args = {}
                    messages_container.add_tool_call(
                        name=name, call_id=tc_id, args=args, live=False
                    )
                    if tc_id:
                        pending_tools[tc_id] = {"name": name}

            elif isinstance(msg, ToolMessage) or msg_type == "tool":
                tc_id = getattr(msg, "tool_call_id", "") or ""
                status = getattr(msg, "status", "success")
                tool_content = text or ""
                success = status != "error"

                if tc_id and tc_id in pending_tools:
                    pending_tools.pop(tc_id)
                    messages_container.update_tool_result(
                        call_id=tc_id,
                        result=tool_content,
                        success=success,
                        live=False,
                    )
                else:
                    tool_name = getattr(msg, "name", None) or "tool"
                    messages_container.update_tool_result(
                        call_id=tc_id or "",
                        result=tool_content,
                        success=success,
                        name=tool_name,
                        live=False,
                    )
            else:
                logger.debug(
                    "Skipping unsupported message type %s during history load",
                    type(msg).__name__,
                )

        # Mark unmatched tool calls as rejected (reference L16301-L16303)
        for tc_id in pending_tools:
            if tc_id in messages_container._tool_calls:
                messages_container._tool_calls[tc_id].set_rejected()

        # Restore context tokens and cumulative estimated cost from message history
        restored_cost = 0.0
        restored_tokens = 0
        model_spec = getattr(self, "_model", "") or ""
        if ":" in model_spec:
            provider_part, model_part = model_spec.split(":", 1)
        else:
            model_part = model_spec
            provider_part = getattr(getattr(self, "_settings", None), "model_provider", "") or ""

        from opscloud.utils.cost_estimation import estimate_cost
        for msg in raw_messages:
            usage_meta = (
                getattr(msg, "usage_metadata", None)
                or (getattr(msg, "response_metadata", None) or {}).get("usage")
                or (getattr(msg, "response_metadata", None) or {}).get("token_usage")
            )
            if isinstance(usage_meta, dict):
                inp = usage_meta.get("input_tokens", 0) or 0
                out = usage_meta.get("output_tokens", 0) or 0
                if inp or out:
                    restored_tokens += inp + out
                if model_part:
                    m_name = (getattr(msg, "response_metadata", {}) or {}).get("model_name") or model_part
                    c = estimate_cost(m_name, provider_part, inp, out)
                    if c is not None and c > 0:
                        restored_cost += c

        checkpoint_cost = state_values.get("_session_cost_usd")
        if isinstance(checkpoint_cost, (int, float)) and math.isfinite(checkpoint_cost) and checkpoint_cost > 0:
            restored_cost = max(restored_cost, float(checkpoint_cost))

        checkpoint_tokens = state_values.get("_session_total_tokens")
        if isinstance(checkpoint_tokens, int) and checkpoint_tokens > 0:
            restored_tokens = max(restored_tokens, checkpoint_tokens)

        context_tokens = state_values.get("_context_tokens")
        if isinstance(context_tokens, int) and context_tokens > 0:
            restored_context_tokens = context_tokens
        else:
            last_ai_tokens = 0
            for msg in reversed(raw_messages):
                if getattr(msg, "type", "") == "ai" or msg.__class__.__name__ == "AIMessage":
                    u = getattr(msg, "usage_metadata", None) or {}
                    inp = u.get("input_tokens", 0) or 0
                    out = u.get("output_tokens", 0) or 0
                    if inp or out:
                        last_ai_tokens = inp + out
                        break
            restored_context_tokens = last_ai_tokens or restored_tokens

        self._reset_thread_usage(
            cost_usd=restored_cost,
            context_tokens=restored_context_tokens,
            cumulative_tokens=restored_tokens,
        )

        # Regroup completed historical tool calls into collapsible summaries (reference L7897, L7955)
        await self._regroup_completed_tools()

        logger.info(
            "TUI: _load_thread_history completed for thread %s (raw_messages=%d, mounted_widgets=%d, tokens=%d, cost=$%.4f)",
            thread_id,
            len(raw_messages),
            len(messages_container.children),
            self._context_tokens,
            self._session_cost_usd,
        )

        # Scroll to bottom after history loads (reference L16769-L16774)
        try:
            from textual.containers import VerticalScroll
            chat = self.query_one("#chat", VerticalScroll)
            chat.scroll_end(animate=False)
        except NoMatches:
            pass

    def _close_active_tool_group(self) -> None:
        """Finalize the open tool group into its collapsed past-tense form.

        """
        try:
            from opscloud.ui.widgets.messages import MessageList
            messages = self.query_one("#messages", MessageList)
            messages.close_active_tool_group()
        except Exception:
            logger.exception("Failed to close active tool group")

    async def _regroup_completed_tools(self) -> None:
        """Fold runs of completed tool calls into collapsible group summaries.

        """
        try:
            from opscloud.ui.widgets.messages import MessageList
            messages = self.query_one("#messages", MessageList)
            await messages.regroup_completed_tools()
        except Exception:
            logger.exception("Failed to regroup completed tools")


    async def resume_thread(self, thread_id: str, *, is_new: bool = False) -> None:
        """Resume conversation from specified checkpoint thread ID and restore history."""
        self._agent_thread_id = thread_id
        bind_logging_to_thread(thread_id)
        session_state = getattr(self, "_session_state", None)
        if session_state:
            session_state.thread_id = thread_id
        self._pending_messages.clear()
        self._queued_widgets.clear()
        try:
            from opscloud.ui.widgets.messages import MessageList
            messages = self.query_one("#messages", MessageList)
            messages.clear()
        except NoMatches:
            pass

        if is_new:
            self._restore_goal_rubric_state({})
            self._reset_thread_usage(0.0, 0)
        else:
            await self._load_thread_history(thread_id)
            if hasattr(self, "_sync_goal_state_from_checkpoint"):
                await self._sync_goal_state_from_checkpoint(force=True)

        label = "✨ Created new thread" if is_new else "🔄 Resumed thread"
        await self._mount_message(
            SystemMessage(f"{label}: `{thread_id}`")
        )


    async def invoke_compact_conversation(self, thread_id: str | None = None, force: bool = True) -> None:
        """Trigger server-side conversation summarization/compaction."""
        from opscloud.commands.core.compact import CompactHandler
        from opscloud.commands._base import CommandContext
        handler = CompactHandler()
        ctx = CommandContext(
            raw_command="/compact",
            args="",
            app=self,
            agent=getattr(self, "_agent_graph", None),
        )
        await handler.execute(ctx)

    def toggle_timestamps(self, visible: bool | None = None) -> bool:
        """Toggle message timestamps visibility across chat messages."""
        if visible is not None:
            self._message_timestamps_visible = visible
        else:
            self._message_timestamps_visible = not getattr(self, "_message_timestamps_visible", False)
        try:
            from opscloud.ui.widgets.messages import MessageList
            messages = self.query_one("#messages", MessageList)
            messages.set_timestamps_visible(self._message_timestamps_visible)
        except Exception:
            pass
        return self._message_timestamps_visible

    def get_context_tokens(self) -> int:
        """Get active context tokens for the current turn/session."""
        adapter_tokens = 0
        if self._adapter and hasattr(self._adapter, "stats") and self._adapter.stats:
            adapter_tokens = getattr(self._adapter.stats, "input_tokens", 0) + getattr(self._adapter.stats, "output_tokens", 0)
        return getattr(self, "_context_tokens", 0) or adapter_tokens

    async def _get_conversation_token_count(self) -> int | None:
        """Get approximate token count of conversation message history."""
        try:
            msgs = self.get_thread_messages()
            if not msgs:
                return None
            from langchain_core.messages.utils import count_tokens_approximately
            return count_tokens_approximately(msgs)
        except Exception:
            return None

    async def _has_conversation_messages(self) -> bool:
        """Check if the active session has conversation messages."""
        try:
            from opscloud.ui.widgets.messages import UserMessage, AssistantMessage, MessageList
            container = self.query_one("#messages", MessageList)
            for child in container.children:
                if isinstance(child, (UserMessage, AssistantMessage)):
                    return True
        except Exception:
            pass

        try:
            msgs = self.get_thread_messages()
            if msgs:
                return True
        except Exception:
            pass

        return False

    async def get_conversation_token_count(self) -> int | None:
        """Get approximate token count of conversation message history."""
        return await self._get_conversation_token_count()

    def get_thread_messages(self) -> list:
        """Fetch current thread messages from UI widgets or adapter."""
        msgs = []
        try:
            from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
            from opscloud.ui.widgets.messages import UserMessage, AssistantMessage, ToolCallMessage, MessageList
            container = self.query_one("#messages", MessageList)
            for child in container.children:
                if isinstance(child, UserMessage):
                    txt = getattr(child, "_raw_content", "") or ""
                    if txt:
                        msgs.append(HumanMessage(content=txt))
                elif isinstance(child, AssistantMessage):
                    txt = getattr(child, "content_text", None) or "".join(getattr(child, "_fragments", []))
                    if txt:
                        msgs.append(AIMessage(content=txt))
                elif isinstance(child, ToolCallMessage):
                    res = getattr(child, "_result", "") or ""
                    call_id = getattr(child, "_call_id", "") or "call_1"
                    if res:
                        msgs.append(ToolMessage(content=str(res), tool_call_id=call_id))
        except Exception:
            pass
        return msgs

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
        return getattr(self, "_mcp_server_info", [])

    def get_discovered_skills(self) -> list[dict[str, Any]]:
        """Get list of discovered skills (project, user, plugin, built-in)."""
        from pathlib import Path
        from opscloud.config.settings import settings
        from opscloud.skills.loader import list_skills

        built_in_dir = Path(__file__).parent.parent / "built_in_skills"
        user_skills_dir = settings.get_user_skills_dir("opscloud")
        project_skills_dir = settings.get_project_skills_dir()
        project_root = (
            getattr(self._settings, "project_root", None)
            if hasattr(self, "_settings") and self._settings
            else settings.project_root
        )

        skills = list_skills(
            built_in_skills_dir=built_in_dir,
            user_skills_dir=user_skills_dir,
            project_skills_dir=project_skills_dir,
            include_plugins=True,
            project_root=project_root,
        )
        return [dict(s) for s in skills]

    async def _discover_startup_skills(self) -> None:
        """Discover skills in the background on startup and populate autocomplete."""
        try:
            skills = await asyncio.to_thread(self.get_discovered_skills)
            self._discovered_skills = skills
            if skills and hasattr(self, "_chat_input") and self._chat_input:
                from opscloud.ui.command_registry import build_skill_commands, get_all_entries

                skill_commands = build_skill_commands(skills)
                self._chat_input.update_slash_commands(get_all_entries(skill_commands))
        except Exception as exc:
            logger.debug("Failed startup skill discovery: %s", exc)

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
    ) -> asyncio.Future:
        """Request user approval inline in the messages area.

        Mounts ApprovalMenu in the messages area (inline with chat).
        ChatInput stays visible - user can still see it.

        If another approval is already pending, queue this one.

        Auto-approves shell commands that are in the configured allow-list.

        Args:
            action_requests: List of action request dicts to approve
            assistant_id: The assistant ID for display purposes

        Returns:
            A Future that resolves to the user's decision dict.
        """
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future = loop.create_future()

        is_auto_fallback = any(
            isinstance(request.get("description"), str)
            and request["description"].startswith("Auto human fallback ")
            for request in action_requests or []
            if isinstance(request, dict)
        )
        shell_allow_list = getattr(self, "_shell_allow_list", None)
        if shell_allow_list and action_requests and not is_auto_fallback:
            from opscloud.security.shell_safety import is_shell_command_allowed
            all_auto_approved = True
            approved_commands = []

            for req in action_requests:
                if isinstance(req, dict) and req.get("name") in {"execute", "run_command", "bash", "shell"}:
                    command = req.get("args", {}).get("command", "") or req.get("args", {}).get("CommandLine", "")
                    if is_shell_command_allowed(command, shell_allow_list):
                        approved_commands.append(command)
                    else:
                        all_auto_approved = False
                        break
                else:
                    all_auto_approved = False
                    break

            if all_auto_approved and approved_commands:
                result_future.set_result({"type": "approve"})
                try:
                    from opscloud.ui.widgets.messages import SystemMessage
                    messages = self.query_one("#messages", MessageList)
                    for command in approved_commands:
                        auto_msg = SystemMessage(f"✓ Auto-approved shell command (allow-list): {command}")
                        await messages.mount(auto_msg)
                except Exception:
                    pass
                return result_future

        # If there's already a pending approval, wait for it to complete first
        if self._pending_approval_widget is not None:
            while self._pending_approval_widget is not None:
                await asyncio.sleep(0.05)

        self._pause_loading_spinner_for_approval()
        result_future.add_done_callback(self._resume_loading_spinner_after_approval)

        from opscloud.ui.widgets.approval import ApprovalMenu

        unique_id = f"approval-menu-{uuid.uuid4().hex[:8]}"
        menu = ApprovalMenu(
            action_requests,
            assistant_id or self._assistant_id,
            id=unique_id,
            auto_mode_eligible=not getattr(self, "_sandbox_active", False),
        )
        menu.set_future(result_future)

        self._pending_approval_widget = menu

        if self._is_user_typing():
            from textual.widgets import Static
            placeholder = Static(
                "Waiting for typing to finish...",
                classes="approval-placeholder",
            )
            self._approval_placeholder = placeholder
            try:
                messages = self.query_one("#messages", MessageList)
                res = messages.mount_inline_prompt(placeholder) if hasattr(messages, "mount_inline_prompt") else messages.mount(placeholder)
                if inspect.isawaitable(res):
                    await res
                self.call_after_refresh(placeholder.scroll_visible)
            except Exception:
                logger.exception("Failed to mount approval placeholder")
                self._approval_placeholder = None
                await self._mount_approval_widget(menu, result_future)
                return result_future

            self.run_worker(
                self._deferred_show_approval(placeholder, menu, result_future),
                exclusive=False,
            )
        else:
            await self._mount_approval_widget(menu, result_future)

        return result_future

    async def _mount_approval_widget(
        self,
        menu: ApprovalMenu,
        result_future: asyncio.Future[dict[str, str]],
    ) -> None:
        """Mount the approval menu widget inline in the messages area."""
        try:
            messages = self.query_one("#messages", MessageList)
            res = messages.mount_inline_prompt(menu) if hasattr(messages, "mount_inline_prompt") else messages.mount(menu)
            if inspect.isawaitable(res):
                await res
            self.call_after_refresh(menu.scroll_visible)
            self.call_after_refresh(menu.focus)
        except Exception as e:
            logger.exception(
                "Failed to mount approval menu (id=%s) in messages container",
                menu.id,
            )
            self._pending_approval_widget = None
            if not result_future.done():
                result_future.set_exception(e)

    async def _deferred_show_approval(
        self,
        placeholder: Any,
        menu: ApprovalMenu,
        result_future: asyncio.Future[dict[str, str]],
    ) -> None:
        """Wait until user is idle, then swap placeholder for real menu."""
        deadline = time.monotonic() + 3.0
        while self._is_user_typing():
            if time.monotonic() > deadline:
                logger.warning("Timed out waiting for user to stop typing; showing approval now")
                break
            await asyncio.sleep(0.2)

        if not getattr(placeholder, "is_attached", False):
            logger.warning("Approval placeholder detached before menu shown (id=%s)", menu.id)
            self._approval_placeholder = None
            self._pending_approval_widget = None
            if not result_future.done():
                result_future.cancel()
            return

        self._approval_placeholder = None
        try:
            await placeholder.remove()
        except Exception:
            logger.warning("Failed to remove approval placeholder during swap", exc_info=True)
        await self._mount_approval_widget(menu, result_future)

    @on(ApprovalMenu.Decided)
    async def _on_approval_menu_decided(self, event: ApprovalMenu.Decided) -> None:
        if self._approval_placeholder is not None:
            if getattr(self._approval_placeholder, "is_attached", False):
                try:
                    await self._approval_placeholder.remove()
                except Exception:
                    pass
            self._approval_placeholder = None

        if self._pending_approval_widget is not None:
            try:
                await self._pending_approval_widget.remove()
            except Exception:
                pass
            self._pending_approval_widget = None
        elif isinstance(event.control, ApprovalMenu):
            try:
                await event.control.remove()
            except Exception:
                pass

        decision = event.decision
        dec_type = decision.get("type")

        if dec_type in {"smart_approve_all", "auto_approve_all"}:
            from opscloud.approval_mode import ApprovalMode
            await self._set_approval_mode(ApprovalMode.SMART)

        if self._adapter and event.call_id:
            self._adapter.submit_approval(event.call_id, event.approved)

        if not event.approved and hasattr(self, "_permission_store") and event.call_id:
            self._permission_store.track_denied(
                tool_name=event.tool_name,
                call_id=event.call_id,
                comment=event.comment,
            )

        self._focus_chat_input_after_refresh()

    @on(TextualAdapter.InterruptRaised)
    async def _on_interrupt_raised(self, event: TextualAdapter.InterruptRaised) -> None:
        """Legacy event handler for InterruptRaised."""
        if self._auto_approve and self._adapter:
            self._adapter.submit_approval(event.call_id, True)
            return

        future = await self._request_approval(
            [{"name": event.tool_name, "call_id": event.call_id, "args": event.args}],
            self._assistant_id,
        )
        decision = await future
        if self._adapter:
            approved = decision.get("type") in {"approve", "auto_approve_all", "smart_approve_all"}
            self._adapter.submit_approval(event.call_id, approved)

    # ── Ask User Interrupt Handling ──────────────────────

    async def _request_ask_user(
        self,
        questions: list[Any],
    ) -> asyncio.Future[Any]:
        """Mount the interactive AskUserMenu widget and return a Future awaiting response."""
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[Any] = loop.create_future()

        # Clean up any stale ask_user widget
        if self._pending_ask_user_widget is not None:
            try:
                self._pending_ask_user_widget.remove()
            except Exception:
                pass
            self._pending_ask_user_widget = None

        unique_id = f"ask-user-{uuid.uuid4().hex[:8]}"
        menu = AskUserMenu(questions, id=unique_id)
        menu.set_future(result_future)
        self._pending_ask_user_widget = menu

        # Mount widget into messages container (inline with chat)
        try:
            messages = self.query_one("#messages", MessageList)
            res = (
                messages.mount_inline_prompt(menu)
                if hasattr(messages, "mount_inline_prompt")
                else messages.mount(menu)
            )
            if inspect.isawaitable(res):
                await res
            self.call_after_refresh(menu.scroll_visible)
            self.call_after_refresh(menu.focus_active)
        except Exception as exc:
            logger.exception("Failed to mount AskUserMenu widget: %s", exc)
            self._pending_ask_user_widget = None
            if not result_future.done():
                result_future.set_result({"type": "cancelled"})
            return result_future

        return result_future

    @on(AskUserMenu.Answered)
    async def _on_ask_user_menu_answered(self, event: AskUserMenu.Answered) -> None:
        """Handle ask_user menu answers - remove widget and refocus input."""
        await self._finish_ask_user_prompt(context="answered")

    @on(AskUserMenu.Cancelled)
    async def _on_ask_user_menu_cancelled(self, event: AskUserMenu.Cancelled) -> None:
        """Handle ask_user menu cancellation - remove widget and refocus input."""
        await self._finish_ask_user_prompt(context="cancelled")

    async def _finish_ask_user_prompt(self, *, context: str = "finished") -> None:
        """Remove the AskUserMenu widget and return focus to chat input."""
        if self._pending_ask_user_widget is not None:
            try:
                await self._pending_ask_user_widget.remove()
            except Exception:
                pass
            self._pending_ask_user_widget = None
        self._focus_chat_input_after_refresh()

    async def _cancel_pending_ask_user(self, *, context: str = "cleanup") -> None:
        """Cancel and remove any mounted pending ask_user prompt."""
        if self._pending_ask_user_widget is not None:
            widget = self._pending_ask_user_widget
            self._pending_ask_user_widget = None
            try:
                widget.action_cancel()
            except Exception:
                pass
            if getattr(widget, "is_mounted", False):
                try:
                    await widget.remove()
                except Exception:
                    pass
            self._focus_chat_input_after_refresh()

    def _is_input_focused(self) -> bool:
        if not hasattr(self, "_chat_input") or not self._chat_input:
            return False
        focused = self.focused
        if focused is None:
            return False
        return focused.id == "chat-input" or focused in self._chat_input.walk_children()

    def action_approval_up(self) -> None:
        widget = self._pending_approval_widget
        if widget is not None:
            widget.action_move_up()

    def action_approval_down(self) -> None:
        widget = self._pending_approval_widget
        if widget is not None:
            widget.action_move_down()

    def action_approval_select(self) -> None:
        widget = self._pending_approval_widget
        if widget is not None:
            widget.action_select()

    def action_approval_yes(self) -> None:
        widget = self._pending_approval_widget
        if widget is not None:
            widget.action_select_approve()

    def action_approval_position(self, position: int) -> None:
        widget = self._pending_approval_widget
        if widget is not None:
            widget.action_select_position(position)

    def action_approval_smart(self) -> None:
        widget = self._pending_approval_widget
        if widget is not None:
            if hasattr(widget, "action_select_smart"):
                widget.action_select_smart()
            else:
                widget.action_select_auto()

    def action_approval_auto(self) -> None:
        self.action_approval_smart()

    def action_approval_no(self) -> None:
        widget = self._pending_approval_widget
        if widget is not None:
            widget.action_select_reject()

    def action_approval_escape(self) -> None:
        widget = self._pending_approval_widget
        if widget is not None:
            widget.action_select_reject()

    @on(ApprovalDecided)
    def _on_approval_decided(self, event: ApprovalDecided) -> None:
        if self._adapter:
            self._adapter.submit_approval(event.call_id, event.approved)
        if not event.approved and hasattr(self, "_permission_store"):
            self._permission_store.track_denied(
                tool_name=event.tool_name,
                call_id=event.call_id,
                comment=event.comment,
            )
        if self._chat_input:
            self.call_after_refresh(self._chat_input.focus)

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

    # ── Global Actions ───────────────────────────────────

    def action_interrupt(self) -> None:
        """Interrupt active agent or shell work."""
        if self._agent_running and self._adapter:
            self._adapter.cancel()
            try:
                self.query_one("#status-bar", StatusBar).set_status("Interrupted")
            except NoMatches:
                pass
        if self._shell_running:
            if self._shell_worker:
                self._shell_worker.cancel()

    def action_clear_chat(self) -> None:
        """Clear chat and start fresh (Ctrl+L)."""
        self._pending_messages.clear()
        self._queued_widgets.clear()
        self._restore_goal_rubric_state({})
        self._reset_thread_usage(0.0, 0)
        try:
            messages = self.query_one("#messages", MessageList)
            messages.clear()
        except NoMatches:
            pass

    async def action_toggle_auto_approve(self) -> None:
        """Cycle approval mode on Shift+Tab (Manual -> Auto -> Smart -> Manual)."""
        from opscloud.approval_mode import (
            ApprovalMode,
            next_approval_mode,
        )

        auto_eligible = not getattr(self, "_sandbox_active", False)
        target = next_approval_mode(
            getattr(self, "_approval_mode", "manual"),
            auto_eligible=auto_eligible,
            smart_eligible=True,
        )
        if target is None:
            self.notify("Approval mode cycling is unavailable.", severity="warning", timeout=3)
            return

        await self._set_approval_mode(target)

    async def _write_live_approval_mode(self, mode: ApprovalMode | None = None) -> bool:
        """Persist approval mode to the remote Store for the active session thread."""
        agent_obj = getattr(self, "_client", None) or getattr(self, "_agent", None)
        thread_id = getattr(self, "_agent_thread_id", None)
        if agent_obj is None or thread_id is None:
            return False
        from opscloud.approval_mode import awrite_approval_mode

        target = mode or getattr(self, "_approval_mode", ApprovalMode.MANUAL)
        try:
            live_key = await awrite_approval_mode(
                agent_obj,
                thread_id,
                mode=target,
            )
            return live_key is not None
        except Exception:
            logger.warning("Failed to write live approval-mode state to store", exc_info=True)
            return False

    async def _set_approval_mode(self, target: ApprovalMode) -> bool:
        from opscloud.approval_mode import ApprovalMode

        agent_obj = getattr(self, "_client", None) or getattr(self, "_agent", None)
        should_persist = (
            agent_obj is not None
            and getattr(self, "_agent_thread_id", None) is not None
        )
        if should_persist and not await self._write_live_approval_mode(target):
            if target in (ApprovalMode.AUTO, ApprovalMode.SMART):
                self.notify(f"{target.value.capitalize()} could not be persisted; remaining in Manual.", severity="warning")
                return False

        self._approval_mode = target
        self._auto_approve = False
        self._update_auto_mode_status_bar(target.value)

        if self._adapter:
            self._adapter._auto_approve = self._auto_approve

        if target is ApprovalMode.AUTO:
            self.notify("Switched to Auto mode (Classic LLM).", severity="information", timeout=3)
            self._notify_auto_mode_enabled_once()
        elif target is ApprovalMode.SMART:
            self.notify("Switched to Smart mode (Jev System 1 Protected).", severity="information", timeout=3)
        elif target is ApprovalMode.MANUAL:
            self.notify("Switched to Manual approval mode.", severity="information", timeout=3)
        return True

    def _notify_auto_mode_enabled_once(self) -> None:
        from opscloud.approval_mode import ApprovalMode, has_auto_mode_notice, save_auto_mode_notice
        from opscloud.ui.widgets.auto_mode_notice import AutoModeNoticeScreen

        if has_auto_mode_notice() or getattr(self, "_auto_mode_notice_pending", False):
            return

        def handle_result(accepted: bool | None) -> None:
            self._auto_mode_notice_pending = False
            if accepted is True:
                save_auto_mode_notice()
                return
            asyncio.create_task(self._set_approval_mode(ApprovalMode.MANUAL))

        try:
            self.push_screen(AutoModeNoticeScreen(), handle_result)
            self._auto_mode_notice_pending = True
        except Exception:
            logger.warning("Could not show Auto first-enable notice", exc_info=True)

    def action_toggle_tool_output(self) -> None:
        """Toggle the most recent collapsible transcript unit (SkillMessage, ToolCallMessage, etc.)."""
        from opscloud.ui.widgets.messages import MessageList, SkillMessage, ToolCallMessage
        try:
            messages = self.query_one("#messages", MessageList)
        except NoMatches:
            return
        for child in reversed(list(messages.children)):
            toggle_details_fn = getattr(child, "toggle_details", None)
            if callable(toggle_details_fn):
                toggle_details_fn()
                return
            if isinstance(child, SkillMessage) and child._stripped_body.strip():
                child.toggle_body()
                return
            if isinstance(child, ToolCallMessage):
                toggle_output_fn = getattr(child, "toggle_output", None)
                if callable(toggle_output_fn):
                    toggle_output_fn()
                    return

    def action_toggle_debug_console(self) -> None:
        """Toggle the Debug Console overlay via keybind or the `/debug` command."""
        from opscloud.ui.widgets.debug_console import DebugConsoleScreen

        if isinstance(self.screen, DebugConsoleScreen):
            self.pop_screen()
            if self._chat_input:
                self._chat_input.focus_input()
            return
        self._open_debug_console()

    async def action_open_editor(self) -> None:
        """Open the focused editable surface in $VISUAL/$EDITOR."""
        chat_input = getattr(self, "_chat_input", None)
        if not chat_input or not getattr(chat_input, "_text_area", None):
            return

        await self._open_text_area_in_editor(
            chat_input._text_area,
            chat_input._text_area.text or "",
            allow_empty=False,
            raise_editor_errors=False,
            restore_focus=chat_input.focus_input,
        )

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
        """Edit text externally, then restore originating field's focus."""
        from opscloud.editor import ExternalEditorError, open_in_editor

        try:
            with self.suspend():
                edited = open_in_editor(
                    current_text,
                    allow_empty=allow_empty,
                    raise_on_error=raise_editor_errors,
                )
        except ExternalEditorError:
            logger.warning("External editor failed", exc_info=True)
            self.notify(
                "External editor failed. Check $VISUAL/$EDITOR.",
                severity="error",
                timeout=5,
            )
        else:
            if edited is not None:
                text_area.text = edited
                if reset_after_edit is not None:
                    reset_after_edit()
                lines = edited.split("\n")
                text_area.move_cursor((len(lines) - 1, len(lines[-1])))
        finally:
            restore_focus()

    def action_open_notifications(self) -> None:
        """Open or toggle the notification center overlay via Ctrl+N."""
        self._open_notification_center()

    def _open_notification_center(self) -> None:
        """Toggle the NotificationCenter panel overlay."""
        from opscloud.ui.widgets.notification_center import NotificationCenter

        try:
            nc = self.query(NotificationCenter)
            if nc:
                nc.first().remove()
            else:
                self.mount(NotificationCenter())
        except Exception:
            logger.warning("Failed to toggle NotificationCenter", exc_info=True)

    def _buffer_shell_for_model_context(
        self, command: str, output: str, returncode: int | None
    ) -> None:
        """Buffer a non-incognito `!` command/output for the next user send."""
        from langchain_core.messages import HumanMessage

        code = returncode if returncode is not None else "unknown"
        body = output or "(no output)"
        content = (
            "<user_shell_command>\n"
            "<command>\n"
            f"{command}\n"
            "</command>\n"
            "<result>\n"
            f"Exit code: {code}\n"
            "Output:\n"
            f"{body}\n"
            "</result>\n"
            "</user_shell_command>"
        )
        self._pending_shell_messages.append(HumanMessage(content=content))

    async def _flush_pending_shell_messages(self) -> None:
        """Write buffered `!` command/output into thread state, then clear it."""
        if not self._pending_shell_messages:
            return
        thread_id = getattr(self, "_agent_thread_id", None) or getattr(
            getattr(self, "_session_state", None), "thread_id", None
        )
        agent_obj = getattr(self, "_agent", None)
        if not agent_obj or not thread_id:
            return

        messages = self._pending_shell_messages
        self._pending_shell_messages = []
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        try:
            await agent_obj.aupdate_state(config, {"messages": messages})
        except Exception:
            logger.warning(
                "Failed to flush pending shell messages to model context", exc_info=True
            )

    def _open_debug_console(self) -> None:
        """Push the read-only Debug Console modal."""
        from opscloud.ui.widgets.debug_console import DebugConsoleScreen

        def handle_result(_: None) -> None:
            if self._chat_input:
                self._chat_input.focus_input()

        def persist_clear(cursor: int) -> None:
            self._debug_console_cleared_upto = cursor

        self.push_screen(
            DebugConsoleScreen(
                self._build_debug_snapshot(),
                snapshot_provider=self._build_debug_snapshot,
                cleared_upto=self._debug_console_cleared_upto,
                on_clear=persist_clear,
                click_to_copy=self._debug_console_click_to_copy,
                on_click_to_copy_change=self._persist_debug_console_click_to_copy,
            ),
            handle_result,
        )

    def _persist_debug_console_click_to_copy(self, enabled: bool) -> None:
        self._debug_console_click_to_copy = enabled

    def _build_debug_snapshot(self) -> list[Any]:
        """Capture a session/runtime snapshot for the debug console header."""
        from opscloud._debug import installed_debug_log_path
        from opscloud.config.env_vars import DEBUG, is_env_truthy
        from opscloud._version import __version__
        from opscloud.ui.widgets.debug_console import SnapshotField
        from pathlib import Path

        def _safe(
            label: str, fn: Callable[[], str], *, copyable: bool = False
        ) -> SnapshotField:
            try:
                return SnapshotField(label=label, value=fn(), copyable=copyable)
            except Exception as exc:
                logger.warning("Debug snapshot field %r failed", label, exc_info=True)
                return SnapshotField(
                    label=label, value=f"(unavailable: {type(exc).__name__})"
                )

        def _mcp() -> str:
            servers = self._mcp_server_info or []
            if not servers:
                return "none"
            return ", ".join(f"{s.name} ({s.status})" for s in servers)

        def _log_path() -> str:
            path = installed_debug_log_path()
            if path:
                return str(path)
            if is_env_truthy(DEBUG):
                return "in-memory only (file logging requested but unavailable)"
            return "in-memory only"

        return [
            _safe("Version", lambda: __version__, copyable=True),
            _safe("Thread", lambda: getattr(self, "_agent_thread_id", "(none)") or "(none)", copyable=True),
            _safe("CWD", lambda: str(Path.cwd()), copyable=True),
            _safe("MCP servers", _mcp),
            _safe("Debug log", _log_path),
        ]

    # ── Theme Selector Integration ────────────────────────

    async def _show_theme_selector(self) -> None:
        """Show interactive theme selector modal screen."""
        def handle_result(result: str | None) -> None:
            if result is not None and result in get_registry():
                self.theme = result
                save_theme_preference(result)
                show_toast(self, f"Switched theme to `{result}`", severity="information")
            if self._chat_input:
                self._chat_input.focus_input()


        screen = ThemeSelectorScreen(current_theme=self.theme)
        self.push_screen(screen, handle_result)

    async def _switch_theme(self, name: str) -> None:
        """Switch theme directly by name or alias."""
        registry = get_registry()
        target = name.strip()

        matched = None
        if target in registry:
            matched = target
        else:
            for key, entry in registry.items():
                if key.lower() == target.lower() or entry.label.lower() == target.lower():
                    matched = key
                    break

        if matched:
            self.theme = matched
            save_theme_preference(matched)
            await self._mount_message(
                SystemMessage(f"Switched to `{matched}` theme")
            )
        else:
            avail = ", ".join(f"`{k}`" for k in list(registry.keys())[:8])
            await self._mount_message(
                ErrorMessage(f"Unknown theme `{name}`. Available themes include: {avail}...")
            )

    # ── Thread Selector Integration ───────────────────────

    async def _show_thread_selector(self) -> None:
        """Open the interactive thread selector modal screen."""
        from opscloud.ui.widgets.thread_selector import ThreadSelectorScreen

        from pathlib import Path
        from opscloud.state.session import SessionManager

        threads = []
        sm = None
        session_state = getattr(self, "_session_state", None)
        if session_state and hasattr(session_state, "session_manager"):
            sm = session_state.session_manager
        else:
            from opscloud.state.session import get_db_path
            db_path = get_db_path()
            if db_path.exists():
                sm = SessionManager(db_path)

        if sm is not None:
            try:
                raw_threads = await sm.list_threads(limit=50)
                threads = [
                    {
                        "thread_id": t.get("thread_id"),
                        "created_at": t.get("created_at"),
                        "updated_at": t.get("updated_at"),
                        "message_count": t.get("message_count", 0),
                        "initial_prompt": t.get("initial_prompt", ""),
                    }
                    for t in raw_threads
                ]
            except Exception as exc:
                logger.debug("Failed fetching threads for selector: %s", exc)

        screen = ThreadSelectorScreen(
            threads=threads,
            current_thread_id=self._agent_thread_id,
        )

        def _on_thread_selected(selected_id: str | None) -> None:
            if selected_id == "__new__":
                import uuid
                new_id = str(uuid.uuid4())
                self.run_worker(self.resume_thread(new_id, is_new=True))
            elif selected_id:
                self.run_worker(self.resume_thread(selected_id, is_new=False))

        self.push_screen(screen, _on_thread_selected)

    async def get_recent_threads(self, limit: int = 20) -> list:
        """Get list of recent threads from session manager."""
        from pathlib import Path
        from opscloud.state.session import SessionManager

        sm = None
        session_state = getattr(self, "_session_state", None)
        if session_state and hasattr(session_state, "session_manager"):
            sm = session_state.session_manager
        else:
            from opscloud.state.session import get_db_path
            db_path = get_db_path()
            if db_path.exists():
                sm = SessionManager(db_path)

        if sm is not None:
            try:
                return await sm.list_threads(limit=limit)
            except Exception:
                pass
        return []


    # ── Cloud Profile Selector Integration ─────────────────

    def _sync_status_cloud(self) -> None:
        """Propagate current AWS cloud profile and region to status bar and banner."""
        from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region

        try:
            profile = get_active_aws_profile()
            region = get_active_aws_region() or ""
        except Exception:
            return

        # Update status bar
        try:
            status_bar = self._status_bar or self.query_one("#status-bar", StatusBar)
            status_bar.set_cloud(profile=profile, region=region)
        except Exception:
            pass

        # Update welcome banner if present
        try:
            banner = self.query_one(WelcomeBanner)
            banner.update_cloud(profile=profile, region=region)
        except Exception:
            pass

    async def _show_cloud_selector(self) -> None:
        """Open the interactive cloud profile picker modal."""
        from opscloud.config.cloud_profiles import get_active_aws_profile
        from opscloud.ui.widgets.cloud_selector import CloudProfileSelectorScreen

        current_profile = get_active_aws_profile()
        screen = CloudProfileSelectorScreen(current_profile=current_profile)

        def _on_cloud_selected(result: tuple[str, str | None] | None) -> None:
            if not result:
                return
            profile, region = result
            self.run_worker(
                self._switch_cloud_profile(profile, region=region),
                exclusive=False,
                group="cloud-switch",
            )

        self.push_screen(screen, _on_cloud_selected)

    async def _switch_cloud_profile(self, profile: str, region: str | None = None) -> None:
        """Switch the active AWS profile and persist across sessions."""
        from opscloud.config.cloud_profiles import get_active_aws_region, set_active_aws_profile
        from opscloud.ui.widgets.messages import SystemMessage

        set_active_aws_profile(profile, region=region, persist=True)

        effective_region = region or get_active_aws_region() or ""

        settings_obj = getattr(self, "_settings", None)
        if settings_obj is not None:
            settings_obj.aws_profile = profile
            settings_obj.aws_region = effective_region or "us-east-1"

        # Update status bar & welcome banner
        self._sync_status_cloud()

        region_info = f" ({effective_region})" if effective_region else ""
        await self._mount_message(
            SystemMessage(
                f"Switched active AWS profile to **`{profile}`**{region_info} "
                "(saved to `config.toml` — active across all sessions)."
            )
        )

    # ── Model Selector Integration ────────────────────────

    async def _show_model_selector(self) -> None:
        """Open the interactive model picker modal."""
        from opscloud.ui.widgets.model_selector import ModelSelectorScreen
        from opscloud.model.config import resolve_model_spec

        current_provider, current_model = None, None
        if self._model:
            try:
                current_provider, current_model = resolve_model_spec(self._model)
            except Exception:
                current_model = self._model
        
        settings_obj = getattr(self, "settings", None)
        current_effort = getattr(self, "_reasoning_effort", None) or (getattr(settings_obj, "reasoning_effort", None) if settings_obj else None)

        screen = ModelSelectorScreen(
            current_model=current_model,
            current_provider=current_provider,
            current_effort=current_effort,
        )

        def _on_model_selected(result: tuple[str, str, str | None] | tuple[str, str] | None) -> None:
            """Callback when the model selector is dismissed."""
            if result is None:
                return
            spec = result[0]
            effort = result[2] if len(result) > 2 else None
            extra = screen.pending_install_extra
            if extra:
                # Run in a worker so modal screens pushed during installation
                # remain interactive (call_later would block the message pump).
                self.run_worker(
                    self._install_extra_then_switch(extra, spec, effort=effort),
                    exclusive=False,
                    group="model-install-switch",
                )
            else:
                self.run_worker(
                    self._switch_model(spec, effort=effort),
                    exclusive=False,
                    group="model-switch",
                )

        self.push_screen(screen, _on_model_selected)

    async def _install_extra_then_switch(self, extra: str, spec: str, effort: str | None = None) -> None:
        """Install provider package extra, then check auth and switch model.

        Stops on install failure rather than falling through to auth/switch.
        After successful install, prompts for credentials if needed before
        performing the model switch.
        """
        await self._mount_message(
            SystemMessage(f"Installing '{extra}' integration package...")
        )

        pkg_name = f"langchain-{extra}"
        install_ok = False
        try:
            def _run_install():
                import shutil
                import subprocess
                import sys

                # Try pip first
                pip_res = subprocess.run(
                    [sys.executable, "-m", "pip", "install", pkg_name],
                    capture_output=True,
                    text=True,
                )
                if pip_res.returncode == 0:
                    return pip_res

                # pip failed (possibly not installed) — fall back to uv
                uv_bin = shutil.which("uv")
                if uv_bin:
                    return subprocess.run(
                        [uv_bin, "pip", "install", pkg_name],
                        capture_output=True,
                        text=True,
                    )

                # Neither pip nor uv succeeded
                return pip_res

            res = await asyncio.to_thread(_run_install)
            if res.returncode == 0:
                install_ok = True
                await self._mount_message(
                    SystemMessage(f"Successfully installed '{extra}' integration package.")
                )
            else:
                logger.warning("install %s failed with code %d: %s", pkg_name, res.returncode, res.stderr)
                await self._mount_message(
                    ErrorMessage(f"Failed to install '{extra}'. Check logs for details.")
                )
        except Exception as e:
            logger.warning("Failed to install package extra %s: %s", extra, e)
            await self._mount_message(
                ErrorMessage(f"Failed to install '{extra}': {e}")
            )

        if not install_ok:
            return

        # Package installed — now check if credentials are needed before switching.
        auth_ok = await self._prompt_model_auth_if_needed(spec)
        if not auth_ok:
            # User cancelled auth. Let them know the extra is installed so they
            # can switch later once a key is set.
            await self._mount_message(
                SystemMessage(
                    f"Installed '{extra}'. Switch to {spec} anytime with "
                    "`/model` — you'll be prompted for credentials."
                )
            )
            return

        await self._finalize_model_switch(spec, effort=effort)

    async def _prompt_model_auth_if_needed(self, spec: str) -> bool:
        """Prompt for missing credentials before switching to a model.

        Returns True when switching can continue, False when the user did not
        save required credentials.
        """
        from opscloud.model.config import (
            get_credential_env_var,
            get_provider_auth_status,
        )
        from opscloud.ui.widgets.auth import AuthPromptScreen, AuthResult

        provider = spec.split(":", 1)[0] if ":" in spec else "openai"
        status = get_provider_auth_status(provider)
        if status.as_legacy_bool():
            return True

        env_var = status.env_var or get_credential_env_var(provider) or f"{provider.upper()}_API_KEY"

        # Use an asyncio.Future to wait for the modal result in this worker
        # without blocking the App message pump.
        loop = asyncio.get_running_loop()
        auth_future: asyncio.Future[AuthResult | None] = loop.create_future()

        def _on_auth_done(result: AuthResult | None) -> None:
            if not auth_future.done():
                loop.call_soon_threadsafe(auth_future.set_result, result)

        self.app.push_screen(
            AuthPromptScreen(
                provider,
                env_var,
                reason=f"Required to use {spec}",
            ),
            _on_auth_done,
        )

        result = await auth_future
        return result is AuthResult.SAVED

    async def _switch_model(self, spec: str, effort: str | None = None) -> None:
        """Switch the active model, checking auth if needed.

        Package installation is NOT checked here — callers are responsible for
        ensuring the provider package is installed before calling this method
        (either via the model selector's install flow, or because the package
        was already present).
        """
        if ":" in spec:
            provider = spec.split(":", 1)[0]
        else:
            from opscloud.model.factory import detect_provider
            provider = detect_provider(spec) or "openai"

        # Check auth before switching
        auth_ok = await self._prompt_model_auth_if_needed(spec)
        if not auth_ok:
            return

        await self._finalize_model_switch(spec)

    async def _finalize_model_switch(self, spec: str, effort: str | None = None) -> None:
        """Perform the actual model switch and persist to recents.

        This is the final step after package installation and auth checks have
        passed. Sets the model, updates the status bar, and persists to recents.
        """
        from opscloud.model.config import save_recent_model, apply_stored_credentials

        if spec in ("auto", "dynamic"):
            import os
            os.environ["OPSCODE_SERVER_MODEL"] = spec
            os.environ["OPSCODE_MODEL_NAME"] = spec
            self._model = spec
            try:
                status_bar = self.query_one("#status-bar", StatusBar)
                status_bar.set_model(provider="typesafe", model="jev-router", effort="auto")
            except NoMatches:
                pass
            await self._mount_message(
                SystemMessage("Switched to **Autonomous Tier Routing (TypeSafe Jev System 1)**.")
            )
            return

        provider = spec.split(":", 1)[0] if ":" in spec else ""
        if provider:
            apply_stored_credentials(provider)

        import os
        os.environ["OPSCODE_SERVER_MODEL"] = spec
        os.environ["OPSCODE_MODEL_NAME"] = spec
        self._model = spec
        if effort is not None:
            self._reasoning_effort = effort
            os.environ["OPSCODE_REASONING_EFFORT"] = effort
            settings_obj = getattr(self, "settings", None)
            if settings_obj:
                settings_obj.reasoning_effort = effort

        eff_val = effort or getattr(self, "_reasoning_effort", None)
        if not eff_val and spec:
            from opscloud.model.reasoning import default_effort_for_model
            eff_val = default_effort_for_model(spec) or ""
        eff_display: str = str(eff_val) if eff_val else ""

        if self._adapter:
            pass
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            _prov, _mdl = _split_model_spec(spec)
            status_bar.set_model(provider=_prov, model=_mdl, effort=eff_display)
            from opscloud.model.config import resolve_model_context_limit

            _ctx_limit = resolve_model_context_limit(spec)
            if _ctx_limit:
                status_bar.set_context_limit(_ctx_limit)
                settings_obj = getattr(self, "settings", None)
                if settings_obj:
                    settings_obj.model_context_limit = _ctx_limit
        except NoMatches:
            pass

        # Persist to recent models
        save_recent_model(spec)

        msg = f"Switched model to `{spec}`"
        if eff_display:
            msg += f" (thinking level: `{eff_display}`)"
        await self._mount_message(
            SystemMessage(msg)
        )

        if self._server_startup_deferred:
            self._server_startup_deferred = False
            self._connecting = True
            self._server_kwargs = {
                "assistant_id": self._assistant_id,
                "model_name": spec,
                "auto_approve": self._auto_approve,
            }
            try:
                status_bar = self.query_one("#status-bar", StatusBar)
                status_bar.set_status("Connecting...")
            except Exception:
                pass
            await self._mount_message(
                SystemMessage(
                    f"⏳ **Model configured!** Starting agent server for model `{spec}`..."
                )
            )
            self.run_worker(self._start_server_background(), group="server-startup")

    # ── Effort Selector Integration ───────────────────────

    async def _show_effort_selector(self) -> None:
        """Open the interactive reasoning effort picker modal."""
        from opscloud.model.reasoning import (
            default_effort_for_model,
            supported_efforts_for_model,
        )
        from opscloud.ui.widgets.effort_selector import EffortSelectorScreen

        model_spec = self._model or "default"
        efforts = supported_efforts_for_model(model_spec) or ("low", "medium", "high")
        current_eff = getattr(self, "_reasoning_effort", None) or getattr(getattr(self, "settings", None), "reasoning_effort", None)
        default_eff = default_effort_for_model(model_spec) or "high"

        screen = EffortSelectorScreen(
            model_spec=model_spec,
            efforts=efforts,
            current_effort=current_eff,
            default_effort=default_eff,
        )

        def _on_effort_selected(result: str | None) -> None:
            if result is not None:
                self.run_worker(self._set_effort_override(result))

        self.push_screen(screen, _on_effort_selected)

    # ── Pool Selector Integration ─────────────────────────

    async def _show_pool_selector(self) -> None:
        """Open the interactive agent model pool configuration modal."""
        from opscloud.model.config import detect_provider
        from opscloud.ui.widgets.pool_selector import PoolSelectorScreen

        settings_obj = getattr(self, "settings", None)
        current_spec = getattr(self, "_model", None) or (getattr(settings_obj, "model", None) if settings_obj else None)
        provider = detect_provider(current_spec or "") or getattr(settings_obj, "model_provider", None) or "google_genai"

        screen = PoolSelectorScreen(active_provider=provider)

        def _on_pool_selected(result: dict[str, str] | None) -> None:
            if result is None:
                return
            if result.get("_cleared"):
                self.notify("Agent model pool cleared (dynamic discovery restored)", severity="information")
                self.run_worker(
                    self._mount_message(
                        SystemMessage("Agent model pool cleared. Reverted to automatic dynamic discovery.")
                    )
                )
                return

            fast = result.get("fast", "")
            std = result.get("standard", "")
            pow_m = result.get("powerful", "")
            self.notify("Agent model pool saved to config.toml", severity="information")
            msg = (
                f"**Agent Model Pool Saved (`config.toml [agent_pool]`):**\n"
                f"• **Fast (Tier 0):** `{fast}`\n"
                f"• **Standard (Tier 1):** `{std}`\n"
                f"• **Powerful (Tier 2):** `{pow_m}`\n"
                f"In smart mode, Jev dynamically routes requests across these pool models."
            )
            self.run_worker(self._mount_message(SystemMessage(msg)))

        self.push_screen(screen, _on_pool_selected)

    async def _set_effort_override(self, effort: str) -> None:
        """Apply reasoning effort override and update UI."""
        import os
        from opscloud.ui.widgets.messages import SystemMessage
        from opscloud.ui.widgets.status import StatusBar

        model_spec = self._model or "default"

        if effort.lower() in {"clear", "reset"}:
            self._reasoning_effort = None
            os.environ.pop("OPSCODE_REASONING_EFFORT", None)
            settings_obj = getattr(self, "settings", None)
            if settings_obj:
                settings_obj.reasoning_effort = None
            try:
                sb = self.query_one("#status-bar", StatusBar)
                _prov, _mdl = _split_model_spec(model_spec)
                from opscloud.model.reasoning import default_effort_for_model
                def_eff = default_effort_for_model(model_spec) or ""
                sb.set_model(provider=_prov, model=_mdl, effort=def_eff)
            except Exception:
                pass
            await self._mount_message(SystemMessage(f"Reasoning effort override cleared for `{model_spec}`."))
            return

        self._reasoning_effort = effort
        os.environ["OPSCODE_REASONING_EFFORT"] = effort
        settings_obj = getattr(self, "settings", None)
        if settings_obj:
            settings_obj.reasoning_effort = effort

        try:
            sb = self.query_one("#status-bar", StatusBar)
            _prov, _mdl = _split_model_spec(model_spec)
            sb.set_model(provider=_prov, model=_mdl, effort=effort)
        except Exception:
            pass

        await self._mount_message(SystemMessage(f"Reasoning effort for `{model_spec}` set to `{effort}`."))

    # ── Agent Selector Integration ────────────────────────

    async def _show_agent_selector(self) -> None:
        """Open the interactive agent picker modal."""
        from opscloud.agent.config import get_available_agent_names, load_default_agent

        agent_names = get_available_agent_names()
        default_agent = load_default_agent()

        def _on_agent_selected(result: str | None) -> None:
            """Callback when the agent selector is dismissed."""
            if result is not None and result != self._assistant_id:
                self._switch_agent(result)
            # Refocus input
            if self._chat_input:
                self._chat_input.focus()

        from opscloud.ui.widgets.agent_selector import AgentSelectorScreen

        screen = AgentSelectorScreen(
            current_agent=self._assistant_id,
            agent_names=agent_names,
            default_agent=default_agent,
        )
        self.push_screen(screen, _on_agent_selected)

    def _switch_agent(self, agent_name: str) -> None:
        """Switch to a different agent and start a new thread.

        Clears the chat, updates the assistant_id, creates a new thread,
        and posts a confirmation message.

        Args:
            agent_name: The agent to switch to.
        """
        import uuid

        if agent_name == self._assistant_id:
            return

        # Guard: don't switch while agent is running
        if self._agent_running or self._shell_running:
            self.notify(
                "Cannot switch agents while a task is running. "
                "Interrupt or wait for it to finish first.",
                severity="warning",
            )
            return

        # Update identity
        old_agent = self._assistant_id
        self._assistant_id = agent_name

        # Clear chat and start new thread
        self._pending_messages.clear()
        self._queued_widgets.clear()
        try:
            messages = self.query_one("#messages", MessageList)
            messages.clear()
        except NoMatches:
            pass

        self._agent_thread_id = str(uuid.uuid4())
        bind_logging_to_thread(self._agent_thread_id)

        # Update adapter if present
        if self._adapter:
            self._adapter._assistant_id = agent_name

        # Update status bar
        try:
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.set_status("Ready")
        except NoMatches:
            pass

        # Post confirmation
        self.call_later(
            self._mount_message,
            SystemMessage(f"Switched to {agent_name}. New thread started."),
        )

        logger.info("Agent switched: %s → %s", old_agent, agent_name)

    # ── Plugin Manager ───────────────────────────────────

    async def _show_plugin_manager(self) -> None:
        """Push the plugin manager modal screen for /plugins."""
        from opscloud.ui.widgets.plugin_manager import PluginManagerScreen

        mcp_info = self.get_mcp_servers() if hasattr(self, "get_mcp_servers") else ()
        project_root = getattr(self._settings, "project_root", None) if self._settings else None
        screen = PluginManagerScreen(
            mcp_server_info=mcp_info,
            project_root=project_root,
        )
        self.push_screen(screen)

    # ── Skills Viewer ────────────────────────────────────

    def _open_skills_viewer(self) -> None:
        """Push the interactive skills viewer modal screen."""
        from opscloud.ui.widgets.skills_viewer import SkillsViewerScreen

        skills = self.get_discovered_skills()
        self.push_screen(SkillsViewerScreen(skills=skills))

    # ── Goal Review ──────────────────────────────────────

    def _focus_chat_input_after_refresh(self) -> None:
        """Restore chat input focus after an inline prompt is removed or turn completes.

        """
        if self._chat_input:
            self.call_after_refresh(self._chat_input.focus_input)
            self.set_timer(0.05, self._chat_input.focus_input)

    @on(Worker.StateChanged)
    def _on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Surface worker state changes and restore focus when agent tasks finish.

        """
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
        """Cancel and remove any mounted pending goal review prompt.

        """
        logger.debug("Cancelling pending goal review (context=%s)", context)
        widget = self._pending_goal_review_widget
        future = self._pending_goal_review_future
        self._pending_goal_review_widget = None
        self._pending_goal_review_future = None

        if widget is not None:
            try:
                widget.action_cancel()
            except Exception:
                pass
        if future is not None and not future.done():
            future.cancel()
        if widget is not None and getattr(widget, "is_mounted", False):
            try:
                widget.remove()
            except Exception:
                pass
        self._focus_chat_input_after_refresh()

    def _open_goal_review(self, objective: str, rubric: str, amendment: bool = False) -> None:
        """Mount the inline goal review widget into messages for user review of criteria.

        """
        from opscloud.ui.widgets.goal_review import GoalReviewMenu

        try:
            messages = self.query_one("#messages", MessageList)
        except NoMatches:
            logger.warning("Cannot mount goal review menu: #messages container not found")
            return

        if self._pending_goal_review_widget is not None:
            try:
                self._pending_goal_review_widget.remove()
            except Exception:
                pass

        unique_id = f"goal-review-menu-{uuid.uuid4().hex[:8]}"
        logger.debug("Mounting GoalReviewMenu id=%s for objective=%r", unique_id, objective[:40])
        menu = GoalReviewMenu(
            objective,
            rubric,
            amendment=amendment,
            id=unique_id,
        )
        self._pending_goal_review_widget = menu
        self.run_worker(messages.mount_inline_prompt(menu))

    @on(GoalReviewMenu.Decided)
    async def _on_goal_review_decided(self, event: GoalReviewMenu.Decided) -> None:
        """Handle a goal review decision by processing result and focusing chat input.

        """
        if self._pending_goal_review_widget is event.widget:
            self._pending_goal_review_widget = None

        if event.widget:
            try:
                event.widget.remove()
            except Exception:
                pass

        result = event.result
        res_type = result.get("type")
        logger.debug("GoalReviewMenu decision received: %s", res_type)

        from opscloud.commands.power.goal import GoalHandler, get_goal_state

        state = get_goal_state(self)
        objective = state.pending_objective or state.objective or ""
        prev_rubric = state.pending_rubric or state.rubric or ""

        if res_type == "accepted":
            asyncio.ensure_future(self._accept_goal_rubric(prev_rubric))
        elif res_type == "edited":
            new_criteria: str = str(result.get("criteria", prev_rubric))
            asyncio.ensure_future(self._accept_goal_rubric(new_criteria))
        elif res_type == "rejected":
            feedback: str = str(result.get("message", ""))
            state.pending_objective = None
            state.pending_rubric = None
            GoalHandler._sync_status_rubric(self, state)

            req = {
                "kind": "create",
                "request_id": str(uuid.uuid4()),
                "objective": objective,
                "feedback": feedback,
                "previous_criteria": prev_rubric,
            }
            asyncio.ensure_future(self._run_goal_criteria_request(req))
        elif res_type == "cancelled":
            state.pending_objective = None
            state.pending_rubric = None
            GoalHandler._sync_status_rubric(self, state)

        self._focus_chat_input_after_refresh()

    def _handle_rubric_evaluation_end(self, rubric_msg: dict[str, Any]) -> None:
        """Handle grader evaluation completion by updating GoalState and status bar.

        """
        from opscloud.commands.power.goal import GoalHandler, get_goal_state
        state = get_goal_state(self)

        result = str(rubric_msg.get("result", "")).lower()
        if state.objective:
            logger.debug("Handling rubric_evaluation_end: result=%s for objective=%r", result, state.objective[:40])
            if result in ("satisfied", "passed", "complete"):
                state.status = "complete"
                if not state.status_note:
                    state.status_note = "Acceptance criteria satisfied."
            elif result in ("unsatisfied", "failed", "blocked"):
                state.status = "blocked"

        if state.next_rubric:
            state.next_rubric = None

        GoalHandler._sync_status_rubric(self, state)
        self._focus_chat_input_after_refresh()



    # ── Skill Command ────────────────────────────────────

    async def _handle_skill_command(self, command: str) -> None:
        """Handle a /skill:<name> command by invoking the skill directly."""
        from opscloud.skills.invocation import parse_skill_command

        skill_name, args = parse_skill_command(command)
        await self._invoke_skill(skill_name, args, command=command)

    async def _invoke_skill(
        self,
        skill_name: str,
        args: str = "",
        *,
        command: str | None = None,
    ) -> None:
        """Load a skill, render its widget, and send its prompt to the agent.

        """
        from opscloud.skills.invocation import build_skill_invocation_envelope
        from opscloud.skills.loader import load_skill_content
        from opscloud.ui.widgets.messages import AppMessage, ErrorMessage, SkillMessage, UserMessage

        normalized_name = skill_name.strip().lower()

        async def _mount_error(message: str) -> None:
            if command is not None:
                await self._mount_message(UserMessage(command))
            await self._mount_message(AppMessage(message))

        if not normalized_name:
            if command is not None:
                await self._mount_message(UserMessage(command))
                await self._mount_message(AppMessage("Usage: /skill:<name> [args]"))
            else:
                await self._mount_message(AppMessage("Skill name is required."))
            return

        skills = self._discovered_skills or self.get_discovered_skills()
        cached = next(
            (
                s for s in skills
                if str(s.get("name", "")).lower() == normalized_name
                or (":" in str(s.get("name", "")) and str(s.get("name", "")).rsplit(":", 1)[-1].lower() == normalized_name)
            ),
            None,
        )
        if cached is None and self._discovered_skills:
            # Fall back to fresh discovery on cache miss
            skills = self.get_discovered_skills()
            self._discovered_skills = skills
            cached = next(
                (
                    s for s in skills
                    if str(s.get("name", "")).lower() == normalized_name
                    or (":" in str(s.get("name", "")) and str(s.get("name", "")).rsplit(":", 1)[-1].lower() == normalized_name)
                ),
                None,
            )

        if cached is None:
            logger.warning("Skill not found: %r", normalized_name)
            await _mount_error(f"Skill not found: {normalized_name}")
            return

        skill_path = str(cached.get("path", ""))
        try:
            content = load_skill_content(skill_path)
        except Exception as exc:
            logger.warning("Error reading skill %r: %s", normalized_name, exc)
            await _mount_error(f"Error loading skill: {normalized_name}. {exc}")
            return

        if not content or not content.strip():
            await _mount_error(
                f"Skill '{normalized_name}' has an empty SKILL.md file. "
                "Add instructions to the file before invoking."
            )
            return

        envelope = build_skill_invocation_envelope(cached, content, args)

        await self._mount_message(
            SkillMessage(
                skill_name=str(cached.get("name", "")),
                description=str(cached.get("description", "")),
                source=str(cached.get("source", "built-in")),
                body=content,
                args=args,
            )
        )
        await self._send_to_agent(envelope.prompt)

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

    def action_quit_app(self) -> None:
        self.exit()
