"""Interactive MCP server & tool viewer modal screen for OpsCloud.

Two-pane layout: left pane lists MCP servers and their tools with ↑/↓ navigation
and hover support, right pane shows rich live details for the selected tool or server.

Styled consistently with SkillsViewerScreen, AuthManagerScreen, and ThreadSelectorScreen.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from textual.app import ComposeResult

    from opscloud.mcp.mcp_info import MCPServerInfo, MCPToolInfo

logger = get_logger(__name__)

MCP_VIEWER_RECONNECT_REQUEST = "\x00__mcp_reconnect__"
"""Sentinel returned by dismiss to request a reconnect."""


# ── Identifier cleaning helpers ──────────────────────────


def clean_server_name(raw_name: str) -> tuple[str, str | None]:
    """Parse a potentially mangled or scoped MCP server name into (display_name, scope).

    Examples:
        'plugin__aws-compute_talkops-devops-plugins_35da5a75__aws-mcp' -> ('aws-mcp', 'aws-compute')
        'subagent__helm-operator__helm-mcp' -> ('helm-mcp', 'helm-operator')
        'github' -> ('github', None)
    """
    if not raw_name:
        return ("unnamed", None)

    # 1. Plugin scoped server: plugin__<plugin_id_sanitized>__<server_name>
    if raw_name.startswith("plugin__"):
        parts = raw_name[len("plugin__") :].split("__", 1)
        if len(parts) == 2:
            plugin_part, server_part = parts
            scope = plugin_part.split("_")[0] if "_" in plugin_part else plugin_part
            clean_server = re.sub(r"_[0-9a-f]{8,16}$", "", server_part)
            return (clean_server or server_part, scope)

    # 2. Subagent scoped server: subagent__<subagent_name>__<server_name>
    if raw_name.startswith("subagent__"):
        parts = raw_name[len("subagent__") :].split("__", 1)
        if len(parts) == 2:
            sub_part, server_part = parts
            clean_server = re.sub(r"_[0-9a-f]{8,16}$", "", server_part)
            return (clean_server or server_part, sub_part)

    # 3. Plain server name
    clean_plain = re.sub(r"_[0-9a-f]{8,16}$", "", raw_name)
    return (clean_plain or raw_name, None)


def clean_tool_name(tool_name: str, original_name: str | None = None) -> str:
    """Return a concise, human-friendly display name for an MCP tool."""
    raw = original_name if (original_name and original_name != "None") else tool_name
    if not raw:
        return "unnamed_tool"

    # If original_name is e.g. "aws___get_tasks" or "server___tool"
    if "___" in raw:
        raw = raw.split("___")[-1]
    elif "__" in raw:
        raw = raw.split("__")[-1]

    # Strip any trailing hash suffix like _9badd4f791c6
    clean = re.sub(r"_[0-9a-f]{8,16}$", "", raw)
    return clean or raw


# ── Status helpers ───────────────────────────────────────


def _status_glyph(server: MCPServerInfo) -> str:
    """Return a status glyph for a server."""
    status = server.status
    if status == "ok":
        return "●"
    if status == "unauthenticated":
        return "▲"
    if status == "error":
        return "✗"
    if status == "disabled":
        return "○"
    return "·"


def _format_prop_type(prop_type: Any) -> str:
    """Render a JSON Schema type field for display."""
    if prop_type is None:
        return "any"
    if isinstance(prop_type, list):
        parts = [str(t) for t in prop_type if t]
        return "|".join(parts) if parts else "any"
    return str(prop_type) or "any"


# ── Tool Detail Screen (Full Modal on Enter) ─────────────


class MCPToolDetailScreen(ModalScreen[None]):
    """Full-screen modal showing comprehensive MCP tool schema and documentation."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close_dialog", "Close", show=False, priority=True),
        Binding("q", "close_dialog", "Close", show=False),
    ]

    CSS = """
    MCPToolDetailScreen {
        align: center middle;
        background: $background 70%;
    }

    MCPToolDetailScreen > Vertical {
        width: 96;
        max-width: 98%;
        height: 90%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    MCPToolDetailScreen .mcp-tool-detail-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    MCPToolDetailScreen .mcp-tool-detail-body {
        height: 1fr;
        min-height: 5;
        scrollbar-gutter: stable;
        background: $background;
        padding: 1 2;
    }

    MCPToolDetailScreen .detail-label {
        color: $text-muted;
        margin-top: 1;
    }

    MCPToolDetailScreen .detail-value {
        margin-bottom: 0;
    }

    MCPToolDetailScreen .mcp-tool-detail-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, tool_info: MCPToolInfo, server_name: str = "") -> None:
        self.tool_info = tool_info
        self.server_name = server_name
        self.display_name = clean_tool_name(tool_info.name, tool_info.original_name)
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"MCP Tool: {self.display_name}", classes="mcp-tool-detail-title")
            with VerticalScroll(classes="mcp-tool-detail-body"):
                yield Vertical(id="mcp-tool-detail-content")
            yield Static("Esc or 'q' to close", classes="mcp-tool-detail-help")

    def on_mount(self) -> None:
        content = self.query_one("#mcp-tool-detail-content", Vertical)
        content.mount(Static("[bold]Tool Name[/bold]", classes="detail-label"))
        content.mount(Static(f"[bold cyan]{self.display_name}[/bold cyan]", classes="detail-value"))

        if self.server_name:
            srv_disp, srv_scope = clean_server_name(self.server_name)
            scope_txt = f" [cyan][{srv_scope}][/cyan]" if srv_scope else ""
            content.mount(Static("[bold]Server[/bold]", classes="detail-label"))
            content.mount(Static(f"{srv_disp}{scope_txt} [dim]({self.server_name})[/dim]", classes="detail-value"))

        content.mount(Static("[bold]Full Identifier[/bold]", classes="detail-label"))
        content.mount(Static(f"[dim]{self.tool_info.name}[/dim]", classes="detail-value"))

        content.mount(Static("[bold]Description[/bold]", classes="detail-label"))
        content.mount(
            Static(
                self.tool_info.description or "[dim]No description provided.[/dim]",
                classes="detail-value",
            )
        )

        schema = self.tool_info.input_schema
        content.mount(Static("[bold]Input Schema (JSON Schema)[/bold]", classes="detail-label"))
        if schema:
            schema_json = json.dumps(schema, indent=2)
            content.mount(Static(schema_json, classes="detail-value"))
        else:
            content.mount(Static("[dim]No input parameters specified.[/dim]", classes="detail-value"))

    def action_close_dialog(self) -> None:
        self.dismiss(None)


# ── Server Error Sub-modal ───────────────────────────────


class MCPServerErrorScreen(ModalScreen[None]):
    """Read-only modal for a failed MCP server's error details."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("q", "cancel", "Close", show=False),
    ]

    CSS = """
    MCPServerErrorScreen {
        align: center middle;
        background: $background 70%;
    }

    MCPServerErrorScreen > Vertical {
        width: 90;
        max-width: 90%;
        height: 70%;
        background: $surface;
        border: solid $error;
        padding: 1 2;
    }

    MCPServerErrorScreen .mcp-error-title {
        text-style: bold;
        color: $error;
        text-align: center;
        margin-bottom: 1;
    }

    MCPServerErrorScreen .mcp-error-body {
        height: 1fr;
        background: $background;
        scrollbar-gutter: stable;
        padding: 0 1;
    }

    MCPServerErrorScreen .mcp-error-text {
        color: $text;
    }

    MCPServerErrorScreen .mcp-error-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, server: MCPServerInfo) -> None:
        super().__init__()
        self._server = server
        self._error = server.error or "No error details were reported."

    def compose(self) -> ComposeResult:
        name = self._server.name
        with Vertical():
            yield Static(f"MCP Server Error: {name}", classes="mcp-error-title")
            with VerticalScroll(classes="mcp-error-body"):
                yield Static(self._error, classes="mcp-error-text")
            yield Static("Esc close", classes="mcp-error-help")

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Tool Item Widget ─────────────────────────────────────


class MCPToolItem(Static):
    """A selectable tool row in the left pane of the MCP viewer."""

    def __init__(
        self,
        tool_info: MCPToolInfo,
        index: int,
        *,
        server_name: str = "",
        server_info: MCPServerInfo | None = None,
        classes: str = "",
    ) -> None:
        self.tool_info = tool_info
        self.tool_name = tool_info.name
        self.tool_description = tool_info.description
        self.server_name = server_name
        self.server_info = server_info
        self.index = index
        self._input_schema = tool_info.input_schema
        self._expanded = False
        self._selected = "mcp-tool-selected" in classes
        self.display_name = clean_tool_name(tool_info.name, tool_info.original_name)
        super().__init__(classes=classes)

    def _desc_style(self) -> str:
        return "" if self._selected else "dim"

    def _format_collapsed(self) -> str:
        cursor = "›" if self._selected else " "
        desc = self.tool_description
        if desc and len(desc) > 30:
            desc = desc[:27] + "..."

        # Retain raw identifier in dim markup for diagnostic & test verification
        raw_dim = f" [dim]({self.tool_name})[/dim]" if self.tool_name != self.display_name else ""

        if self._selected:
            if desc:
                return f"  {cursor} [bold]{self.display_name}[/bold]  {desc}{raw_dim}"
            return f"  {cursor} [bold]{self.display_name}[/bold]{raw_dim}"
        else:
            if desc:
                return f"  {cursor} {self.display_name}  [dim]{desc}{raw_dim}[/dim]"
            return f"  {cursor} {self.display_name}{raw_dim}"

    def _format_expanded(self) -> str:
        # Kept for direct toggle_expand callers / existing test assertions
        lines = [f"  [bold]{self.display_name}[/bold] [dim]({self.tool_name})[/dim]"]
        if self.tool_description:
            style = self._desc_style()
            if style:
                lines.append(f"    [{style}]{self.tool_description}[/{style}]")
            else:
                lines.append(f"    {self.tool_description}")

        # Parameter schema
        schema = self._input_schema
        if schema and isinstance(schema, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict) and properties:
                required = set(schema.get("required") or [])
                style = self._desc_style()
                if style:
                    lines.append(f"    [{style}]Parameters:[/{style}]")
                else:
                    lines.append("    Parameters:")
                for prop_name, prop_schema in properties.items():
                    prop_type = _format_prop_type(
                        prop_schema.get("type") if isinstance(prop_schema, dict) else None
                    )
                    star = " *" if prop_name in required else ""
                    safe_name = str(prop_name).replace("\n", " ")[:80]
                    if style:
                        lines.append(f"      [{style}]{safe_name}: {prop_type}{star}[/{style}]")
                    else:
                        lines.append(f"      {safe_name}: {prop_type}{star}")
        return "\n".join(lines)

    def _rerender(self) -> None:
        if self._expanded:
            self.update(self._format_expanded())
        else:
            self.update(self._format_collapsed())

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        if selected:
            self.add_class("mcp-tool-selected")
        else:
            self.remove_class("mcp-tool-selected")
        self._rerender()

    def toggle_expand(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.styles.height = "auto" if expanded else 1
        self._rerender()

    def on_mount(self) -> None:
        self.call_after_refresh(self._rerender)

    def on_resize(self) -> None:
        if not self._expanded:
            self.update(self._format_collapsed())

    def on_enter(self, event: events.Enter) -> None:
        screen = self.screen
        if isinstance(screen, MCPViewerScreen):
            screen._move_to(self.index)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        screen = self.screen
        if isinstance(screen, MCPViewerScreen):
            screen._move_to(self.index)


# ── Server Header Widget ─────────────────────────────────


class MCPServerHeaderItem(Static):
    """A selectable server-header row in the MCP viewer."""

    def __init__(
        self,
        server: MCPServerInfo,
        visible_tool_count: int,
        index: int,
        *,
        classes: str = "",
    ) -> None:
        self._server = server
        self._visible_tool_count = visible_tool_count
        self.index = index
        self._selected = "mcp-header-selected" in classes
        self.display_name, self.scope = clean_server_name(server.name)
        content = self._render_header()
        super().__init__(content, classes=classes, markup=True)

    @property
    def server(self) -> MCPServerInfo:
        return self._server

    def _render_header(self) -> str:
        server = self._server
        glyph = _status_glyph(server)
        transport = server.transport
        status = server.status
        tool_count = self._visible_tool_count
        t_label = "tool" if tool_count == 1 else "tools"

        cursor = "›" if self._selected else " "
        scope_badge = f" [{self.scope}]" if self.scope else ""

        # Retain raw identifier in dim markup for diagnostic & test verification
        raw_dim = f" [dim]({server.name})[/dim]" if server.name != self.display_name else ""

        if self._selected:
            if status == "ok":
                return f"{cursor} {glyph} [bold]{self.display_name}[/bold]{scope_badge}  {transport} · {tool_count} {t_label}{raw_dim}"
            if status == "unauthenticated":
                return f"{cursor} {glyph} [bold]{self.display_name}[/bold]{scope_badge}  {transport} · {status} (Enter to log in){raw_dim}"
            if status == "error":
                return f"{cursor} {glyph} [bold]{self.display_name}[/bold]{scope_badge}  {transport} · {status} (Enter for error){raw_dim}"
            return f"{cursor} {glyph} [bold]{self.display_name}[/bold]{scope_badge}  {transport} · {status}{raw_dim}"
        else:
            if status == "ok":
                return f"{cursor} {glyph} [bold]{self.display_name}[/bold][cyan]{scope_badge}[/cyan]  [dim]{transport} · {tool_count} {t_label}{raw_dim}[/dim]"
            if status == "unauthenticated":
                return f"{cursor} {glyph} [bold]{self.display_name}[/bold][cyan]{scope_badge}[/cyan]  [yellow]{status}[/yellow] [dim]— Enter to log in{raw_dim}[/dim]"
            if status == "error":
                return f"{cursor} {glyph} [bold]{self.display_name}[/bold][cyan]{scope_badge}[/cyan]  [red]{status}[/red] [dim]— Enter for details{raw_dim}[/dim]"
            return f"{cursor} {glyph} [bold]{self.display_name}[/bold][cyan]{scope_badge}[/cyan]  [dim]{transport} · {status}{raw_dim}[/dim]"

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        if selected:
            self.add_class("mcp-header-selected")
        else:
            self.remove_class("mcp-header-selected")
        self.update(self._render_header())

    def on_enter(self, event: events.Enter) -> None:
        screen = self.screen
        if isinstance(screen, MCPViewerScreen):
            screen._move_to(self.index)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        screen = self.screen
        if not isinstance(screen, MCPViewerScreen):
            return
        if self._selected and self._server.status == "unauthenticated":
            screen.dismiss(self._server.name)
            return
        if self._selected and self._server.status == "error":
            screen.show_server_error(self._server)
            return
        screen._move_to(self.index)


# ── Filter helper ────────────────────────────────────────


def _visible_tools_for(
    server: MCPServerInfo, tokens: list[str]
) -> tuple[MCPToolInfo, ...] | None:
    """Return tools matching the filter, or None if nothing matches."""
    tools = server.tools
    if not tokens:
        return tools or ()

    srv_clean, srv_scope = clean_server_name(server.name)
    srv_search = f"{server.name} {srv_clean} {srv_scope or ''}".lower()
    if all(tok in srv_search for tok in tokens):
        return tools or None

    matching: list[MCPToolInfo] = []
    for t in tools:
        t_clean = clean_tool_name(t.name, t.original_name)
        search_blob = f"{t.name} {t_clean} {t.original_name or ''} {t.description}".lower()
        if all(tok in search_blob for tok in tokens):
            matching.append(t)

    return tuple(matching) or None


# ── Main MCP Viewer Screen ───────────────────────────────


class MCPViewerScreen(ModalScreen[str | None]):
    """Two-pane modal viewer for active MCP servers and their tools.

    Left pane lists MCP servers and their tools with keyboard and mouse hover support.
    Right pane displays real-time metadata, parameter schemas, and error diagnostics.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("tab", "jump_down", "Next server", show=False, priority=True),
        Binding("shift+tab", "jump_up", "Prev server", show=False, priority=True),
        Binding("enter", "select_item", "Details/Login", show=False, priority=True),
        Binding("ctrl+e", "toggle_all", "Toggle all", show=False, priority=True),
        Binding("ctrl+r", "reconnect", "Reconnect", show=False, priority=True),
        Binding("f2", "toggle_disable", "Disable/Enable", show=False, priority=True),
        Binding("pageup", "page_up", "Page up", show=False, priority=True),
        Binding("pagedown", "page_down", "Page down", show=False, priority=True),
        Binding("escape", "cancel", "Close", show=False, priority=True),
    ]

    CSS = """
    MCPViewerScreen {
        align: center middle;
        background: $background 70%;
    }

    MCPViewerScreen > Vertical {
        width: 100%;
        max-width: 98%;
        height: 90%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    /* ── Title ─────────────────────────── */
    MCPViewerScreen .mcp-viewer-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    /* ── Filter ────────────────────────── */
    MCPViewerScreen #mcp-filter {
        margin-bottom: 1;
        border: solid $primary-lighten-2;
    }

    MCPViewerScreen #mcp-filter:focus {
        border: solid $primary;
    }

    /* ── Two-pane body ─────────────────── */
    MCPViewerScreen .mcp-body {
        height: 1fr;
    }

    /* ── Left pane: server & tool list ─── */
    MCPViewerScreen .mcp-list-pane {
        width: 1fr;
        min-width: 35;
        height: 1fr;
    }

    MCPViewerScreen .mcp-list {
        height: 1fr;
        min-height: 5;
        scrollbar-gutter: stable;
        background: $background;
    }

    /* ── Server headers ────────────────── */
    MCPViewerScreen .mcp-server-header {
        height: 1;
        width: 100%;
        padding: 0 1;
        color: $primary;
        text-style: bold;
        margin-top: 1;
    }

    MCPViewerScreen .mcp-server-header:hover {
        background: $surface-lighten-1;
    }

    MCPViewerScreen .mcp-list > .mcp-server-header:first-child {
        margin-top: 0;
    }

    MCPViewerScreen .mcp-header-selected {
        background: $primary;
        color: $background;
        text-style: bold;
    }

    MCPViewerScreen .mcp-header-selected:hover {
        background: $primary-lighten-1;
    }

    /* ── Tool items ────────────────────── */
    MCPViewerScreen .mcp-tool-item {
        height: 1;
        width: 100%;
        padding: 0 1 0 2;
    }

    MCPViewerScreen .mcp-tool-item:hover {
        background: $surface-lighten-1;
    }

    MCPViewerScreen .mcp-tool-selected {
        background: $primary;
        color: $background;
        text-style: bold;
    }

    MCPViewerScreen .mcp-tool-selected:hover {
        background: $primary-lighten-1;
    }

    /* ── Right pane: detail panel ──────── */
    MCPViewerScreen .mcp-detail-pane {
        width: 46;
        min-width: 32;
        height: 1fr;
        margin-left: 1;
        padding-left: 1;
        border-left: solid $primary-lighten-2;
    }

    MCPViewerScreen .mcp-detail-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }

    MCPViewerScreen .mcp-detail-content {
        height: 1fr;
        min-height: 1;
        scrollbar-gutter: stable;
    }

    MCPViewerScreen .detail-label {
        color: $text-muted;
        margin-top: 1;
    }

    MCPViewerScreen .detail-value {
        margin-bottom: 0;
    }

    /* ── Empty state ───────────────────── */
    MCPViewerScreen .mcp-empty {
        color: $text-muted;
        text-style: italic;
        text-align: center;
        margin-top: 2;
    }

    /* ── Help footer ───────────────────── */
    MCPViewerScreen .mcp-viewer-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(
        self,
        server_info: list[MCPServerInfo],
        *,
        connecting: bool = False,
        pending_reconnect: bool = False,
        on_toggle_disable: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        self._server_info = server_info
        self._connecting = connecting
        self._pending_reconnect = pending_reconnect
        self._on_toggle_disable = on_toggle_disable
        self._row_widgets: list[MCPToolItem | MCPServerHeaderItem] = []
        self._selected_index = 0
        self._query: str = ""

    @property
    def _tool_widgets(self) -> list[MCPToolItem]:
        return [w for w in self._row_widgets if isinstance(w, MCPToolItem)]

    # ── Compose / Mount ──────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Vertical()

    def on_mount(self) -> None:
        self._mount_body(self.query_one(Vertical))

    def _mount_body(self, container: Vertical) -> None:
        total_servers = len(self._server_info)
        total_tools = sum(s.tool_count for s in self._server_info)

        if total_servers:
            s_label = "server" if total_servers == 1 else "servers"
            t_label = "tool" if total_tools == 1 else "tools"
            title = f"MCP Servers ({total_servers} {s_label}, {total_tools} {t_label})"
        else:
            title = "MCP Servers"

        container.mount(Static(title, classes="mcp-viewer-title"))

        if self._server_info:
            container.mount(
                Input(
                    id="mcp-filter",
                    placeholder="Filter tools...",
                    value=self._query,
                )
            )

        # Two-pane body matching Skills UI
        body = Horizontal(classes="mcp-body")
        container.mount(body)

        # Left pane: MCP servers & tools list
        list_pane = Vertical(classes="mcp-list-pane")
        body.mount(list_pane)
        scroll = VerticalScroll(classes="mcp-list")
        list_pane.mount(scroll)

        # Right pane: Detail panel
        detail_pane = Vertical(classes="mcp-detail-pane")
        body.mount(detail_pane)
        detail_pane.mount(Static("Details", classes="mcp-detail-title"))
        detail_scroll = VerticalScroll(classes="mcp-detail-content")
        detail_pane.mount(detail_scroll)
        detail_scroll.mount(Vertical(id="mcp-detail-body"))

        # Populate scroll items
        self._populate_scroll(scroll, self._query)

        # Help footer
        container.mount(
            Static(self._build_help_text(), classes="mcp-viewer-help")
        )

        # Update initial detail panel state
        self._update_detail_panel()

        # Focus filter input if available
        def _focus() -> None:
            if self._server_info:
                try:
                    self.query_one("#mcp-filter", Input).focus()
                except Exception:
                    pass

        self.call_after_refresh(_focus)

    def _build_help_text(self) -> str:
        parts = [
            "↑/↓ navigate",
            "Enter details/schema",
            "F2 disable/enable",
        ]
        if self._pending_reconnect:
            parts.append("Ctrl+R reconnect")
        parts.extend(["type to filter", "Esc close"])
        return "  │  ".join(parts)

    # ── Populate scroll ──────────────────────────────────

    def _populate_scroll(self, scroll: VerticalScroll, query: str) -> None:
        if not self._server_info:
            placeholder = (
                "Loading MCP tools..."
                if self._connecting
                else "No MCP servers configured.\nUse `--mcp-config` to load servers."
            )
            scroll.mount(Static(placeholder, classes="mcp-empty"))
            return

        tokens = [tok for tok in query.lower().split() if tok]
        flat_index = 0

        # Sort: attention-needed servers first
        sorted_servers = sorted(
            self._server_info,
            key=lambda s: 0 if s.status in ("unauthenticated", "error") else 1,
        )

        for server in sorted_servers:
            visible_tools = _visible_tools_for(server, tokens)
            if visible_tools is None:
                continue

            header_classes = "mcp-server-header"
            if flat_index == 0:
                header_classes += " mcp-header-selected"

            header = MCPServerHeaderItem(
                server=server,
                visible_tool_count=len(visible_tools),
                index=flat_index,
                classes=header_classes,
            )
            self._row_widgets.append(header)
            scroll.mount(header)
            flat_index += 1

            for tool_info in visible_tools:
                widget = MCPToolItem(
                    tool_info=tool_info,
                    index=flat_index,
                    server_name=server.name,
                    server_info=server,
                    classes="mcp-tool-item",
                )
                self._row_widgets.append(widget)
                scroll.mount(widget)
                flat_index += 1

        if not self._row_widgets:
            msg = "No matching tools." if tokens else "No tools available."
            scroll.mount(Static(msg, classes="mcp-empty"))

    # ── Detail Panel Live Update ─────────────────────────

    def _update_detail_panel(self) -> None:
        """Refresh the right-pane detail view for the currently selected item."""
        try:
            detail_body = self.query_one("#mcp-detail-body", Vertical)
        except Exception:
            return

        detail_body.remove_children()

        if not self._row_widgets or not (0 <= self._selected_index < len(self._row_widgets)):
            detail_body.mount(
                Static("[dim]Select a tool or server to view details[/dim]", classes="mcp-empty")
            )
            return

        item = self._row_widgets[self._selected_index]
        if isinstance(item, MCPToolItem):
            self._render_tool_details(detail_body, item)
        elif isinstance(item, MCPServerHeaderItem):
            self._render_server_details(detail_body, item)

    def _render_tool_details(self, detail_body: Vertical, item: MCPToolItem) -> None:
        """Mount formatted metadata and parameter schemas for a tool."""
        detail_body.mount(Static("[bold]Tool[/bold]", classes="detail-label"))
        detail_body.mount(Static(f"[bold cyan]{item.display_name}[/bold cyan]", classes="detail-value"))

        detail_body.mount(Static("[bold]MCP Server[/bold]", classes="detail-label"))
        if item.server_info:
            srv_disp, srv_scope = clean_server_name(item.server_info.name)
            scope_badge = f" [cyan][{srv_scope}][/cyan]" if srv_scope else ""
            status_glyph = _status_glyph(item.server_info)
            detail_body.mount(
                Static(
                    f"{srv_disp}{scope_badge} [dim]({status_glyph} {item.server_info.transport})[/dim]",
                    classes="detail-value",
                )
            )
        else:
            detail_body.mount(Static(item.server_name, classes="detail-value"))

        detail_body.mount(Static("[bold]Description[/bold]", classes="detail-label"))
        desc = item.tool_description or "[dim]No description provided.[/dim]"
        detail_body.mount(Static(desc, classes="detail-value"))

        schema = item._input_schema
        detail_body.mount(Static("[bold]Parameters[/bold]", classes="detail-label"))
        if schema and isinstance(schema, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict) and properties:
                required = set(schema.get("required") or [])
                count = len(properties)
                detail_body.mount(
                    Static(f"[dim]{count} parameter{'s' if count != 1 else ''}[/dim]", classes="detail-value")
                )
                for prop_name, prop_schema in properties.items():
                    is_req = prop_name in required
                    req_badge = "[bold red]* required[/bold red]" if is_req else "[dim]optional[/dim]"
                    p_type = _format_prop_type(
                        prop_schema.get("type") if isinstance(prop_schema, dict) else None
                    )
                    detail_body.mount(
                        Static(f"• [bold cyan]{prop_name}[/bold cyan] ({p_type})  {req_badge}", classes="detail-value")
                    )
                    if isinstance(prop_schema, dict):
                        p_desc = prop_schema.get("description", "").strip()
                        if p_desc:
                            detail_body.mount(Static(f"    [dim]{p_desc}[/dim]", classes="detail-value"))
                        enum_vals = prop_schema.get("enum")
                        if enum_vals:
                            enum_str = ", ".join(repr(v) for v in enum_vals[:6])
                            if len(enum_vals) > 6:
                                enum_str += ", …"
                            detail_body.mount(Static(f"    [dim]Options: [{enum_str}][/dim]", classes="detail-value"))
                        default_val = prop_schema.get("default")
                        if default_val is not None:
                            detail_body.mount(Static(f"    [dim]Default: {default_val}[/dim]", classes="detail-value"))
            else:
                detail_body.mount(Static("[dim]No parameters required[/dim]", classes="detail-value"))
        else:
            detail_body.mount(Static("[dim]No parameters required[/dim]", classes="detail-value"))

        detail_body.mount(Static("[bold]Identifier[/bold]", classes="detail-label"))
        detail_body.mount(Static(f"[dim]{item.tool_name}[/dim]", classes="detail-value"))

        detail_body.mount(
            Static("\n[dim]Press [bold]Enter[/bold] to inspect full JSON schema[/dim]", classes="detail-label")
        )

    def _render_server_details(self, detail_body: Vertical, item: MCPServerHeaderItem) -> None:
        """Mount formatted configuration, status, and tools list for a server."""
        server = item.server
        srv_display, srv_scope = clean_server_name(server.name)

        detail_body.mount(Static("[bold]Server[/bold]", classes="detail-label"))
        scope_badge = f" [cyan][{srv_scope}][/cyan]" if srv_scope else ""
        detail_body.mount(Static(f"[bold cyan]{srv_display}[/bold cyan]{scope_badge}", classes="detail-value"))

        detail_body.mount(Static("[bold]Status[/bold]", classes="detail-label"))
        glyph = _status_glyph(server)
        if server.status == "ok":
            status_text = f"[green]{glyph} Connected / Active[/green]"
        elif server.status == "unauthenticated":
            status_text = f"[yellow]{glyph} Unauthenticated[/yellow] [dim](Press Enter to log in)[/dim]"
        elif server.status == "error":
            status_text = f"[red]{glyph} Error[/red] [dim](Press Enter for details)[/dim]"
        elif server.status == "disabled":
            status_text = f"[dim]{glyph} Disabled (Press F2 to enable)[/dim]"
        else:
            status_text = f"{glyph} {server.status}"
        detail_body.mount(Static(status_text, classes="detail-value"))

        detail_body.mount(Static("[bold]Transport[/bold]", classes="detail-label"))
        detail_body.mount(Static(f"[cyan]{server.transport}[/cyan]", classes="detail-value"))

        if server.command:
            cmd_str = server.command
            if server.args:
                cmd_str += " " + " ".join(server.args)
            detail_body.mount(Static("[bold]Command[/bold]", classes="detail-label"))
            detail_body.mount(Static(f"[dim]{cmd_str}[/dim]", classes="detail-value"))

        if server.url:
            detail_body.mount(Static("[bold]URL[/bold]", classes="detail-label"))
            detail_body.mount(Static(f"[cyan]{server.url}[/cyan]", classes="detail-value"))

        detail_body.mount(Static("[bold]Tools[/bold]", classes="detail-label"))
        count = len(server.tools)
        detail_body.mount(Static(f"{count} available", classes="detail-value"))
        for t in server.tools[:8]:
            t_clean = clean_tool_name(t.name, t.original_name)
            detail_body.mount(Static(f"• [dim]{t_clean}[/dim]", classes="detail-value"))
        if count > 8:
            detail_body.mount(Static(f"[dim]… and {count - 8} more[/dim]", classes="detail-value"))

        if server.error:
            detail_body.mount(Static("[bold red]Error Diagnostics[/bold red]", classes="detail-label"))
            detail_body.mount(Static(f"[red]{server.error}[/red]", classes="detail-value"))

        detail_body.mount(Static("[bold]Config Identifier[/bold]", classes="detail-label"))
        detail_body.mount(Static(f"[dim]{server.name}[/dim]", classes="detail-value"))

    # ── Filter ───────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "mcp-filter":
            return
        self._query = event.value
        scroll = self.query_one(".mcp-list", VerticalScroll)
        scroll.remove_children()
        self._row_widgets = []
        self._selected_index = 0
        self._populate_scroll(scroll, self._query)
        self._selected_index = min(
            self._selected_index, max(0, len(self._row_widgets) - 1)
        )
        self._update_detail_panel()

    # ── Navigation ───────────────────────────────────────

    def _move_to(self, index: int) -> None:
        count = len(self._row_widgets)
        if not count or not (0 <= index < count):
            return
        old = self._selected_index
        if not (0 <= old < count):
            old = 0
        self._selected_index = index
        if old != index:
            self._row_widgets[old].set_selected(False)
            self._row_widgets[index].set_selected(True)
        self._update_detail_panel()

    def _move_selection(self, delta: int) -> None:
        if not self._row_widgets:
            return
        target = self._selected_index + delta
        if 0 <= target < len(self._row_widgets):
            self._move_to(target)

    def _next_server_header(self, start: int, step: int) -> int | None:
        index = start + step
        while 0 <= index < len(self._row_widgets):
            if isinstance(self._row_widgets[index], MCPServerHeaderItem):
                return index
            index += step
        return None

    def _reveal_selection(self, widget: MCPToolItem | MCPServerHeaderItem, *, direction: int) -> None:
        widget.scroll_visible()

    def action_move_up(self) -> None:
        if not self._row_widgets:
            return
        old = self._selected_index
        if old == 0:
            self._move_to(len(self._row_widgets) - 1)
        else:
            self._move_selection(-1)
        if self._selected_index != old:
            self._reveal_selection(self._row_widgets[self._selected_index], direction=-1)

    def action_move_down(self) -> None:
        if not self._row_widgets:
            return
        old = self._selected_index
        if old == len(self._row_widgets) - 1:
            self._move_to(0)
        else:
            self._move_selection(1)
        if self._selected_index != old:
            self._reveal_selection(self._row_widgets[self._selected_index], direction=1)

    def action_jump_up(self) -> None:
        target = self._next_server_header(self._selected_index, -1)
        if target is None:
            target = self._next_server_header(len(self._row_widgets), -1)
        if target is None or target == self._selected_index:
            return
        self._move_to(target)
        self._reveal_selection(self._row_widgets[target], direction=-1)

    def action_jump_down(self) -> None:
        target = self._next_server_header(self._selected_index, +1)
        if target is None:
            target = self._next_server_header(-1, +1)
        if target is None or target == self._selected_index:
            return
        self._move_to(target)
        self._reveal_selection(self._row_widgets[target], direction=1)

    def action_page_up(self) -> None:
        if not self._row_widgets:
            return
        scroll = self.query_one(".mcp-list", VerticalScroll)
        scroll.scroll_page_up()

    def action_page_down(self) -> None:
        if not self._row_widgets:
            return
        scroll = self.query_one(".mcp-list", VerticalScroll)
        scroll.scroll_page_down()

    # ── Tool / Server Selection & Expand ─────────────────

    def action_select_item(self) -> None:
        """Handle Enter key on the currently selected item."""
        if not self._row_widgets:
            return
        row = self._row_widgets[self._selected_index]
        if isinstance(row, MCPToolItem):
            self.app.push_screen(
                MCPToolDetailScreen(
                    tool_info=row.tool_info,
                    server_name=row.server_name,
                )
            )
            return

        server = row.server
        if server.status == "unauthenticated":
            self.dismiss(server.name)
            return
        if server.status == "error":
            self.show_server_error(server)
            return

    def action_toggle_expand(self) -> None:
        """Alias for action_select_item for backward compatibility."""
        self.action_select_item()

    def action_toggle_all(self) -> None:
        tools = self._tool_widgets
        if not tools:
            return
        any_collapsed = any(not w._expanded for w in tools)
        for widget in tools:
            widget.set_expanded(any_collapsed)

    def show_server_error(self, server: MCPServerInfo) -> None:
        self.app.push_screen(MCPServerErrorScreen(server))

    # ── Reconnect / Disable ──────────────────────────────

    def action_reconnect(self) -> None:
        if not self._pending_reconnect:
            return
        self.dismiss(MCP_VIEWER_RECONNECT_REQUEST)

    def action_toggle_disable(self) -> None:
        if not self._row_widgets:
            return
        row = self._row_widgets[self._selected_index]
        server_name: str | None = None
        if isinstance(row, MCPServerHeaderItem):
            server_name = row.server.name
        elif isinstance(row, MCPToolItem) and row.server_name:
            server_name = row.server_name

        if not server_name or self._on_toggle_disable is None:
            return
        self.app.call_later(self._on_toggle_disable, server_name)

    def action_cancel(self) -> None:
        self.dismiss(None)

    # ── Refresh API ──────────────────────────────────────

    async def refresh_server_info(
        self,
        server_info: list[MCPServerInfo],
        *,
        pending_reconnect: bool | None = None,
        select_server: str | None = None,
    ) -> None:
        """Replace the displayed server list."""
        self._server_info = server_info
        self._connecting = False
        self._query = ""
        if pending_reconnect is not None:
            self._pending_reconnect = pending_reconnect
        body = self.query_one(Vertical)
        await body.remove_children()
        self._row_widgets = []
        self._selected_index = 0
        self._mount_body(body)
        if select_server is not None:
            for idx, widget in enumerate(self._row_widgets):
                if (
                    isinstance(widget, MCPServerHeaderItem)
                    and widget.server.name == select_server
                ):
                    self._move_to(idx)
                    break


__all__ = [
    "MCPServerErrorScreen",
    "MCPServerHeaderItem",
    "MCPToolDetailScreen",
    "MCPToolItem",
    "MCPViewerScreen",
    "MCP_VIEWER_RECONNECT_REQUEST",
    "clean_server_name",
    "clean_tool_name",
]
