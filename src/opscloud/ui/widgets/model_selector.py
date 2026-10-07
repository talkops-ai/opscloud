"""Interactive model selector screen for `/model` command."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Click
from textual.fuzzy import Matcher
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from opscloud.model.config import (
    DEFAULT_PROVIDER_PRIORITY,
    RECOMMENDED_SPECS,
    ProviderAuthState,
    ProviderAuthStatus,
    format_token_count,
    get_available_models_list,
    get_model_profile,
    get_provider_auth_status,
    get_provider_display_name,
    is_provider_package_installed,
    load_default_model,
    load_recent_models,
    save_default_model,
)
from opscloud.utils.logger import get_logger

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

    def on_click(self, event: Click) -> None:
        event.stop()
        if event.chain > 1:
            return
        try:
            if getattr(self.screen, "_dismissed", False):
                return
        except Exception:
            pass
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
        Binding("pageup", "page_up", "Page Up", show=False, priority=True),
        Binding("pagedown", "page_down", "Page Down", show=False, priority=True),
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
        self._dismissed: bool = False
        self._loaded: bool = False
        self._rebuild_counter: int = 0

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
                "↑/↓ navigate • PgUp/PgDn page • Tab autocomplete • Enter select • Ctrl+S default • Ctrl+R recommended • Ctrl+N IDs",
                classes="model-selector-help",
            )

    def on_mount(self) -> None:
        try:
            self.query_one("#model-filter", Input).focus()
        except NoMatches:
            pass
        self.call_after_refresh(self._fit_model_list)

        try:
            has_app = self.app is not None
        except Exception:
            has_app = False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if has_app and loop and loop.is_running():
            self.run_worker(self._async_load_models(), exclusive=True)
        else:
            self._sync_load_models()

    def on_resize(self) -> None:
        self.call_after_refresh(self._fit_model_list)

    def _fit_model_list(self) -> None:
        try:
            container = self.query_one(Vertical)
            body = self.query_one(".model-list", VerticalScroll)
        except NoMatches:
            return
        non_body_height = max(0, container.region.height - body.region.height)
        available_height = self.size.height - non_body_height
        max_height = max(1, min(16, available_height))
        current = body.styles.max_height
        if current is not None and current.cells == max_height:
            return
        body.styles.max_height = max_height

    def _load_model_data(self) -> list[tuple[str, str, str]]:
        raw_models = get_available_models_list()
        return [("auto", "Autonomous Tier Routing (TypeSafe Jev System 1)", "dynamic")] + raw_models

    async def _async_load_models(self) -> None:
        try:
            raw_models = await asyncio.to_thread(self._load_model_data)
        except Exception:
            logger.exception("Failed to load model data in background")
            raw_models = [("auto", "Autonomous Tier Routing (TypeSafe Jev System 1)", "dynamic")]
        if getattr(self, "_dismissed", False):
            return
        self._all_models = raw_models
        self._loaded = True
        self._apply_filter()
        await self._rebuild_options()
        self._update_info()

    def _sync_load_models(self) -> None:
        self._all_models = self._load_model_data()
        self._loaded = True
        self._apply_filter()
        self._rebuild_options_sync()
        self._update_info()

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
            tokens = query.split()
            try:
                matchers = [Matcher(tok, case_sensitive=False) for tok in tokens]
                scored: list[tuple[float, tuple[str, str, str]]] = []
                for item in self._all_models:
                    spec, name, prov = item
                    prov_display = get_provider_display_name(prov)
                    haystack = f"{spec} {name} {prov} {prov_display}".lower()
                    scores = [m.match(haystack) for m in matchers]
                    if all(s > 0 for s in scores) or all(tok in haystack for tok in tokens):
                        score = min(scores) if scores and all(s > 0 for s in scores) else 0.5
                        scored.append((score, item))
                scored.sort(key=lambda x: x[0], reverse=True)
                self._filtered_models = [item for _, item in scored]
            except Exception:
                self._filtered_models = [
                    m for m in self._all_models
                    if all(t in f"{m[0]} {m[1]} {m[2]}".lower() for t in tokens)
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
            self._update_info()
            if not self._loaded:
                return
            self._apply_filter()
            self._schedule_rebuild()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_select()

    def _schedule_rebuild(self) -> None:
        self._rebuild_counter += 1
        gen = self._rebuild_counter
        self.call_after_refresh(self._debounced_rebuild, gen)

    async def _debounced_rebuild(self, gen: int) -> None:
        if gen != self._rebuild_counter:
            return
        await self._rebuild_options()

    # ── Rebuild Options ──────────────────────────────────

    def _build_widgets(self) -> list[Static]:
        """Construct all Static widgets and update _option_widgets and _flat_order."""
        self._option_widgets.clear()
        if not self._filtered_models:
            self._flat_order = []
            if not self._loaded:
                return [Static("Loading models...", classes="model-option")]
            return [Static("No models match your filter.", classes="model-option")]

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

        # Pre-resolve provider auth & installation upfront (Root Cause 4)
        all_provs = set(groups.keys()) | {prov for _, _, prov in recent_matches}
        auth_statuses: dict[str, ProviderAuthStatus] = {
            p: get_provider_auth_status(p) for p in all_provs
        }
        pkg_installed_statuses: dict[str, bool] = {
            p: is_provider_package_installed(p) for p in all_provs
        }

        # Sort provider groups
        def _provider_sort_key(prov: str) -> tuple[int, int, str]:
            if prov == "dynamic":
                return (0, 0, prov)
            if self._current_provider and prov == self._current_provider:
                return (1, 0, prov)
            auth = auth_statuses.get(prov)
            is_authed = auth.as_legacy_bool() is True if auth else False
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

        widgets: list[Static] = []
        current_flat_index = 0

        # 1. Render Pinned Recent Section
        if recent_matches:
            widgets.append(
                Static(
                    f"[bold]{_RECENT_SECTION_LABEL}[/bold]",
                    classes="model-provider-header",
                    markup=True,
                )
            )
            for spec, name, prov in recent_matches:
                auth = auth_statuses.get(prov)
                is_selected = current_flat_index == self._selected_index
                is_current = spec == self._current_spec
                is_default = spec == self._default_spec
                prov_tag = get_provider_display_name(prov)

                label = self._format_row_label(
                    display_name=name,
                    spec=spec,
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
                    effort=None,
                    auth_status=auth,
                    classes=classes,
                    show_provider=True,
                )
                widgets.append(opt)
                self._option_widgets.append(opt)
                current_flat_index += 1

        # 2. Render Provider-Grouped Sections
        for prov in sorted_provs:
            items = groups[prov]
            if prov == "dynamic":
                header_text = "[bold]TypeSafe Jev (System 1)[/bold]"
            else:
                header_name = get_provider_display_name(prov)
                auth = auth_statuses.get(prov)
                pkg_installed = pkg_installed_statuses.get(prov, False)

                if not pkg_installed:
                    header_text = f"[bold]{header_name}[/bold] [dim](not installed)[/dim]"
                elif not auth or not auth.as_legacy_bool():
                    header_text = f"[bold]{header_name}[/bold] [dim](missing credentials)[/dim]"
                else:
                    header_text = f"[bold]{header_name}[/bold]"

            widgets.append(
                Static(
                    header_text,
                    classes="model-provider-header",
                    markup=True,
                )
            )

            for spec, name, _ in items:
                auth = auth_statuses.get(prov)
                is_selected = current_flat_index == self._selected_index
                is_current = spec == self._current_spec
                is_default = spec == self._default_spec

                label = self._format_row_label(
                    display_name=name,
                    spec=spec,
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
                    effort=None,
                    auth_status=auth,
                    classes=classes,
                    show_provider=False,
                )
                widgets.append(opt)
                self._option_widgets.append(opt)
                current_flat_index += 1

        return widgets

    async def _rebuild_options(self) -> None:
        if not self._options_container:
            return

        self._rebuild_counter += 1
        gen = self._rebuild_counter

        widgets = self._build_widgets()
        await self._options_container.remove_children()

        if gen != self._rebuild_counter:
            return

        if widgets:
            await self._options_container.mount(*widgets)

        if not self._filtered_models:
            self._update_detail_footer(None)
        else:
            self._update_detail_footer(
                self._flat_order[self._selected_index]
                if self._flat_order and self._selected_index < len(self._flat_order)
                else None
            )
            self._scroll_to_selected()

    def _rebuild_options_sync(self) -> None:
        if not self._options_container:
            return
        widgets = self._build_widgets()
        self._options_container.remove_children()
        if widgets:
            self._options_container.mount(*widgets)
        if not self._filtered_models:
            self._update_detail_footer(None)
        else:
            self._update_detail_footer(
                self._flat_order[self._selected_index]
                if self._flat_order and self._selected_index < len(self._flat_order)
                else None
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
                mod_parts_cap = f"[green]{tag}[/green]"
            else:
                mod_parts_cap = f"[dim]{tag}[/dim]"
            cap_parts.append(mod_parts_cap)
        line3 = f"Capabilities: {' '.join(cap_parts)}"

        footer.update(f"{line1}\n{line2}\n{line3}")

    # ── Selection Movement ───────────────────────────────

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
        count = len(self._option_widgets)
        new_index = (self._selected_index + delta) % count
        self._set_selected_index(new_index, scroll=True)

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
        elif self._selected_index == len(self._option_widgets) - 1:
            try:
                scroll = self.query_one(".model-list", VerticalScroll)
                scroll.scroll_end(animate=False)
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

    def _visible_page_size(self) -> int:
        default_page_size = 10
        try:
            scroll = self.query_one(".model-list", VerticalScroll)
            height = scroll.size.height
        except Exception:
            return default_page_size
        if height <= 0:
            return default_page_size
        total_models = len(self._option_widgets)
        if total_models == 0:
            return default_page_size
        num_headers = len(self.query(".model-provider-header"))
        header_rows = max(0, num_headers * 2 - 1) if num_headers else 0
        total_rows = total_models + header_rows
        return max(1, int(height * total_models / total_rows))

    def action_page_up(self) -> None:
        if not self._option_widgets:
            return
        page = self._visible_page_size()
        target = max(0, self._selected_index - page)
        delta = target - self._selected_index
        if delta != 0:
            self._move_selection(delta)

    def action_page_down(self) -> None:
        if not self._option_widgets:
            return
        count = len(self._option_widgets)
        page = self._visible_page_size()
        target = min(count - 1, self._selected_index + page)
        delta = target - self._selected_index
        if delta != 0:
            self._move_selection(delta)

    def action_select(self) -> None:
        if getattr(self, "_dismissed", False):
            return
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
        if getattr(self, "_dismissed", False):
            return
        from opscloud.model.config import (
            get_credential_env_var,
            get_provider_auth_status,
            is_provider_package_installed,
            provider_install_extra,
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
                self._schedule_rebuild()

        self.app.push_screen(
            AuthPromptScreen(
                provider,
                env_var,
                reason=f"Required to use {model_spec}",
            ),
            _on_auth_done,
        )

        self.pending_install_extra = None

    def _prompt_install_provider(
        self, model_spec: str, provider: str, extra: str, effort: str | None = None
    ) -> None:
        from opscloud.ui.widgets.install_confirm import InstallProviderConfirmScreen

        def _on_confirm(proceed: bool | None) -> None:
            if proceed:
                self.pending_install_extra = extra
                self.dismiss((model_spec, provider, effort))
            else:
                self._schedule_rebuild()

        self.app.push_screen(
            InstallProviderConfirmScreen(provider, extra, model_spec),
            _on_confirm,
        )

    def dismiss(
        self,
        result: tuple[str, str, str | None] | tuple[str, str] | None = None,
    ) -> Any:
        """Safely dismiss the modal screen with idempotency protection.

        Prevents ScreenStackError if dismiss is invoked multiple times (e.g. from
        rapid clicks, enter key repeat, or queued event dispatch during screen pop).
        """
        if getattr(self, "_dismissed", False):
            return None
        self._dismissed = True

        try:
            app = self.app
        except Exception:
            app = None

        if app is not None:
            try:
                screen_stack = app._screen_stack
            except Exception:
                screen_stack = None
            if screen_stack is not None:
                if len(screen_stack) <= 1 or self not in screen_stack:
                    logger.debug(
                        "ModelSelectorScreen.dismiss bypassed: not in stack or stack size <= 1"
                    )
                    return None

        try:
            return super().dismiss(result)
        except Exception as exc:
            logger.warning("Error dismissing ModelSelectorScreen: %s", exc)
            return None

    def action_cancel(self) -> None:
        if getattr(self, "_dismissed", False):
            return
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
        self._schedule_rebuild()
        self._update_info()

    def action_toggle_names(self) -> None:
        self._show_specs = not self._show_specs
        for opt in self._option_widgets:
            opt.update(opt.render_label_text(self._show_specs))

    def on_model_option_clicked(self, event: ModelOption.Clicked) -> None:
        if getattr(self, "_dismissed", False):
            return
        self._select_with_auth_check(event.model_spec, event.provider, event.effort)


__all__ = ["ModelOption", "ModelSelectorScreen"]
