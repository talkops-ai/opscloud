"""Interactive permissions manager modal screen for /permissions.

Full-screen modal with tabbed interface inspired by Claude Code's permission
management TUI.  Six tabs: Permissions · Recently Denied · Allow · Ask · Deny ·
Workspace.  Supports search/filter, rule add/delete, and keyboard navigation.

Styled consistently with the project's existing modals (AuthManagerScreen,
ConfigManagerScreen, ThreadSelectorScreen).
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
import time
from typing import TYPE_CHECKING, Any, ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.content import Content
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from opscloud.config.settings import get_glyphs
from opscloud.ui.permission_store import (
    PERMISSION_MODES,
    PermissionRule,
    PermissionStore,
    VALID_SCOPES,
    save_permission_store,
)
from opscloud.ui.widgets.modal_base import (
    OpsModalScreen,
    PillTab,
    PillTabs,
    format_modal_option_row,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult

logger = get_logger(__name__)

# ── Tab definitions ──────────────────────────────────────
_TABS: list[tuple[str, str]] = [
    ("Permissions", "permissions"),
    ("Recently Denied", "recently_denied"),
    ("Allow", "allow"),
    ("Ask", "ask"),
    ("Deny", "deny"),
    ("Workspace", "workspace"),
]


class _PermSearchInput(Input):
    """Search input that forwards arrow / enter keys to the OptionList."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key in ("down", "up", "enter"):
            try:
                screen = self.screen
                if isinstance(screen, PermissionsManagerScreen):
                    if event.key == "down":
                        screen.action_cursor_down()
                    elif event.key == "up":
                        screen.action_cursor_up()
                    elif event.key == "enter":
                        screen.action_select_item()
                    event.prevent_default()
                    event.stop()
                    return
            except Exception:
                pass
        await super()._on_key(event)


class PermissionsManagerScreen(OpsModalScreen[None]):
    """Full-screen modal for interactive permission management.

    Mirrors Claude Code's /permissions TUI adapted for OpsCloud's DevOps scopes
    and tool-pattern rules.
    """

    DIALOG_CLASSES: ClassVar[str] = "-workspace"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "prev_tab", "Prev tab", show=False, priority=True),
        Binding("right", "next_tab", "Next tab", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "select_item", "Select", show=False, priority=True),
        Binding("d", "delete_rule", "Delete", show=False),
        Binding("m", "cycle_mode", "Mode", show=False),
        Binding("escape", "close", "Close", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    PermissionsManagerScreen .perm-title {
        display: none;
    }

    PermissionsManagerScreen PillTabs {
        margin-bottom: 1;
    }

    PermissionsManagerScreen .perm-tab-bar {
        display: none;
    }

    PermissionsManagerScreen _PermSearchInput {
        margin-bottom: 1;
        border: solid $panel;
    }

    PermissionsManagerScreen _PermSearchInput:focus {
        border: solid $primary;
    }

    PermissionsManagerScreen OptionList#perm-options {
        height: 1fr;
        min-height: 5;
        background: $background;
        border: solid $primary 20%;
    }

    PermissionsManagerScreen OptionList#perm-options > .option-list--option-highlighted {
        background: $primary 18%;
        color: $text;
        text-style: bold;
    }

    PermissionsManagerScreen .perm-mode {
        height: auto;
        margin-top: 1;
        color: $secondary;
    }

    PermissionsManagerScreen .perm-help {
        display: none;
    }
    """

    def __init__(self, store: PermissionStore, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._store = store
        self._active_tab = "permissions"
        self._search_query = ""

    # ── Compose ──────────────────────────────────────────

    def compose_modal(self):
        yield Static("Permissions", classes="perm-title")
        yield PillTabs(
            tabs=[(tab_id, label) for label, tab_id in _TABS],
            initial_tab=self._active_tab,
            id="perm-tabs",
        )
        yield Static(self._build_tab_bar_text(), classes="perm-tab-bar", id="perm-tab-bar")

        # Search input
        yield _PermSearchInput(
            placeholder="Type to search rules...",
            id="perm-search-input",
        )

        # Content area
        yield OptionList(id="perm-options")

        # Mode indicator
        mode_desc = PERMISSION_MODES.get(self._store.mode, "")
        yield Static(
            f"Mode: [bold]{self._store.mode}[/bold] — {mode_desc}",
            classes="perm-mode",
            id="perm-mode-label",
            markup=True,
        )

        yield Static(
            "←/→ tabs · ↑/↓ navigate · Enter select · d delete · m mode · Esc close",
            classes="perm-help",
        )

    def on_mount(self) -> None:
        super().on_mount()
        glyphs = get_glyphs()
        self.set_modal_title("Permissions Manager", counter=f"mode: {self._store.mode}")
        self.set_modal_subtitle([
            ("←/→", "tabs"),
            (f"{glyphs.arrow_up}{glyphs.arrow_down}", "navigate"),
            ("enter", "select"),
            ("d", "delete"),
            ("m", "mode"),
            ("esc", "close"),
        ])
        self._render_tab_content()
        self._last_row_width: int | None = None
        self.call_after_refresh(self._render_tab_content)
        try:
            self.query_one("#perm-options", OptionList).focus()
        except Exception:
            pass

    def on_pill_tabs_tab_changed(self, event: PillTabs.TabChanged) -> None:
        self._active_tab = event.tab_id
        self._render_tab_content()

    # ── Tab bar rendering ────────────────────────────────

    def _build_tab_bar_text(self) -> str:
        """Build a single-line tab bar string with the active tab highlighted."""
        parts: list[str] = []
        for label, tab_id in _TABS:
            if tab_id == self._active_tab:
                parts.append(f"[bold underline]{label}[/bold underline]")
            else:
                parts.append(label)
        return "  ".join(parts)

    def _refresh_tab_bar(self) -> None:
        """Update the tab bar text after tab switch."""
        try:
            tab_bar = self.query_one("#perm-tab-bar", Static)
            tab_bar.update(self._build_tab_bar_text())
        except Exception:
            pass

    # ── Keyboard routing ─────────────────────────────────

    def on_key(self, event: events.Key) -> None:
        """Route keyboard between search Input and OptionList."""
        focused = self.app.focused
        search_input = self.query_one("#perm-search-input", _PermSearchInput)
        option_list = self.query_one("#perm-options", OptionList)

        if focused is search_input and event.key in ("down", "enter"):
            event.prevent_default()
            option_list.focus()
        elif focused is option_list and event.key == "/":
            event.prevent_default()
            search_input.focus()
        elif focused is option_list and event.is_printable and event.key not in (
            "d", "m", "left", "right",
        ):
            event.prevent_default()
            search_input.focus()
            search_input.value += event.character or ""
            search_input.cursor_position = len(search_input.value)

    # ── Tab navigation ───────────────────────────────────

    def action_prev_tab(self) -> None:
        idx = next((i for i, (_, tid) in enumerate(_TABS) if tid == self._active_tab), 0)
        new_idx = (idx - 1) % len(_TABS)
        self._switch_tab(_TABS[new_idx][1])

    def action_next_tab(self) -> None:
        idx = next((i for i, (_, tid) in enumerate(_TABS) if tid == self._active_tab), 0)
        new_idx = (idx + 1) % len(_TABS)
        self._switch_tab(_TABS[new_idx][1])

    def _switch_tab(self, tab_id: str) -> None:
        self._active_tab = tab_id
        try:
            tabs = self.query_one("#perm-tabs", PillTabs)
            tabs.activate_tab(tab_id)
        except Exception:
            pass
        self._refresh_tab_bar()
        self._render_tab_content()

    # ── Search ───────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "perm-search-input":
            self._search_query = event.value.strip().lower()
            self._render_tab_content()

    # ── Content rendering ────────────────────────────────

    def on_resize(self) -> None:
        width = self._option_content_width("#perm-options", fallback=82)
        if width != getattr(self, "_last_row_width", None):
            self._last_row_width = width
            self.call_after_refresh(self._render_tab_content)

    def _render_tab_content(self) -> None:
        option_list = self.query_one("#perm-options", OptionList)
        option_list.clear_options()
        w = self._option_content_width("#perm-options", fallback=82)

        if self._active_tab == "permissions":
            self._render_overview(option_list, width=w)
        elif self._active_tab == "recently_denied":
            self._render_recently_denied(option_list, width=w)
        elif self._active_tab in ("allow", "ask", "deny"):
            self._render_rule_list(option_list, self._active_tab, width=w)
        elif self._active_tab == "workspace":
            self._render_workspace(option_list, width=w)

    def _render_overview(self, option_list: OptionList, width: int = 82) -> None:
        """Permissions overview — all scopes with their current status."""
        for scope in sorted(VALID_SCOPES):
            if self._search_query and self._search_query not in scope.lower():
                continue

            status = self._store.evaluate(scope)
            if status == "allow":
                badge_style = "bold $success"
                status_desc = "granted"
            elif status == "ask":
                badge_style = "bold $warning"
                status_desc = "requires approval"
            else:
                badge_style = "bold $error"
                status_desc = "denied"

            content = format_modal_option_row(
                label=scope,
                badges=[(status.upper(), badge_style)],
                status=(status_desc, "dim"),
                width=width,
            )
            option_list.add_option(Option(content, id=f"scope:{scope}"))

        # Tool-pattern rules summary
        tool_rules = [
            r for r in (self._store.allow + self._store.ask + self._store.deny)
            if r.is_tool_pattern
        ]
        if tool_rules:
            option_list.add_option(Option("", disabled=True))
            option_list.add_option(
                Option(Content.from_markup("[bold $primary]── Tool-Pattern Rules ──[/]"), disabled=True)
            )
            for rule in tool_rules:
                if self._search_query and self._search_query not in rule.pattern.lower():
                    continue
                cat = self._find_rule_category(rule)
                b_style = (
                    "bold $success"
                    if cat == "allow"
                    else ("bold $warning" if cat == "ask" else "bold $error")
                )
                content = format_modal_option_row(
                    label=rule.pattern,
                    badges=[(cat.upper(), b_style)],
                    width=width,
                )
                option_list.add_option(Option(content, id=f"tool:{rule.pattern}"))

    def _render_recently_denied(self, option_list: OptionList, width: int = 82) -> None:
        """Show recently denied actions."""
        if not self._store.recently_denied:
            option_list.add_option(Option("  No recently denied actions.", disabled=True))
            return

        for i, action in enumerate(self._store.recently_denied):
            if self._search_query and self._search_query not in action.display_label.lower():
                continue
            ts = time.strftime("%H:%M:%S", time.localtime(action.denied_at))
            content = format_modal_option_row(
                label=action.display_label,
                description=action.comment or "",
                badges=[(ts, "dim")],
                width=width,
            )
            option_list.add_option(Option(content, id=f"denied:{i}"))

    def _render_rule_list(self, option_list: OptionList, category: str, width: int = 82) -> None:
        """Render allow/ask/deny rule list with add option."""
        cat_display = category.title()
        desc = {
            "allow": "OpsCloud won't ask before using allowed tools.",
            "ask": "OpsCloud will prompt for confirmation every time.",
            "deny": "These tools are completely blocked.",
        }.get(category, "")

        option_list.add_option(Option(f"  {desc}", disabled=True))
        option_list.add_option(Option("", disabled=True))

        rules = self._store._get_list(category)  # type: ignore[arg-type]
        filtered = [r for r in rules if not self._search_query or self._search_query in r.pattern.lower()]

        if not filtered:
            option_list.add_option(Option("  (no rules)", disabled=True))
        else:
            for rule in filtered:
                b_style = (
                    "bold $success"
                    if category == "allow"
                    else ("bold $warning" if category == "ask" else "bold $error")
                )
                badges: list[tuple[str, str]] = [(category.upper(), b_style)]
                if rule.source != "session":
                    badges.append((rule.source.upper(), "dim"))
                content = format_modal_option_row(
                    label=rule.pattern,
                    badges=badges,
                    width=width,
                )
                option_list.add_option(Option(content, id=f"rule:{category}:{rule.pattern}"))

        option_list.add_option(Option("", disabled=True))
        option_list.add_option(
            Option(Content.from_markup("[bold $primary]+ Add a new rule...[/]"), id=f"add:{category}")
        )

    def _render_workspace(self, option_list: OptionList, width: int = 82) -> None:
        """Show all rules grouped by source."""
        all_rules: list[tuple[str, PermissionRule]] = []
        for rule in self._store.allow:
            all_rules.append(("allow", rule))
        for rule in self._store.ask:
            all_rules.append(("ask", rule))
        for rule in self._store.deny:
            all_rules.append(("deny", rule))

        # Group by source
        sources: dict[str, list[tuple[str, PermissionRule]]] = {}
        for cat, rule in all_rules:
            if self._search_query and self._search_query not in rule.pattern.lower():
                continue
            sources.setdefault(rule.source, []).append((cat, rule))

        source_labels = {
            "default": "Default Rules",
            "session": "Session Rules (this session only)",
            "config": "Config Rules (~/.opscloud/config.toml)",
            "mode": "Mode Preset Rules",
        }

        for source, items in sources.items():
            label = source_labels.get(source, str(source))
            option_list.add_option(
                Option(Content.from_markup(f"[bold $primary]── {label} ──[/]"), disabled=True)
            )
            for cat, rule in items:
                b_style = (
                    "bold $success"
                    if cat == "allow"
                    else ("bold $warning" if cat == "ask" else "bold $error")
                )
                content = format_modal_option_row(
                    label=rule.pattern,
                    badges=[(cat.upper(), b_style)],
                    width=width,
                )
                option_list.add_option(Option(content, id=f"ws:{cat}:{rule.pattern}"))
            option_list.add_option(Option("", disabled=True))

    # ── Actions ──────────────────────────────────────────

    def action_cursor_up(self) -> None:
        self.query_one("#perm-options", OptionList).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#perm-options", OptionList).action_cursor_down()

    def action_select_item(self) -> None:
        option_list = self.query_one("#perm-options", OptionList)
        if option_list.highlighted is None:
            return
        try:
            option = option_list.get_option_at_index(option_list.highlighted)
        except Exception:
            return

        opt_id = option.id
        if opt_id is None:
            return

        # Handle "Add a new rule..." option
        if opt_id.startswith("add:"):
            category = opt_id.split(":", 1)[1]
            self._show_add_rule_modal(category)
            return

        # Handle clicking a scope in overview — cycle its status
        if opt_id.startswith("scope:"):
            scope = opt_id.split(":", 1)[1]
            self._cycle_scope(scope)
            return

        # Handle clicking a recently denied action — offer to allow it
        if opt_id.startswith("denied:"):
            idx = int(opt_id.split(":", 1)[1])
            if idx < len(self._store.recently_denied):
                action = list(self._store.recently_denied)[idx]
                pattern = action.tool_name
                if action.args.get("command"):
                    cmd = str(action.args["command"])
                    pattern = f"{action.tool_name}({cmd})"
                self._store.add_rule("allow", pattern, source="session")
                self._persist()
                self._render_tab_content()
                self.app.notify(f"Added Allow rule: {pattern}", severity="information")
            return

    def action_delete_rule(self) -> None:
        """Delete the currently highlighted rule."""
        if self._active_tab not in ("allow", "ask", "deny"):
            return

        option_list = self.query_one("#perm-options", OptionList)
        if option_list.highlighted is None:
            return
        try:
            option = option_list.get_option_at_index(option_list.highlighted)
        except Exception:
            return

        opt_id = option.id
        if opt_id is None or not opt_id.startswith("rule:"):
            return

        parts = opt_id.split(":", 2)
        if len(parts) < 3:
            return
        category, pattern = parts[1], parts[2]
        if self._store.remove_rule(category, pattern):  # type: ignore[arg-type]
            self._persist()
            self._render_tab_content()
            self.app.notify(f"Removed: {pattern}", severity="information")

    def action_cycle_mode(self) -> None:
        """Cycle through permission modes."""
        mode_names = list(PERMISSION_MODES.keys())
        idx = mode_names.index(self._store.mode) if self._store.mode in mode_names else 0
        new_mode = mode_names[(idx + 1) % len(mode_names)]
        self._store.apply_mode(new_mode)
        self._persist()

        # Update modal title and mode label
        self.set_modal_title("Permissions Manager", counter=f"mode: {new_mode}")
        mode_desc = PERMISSION_MODES.get(new_mode, "")
        try:
            label = self.query_one("#perm-mode-label", Static)
            label.update(f"Mode: [bold]{new_mode}[/bold] — {mode_desc}")
        except Exception:
            pass

        self._render_tab_content()
        self.app.notify(f"Permission mode: {new_mode}", severity="information")

    def action_close(self) -> None:
        self._persist()
        self.dismiss(None)

    # ── Helpers ──────────────────────────────────────────

    def _cycle_scope(self, scope: str) -> None:
        """Cycle a scope through allow → ask → deny → allow."""
        current = self._store.evaluate(scope)
        cycle = {"allow": "ask", "ask": "deny", "deny": "allow"}
        new_status = cycle[current]
        self._store.add_rule(new_status, scope, source="session")  # type: ignore[arg-type]
        self._persist()
        self._render_tab_content()

        self.app.notify(
            f"[{new_status}] {scope}: {new_status}",
            severity="information",
        )

    def _find_rule_category(self, rule: PermissionRule) -> str:
        """Find which category a rule belongs to."""
        if rule in self._store.allow:
            return "allow"
        if rule in self._store.ask:
            return "ask"
        if rule in self._store.deny:
            return "deny"
        return "unknown"

    def _show_add_rule_modal(self, category: str) -> None:
        """Push a small modal to add a new rule."""
        screen = _AddRuleScreen(category)
        self.app.push_screen(screen, callback=self._on_add_rule_result)

    def _on_add_rule_result(self, result: tuple[str, str] | None) -> None:
        """Handle result from add-rule modal."""
        if result is None:
            return
        category, pattern = result
        if pattern:
            self._store.add_rule(category, pattern.strip(), source="session")  # type: ignore[arg-type]
            self._persist()
            self._render_tab_content()
            self.app.notify(f"Added {category} rule: {pattern}", severity="information")

    def _persist(self) -> None:
        """Save permission state to config.toml."""
        try:
            save_permission_store(self._store)
        except Exception:
            logger.warning("Failed to persist permissions", exc_info=True)


# ── Add Rule Sub-modal ───────────────────────────────────

class _AddRuleScreen(OpsModalScreen[tuple[str, str] | None]):
    """Small modal for adding a new permission rule."""

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    _AddRuleScreen .add-rule-title {
        display: none;
    }

    _AddRuleScreen .add-rule-help {
        color: $text-muted;
        text-style: italic;
        margin-bottom: 1;
        height: auto;
    }

    _AddRuleScreen Input {
        margin-bottom: 1;
        border: solid $panel;
    }

    _AddRuleScreen Input:focus {
        border: solid $primary;
    }

    _AddRuleScreen .add-rule-buttons {
        layout: horizontal;
        height: 3;
        align: right middle;
    }

    _AddRuleScreen .add-rule-buttons Button {
        margin-left: 1;
        min-width: 12;
    }
    """

    def __init__(self, category: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._category = category

    def compose_modal(self):
        cat_display = self._category.title()
        yield Static(f"Add {cat_display} Rule", classes="add-rule-title")
        yield Static(
            "Scope: shell:read, file:write, infra:apply\n"
            "Pattern: Shell(kubectl get *), FileEdit(src/*)",
            classes="add-rule-help",
        )
        yield Input(
            placeholder="Enter rule pattern…",
            id="add-rule-input",
        )
        with Container(classes="add-rule-buttons"):
            yield Button("Add", variant="primary", id="btn-add-rule")
            yield Button("Cancel", variant="default", id="btn-cancel-rule")

    def on_mount(self) -> None:
        super().on_mount()
        cat_display = self._category.title()
        self.set_modal_title(f"Add {cat_display} Rule")
        self.set_modal_subtitle([
            ("enter", "add"),
            ("esc", "cancel"),
        ])
        try:
            self.query_one("#add-rule-input", Input).focus()
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-add-rule":
            pattern = self.query_one("#add-rule-input", Input).value.strip()
            if pattern:
                self.dismiss((self._category, pattern))
            else:
                self.app.notify("Rule pattern cannot be empty.", severity="warning")
        elif event.button.id == "btn-cancel-rule":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "add-rule-input":
            pattern = event.value.strip()
            if pattern:
                self.dismiss((self._category, pattern))

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["PermissionsManagerScreen"]
