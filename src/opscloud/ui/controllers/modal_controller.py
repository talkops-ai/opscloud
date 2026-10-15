"""Modal screens, selector workflows, approval-mode switching, and skill/agent session controller."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import is_dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from textual.css.query import NoMatches

from opscloud.approval_mode import ApprovalMode
from opscloud.ui.theme import get_registry, save_theme_preference
from opscloud.ui.widgets.messages import (
    AppMessage,
    CommandOutputMessage,
    ErrorMessage,
    MessageList,
    SkillMessage,
    SystemMessage,
    ToolCallMessage,
    UserMessage,
)
from opscloud.ui.widgets.notification_center import NotificationCenter
from opscloud.ui.widgets.status import StatusBar
from opscloud.ui.widgets.theme_selector import ThemeSelectorScreen
from opscloud.ui.widgets.toast import show_toast
from opscloud.ui.widgets.welcome import WelcomeBanner
from opscloud.utils.logger import bind_logging_to_thread, get_logger

if TYPE_CHECKING:
    from opscloud.ui.app import OpsCloudApp

logger = get_logger(__name__)


class ModalController:
    """Manages modal selectors, model/cloud/theme/effort switching, and skill/agent persona sessions."""

    def __init__(self, app: OpsCloudApp) -> None:
        self._app = app

    # ── Approval Mode & Model Restoration ────────────────

    async def action_toggle_auto_approve(self) -> None:
        """Cycle approval mode on Shift+Tab (Manual -> Auto -> Smart -> Manual)."""
        from opscloud.approval_mode import next_approval_mode

        app = self._app
        auto_eligible = not getattr(app, "_sandbox_active", False)
        target = next_approval_mode(
            getattr(app, "_approval_mode", "manual"),
            auto_eligible=auto_eligible,
            smart_eligible=True,
        )
        if target is None:
            app.notify("Approval mode cycling is unavailable.", severity="warning", timeout=3)
            return

        await app._set_approval_mode(target)

    async def write_live_approval_mode(self, mode: ApprovalMode | None = None) -> bool:
        """Persist approval mode to the remote Store for the active session thread."""
        app = self._app
        agent_obj = getattr(app, "_client", None) or getattr(app, "_agent", None)
        thread_id = getattr(app, "_agent_thread_id", None)
        if agent_obj is None or thread_id is None:
            return False
        from opscloud.approval_mode import awrite_approval_mode

        target = mode or getattr(app, "_approval_mode", ApprovalMode.MANUAL)
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

    async def set_approval_mode(self, target: ApprovalMode) -> bool:
        app = self._app
        agent_obj = getattr(app, "_client", None) or getattr(app, "_agent", None)
        should_persist = (
            agent_obj is not None
            and getattr(app, "_agent_thread_id", None) is not None
        )
        if should_persist and not await app._write_live_approval_mode(target):
            if target in (ApprovalMode.AUTO, ApprovalMode.SMART):
                app.notify(f"{target.value.capitalize()} could not be persisted; remaining in Manual.", severity="warning")
                return False

        prev_mode = getattr(app, "_approval_mode", ApprovalMode.MANUAL)
        app._approval_mode = target
        app._auto_approve = False
        app._update_auto_mode_status_bar(target.value)

        if app._adapter:
            app._adapter._auto_approve = app._auto_approve

        if target is ApprovalMode.SMART:
            if not getattr(app, "_smart_routed_active", False) and getattr(app, "_model", None):
                app._manual_model = app._model
        elif target in (ApprovalMode.MANUAL, ApprovalMode.AUTO):
            if prev_mode is ApprovalMode.SMART or getattr(app, "_smart_routed_active", False):
                await app._restore_manual_default_model()

        if target is ApprovalMode.AUTO:
            app.notify("Switched to Auto mode (Classic LLM).", severity="information", timeout=3)
            app._notify_auto_mode_enabled_once()
        elif target is ApprovalMode.SMART:
            app.notify("Switched to Smart mode (Jev System 1 Protected).", severity="information", timeout=3)
        elif target is ApprovalMode.MANUAL:
            app.notify("Switched to Manual approval mode.", severity="information", timeout=3)
        return True

    def resolve_default_model_spec(self) -> str | None:
        """Resolve the model specification to revert to when exiting smart mode."""
        from opscloud.config.toml_config import load_default_model, read_config_toml

        app = self._app
        try:
            data = read_config_toml()
            explicit_default = data.get("models", {}).get("default")
            if isinstance(explicit_default, str) and explicit_default.strip():
                return explicit_default.strip()
        except Exception:
            pass

        manual_spec = getattr(app, "_manual_model", None)
        if manual_spec and manual_spec not in ("auto", "dynamic"):
            return manual_spec

        try:
            recent_default = load_default_model()
            if recent_default and recent_default not in ("auto", "dynamic"):
                return recent_default
        except Exception:
            pass

        try:
            from opscloud.model.factory import _get_default_model_spec
            settings_obj = getattr(app, "settings", None)
            saved_name = getattr(settings_obj, "model_name", None) if settings_obj else None
            if settings_obj and getattr(app, "_smart_routed_active", False):
                settings_obj.model_name = None
            try:
                return _get_default_model_spec()
            finally:
                if settings_obj and settings_obj.model_name is None and saved_name:
                    settings_obj.model_name = saved_name
        except Exception:
            return None

    async def restore_manual_default_model(self) -> bool:
        """Restore active model and status bar to default model when exiting smart mode."""
        from opscloud.config.settings import get_settings
        from opscloud.config.toml_config import load_effort_for_model
        from opscloud.model.config import apply_stored_credentials, resolve_model_context_limit
        from opscloud.model.reasoning import default_effort_for_model
        from opscloud.ui.app import _split_model_spec

        app = self._app
        default_spec = app._resolve_default_model_spec()
        if not default_spec:
            app._smart_routed_active = False
            return False

        provider = default_spec.split(":", 1)[0] if ":" in default_spec else ""
        if provider:
            apply_stored_credentials(provider)

        os.environ["OPSCLOUD_SERVER_MODEL"] = default_spec
        os.environ["OPSCLOUD_MODEL_NAME"] = default_spec
        os.environ["OPSCODE_SERVER_MODEL"] = default_spec
        os.environ["OPSCODE_MODEL_NAME"] = default_spec

        app._model = default_spec
        app._manual_model = default_spec
        app._smart_routed_active = False

        settings_obj = get_settings()
        app._settings = settings_obj

        if settings_obj is not None:
            settings_obj.model = default_spec
            settings_obj.model_name = default_spec
            if provider:
                settings_obj.model_provider = provider

        eff_val = load_effort_for_model(default_spec) or default_effort_for_model(default_spec) or ""
        eff_display: str = eff_val if eff_val else ""
        app._reasoning_effort = eff_val or None
        if settings_obj is not None:
            settings_obj.reasoning_effort = eff_val or None

        if eff_val:
            os.environ["OPSCLOUD_REASONING_EFFORT"] = eff_val
            os.environ["OPSCODE_REASONING_EFFORT"] = eff_val
        else:
            os.environ.pop("OPSCLOUD_REASONING_EFFORT", None)
            os.environ.pop("OPSCODE_REASONING_EFFORT", None)

        ctx_limit = resolve_model_context_limit(default_spec)
        if ctx_limit and settings_obj is not None:
            settings_obj.model_context_limit = ctx_limit

        try:
            status_bar = getattr(app, "_status_bar", None) or app.query_one("#status-bar", StatusBar)
            _prov, _mdl = _split_model_spec(default_spec)
            status_bar.set_model(provider=_prov, model=_mdl, effort=eff_display)
            if ctx_limit:
                status_bar.set_context_limit(ctx_limit)
        except Exception:
            pass

        try:
            app._sync_banner_model(default_spec, eff_display)
        except Exception:
            pass

        server_kwargs = getattr(app, "_server_kwargs", None)
        if isinstance(server_kwargs, dict):
            server_kwargs["model_name"] = default_spec

        return True

    def notify_auto_mode_enabled_once(self) -> None:
        from opscloud.approval_mode import has_auto_mode_notice, save_auto_mode_notice
        from opscloud.ui.widgets.auto_mode_notice import AutoModeNoticeScreen

        app = self._app
        if has_auto_mode_notice() or getattr(app, "_auto_mode_notice_pending", False):
            return

        def handle_result(accepted: bool | None) -> None:
            app._auto_mode_notice_pending = False
            if accepted is True:
                save_auto_mode_notice()
                return
            asyncio.create_task(app._set_approval_mode(ApprovalMode.MANUAL))

        try:
            app.push_screen(AutoModeNoticeScreen(), handle_result)
            app._auto_mode_notice_pending = True
        except Exception:
            logger.warning("Could not show Auto first-enable notice", exc_info=True)

    # ── Debug Console, Editor, Notifications & Tool Output ─

    def action_toggle_tool_output(self) -> None:
        """Toggle the most recent collapsible transcript unit (SkillMessage, ToolCallMessage, etc.)."""
        app = self._app
        try:
            messages = app.query_one("#messages", MessageList)
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

        app = self._app
        if isinstance(app.screen, DebugConsoleScreen):
            app.pop_screen()
            if app._chat_input:
                app._chat_input.focus_input()
            return
        app._open_debug_console()

    async def action_open_editor(self) -> None:
        """Open the focused editable surface in $VISUAL/$EDITOR."""
        app = self._app
        chat_input = getattr(app, "_chat_input", None)
        if not chat_input or not getattr(chat_input, "_text_area", None):
            return

        await app._open_text_area_in_editor(
            chat_input._text_area,
            chat_input._text_area.text or "",
            allow_empty=False,
            raise_editor_errors=False,
            restore_focus=chat_input.focus_input,
        )

    async def open_text_area_in_editor(
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

        app = self._app
        try:
            with app.suspend():
                edited = open_in_editor(
                    current_text,
                    allow_empty=allow_empty,
                    raise_on_error=raise_editor_errors,
                )
        except ExternalEditorError:
            logger.warning("External editor failed", exc_info=True)
            app.notify(
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
        self._app._open_notification_center()

    def open_notification_center(self) -> None:
        """Toggle the NotificationCenter panel overlay."""
        app = self._app
        try:
            nc = app.query(NotificationCenter)
            if nc:
                nc.first().remove()
            else:
                app.mount(NotificationCenter())
        except Exception:
            logger.warning("Failed to toggle NotificationCenter", exc_info=True)

    def open_debug_console(self) -> None:
        """Push the read-only Debug Console modal."""
        from opscloud.ui.widgets.debug_console import DebugConsoleScreen

        app = self._app

        def handle_result(_: None) -> None:
            if app._chat_input:
                app._chat_input.focus_input()

        def persist_clear(cursor: int) -> None:
            app._debug_console_cleared_upto = cursor

        app.push_screen(
            DebugConsoleScreen(
                app._build_debug_snapshot(),
                snapshot_provider=app._build_debug_snapshot,
                cleared_upto=app._debug_console_cleared_upto,
                on_clear=persist_clear,
                click_to_copy=app._debug_console_click_to_copy,
                on_click_to_copy_change=app._persist_debug_console_click_to_copy,
            ),
            handle_result,
        )

    def persist_debug_console_click_to_copy(self, enabled: bool) -> None:
        self._app._debug_console_click_to_copy = enabled

    def build_debug_snapshot(self) -> list[Any]:
        """Capture a session/runtime snapshot for the debug console header."""
        from opscloud._debug import installed_debug_log_path
        from opscloud._version import __version__
        from opscloud.config.env_vars import DEBUG, is_env_truthy
        from opscloud.ui.widgets.debug_console import SnapshotField

        app = self._app

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
            servers = app._mcp_server_info or []
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
            _safe("Thread", lambda: getattr(app, "_agent_thread_id", "(none)") or "(none)", copyable=True),
            _safe("CWD", lambda: str(Path.cwd()), copyable=True),
            _safe("MCP servers", _mcp),
            _safe("Debug log", _log_path),
        ]

    # ── Theme Selector Integration ────────────────────────

    async def show_theme_selector(self) -> None:
        """Show interactive theme selector modal screen."""
        app = self._app

        def handle_result(result: str | None) -> None:
            if result is not None and result in get_registry():
                app.theme = result
                save_theme_preference(result)
                show_toast(app, f"Switched theme to `{result}`", severity="information")
            if app._chat_input:
                app._chat_input.focus_input()

        screen = ThemeSelectorScreen(current_theme=app.theme)
        app.push_screen(screen, handle_result)

    async def switch_theme(self, name: str) -> None:
        """Switch theme directly by name or alias."""
        app = self._app
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
            app.theme = matched
            save_theme_preference(matched)
            await app._mount_message(
                SystemMessage(f"Switched to `{matched}` theme")
            )
        else:
            avail = ", ".join(f"`{k}`" for k in list(registry.keys())[:8])
            await app._mount_message(
                ErrorMessage(f"Unknown theme `{name}`. Available themes include: {avail}...")
            )

    # ── Cloud Profile Selector Integration ─────────────────

    def sync_status_cloud(self) -> None:
        """Propagate current AWS cloud profile and region to status bar and banner."""
        from opscloud.config.cloud_profiles import get_active_aws_profile, get_active_aws_region

        app = self._app
        try:
            profile = get_active_aws_profile()
            region = get_active_aws_region() or ""
        except Exception:
            return

        try:
            status_bar = app._status_bar or app.query_one("#status-bar", StatusBar)
            status_bar.set_cloud(profile=profile, region=region)
        except Exception:
            pass

        try:
            banner = app.query_one(WelcomeBanner)
            banner.update_cloud(profile=profile, region=region)
        except Exception:
            pass

    async def show_cloud_selector(self) -> None:
        """Open the interactive cloud profile picker modal."""
        from opscloud.config.cloud_profiles import get_active_aws_profile
        from opscloud.ui.widgets.cloud_selector import CloudProfileSelectorScreen

        app = self._app
        current_profile = get_active_aws_profile()
        screen = CloudProfileSelectorScreen(current_profile=current_profile)

        def _on_cloud_selected(result: tuple[str, str | None] | None) -> None:
            if result:
                profile, region = result
                app.run_worker(
                    app._switch_cloud_profile(profile, region=region),
                    exclusive=False,
                    group="cloud-switch",
                )
            elif getattr(screen, "_mutated", False):
                app._schedule_reload()

        app.push_screen(screen, _on_cloud_selected)

    async def switch_cloud_profile(self, profile: str, region: str | None = None) -> None:
        """Switch the active AWS profile and persist across sessions."""
        from opscloud.config.cloud_profiles import get_active_aws_region, set_active_aws_profile

        app = self._app
        set_active_aws_profile(profile, region=region, persist=True)

        effective_region = region or get_active_aws_region() or ""

        settings_obj = getattr(app, "_settings", None)
        if settings_obj is not None:
            settings_obj.aws_profile = profile
            if effective_region:
                settings_obj.aws_region = effective_region
            settings_obj.export_to_environment()

        os.environ["OPSCLOUD_SERVER_AWS_PROFILE"] = profile
        if effective_region:
            os.environ["OPSCLOUD_SERVER_AWS_REGION"] = effective_region

        if app._server_kwargs is not None:
            app._server_kwargs["aws_profile"] = profile
            if effective_region:
                app._server_kwargs["aws_region"] = effective_region

        if app._server_proc is not None:
            try:
                cur_cfg = getattr(app._server_proc, "config", None)
                updates: dict[str, Any] = {"aws_profile": profile}
                if effective_region:
                    updates["aws_region"] = effective_region
                if cur_cfg is not None and is_dataclass(cur_cfg):
                    app._server_proc.config = replace(cast(Any, cur_cfg), **updates)
            except Exception:
                logger.debug("Could not update server process config with active cloud profile", exc_info=True)

        app._sync_status_cloud()

        region_info = f" ({effective_region})" if effective_region else ""
        await app._mount_message(
            SystemMessage(
                f"Switched active AWS profile to **`{profile}`**{region_info} "
                "(saved to `config.toml` — active across all sessions)."
            )
        )
        app._schedule_reload()

    # ── Model Selector Integration ────────────────────────

    async def show_model_selector(self) -> None:
        """Open the interactive model picker modal."""
        from opscloud.model.config import resolve_model_spec
        from opscloud.ui.widgets.model_selector import ModelSelectorScreen

        app = self._app
        for s in getattr(app, "_screen_stack", []):
            if isinstance(s, ModelSelectorScreen):
                logger.debug("ModelSelectorScreen already open on screen stack, skipping")
                return

        current_provider, current_model = None, None
        if app._model:
            try:
                current_provider, current_model = resolve_model_spec(app._model)
            except Exception:
                current_model = app._model

        settings_obj = getattr(app, "settings", None)
        current_effort = getattr(app, "_reasoning_effort", None) or (getattr(settings_obj, "reasoning_effort", None) if settings_obj else None)

        screen = ModelSelectorScreen(
            current_model=current_model,
            current_provider=current_provider,
            current_effort=current_effort,
        )

        _handled = False

        def _on_model_selected(result: tuple[str, str, str | None] | tuple[str, str] | None) -> None:
            nonlocal _handled
            if _handled or result is None:
                return
            _handled = True
            spec = result[0]
            effort = result[2] if len(result) > 2 else None
            extra = screen.pending_install_extra
            if extra:
                app.run_worker(
                    app._install_extra_then_switch(extra, spec, effort=effort),
                    exclusive=False,
                    group="model-install-switch",
                )
            else:
                app.run_worker(
                    app._switch_model(spec, effort=effort),
                    exclusive=False,
                    group="model-switch",
                )

        app.push_screen(screen, _on_model_selected)

    async def install_extra_then_switch(self, extra: str, spec: str, effort: str | None = None) -> None:
        """Install provider package extra, then check auth and switch model."""
        app = self._app
        await app._mount_message(
            SystemMessage(f"Installing '{extra}' integration package...")
        )

        pkg_name = f"langchain-{extra}"
        install_ok = False
        try:
            def _run_install() -> subprocess.CompletedProcess[str]:
                pip_res = subprocess.run(
                    [sys.executable, "-m", "pip", "install", pkg_name],
                    capture_output=True,
                    text=True,
                )
                if pip_res.returncode == 0:
                    return pip_res

                uv_bin = shutil.which("uv")
                if uv_bin:
                    return subprocess.run(
                        [uv_bin, "pip", "install", pkg_name],
                        capture_output=True,
                        text=True,
                    )

                return pip_res

            res = await asyncio.to_thread(_run_install)
            if res.returncode == 0:
                install_ok = True
                await app._mount_message(
                    SystemMessage(f"Successfully installed '{extra}' integration package.")
                )
            else:
                logger.warning("install %s failed with code %d: %s", pkg_name, res.returncode, res.stderr)
                await app._mount_message(
                    ErrorMessage(f"Failed to install '{extra}'. Check logs for details.")
                )
        except Exception as e:
            logger.warning("Failed to install package extra %s: %s", extra, e)
            await app._mount_message(
                ErrorMessage(f"Failed to install '{extra}': {e}")
            )

        if not install_ok:
            return

        auth_ok = await app._prompt_model_auth_if_needed(spec)
        if not auth_ok:
            await app._mount_message(
                SystemMessage(
                    f"Installed '{extra}'. Switch to {spec} anytime with "
                    "`/model` — you'll be prompted for credentials."
                )
            )
            return

        await app._finalize_model_switch(spec, effort=effort)

    async def prompt_model_auth_if_needed(self, spec: str) -> bool:
        """Prompt for missing credentials before switching to a model."""
        from opscloud.model.config import (
            get_credential_env_var,
            get_provider_auth_status,
        )
        from opscloud.ui.widgets.auth import AuthPromptScreen, AuthResult

        app = self._app
        provider = spec.split(":", 1)[0] if ":" in spec else "openai"
        status = get_provider_auth_status(provider)
        if status.as_legacy_bool():
            return True

        env_var = status.env_var or get_credential_env_var(provider) or f"{provider.upper()}_API_KEY"

        loop = asyncio.get_running_loop()
        auth_future: asyncio.Future[AuthResult | None] = loop.create_future()

        def _on_auth_done(result: AuthResult | None) -> None:
            if not auth_future.done():
                loop.call_soon_threadsafe(auth_future.set_result, result)

        app.app.push_screen(
            AuthPromptScreen(
                provider,
                env_var,
                reason=f"Required to use {spec}",
            ),
            _on_auth_done,
        )

        result = await auth_future
        return result is AuthResult.SAVED

    async def switch_model(self, spec: str, effort: str | None = None) -> None:
        """Switch the active model, checking auth if needed."""
        app = self._app
        if ":" in spec:
            _provider = spec.split(":", 1)[0]
        else:
            from opscloud.model.factory import detect_provider
            _provider = detect_provider(spec) or "openai"

        auth_ok = await app._prompt_model_auth_if_needed(spec)
        if not auth_ok:
            return

        await app._finalize_model_switch(spec)

    async def finalize_model_switch(self, spec: str, effort: str | None = None) -> None:
        """Perform the actual model switch and persist to recents."""
        from opscloud.model.config import apply_stored_credentials, resolve_model_context_limit, save_recent_model
        from opscloud.ui.app import _split_model_spec

        app = self._app
        if spec in ("auto", "dynamic"):
            os.environ["OPSCLOUD_SERVER_MODEL"] = spec
            os.environ["OPSCLOUD_MODEL_NAME"] = spec
            os.environ["OPSCODE_SERVER_MODEL"] = spec
            os.environ["OPSCODE_MODEL_NAME"] = spec
            app._model = spec
            settings_obj = getattr(app, "settings", None)
            if settings_obj:
                settings_obj.model = spec
                settings_obj.model_name = spec
            try:
                status_bar = app.query_one("#status-bar", StatusBar)
                status_bar.set_model(provider="typesafe", model="jev-router", effort="auto")
            except NoMatches:
                pass
            app._sync_banner_model("typesafe:jev-router", "auto")
            await app._mount_message(
                SystemMessage("Switched to **Autonomous Tier Routing (TypeSafe Jev System 1)**.")
            )
            return

        provider = spec.split(":", 1)[0] if ":" in spec else ""
        if provider:
            apply_stored_credentials(provider)

        os.environ["OPSCLOUD_SERVER_MODEL"] = spec
        os.environ["OPSCLOUD_MODEL_NAME"] = spec
        os.environ["OPSCODE_SERVER_MODEL"] = spec
        os.environ["OPSCODE_MODEL_NAME"] = spec
        app._model = spec
        if getattr(app, "_approval_mode", ApprovalMode.MANUAL) is not ApprovalMode.SMART:
            app._manual_model = spec
        app._smart_routed_active = False
        settings_obj = getattr(app, "settings", None)
        if settings_obj:
            settings_obj.model = spec
            settings_obj.model_name = spec
            if provider:
                settings_obj.model_provider = provider
        if effort is not None:
            app._reasoning_effort = effort
            os.environ["OPSCLOUD_REASONING_EFFORT"] = effort
            os.environ["OPSCODE_REASONING_EFFORT"] = effort
            if settings_obj:
                settings_obj.reasoning_effort = effort

        eff_val = effort or getattr(app, "_reasoning_effort", None)
        if not eff_val and spec:
            from opscloud.model.reasoning import default_effort_for_model
            eff_val = default_effort_for_model(spec) or ""
        eff_display: str = str(eff_val) if eff_val else ""

        try:
            status_bar = app.query_one("#status-bar", StatusBar)
            _prov, _mdl = _split_model_spec(spec)
            status_bar.set_model(provider=_prov, model=_mdl, effort=eff_display)
            app._sync_banner_model(spec, eff_display)
            _ctx_limit = resolve_model_context_limit(spec)
            if _ctx_limit:
                status_bar.set_context_limit(_ctx_limit)
                settings_obj = getattr(app, "settings", None)
                if settings_obj:
                    settings_obj.model_context_limit = _ctx_limit
        except NoMatches:
            pass

        save_recent_model(spec)

        msg = f"Switched model to `{spec}`"
        if eff_display:
            msg += f" (thinking level: `{eff_display}`)"
        await app._mount_message(
            SystemMessage(msg)
        )

        if app._server_startup_deferred:
            app._server_startup_deferred = False
            app._connecting = True
            app._server_kwargs = {
                "assistant_id": app._assistant_id,
                "model_name": spec,
                "auto_approve": app._auto_approve,
            }
            try:
                status_bar = app.query_one("#status-bar", StatusBar)
                status_bar.set_status("Connecting...")
            except Exception:
                pass
            await app._mount_message(
                SystemMessage(
                    f"**Model configured!** Starting agent server for model `{spec}`..."
                )
            )
            app.run_worker(app._start_server_background(), group="server-startup")

    # ── Effort & Pool Selector Integration ────────────────

    async def show_effort_selector(self) -> None:
        """Open the interactive reasoning effort picker modal."""
        from opscloud.model.reasoning import (
            default_effort_for_model,
            supported_efforts_for_model,
        )
        from opscloud.ui.widgets.effort_selector import EffortSelectorScreen

        app = self._app
        model_spec = app._model or "default"
        efforts = supported_efforts_for_model(model_spec) or ("low", "medium", "high")
        current_eff = getattr(app, "_reasoning_effort", None) or getattr(getattr(app, "settings", None), "reasoning_effort", None)
        default_eff = default_effort_for_model(model_spec) or "high"

        screen = EffortSelectorScreen(
            model_spec=model_spec,
            efforts=efforts,
            current_effort=current_eff,
            default_effort=default_eff,
        )

        def _on_effort_selected(result: str | None) -> None:
            if result is not None:
                app.run_worker(app._set_effort_override(result))

        app.push_screen(screen, _on_effort_selected)

    async def show_pool_selector(self) -> None:
        """Open the interactive agent model pool configuration modal."""
        from opscloud.model.config import apply_stored_credentials, detect_provider
        from opscloud.model.pool import get_model_pool_manager
        from opscloud.ui.widgets.pool_selector import PoolSelectorScreen

        app = self._app
        settings_obj = getattr(app, "settings", None)
        current_spec = getattr(app, "_model", None) or (getattr(settings_obj, "model", None) if settings_obj else None)
        provider = detect_provider(current_spec or "") or getattr(settings_obj, "model_provider", None) or "google_genai"

        screen = PoolSelectorScreen(active_provider=provider)

        def _on_pool_selected(result: dict[str, str] | None) -> None:
            if result is None:
                return

            pool_mgr = get_model_pool_manager()
            with pool_mgr._lock:
                pool_mgr._instances.clear()

            if result.get("_cleared"):
                app.notify("Agent model pool cleared (dynamic discovery restored)", severity="information")
                app.run_worker(
                    app._mount_message(
                        SystemMessage("Agent model pool cleared. Reverted to automatic dynamic discovery.")
                    )
                )
                app._schedule_reload()
                return

            fast = result.get("fast", "")
            std = result.get("standard", "")
            pow_m = result.get("powerful", "")

            provs = {s.split(":", 1)[0] for s in (fast, std, pow_m) if ":" in s}
            for p in provs:
                apply_stored_credentials(p)

            app.notify("Agent model pool saved to config.toml", severity="information")
            msg = (
                f"**Agent Model Pool Saved (`config.toml [agent_pool]`):**\n"
                f"• **Fast (Tier 0):** `{fast}`\n"
                f"• **Standard (Tier 1):** `{std}`\n"
                f"• **Powerful (Tier 2):** `{pow_m}`\n"
                f"In smart mode, Jev dynamically routes requests across these pool models."
            )
            app.run_worker(app._mount_message(SystemMessage(msg)))
            app._schedule_reload()

        app.push_screen(screen, _on_pool_selected)

    async def set_effort_override(self, effort: str) -> None:
        """Apply reasoning effort override and update UI."""
        from opscloud.model.reasoning import default_effort_for_model
        from opscloud.ui.app import _split_model_spec

        app = self._app
        model_spec = app._model or "default"

        if effort.lower() in {"clear", "reset"}:
            app._reasoning_effort = None
            os.environ.pop("OPSCODE_REASONING_EFFORT", None)
            settings_obj = getattr(app, "settings", None)
            if settings_obj:
                settings_obj.reasoning_effort = None
            try:
                sb = app.query_one("#status-bar", StatusBar)
                _prov, _mdl = _split_model_spec(model_spec)
                def_eff = default_effort_for_model(model_spec) or ""
                sb.set_model(provider=_prov, model=_mdl, effort=def_eff)
                app._sync_banner_model(model_spec, def_eff)
            except Exception:
                pass
            await app._mount_message(SystemMessage(f"Reasoning effort override cleared for `{model_spec}`."))
            return

        app._reasoning_effort = effort
        os.environ["OPSCODE_REASONING_EFFORT"] = effort
        settings_obj = getattr(app, "settings", None)
        if settings_obj:
            settings_obj.reasoning_effort = effort

        try:
            sb = app.query_one("#status-bar", StatusBar)
            _prov, _mdl = _split_model_spec(model_spec)
            sb.set_model(provider=_prov, model=_mdl, effort=effort)
        except Exception:
            pass
        app._sync_banner_model(model_spec, effort)

        await app._mount_message(SystemMessage(f"Reasoning effort for `{model_spec}` set to `{effort}`."))

    # ── Agent Selector Integration ────────────────────────

    async def show_agent_selector(self) -> None:
        """Open the interactive agent picker modal with plugin subagents."""
        from opscloud.commands.power.agents import get_agent_session_state
        from opscloud.ui.widgets.agent_selector import AgentSelectorScreen

        app = self._app
        current_state = get_agent_session_state(app)
        current_agent = current_state.short_name or current_state.agent_id

        plugin_agents = app.get_discovered_plugin_agents()
        agent_names: list[str] = []
        for a in plugin_agents:
            raw_name = a.get("name") or ""
            short = raw_name.rsplit(":", 1)[-1]
            if "@" in short:
                short = short.split("@", 1)[0]
            if short and short not in agent_names:
                agent_names.append(short)

        is_plugin_picker = bool(agent_names)

        if not agent_names:
            from opscloud.agent.config import get_available_agent_names, load_default_agent
            agent_names = get_available_agent_names()
            default_agent = load_default_agent()
        else:
            default_agent = None

        def _on_agent_selected(result: str | None) -> None:
            if result is not None:
                if is_plugin_picker:
                    asyncio.create_task(app._activate_selected_agent(result))
                elif result != app._assistant_id:
                    app._switch_agent(result)
            if app._chat_input:
                app._chat_input.focus()

        screen = AgentSelectorScreen(
            current_agent=current_agent or app._assistant_id,
            agent_names=agent_names,
            default_agent=default_agent,
        )
        app.push_screen(screen, _on_agent_selected)

    async def activate_selected_agent(self, agent_name: str, prompt: str = "") -> None:
        """Activate a plugin agent persona session."""
        from opscloud.commands._base import CommandContext
        from opscloud.commands.power.agents import activate_agent_session

        app = self._app
        ctx = CommandContext(app=app, raw_command=f"/agent:{agent_name}", args=agent_name)
        res = await activate_agent_session(ctx, agent_name, prompt=prompt)
        if res.message:
            await app._mount_message(CommandOutputMessage(res.message, command=f"/agent:{agent_name}"))

    def switch_agent(self, agent_name: str) -> None:
        """Switch to a different agent and start a new thread."""
        app = self._app
        if agent_name == app._assistant_id:
            return

        if app._agent_running or app._shell_running:
            app.notify(
                "Cannot switch agents while a task is running. "
                "Interrupt or wait for it to finish first.",
                severity="warning",
            )
            return

        old_agent = app._assistant_id
        app._assistant_id = agent_name

        app._pending_messages.clear()
        app._queued_widgets.clear()
        app._reset_ephemeral_mode_states()
        app._restore_goal_rubric_state({})
        app._reset_thread_usage(0.0, 0)
        try:
            messages = app.query_one("#messages", MessageList)
            messages.clear()
        except NoMatches:
            pass

        app._agent_thread_id = str(uuid.uuid4())
        bind_logging_to_thread(app._agent_thread_id)

        if app._adapter:
            app._adapter._assistant_id = agent_name

        try:
            status_bar = app.query_one("#status-bar", StatusBar)
            status_bar.set_status("Ready")
        except NoMatches:
            pass

        app.call_later(
            app._mount_message,
            SystemMessage(f"Switched to {agent_name}. New thread started."),
        )

        logger.info("Agent switched: %s → %s", old_agent, agent_name)

    # ── Plugin Manager & Skills Viewer ───────────────────

    def snapshot_plugin_state(self) -> tuple[frozenset[str], dict[str, Any]]:
        """Snapshot enabled plugin ids and installed plugin metadata for change detection."""
        from opscloud.plugins.store import (
            load_all_enabled_plugin_ids,
            load_installed_plugins,
            load_marketplace_records,
        )

        app = self._app
        project_root = getattr(app._settings, "project_root", None) if app._settings else None
        p_root = Path(project_root) if project_root else None
        enabled = load_all_enabled_plugin_ids(project_root=p_root)
        installed = {
            k: (v.scope, v.install_path)
            for k, v in load_installed_plugins(project_root=p_root).items()
        }
        marketplaces = {
            k: v.source
            for k, v in load_marketplace_records(project_root=p_root).items()
        }
        return enabled, {"installed": installed, "marketplaces": marketplaces}

    async def check_plugin_manager_changes(
        self,
        baseline_enabled: frozenset[str],
        baseline_meta: dict[str, Any],
    ) -> bool | None:
        """Check whether plugin state changed while the manager was open."""
        try:
            curr_enabled, curr_meta = await asyncio.to_thread(self._app._snapshot_plugin_state)
            return curr_enabled != baseline_enabled or curr_meta != baseline_meta
        except Exception:
            logger.exception("Failed to snapshot plugin state while closing manager")
            return None

    async def show_plugin_manager(self) -> None:
        """Push the plugin manager modal screen for /plugins."""
        from opscloud.plugins.store import load_all_enabled_plugin_ids
        from opscloud.ui.widgets.plugin_manager import (
            PluginManagerResult,
            PluginManagerScreen,
        )

        app = self._app
        try:
            baseline = await asyncio.to_thread(app._snapshot_plugin_state)
        except Exception:
            logger.exception("Failed to snapshot plugin state before opening manager")
            baseline = None

        async def check_changes() -> bool | None:
            if baseline is None:
                return None
            baseline_enabled, baseline_meta = baseline
            return await app._check_plugin_manager_changes(baseline_enabled, baseline_meta)

        def on_close(result: PluginManagerResult) -> None:
            if result in ("reload", "later"):
                app._schedule_reload()
            elif result == "check_failed":
                app.run_worker(
                    app._mount_message(
                        SystemMessage("Could not verify plugin changes. Run `/reload` to apply.")
                    ),
                    exclusive=False,
                )

        mcp_info = app.get_mcp_servers() if hasattr(app, "get_mcp_servers") else ()
        project_root = getattr(app._settings, "project_root", None) if app._settings else None
        p_root = Path(project_root) if project_root else None
        loaded_ids = load_all_enabled_plugin_ids(project_root=p_root)

        screen = PluginManagerScreen(
            mcp_server_info=mcp_info,
            loaded_plugin_ids=loaded_ids,
            project_root=p_root,
            check_reload_required=check_changes,
        )
        app.push_screen(screen, on_close)

    def open_skills_viewer(self) -> None:
        """Push the interactive skills viewer modal screen."""
        from opscloud.ui.widgets.skills_viewer import SkillsViewerScreen

        app = self._app
        skills = app.get_discovered_skills()
        app.push_screen(SkillsViewerScreen(skills=skills))

    # ── Agent Persona Session (/agent) & Skill Command ────

    def reset_ephemeral_mode_states(self) -> None:
        """Reset TUI mirrors for active plugin agent sessions and workflows."""
        app = self._app
        try:
            from opscloud.commands.power.agents import (
                get_agent_session_state,
                sync_agent_session_status_label,
            )

            ag_state = get_agent_session_state(app)
            ag_state.clear()
            sync_agent_session_status_label(app, ag_state)
        except Exception:
            pass

    async def sync_agent_session_state_from_checkpoint(self) -> None:
        """Mirror the thread's `_active_agent_id` channel into `AgentSessionState`."""
        from opscloud.commands.power.agents import (
            _clean_short_name,
            _discover_plugin_agents,
            get_agent_session_state,
            resolve_plugin_agent,
            sync_agent_session_status_label,
        )

        app = self._app
        state = get_agent_session_state(app)
        values = await app._get_thread_state_values()
        if not values:
            state.clear()
            sync_agent_session_status_label(app, state)
            return

        active_id = values.get("_active_agent_id")
        if isinstance(active_id, str) and active_id.strip():
            full_name = active_id.strip()
            spec = resolve_plugin_agent(full_name, _discover_plugin_agents(app))
            state.agent_id = str(spec.get("name") or full_name) if spec else full_name
            state.short_name = _clean_short_name(state.agent_id)
            state.plugin_id = str(spec.get("plugin_id") or "") if spec else ""
            state.description = (
                str(spec.get("description") or "").splitlines()[0]
                if spec and spec.get("description")
                else ""
            )
            state.attached = True
            state.thread_id = app._agent_thread_id
            state.needs_server_reset = False
        else:
            state.clear()
        sync_agent_session_status_label(app, state)

    async def run_agent_session_request(self, request: dict[str, Any]) -> None:
        """Submit one typed agent session request through the normal agent stream."""
        from opscloud.commands.power.agents import get_agent_session_state

        app = self._app
        if not app._adapter or not app._adapter.connected:
            await app._mount_message(ErrorMessage("Agent sessions require a connected server."))
            return

        state = get_agent_session_state(app)
        state.thread_id = app._agent_thread_id
        kind = request.get("kind")

        if kind == "start" and not request.get("prompt"):
            return

        if kind == "clear":
            spinner = "Detaching agent"
        else:
            name = state.short_name or "Agent"
            spinner = f"Thinking ({name})"
        app._agent_turn_started = False
        app._agent_running = True
        await app._set_spinner(spinner)
        prompt_text = str(request.get("message") or request.get("prompt") or "")
        app._agent_worker = app.run_worker(
            app._run_agent_task(
                prompt_text,
                graph_input={
                    "messages": [],
                    "agent_session_request": request,
                },
            ),
            exclusive=False,
        )

    async def maybe_continue_agent_session(self, message: str) -> bool:
        """Route plain chat input to an active, attached plugin agent session."""
        from opscloud.commands.power.agents import get_agent_session_state
        from opscloud.input import parse_file_mentions
        from opscloud.ui.textual_adapter import _read_mentioned_file

        app = self._app
        state = get_agent_session_state(app)
        if state.thread_id not in {None, app._agent_thread_id}:
            state.clear()
            return False
        if not state.accepts_replies:
            return False

        prompt_text, mentioned_files = parse_file_mentions(message)
        max_embed_bytes = 256 * 1024
        if mentioned_files:
            context_parts = [prompt_text, "\n\n## Referenced Files\n"]
            for file_path in mentioned_files:
                try:
                    part = _read_mentioned_file(file_path, max_embed_bytes)
                    context_parts.append(part)
                except Exception as e:
                    context_parts.append(f"\n### {file_path.name}\n[Error reading file: {e}]")
            final_content = "\n".join(context_parts)
        else:
            final_content = prompt_text

        reset_flag = bool(getattr(state, "needs_server_reset", False))
        state.needs_server_reset = False
        req: dict[str, Any] = {
            "kind": "continue",
            "request_id": str(uuid.uuid4()),
            "agent_name": state.agent_id,
            "message": final_content,
        }
        if reset_flag:
            req["reset"] = True
        await app._run_agent_session_request(req)
        return True

    async def handle_skill_command(self, command: str) -> None:
        """Handle a /skill:<name> command by invoking the skill directly."""
        from opscloud.skills.invocation import parse_skill_command

        skill_name, args = parse_skill_command(command)
        await self._app._invoke_skill(skill_name, args, command=command)

    async def invoke_skill(
        self,
        skill_name: str,
        args: str = "",
        *,
        command: str | None = None,
    ) -> None:
        """Load a skill, render its widget, and send its prompt to the agent."""
        from opscloud.skills.invocation import (
            build_skill_invocation_envelope,
            match_discovered_skill,
        )
        from opscloud.skills.loader import load_skill_content

        app = self._app
        normalized_name = skill_name.strip().lower()

        async def _mount_error(message: str) -> None:
            if command is not None:
                await app._mount_message(UserMessage(command))
            await app._mount_message(AppMessage(message))

        if not normalized_name:
            if command is not None:
                await app._mount_message(UserMessage(command))
                await app._mount_message(AppMessage("Usage: /<skill-name> [args]"))
            else:
                await app._mount_message(AppMessage("Skill name is required."))
            return

        skills = app._discovered_skills or app.get_discovered_skills()
        cached = match_discovered_skill(skills, normalized_name)
        if cached is None and app._discovered_skills:
            skills = app.get_discovered_skills()
            app._discovered_skills = skills
            cached = match_discovered_skill(skills, normalized_name)

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

        await app._mount_message(
            SkillMessage(
                skill_name=str(cached.get("name", "")),
                description=str(cached.get("description", "")),
                source=str(cached.get("source", "built-in")),
                body=content,
                args=args,
            )
        )
        await app._send_to_agent(
            envelope.prompt,
            message_kwargs=envelope.message_kwargs,
        )
