"""Interactive model selector screen for `/model` command."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Click
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from opscloud.model.config import (
    AVAILABLE_MODELS,
    DEFAULT_PROVIDER_PRIORITY,
    RECOMMENDED_SPECS,
    ProviderAuthState,
    ProviderAuthStatus,
    format_token_count,
    get_available_models_list,
    get_credential_env_var,
    get_model_profile,
    get_provider_auth_status,
    get_provider_display_name,
    is_provider_package_installed,
    load_default_model,
    load_recent_models,
    save_default_model,
)

logger = get_logger(__name__)

_RECENT_SECTION_LABEL = "Recent"


class ModelOption(Static):
    """A single clickable model option row in the selector."""

    def __init__(
        self,
        label: str,
        model_spec: str,
        provider: str,
        index: int,
        *,
        display_name: str | None = None,
        is_selected: bool = False,
        is_current: bool = False,
        is_default: bool = False,
        provider_tag: str | None = None,
        effort: str | None = None,
        auth_status: ProviderAuthStatus | None = None,
        classes: str = "",
        show_provider: bool = False,
    ) -> None:
        super().__init__(label, classes=classes, markup=True)
        self.model_spec = model_spec
        self.display_name = display_name or label.strip().lstrip("› ").strip()
        self.provider = provider
        self.index = index
        self.is_selected = is_selected
        self.is_current = is_current
        self.is_default = is_default
        self.provider_tag = provider_tag
        self.effort = effort
        self.show_provider = show_provider
        self.auth_status = auth_status or ProviderAuthStatus(
            state=ProviderAuthState.UNKNOWN,
            provider=provider,
        )

    class Hovered(Message):
        """Posted when a model option is hovered with the mouse."""

        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    class Clicked(Message):
        """Posted when a model option is clicked."""

        def __init__(
            self,
            model_spec: str,
            provider: str,
            index: int,
            effort: str | None = None,
        ) -> None:
            super().__init__()
            self.model_spec = model_spec
            self.provider = provider
            self.index = index
            self.effort = effort

    def on_enter(self) -> None:
        self.post_message(self.Hovered(self.index))

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(self.Clicked(self.model_spec, self.provider, self.index, self.effort))

    def render_label_text(self, show_specs: bool = False) -> str:
        cursor = "› " if self.is_selected else "  "
        main_name = self.model_spec if show_specs else self.display_name
        text = f"{cursor}{main_name}"
        if self.show_provider and self.provider_tag and not show_specs:
            text += f" [dim]({self.provider_tag})[/dim]"
        if self.is_current:
            text += " [dim](current)[/dim]"
        if self.is_default:
            text += " [bold](default)[/bold]"
        return text

    def set_selected(self, selected: bool, show_specs: bool = False) -> None:
        if self.is_selected == selected:
            return
        self.is_selected = selected
        if selected:
            self.add_class("model-option-selected")
        else:
            self.remove_class("model-option-selected")
        self.update(self.render_label_text(show_specs))



class ModelSelectorScreen(ModalScreen[tuple[str, str, str | None] | tuple[str, str] | None]):
    """Full-screen modal for interactive model selection."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("tab", "tab_complete", "Tab complete", show=False, priority=True),
        Binding("enter", "select", "Select", show=False, priority=True),
        Binding("ctrl+s", "set_default", "Set default", show=False, priority=True),
        Binding("ctrl+r", "toggle_recommended", "Recommended", show=False, priority=True),
        Binding("ctrl+n", "toggle_names", "Model IDs", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    ModelSelectorScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    ModelSelectorScreen > Vertical {
        width: 82;
        max-width: 95%;
        height: auto;
        max-height: 90vh;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    ModelSelectorScreen .model-selector-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    ModelSelectorScreen .model-selector-info {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }

    ModelSelectorScreen #model-filter {
        margin-bottom: 1;
        border: solid $panel;
    }

    ModelSelectorScreen #model-filter:focus {
        border: solid $primary;
    }

    ModelSelectorScreen .model-list {
        height: auto;
        min-height: 1;
        max-height: 16;
        background: $background;
        scrollbar-gutter: stable;
    }

    ModelSelectorScreen #model-options {
        height: auto;
    }

    ModelSelectorScreen .model-provider-header {
        color: $primary;
        margin-top: 1;
        text-style: bold;
    }

    ModelSelectorScreen #model-options > .model-provider-header:first-child {
        margin-top: 0;
    }

    ModelSelectorScreen .model-option {
        height: 1;
        padding: 0 1;
    }

    ModelSelectorScreen .model-option:hover {
        background: $panel;
    }

    ModelSelectorScreen .model-option-selected {
        background: $primary;
        color: $background;
        text-style: bold;
    }

    ModelSelectorScreen .model-option-selected:hover {
        background: $primary;
    }

    ModelSelectorScreen .model-option-current {
        text-style: italic;
    }

    ModelSelectorScreen .model-detail-footer {
        height: 4;
        padding: 0 2;
        margin-top: 1;
        border-top: solid $panel;
        background: $surface;
    }

    ModelSelectorScreen .model-selector-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(
        self,
        current_model: str | None = None,
        current_provider: str | None = None,
        current_effort: str | None = None,
    ) -> None:
        super().__init__()
        self._current_model = current_model
        self._current_provider = current_provider
        self._current_effort = current_effort
        self._current_spec: str | None = None
        if current_model and current_provider:
            self._current_spec = f"{current_provider}:{current_model}"
        elif current_model:
            self._current_spec = current_model
        else:
            self._current_spec = None

        self._all_models: list[tuple[str, str, str]] = []
        self._filtered_models: list[tuple[str, str, str]] = []
        self._selected_index = 0
        self._option_widgets: list[ModelOption] = []
        self._flat_order: list[tuple[str, str, str]] = []
        self._options_container: Container | None = None
        self._filter_text = ""
        self._recommended_only = True
        self._show_specs = False
        self._default_spec = load_default_model()
        self._recent_specs = load_recent_models()
        self.pending_install_extra: str | None = None

    # ── Compose ──────────────────────────────────────────

    def compose(self):
        with Vertical():
            if self._current_spec:
                title = f"Select Model (current: {self._current_spec})"
            else:
                title = "Select Model"
            yield Static(title, classes="model-selector-title")

            yield Static(
                self._info_content(),
                classes="model-selector-info",
                id="model-selector-info",
            )

            yield Input(
                placeholder="Type to filter or enter provider:model...",
                id="model-filter",
            )

            with VerticalScroll(classes="model-list"):
                self._options_container = Container(id="model-options")
                yield self._options_container

            yield Static("", classes="model-detail-footer", id="model-detail-footer", markup=True)

            yield Static(
                "↑/↓ navigate • Tab autocomplete • Enter select • Ctrl+S set default • Ctrl+R recommended • Ctrl+N IDs",
                classes="model-selector-help",
            )

    def on_mount(self) -> None:
        raw_models = get_available_models_list()
        self._all_models = [("auto", "Autonomous Tier Routing (TypeSafe Jev System 1)", "dynamic")] + raw_models
        self._apply_filter()
        self._rebuild_options()
        try:
            self.query_one("#model-filter", Input).focus()
        except NoMatches:
            pass

    # ── Info Line ────────────────────────────────────────

    def _info_content(self) -> str:
        if self._filter_text.strip():
            return "Searching all models"
        if self._recommended_only:
            return "Showing recommended models — Ctrl+R for all"
        return "Showing all models — Ctrl+R for recommended"

    def _update_info(self) -> None:
        try:
            self.query_one("#model-selector-info", Static).update(self._info_content())
        except NoMatches:
            pass

    # ── Filtering ────────────────────────────────────────

    def _apply_filter(self) -> None:
        query = self._filter_text.strip().lower()
        if query:
            self._filtered_models = [
                m for m in self._all_models
                if query in m[0].lower() or query in m[1].lower() or query in m[2].lower()
            ]
        elif self._recommended_only:
            self._filtered_models = [
                m for m in self._all_models if m[0] in RECOMMENDED_SPECS
            ]
        else:
            self._filtered_models = list(self._all_models)

        if self._selected_index >= len(self._filtered_models):
            self._selected_index = max(0, len(self._filtered_models) - 1)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "model-filter":
            self._filter_text = event.value
            self._selected_index = 0
            self._apply_filter()
            self._rebuild_options()
            self._update_info()

    # ── Rebuild Options ──────────────────────────────────

    def _rebuild_options(self) -> None:
        if not self._options_container:
            return

        self._options_container.remove_children()
        self._option_widgets.clear()

        if not self._filtered_models:
            self._options_container.mount(
                Static("No models match your filter.", classes="model-option")
            )
            self._update_detail_footer(None)
            return

        has_filter = bool(self._filter_text.strip())

        # Resolve Recent matches when unfiltered
        recent_matches: list[tuple[str, str, str]] = []
        if not has_filter:
            spec_map = {m[0]: m for m in self._all_models}
            for rspec in self._recent_specs:
                if rspec in spec_map:
                    recent_matches.append(spec_map[rspec])

        # Group filtered models by provider
        groups: dict[str, list[tuple[str, str, str]]] = {}
        for spec, name, prov in self._filtered_models:
            groups.setdefault(prov, []).append((spec, name, prov))

        # Sort provider groups: dynamic router first, active provider second,
        # then configured providers, followed by unconfigured providers.
        def _provider_sort_key(prov: str) -> tuple[int, int, str]:
            if prov == "dynamic":
                return (0, 0, prov)
            if self._current_provider and prov == self._current_provider:
                return (1, 0, prov)
            auth = get_provider_auth_status(prov)
            is_authed = auth.as_legacy_bool() is True
            tier = 2 if is_authed else 3
            p_idx = (
                DEFAULT_PROVIDER_PRIORITY.index(prov)
                if prov in DEFAULT_PROVIDER_PRIORITY
                else len(DEFAULT_PROVIDER_PRIORITY) + 10
            )
            return (tier, p_idx, prov)

        sorted_provs = sorted(groups.keys(), key=_provider_sort_key)

        # Re-build total flat item order (recents first, then grouped)
        flat_order: list[tuple[str, str, str]] = list(recent_matches)
        for prov in sorted_provs:
            flat_order.extend(groups[prov])
        self._flat_order = flat_order

        if self._selected_index >= len(flat_order):
            self._selected_index = max(0, len(flat_order) - 1)

        current_flat_index = 0

        # Effort is managed separately via /config or /effort — show one entry per model
        def get_model_efforts(_spec: str) -> list[str | None]:
            return [None]

        # 1. Render Pinned Recent Section
        if recent_matches:
            self._options_container.mount(
                Static(
                    f"[bold]{_RECENT_SECTION_LABEL}[/bold]",
                    classes="model-provider-header",
                    markup=True,
                )
            )
            for spec, name, prov in recent_matches:
                for eff in get_model_efforts(spec):
                    auth = get_provider_auth_status(prov)
                    is_selected = current_flat_index == self._selected_index
                    is_current = spec == self._current_spec
                    is_default = (spec == self._default_spec) and not eff
                    prov_tag = get_provider_display_name(prov)

                    label = self._format_row_label(
                        display_name=name,
                        spec=spec if self._show_specs else spec,
                        selected=is_selected,
                        is_current=is_current,
                        is_default=is_default,
                        provider_tag=prov_tag,
                    )
                    classes = "model-option"
                    if is_selected:
                        classes += " model-option-selected"
                    if is_current:
                        classes += " model-option-current"

                    opt = ModelOption(
                        label,
                        spec,
                        prov,
                        current_flat_index,
                        display_name=name,
                        is_selected=is_selected,
                        is_current=is_current,
                        is_default=is_default,
                        provider_tag=prov_tag,
                        effort=eff,
                        auth_status=auth,
                        classes=classes,
                        show_provider=True,
                    )
                    self._options_container.mount(opt)
                    self._option_widgets.append(opt)
                    current_flat_index += 1

        # 2. Render Provider-Grouped Sections
        for prov in sorted_provs:
            items = groups[prov]
            if prov == "dynamic":
                header_text = "[bold]TypeSafe Jev (System 1)[/bold]"
            else:
                header_name = get_provider_display_name(prov)
                auth = get_provider_auth_status(prov)
                pkg_installed = is_provider_package_installed(prov)

                if not pkg_installed:
                    header_text = f"[bold]{header_name}[/bold] [dim](not installed)[/dim]"
                elif not auth.as_legacy_bool():
                    header_text = f"[bold]{header_name}[/bold] [dim](missing credentials)[/dim]"
                else:
                    header_text = f"[bold]{header_name}[/bold]"

            self._options_container.mount(
                Static(
                    header_text,
                    classes="model-provider-header",
                    markup=True,
                )
            )

            for spec, name, _ in items:
                for eff in get_model_efforts(spec):
                    auth = get_provider_auth_status(prov)
                    is_selected = current_flat_index == self._selected_index
                    is_current = spec == self._current_spec
                    is_default = (spec == self._default_spec) and not eff

                    label = self._format_row_label(
                        display_name=name,
                        spec=spec if self._show_specs else spec,
                        selected=is_selected,
                        is_current=is_current,
                        is_default=is_default,
                        provider_tag=None,
                    )
                    classes = "model-option"
                    if is_selected:
                        classes += " model-option-selected"
                    if is_current:
                        classes += " model-option-current"

                    opt = ModelOption(
                        label,
                        spec,
                        prov,
                        current_flat_index,
                        display_name=name,
                        is_selected=is_selected,
                        is_current=is_current,
                        is_default=is_default,
                        provider_tag=None,
                        effort=eff,
                        auth_status=auth,
                        classes=classes,
                        show_provider=False,
                    )
                    self._options_container.mount(opt)
                    self._option_widgets.append(opt)
                    current_flat_index += 1

        self._update_detail_footer(
            flat_order[self._selected_index] if flat_order and self._selected_index < len(flat_order) else None
        )
        self._scroll_to_selected()

    def _format_row_label(
        self,
        display_name: str,
        spec: str,
        selected: bool,
        is_current: bool,
        is_default: bool,
        provider_tag: str | None = None,
    ) -> str:
        cursor = "› " if selected else "  "
        main_name = spec if self._show_specs else display_name

        text = f"{cursor}{main_name}"
        if provider_tag and not self._show_specs:
            text += f" [dim]({provider_tag})[/dim]"
        if is_current:
            text += " [dim](current)[/dim]"
        if is_default:
            text += " [bold](default)[/bold]"
        return text

    # ── Detail Footer Rendering ──────────────────────────

    def _update_detail_footer(self, model: tuple[str, str, str] | None) -> None:
        try:
            footer = self.query_one("#model-detail-footer", Static)
        except NoMatches:
            return

        if not model:
            footer.update("Model profile not available")
            return

        spec, display_name, provider = model
        if spec in ("auto", "dynamic"):
            footer.update(
                "Router: Autonomous tier routing (Fast / Standard / Powerful)\n"
                "Classifier: TypeSafe Jev (System 1 Calibrated Decision Model)\n"
                "Capabilities: [green]parallel classification[/green] [green]zero token generation[/green] [green]RLCD probabilities[/green]"
            )
            return

        profile_entry = get_model_profile(spec)

        if not profile_entry or not profile_entry.get("profile"):
            footer.update(f"Spec: {spec}\nProvider: {provider}")
            return

        prof = profile_entry["profile"]

        # Line 1: Context window (Clean text, no markdown asterisks!)
        inp_tok = format_token_count(prof.get("max_input_tokens", 128000))
        out_tok = format_token_count(prof.get("max_output_tokens", 16384))
        line1 = f"Context: {inp_tok} in • {out_tok} out"

        # Line 2: Input Modalities
        modalities = [
            ("text_inputs", "text"),
            ("image_inputs", "image"),
            ("audio_inputs", "audio"),
            ("pdf_inputs", "pdf"),
            ("video_inputs", "video"),
        ]
        mod_parts = []
        for key, tag in modalities:
            if prof.get(key):
                mod_parts.append(f"[green]{tag}[/green]")
            else:
                mod_parts.append(f"[dim]{tag}[/dim]")
        line2 = f"Input: {' '.join(mod_parts)}"

        # Line 3: Capabilities
        capabilities = [
            ("reasoning_output", "reasoning"),
            ("tool_calling", "tool calling"),
            ("structured_output", "structured output"),
        ]
        cap_parts = []
        for key, tag in capabilities:
            if prof.get(key):
                cap_parts.append(f"[green]{tag}[/green]")
            else:
                cap_parts.append(f"[dim]{tag}[/dim]")
        line3 = f"Capabilities: {' '.join(cap_parts)}"

        footer.update(f"{line1}\n{line2}\n{line3}")

    # ── Selection Movement ───────────────────────────────

    def on_model_option_hovered(self, event: ModelOption.Hovered) -> None:
        if event.index != self._selected_index:
            self._set_selected_index(event.index, scroll=False)

    def _set_selected_index(self, new_index: int, scroll: bool = False) -> None:
        if not self._option_widgets:
            return
        new_index = max(0, min(new_index, len(self._option_widgets) - 1))
        old_index = self._selected_index
        if new_index == old_index and not scroll:
            return

        # Deselect previous
        if 0 <= old_index < len(self._option_widgets):
            self._option_widgets[old_index].set_selected(False, self._show_specs)

        # Select new
        self._selected_index = new_index
        if 0 <= new_index < len(self._option_widgets):
            self._option_widgets[new_index].set_selected(True, self._show_specs)

        # Update footer
        if self._flat_order and new_index < len(self._flat_order):
            self._update_detail_footer(self._flat_order[new_index])

        if scroll:
            self._scroll_to_selected()

    def _move_selection(self, delta: int) -> None:
        if not self._option_widgets:
            return
        self._set_selected_index(self._selected_index + delta, scroll=True)

    def _scroll_to_selected(self) -> None:
        if not self._option_widgets or self._selected_index >= len(self._option_widgets):
            return
        widget = self._option_widgets[self._selected_index]
        if self._selected_index == 0:
            try:
                scroll = self.query_one(".model-list", VerticalScroll)
                scroll.scroll_home(animate=False)
            except Exception:
                try:
                    widget.scroll_visible(animate=False)
                except Exception:
                    pass
        else:
            try:
                widget.scroll_visible(animate=False)
            except Exception:
                pass


    # ── Actions ──────────────────────────────────────────

    def action_move_up(self) -> None:
        self._move_selection(-1)

    def action_move_down(self) -> None:
        self._move_selection(1)

    def action_select(self) -> None:
        if not self._option_widgets:
            typed = self._filter_text.strip()
            if typed:
                if ":" in typed:
                    p, m = typed.split(":", 1)
                    self._select_with_auth_check(typed, p)
                else:
                    self._select_with_auth_check(typed, "openai")
            return
        opt = self._option_widgets[self._selected_index]
        self._select_with_auth_check(opt.model_spec, opt.provider, opt.effort)

    def _select_with_auth_check(
        self, model_spec: str, provider: str, effort: str | None = None
    ) -> None:
        from opscloud.model.config import (
            is_provider_package_installed,
            provider_install_extra,
            get_provider_auth_status,
            get_credential_env_var,
        )

        extra = provider_install_extra(provider)
        if extra is not None and not is_provider_package_installed(provider):
            self._prompt_install_provider(model_spec, provider, extra, effort)
            return

        status = get_provider_auth_status(provider)
        if status.as_legacy_bool():
            self.dismiss((model_spec, provider, effort))
            return

        env_var = status.env_var or get_credential_env_var(provider)

        from opscloud.ui.widgets.auth import AuthPromptScreen, AuthResult

        def _on_auth_done(result: AuthResult | None) -> None:
            if result is AuthResult.SAVED:
                self.dismiss((model_spec, provider, effort))
            else:
                self._rebuild_options()

        self.app.push_screen(
            AuthPromptScreen(
                provider,
                env_var,
                reason=f"Required to use {model_spec}",
            ),
            _on_auth_done,
        )

        self.pending_install_extra: str | None = None

    def _prompt_install_provider(
        self, model_spec: str, provider: str, extra: str, effort: str | None = None
    ) -> None:
        from opscloud.ui.widgets.install_confirm import InstallProviderConfirmScreen

        def _on_confirm(proceed: bool | None) -> None:
            if proceed:
                self.pending_install_extra = extra
                self.dismiss((model_spec, provider, effort))
            else:
                self._rebuild_options()

        self.app.push_screen(
            InstallProviderConfirmScreen(provider, extra, model_spec),
            _on_confirm,
        )

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_tab_complete(self) -> None:
        if not self._option_widgets:
            return
        opt = self._option_widgets[self._selected_index]
        try:
            inp = self.query_one("#model-filter", Input)
            inp.value = opt.model_spec
            inp.cursor_position = len(opt.model_spec)
        except NoMatches:
            pass

    def action_set_default(self) -> None:
        if not self._option_widgets:
            return
        opt = self._option_widgets[self._selected_index]
        spec = opt.model_spec
        if self._default_spec == spec:
            self._default_spec = None
            from opscloud.model.config import clear_default_model
            clear_default_model()
            self.notify("Default model cleared", severity="information", timeout=3)
        else:
            self._default_spec = spec
            save_default_model(spec)
            self.notify(f"Default set: {spec}", severity="information", timeout=3)
        for w in self._option_widgets:
            w.is_default = (w.model_spec == self._default_spec) and not w.effort
            w.update(w.render_label_text(self._show_specs))

    def action_toggle_recommended(self) -> None:
        self._recommended_only = not self._recommended_only
        self._selected_index = 0
        self._apply_filter()
        self._rebuild_options()
        self._update_info()

    def action_toggle_names(self) -> None:
        self._show_specs = not self._show_specs
        for opt in self._option_widgets:
            opt.update(opt.render_label_text(self._show_specs))


    def on_model_option_clicked(self, event: ModelOption.Clicked) -> None:
        self._select_with_auth_check(event.model_spec, event.provider, event.effort)


__all__ = ["ModelOption", "ModelSelectorScreen"]
