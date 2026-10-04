"""Interactive configuration manager screen for /config.

Dynamically discovers all configurable options from the manifest and displays
them grouped by category with value source indicators. Every setting is
interactive: booleans toggle, choices cycle, strings/paths/lists open an inline
editor, credentials redirect to the auth manager, and theme/model open their
dedicated selector screens.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import os
from typing import TYPE_CHECKING, ClassVar, Any

from textual.binding import Binding, BindingType
from textual.containers import Vertical, Horizontal
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static, Button
from textual.widgets.option_list import Option

from opscloud.config.manifest import (
    ConfigOption,
    OptionKind,
    get_config_options,
    resolve_scalar,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from opscloud.config.settings import Settings

logger = get_logger(__name__)


# ── Inline Edit Modal ─────────────────────────────────────

class _InlineEditScreen(ModalScreen[str | None]):
    """Small modal for editing a single string/path/list config value."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    _InlineEditScreen {
        align: center middle;
        background: $background 70%;
    }

    _InlineEditScreen > Vertical {
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 12;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    _InlineEditScreen .edit-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }

    _InlineEditScreen Input {
        margin-bottom: 1;
    }

    _InlineEditScreen Horizontal {
        height: auto;
        align: right middle;
    }

    _InlineEditScreen Button {
        margin-left: 1;
    }
    """

    def __init__(self, label: str, current_value: str, placeholder: str = "") -> None:
        super().__init__()
        self._label = label
        self._current_value = current_value
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Edit: {self._label}", classes="edit-title")
            yield Input(
                value=self._current_value,
                placeholder=self._placeholder or "Enter value...",
                id="edit-input",
            )
            with Horizontal():
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", variant="default", id="cancel-btn")

    def on_mount(self) -> None:
        try:
            inp = self.query_one("#edit-input", Input)
            inp.focus()
            # Select all text for easy replacement
            inp.cursor_position = len(inp.value)
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            val = self.query_one("#edit-input", Input).value
            self.dismiss(val)
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Config Manager Screen ─────────────────────────────────

class ConfigManagerScreen(ModalScreen[None]):
    """Modal screen for searching, inspecting, and modifying OpsCloud configuration settings.

    Features a top search bar, live filterable setting entries formatted with setting name,
    value, and value source, with interactive toggling for booleans and choice cycling.
    All settings are interactively editable.
    """

    class SettingChanged(Message):
        """Posted when a setting field is updated."""
        def __init__(self, key: str, value: Any) -> None:
            super().__init__()
            self.key = key
            self.value = value

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("tab", "focus_next_widget", "Next", show=False, priority=True),
        Binding("shift+tab", "focus_prev_widget", "Previous", show=False, priority=True),
        Binding("delete", "reset_selected", "Reset setting", show=False, priority=True),
    ]

    CSS = """
    ConfigManagerScreen {
        align: center middle;
        background: $background 70%;
    }

    ConfigManagerScreen > Vertical {
        width: 90;
        max-width: 95%;
        height: 85%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    ConfigManagerScreen .config-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    ConfigManagerScreen Input#config-search {
        margin-bottom: 1;
        border: solid $panel;
    }

    ConfigManagerScreen Input#config-search:focus {
        border: solid $primary;
    }

    ConfigManagerScreen OptionList#config-options {
        height: 1fr;
        min-height: 5;
        background: $background;
    }

    ConfigManagerScreen .config-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        text-align: center;
        margin-top: 1;
    }
    """

    # Settings that should open dedicated selector screens rather than inline edit
    _SCREEN_DELEGATES: dict[str, str] = {
        "display.theme": "theme",
        "models.name": "model",
        "models.reasoning_effort": "effort",
        "models.context_limit": "model",
        "aws.profile": "cloud_profile",
    }

    # Settings that are read-only runtime state (informational, not user-editable)
    _READ_ONLY_KEYS: frozenset[str] = frozenset({
        "models.provider",
    })

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        if settings is None:
            from opscloud.config.settings import settings as global_settings
            settings = global_settings
        self._settings: Settings = settings
        self._filter_query: str = ""
        # Cache TOML data once at mount time so we don't re-read on every rebuild
        self._toml_data: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("OpsCloud Configuration", classes="config-title")
            yield Input(
                placeholder="Type to search settings...",
                id="config-search",
            )
            yield OptionList(*self._build_options(), id="config-options")
            yield Static(
                "↑/↓ navigate • Enter edit/toggle • / search • Delete reset • Esc close",
                classes="config-help",
            )

    def on_mount(self) -> None:
        from opscloud.config.manifest import load_config_toml
        self._toml_data = load_config_toml()
        # Focus the OptionList so arrow keys navigate immediately
        try:
            self.query_one("#config-options", OptionList).focus()
        except Exception:
            pass

    def on_key(self, event: Any) -> None:
        """Route keyboard between search Input and OptionList.

        - Down arrow from search → move focus to the option list.
        - ``/`` from option list → move focus to search bar (vi-style search).
        - Any printable char from option list → move focus to search and forward keystroke.
        """
        focused = self.app.focused
        search_input = self.query_one("#config-search", Input)
        option_list = self.query_one("#config-options", OptionList)

        if focused is search_input and event.key in ("down", "enter"):
            event.prevent_default()
            option_list.focus()
        elif focused is option_list and event.key == "slash":
            event.prevent_default()
            search_input.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "config-search":
            self._filter_query = event.value.strip().lower()
            self._refresh_options()

    def _build_options(self) -> list[Option]:
        """Build filtered OptionList items dynamically from the config manifest."""
        options: list[Option] = []
        current_group: str | None = None

        for opt in get_config_options():
            # Apply search filter across key, summary, and group
            if self._filter_query and not (
                self._filter_query in opt.key.lower()
                or self._filter_query in opt.summary.lower()
                or self._filter_query in opt.group.lower()
            ):
                continue

            # Insert group header separator
            if opt.group != current_group:
                current_group = opt.group
                options.append(
                    Option(f"── {current_group} ──", id=f"_group_{current_group}", disabled=True)
                )

            # Resolve effective value and its source
            value, source = resolve_scalar(
                opt, settings=self._settings, toml_data=self._toml_data,
            )
            val_str = self._format_value(opt, value)
            source_tag = f"[{source}]" if source != "default" else ""

            # Build display line: summary (left-aligned) + value + source
            summary_col = f"{opt.summary[:38]:<38}"
            value_col = f"{val_str:<20}"
            display = f"{summary_col} {value_col} {source_tag}".rstrip()
            options.append(Option(display, id=opt.key))

        if not options:
            options.append(Option("No matching settings found.", id="none", disabled=True))

        return options

    @staticmethod
    def _format_value(opt: ConfigOption, val: Any) -> str:
        """Format a value for display based on option kind and redaction."""
        if val is None:
            return "false" if opt.kind == OptionKind.BOOL else "None"
        if opt.redacted:
            s_val = str(val)
            return f"`{s_val[:4]}...{s_val[-4:]}`" if len(s_val) > 8 else "set ✓"
        if opt.kind == OptionKind.BOOL:
            return "true" if bool(val) else "false"
        if opt.kind == OptionKind.CHOICE:
            return str(val)
        if opt.kind == OptionKind.PATH:
            return str(val)
        if opt.kind == OptionKind.SHELL_LIST:
            if isinstance(val, list):
                return ", ".join(val) if val else "None"
            return str(val) if val else "None"
        return str(val)

    # ── Selection Handler ─────────────────────────────────

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        key = event.option.id
        if not key or key.startswith("_group_") or key == "none":
            return

        opt = self._find_option(key)
        if not opt:
            return

        # Read-only runtime state → informational message
        if key in self._READ_ONLY_KEYS:
            value, source = resolve_scalar(opt, settings=self._settings, toml_data=self._toml_data)
            self.app.notify(
                f"{opt.summary}: `{value}` (read-only, set by model selection)",
                severity="information",
            )
            return

        # Dedicated screen delegates (theme, model)
        if key in self._SCREEN_DELEGATES:
            self._open_delegate_screen(key)
            return

        # Toggle booleans
        if opt.kind == OptionKind.BOOL:
            self._toggle_bool(opt)
        # Cycle choice values
        elif opt.kind == OptionKind.CHOICE and opt.choices:
            self._cycle_choice(opt)
        # Secret fields → open auth manager
        elif opt.kind == OptionKind.SECRET:
            self._open_secret_editor(opt)
        # String, Path, Int, Float, ShellList → inline editor
        elif opt.kind in (OptionKind.STR, OptionKind.PATH, OptionKind.INT,
                          OptionKind.FLOAT, OptionKind.SHELL_LIST):
            self._open_inline_editor(opt)
        else:
            self.app.notify(
                f"Edit `{opt.key}` in config.toml or set env var `{opt.env_var or opt.key}`",
                severity="information",
            )

    # ── Edit Actions ──────────────────────────────────────

    def _toggle_bool(self, opt: ConfigOption) -> None:
        """Toggle a boolean setting."""
        if opt.settings_field and self._settings:
            cur_val = bool(getattr(self._settings, opt.settings_field, False))
            new_val = not cur_val
            ok, msg = self._settings.set_field(opt.settings_field, str(new_val))
            if ok:
                self.post_message(self.SettingChanged(opt.key, new_val))
                self.app.notify(f"Set {opt.summary} = {new_val}", severity="information")
                self._refresh_options()
        elif opt.key.startswith("permissions."):
            # Permission scopes live on app._permission_store, not Settings
            self._toggle_permission(opt)

    def _toggle_permission(self, opt: ConfigOption) -> None:
        """Toggle a permission scope on app._permission_store."""
        # Map manifest key to scope name: "permissions.shell_read" → "shell:read"
        scope_name = opt.key.replace("permissions.", "").replace("_", ":")
        store = getattr(self.app, "_permission_store", None)
        if store is not None:
            current = store.evaluate(scope_name)
            # Cycle: allow → ask → deny → allow
            cycle = {"allow": "ask", "ask": "deny", "deny": "allow"}
            new_status = cycle[current]
            store.add_rule(new_status, scope_name, source="session")
            self.app.notify(f"Permission `{scope_name}` → {new_status}", severity="information")
            self._refresh_options()

    def _cycle_choice(self, opt: ConfigOption) -> None:
        """Cycle through choice values."""
        if not opt.choices or not opt.settings_field:
            return
        cur_val = str(getattr(self._settings, opt.settings_field, None) or opt.choices[0])
        try:
            idx = list(opt.choices).index(cur_val)
            new_val = opt.choices[(idx + 1) % len(opt.choices)]
        except ValueError:
            new_val = opt.choices[0]

        ok, msg = self._settings.set_field(opt.settings_field, new_val)
        if ok:
            self.post_message(self.SettingChanged(opt.key, new_val))
            self.app.notify(f"Set {opt.summary} = {new_val}", severity="information")
            self._refresh_options()

    def _open_secret_editor(self, opt: ConfigOption) -> None:
        """Redirect to auth manager for secret values."""
        try:
            from opscloud.ui.widgets.auth_manager import AuthManagerScreen
            provider = opt.key.replace("credentials.", "")
            self.app.push_screen(AuthManagerScreen(initial_provider=provider))
        except Exception:
            self.app.notify(
                f"Set `{opt.env_var or opt.key}` via environment or `/login`",
                severity="information",
            )

    def _open_delegate_screen(self, key: str) -> None:
        """Open a dedicated selector screen for theme, model, or effort."""
        delegate = self._SCREEN_DELEGATES[key]
        if delegate == "theme":
            try:
                from opscloud.ui.widgets.theme_selector import ThemeSelectorScreen
                screen = ThemeSelectorScreen(current_theme=self.app.theme)

                def _on_theme_result(result: str | None) -> None:
                    if result:
                        self.app.theme = result
                        from opscloud.ui.theme import save_theme_preference
                        save_theme_preference(result)
                        if self._settings:
                            self._settings.theme = result
                        self.post_message(self.SettingChanged("display.theme", result))
                        self._refresh_options()

                self.app.push_screen(screen, callback=_on_theme_result)
            except Exception as exc:
                logger.debug("Failed opening ThemeSelectorScreen: %s", exc)
                self.app.notify("Theme selector not available", severity="warning")

        elif delegate == "model":
            try:
                from opscloud.ui.widgets.model_selector import ModelSelectorScreen
                screen = ModelSelectorScreen(
                    current_model=self._settings.model_name,
                    current_provider=self._settings.model_provider,
                    current_effort=self._settings.reasoning_effort,
                )

                def _on_model_result(result: Any) -> None:
                    if result:
                        self.post_message(self.SettingChanged("models.name", result))
                        self._refresh_options()

                self.app.push_screen(screen, callback=_on_model_result)
            except Exception as exc:
                logger.debug("Failed opening ModelSelectorScreen: %s", exc)
                self.app.notify("Use `/model` to switch models", severity="information")

        elif delegate == "effort":
            try:
                from opscloud.model.reasoning import (
                    default_effort_for_model,
                    supported_efforts_for_model,
                )
                from opscloud.ui.widgets.effort_selector import EffortSelectorScreen

                model_spec: str = getattr(self.app, "_model", None) or "default"
                efforts = supported_efforts_for_model(model_spec) or ("low", "medium", "high")
                current_eff = self._settings.reasoning_effort
                default_eff = default_effort_for_model(model_spec) or "high"

                screen = EffortSelectorScreen(
                    model_spec=model_spec,
                    efforts=efforts,
                    current_effort=current_eff,
                    default_effort=default_eff,
                )

                def _on_effort_result(result: str | None) -> None:
                    if result is not None:
                        self._settings.reasoning_effort = result
                        # Sync to app._reasoning_effort if present
                        if hasattr(self.app, "_reasoning_effort"):
                            self.app._reasoning_effort = result  # type: ignore[attr-defined]
                        # Update status bar
                        try:
                            from opscloud.ui.widgets.status import StatusBar
                            sb = self.app.query_one("#status-bar", StatusBar)
                            _spec = getattr(self.app, "_model", "") or ""
                            _prov, _mod = (_spec.split(":", 1) if ":" in _spec else ("", _spec))
                            sb.set_model(provider=_prov, model=_mod, effort=result)
                        except Exception:
                            pass
                        self.post_message(self.SettingChanged("models.reasoning_effort", result))
                        self._refresh_options()

                self.app.push_screen(screen, callback=_on_effort_result)
            except Exception as exc:
                logger.debug("Failed opening EffortSelectorScreen: %s", exc)
                self.app.notify("Use `/effort` to change reasoning effort", severity="information")

        elif delegate == "cloud_profile":
            try:
                from opscloud.ui.widgets.cloud_selector import CloudProfileSelectorScreen

                screen = CloudProfileSelectorScreen(
                    current_profile=self._settings.aws_profile,
                )

                def _on_cloud_result(result: str | None) -> None:
                    if result:
                        from opscloud.config.cloud_profiles import set_active_aws_profile

                        set_active_aws_profile(result, persist=True)
                        self._settings.aws_profile = result
                        self.post_message(self.SettingChanged("aws.profile", result))
                        self._refresh_options()

                self.app.push_screen(screen, callback=_on_cloud_result)
            except Exception as exc:
                logger.debug("Failed opening CloudProfileSelectorScreen: %s", exc)
                self.app.notify("Use `/cloud` to switch AWS profiles", severity="information")

    def _open_inline_editor(self, opt: ConfigOption) -> None:
        """Open a small inline editor for string/path/int/float/list values."""
        value, _ = resolve_scalar(opt, settings=self._settings, toml_data=self._toml_data)
        current_str = str(value) if value is not None else ""

        # Build placeholder hint
        if opt.kind == OptionKind.SHELL_LIST:
            placeholder = "Comma-separated commands (e.g. git status, ls, cat)"
        elif opt.kind == OptionKind.PATH:
            placeholder = "Filesystem path (e.g. /home/user/project)"
        elif opt.kind == OptionKind.INT:
            placeholder = "Integer value"
        elif opt.kind == OptionKind.FLOAT:
            placeholder = "Numeric value"
        else:
            placeholder = f"Value for {opt.summary}"

        screen = _InlineEditScreen(
            label=opt.summary,
            current_value=current_str,
            placeholder=placeholder,
        )

        def _on_edit_result(result: str | None) -> None:
            if result is None:
                return  # Cancelled
            self._apply_edit(opt, result)

        self.app.push_screen(screen, callback=_on_edit_result)

    def _apply_edit(self, opt: ConfigOption, raw_value: str) -> None:
        """Apply an edited value to settings, with type coercion."""
        if not opt.settings_field or not self._settings:
            # Env-var-only or no settings backing; set env var directly
            if opt.env_var:
                if raw_value:
                    os.environ[opt.env_var] = raw_value
                else:
                    os.environ.pop(opt.env_var, None)
                self.app.notify(f"Set env {opt.env_var}", severity="information")
                self._refresh_options()
            return

        # Use Settings.set_field for type-safe coercion
        ok, msg = self._settings.set_field(opt.settings_field, raw_value)
        if ok:
            self.post_message(self.SettingChanged(opt.key, raw_value))
            self.app.notify(f"Set {opt.summary} = {raw_value}", severity="information")
            self._refresh_options()
        else:
            self.app.notify(msg, severity="error")

    # ── Reset / Navigation ────────────────────────────────

    def action_reset_selected(self) -> None:
        """Reset highlighted setting to its default value."""
        try:
            option_list = self.query_one("#config-options", OptionList)
            highlighted = option_list.highlighted
            if highlighted is None:
                return
            option = option_list.get_option_at_index(highlighted)
            key = option.id
            if not key or key.startswith("_group_") or key == "none":
                return
            opt = self._find_option(key)
            if not opt:
                return

            # Permission scopes
            if key.startswith("permissions."):
                scope_name = key.replace("permissions.", "").replace("_", ":")
                store = getattr(self.app, "_permission_store", None)
                if store is not None:
                    # Reset scope to its default status
                    from opscloud.ui.permission_store import _DEFAULT_ALLOW, _DEFAULT_ASK, _DEFAULT_DENY
                    if scope_name in _DEFAULT_ALLOW:
                        store.add_rule("allow", scope_name, source="default")
                    elif scope_name in _DEFAULT_ASK:
                        store.add_rule("ask", scope_name, source="default")
                    elif scope_name in _DEFAULT_DENY:
                        store.add_rule("deny", scope_name, source="default")
                    self.app.notify(f"Reset `{scope_name}` to default", severity="warning")
                    self._refresh_options()
                return

            # Settings-backed fields
            if opt.settings_field:
                ok, msg = self._settings.reset_field(opt.settings_field)
                if ok:
                    self.app.notify(msg, severity="warning")
                    self.post_message(self.SettingChanged(opt.key, None))
                    self._refresh_options()
        except Exception:
            pass

    def action_focus_next_widget(self) -> None:
        self.screen.focus_next()

    def action_focus_prev_widget(self) -> None:
        self.screen.focus_previous()

    def _refresh_options(self) -> None:
        try:
            option_list = self.query_one("#config-options", OptionList)
            option_list.clear_options()
            for opt in self._build_options():
                option_list.add_option(opt)
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.dismiss(None)

    @staticmethod
    def _find_option(key: str) -> ConfigOption | None:
        """Look up a ConfigOption by key from the manifest."""
        from opscloud.config.manifest import get_option
        return get_option(key)


__all__ = ["ConfigManagerScreen"]
