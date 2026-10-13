"""Interactive plugin manager modal screen for /plugins.

Full-screen modal for browsing, installing, enabling/disabling, and managing
plugins and marketplaces in OpsCloud.
"""

from __future__ import annotations

import asyncio
from opscloud.utils.logger import get_logger
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable, Sequence, Set as AbstractSet
from typing import TYPE_CHECKING, ClassVar, Literal

from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.events import Key
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Rule, Static
from textual.widgets.option_list import Option

from opscloud.ui.widgets.modal_base import (
    ModalSearchInput,
    OpsModalScreen,
    PillTabs,
    format_modal_option_row,
)

from opscloud.config.settings import get_glyphs, is_ascii_mode
from opscloud.plugins import (
    add_marketplace_source,
    discover_marketplace_plugins,
    install_plugin,
    remove_marketplace,
    set_installed_plugin_enabled,
    uninstall_plugin,
)
from opscloud.plugins.manifest import (
    UnsupportedComponent,
)
from opscloud.plugins.marketplace import (
    MarketplaceError,
    load_marketplace_location,
    materialize_plugin_source,
    redact_marketplace_source,
    redact_urls_in_text,
)
from opscloud.plugins.models import InstallScope
from opscloud.plugins.store import (
    load_all_enabled_plugin_ids,
    load_installed_plugin_entries,
    load_installed_plugins,
    load_marketplace_records,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from opscloud.mcp.mcp_info import MCPServerInfo

logger = get_logger(__name__)

PluginTab = Literal["discover", "installed", "marketplaces", "errors"]
PluginManagerView = Literal[
    "list",
    "add_marketplace",
    "plugin_details",
    "installed_details",
    "marketplace_details",
    "confirm_remove_marketplace",
]
PluginManagerAction = Literal["reload", "later", "check_failed"]
PluginManagerResult = PluginManagerAction | None
PluginClosePhase = Literal["browsing", "checking", "reload_prompt"]
PluginLoadState = Literal["disabled", "pending_reload", "enabled", "error"]

TAB_LABELS: dict[PluginTab, str] = {
    "discover": "Discover",
    "installed": "Installed",
    "marketplaces": "Marketplaces",
    "errors": "Errors",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class _PluginRow:
    plugin_id: str
    description: str
    enabled: bool
    version: str | None
    author: str | None
    display_name: str = ""
    skill_count: int | None = None
    skill_names: tuple[str, ...] = ()
    mcp_connected: bool | None = None
    mcp_server_names: tuple[str, ...] = ()
    mcp_login_servers: tuple[str, ...] = ()
    unsupported_components: tuple[UnsupportedComponent, ...] = ()
    session_loaded: bool = False
    load_error: str | None = None
    scope: InstallScope | None = None
    project_path: str | None = None

    @property
    def load_state(self) -> PluginLoadState:
        if self.load_error:
            return "error"
        if self.enabled != self.session_loaded:
            return "pending_reload"
        if self.enabled:
            return "enabled"
        return "disabled"

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        return self.plugin_id.partition("@")[0]


@dataclass(frozen=True, slots=True)
class _MarketplaceRow:
    name: str
    source: str
    plugin_count: int | None
    installed_count: int
    error: str | None = None

    @property
    def has_error(self) -> bool:
        return self.error is not None


@dataclass(frozen=True, slots=True)
class _ManagerState:
    available_plugins: tuple[_PluginRow, ...]
    installed_plugins: tuple[_PluginRow, ...]
    marketplaces: tuple[_MarketplaceRow, ...]
    errors: tuple[str, ...]


# ── Content Formatters ────────────────────────────────────


def _plugin_options(
    rows: tuple[_PluginRow, ...],
    *,
    action: Literal["detail", "installed"],
    status: str | None,
    width: int = 72,
) -> list[Option]:
    options: list[Option] = []
    for index, row in enumerate(rows):
        if index > 0:
            options.append(Option(" ", id=f"spacer:{index}", disabled=True))
        options.append(
            Option(
                _plugin_prompt(
                    row,
                    status=status,
                    is_selected=False,
                    width=width,
                ),
                id=f"{action}:{row.plugin_id}",
            )
        )
    return options


def _load_state_label(row: _PluginRow) -> str | None:
    glyphs = get_glyphs()
    if row.load_state == "error":
        return "error"
    if row.load_state == "pending_reload":
        return "pending /reload"
    if row.load_state == "enabled":
        return f"{glyphs.checkmark} enabled"
    return None


def _plugin_prompt(
    row: _PluginRow,
    *,
    status: str | None = None,
    is_selected: bool = False,
    width: int = 76,
) -> Content:
    glyphs = get_glyphs()
    _, _, marketplace = row.plugin_id.partition("@")

    badges: list[str | tuple[str, str]] = [("plugin", "$mode-command")]
    if marketplace:
        badges.append((marketplace, "$secondary"))
    if row.scope:
        badges.append((str(row.scope), "dim"))

    status_tuple: str | tuple[str, str] | None = None
    meta_parts: list[str] = []

    if row.load_state == "enabled":
        status_tuple = (f"{glyphs.checkmark} Enabled", "bold $success")
    elif row.load_state == "error":
        status_tuple = (f"{glyphs.error} Error", "bold $error")
    elif row.load_state == "pending_reload":
        status_tuple = ("pending /reload", "dim")
    elif status:
        status_tuple = (status, "dim")

    if row.skill_count:
        skill_label = "skill" if row.skill_count == 1 else "skills"
        meta_parts.append(f"{row.skill_count} {skill_label}")

    if row.version:
        meta_parts.append(f"v{row.version}")

    if row.load_state == "enabled" and row.mcp_connected is False:
        status_tuple = ("run /reload to connect", "bold $warning")

    meta_str = f" {glyphs.separator} ".join(meta_parts) if meta_parts else None

    # First sentence of description in dim, truncated cleanly
    raw_desc = (row.description or "No description provided.").strip()
    if ". " in raw_desc:
        first_sent = raw_desc.split(". ")[0].strip() + "."
        desc = first_sent if len(first_sent) >= 20 else raw_desc
    else:
        desc = raw_desc

    return format_modal_option_row(
        label=row.label,
        description=desc,
        badges=badges,
        meta=meta_str,
        status=status_tuple,
        width=width,
        is_selected=is_selected,
    )


def _install_details_options(*, has_project: bool) -> list[Option]:
    options = [
        Option("Install for you", id="action:install-user"),
    ]
    if has_project:
        options.extend([
            Option(
                "Install for all collaborators on this repository",
                id="action:install-project",
            ),
            Option(
                "Install for you, in this repo only",
                id="action:install-local",
            ),
        ])
    options.append(Option("Back to plugin list", id="details-back"))
    return options


def _installed_details_options(row: _PluginRow, *, divider_width: int) -> list[Option]:
    glyphs = get_glyphs()
    return [
        Option(
            "Disable plugin" if row.enabled else "Enable plugin",
            id="action:toggle-enabled",
        ),
        Option(Content.styled("Uninstall", "bold"), id="action:uninstall"),
        Option(
            Content.styled(glyphs.box_horizontal * divider_width, "dim"),
            id="details-divider",
            disabled=True,
        ),
        Option("Back to plugin list", id="details-back"),
    ]


def _component_summary_lines(row: _PluginRow) -> list[str]:
    lines: list[str] = []
    if row.skill_names:
        lines.append(f"Skills: {', '.join(row.skill_names)}")
    elif row.skill_count:
        lines.append(f"Skills: {row.skill_count}")
    if row.mcp_server_names:
        lines.append(f"MCP: {', '.join(row.mcp_server_names)}")
    if row.unsupported_components:
        names = ", ".join(f"{name}/" for name in row.unsupported_components)
        lines.append(f"Unsupported (not loaded): {names}")
    return lines


def _plugin_details_content(row: _PluginRow) -> Content:
    _, _, marketplace = row.plugin_id.partition("@")
    parts: list[Content | str] = [
        Content.styled("Plugin details", "bold"),
        "\n\n",
        Content.styled(row.label, "bold"),
        "\n",
        Content.styled(f"from {marketplace}", "dim"),
    ]
    if row.version:
        parts.extend(["\n", Content.styled(f"Version: {row.version}", "dim")])
    if row.description:
        parts.extend(["\n\n", row.description])
    if row.author:
        parts.extend(["\n\n", Content.styled(f"By: {row.author}", "dim")])
    parts.extend(["\n\n", Content.styled("Will install:", "bold")])
    summary_lines = _component_summary_lines(row) or ["Skills and MCP servers if present."]
    for line in summary_lines:
        parts.extend(["\n  ", Content.styled(line, "dim")])
    parts.extend(
        [
            "\n\n",
            Content.styled(
                "Make sure you trust a plugin before installing or using it.",
                "dim",
            ),
        ]
    )
    return Content.assemble(*parts)


def _installed_plugin_details_content(row: _PluginRow) -> Content:
    _, _, marketplace = row.plugin_id.partition("@")
    parts: list[Content | str] = [
        Content.styled(f"{row.label} @ {marketplace}", "bold")
    ]
    if row.version:
        parts.extend(["\n", Content.styled(f"Version: {row.version}", "dim")])
    if row.scope:
        scope_str = f"Scope: {row.scope}"
        if row.project_path:
            scope_str += f" ({row.project_path})"
        parts.extend(["\n", Content.styled(scope_str, "dim")])
    if row.description:
        parts.extend(["\n\n", row.description])
    if row.author:
        parts.extend(["\n\n", Content.styled(f"Author: {row.author}", "dim")])
    parts.extend(["\n\n"])
    glyphs = get_glyphs()
    if row.load_state == "enabled":
        parts.append(Content.styled(f"Status: {glyphs.checkmark} Enabled", "$success"))
    elif row.load_state == "disabled":
        parts.append(Content.styled("Status: Disabled", "dim"))
    elif row.load_state == "error":
        parts.append(Content.styled(f"Status: Error — {row.load_error}", "$error"))
    else:
        parts.append(Content.styled("Status: Pending reload", "dim"))

    parts.extend(["\n\n", Content.styled("Installed components:", "bold")])
    summary_lines = _component_summary_lines(row) or ["No components found."]
    for line in summary_lines:
        parts.extend(["\n  ", Content.styled(line, "dim")])
    return Content.assemble(*parts)


def _marketplace_label(
    row: _MarketplaceRow,
    *,
    is_selected: bool = False,
    width: int = 76,
) -> Content:
    glyphs = get_glyphs()
    badges: list[str | tuple[str, str]] = [("marketplace", "$mode-command")]
    if row.has_error:
        status: str | tuple[str, str] | None = (f"{glyphs.error} Error", "bold $error")
        meta = row.error
    else:
        status = None
        count_label = "plugin" if row.plugin_count == 1 else "plugins"
        installed_label = "installed"
        meta = f"{row.plugin_count} {count_label} {glyphs.separator} {row.installed_count} {installed_label}"

    return format_modal_option_row(
        label=row.name,
        description=f"Source: {row.source}",
        badges=badges,
        meta=meta,
        status=status,
        width=width,
        is_selected=is_selected,
    )


def _marketplace_details_options(*, is_refreshing: bool = False) -> list[Option]:
    refresh_label = "Refreshing marketplace..." if is_refreshing else "Refresh marketplace"
    return [
        Option(
            Content.styled(refresh_label, "cyan" if is_refreshing else "bold"),
            id="action:refresh-marketplace",
            disabled=is_refreshing,
        ),
        Option(
            Content.styled("Remove marketplace", "bold"),
            id="action:remove-marketplace",
            disabled=is_refreshing,
        ),
        Option("Back to marketplace list", id="details-back"),
    ]


def _confirm_marketplace_removal_options(row: _MarketplaceRow) -> list[Option]:
    label = "installed plugin" if row.installed_count == 1 else "installed plugins"
    return [
        Option(
            Content.styled(
                f"Remove marketplace and {row.installed_count} {label}", "bold"
            ),
            id="action:confirm-remove-marketplace",
        ),
        Option("Cancel", id="details-back"),
    ]


def _marketplace_details_content(row: _MarketplaceRow) -> Content:
    available = "Unavailable" if row.has_error else f"{row.plugin_count} available"
    return Content.assemble(
        Content.styled(row.name, "bold"),
        "\n",
        Content.styled(f"Source: {row.source}", "dim"),
        "\n",
        Content.styled(f"Plugins: {available}", "dim"),
        "\n",
        Content.styled(f"Installed: {row.installed_count}", "dim"),
    )


def _marketplace_removal_content(row: _MarketplaceRow) -> Content:
    suffix = "s" if row.installed_count != 1 else ""
    if row.installed_count:
        detail = (
            f"This removes the marketplace and uninstalls {row.installed_count} "
            f"plugin{suffix} from it."
        )
    else:
        detail = "This removes the marketplace from your installed list."
    return Content.assemble(
        Content.styled(f"Remove marketplace {row.name}?", "bold"),
        "\n\n",
        Content.styled(detail, "dim"),
    )


# ── State Loader ──────────────────────────────────────────


def _load_manager_state(
    mcp_server_info: Sequence[MCPServerInfo] = (),
    *,
    loaded_plugin_ids: AbstractSet[str] = frozenset(),
    project_root: Path | None = None,
) -> _ManagerState:
    records = load_marketplace_records(project_root=project_root)
    enabled = load_all_enabled_plugin_ids(project_root=project_root)
    installed = load_installed_plugins(project_root=project_root)
    all_entries = load_installed_plugin_entries()
    from opscloud.plugins.store import load_all_disabled_plugin_ids

    disabled = load_all_disabled_plugin_ids(project_root=project_root)
    errors: list[str] = []
    plugin_result = discover_marketplace_plugins(project_root=project_root)
    errors.extend(plugin_result.warnings)
    discovered = {instance.plugin_id: instance for instance in plugin_result.plugins}
    available_plugins: list[_PluginRow] = []
    installed_plugins: list[_PluginRow] = []
    marketplaces: list[_MarketplaceRow] = []

    for name, record in sorted(records.items()):
        try:
            marketplace = load_marketplace_location(Path(record.install_location))
        except MarketplaceError as exc:
            detail = redact_urls_in_text(str(exc))
            if record.source_type not in {"directory", "file"}:
                detail = detail.replace(record.install_location, "<managed cache>")
            errors.append(f"{name}: {detail}")
            marketplaces.append(
                _MarketplaceRow(
                    name,
                    redact_marketplace_source(record.source),
                    None,
                    sum(plugin_id.endswith(f"@{name}") for plugin_id in installed),
                    detail,
                )
            )
            continue

        is_project_marketplace = record.is_project

        installed_cnt = (
            len([p for p in marketplace.plugins if f"{p.name}@{marketplace.name}" not in disabled])
            if is_project_marketplace
            else sum(
                plugin_id.endswith(f"@{marketplace.name}")
                for plugin_id in installed
            )
        )
        marketplaces.append(
            _MarketplaceRow(
                marketplace.name,
                redact_marketplace_source(record.source),
                len(marketplace.plugins),
                installed_cnt,
            )
        )
        errors.extend(
            f"{marketplace.name}: {warning}" for warning in marketplace.warnings
        )
        for plugin in marketplace.plugins:
            plugin_id = f"{plugin.name}@{marketplace.name}"
            is_disabled = plugin_id in disabled
            is_enabled = plugin_id in enabled or (is_project_marketplace and not is_disabled)
            is_installed = plugin_id in installed or (is_project_marketplace and not is_disabled)
            instance = discovered.get(plugin_id)

            if instance is None and is_project_marketplace:
                source_root = materialize_plugin_source(marketplace, plugin)
                if source_root is not None:
                    from opscloud.plugins.project_plugins import _build_plugin_instance

                    instance, _ = _build_plugin_instance(
                        plugin.name, marketplace.name, source_root, plugin.name
                    )

            display_name = (
                plugin.display_name
                or (instance.manifest.display_name if instance and instance.manifest else None)
                or plugin.name
            )
            author = (
                plugin.author.get("name") if isinstance(plugin.author, dict) else plugin.author
            )
            # Determine scope and project_path from install entries
            entry_list = all_entries.get(plugin_id, [])
            primary_entry = None
            if entry_list:
                if project_root:
                    for e in entry_list:
                        if e.project_path and (
                            e.project_path == str(project_root)
                            or Path(e.project_path).resolve() == project_root.resolve()
                        ):
                            primary_entry = e
                            break
                if primary_entry is None:
                    for e in entry_list:
                        if e.scope in {"user", "global"}:
                            primary_entry = e
                            break
                if primary_entry is None:
                    primary_entry = entry_list[0]

            scope: InstallScope | None = (
                primary_entry.scope
                if primary_entry
                else ("project" if is_project_marketplace else None)
            )
            project_path: str | None = (
                primary_entry.project_path
                if primary_entry
                else (str(project_root) if is_project_marketplace and project_root else None)
            )
            row = _PluginRow(
                plugin_id=plugin_id,
                description=plugin.description or "",
                enabled=is_enabled,
                version=instance.version if instance else getattr(plugin, "version", None),
                author=author,
                display_name=display_name,
                skill_count=len(instance.inventory.skills) if instance else None,
                skill_names=tuple(p.stem for p in instance.inventory.skills) if instance else (),
                mcp_connected=None,
                mcp_server_names=(),
                unsupported_components=instance.inventory.unsupported if instance else (),
                session_loaded=plugin_id in loaded_plugin_ids,
                scope=scope if is_installed else None,
                project_path=project_path if is_installed else None,
            )
            (installed_plugins if is_installed else available_plugins).append(row)

    # Ensure any installed plugin not listed in active marketplaces still appears in installed_plugins
    seen_installed_ids = {row.plugin_id for row in installed_plugins}
    for plugin_id, entry in sorted(installed.items()):
        if plugin_id not in seen_installed_ids:
            entry_list = all_entries.get(plugin_id, [entry])
            primary_entry = entry_list[0] if entry_list else entry
            instance = discovered.get(plugin_id)
            row = _PluginRow(
                plugin_id=plugin_id,
                description="",
                enabled=plugin_id in enabled,
                version=instance.version if instance else primary_entry.version,
                author=None,
                display_name=instance.manifest.display_name if instance and instance.manifest and instance.manifest.display_name else plugin_id.partition("@")[0],
                skill_count=len(instance.inventory.skills) if instance else None,
                skill_names=tuple(p.stem for p in instance.inventory.skills) if instance else (),
                mcp_connected=None,
                mcp_server_names=(),
                unsupported_components=instance.inventory.unsupported if instance else (),
                session_loaded=plugin_id in loaded_plugin_ids,
                scope=primary_entry.scope,
                project_path=primary_entry.project_path,
            )
            installed_plugins.append(row)

    return _ManagerState(
        tuple(available_plugins),
        tuple(installed_plugins),
        tuple(marketplaces),
        tuple(dict.fromkeys(errors)),
    )


# ── UI Components ─────────────────────────────────────────


class PluginTabSelected(Message):
    """Message sent when a plugin tab label is clicked."""

    def __init__(self, tab: PluginTab) -> None:
        super().__init__()
        self.tab: PluginTab = tab


class PluginTabLabel(Static):
    """Clickable tab label header widget."""

    def __init__(self, tab: PluginTab, label: str) -> None:
        self.tab: PluginTab = tab
        self._label = label
        super().__init__(f"  {label}  ", id=f"plugin-tab-{tab}", classes="plugin-tab-label")

    def set_active(self, active: bool) -> None:
        self.update(f"> {self._label} <" if active else f"  {self._label}  ")
        if active:
            self.add_class("plugin-tab-active")
        else:
            self.remove_class("plugin-tab-active")

    def on_click(self) -> None:
        self.post_message(PluginTabSelected(self.tab))


# ── Main Screen ───────────────────────────────────────────


class PluginManagerScreen(OpsModalScreen[PluginManagerResult]):
    """Interactive modal screen for /plugins command."""

    DIALOG_CLASSES: ClassVar[str] = "-workspace"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("enter", "reload_plugins", "Reload", show=False, priority=True),
        Binding("left", "arrow_previous_tab", "Previous tab", show=False, priority=True),
        Binding("right", "arrow_next_tab", "Next tab", show=False, priority=True),
        Binding("tab", "next_tab", "Next tab", show=False, priority=True),
        Binding("shift+tab", "previous_tab", "Previous tab", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("slash", "focus_search", "Search", show=False, priority=True),
        Binding("ctrl+f", "focus_search", "Search", show=False, priority=True),
    ]

    CSS = """
    PluginManagerScreen #plugin-marketplace-source {
        margin-bottom: 1;
    }

    PluginManagerScreen .plugin-manager-status {
        color: $text-muted;
        margin-bottom: 1;
        height: auto;
    }

    PluginManagerScreen .plugin-manager-error {
        color: $error;
        margin-bottom: 1;
        height: auto;
    }

    PluginManagerScreen #plugin-manager-options {
        height: 1fr;
        min-height: 5;
        background: $background;
    }
    """

    AUTO_FOCUS = "#plugin-manager-options"
    _DIVIDER_FALLBACK_WIDTH: ClassVar[int] = 72

    _tabs: ClassVar[tuple[PluginTab, ...]] = (
        "discover",
        "installed",
        "marketplaces",
        "errors",
    )

    def __init__(
        self,
        *,
        mcp_server_info: Sequence[MCPServerInfo] = (),
        loaded_plugin_ids: AbstractSet[str] | None = None,
        project_root: Path | None = None,
        check_reload_required: Callable[[], Awaitable[bool | None]] | None = None,
    ) -> None:
        super().__init__()
        self._tab: PluginTab = "discover"
        self._mode: PluginManagerView = "list"
        self._mcp_server_info = mcp_server_info
        self._loaded_plugin_ids: frozenset[str] = frozenset(loaded_plugin_ids or ())
        self._project_root = project_root
        self._check_reload_required = check_reload_required
        self._close_phase: PluginClosePhase = "browsing"
        self._mutated: bool = False
        self._state = _ManagerState((), (), (), ())
        self._status: str | None = None
        self._error: str | None = None
        self._selected_plugin: _PluginRow | None = None
        self._selected_marketplace: _MarketplaceRow | None = None
        self._search_query = ""
        self._last_highlighted_index: int | None = None
        self._is_refreshing_marketplaces: bool = False

    def compose_modal(self) -> ComposeResult:
        yield PillTabs(
            [
                ("discover", "Discover"),
                ("installed", "Installed"),
                ("marketplaces", "Marketplaces"),
                ("errors", "Errors"),
            ],
            id="plugin-manager-tabs",
            initial_tab=self._tab,
        )
        yield Static("", id="plugin-manager-status", classes="plugin-manager-status")
        yield Static("", id="plugin-manager-error", classes="plugin-manager-error")
        yield ModalSearchInput(
            placeholder="Search plugins...",
            id="plugin-manager-search-box",
            input_id="plugin-manager-search",
        )
        yield OptionList(id="plugin-manager-options")
        yield Input(placeholder="", id="plugin-marketplace-source")

    async def on_mount(self) -> None:
        super().on_mount()
        self._status = "Loading plugins..."
        self._refresh_view()
        await self._refresh_state()
        if self._status == "Loading plugins...":
            self._status = None
            self._refresh_view()

    def _update_tabs(self) -> None:
        try:
            tabs = self.query_one("#plugin-manager-tabs", PillTabs)
            if tabs.active_tab != self._tab:
                tabs.activate_tab(self._tab)
            tabs.set_tab_count(
                "installed",
                len(self._state.installed_plugins) if self._state.installed_plugins else None,
            )
            tabs.set_tab_count(
                "marketplaces",
                len(self._state.marketplaces) if self._state.marketplaces else None,
            )
            tabs.set_tab_count(
                "errors",
                len(self._state.errors) if self._state.errors else None,
            )
        except NoMatches:
            pass

    def _update_tab_labels(self) -> None:
        self._update_tabs()
        for tab in self._tabs:
            try:
                self.query_one(f"#plugin-tab-{tab}", PluginTabLabel).set_active(
                    tab == self._tab
                )
            except NoMatches:
                pass

    def on_pill_tabs_tab_changed(self, event: PillTabs.TabChanged) -> None:
        if event.tab_id in self._tabs and event.tab_id != self._tab:
            self._select_tab(event.tab_id)  # type: ignore[arg-type]

    def on_plugin_tab_selected(self, event: PluginTabSelected) -> None:
        self._select_tab(event.tab)

    def _select_tab(self, tab: PluginTab) -> None:
        if self._mode == "add_marketplace":
            return
        if self._details_mode_active():
            self._mode = "list"
            self._selected_plugin = None
            self._selected_marketplace = None
        if tab != self._tab:
            self._search_query = ""
            try:
                search_input = self.query_one("#plugin-manager-search", Input)
                search_input.value = ""
            except NoMatches:
                pass
        self._tab = tab
        self._error = None
        self._refresh_view()

    def _search_available(self) -> bool:
        if self._mode != "list":
            return False
        if self._tab == "discover":
            return bool(self._state.marketplaces and self._state.available_plugins)
        if self._tab == "installed":
            return bool(self._state.installed_plugins)
        return False

    def _filtered_plugins(self, rows: Sequence[_PluginRow]) -> tuple[_PluginRow, ...]:
        query = self._search_query.strip().casefold()
        if not query:
            return tuple(rows)
        return tuple(
            row
            for row in rows
            if query in row.plugin_id.casefold()
            or query in row.label.casefold()
            or query in row.description.casefold()
            or (row.author and query in row.author.casefold())
            or any(query in s.casefold() for s in row.skill_names)
            or any(query in s.casefold() for s in row.mcp_server_names)
        )

    def _option_width(self) -> int:
        try:
            ol = self.query_one("#plugin-manager-options", OptionList)
            w = ol.scrollable_content_region.width
            if w > 20:
                return max(50, w - 2)
            w = ol.content_size.width
            if w > 20:
                return max(50, w - 4)
        except NoMatches:
            pass
        return 72

    def _current_options(self) -> list[Option]:
        glyphs = get_glyphs()
        width = self._option_width()
        if self._tab == "discover":
            if not self._state.marketplaces:
                return [
                    Option("No marketplaces installed. Add one to discover plugins.", id="empty", disabled=True),
                    Option("+ Add marketplace", id="add-marketplace"),
                ]
            if not self._state.available_plugins:
                return [Option("All available plugins are installed.", id="empty")]
            rows = self._filtered_plugins(self._state.available_plugins)
            if not rows:
                return [Option("No plugins match your search.", id="empty", disabled=True)]
            return _plugin_options(rows, action="detail", status=None, width=width)
        if self._tab == "installed":
            if not self._state.installed_plugins:
                return [Option("No plugins installed.", id="empty")]
            rows = self._filtered_plugins(self._state.installed_plugins)
            if not rows:
                return [Option("No installed plugins match your search.", id="empty", disabled=True)]
            return _plugin_options(rows, action="installed", status=None, width=width)
        if self._tab == "marketplaces":
            refresh_label = (
                f"{glyphs.retry} Refreshing all marketplaces..."
                if self._is_refreshing_marketplaces
                else f"{glyphs.retry} Refresh all marketplaces"
            )
            options = [
                Option(Content.styled("+ Add marketplace", "bold"), id="add-marketplace"),
                Option(
                    Content.styled(
                        refresh_label,
                        "cyan" if self._is_refreshing_marketplaces else "dim",
                    ),
                    id="refresh-all-marketplaces",
                    disabled=self._is_refreshing_marketplaces,
                ),
            ]
            if self._state.marketplaces:
                options.append(
                    Option(
                        Content.styled(glyphs.box_horizontal * self._divider_width(), "dim"),
                        id="marketplace-divider",
                        disabled=True,
                    )
                )
            for index, row in enumerate(self._state.marketplaces):
                if index > 0:
                    options.append(Option(" ", id=f"marketplace-spacer:{index}", disabled=True))
                options.append(
                    Option(
                        _marketplace_label(row, is_selected=False, width=width),
                        id=f"marketplace:{row.name}",
                    )
                )
            return options
        if not self._state.errors:
            return [Option("No plugin errors.", id="empty")]
        return [Option(Content(error), id="empty") for error in self._state.errors]

    def _divider_width(self) -> int:
        try:
            width = self.query_one("#plugin-manager-options", OptionList).content_size.width
        except NoMatches:
            return self._DIVIDER_FALLBACK_WIDTH
        return width if width > 0 else self._DIVIDER_FALLBACK_WIDTH

    @staticmethod
    def _nearest_enabled_index(options: OptionList, candidate: int) -> int | None:
        """Return the nearest selectable option index to candidate."""
        if not options.option_count:
            return None
        if not options.get_option_at_index(candidate).disabled:
            return candidate
        for index in range(candidate + 1, options.option_count):
            if not options.get_option_at_index(index).disabled:
                return index
        for index in range(candidate - 1, -1, -1):
            if not options.get_option_at_index(index).disabled:
                return index
        return None

    def _details_mode_active(self) -> bool:
        return self._mode in {
            "plugin_details",
            "installed_details",
            "marketplace_details",
            "confirm_remove_marketplace",
        }

    def _prompt_for_option_id(self, option_id: str, *, is_selected: bool) -> Content | None:
        width = self._option_width()
        if option_id.startswith("detail:"):
            plugin_id = option_id.removeprefix("detail:")
            row = next((r for r in self._state.available_plugins if r.plugin_id == plugin_id), None)
            if row:
                return _plugin_prompt(row, status=None, is_selected=is_selected, width=width)
        elif option_id.startswith("installed:"):
            plugin_id = option_id.removeprefix("installed:")
            row = next((r for r in self._state.installed_plugins if r.plugin_id == plugin_id), None)
            if row:
                return _plugin_prompt(row, status=None, is_selected=is_selected, width=width)
        elif option_id.startswith("marketplace:"):
            name = option_id.removeprefix("marketplace:")
            row = next((r for r in self._state.marketplaces if r.name == name), None)
            if row:
                return _marketplace_label(row, is_selected=is_selected, width=width)
        return None

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id != "plugin-manager-options":
            return
        idx = event.option_index
        if idx is None or idx == self._last_highlighted_index:
            return
        prev_idx = self._last_highlighted_index
        self._last_highlighted_index = idx
        options = event.option_list

        if prev_idx is not None and prev_idx < options.option_count:
            try:
                prev_opt = options.get_option_at_index(prev_idx)
                if prev_opt.id and not prev_opt.id.startswith("spacer:") and not prev_opt.disabled:
                    prompt = self._prompt_for_option_id(prev_opt.id, is_selected=False)
                    if prompt is not None:
                        options.replace_option_prompt_at_index(prev_idx, prompt)
            except Exception:
                pass

        if idx < options.option_count:
            try:
                curr_opt = options.get_option_at_index(idx)
                if curr_opt.id and not curr_opt.id.startswith("spacer:") and not curr_opt.disabled:
                    prompt = self._prompt_for_option_id(curr_opt.id, is_selected=True)
                    if prompt is not None:
                        options.replace_option_prompt_at_index(idx, prompt)
            except Exception:
                pass

    def _refresh_view(self) -> None:
        if self._close_phase != "browsing":
            return
        self._update_tabs()

        status_widget = self.query_one("#plugin-manager-status", Static)
        if self._mode == "plugin_details" and self._selected_plugin is not None:
            status_widget.update(_plugin_details_content(self._selected_plugin))
            status_widget.display = True
        elif self._mode == "installed_details" and self._selected_plugin is not None:
            status_widget.update(_installed_plugin_details_content(self._selected_plugin))
            status_widget.display = True
        elif self._mode == "marketplace_details" and self._selected_marketplace is not None:
            content = _marketplace_details_content(self._selected_marketplace)
            if self._status:
                content = Content.assemble(
                    content,
                    "\n\n",
                    Content.styled(
                        self._status,
                        "cyan" if self._is_refreshing_marketplaces else "$text-muted",
                    ),
                )
            status_widget.update(content)
            status_widget.display = True
        elif self._mode == "confirm_remove_marketplace" and self._selected_marketplace is not None:
            status_widget.update(_marketplace_removal_content(self._selected_marketplace))
            status_widget.display = True
        elif self._status:
            status_widget.update(self._status)
            status_widget.display = True
        else:
            status_widget.update("")
            status_widget.display = False

        error_widget = self.query_one("#plugin-manager-error", Static)
        if self._error:
            error_widget.update(self._error)
            error_widget.display = True
        else:
            error_widget.update("")
            error_widget.display = False

        options = self.query_one("#plugin-manager-options", OptionList)
        search_box = self.query_one("#plugin-manager-search-box", ModalSearchInput)
        search_input = self.query_one("#plugin-manager-search", Input)
        source_input = self.query_one("#plugin-marketplace-source", Input)

        try:
            tabs = self.query_one("#plugin-manager-tabs", PillTabs)
        except NoMatches:
            tabs = None

        if self._mode == "add_marketplace":
            self.set_modal_title("Add Marketplace")
            self.set_modal_subtitle([("↵", "add"), ("esc", "cancel")])
            if tabs is not None:
                tabs.display = False
            if self._status is None:
                status_widget.update(
                    "Enter marketplace source:\n"
                    "Examples:\n"
                    f"  {get_glyphs().bullet} owner/repo (GitHub)\n"
                    f"  {get_glyphs().bullet} https://example.com/marketplace.json\n"
                    f"  {get_glyphs().bullet} ./path/to/marketplace"
                )
                status_widget.display = True
            options.display = False
            search_box.display = False
            search_input.display = False
            source_input.display = True
            source_input.focus()
            return

        if tabs is not None:
            tabs.display = True

        if self._details_mode_active():
            if self._mode == "plugin_details":
                self.set_modal_title(f"Install {self._selected_plugin.label}" if self._selected_plugin else "Plugin Details")
                self.set_modal_subtitle([("↑↓", "select"), ("↵", "choose"), ("esc", "back")])
            elif self._mode == "installed_details":
                self.set_modal_title(self._selected_plugin.label if self._selected_plugin else "Installed Plugin")
                self.set_modal_subtitle([("↑↓", "select"), ("↵", "choose"), ("esc", "back")])
            elif self._mode == "marketplace_details":
                self.set_modal_title(self._selected_marketplace.name if self._selected_marketplace else "Marketplace Details")
                self.set_modal_subtitle([("↑↓", "select"), ("↵", "choose"), ("esc", "back")])
            elif self._mode == "confirm_remove_marketplace":
                self.set_modal_title("Remove Marketplace")
                self.set_modal_subtitle([("↑↓", "select"), ("↵", "confirm"), ("esc", "cancel")])

            search_box.display = False
            search_input.display = False
            source_input.display = False
            options.display = True
            options.clear_options()
            for option in self._active_details_options():
                options.add_option(option)
            options.focus()
            return

        source_input.display = False
        options.display = True
        search_available = self._search_available()
        search_box.display = search_available
        search_input.display = search_available
        if search_available and search_input.value != self._search_query:
            search_input.value = self._search_query

        if self._tab == "discover":
            count = len(self._state.available_plugins)
            counter = f"{count} available" if count else None
        elif self._tab == "installed":
            count = len(self._state.installed_plugins)
            counter = f"{count} installed" if count else None
        elif self._tab == "marketplaces":
            count = len(self._state.marketplaces)
            counter = f"{count} marketplaces" if count else None
        else:
            count = len(self._state.errors)
            counter = f"{count} errors" if count else None
        self.set_modal_title("Plugins", counter)

        if search_available:
            hints = [
                ("↑↓", "navigate"),
                ("↵", "details"),
                ("/", "search"),
                ("↔", "tabs"),
                ("esc", "close"),
            ]
        else:
            hints = [
                ("↑↓", "navigate"),
                ("↵", "select"),
                ("↔", "tabs"),
                ("esc", "close"),
            ]
        self.set_modal_subtitle(hints)

        highlighted = options.highlighted
        options.clear_options()
        self._last_highlighted_index = None
        for option in self._current_options():
            options.add_option(option)
        if options.option_count:
            candidate = 0 if highlighted is None else min(highlighted, options.option_count - 1)
            new_hl = self._nearest_enabled_index(options, candidate)
            options.highlighted = new_hl
            self._last_highlighted_index = new_hl
            if new_hl is not None:
                opt = options.get_option_at_index(new_hl)
                if opt.id and not opt.id.startswith("spacer:") and not opt.disabled:
                    prompt = self._prompt_for_option_id(opt.id, is_selected=True)
                    if prompt is not None:
                        options.replace_option_prompt_at_index(new_hl, prompt)

        if not search_input.has_focus:
            options.focus()

    def _active_details_options(self) -> list[Option]:
        if self._mode == "plugin_details":
            return _install_details_options(has_project=self._project_root is not None)
        if self._mode == "installed_details" and self._selected_plugin is not None:
            return _installed_details_options(
                self._selected_plugin, divider_width=self._divider_width()
            )
        if self._mode == "marketplace_details" and self._selected_marketplace is not None:
            return _marketplace_details_options(
                is_refreshing=self._is_refreshing_marketplaces
            )
        if self._mode == "confirm_remove_marketplace" and self._selected_marketplace is not None:
            return _confirm_marketplace_removal_options(self._selected_marketplace)
        return [Option("Back to plugin list", id="details-back")]

    async def _refresh_state(self) -> None:
        self._search_query = ""
        self._state = await asyncio.to_thread(
            _load_manager_state,
            self._mcp_server_info,
            loaded_plugin_ids=self._loaded_plugin_ids,
            project_root=self._project_root,
        )
        if self._selected_marketplace is not None:
            updated_mp = next(
                (m for m in self._state.marketplaces if m.name == self._selected_marketplace.name),
                None,
            )
            if updated_mp:
                self._selected_marketplace = updated_mp
        self._refresh_view()

    async def _async_refresh_all_marketplaces(self) -> None:
        try:
            from opscloud.plugins.discovery import refresh_all_marketplaces

            await asyncio.to_thread(
                refresh_all_marketplaces,
                project_root=self._project_root,
            )
            self._status = "All marketplaces refreshed."
            self._error = None
            await self._refresh_state()
        except Exception as exc:
            self._status = None
            self._error = str(exc)
            if self.is_mounted and self._close_phase == "browsing":
                self._refresh_view()
        finally:
            self._is_refreshing_marketplaces = False
            if self.is_mounted and self._close_phase == "browsing":
                self._refresh_view()

    async def _async_refresh_single_marketplace(self, marketplace_name: str) -> None:
        try:
            from opscloud.plugins.discovery import refresh_marketplace

            mp = await asyncio.to_thread(
                refresh_marketplace,
                marketplace_name,
                project_root=self._project_root,
            )
            self._status = f"Marketplace refreshed ({len(mp.plugins)} available)."
            self._error = None
            await self._refresh_state()
        except Exception as exc:
            self._status = None
            self._error = str(exc)
            if self.is_mounted and self._close_phase == "browsing":
                self._refresh_view()
        finally:
            self._is_refreshing_marketplaces = False
            if self.is_mounted and self._close_phase == "browsing":
                self._refresh_view()

    def check_action(
        self,
        action: str,
        parameters: tuple[object, ...],
    ) -> bool | None:
        """Gate priority bindings that would otherwise steal Input keystrokes or run during close phase."""
        if self._close_phase != "browsing":
            prompting = self._close_phase == "reload_prompt"
            if action == "reload_plugins":
                return prompting
            return action == "cancel" and prompting
        if action == "reload_plugins":
            return False
        if action in {"arrow_previous_tab", "arrow_next_tab"}:
            focused = self.focused
            return not (isinstance(focused, Input) and bool(focused.value))
        if action == "focus_search":
            if not self._search_available():
                return False
            try:
                return not self.query_one("#plugin-manager-search", Input).has_focus
            except NoMatches:
                return True
        return True

    def on_key(self, event: Key) -> None:
        """Focus plugin search when a letter is typed from another control."""
        if not self._search_available():
            return

        search_input = self.query_one("#plugin-manager-search", Input)
        if search_input.has_focus:
            return

        character = event.character
        if not character or not character.isalpha():
            return

        new_value = f"{search_input.value}{character}"
        search_input.value = new_value
        search_input.selection = type(search_input.selection).cursor(len(new_value))
        search_input.focus()
        event.stop()

    def action_reload_plugins(self) -> None:
        """Apply pending plugin changes from the inline reload prompt."""
        if self._close_phase == "reload_prompt":
            self.dismiss("reload")

    def _show_close_state(self, title: str, body: str, help_text: str = "") -> None:
        """Switch the modal to the reload confirmation prompt."""
        self.add_class("plugin-close-state")
        self.set_modal_title(title)
        self.set_modal_subtitle([
            ("enter", "reload"),
            ("esc", "later"),
        ])
        try:
            self.query_one("#plugin-manager-tabs").display = False
        except NoMatches:
            pass
        status = self.query_one("#plugin-manager-status", Static)
        status.update(body)
        status.display = True
        try:
            self.query_one("#plugin-manager-error").display = False
        except NoMatches:
            pass
        try:
            self.query_one("#plugin-manager-search-box").display = False
        except NoMatches:
            pass
        try:
            self.query_one("#plugin-manager-options").display = False
        except NoMatches:
            pass
        try:
            self.query_one("#plugin-marketplace-source").display = False
        except NoMatches:
            pass

    async def _close_or_prompt(self) -> None:
        """Run the close check, then dismiss or switch to the reload prompt."""
        check_reload_required = self._check_reload_required
        if check_reload_required is None and not self._mutated:
            self.dismiss(None)
            return

        reload_required = self._mutated
        if check_reload_required is not None:
            try:
                check_result = await asyncio.wait_for(check_reload_required(), 10.0)
                if check_result is not None:
                    reload_required = reload_required or check_result
            except Exception:
                logger.exception("Plugin state check failed before manager close")

        if reload_required:
            self._show_close_state(
                "Reload plugins?",
                "Reload to apply changes to plugin skills and MCP tools.",
                "Enter to reload, Esc for later",
            )
            self._close_phase = "reload_prompt"
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        if self._close_phase == "checking":
            return
        if self._close_phase == "reload_prompt":
            self.dismiss("later")
            return
        search_input = self.query_one("#plugin-manager-search", Input)
        if search_input.has_focus:
            if self._search_query:
                self._search_query = ""
                search_input.value = ""
                self._refresh_view()
                search_input.focus()
            else:
                self.query_one("#plugin-manager-options", OptionList).focus()
            return
        if self._mode == "add_marketplace":
            self._mode = "list"
            self._error = None
            self._refresh_view()
            return
        if self._details_mode_active():
            if self._mode == "confirm_remove_marketplace":
                self._mode = "marketplace_details"
                self._error = None
                self._refresh_view()
                return
            self._mode = "list"
            self._selected_plugin = None
            self._selected_marketplace = None
            self._error = None
            self._refresh_view()
            return

        if self._check_reload_required is None and not self._mutated:
            self.dismiss(None)
            return

        self._close_phase = "checking"
        self.run_worker(
            self._close_or_prompt(),
            exclusive=True,
            group="plugin-manager-close",
            exit_on_error=False,
        )

    def action_focus_search(self) -> None:
        if self._search_available():
            self.query_one("#plugin-manager-search", Input).focus()

    def action_next_tab(self) -> None:
        if self._details_mode_active():
            return
        index = self._tabs.index(self._tab)
        next_tab = self._tabs[(index + 1) % len(self._tabs)]
        self._select_tab(next_tab)

    def action_previous_tab(self) -> None:
        if self._details_mode_active():
            return
        index = self._tabs.index(self._tab)
        prev_tab = self._tabs[(index - 1) % len(self._tabs)]
        self._select_tab(prev_tab)

    def action_arrow_next_tab(self) -> None:
        self.action_next_tab()

    def action_arrow_previous_tab(self) -> None:
        self.action_previous_tab()

    def action_cursor_down(self) -> None:
        self.query_one("#plugin-manager-options", OptionList).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#plugin-manager-options", OptionList).action_cursor_up()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter the current plugin list as the search query changes."""
        if event.input.id != "plugin-manager-search":
            return
        self._search_query = event.value
        self._refresh_view()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "plugin-manager-search":
            event.stop()
            options = self.query_one("#plugin-manager-options", OptionList)
            highlighted = options.highlighted
            if highlighted is None:
                return
            option_id = options.get_option_at_index(highlighted).id
            if option_id is None or not option_id.startswith(("detail:", "installed:")):
                return
            options.focus()
            options.action_select()
            return
        if event.input.id == "plugin-marketplace-source":
            source = event.value.strip()
            if not source:
                return
            self._status = "Adding marketplace..."
            self._refresh_view()
            try:
                await asyncio.to_thread(add_marketplace_source, source)
                self._mutated = True
                self._mode = "list"
                self._tab = "discover"
                self._status = "Marketplace added."
                await self._refresh_state()
            except Exception as exc:
                self._status = None
                self._error = str(exc)
                self._refresh_view()

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        if option_id is None or option_id == "empty":
            return
        if option_id == "add-marketplace":
            self._mode = "add_marketplace"
            self._status = None
            self._error = None
            self._refresh_view()
            return
        if option_id == "refresh-all-marketplaces":
            if self._is_refreshing_marketplaces:
                return
            self._is_refreshing_marketplaces = True
            self._status = "Refreshing all marketplaces..."
            self._error = None
            self._refresh_view()
            self.run_worker(
                self._async_refresh_all_marketplaces(),
                group="plugin-refresh",
                exclusive=True,
                exit_on_error=False,
            )
            return
        if option_id.startswith("marketplace:"):
            name = option_id.removeprefix("marketplace:")
            row = next((r for r in self._state.marketplaces if r.name == name), None)
            if row:
                self._selected_marketplace = row
                self._mode = "marketplace_details"
                self._refresh_view()
            return
        if option_id.startswith("detail:"):
            plugin_id = option_id.removeprefix("detail:")
            row = next((r for r in self._state.available_plugins if r.plugin_id == plugin_id), None)
            if row:
                self._selected_plugin = row
                self._mode = "plugin_details"
                self._refresh_view()
            return
        if option_id.startswith("installed:"):
            plugin_id = option_id.removeprefix("installed:")
            row = next((r for r in self._state.installed_plugins if r.plugin_id == plugin_id), None)
            if row:
                self._selected_plugin = row
                self._mode = "installed_details"
                self._refresh_view()
            return
        if option_id in ("action:install-user", "action:install-project", "action:install-local"):
            if self._selected_plugin:
                scope_map: dict[str, InstallScope] = {
                    "action:install-user": "user",
                    "action:install-project": "project",
                    "action:install-local": "local",
                }
                scope = scope_map[option_id]
                self._status = f"Installing {self._selected_plugin.label}..."
                self._refresh_view()
                try:
                    await asyncio.to_thread(
                        install_plugin,
                        self._selected_plugin.plugin_id,
                        scope=scope,
                        project_root=self._project_root,
                    )
                    self._mutated = True
                    self._mode = "list"
                    self._tab = "installed"
                    scope_labels = {"user": "for you", "project": "for all collaborators", "local": "for you in this repo"}
                    self._status = f"Installed {self._selected_plugin.label} ({scope_labels[scope]})."
                    await self._refresh_state()
                except Exception as exc:
                    self._status = None
                    self._error = str(exc)
                    self._refresh_view()
            return
        if option_id == "action:toggle-enabled":
            if self._selected_plugin:
                new_state = not self._selected_plugin.enabled
                try:
                    await asyncio.to_thread(
                        set_installed_plugin_enabled, self._selected_plugin.plugin_id, enabled=new_state
                    )
                    self._mutated = True
                    await self._refresh_state()
                except Exception as exc:
                    self._error = str(exc)
                    self._refresh_view()
            return
        if option_id == "action:uninstall":
            if self._selected_plugin:
                plugin = self._selected_plugin
                self._status = f"Uninstalling {plugin.label}..."
                self._error = None
                self._refresh_view()
                target_root = (
                    Path(plugin.project_path)
                    if plugin.project_path
                    else self._project_root
                )
                try:
                    await asyncio.to_thread(
                        uninstall_plugin,
                        plugin.plugin_id,
                        scope=plugin.scope,
                        project_root=target_root,
                        project_path=plugin.project_path,
                    )
                    self._mutated = True
                    self._mode = "list"
                    self._selected_plugin = None
                    reload_hint = " Run /reload to unload." if plugin.enabled else ""
                    self._status = f"Uninstalled {plugin.label}.{reload_hint}"
                    self._error = None
                    await self._refresh_state()
                except Exception as exc:
                    self._status = None
                    self._error = f"Could not uninstall plugin: {exc}"
                    self._refresh_view()
            return
        if option_id == "action:refresh-marketplace":
            if self._selected_marketplace:
                if self._is_refreshing_marketplaces:
                    return
                self._is_refreshing_marketplaces = True
                self._status = f"Refreshing marketplace {self._selected_marketplace.name}..."
                self._error = None
                self._refresh_view()
                self.run_worker(
                    self._async_refresh_single_marketplace(self._selected_marketplace.name),
                    group="plugin-refresh",
                    exclusive=True,
                    exit_on_error=False,
                )
            return
        if option_id == "action:remove-marketplace":
            self._mode = "confirm_remove_marketplace"
            self._refresh_view()
            return
        if option_id == "action:confirm-remove-marketplace":
            if self._selected_marketplace:
                self._status = f"Removing marketplace {self._selected_marketplace.name}..."
                self._refresh_view()
                try:
                    await asyncio.to_thread(remove_marketplace, self._selected_marketplace.name)
                    self._mutated = True
                    self._mode = "list"
                    self._selected_marketplace = None
                    self._status = "Marketplace removed."
                    await self._refresh_state()
                except Exception as exc:
                    self._status = None
                    self._error = str(exc)
                    self._refresh_view()
            return
        if option_id == "details-back":
            self._mode = "list"
            self._selected_plugin = None
            self._selected_marketplace = None
            self._refresh_view()


__all__ = [
    "PluginClosePhase",
    "PluginManagerAction",
    "PluginManagerResult",
    "PluginManagerScreen",
    "PluginTabLabel",
    "PluginTabSelected",
    "_install_details_options",
]
