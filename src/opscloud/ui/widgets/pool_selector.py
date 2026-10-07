"""Interactive agent model pool configuration screen for `/pool`."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static
from textual.widgets._select import SelectOverlay

from opscloud.config.toml_config import clear_agent_pool, load_agent_pool, save_agent_pool
from opscloud.model.config import (
    AVAILABLE_MODELS,
    DEFAULT_PROVIDER_PRIORITY,
    get_curated_models_for_provider,
    get_provider_display_name,
    has_provider_credentials,
    normalize_model_spec,
    resolve_model_spec,
)
from opscloud.model.pool import get_model_pool_manager

logger = get_logger(__name__)


class PoolSelectorScreen(ModalScreen[dict[str, Any] | None]):
    """Modal dialog for configuring execution models for agent tiers."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save_pool", "Save Pool", show=False),
        Binding("ctrl+r", "reset_pool", "Reset Defaults", show=False),
    ]

    DEFAULT_CSS = """
    PoolSelectorScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    PoolSelectorScreen > Vertical {
        width: 86;
        max-width: 95%;
        height: 85%;
        min-height: 20;
        max-height: 46;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    PoolSelectorScreen .pool-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    PoolSelectorScreen .pool-subtitle {
        color: $text-muted;
        text-align: center;
        margin-bottom: 1;
    }

    PoolSelectorScreen .pool-section-container {
        height: 1fr;
        min-height: 6;
        margin-bottom: 1;
        scrollbar-gutter: stable;
    }

    PoolSelectorScreen .bulk-row {
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
    }

    PoolSelectorScreen .tier-block {
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
        border: round $primary 40%;
        background: $background;
    }

    PoolSelectorScreen .tier-block:hover {
        border: round $primary;
    }

    PoolSelectorScreen .tier-header {
        text-style: bold;
        color: $text;
        margin-top: 1;
    }

    PoolSelectorScreen .tier-hint {
        color: $text-muted;
        text-style: italic;
        margin-bottom: 1;
    }

    PoolSelectorScreen .tier-row {
        height: auto;
        width: 100%;
        margin-top: 0;
        margin-bottom: 1;
    }

    PoolSelectorScreen .tier-field-prov {
        width: 32;
        height: auto;
        margin-right: 1;
    }

    PoolSelectorScreen .tier-field-model {
        width: 1fr;
        height: auto;
    }

    PoolSelectorScreen .tier-label {
        color: $text-muted;
        text-style: bold;
        margin-bottom: 0;
    }

    PoolSelectorScreen Select {
        margin-bottom: 1;
    }

    PoolSelectorScreen Select:focus > SelectCurrent {
        border: tall $primary;
    }

    PoolSelectorScreen Select > SelectOverlay {
        max-height: 10;
        background: $surface;
        border: solid $primary;
    }

    PoolSelectorScreen Select > SelectOverlay > .option-list--option {
        padding: 0 1;
    }

    PoolSelectorScreen Select > SelectOverlay > .option-list--option-hover {
        background: $primary 35%;
        color: $text;
        text-style: bold;
    }

    PoolSelectorScreen Select > SelectOverlay > .option-list--option-highlighted {
        background: $primary;
        color: $background;
        text-style: bold;
    }

    PoolSelectorScreen .pool-buttons {
        height: auto;
        align: center middle;
        margin-top: 1;
    }

    PoolSelectorScreen .pool-buttons Button {
        margin: 0 1;
    }

    PoolSelectorScreen .pool-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        text-align: center;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        active_provider: str = "google_genai",
    ) -> None:
        super().__init__()
        saved = load_agent_pool() or {}
        saved_prov = saved.get("provider")
        self._provider = saved_prov or active_provider or "google_genai"
        self._pool_mgr = get_model_pool_manager()
        self._dismissed: bool = False
        self._updating: bool = False

    def _get_provider_options(self) -> list[tuple[str, str]]:
        """Return list of (display_name, provider_id) options."""
        providers = [p for p in DEFAULT_PROVIDER_PRIORITY if p in AVAILABLE_MODELS] + [
            p for p in AVAILABLE_MODELS if p not in DEFAULT_PROVIDER_PRIORITY
        ]
        options: list[tuple[str, str]] = []
        for prov in providers:
            disp = get_provider_display_name(prov)
            has_creds = has_provider_credentials(prov)
            badge = " ✓" if has_creds is True else ""
            options.append((f"{disp} ({prov}){badge}", prov))
        return options

    def _get_model_options_for_provider(self, provider: str) -> list[tuple[str, str]]:
        """Return list of (display_name, model_spec) options for a given provider."""
        models = get_curated_models_for_provider(provider)
        options: list[tuple[str, str]] = []
        for model_id, display_name in models:
            spec = f"{provider}:{model_id}"
            options.append((f"{display_name} ({model_id})", spec))
        if not options:
            options.append((f"{provider} (default)", f"{provider}:default"))
        return options

    def compose(self) -> ComposeResult:
        provider_options = self._get_provider_options()

        # Pre-select initial values from saved pool or dynamic discovery
        saved_pool = load_agent_pool() or {}
        default_tiers = self._pool_mgr.discover_tiers(provider=self._provider)

        def _resolve_tier_init(tier_name: str, tier_num: int) -> tuple[str, str, list[tuple[str, str]]]:
            saved_spec = saved_pool.get(tier_name)
            def_spec = default_tiers.get(tier_num, ("", ""))[0]
            candidate = saved_spec or def_spec or ""
            prov, _ = resolve_model_spec(candidate)
            if not prov or prov not in AVAILABLE_MODELS:
                prov = self._provider if (self._provider in AVAILABLE_MODELS) else "google_genai"
            tier_options = self._get_model_options_for_provider(prov)
            avail_specs = [opt[1] for opt in tier_options]

            if candidate and candidate in avail_specs:
                val = candidate
            elif candidate:
                prefixed = f"{prov}:{candidate}" if ":" not in candidate else candidate
                if prefixed in avail_specs:
                    val = prefixed
                else:
                    tier_options.append((candidate, candidate))
                    avail_specs.append(candidate)
                    val = candidate
            elif def_spec and def_spec in avail_specs:
                val = def_spec
            else:
                val = avail_specs[0] if avail_specs else ""
            return prov, val, tier_options

        fast_prov, fast_val, fast_opts = _resolve_tier_init("fast", 0)
        std_prov, std_val, std_opts = _resolve_tier_init("standard", 1)
        pow_prov, pow_val, pow_opts = _resolve_tier_init("powerful", 2)

        with Vertical():
            yield Static("Model Pool Configuration", classes="pool-title")
            yield Static(
                "Configure execution models for fast, standard, and powerful tiers.\n"
                "Tiers can use different providers (e.g. Fast: OpenAI, Standard: Gemini, Powerful: Anthropic).",
                classes="pool-subtitle",
            )

            with VerticalScroll(classes="pool-section-container"):
                # Quick Bulk Preset
                with Vertical(classes="bulk-row"):
                    yield Static("Quick Preset: Set all tiers to one provider", classes="tier-label")
                    bulk_options = [("-- Keep current tier selections --", "")] + [
                        (disp, prov) for (disp, prov) in provider_options
                    ]
                    yield Select(
                        bulk_options,
                        value="",
                        allow_blank=False,
                        id="select-provider",
                    )

                # Tier 0: Fast
                with Vertical(classes="tier-block"):
                    yield Static("Fast Tier (Tier 0)", classes="tier-header")
                    yield Static("Low-latency reflex model. Best for greetings, lookups, and simple queries.", classes="tier-hint")
                    with Horizontal(classes="tier-row"):
                        with Vertical(classes="tier-field-prov"):
                            yield Static("Provider", classes="tier-label")
                            yield Select(
                                provider_options,
                                value=fast_prov,
                                allow_blank=False,
                                id="select-provider-fast",
                            )
                        with Vertical(classes="tier-field-model"):
                            yield Static("Model", classes="tier-label")
                            yield Select(
                                fast_opts,
                                value=fast_val,
                                allow_blank=False,
                                id="select-fast",
                            )

                # Tier 1: Standard
                with Vertical(classes="tier-block"):
                    yield Static("Standard Tier (Tier 1)", classes="tier-header")
                    yield Static("Balanced execution model. Best for single-domain coding, operations, and routine tasks.", classes="tier-hint")
                    with Horizontal(classes="tier-row"):
                        with Vertical(classes="tier-field-prov"):
                            yield Static("Provider", classes="tier-label")
                            yield Select(
                                provider_options,
                                value=std_prov,
                                allow_blank=False,
                                id="select-provider-standard",
                            )
                        with Vertical(classes="tier-field-model"):
                            yield Static("Model", classes="tier-label")
                            yield Select(
                                std_opts,
                                value=std_val,
                                allow_blank=False,
                                id="select-standard",
                            )

                # Tier 2: Powerful
                with Vertical(classes="tier-block"):
                    yield Static("Powerful Tier (Tier 2)", classes="tier-header")
                    yield Static("Frontier deep reasoning model. Best for multi-agent planning, root-cause analysis, and architecture.", classes="tier-hint")
                    with Horizontal(classes="tier-row"):
                        with Vertical(classes="tier-field-prov"):
                            yield Static("Provider", classes="tier-label")
                            yield Select(
                                provider_options,
                                value=pow_prov,
                                allow_blank=False,
                                id="select-provider-powerful",
                            )
                        with Vertical(classes="tier-field-model"):
                            yield Static("Model", classes="tier-label")
                            yield Select(
                                pow_opts,
                                value=pow_val,
                                allow_blank=False,
                                id="select-powerful",
                            )

            with Horizontal(classes="pool-buttons"):
                yield Button("Save Pool", variant="primary", id="btn-save")
                yield Button("Reset Defaults", variant="default", id="btn-reset")
                yield Button("Cancel", variant="error", id="btn-cancel")

            yield Static(
                "Tab Navigate  •  Enter Select  •  Ctrl+S Save  •  Ctrl+R Reset  •  Esc Dismiss",
                classes="pool-help",
            )

    def on_click(self, event: Click) -> None:
        """Handle clicks on the modal backdrop or outside open dropdowns."""
        if event.chain > 1 or getattr(self, "_dismissed", False):
            return

        # 1. Click on dark backdrop outside modal dialog
        if event.widget == self:
            expanded = [s for s in self.query(Select) if s.expanded]
            if expanded:
                for s in expanded:
                    s.expanded = False
                return
            self.dismiss(None)
            return

        # 2. Click outside any currently open dropdown menu
        try:
            clicked_widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
        except Exception:
            clicked_widget = None

        if clicked_widget is None:
            return

        for s in self.query(Select):
            if s.expanded:
                is_inside = (clicked_widget == s) or (s in getattr(clicked_widget, "ancestors", []))
                try:
                    overlay = s.query_one(SelectOverlay)
                    if clicked_widget == overlay or (overlay in getattr(clicked_widget, "ancestors", [])):
                        is_inside = True
                except Exception:
                    pass
                if not is_inside:
                    s.expanded = False

    def _update_tier_models(self, tier_name: str, provider: str, tier_num: int) -> None:
        """Update model options and default selection for a specific tier."""
        new_options = self._get_model_options_for_provider(provider)
        try:
            sel = self.query_one(f"#select-{tier_name}", Select)
            sel.set_options(new_options)
            default_tiers = self._pool_mgr.discover_tiers(provider=provider)
            rec_spec = default_tiers.get(tier_num, ("", ""))[0]
            avail_specs = [opt[1] for opt in new_options]
            sel.value = rec_spec if rec_spec in avail_specs else (avail_specs[0] if avail_specs else Select.NULL)
        except NoMatches:
            pass

    def on_select_changed(self, event: Select.Changed) -> None:
        """Handle provider change for individual tiers or bulk sync."""
        if getattr(self, "_dismissed", False) or getattr(self, "_updating", False):
            return
        if event.value == Select.NULL:
            return

        sid = event.select.id
        val = str(event.value)

        # 1. Bulk preset selector
        if sid == "select-provider":
            if not val:
                return
            self._updating = True
            try:
                for tier_name, tier_num in (("fast", 0), ("standard", 1), ("powerful", 2)):
                    try:
                        p_sel = self.query_one(f"#select-provider-{tier_name}", Select)
                        p_sel.value = val
                    except NoMatches:
                        pass
                    self._update_tier_models(tier_name, val, tier_num)
            finally:
                self._updating = False
            return

        # 2. Individual tier provider selector
        for tier_name, tier_num in (("fast", 0), ("standard", 1), ("powerful", 2)):
            if sid == f"select-provider-{tier_name}":
                self._update_tier_models(tier_name, val, tier_num)
                break

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button actions."""
        if getattr(self, "_dismissed", False):
            return
        if event.button.id == "btn-save":
            self.action_save_pool()
        elif event.button.id == "btn-reset":
            self.action_reset_pool()
        elif event.button.id == "btn-cancel":
            self.action_cancel()

    def action_save_pool(self) -> None:
        """Save the chosen pool configuration to config.toml [agent_pool]."""
        if getattr(self, "_dismissed", False):
            return
        try:
            fast_sel = self.query_one("#select-fast", Select)
            std_sel = self.query_one("#select-standard", Select)
            pow_sel = self.query_one("#select-powerful", Select)

            fast_val = str(fast_sel.value) if fast_sel.value != Select.NULL else ""
            std_val = str(std_sel.value) if std_sel.value != Select.NULL else ""
            pow_val = str(pow_sel.value) if pow_sel.value != Select.NULL else ""

            if not fast_val or not std_val or not pow_val:
                return

            pool_dict = {
                "fast": fast_val,
                "standard": std_val,
                "powerful": pow_val,
            }
            provs = {s.split(":", 1)[0] for s in pool_dict.values() if ":" in s}
            pool_provider = next(iter(provs)) if len(provs) == 1 else "multi"

            missing_creds = [p for p in provs if has_provider_credentials(p) is False]
            if missing_creds:
                self.notify(
                    f"Notice: Missing API key for {', '.join(missing_creds)}. Run /config to add credentials.",
                    severity="warning",
                )

            save_agent_pool(pool_dict, provider=pool_provider)
            logger.info("Saved agent model pool to config.toml [agent_pool]: %s (provider=%s)", pool_dict, pool_provider)
            self.dismiss({**pool_dict, "provider": pool_provider})
        except Exception as exc:
            logger.exception("Failed to save agent model pool: %s", exc)
            self.dismiss(None)

    def action_reset_pool(self) -> None:
        """Reset the pool to automatic dynamic discovery."""
        if getattr(self, "_dismissed", False):
            return
        clear_agent_pool()
        logger.info("Cleared user-configured agent model pool from config.toml")
        self.dismiss({"_cleared": True})

    def action_cancel(self) -> None:
        """Cancel without making changes, or collapse open dropdowns."""
        if getattr(self, "_dismissed", False):
            return
        expanded = [s for s in self.query(Select) if s.expanded]
        if expanded:
            for s in expanded:
                s.expanded = False
            return
        self.dismiss(None)

    def dismiss(
        self,
        result: dict[str, Any] | None = None,
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
                        "PoolSelectorScreen.dismiss bypassed: not in stack or stack size <= 1"
                    )
                    return None

        try:
            return super().dismiss(result)
        except Exception as exc:
            logger.warning("Error dismissing PoolSelectorScreen: %s", exc)
            return None


__all__ = ["PoolSelectorScreen"]
