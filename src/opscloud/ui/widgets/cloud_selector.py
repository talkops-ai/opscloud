"""Interactive cloud profile selector screen for `/cloud` command."""

from __future__ import annotations

from opscloud.utils.logger import get_logger
from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Click
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from opscloud.config.cloud_profiles import (
    AWSProfileInfo,
    get_active_aws_profile,
    list_aws_profiles,
)
from opscloud.config.toml_config import (
    load_aws_profile,
    load_recent_aws_profiles,
    save_aws_profile,
)

logger = get_logger(__name__)

_RECENT_SECTION_LABEL = "Recent & Active"


class CloudProfileOption(Static):
    """A single clickable cloud profile row in the selector."""

    def __init__(
        self,
        label: str,
        profile_info: AWSProfileInfo,
        index: int,
        *,
        classes: str = "",
    ) -> None:
        super().__init__(label, classes=classes, markup=True)
        self.profile_info = profile_info
        self.profile_name = profile_info.name
        self.index = index

    class Clicked(Message):
        """Posted when a cloud profile row is clicked."""

        def __init__(self, profile_name: str, index: int) -> None:
            super().__init__()
            self.profile_name = profile_name
            self.index = index

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(self.Clicked(self.profile_name, self.index))


class CloudProfileSelectorScreen(ModalScreen[tuple[str, str | None] | None]):
    """Full-screen modal for interactive cloud profile selection."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("tab", "tab_complete", "Tab complete", show=False, priority=True),
        Binding("enter", "select", "Select", show=False, priority=True),
        Binding("ctrl+s", "set_default", "Set default", show=False, priority=True),
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    CSS = """
    CloudProfileSelectorScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    CloudProfileSelectorScreen > Vertical {
        width: 82;
        max-width: 95%;
        height: auto;
        max-height: 90vh;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }

    CloudProfileSelectorScreen .cloud-selector-title {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    CloudProfileSelectorScreen .cloud-selector-info {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }

    CloudProfileSelectorScreen #cloud-filter {
        margin-bottom: 1;
        border: solid $panel;
    }

    CloudProfileSelectorScreen #cloud-filter:focus {
        border: solid $primary;
    }

    CloudProfileSelectorScreen .cloud-list {
        height: auto;
        min-height: 1;
        max-height: 16;
        background: $background;
        scrollbar-gutter: stable;
    }

    CloudProfileSelectorScreen #cloud-options {
        height: auto;
    }

    CloudProfileSelectorScreen .cloud-provider-header {
        color: $primary;
        margin-top: 1;
        text-style: bold;
    }

    CloudProfileSelectorScreen #cloud-options > .cloud-provider-header:first-child {
        margin-top: 0;
    }

    CloudProfileSelectorScreen .cloud-option {
        height: 1;
        padding: 0 1;
    }

    CloudProfileSelectorScreen .cloud-option:hover {
        background: $panel;
    }

    CloudProfileSelectorScreen .cloud-option-selected {
        background: $primary;
        color: $background;
        text-style: bold;
    }

    CloudProfileSelectorScreen .cloud-option-selected:hover {
        background: $primary;
    }

    CloudProfileSelectorScreen .cloud-option-current {
        text-style: italic;
    }

    CloudProfileSelectorScreen .cloud-detail-footer {
        height: 4;
        padding: 0 2;
        margin-top: 1;
        border-top: solid $panel;
        background: $surface;
    }

    CloudProfileSelectorScreen .cloud-selector-help {
        height: auto;
        color: $text-muted;
        text-style: italic;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, current_profile: str | None = None) -> None:
        super().__init__()
        self._current_profile = current_profile or get_active_aws_profile()
        self._default_profile = load_aws_profile()
        self._recent_profiles = load_recent_aws_profiles()

        self._all_profiles: list[AWSProfileInfo] = []
        self._filtered_profiles: list[AWSProfileInfo] = []
        self._selected_index = 0
        self._option_widgets: list[CloudProfileOption] = []
        self._flat_profiles: list[AWSProfileInfo] = []
        self._options_container: Container | None = None
        self._filter_text = ""

    # ── Compose ──────────────────────────────────────────

    def compose(self):
        with Vertical():
            title = f"Select Cloud Profile (current: {self._current_profile})"
            yield Static(title, classes="cloud-selector-title")

            yield Static(
                self._info_content(),
                classes="cloud-selector-info",
                id="cloud-selector-info",
            )

            yield Input(
                placeholder="Type to filter or enter profile name...",
                id="cloud-filter",
            )

            with VerticalScroll(classes="cloud-list"):
                self._options_container = Container(id="cloud-options")
                yield self._options_container

            yield Static("", classes="cloud-detail-footer", id="cloud-detail-footer", markup=True)

            yield Static(
                "↑/↓ navigate • Tab autocomplete • Enter select • Ctrl+S set default • Esc cancel",
                classes="cloud-selector-help",
            )

    def on_mount(self) -> None:
        self._all_profiles = list_aws_profiles()
        self._apply_filter()
        self._rebuild_options()
        try:
            self.query_one("#cloud-filter", Input).focus()
        except NoMatches:
            pass

    # ── Info Line ────────────────────────────────────────

    def _info_content(self) -> str:
        count = len(self._all_profiles)
        if self._filter_text.strip():
            return f"Filtering AWS profiles ({len(self._filtered_profiles)} matches)"
        return f"Showing AWS profiles ({count} configured on system)"

    def _update_info(self) -> None:
        try:
            self.query_one("#cloud-selector-info", Static).update(self._info_content())
        except NoMatches:
            pass

    # ── Filtering ────────────────────────────────────────

    def _apply_filter(self) -> None:
        query = self._filter_text.strip().lower()
        if query:
            self._filtered_profiles = [
                p for p in self._all_profiles
                if query in p.name.lower()
                or (p.region and query in p.region.lower())
                or (p.account_id and query in p.account_id.lower())
                or (p.role_arn and query in p.role_arn.lower())
            ]
        else:
            self._filtered_profiles = list(self._all_profiles)

        if self._selected_index >= len(self._filtered_profiles):
            self._selected_index = max(0, len(self._filtered_profiles) - 1)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "cloud-filter":
            self._filter_text = event.value
            self._selected_index = 0
            self._apply_filter()
            self._rebuild_options()
            self._update_info()

    # ── Rebuild Options ──────────────────────────────────

    def _rebuild_options(self) -> None:
        if not self._options_container:
            return

        self._options_container.remove_children()
        self._option_widgets.clear()
        self._flat_profiles.clear()

        if not self._filtered_profiles:
            self._options_container.mount(
                Static("No AWS profiles match your filter.", classes="cloud-option")
            )
            self._update_detail_footer(None)
            return

        has_filter = bool(self._filter_text.strip())

        # Resolve pinned Recent / Active entries when unfiltered
        recent_matches: list[AWSProfileInfo] = []
        if not has_filter:
            prof_map = {p.name: p for p in self._all_profiles}
            # Add active profile first if known
            if self._current_profile in prof_map:
                recent_matches.append(prof_map[self._current_profile])
            for rname in self._recent_profiles:
                if rname in prof_map and prof_map[rname] not in recent_matches:
                    recent_matches.append(prof_map[rname])

        # Main profile list
        main_profiles = [p for p in self._filtered_profiles if p not in recent_matches] if not has_filter else self._filtered_profiles

        current_flat_index = 0

        # 1. Pinned Active / Recent Section
        if recent_matches and not has_filter:
            self._options_container.mount(
                Static(
                    f"[bold]{_RECENT_SECTION_LABEL}[/bold]",
                    classes="cloud-provider-header",
                    markup=True,
                )
            )
            for prof in recent_matches:
                opt, is_sel = self._create_option_widget(prof, current_flat_index)
                self._options_container.mount(opt)
                self._option_widgets.append(opt)
                self._flat_profiles.append(prof)
                current_flat_index += 1

        # 2. AWS Profiles Section
        if main_profiles:
            header_label = f"AWS Profiles ({len(self._all_profiles)})"
            self._options_container.mount(
                Static(
                    f"[bold]{header_label}[/bold]",
                    classes="cloud-provider-header",
                    markup=True,
                )
            )
            for prof in main_profiles:
                opt, is_sel = self._create_option_widget(prof, current_flat_index)
                self._options_container.mount(opt)
                self._option_widgets.append(opt)
                self._flat_profiles.append(prof)
                current_flat_index += 1

        # Update footer and scroll to selected
        if self._selected_index < len(self._flat_profiles):
            self._update_detail_footer(self._flat_profiles[self._selected_index])
        else:
            self._update_detail_footer(None)

        self._scroll_to_selected()

    def _create_option_widget(
        self,
        prof: AWSProfileInfo,
        flat_index: int,
    ) -> tuple[CloudProfileOption, bool]:
        is_selected = flat_index == self._selected_index
        is_current = prof.name == self._current_profile
        is_default = prof.name == self._default_profile

        cursor = "› " if is_selected else "  "
        text = f"{cursor}{prof.name}"
        if is_current:
            text += " [dim](current)[/dim]"
        if is_default:
            text += " [bold](default)[/bold]"

        classes = "cloud-option"
        if is_selected:
            classes += " cloud-option-selected"
        if is_current:
            classes += " cloud-option-current"

        opt = CloudProfileOption(text, prof, flat_index, classes=classes)
        return opt, is_selected

    # ── Detail Footer Rendering ──────────────────────────

    def _update_detail_footer(self, profile: AWSProfileInfo | None) -> None:
        try:
            footer = self.query_one("#cloud-detail-footer", Static)
        except NoMatches:
            return

        if not profile:
            footer.update("No profile selected.")
            return

        # Line 1: Provider, Profile name, Region
        region_str = profile.region or "us-east-1 (default)"
        line1 = f"Provider: [green]AWS[/green] • Profile: [bold]{profile.name}[/bold] • Region: {region_str}"

        # Line 2: Account ID / Role ARN
        if profile.account_id and profile.role_arn:
            line2 = f"Account: [cyan]{profile.account_id}[/cyan] • Role: {profile.role_arn}"
        elif profile.account_id:
            line2 = f"Account: [cyan]{profile.account_id}[/cyan]"
        elif profile.role_arn:
            line2 = f"Role: {profile.role_arn}"
        elif profile.is_sso:
            line2 = "Authentication: [cyan]AWS SSO Session[/cyan]"
        elif profile.has_keys:
            line2 = "Authentication: [cyan]Static IAM Access Keys[/cyan]"
        else:
            line2 = "Authentication: Standard AWS Credential Chain"

        # Line 3: Capabilities & Status
        status_tag = "[green]configured[/green]"
        caps = "[green]read & write operations active[/green]"
        line3 = f"Status: {status_tag} • Mode: {caps}"

        footer.update(f"{line1}\n{line2}\n{line3}")

    # ── Selection Movement ───────────────────────────────

    def _move_selection(self, delta: int) -> None:
        if not self._option_widgets:
            return
        old = self._selected_index
        new = max(0, min(old + delta, len(self._option_widgets) - 1))
        if new == old:
            return
        self._selected_index = new
        self._rebuild_options()
        self._scroll_to_selected()

    def _scroll_to_selected(self) -> None:
        if not self._option_widgets or self._selected_index >= len(self._option_widgets):
            return
        widget = self._option_widgets[self._selected_index]
        if self._selected_index == 0:
            try:
                scroll = self.query_one(".cloud-list", VerticalScroll)
                scroll.scroll_home(animate=False)
            except NoMatches:
                widget.scroll_visible(animate=False)
        else:
            widget.scroll_visible(animate=False)

    # ── Actions ──────────────────────────────────────────

    def action_move_up(self) -> None:
        self._move_selection(-1)

    def action_move_down(self) -> None:
        self._move_selection(1)

    def action_select(self) -> None:
        if not self._option_widgets:
            typed = self._filter_text.strip()
            if typed:
                known_names = {p.name for p in self._all_profiles}
                if typed not in known_names:
                    self.notify(
                        f"Profile '{typed}' is not configured in ~/.aws/config",
                        severity="warning",
                        timeout=3,
                    )
                self.dismiss((typed, None))
            return

        opt = self._option_widgets[self._selected_index]
        self.dismiss((opt.profile_name, opt.profile_info.region))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_tab_complete(self) -> None:
        if not self._option_widgets:
            return
        opt = self._option_widgets[self._selected_index]
        try:
            inp = self.query_one("#cloud-filter", Input)
            inp.value = opt.profile_name
            inp.cursor_position = len(opt.profile_name)
        except NoMatches:
            pass

    def action_set_default(self) -> None:
        if not self._option_widgets:
            return
        opt = self._option_widgets[self._selected_index]
        pname = opt.profile_name
        self._default_profile = pname
        save_aws_profile(pname)
        self.notify(f"Default AWS profile set: {pname} (saved to config.toml)", severity="information", timeout=3)
        self._rebuild_options()

    def on_cloud_profile_option_clicked(self, event: CloudProfileOption.Clicked) -> None:
        opt = self._option_widgets[event.index] if 0 <= event.index < len(self._option_widgets) else None
        region = opt.profile_info.region if opt else None
        self.dismiss((event.profile_name, region))


__all__ = ["CloudProfileOption", "CloudProfileSelectorScreen"]
