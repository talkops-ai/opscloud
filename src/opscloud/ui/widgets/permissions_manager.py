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
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from opscloud.ui.permission_store import (
    PERMISSION_MODES,
    PermissionRule,
    PermissionStore,
    VALID_SCOPES,
    save_permission_store,
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


class PermissionsManagerScreen(ModalScreen[None]):
    """Full-screen modal for interactive permission management.

    Mirrors Claude Code's /permissions TUI adapted for OpsCloud's DevOps scopes
    and tool-pattern rules.
    """

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

    # ── CSS — matches AuthManagerScreen / ConfigManagerScreen patterns ──
    CSS = """
    PermissionsManagerScreen {
        align: center middle;
        background: $background 70%;
    }

    PermissionsManagerScreen > Vertical {
        width: 90;
        max-width: 95%;
        height: 85%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    /* ── Title ─────────────────────────── */
    PermissionsManagerScreen .perm-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    /* ── Tab bar ───────────────────────── */
    PermissionsManagerScreen .perm-tab-bar {
        height: auto;
        color: $text-muted;
        text-align: center;
        margin-bottom: 1;
    }

    /* ── Search ────────────────────────── */
    PermissionsManagerScreen _PermSearchInput {
        margin-bottom: 1;
        border: solid $panel;
    }

    PermissionsManagerScreen _PermSearchInput:focus {
        border: solid $primary;
    }

    /* ── Content ───────────────────────── */
    PermissionsManagerScreen OptionList#perm-options {
        height: 1fr;
        min-height: 5;
        background: $background;
    }

    PermissionsManagerScreen OptionList#perm-options > .option-list--option-highlighted {
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }

    PermissionsManagerScreen OptionList#perm-options > .option-list--option-hover {
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }

    /* ── Mode indicator ────────────────── */
    PermissionsManagerScreen .perm-mode {
        height: auto;
        margin-top: 1;
        color: $secondary;
    }

    /* ── Help bar ──────────────────────── */
    PermissionsManagerScreen .perm-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        text-align: center;
        margin-top: 1;
    }
    """

    def __init__(self, store: PermissionStore, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._store = store
        self._active_tab = "permissions"
        self._search_query = ""

    # ── Compose ──────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Permissions", classes="perm-title")

            # Tab bar as a single Static with formatted text
            yield Static(self._build_tab_bar_text(), classes="perm-tab-bar", id="perm-tab-bar")

            # Search input
            yield _PermSearchInput(
                placeholder="Type to search rules...",
                id="perm-search-input",
            )

            # Content area (OptionList fills remaining space via 1fr)
            yield OptionList(id="perm-options")

            # Mode indicator
            mode_desc = PERMISSION_MODES.get(self._store.mode, "")
            yield Static(
                f"Mode: [bold]{self._store.mode}[/bold] — {mode_desc}",
                classes="perm-mode",
                id="perm-mode-label",
                markup=True,
            )

            # Help bar
            yield Static(
                "←/→ tabs · ↑/↓ navigate · Enter select · d delete · m mode · Esc close",
                classes="perm-help",
            )

    def on_mount(self) -> None:
        self._render_tab_content()
        # Focus the OptionList so arrow keys navigate immediately
        try:
            self.query_one("#perm-options", OptionList).focus()
        except Exception:
            pass

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
        self._refresh_tab_bar()
        self._render_tab_content()

    # ── Search ───────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "perm-search-input":
            self._search_query = event.value.strip().lower()
            self._render_tab_content()

    # ── Content rendering ────────────────────────────────

    def _render_tab_content(self) -> None:
        option_list = self.query_one("#perm-options", OptionList)
        option_list.clear_options()

        if self._active_tab == "permissions":
            self._render_overview(option_list)
        elif self._active_tab == "recently_denied":
            self._render_recently_denied(option_list)
        elif self._active_tab in ("allow", "ask", "deny"):
            self._render_rule_list(option_list, self._active_tab)
        elif self._active_tab == "workspace":
            self._render_workspace(option_list)

    def _render_overview(self, option_list: OptionList) -> None:
        """Permissions overview — all scopes with their current status."""
        for scope in sorted(VALID_SCOPES):
            if self._search_query and self._search_query not in scope.lower():
                continue

            status = self._store.evaluate(scope)
            if status == "allow":
                icon, label = "[allow]", "granted"
            elif status == "ask":
                icon, label = "[ask]", "requires approval"
            else:
                icon, label = "[deny]", "denied"

            option_list.add_option(Option(f"  {icon} {scope}: {label}", id=f"scope:{scope}"))

        # Tool-pattern rules summary
        tool_rules = [
            r for r in (self._store.allow + self._store.ask + self._store.deny)
            if r.is_tool_pattern
        ]
        if tool_rules:
            option_list.add_option(Option("", disabled=True))
            option_list.add_option(Option("  Tool-Pattern Rules", disabled=True))
            for rule in tool_rules:
                if self._search_query and self._search_query not in rule.pattern.lower():
                    continue
                cat = self._find_rule_category(rule)
                icon = {"allow": "[allow]", "ask": "[ask]", "deny": "[deny]"}.get(cat, "[?]")
                option_list.add_option(Option(f"  {icon} {rule.pattern} [{cat}]", id=f"tool:{rule.pattern}"))

    def _render_recently_denied(self, option_list: OptionList) -> None:
        """Show recently denied actions."""
        if not self._store.recently_denied:
            option_list.add_option(Option("  No recently denied actions.", disabled=True))
            return

        for i, action in enumerate(self._store.recently_denied):
            if self._search_query and self._search_query not in action.display_label.lower():
                continue
            ts = time.strftime("%H:%M:%S", time.localtime(action.denied_at))
            label = f"  [{ts}] {action.display_label}"
            if action.comment:
                label += f" — {action.comment[:30]}"
            option_list.add_option(Option(label, id=f"denied:{i}"))

    def _render_rule_list(self, option_list: OptionList, category: str) -> None:
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
                source_tag = f" [{rule.source}]" if rule.source != "session" else ""
                icon = {"allow": "[allow]", "ask": "[ask]", "deny": "[deny]"}.get(category, "")
                option_list.add_option(Option(
                    f"  {icon} {rule.pattern}{source_tag}",
                    id=f"rule:{category}:{rule.pattern}",
                ))

        option_list.add_option(Option("", disabled=True))
        option_list.add_option(Option("  + Add a new rule...", id=f"add:{category}"))

    def _render_workspace(self, option_list: OptionList) -> None:
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
            option_list.add_option(Option(f"  {label}", disabled=True))
            for cat, rule in items:
                icon = {"allow": "[allow]", "ask": "[ask]", "deny": "[deny]"}.get(cat, "")
                option_list.add_option(Option(
                    f"    {icon} {rule.pattern} [{cat}]",
                    id=f"ws:{cat}:{rule.pattern}",
                ))
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

        # Update mode label
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

class _AddRuleScreen(ModalScreen[tuple[str, str] | None]):
    """Small modal for adding a new permission rule."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    _AddRuleScreen {
        align: center middle;
        background: $background 70%;
    }

    _AddRuleScreen > Vertical {
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 14;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    _AddRuleScreen .add-rule-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
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

    _AddRuleScreen .add-rule-buttons #btn-add-rule {
        background: $primary;
        color: $text;
        text-style: bold;
        border: tall $primary-lighten-2;
    }

    _AddRuleScreen .add-rule-buttons #btn-cancel-rule {
        background: $surface;
        color: $text;
        border: tall $panel;
    }
    """

    def __init__(self, category: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._category = category

    def compose(self) -> ComposeResult:
        cat_display = self._category.title()
        with Vertical():
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
        self.query_one("#add-rule-input", Input).focus()

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
