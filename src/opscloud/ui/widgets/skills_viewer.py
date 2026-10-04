"""Interactive Skills Viewer modal screen for OpsCloud.

Two-pane layout: left pane lists skills with ↑/↓ navigation,
right pane shows the selected skill's details live.
"""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from pathlib import Path
from typing import Any, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static

logger = get_logger(__name__)


# ── Skill Detail Screen (for Enter → full SKILL.md) ─────────────


class SkillDetailScreen(ModalScreen[None]):
    """Full-screen modal showing SKILL.md raw content."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close_dialog", "Close", show=False, priority=True),
        Binding("q", "close_dialog", "Close", show=False),
    ]

    CSS = """
    SkillDetailScreen {
        align: center middle;
        background: $background 70%;
    }

    SkillDetailScreen > Vertical {
        width: 96;
        max-width: 98%;
        height: 90%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    SkillDetailScreen .skill-detail-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    SkillDetailScreen .skill-detail-body {
        height: 1fr;
        min-height: 5;
        scrollbar-gutter: stable;
        background: $background;
        padding: 1;
    }

    SkillDetailScreen .skill-detail-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, skill_name: str, content: str) -> None:
        self.skill_name = skill_name
        self.content = content
        super().__init__()

    def compose(self):
        with Vertical():
            yield Static(f"SKILL.md — {self.skill_name}", classes="skill-detail-title")
            with VerticalScroll(classes="skill-detail-body"):
                yield Static(self.content if self.content else "[dim]No content available.[/dim]")
            yield Static("Esc to close", classes="skill-detail-help")

    def action_close_dialog(self) -> None:
        self.dismiss(None)


# ── Skill List Item Widget ───────────────────────────────────────


class SkillItemWidget(Static):
    """A compact, selectable skill row in the left pane."""

    def __init__(self, skill: dict[str, Any], index: int, *, classes: str = "") -> None:
        self.skill_name = str(skill.get("name", "unnamed"))
        self.source = str(skill.get("source", "unknown"))
        self.index = index
        self._selected = "skill-item-selected" in classes
        super().__init__(classes=classes)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        if selected:
            self.add_class("skill-item-selected")
        else:
            self.remove_class("skill-item-selected")
        self.refresh()

    def render(self) -> str:
        cursor = "›" if self._selected else " "
        badge = f"[{self.source.upper()}]"
        if self._selected:
            return f"[bold]{cursor} {self.skill_name}[/bold]  [dim cyan]{badge}[/dim cyan]"
        else:
            return f"{cursor} {self.skill_name}  [dim]{badge}[/dim]"


# ── Skills Viewer Screen (Two-Pane) ─────────────────────────────


class SkillsViewerScreen(ModalScreen[None]):
    """Two-pane skills viewer: list on left, details on right.

    Arrow keys navigate the left list; the right pane updates live to show
    the selected skill's metadata. Press Enter to open the full SKILL.md content.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("enter", "select_skill", "View SKILL.md", show=False, priority=True),
    ]

    CSS = """
    SkillsViewerScreen {
        align: center middle;
        background: $background 70%;
    }

    SkillsViewerScreen > Vertical {
        width: 100%;
        max-width: 98%;
        height: 90%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    /* ── Title ─────────────────────────── */
    SkillsViewerScreen .skills-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    /* ── Filter ────────────────────────── */
    SkillsViewerScreen #skills-filter {
        margin-bottom: 1;
        border: solid $primary-lighten-2;
    }

    SkillsViewerScreen #skills-filter:focus {
        border: solid $primary;
    }

    /* ── Two-pane body ─────────────────── */
    SkillsViewerScreen .skills-body {
        height: 1fr;
    }

    /* ── Left pane: skill list ─────────── */
    SkillsViewerScreen .skills-list-pane {
        width: 1fr;
        min-width: 30;
        height: 1fr;
    }

    SkillsViewerScreen .skills-list {
        height: 1fr;
        min-height: 5;
        scrollbar-gutter: stable;
        background: $background;
    }

    SkillsViewerScreen .skill-item {
        height: 1;
        width: 100%;
        padding: 0 1;
    }

    SkillsViewerScreen .skill-item:hover {
        background: $surface-lighten-1;
    }

    SkillsViewerScreen .skill-item-selected {
        background: $primary;
        color: $background;
        text-style: bold;
    }

    SkillsViewerScreen .skill-item-selected:hover {
        background: $primary-lighten-1;
    }

    /* ── Right pane: detail panel ──────── */
    SkillsViewerScreen .skills-detail-pane {
        width: 42;
        min-width: 30;
        height: 1fr;
        margin-left: 1;
        padding-left: 1;
        border-left: solid $primary-lighten-2;
    }

    SkillsViewerScreen .skills-detail-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }

    SkillsViewerScreen .skills-detail-content {
        height: 1fr;
        min-height: 1;
        scrollbar-gutter: stable;
    }

    SkillsViewerScreen .detail-label {
        color: $text-muted;
        margin-top: 1;
    }

    SkillsViewerScreen .detail-value {
        margin-bottom: 0;
    }

    /* ── Empty state ───────────────────── */
    SkillsViewerScreen .skills-empty {
        color: $text-muted;
        text-style: italic;
        text-align: center;
        margin-top: 2;
    }

    /* ── Help footer ───────────────────── */
    SkillsViewerScreen .skills-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, skills: list[dict[str, Any]]) -> None:
        self._all_skills = skills
        self._filtered_skills = list(skills)
        self._selected_index = 0
        self._item_widgets: list[SkillItemWidget] = []
        super().__init__()

    def compose(self):
        with Vertical():
            yield Static("Skills", classes="skills-title")
            yield Input(
                placeholder="Filter skills…",
                id="skills-filter",
            )
            with Horizontal(classes="skills-body"):
                # Left pane: skill list
                with Vertical(classes="skills-list-pane"):
                    with VerticalScroll(classes="skills-list"):
                        yield Vertical(id="skills-items-container")
                # Right pane: detail panel
                with Vertical(classes="skills-detail-pane"):
                    yield Static("Details", classes="skills-detail-title")
                    with VerticalScroll(classes="skills-detail-content"):
                        yield Vertical(id="skills-detail-body")
            yield Static(
                "↑/↓ navigate  │  Enter view SKILL.md  │  Esc close",
                classes="skills-help",
            )

    def on_mount(self) -> None:
        self._render_items()
        self._update_detail_panel()
        inp = self.query_one("#skills-filter", Input)
        inp.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        q = event.value.strip().lower()
        if not q:
            self._filtered_skills = list(self._all_skills)
        else:
            self._filtered_skills = [
                s for s in self._all_skills
                if q in str(s.get("name", "")).lower()
                or q in str(s.get("description", "")).lower()
                or q in str(s.get("path", "")).lower()
                or q in str(s.get("source", "")).lower()
            ]
        self._selected_index = 0
        self._render_items()
        self._update_detail_panel()

    # ── Left Pane: Skill List ────────────────────────────

    def _render_items(self) -> None:
        container = self.query_one("#skills-items-container", Vertical)
        container.remove_children()
        self._item_widgets = []

        if not self._filtered_skills:
            empty_msg = (
                "No skills found\n\n"
                "Create skills in .opscloud/skills/ or ~/.opscloud/skills/"
            )
            container.mount(Static(empty_msg, classes="skills-empty"))
            return

        for idx, skill in enumerate(self._filtered_skills):
            cls = "skill-item skill-item-selected" if idx == self._selected_index else "skill-item"
            widget = SkillItemWidget(skill, index=idx, classes=cls)
            self._item_widgets.append(widget)
            container.mount(widget)

    def _update_selection(self) -> None:
        for idx, widget in enumerate(self._item_widgets):
            widget.set_selected(idx == self._selected_index)
        if self._item_widgets and 0 <= self._selected_index < len(self._item_widgets):
            self._item_widgets[self._selected_index].scroll_visible()
        self._update_detail_panel()

    # ── Right Pane: Detail Panel ─────────────────────────

    def _update_detail_panel(self) -> None:
        """Refresh the right-pane detail view for the currently selected skill."""
        detail_body = self.query_one("#skills-detail-body", Vertical)
        detail_body.remove_children()

        if not self._filtered_skills or self._selected_index >= len(self._filtered_skills):
            detail_body.mount(
                Static("[dim]Select a skill to view details[/dim]", classes="skills-empty")
            )
            return

        skill = self._filtered_skills[self._selected_index]
        name = str(skill.get("name", "unnamed"))
        desc = str(skill.get("description", ""))
        path_str = str(skill.get("path", ""))
        source = str(skill.get("source", "unknown"))

        # Determine folder path
        folder = ""
        if path_str:
            p = Path(path_str)
            folder = str(p.parent) if p.name == "SKILL.md" else str(p)

        # Source badge
        source_label = source.upper()

        # Build detail widgets
        detail_body.mount(Static("[bold]Name[/bold]", classes="detail-label"))
        detail_body.mount(Static(name, classes="detail-value"))

        detail_body.mount(Static("[bold]Scope[/bold]", classes="detail-label"))
        detail_body.mount(Static(f"[cyan]{source_label}[/cyan]", classes="detail-value"))

        if folder:
            detail_body.mount(Static("[bold]Location[/bold]", classes="detail-label"))
            detail_body.mount(Static(f"[dim]{folder}[/dim]", classes="detail-value"))

        if desc:
            detail_body.mount(Static("[bold]Description[/bold]", classes="detail-label"))
            detail_body.mount(Static(desc, classes="detail-value"))

        # Show a snippet of SKILL.md content if available
        if path_str and Path(path_str).exists():
            try:
                content = Path(path_str).read_text(encoding="utf-8")
                # Show first ~20 lines as preview
                preview_lines = content.splitlines()[:20]
                preview = "\n".join(preview_lines)
                if len(content.splitlines()) > 20:
                    preview += "\n[dim]… (press Enter for full content)[/dim]"
                detail_body.mount(Static("[bold]Preview[/bold]", classes="detail-label"))
                detail_body.mount(Static(preview, classes="detail-value"))
            except Exception:
                pass

    # ── Key Actions ──────────────────────────────────────

    def action_move_up(self) -> None:
        if self._selected_index > 0:
            self._selected_index -= 1
            self._update_selection()

    def action_move_down(self) -> None:
        if self._selected_index < len(self._filtered_skills) - 1:
            self._selected_index += 1
            self._update_selection()

    def action_select_skill(self) -> None:
        if not self._filtered_skills or self._selected_index >= len(self._filtered_skills):
            return
        skill = self._filtered_skills[self._selected_index]
        path_str = str(skill.get("path", ""))
        content = ""
        if path_str and Path(path_str).exists():
            try:
                content = Path(path_str).read_text(encoding="utf-8")
            except Exception as e:
                content = f"Failed to read file {path_str}: {e}"
        else:
            content = "No SKILL.md file available."

        self.app.push_screen(
            SkillDetailScreen(
                skill_name=str(skill.get("name", "")),
                content=content,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = [
    "SkillDetailScreen",
    "SkillItemWidget",
    "SkillsViewerScreen",
]
