"""Interactive MCP server & tool viewer modal screen.

Displays servers grouped by name with status indicators, transport type,
and tool counts. Expandable tool items show descriptions and parameter
schemas.  Filter input, keyboard navigation, and server error detail.

Styled consistently with AuthManagerScreen, ConfigManagerScreen, and
ThreadSelectorScreen.

Now works with :class:`MCPServerInfo` dataclasses preloaded at startup.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from typing import TYPE_CHECKING, Any, ClassVar

from textual import events
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from textual.app import ComposeResult

    from opscloud.mcp.mcp_info import MCPServerInfo, MCPToolInfo

logger = get_logger(__name__)

MCP_VIEWER_RECONNECT_REQUEST = "\x00__mcp_reconnect__"
"""Sentinel returned by dismiss to request a reconnect."""

# ── Status helpers ───────────────────────────────────────


def _status_glyph(server: MCPServerInfo) -> str:
    """Return a status emoji for a server."""
    status = server.status
    if status == "ok":
        return "✅"
    if status == "unauthenticated":
        return "🟡"
    if status == "error":
        return "🔴"
    if status == "disabled":
        return "⏸"
    return "⚪"


def _format_prop_type(prop_type: Any) -> str:
    """Render a JSON Schema type field for display."""
    if prop_type is None:
        return "any"
    if isinstance(prop_type, list):
        parts = [str(t) for t in prop_type if t]
        return "|".join(parts) if parts else "any"
    return str(prop_type) or "any"


# ── Tool Item Widget ─────────────────────────────────────


class MCPToolItem(Static):
    """A selectable, expandable tool row in the MCP viewer."""

    def __init__(
        self,
        tool_info: MCPToolInfo,
        index: int,
        *,
        classes: str = "",
    ) -> None:
        self.tool_name = tool_info.name
        self.tool_description = tool_info.description
        self.index = index
        self._input_schema = tool_info.input_schema
        self._expanded = False
        self._selected = "mcp-tool-selected" in classes
        super().__init__(classes=classes)

    def _desc_style(self) -> str:
        return "" if self._selected else "dim"

    def _format_collapsed(self) -> str:
        desc = self.tool_description
        avail = (self.size.width - len(self.tool_name) - 4) if self.size.width else 60
        if avail > 0 and len(desc) > avail:
            desc = desc[: max(0, avail - 5)] + " (...)"
        style = self._desc_style()
        if desc:
            if style:
                return f"  {self.tool_name} [{style}]{desc}[/{style}]"
            return f"  {self.tool_name} {desc}"
        return f"  {self.tool_name}"

    def _format_expanded(self) -> str:
        lines = [f"  [bold]{self.tool_name}[/bold]"]
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

    def on_click(self, event: events.Click) -> None:
        event.stop()
        screen = self.screen
        if isinstance(screen, MCPViewerScreen):
            screen._move_to(self.index)
            self.toggle_expand()


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
        content = self._render_header()
        super().__init__(content, classes=classes, markup=True)

    @property
    def server(self) -> MCPServerInfo:
        return self._server

    def _render_header(self) -> str:
        server = self._server
        glyph = _status_glyph(server)
        name = server.name
        transport = server.transport
        status = server.status
        tool_count = self._visible_tool_count
        t_label = "tool" if tool_count == 1 else "tools"
        dim = "" if self._selected else "dim"

        if status == "ok":
            summary = f" {transport} · {tool_count} {t_label}"
            if dim:
                return f"{glyph} [bold]{name}[/bold] [{dim}]{summary}[/{dim}]"
            return f"{glyph} [bold]{name}[/bold] {summary}"

        if status == "unauthenticated":
            hint = " — Enter to log in"
            if dim:
                return f"{glyph} [bold]{name}[/bold] [{dim}]{transport}[/{dim}] [yellow]{status}[/yellow] [{dim}]{hint}[/{dim}]"
            return f"{glyph} [bold]{name}[/bold] {transport} [yellow]{status}[/yellow] {hint}"

        if status == "error":
            hint = " — Enter for details"
            if dim:
                return f"{glyph} [bold]{name}[/bold] [{dim}]{transport}[/{dim}] [red]{status}[/red] [{dim}]{hint}[/{dim}]"
            return f"{glyph} [bold]{name}[/bold] {transport} [red]{status}[/red] {hint}"

        if status == "disabled":
            error_text = server.error or ""
            suffix = f" — {error_text[:80]}" if error_text else ""
            if dim:
                return f"{glyph} [bold]{name}[/bold] [{dim}]{transport} · {status}{suffix}[/{dim}]"
            return f"{glyph} [bold]{name}[/bold] {transport} · {status}{suffix}"

        # Default: disconnected / unknown
        if dim:
            return f"{glyph} [bold]{name}[/bold] [{dim}]{transport} · {status}[/{dim}]"
        return f"{glyph} [bold]{name}[/bold] {transport} · {status}"

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        if selected:
            self.add_class("mcp-header-selected")
        else:
            self.remove_class("mcp-header-selected")
        self.update(self._render_header())

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


# ── Server Error Sub-modal ───────────────────────────────


class MCPServerErrorScreen(ModalScreen[None]):
    """Read-only modal for a failed MCP server's error details."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
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


# ── Main MCP Viewer Screen ───────────────────────────────


def _visible_tools_for(
    server: MCPServerInfo, tokens: list[str]
) -> tuple[MCPToolInfo, ...] | None:
    """Return tools matching the filter, or None if nothing matches."""
    tools = server.tools
    if not tokens:
        return tools or ()

    name = server.name.lower()
    if all(tok in name for tok in tokens):
        return tools or None

    matching = tuple(
        t for t in tools
        if all(tok in t.name.lower() for tok in tokens)
    )
    return matching or None


class MCPViewerScreen(ModalScreen[str | None]):
    """Modal viewer for active MCP servers and their tools.

    Works with :class:`MCPServerInfo` dataclasses preloaded at startup.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("tab", "jump_down", "Next server", show=False, priority=True),
        Binding("shift+tab", "jump_up", "Prev server", show=False, priority=True),
        Binding("enter", "toggle_expand", "Expand", show=False, priority=True),
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
        width: 90;
        max-width: 95%;
        height: 85%;
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
        border: solid $panel;
    }

    MCPViewerScreen #mcp-filter:focus {
        border: solid $primary;
    }

    /* ── Scroll area ───────────────────── */
    MCPViewerScreen .mcp-list {
        height: 1fr;
        min-height: 5;
        scrollbar-gutter: stable;
        background: $background;
    }

    /* ── Server headers ────────────────── */
    MCPViewerScreen .mcp-server-header {
        color: $primary;
        margin-top: 1;
    }

    MCPViewerScreen .mcp-server-header:hover {
        background: $surface-lighten-1;
    }

    MCPViewerScreen .mcp-list > .mcp-server-header:first-child {
        margin-top: 0;
    }

    MCPViewerScreen .mcp-header-selected {
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }

    MCPViewerScreen .mcp-header-selected:hover {
        background: $primary-darken-1;
        color: $text;
    }

    /* ── Tool items ────────────────────── */
    MCPViewerScreen .mcp-tool-item {
        height: 1;
        padding: 0 1;
    }

    MCPViewerScreen .mcp-tool-item:hover {
        background: $surface-lighten-1;
    }

    MCPViewerScreen .mcp-tool-selected {
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }

    MCPViewerScreen .mcp-tool-selected:hover {
        background: $primary-darken-1;
        color: $text;
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

            scroll = VerticalScroll(classes="mcp-list")
            container.mount(scroll)
            self._populate_scroll(scroll, self._query)

            container.mount(
                Static(self._build_help_text(), classes="mcp-viewer-help")
            )

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
            "Enter expand/login/details",
            "F2 disable/enable",
            "Ctrl+E expand all",
        ]
        if self._pending_reconnect:
            parts.append("Ctrl+R reconnect")
        parts.extend(["type to filter", "Esc close"])
        return " · ".join(parts)

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
                    classes="mcp-tool-item",
                )
                self._row_widgets.append(widget)
                scroll.mount(widget)
                flat_index += 1

        if not self._row_widgets:
            msg = "No matching tools." if tokens else "No tools available."
            scroll.mount(Static(msg, classes="mcp-empty"))

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

    # ── Tool expand ──────────────────────────────────────

    def action_toggle_expand(self) -> None:
        if not self._row_widgets:
            return
        row = self._row_widgets[self._selected_index]
        if isinstance(row, MCPToolItem):
            row.toggle_expand()
            self.call_after_refresh(row.scroll_visible)
            return
        # Server header: login or show error
        server = row.server
        if server.status == "unauthenticated":
            self.dismiss(server.name)
            return
        if server.status == "error":
            self.show_server_error(server)
            return

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
        if isinstance(row, MCPToolItem):
            return
        if self._on_toggle_disable is None:
            return
        self.app.call_later(self._on_toggle_disable, row.server.name)

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
    "MCPToolItem",
    "MCPViewerScreen",
    "MCP_VIEWER_RECONNECT_REQUEST",
]
