"""Server lifecycle, startup trust, reload, restart, and package update controller."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from dataclasses import dataclass, is_dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from textual.css.query import NoMatches

from opscloud.mcp.mcp_info import MCPServerInfo
from opscloud.ui.widgets.messages import CommandOutputMessage, ErrorMessage, SystemMessage, UserMessage
from opscloud.ui.widgets.status import StatusBar
from opscloud.ui.widgets.welcome import WelcomeBanner
from opscloud.utils.logger import bind_logging_to_thread, get_logger

if TYPE_CHECKING:
    from opscloud.ui.app import OpsCloudApp

logger = get_logger(__name__)

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
            f"`{name}` ({before[name].status} {glyphs.arrow_right} {servers[name].status})"
            for name in status_changed
        )
        lines.append(f"  - **Status changed:** {transitions}")
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


class ServerLifecycleController:
    """Manages workspace trust, background server lifecycle, `/reload`, `/restart`, `/install`, and `/update`."""

    def __init__(self, app: OpsCloudApp) -> None:
        self._app = app

    async def evaluate_startup_trust_and_settings(self) -> None:
        """Evaluate CLI settings validity and startup workspace folder trust."""
        from opscloud.config.cli_settings import get_cli_settings_manager
        from opscloud.ui.widgets.workspace_trust import SettingsErrorScreen

        app = self._app
        mgr = get_cli_settings_manager()
        mgr.init_cli_settings()

        if mgr.settings_load_error is not None:
            if app._status_bar is not None:
                app._status_bar.set_status("Settings error")

            def _on_settings_error_decided(quarantine: bool | None) -> None:
                if quarantine is True:
                    mgr.quarantine_cli_settings()
                    app.run_worker(
                        app._prompt_workspace_trust_if_needed(),
                        group="workspace-trust-gate",
                    )
                else:
                    app._exit = True
                    app.exit()

            app.push_screen(
                SettingsErrorScreen(mgr.settings_path, mgr.settings_load_error),
                _on_settings_error_decided,
            )
            return

        await app._prompt_workspace_trust_if_needed()

    async def prompt_workspace_trust_if_needed(self) -> None:
        """Check whether the startup workspace is trusted; push WorkspaceTrustScreen if not."""
        from opscloud.config.cli_settings import get_cli_settings_manager
        from opscloud.ui.widgets.workspace_trust import WorkspaceTrustScreen

        app = self._app
        mgr = get_cli_settings_manager()
        trusted = mgr.init_workspace_dirs(app._cwd)
        if trusted or mgr.workspace_trusted(app._cwd):
            app._workspace_trusted = True
            await app._continue_post_trust_startup()
            return

        app._workspace_trusted = False
        if app._status_bar is not None:
            app._status_bar.set_status("Workspace trust required")

        def _on_workspace_trust_decided(accepted: bool | None) -> None:
            if accepted is True:
                mgr.trust_workspace(app._cwd)
                app._workspace_trusted = True
                try:
                    from opscloud.config.settings import _load_dotenv

                    _load_dotenv(start_path=Path(app._cwd), refresh_loaded=True)
                except Exception:
                    logger.debug("Dotenv refresh after workspace trust failed", exc_info=True)
                app.run_worker(
                    app._continue_post_trust_startup(),
                    group="post-trust-startup",
                )
            else:
                app._exit = True
                app.exit()

        app.push_screen(
            WorkspaceTrustScreen(app._cwd),
            _on_workspace_trust_decided,
        )

    async def continue_post_trust_startup(self) -> None:
        """Run skill discovery, background server startup, and initial goal after workspace trust is satisfied."""
        app = self._app
        status_bar = app._status_bar or app.query_one("#status-bar", StatusBar)

        if app._chat_input is not None:
            app._chat_input.focus()

        app.run_worker(app._discover_startup_skills(), group="skill-discovery")

        if app._server_startup_deferred:
            status_bar.set_status("No model configured")
            await app._mount_message(
                SystemMessage(
                    "No model is configured yet. Run `/model` to choose one.\n\n"
                    "OpsCloud will ask for credentials for the selected provider."
                )
            )
        elif app._connecting:
            status_bar.set_status("Connecting...")
            app._startup_sequence_running = True
            app.run_worker(app._start_server_background(), group="server-startup")
        elif app._client:
            if app._adapter:
                app._adapter.set_client(app._client)
            await app._finalize_connection(app._client)
            try:
                banner = app.query_one(WelcomeBanner)
                banner.set_connected()
            except Exception:
                pass
            if not app._mcp_server_info and not app._mcp_preload_kwargs.get("no_mcp"):
                app.run_worker(app._preload_mcp_metadata_background(), group="mcp-preload")
        else:
            app._agent_thread_id = "local_session"
            await app._mount_message(
                SystemMessage(
                    "**OpsCloud TUI Ready** (offline) — "
                    "Type `/help` for slash commands."
                )
            )

        if app._goal:
            app.call_later(app._send_goal)

    async def maybe_start_deferred_server(self) -> bool:
        """Start the background server if startup was deferred due to missing credentials."""
        app = self._app
        if not app._server_startup_deferred:
            return False

        from opscloud.exceptions import ModelConfigError, NoCredentialsConfiguredError
        from opscloud.model.factory import _get_default_model_spec

        try:
            model_spec = _get_default_model_spec()
        except (NoCredentialsConfiguredError, ModelConfigError):
            return False

        app._server_startup_deferred = False
        app._model = model_spec
        if not getattr(app, "_manual_model", None):
            app._manual_model = model_spec
        app._connecting = True
        if app._server_kwargs is None:
            app._server_kwargs = {
                "assistant_id": app._assistant_id,
                "model_name": model_spec,
                "auto_approve": app._auto_approve,
            }
        else:
            app._server_kwargs["model_name"] = model_spec

        try:
            status_bar = app.query_one(StatusBar)
            status_bar.set_status("Connecting...")
        except Exception:
            pass

        await app._mount_message(
            SystemMessage(
                f"**Credentials configured!** Starting agent server for model `{model_spec}`..."
            )
        )
        app.run_worker(app._start_server_background(), group="server-startup")
        return True

    async def start_server_background(self) -> None:
        """Background worker: start the LangGraph server subprocess and preload MCP metadata."""
        from opscloud.cli.server_manager import start_server_and_get_agent

        app = self._app
        try:
            kwargs: dict[str, Any] = dict(app._server_kwargs or {})
            assistant_id = str(kwargs.pop("assistant_id", app._assistant_id))

            client, server_proc = await start_server_and_get_agent(
                assistant_id=assistant_id,
                on_spawn=lambda proc: setattr(app, "_server_proc", proc),
                **kwargs,
            )
            app._server_proc = server_proc
            if app._exit:
                try:
                    server_proc.stop()
                except Exception:
                    pass
                return

            mcp_info = None
            if app._mcp_preload_kwargs is not None and not app._mcp_preload_kwargs.get("no_mcp"):
                from opscloud.mcp.preload import get_cached_mcp_server_infos

                cached = get_cached_mcp_server_infos()
                mcp_info = cached if cached else None
                app.run_worker(
                    app._preload_mcp_metadata_background(),
                    group="mcp-preload",
                    exclusive=True,
                )

            app.post_message(
                app.ServerReady(
                    client=client,
                    server_proc=server_proc,
                    mcp_server_info=mcp_info,
                )
            )
        except Exception as exc:
            logger.exception("Server startup failed: %s", exc)
            app.post_message(app.ServerStartFailed(error=exc))

    async def preload_mcp_metadata_background(self) -> None:
        """Preload MCP server metadata in background and refresh open viewer if active."""
        app = self._app
        try:
            from opscloud.mcp.preload import preload_mcp_server_info

            infos = await preload_mcp_server_info(**app._mcp_preload_kwargs)
            if infos:
                app._mcp_server_info = infos
                logger.debug("Background MCP preload populated %d servers", len(infos))
                app._sync_banner_mcp()
                try:
                    from opscloud.ui.widgets.mcp_viewer import MCPViewerScreen

                    if isinstance(app.screen, MCPViewerScreen):
                        await app.screen.refresh_server_info(infos)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Background MCP preload failed: %s", exc)

    async def on_server_ready(self, event: Any) -> None:
        """Handle successful background server startup."""
        app = self._app
        app._client = event.client
        app._server_proc = event.server_proc
        if event.mcp_server_info is not None:
            app._mcp_server_info = event.mcp_server_info
            try:
                from opscloud.ui.widgets.mcp_viewer import MCPViewerScreen

                if isinstance(app.screen, MCPViewerScreen):
                    await app.screen.refresh_server_info(app._mcp_server_info)
            except Exception:
                pass

        if app._adapter:
            app._adapter.set_client(event.client)

        await app._finalize_connection(event.client)
        app._connecting = False
        app._startup_sequence_running = False

        logger.debug("TUI: server ready, client bound to adapter")

        try:
            banner = app.query_one(WelcomeBanner)
            banner.set_connected()
        except Exception:
            pass
        app._sync_banner_mcp()

        if app._chat_input:
            app._chat_input.focus()

        await app._process_next_from_queue()

    async def on_server_start_failed(self, event: Any) -> None:
        """Handle server startup failure — show error and allow recovery."""
        app = self._app
        app._connecting = False
        app._startup_sequence_running = False
        app._server_startup_error = event.error

        app._pending_messages.clear()
        while app._queued_widgets:
            widget = app._queued_widgets.popleft()
            try:
                await widget.remove()
            except Exception:
                pass

        app._connecting = False
        app._server_startup_error = event.error
        try:
            status_bar = app.query_one("#status-bar", StatusBar)
            status_bar.set_status("Server Error")
        except Exception:
            pass

        try:
            banner = app.query_one(WelcomeBanner)
            banner.set_error()
        except Exception:
            pass

        from opscloud.exceptions import MissingCredentialsError, MissingProviderPackageError

        if isinstance(event.error, MissingCredentialsError):
            env_var_hint = f" (`{event.error.env_var}`)" if event.error.env_var else ""
            provider_hint = f" for provider `{event.error.provider}`{env_var_hint}" if event.error.provider else ""
            msg = (
                f"Server startup failed: {event.error}\n\n"
                f"Hint: Run `/auth` to configure credentials{provider_hint}, or use `/model` to select a different model."
            )
        elif isinstance(event.error, MissingProviderPackageError):
            msg = (
                f"Server startup failed: {event.error}\n\n"
                f"Hint: Install `{event.error.package}` to use provider `{event.error.provider}`, or switch models with `/model`."
            )
        else:
            msg = (
                f"Server startup failed: {event.error}\n\n"
                "Hint: Check credentials with `/auth` or use `/model` to reconfigure."
            )

        await app._mount_message(ErrorMessage(msg))
        logger.error("Server startup failed: %s", event.error)

        if app._chat_input:
            app._chat_input.focus()

    async def finalize_connection(self, client: Any) -> None:
        """Create thread and mark the app as ready for agent turns."""
        app = self._app
        if app._resume_thread:
            app._agent_thread_id = app._resume_thread
            session_state = getattr(app, "_session_state", None)
            if session_state:
                session_state.thread_id = app._resume_thread
            await app._mount_message(
                SystemMessage(f"Resumed thread: `{app._resume_thread}`")
            )
            await app._load_thread_history(app._resume_thread)
        else:
            app._agent_thread_id = str(uuid.uuid4())

        try:
            status_bar = app.query_one("#status-bar", StatusBar)
            status_bar.set_status("Ready")
        except NoMatches:
            pass
        if app._agent_thread_id:
            bind_logging_to_thread(app._agent_thread_id)
        logger.debug("TUI: connection finalized, thread_id=%s", app._agent_thread_id)

        if app._startup_cmd:
            await app._mount_message(SystemMessage(f"Running startup command: `{app._startup_cmd}`"))
            await app._submit_input(app._startup_cmd, "shell")

        if app._initial_skill:
            await app._submit_input(f"/skill {app._initial_skill}", "normal")

        if app._initial_prompt:
            await app._submit_input(app._initial_prompt, "normal")

        if app._goal:
            await app._send_goal()

    async def send_goal(self) -> None:
        """Submit the initial goal objective if present."""
        if self._app._goal:
            await self._app._submit_input(self._app._goal, "normal")

    # ── /restart Implementation ──────────────────────────

    async def handle_restart_command(self, command: str, *, echo: bool = False) -> None:
        """Drive the `/restart` slash command."""
        app = self._app
        reload_task = app._reload_task
        defer_restart = (
            app._reloading and reload_task is not None and not reload_task.done()
        )
        if defer_restart:
            app._restart_requested_during_reload = True

        if echo:
            await app._mount_message(UserMessage(command))

        if defer_restart:
            await app._mount_message(
                SystemMessage("Reload already in progress; server restart requested.")
            )
            return

        restart_task = app._restart_respawn_task
        if restart_task is not None and not restart_task.done():
            await app._mount_message(
                SystemMessage(
                    "A server restart is already in progress. Queued prompts "
                    "will be sent once it finishes."
                )
            )
            return
        app._schedule_restart_command()

    def schedule_restart_command(
        self,
        *,
        preserve_queue: bool = False,
        propagate_errors: bool = False,
        mutation_lock_held: bool = False,
    ) -> asyncio.Task[bool]:
        """Run config refresh and server respawn off the Textual message pump."""
        app = self._app
        active = app._restart_respawn_task
        if active is not None and not active.done():
            return cast("asyncio.Task[bool]", active)

        task = asyncio.create_task(
            app._run_restart_command_detached(
                preserve_queue=preserve_queue,
                propagate_errors=propagate_errors,
                mutation_lock_held=mutation_lock_held,
            ),
            name="restart",
        )
        app._restart_respawn_task = task
        app._track_server_restart_task(task)
        task.add_done_callback(app._finish_restart)
        return task

    def track_server_restart_task(self, task: asyncio.Task[Any]) -> None:
        self._app._server_restart_tasks.add(task)
        task.add_done_callback(self._app._server_restart_tasks.discard)

    def finish_restart(self, task: asyncio.Task[Any]) -> None:
        """Resume prompts whose server-ready drain raced restart completion."""
        app = self._app
        if task is not app._restart_respawn_task:
            return
        if app._pending_messages and not app._agent_running and not app._exit:
            drain = asyncio.create_task(
                app._process_next_from_queue(),
                name="drain-after-restart",
            )
            drain.add_done_callback(lambda t: None)

    async def run_restart_command_detached(
        self,
        *,
        preserve_queue: bool = False,
        propagate_errors: bool = False,
        mutation_lock_held: bool = False,
    ) -> bool:
        """Serialize and safely report a detached /restart continuation."""
        app = self._app
        try:
            if mutation_lock_held:
                return await app._run_restart_command(
                    preserve_queue=preserve_queue,
                    propagate_errors=propagate_errors,
                )
            async with app._environment_mutation_lock:
                return await app._run_restart_command(
                    preserve_queue=preserve_queue,
                    propagate_errors=propagate_errors,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if propagate_errors:
                raise
            logger.exception("Manual /restart failed unexpectedly")
            await app._mount_message(
                ErrorMessage(f"Restart failed: {type(exc).__name__}: {exc}")
            )
            return False

    async def run_restart_command(
        self,
        *,
        preserve_queue: bool = False,
        propagate_errors: bool = False,
    ) -> bool:
        """Validate and schedule a restart."""
        app = self._app
        if app._connecting and app._reconnecting:
            await app._mount_message(
                SystemMessage(
                    "A server restart is already in progress. Queued prompts "
                    "will be sent once it finishes."
                )
            )
            return False

        preserved = None
        if app._agent_running and app._agent_worker:
            preserved = list(app._pending_messages) if preserve_queue else None
            app._agent_worker.cancel()
            app._agent_running = False
        elif not preserve_queue:
            app._discard_queue()

        if preserved is not None:
            app._pending_messages.extendleft(reversed(preserved))
            app._sync_status_queued()

        if not await app._reload_configuration_for_restart():
            await app._restore_queue_to_input(
                "Configuration reload failed; queued prompts were returned to "
                "the input. Re-submit them or run `/restart` again after fixing "
                "the configuration.",
                empty_notice="Configuration reload failed. Fix the configuration, then run `/restart` again.",
            )
            return False

        if app._server_kwargs is None:
            await app._restore_queue_to_input(
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

        if app._connecting or app._server_proc is None:
            if app._server_startup_deferred:
                notice = (
                    "Server startup is waiting for a model. Configuration was "
                    "reloaded; queued prompts were returned to the input. Set "
                    "credentials with `/auth`, reload the environment with "
                    "`/reload`, or pick a model with `/model` to start the "
                    "server."
                )
                empty_notice = (
                    "Server startup is waiting for a model. Configuration was "
                    "reloaded. Set credentials with `/auth`, reload the "
                    "environment with `/reload`, or pick a model with `/model` "
                    "to start the server."
                )
            elif app._connecting:
                notice = (
                    "The server is still starting. Configuration was reloaded "
                    "and will apply once it finishes connecting; queued prompts "
                    "were returned to the input. Re-submit them to send on the "
                    "current session, or run `/restart` again afterward."
                )
                empty_notice = (
                    "The server is still starting. Configuration was reloaded "
                    "and will apply once it finishes connecting; run `/restart` "
                    "again afterward."
                )
            elif app._server_startup_error is not None:
                notice = (
                    "Cannot restart yet because the server did not finish "
                    "starting. Configuration was reloaded; queued prompts were "
                    "returned to the input. Update credentials with `/auth` if "
                    "needed, then pick a model with `/model` to try again. You "
                    "can also relaunch opscloud.\n\n"
                    f"Last error: {app._server_startup_error}"
                )
                empty_notice = (
                    "Cannot restart yet because the server did not finish "
                    "starting. Configuration was reloaded. Update credentials "
                    "with `/auth` if needed, then pick a model with `/model` to "
                    "try again. You can also relaunch opscloud.\n\n"
                    f"Last error: {app._server_startup_error}"
                )
            else:
                notice = (
                    "Cannot restart yet because the server is not running. "
                    "Configuration was reloaded; queued prompts were returned "
                    "to the input. Relaunch opscloud to start again."
                )
                empty_notice = (
                    "Cannot restart yet because the server is not running. "
                    "Configuration was reloaded. Relaunch opscloud to start again."
                )
            await app._restore_queue_to_input(notice, empty_notice=empty_notice)
            return False

        app._connecting = True
        app._reconnecting = True
        app._sync_status_connection()
        return await app._run_restart_respawn(propagate_errors=propagate_errors)

    async def reload_configuration_for_restart(self) -> bool:
        """Refresh configuration and caches for restart."""
        app = self._app
        try:
            if hasattr(app._settings, "reload_from_environment"):
                await asyncio.to_thread(app._settings.reload_from_environment)
            from opscloud.model.config import clear_caches
            clear_caches()
            return True
        except Exception as exc:
            logger.warning("Configuration reload for restart error: %s", exc)
            return False

    async def run_restart_respawn(self, *, propagate_errors: bool = False) -> bool:
        """Respawn the server for `/restart`, detached from the message pump."""
        app = self._app
        restarting = None
        restarted = False
        failure: Exception | None = None
        try:
            restarting = await app._mount_transient_app_message("Restarting server...")
            restarted = await app._restart_server_manual()
        except Exception as exc:
            if propagate_errors:
                failure = exc
            else:
                logger.exception("Manual /restart of server raised unexpectedly")
                await app._mount_message(
                    ErrorMessage(f"Restart failed: {type(exc).__name__}: {exc}")
                )
        finally:
            if not restarted:
                app._connecting = False
                app._reconnecting = False
                app._sync_status_connection()
            if restarting is not None:
                try:
                    await restarting.remove()
                except Exception:
                    pass

        if restarted:
            await app._mount_message(SystemMessage("Restart complete."))
        else:
            await app._restore_queue_to_input(
                "Restart did not complete; queued prompts were returned to the "
                "input. Re-submit them to send on the current session.",
                empty_notice="Restart did not complete. Run `/restart` to try again.",
            )
        if failure is not None:
            raise failure
        return restarted

    async def restart_server_manual(self) -> bool:
        """Respawn the app-owned LangGraph server for a user-initiated restart."""
        return (await self._app._restart_server_manual_result()).restarted

    async def restart_server_manual_result(self) -> _ServerRespawnResult:
        """Respawn the app-owned server for a user-initiated restart."""
        return await self._app._respawn_server(
            log_message="User-initiated server restart failed",
            mcp_failure_log="MCP metadata preload after user-initiated restart failed",
            mcp_failure_toast="MCP tool metadata could not be refreshed. Use /mcp to check.",
        )

    async def respawn_server(
        self,
        *,
        log_message: str = "Server respawn failed",
        mcp_failure_log: str = "MCP preload after restart failed",
        mcp_failure_toast: str = "MCP metadata could not be refreshed",
        restart_timeout: float = 60.0,
    ) -> _ServerRespawnResult:
        """Stop the app-owned server subprocess and rebuild the agent."""
        app = self._app
        server_proc = app._server_proc
        if server_proc is None or app._exit:
            return _ServerRespawnResult(restarted=False)

        try:
            app._connecting = True
            app._reconnecting = True
            app._sync_status_connection()

            if hasattr(server_proc, "restart"):
                await server_proc.restart(timeout=restart_timeout)
            else:
                await asyncio.to_thread(server_proc.stop)
                await asyncio.to_thread(server_proc.start)

            if app._exit:
                try:
                    server_proc.stop()
                except Exception:
                    pass
                return _ServerRespawnResult(restarted=False)

            from opscloud.mcp.preload import preload_mcp_server_info
            from opscloud.ui.remote_client import RemoteAgent

            mcp_info = None
            mcp_error = None
            mcp_status: _McpRefreshStatus = "unavailable"
            preload_kwargs = dict(getattr(app, "_mcp_preload_kwargs", {}) or {})

            if preload_kwargs.get("no_mcp"):
                mcp_status = "disabled"
            else:
                try:
                    mcp_info = await preload_mcp_server_info(**preload_kwargs)
                    mcp_status = "fresh" if mcp_info is not None else "unavailable"
                except Exception as exc:
                    logger.exception(mcp_failure_log)
                    mcp_error = f"{type(exc).__name__}: {exc}"
                    app.notify(f"{mcp_failure_toast} ({mcp_error})", severity="warning")

            ready_mcp_info = mcp_info if mcp_status != "unavailable" else app._mcp_server_info

            if getattr(server_proc, "is_in_process", False) is True:
                app._client = server_proc.create_client()
            else:
                app._client = RemoteAgent(url=server_proc.url, graph_name="agent")
            if app._adapter:
                app._adapter.set_client(app._client)

            app.post_message(
                app.ServerReady(
                    client=app._client,
                    server_proc=server_proc,
                    mcp_server_info=ready_mcp_info,
                )
            )
            app._reconnecting = False
            return _ServerRespawnResult(
                restarted=True,
                mcp_server_info=mcp_info,
                mcp_status=mcp_status,
                mcp_error=mcp_error,
            )
        except Exception as exc:
            app._connecting = False
            app._reconnecting = False
            app._sync_status_connection()
            logger.exception(log_message)
            app.post_message(app.ServerStartFailed(error=exc))
            return _ServerRespawnResult(restarted=False)

    # ── /reload Implementation ───────────────────────────

    def take_reload_snapshot(self) -> dict[str, Any]:
        """Capture runtime snapshot of reloadable items for change detection."""
        app = self._app
        try:
            from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region
            from opscloud.config.toml_config import load_agent_pool, load_default_agent, load_default_model

            prof = get_active_aws_profile()
            reg = get_active_aws_region() or (getattr(app._settings, "aws_region", None) if app._settings else None)
            pool = load_agent_pool()
            def_model = load_default_model()
            def_agent = load_default_agent()
            theme_name = getattr(app, "theme", None)
            return {
                "aws_profile": prof,
                "aws_region": reg,
                "agent_pool": pool.copy() if pool else None,
                "default_model": def_model,
                "default_agent": def_agent,
                "theme": theme_name,
            }
        except Exception:
            logger.exception("Failed to capture reload snapshot")
            return {}

    def schedule_reload(self) -> asyncio.Task[None]:
        """Run `/reload` off the Textual message pump."""
        app = self._app
        reload_task = app._reload_task
        if app._reloading and reload_task is not None and not reload_task.done():
            app.notify("Reload already in progress.", severity="information")
            return reload_task

        app._reloading = True
        task = asyncio.create_task(app._run_reload_sequence(), name="reload")
        task.add_done_callback(app._finish_reload)
        app._reload_task = task
        return task

    def finish_reload(self, task: asyncio.Task[None]) -> None:
        """Release the reload guard and resume prompts queued during reload."""
        app = self._app
        if task is not app._reload_task:
            return
        app._reloading = False
        if app._pending_messages and not app._agent_running and not app._exit:
            app.call_after_refresh(
                lambda: asyncio.create_task(app._process_next_from_queue())
            )

    async def run_reload_sequence(self) -> None:
        """Run reload work, then honor restart requests it did not satisfy."""
        app = self._app
        try:
            await app._run_reload()
        except asyncio.CancelledError:
            app._restart_requested_during_reload = False
            raise

        if app._exit:
            app._restart_requested_during_reload = False
            return
        if app._restart_requested_during_reload:
            app._restart_requested_during_reload = False
            app._schedule_restart_command(preserve_queue=True)

    async def run_reload(self) -> None:
        """Refresh config, themes, skills, plugins, and hooks, then report."""
        app = self._app
        async with app._environment_mutation_lock:
            await app._run_reload_unlocked()

    async def run_reload_unlocked(self) -> None:
        """Run `/reload` while the environment mutation lock is held."""
        from opscloud.config.toml_config import format_agent_pool_summary
        from opscloud.plugins.adapters.mcp import plugin_mcp_configs
        from opscloud.plugins.discovery import discover_plugins

        app = self._app
        try:
            old_snapshot = dict(app._last_reload_snapshot) if app._last_reload_snapshot else app._take_reload_snapshot()
            old_skill_names = {
                str(s.get("name", "")) for s in (app._discovered_skills or []) if s.get("name")
            }
            old_mcp_server_info = app._mcp_server_info

            # 1. Reload configuration & .env
            try:
                changes = await asyncio.to_thread(app._settings.reload_from_environment)
                from opscloud.model.config import clear_caches
                clear_caches()
                from opscloud.model.pool import clear_pool_caches
                clear_pool_caches()
                from opscloud.config.cloud_profiles import invalidate_aws_profile_cache
                invalidate_aws_profile_cache()

                if app._settings is not None:
                    prof = getattr(app._settings, "aws_profile", None)
                    if isinstance(prof, str) and prof:
                        os.environ["OPSCLOUD_SERVER_AWS_PROFILE"] = prof
                    reg = getattr(app._settings, "aws_region", None)
                    if isinstance(reg, str) and reg:
                        os.environ["OPSCLOUD_SERVER_AWS_REGION"] = reg

                    if app._server_kwargs is not None:
                        if isinstance(prof, str) and prof:
                            app._server_kwargs["aws_profile"] = prof
                        if isinstance(reg, str) and reg:
                            app._server_kwargs["aws_region"] = reg

                    if app._server_proc is not None:
                        try:
                            cur_cfg = getattr(app._server_proc, "config", None)
                            updates: dict[str, Any] = {}
                            if isinstance(prof, str) and prof:
                                updates["aws_profile"] = prof
                            if isinstance(reg, str) and reg:
                                updates["aws_region"] = reg
                            if updates and cur_cfg is not None and is_dataclass(cur_cfg):
                                app._server_proc.config = replace(cast(Any, cur_cfg), **updates)
                        except Exception:
                            logger.debug("Could not update server_proc.config with reloaded settings", exc_info=True)

                    app._sync_status_cloud()
            except (OSError, ValueError, Exception):
                logger.exception("Failed to reload configuration")
                await app._mount_message(
                    SystemMessage(
                        "Failed to reload configuration. Check your .env "
                        "file and environment variables for syntax errors, "
                        "then try again."
                    )
                )
                return

            new_snapshot = app._take_reload_snapshot()
            app._last_reload_snapshot = new_snapshot

            if old_snapshot.get("aws_profile") != new_snapshot.get("aws_profile"):
                c_str = f"aws_profile: {old_snapshot.get('aws_profile')} -> {new_snapshot.get('aws_profile')}"
                if not any(c.startswith("aws_profile:") for c in changes):
                    changes.append(c_str)

            if old_snapshot.get("aws_region") != new_snapshot.get("aws_region"):
                c_str = f"aws_region: {old_snapshot.get('aws_region')} -> {new_snapshot.get('aws_region')}"
                if not any(c.startswith("aws_region:") for c in changes):
                    changes.append(c_str)

            if old_snapshot.get("agent_pool") != new_snapshot.get("agent_pool"):
                old_desc = format_agent_pool_summary(old_snapshot.get("agent_pool"))
                new_desc = format_agent_pool_summary(new_snapshot.get("agent_pool"))
                c_str = f"agent_pool: {old_desc} -> {new_desc}"
                if not any(c.startswith("agent_pool:") for c in changes):
                    changes.append(c_str)

            if old_snapshot.get("default_model") != new_snapshot.get("default_model"):
                c_str = f"default_model: {old_snapshot.get('default_model')} -> {new_snapshot.get('default_model')}"
                if not any(c.startswith("default_model:") for c in changes):
                    changes.append(c_str)

            if old_snapshot.get("default_agent") != new_snapshot.get("default_agent"):
                c_str = f"default_agent: {old_snapshot.get('default_agent')} -> {new_snapshot.get('default_agent')}"
                if not any(c.startswith("default_agent:") for c in changes):
                    changes.append(c_str)

            # 2. Reload themes
            theme_reload_ok = True
            try:
                from opscloud.ui import theme
                theme.reload_registry()
                theme.register_app_themes(app)
            except Exception:
                theme_reload_ok = False
                logger.warning("Failed to reload themes", exc_info=True)

            # 3. Re-discover skills & custom commands
            try:
                from opscloud.commands.custom.loader import clear_custom_command_cache

                clear_custom_command_cache()
                if hasattr(app, "_command_router") and app._command_router:
                    app._command_router.reload_custom_commands()
            except Exception:
                logger.debug("Custom command reload skipped", exc_info=True)

            app._discovered_skills = await asyncio.to_thread(app.get_discovered_skills)
            new_skill_names = {
                str(s.get("name", "")) for s in (app._discovered_skills or []) if s.get("name")
            }
            added_skills = sorted(new_skill_names - old_skill_names)
            removed_skills = sorted(old_skill_names - new_skill_names)

            if hasattr(app, "_chat_input") and app._chat_input:
                from opscloud.ui.command_registry import (
                    build_agent_commands,
                    build_custom_commands,
                    build_skill_commands,
                    get_all_entries,
                )

                skill_commands = build_skill_commands(app._discovered_skills)
                plugin_agents = app.get_discovered_plugin_agents()
                app._discovered_plugin_agents = plugin_agents
                skill_commands.extend(build_agent_commands(plugin_agents or []))
                skill_commands.extend(build_custom_commands())
                app._chat_input.update_slash_commands(get_all_entries(skill_commands))

            # 4. Compose initial report lines
            report_lines = ["### Configuration Reloaded", ""]
            if changes:
                report_lines.append("**Configuration reloaded. Changes:**")
                report_lines.extend(f"  - `{change}`" for change in changes)
            else:
                report_lines.append("• **Configuration reloaded. No changes detected.**")

            active_prof = new_snapshot.get("aws_profile") or (getattr(app._settings, "aws_profile", None) if app._settings else None) or "default"
            active_reg = new_snapshot.get("aws_region") or (getattr(app._settings, "aws_region", None) if app._settings else None) or "us-east-1"
            report_lines.append(f"• **AWS configuration:** profile `{active_prof}` ({active_reg})")

            active_pool = new_snapshot.get("agent_pool")
            if active_pool:
                fast = active_pool.get("fast", "-")
                std = active_pool.get("standard", "-")
                pow_m = active_pool.get("powerful", "-")
                prov = active_pool.get("provider", "multi")
                report_lines.append(
                    f"• **Agent model pool:** fast=`{fast}`, standard=`{std}`, powerful=`{pow_m}` (provider: {prov})"
                )
            else:
                report_lines.append("• **Agent model pool:** default dynamic routing")

            report_lines.append("• **Model config caches cleared.**")
            if theme_reload_ok:
                report_lines.append("• **Theme registry reloaded.**")
            else:
                report_lines.append("• **Theme registry reload failed.**")

            if added_skills or removed_skills:
                report_lines.append("• **Skills updated:**")
                if added_skills:
                    report_lines.append("  - **Added:** " + ", ".join(f"`{s}`" for s in added_skills))
                if removed_skills:
                    report_lines.append("  - **Removed:** " + ", ".join(f"`{s}`" for s in removed_skills))

            # 5. Discover plugins
            try:
                plugin_result = await asyncio.to_thread(discover_plugins)
                plugins = plugin_result.plugins
            except Exception:
                logger.exception("Failed to discover plugins during /reload")
                plugins = ()
                report_lines.append("• ⚠ **Couldn't read plugin state; run /reload to be safe.**")

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
            report_lines.append(
                f"• **Plugins: {plugin_count} plugin"
                f"{'s' if plugin_count != 1 else ''}{separator}"
                f"{plugin_skill_count} skill"
                f"{'s' if plugin_skill_count != 1 else ''}{separator}"
                f"{mcp_count} plugin MCP server"
                f"{'s' if mcp_count != 1 else ''}{separator}"
                f"{hook_count} hook{'s' if hook_count != 1 else ''}**"
            )

            # 6. Restart owned server for plugin MCP
            if app._server_proc is not None:
                preserved = list(app._pending_messages)
                if app._agent_running and app._agent_worker:
                    app._agent_worker.cancel()
                    app._agent_running = False
                else:
                    app._discard_queue()

                try:
                    restart_result = await app._restart_server_manual_result()
                finally:
                    app._restart_requested_during_reload = False
                    if preserved:
                        app._pending_messages.extendleft(reversed(preserved))
                        app._sync_status_queued()

                if restart_result.restarted:
                    report_lines.append("• **Agent server restarted for plugin MCP.**")
                    if restart_result.mcp_status == "disabled":
                        report_lines.append(
                            "  _MCP is disabled for this session (--no-mcp); no servers were loaded._"
                        )
                    else:
                        report_lines.append(
                            _format_mcp_server_changes(
                                old_mcp_server_info,
                                restart_result.mcp_server_info,
                                restart_result.mcp_error,
                            )
                        )
                else:
                    report_lines.append(
                        "• ⚠ **Agent server was not restarted; plugin MCP may be stale.**"
                    )
            elif app._server_kwargs is None:
                report_lines.append(
                    "• **MCP servers unchanged; this session uses a remote "
                    "server and cannot reload MCP config.**"
                )

            report = "\n".join(report_lines)
            await app._mount_message(CommandOutputMessage(report, command="/reload"))
        except Exception as exc:
            logger.exception("Detached /reload failed unexpectedly")
            await app._mount_message(
                ErrorMessage(f"Reload failed: {type(exc).__name__}: {exc}")
            )

    async def invoke_reload(self, *, command: str | None = None) -> None:
        """Perform hot-reload of config, themes, skills, and plugins."""
        if command is not None:
            await self._app._mount_message(UserMessage(command))
        self._app._schedule_reload()

    async def handle_install_command(self, command: str) -> None:
        """Execute in-app package installation and auto-reload."""
        app = self._app
        await app._mount_message(UserMessage(command))
        parts = command.strip().split()
        if len(parts) < 2:
            await app._mount_message(
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

        await app._mount_message(SystemMessage(f"Installing `{target}`..."))

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
                await app._mount_message(
                    SystemMessage(f"Successfully installed `{target}`. Triggering reload...")
                )
                await app._invoke_reload()
            else:
                err_text = stderr.decode().strip() or stdout.decode().strip()
                await app._mount_message(ErrorMessage(f"Failed to install `{target}`:\n{err_text}"))
        except Exception as exc:
            await app._mount_message(ErrorMessage(f"Error running installer for `{target}`: {exc}"))

    async def handle_update_command(self, command: str = "/update") -> None:
        """Check for and apply OpsCloud software updates."""
        app = self._app
        await app._mount_message(UserMessage(command))
        parts = command.strip().split()
        allowed = {"--prerelease", "--deps"}
        unknown = [opt for opt in parts[1:] if opt not in allowed]
        if unknown:
            await app._mount_message(
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

        await app._mount_message(SystemMessage(f"Checking updates for OpsCloud (current: v{cli_version})..."))
        from opscloud.commands.power.runtime import _check_pypi_version, _perform_upgrade

        latest = await asyncio.to_thread(_check_pypi_version, prerelease=prerelease)
        if latest is None:
            await app._mount_message(
                SystemMessage(f"Could not determine latest version. Currently on v{cli_version}.")
            )
            return

        if latest == cli_version:
            await app._mount_message(
                SystemMessage(f"OpsCloud v{cli_version} is on the latest version.")
            )
            return

        await app._mount_message(SystemMessage(f"Updating OpsCloud v{cli_version} → v{latest}..."))
        success, output = await asyncio.to_thread(_perform_upgrade, target_version=latest, prerelease=prerelease)
        if success:
            await app._mount_message(SystemMessage(f"Updated OpsCloud to v{latest}. Run `/restart` to apply."))
        else:
            await app._mount_message(ErrorMessage(f"Failed to update OpsCloud:\n{output}"))
