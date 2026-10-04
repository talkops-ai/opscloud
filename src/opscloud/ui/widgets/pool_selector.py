"""Interactive agent model pool configuration screen for `/pool`."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Click, MouseMove
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static
from textual.widgets._select import SelectOverlay

from opscloud.config.toml_config import clear_agent_pool, load_agent_pool, save_agent_pool
from opscloud.model.config import (
    AVAILABLE_MODELS,
    DEFAULT_PROVIDER_PRIORITY,
    get_curated_models_for_provider,
    get_provider_display_name,
    normalize_model_spec,
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
        width: 76;
        max-width: 95%;
        height: 85%;
        min-height: 20;
        max-height: 42;
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

    PoolSelectorScreen Select {
        margin-bottom: 1;
    }

    PoolSelectorScreen Select:focus > SelectCurrent {
        border: tall $primary;
    }

    PoolSelectorScreen Select > SelectOverlay {
        max-height: 6 !important;
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

    def _get_provider_options(self) -> list[tuple[str, str]]:
        """Return list of (display_name, provider_id) options."""
        providers = [p for p in DEFAULT_PROVIDER_PRIORITY if p in AVAILABLE_MODELS] + [
            p for p in AVAILABLE_MODELS if p not in DEFAULT_PROVIDER_PRIORITY
        ]
        options: list[tuple[str, str]] = []
        for prov in providers:
            disp = get_provider_display_name(prov)
            options.append((f"{disp} ({prov})", prov))
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
        model_options = self._get_model_options_for_provider(self._provider)

        # Pre-select initial values from saved pool or dynamic discovery
        saved_pool = load_agent_pool() or {}
        default_tiers = self._pool_mgr.discover_tiers(provider=self._provider)

        avail_specs = [opt[1] for opt in model_options]

        def _resolve_spec(val: str | None, default: str) -> str:
            if val and val in avail_specs:
                return val
            if val:
                prefixed = f"{self._provider}:{val}"
                if prefixed in avail_specs:
                    return prefixed
            if default and default in avail_specs:
                return default
            return avail_specs[0] if avail_specs else ""

        fast_default = default_tiers.get(0, ("", ""))[0]
        std_default = default_tiers.get(1, ("", ""))[0]
        pow_default = default_tiers.get(2, ("", ""))[0]

        fast_val = _resolve_spec(saved_pool.get("fast"), fast_default)
        std_val = _resolve_spec(saved_pool.get("standard"), std_default)
        pow_val = _resolve_spec(saved_pool.get("powerful"), pow_default)

        with Vertical():
            yield Static("Model Pool Configuration", classes="pool-title")
            yield Static(
                "Configure execution models for fast, standard, and powerful tiers.\n"
                "Jev routes tasks across your pool based on complexity.",
                classes="pool-subtitle",
            )

            with VerticalScroll(classes="pool-section-container"):
                # Provider Selector
                yield Static("Provider", classes="tier-header")
                yield Select(
                    provider_options,
                    value=self._provider,
                    allow_blank=False,
                    id="select-provider",
                )

                # Tier 0: Fast
                with Vertical(classes="tier-block"):
                    yield Static("Fast Tier", classes="tier-header")
                    yield Static("Low-latency model with minimal reasoning. Best for greetings, lookups, and simple queries.", classes="tier-hint")
                    yield Select(
                        model_options,
                        value=fast_val,
                        allow_blank=False,
                        id="select-fast",
                    )

                # Tier 1: Standard
                with Vertical(classes="tier-block"):
                    yield Static("Standard Tier", classes="tier-header")
                    yield Static("Balanced model with medium reasoning. Best for cloud operations, scripts, and routine tasks.", classes="tier-hint")
                    yield Select(
                        model_options,
                        value=std_val,
                        allow_blank=False,
                        id="select-standard",
                    )

                # Tier 2: Powerful
                with Vertical(classes="tier-block"):
                    yield Static("Powerful Tier", classes="tier-header")
                    yield Static("Frontier model with full reasoning. Best for complex diagnostics, architecture, and high-impact actions.", classes="tier-hint")
                    yield Select(
                        model_options,
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

    def on_mouse_move(self, event: MouseMove) -> None:
        """Update highlighted option when hovering mouse over SelectOverlay options."""
        if isinstance(event.widget, SelectOverlay):
            opt_idx = event.style.meta.get("option")
            if opt_idx is not None and 0 <= opt_idx < len(event.widget._options):
                event.widget.highlighted = opt_idx

    def on_click(self, event: Click) -> None:
        """Handle clicks on the modal backdrop or outside open dropdowns."""
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
        clicked_widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
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

    def on_select_changed(self, event: Select.Changed) -> None:
        """Handle provider change to reload model options for all tiers."""
        if event.select.id == "select-provider" and event.value != Select.NULL:
            new_prov = str(event.value)
            if new_prov == self._provider:
                return
            self._provider = new_prov
            new_options = self._get_model_options_for_provider(new_prov)
            default_tiers = self._pool_mgr.discover_tiers(provider=new_prov)

            for tier_id, tier_num in (("select-fast", 0), ("select-standard", 1), ("select-powerful", 2)):
                try:
                    sel = self.query_one(f"#{tier_id}", Select)
                    sel.set_options(new_options)
                    rec_spec = default_tiers.get(tier_num, ("", ""))[0]
                    avail_specs = [opt[1] for opt in new_options]
                    sel.value = rec_spec if rec_spec in avail_specs else (avail_specs[0] if avail_specs else Select.NULL)
                except NoMatches:
                    pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button actions."""
        if event.button.id == "btn-save":
            self.action_save_pool()
        elif event.button.id == "btn-reset":
            self.action_reset_pool()
        elif event.button.id == "btn-cancel":
            self.action_cancel()

    def action_save_pool(self) -> None:
        """Save the chosen pool configuration to config.toml [agent_pool]."""
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
            save_agent_pool(pool_dict, provider=self._provider)
            logger.info("Saved agent model pool to config.toml [agent_pool]: %s", pool_dict)
            self.dismiss({**pool_dict, "provider": self._provider})
        except Exception as exc:
            logger.exception("Failed to save agent model pool: %s", exc)
            self.dismiss(None)

    def action_reset_pool(self) -> None:
        """Reset the pool to automatic dynamic discovery."""
        clear_agent_pool()
        logger.info("Cleared user-configured agent model pool from config.toml")
        self.dismiss({"_cleared": True})

    def action_cancel(self) -> None:
        """Cancel without making changes, or collapse open dropdowns."""
        expanded = [s for s in self.query(Select) if s.expanded]
        if expanded:
            for s in expanded:
                s.expanded = False
            return
        self.dismiss(None)


__all__ = ["PoolSelectorScreen"]
