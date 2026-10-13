"""Interactive Skills Viewer modal screen for OpsCloud.

Two-pane layout: left pane lists skills with ↑/↓ navigation and search filtering,
right pane shows the selected skill's details and metadata cards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from rich.markup import escape
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from opscloud.config.settings import get_glyphs
from opscloud.ui.widgets.modal_base import (
    ModalSearchInput,
    OpsModalScreen,
    format_modal_option_row,
)
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)


# ── Skill Detail Screen (for Enter → full SKILL.md) ─────────────


class SkillDetailScreen(OpsModalScreen[None]):
    """Modal screen displaying structured skill details and SKILL.md content."""

    DIALOG_CLASSES: ClassVar[str] = "-compact"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("q", "cancel", "Close", show=False),
    ]

    DEFAULT_CSS = """
    SkillDetailScreen .skill-detail-meta {
        height: auto;
        margin-bottom: 1;
        padding-bottom: 1;
        border-bottom: solid $primary 25%;
    }

    SkillDetailScreen .skill-detail-body {
        height: 1fr;
        min-height: 5;
        scrollbar-gutter: stable;
        background: $background;
        padding: 1;
    }
    """

    def __init__(
        self,
        skill_name: str,
        content: str,
        skill_meta: dict[str, Any] | None = None,
    ) -> None:
        self.skill_name = skill_name
        self.content = content
        self.skill_meta = skill_meta or {}
        super().__init__()

    def compose_modal(self):
        source = str(self.skill_meta.get("source", "built-in")).lower()
        badge_style = {
            "plugin": "bold $secondary",
            "project": "bold $primary",
            "user": "bold $skill",
        }.get(source, "dim")
        source_label = source.upper()

        folder = ""
        path_str = str(self.skill_meta.get("path", ""))
        if path_str:
            p = Path(path_str)
            folder = str(p.parent) if p.name == "SKILL.md" else str(p)

        meta_parts: list[Any] = [
            ("Scope: ", "dim"),
            (source_label, badge_style),
        ]
        if folder:
            meta_parts.extend([
                ("  ·  Path: ", "dim"),
                (folder, "dim"),
            ])
        tools = self.skill_meta.get("tools")
        if isinstance(tools, list) and tools:
            meta_parts.extend([
                ("  ·  Tools: ", "dim"),
                (str(len(tools)), "bold $primary"),
            ])

        yield Static(Content.assemble(*meta_parts), classes="skill-detail-meta")

        with VerticalScroll(classes="skill-detail-body"):
            body_text = self.content if self.content else "No content available."
            style = "" if self.content else "dim"
            yield Static(Content.styled(body_text, style))

    def on_mount(self) -> None:
        super().on_mount()
        display_title = (
            self.skill_name.rsplit(":", 1)[-1]
            if ":" in self.skill_name
            else self.skill_name
        )
        self.set_modal_title("SKILL.md", counter=display_title)
        self.set_modal_subtitle([("esc", "close")])


# ── Skill List Item Widget (Backwards compatibility) ─────────────


class SkillItemWidget(Static):
    """A compact skill row representation retained for backwards compatibility."""

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


# ── Skills Viewer Screen (Two-Pane Modal) ─────────────────────────


class SkillsViewerScreen(OpsModalScreen[None]):
    """Two-pane skills viewer: list on left, details on right.

    Arrow keys navigate the left OptionList; the right pane updates live to show
    the selected skill's metadata. Press Enter to open full SKILL.md content.
    """

    DIALOG_CLASSES: ClassVar[str] = "-workspace"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Close", show=False, priority=True),
        Binding("slash", "focus_search", "Search", show=False),
    ]

    DEFAULT_CSS = """
    SkillsViewerScreen .skills-body {
        height: 1fr;
        width: 100%;
    }

    /* Wide layout: balanced 1fr/1fr split so list pane has generous breathing room */
    SkillsViewerScreen .skills-list-pane {
        width: 1fr;
        min-width: 36;
        height: 1fr;
    }

    SkillsViewerScreen .skills-detail-pane {
        width: 1fr;
        min-width: 36;
        height: 1fr;
        margin-left: 1;
        padding-left: 1;
        border-left: solid $primary 25%;
    }

    /* Narrow terminals: list first, Enter opens details. */
    SkillsViewerScreen.-narrow .skills-list-pane {
        width: 1fr;
        min-width: 0;
    }

    SkillsViewerScreen.-narrow .skills-detail-pane {
        display: none;
    }

    SkillsViewerScreen OptionList {
        height: 1fr;
        background: $background;
        border: none;
        padding: 0;
    }

    SkillsViewerScreen OptionList > .option-list--option-highlighted {
        background: $primary 18%;
        color: $foreground;
        text-style: none;
    }

    SkillsViewerScreen .skills-detail-content {
        height: 1fr;
        scrollbar-gutter: stable;
        background: $background;
        padding: 1;
    }

    SkillsViewerScreen .detail-card-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }

    SkillsViewerScreen .detail-label {
        color: $text-muted;
        margin-top: 1;
        text-style: bold;
    }

    SkillsViewerScreen .detail-value {
        margin-bottom: 0;
    }

    SkillsViewerScreen .skills-empty {
        color: $text-muted;
        text-style: italic;
        text-align: center;
        margin-top: 2;
    }
    """

    def __init__(self, skills: list[dict[str, Any]] | None = None) -> None:
        self._all_skills = list(skills) if skills is not None else []
        self._filtered_skills = list(self._all_skills)
        self._selected_index = 0
        self._last_row_width: int | None = None
        super().__init__()

    def compose_modal(self):
        yield ModalSearchInput(
            placeholder="Filter skills…",
            id="skills-filter",
        )
        with Horizontal(classes="skills-body"):
            with Vertical(classes="skills-list-pane"):
                yield OptionList(id="skills-options")
            with Vertical(classes="skills-detail-pane"):
                with VerticalScroll(id="skills-detail-body", classes="skills-detail-content"):
                    yield Vertical(id="skills-detail-container")

    def on_mount(self) -> None:
        super().on_mount()
        is_narrow = self.app.size.width < 90
        self.set_class(is_narrow, "-narrow")
        self._update_modal_chrome()
        self._populate_options()
        self._update_detail_panel()
        self.call_after_refresh(self._repopulate_if_width_changed)

        try:
            self.query_one("#skills-filter", ModalSearchInput).focus()
        except Exception:
            pass

    def _get_skills_option_width(self) -> int:
        """Calculate responsive content width for skills OptionList rows."""
        try:
            opt_list = self.query_one("#skills-options", OptionList)
            w = opt_list.scrollable_content_region.width
            if w > 0:
                return max(20, w - 2)
            if opt_list.region.width > 0:
                return max(20, opt_list.region.width - 3)
        except Exception:
            pass
        if self.dialog is not None and self.dialog.region.width > 0:
            if not self.has_class("-narrow"):
                # In two-pane mode, the left pane gets ~half dialog width minus padding/borders
                return max(20, (self.dialog.region.width - 8) // 2)
            return max(20, self.dialog.region.width - 6)
        return 48

    def _repopulate_if_width_changed(self) -> None:
        w = self._get_skills_option_width()
        if w != self._last_row_width:
            self._last_row_width = w
            self._populate_options()

    def on_resize(self) -> None:
        is_narrow = self.app.size.width < 90
        if is_narrow != self.has_class("-narrow"):
            self.set_class(is_narrow, "-narrow")
        self._repopulate_if_width_changed()

    def _update_modal_chrome(self) -> None:
        glyphs = get_glyphs()
        total = len(self._all_skills)
        shown = len(self._filtered_skills)
        counter = f"{shown} matches" if shown != total else f"{total} available"
        self.set_modal_title("Skills", counter=counter)
        self.set_modal_subtitle([
            (f"{glyphs.arrow_up}{glyphs.arrow_down}", "navigate"),
            (glyphs.newline, "view SKILL.md"),
            ("/", "search"),
            ("esc", "close"),
        ])

    def _populate_options(self) -> None:
        try:
            opt_list = self.query_one("#skills-options", OptionList)
        except Exception:
            return

        opt_list.clear_options()
        if not self._filtered_skills:
            return

        row_w = self._get_skills_option_width()
        self._last_row_width = row_w
        for idx, skill in enumerate(self._filtered_skills):
            name = str(skill.get("name", "unnamed"))
            desc = str(skill.get("description", ""))
            source = str(skill.get("source", "built-in")).lower()

            # Clean display name and plugin tag for namespaced skills
            if ":" in name:
                plugin_part, short_name = name.rsplit(":", 1)
                plugin_id = plugin_part.split("@", 1)[0]
            else:
                short_name = name
                plugin_id = None

            badge_style = {
                "plugin": "bold $secondary",
                "project": "bold $primary",
                "user": "bold $skill",
            }.get(source, "dim")

            if source == "plugin" and plugin_id and plugin_id != short_name:
                badge_text = plugin_id if (len(short_name) + len(plugin_id) + 5 <= row_w) else "PLUGIN"
                badges = [(badge_text, badge_style)]
            else:
                badges = [(source.upper(), badge_style)]

            tools = skill.get("tools")
            meta = f"{len(tools)} tools" if isinstance(tools, list) and tools else None

            row_content = format_modal_option_row(
                label=short_name,
                description=desc,
                badges=badges,
                meta=meta,
                width=row_w,
                is_selected=(idx == self._selected_index),
            )
            opt_list.add_option(Option(row_content, id=f"skill:{name}"))

        if 0 <= self._selected_index < len(self._filtered_skills):
            opt_list.highlighted = self._selected_index

    def on_modal_search_input_changed(self, event: ModalSearchInput.Changed) -> None:
        q = event.value.strip().lower()
        if not q:
            self._filtered_skills = list(self._all_skills)
        else:
            self._filtered_skills = [
                s
                for s in self._all_skills
                if q in str(s.get("name", "")).lower()
                or q in str(s.get("description", "")).lower()
                or q in str(s.get("path", "")).lower()
                or q in str(s.get("source", "")).lower()
            ]
        self._selected_index = 0
        self._update_modal_chrome()
        self._populate_options()
        self._update_detail_panel()

    def on_input_changed(self, event: Input.Changed) -> None:
        # Prevent double-trigger from internal input widget
        if event.input.id == "skills-filter-input" or "modal-search" in (event.input.id or ""):
            return

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_index is not None and 0 <= event.option_index < len(self._filtered_skills):
            self._selected_index = event.option_index
            self._update_detail_panel()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.action_select_skill()

    def action_focus_search(self) -> None:
        try:
            self.query_one("#skills-filter", ModalSearchInput).focus()
        except Exception:
            pass

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
                skill_meta=skill,
            )
        )

    def _update_detail_panel(self) -> None:
        try:
            container = self.query_one("#skills-detail-container", Vertical)
        except Exception:
            return

        container.remove_children()

        if not self._filtered_skills or self._selected_index >= len(self._filtered_skills):
            container.mount(
                Static("[dim]Select a skill to view details[/dim]", classes="skills-empty")
            )
            return

        skill = self._filtered_skills[self._selected_index]
        name = str(skill.get("name", "unnamed"))
        desc = str(skill.get("description", ""))
        path_str = str(skill.get("path", ""))
        source = str(skill.get("source", "built-in")).lower()

        folder = ""
        if path_str:
            p = Path(path_str)
            folder = str(p.parent) if p.name == "SKILL.md" else str(p)

        badge_style = {
            "plugin": "bold $secondary",
            "project": "bold $primary",
            "user": "bold $skill",
        }.get(source, "dim")

        if ":" in name:
            _, short_name = name.rsplit(":", 1)
            container.mount(
                Static(
                    Content.assemble(
                        (short_name, "bold $foreground"),
                        (" ", ""),
                        (f"[{source.upper()}]", badge_style),
                    ),
                    classes="detail-card-title",
                )
            )
            container.mount(Static("Identifier", classes="detail-label"))
            container.mount(Static(Content.styled(name, "dim"), classes="detail-value"))
        else:
            container.mount(
                Static(
                    Content.assemble(
                        (name, "bold $foreground"),
                        (" ", ""),
                        (f"[{source.upper()}]", badge_style),
                    ),
                    classes="detail-card-title",
                )
            )

        container.mount(Static("Scope", classes="detail-label"))
        container.mount(
            Static(Content.styled(source.upper(), badge_style), classes="detail-value")
        )

        if folder:
            container.mount(Static("Location", classes="detail-label"))
            container.mount(Static(Content.styled(folder, "dim"), classes="detail-value"))

        if desc:
            container.mount(Static("Description", classes="detail-label"))
            container.mount(Static(Content(desc), classes="detail-value"))

        tools = skill.get("tools")
        if isinstance(tools, list) and tools:
            container.mount(Static("Exposed Tools", classes="detail-label"))
            tools_str = ", ".join(str(t) for t in tools)
            container.mount(
                Static(
                    Content.assemble(
                        (f"{len(tools)} tools: ", "bold $primary"),
                        (tools_str, "dim"),
                    ),
                    classes="detail-value",
                )
            )

        tags = skill.get("tags")
        if isinstance(tags, list) and tags:
            container.mount(Static("Tags", classes="detail-label"))
            tags_pills = " ".join(f"[{t}]" for t in tags)
            container.mount(Static(Content.styled(tags_pills, "dim"), classes="detail-value"))

        if path_str and Path(path_str).exists():
            try:
                content = Path(path_str).read_text(encoding="utf-8")
                preview_lines = content.splitlines()[:16]
                preview = "\n".join(preview_lines)
                if len(content.splitlines()) > 16:
                    preview += "\n… (press Enter for full content)"
                container.mount(Static("Preview", classes="detail-label"))
                container.mount(Static(Content(preview), classes="detail-value"))
            except Exception:
                pass


__all__ = [
    "SkillDetailScreen",
    "SkillItemWidget",
    "SkillsViewerScreen",
]
