"""Interactive cloud profile selector screen for `/cloud` command."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Container, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Click
from textual.fuzzy import Matcher
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
from opscloud.utils.logger import get_logger

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
        is_selected: bool = False,
        is_current: bool = False,
        is_default: bool = False,
        classes: str = "",
    ) -> None:
        super().__init__(label, classes=classes, markup=True)
        self.profile_info = profile_info
        self.profile_name = profile_info.name
        self.index = index
        self.is_selected = is_selected
        self.is_current = is_current
        self.is_default = is_default

    class Clicked(Message):
        """Posted when a cloud profile row is clicked."""

        def __init__(self, profile_name: str, index: int) -> None:
            super().__init__()
            self.profile_name = profile_name
            self.index = index

    def on_click(self, event: Click) -> None:
        event.stop()
        if event.chain > 1:
            return
        try:
            if getattr(self.screen, "_dismissed", False):
                return
        except Exception:
            pass
        self.post_message(self.Clicked(self.profile_name, self.index))

    def render_label_text(self) -> str:
        cursor = "› " if self.is_selected else "  "
        text = f"{cursor}{self.profile_name}"
        if self.is_current:
            text += " [dim](current)[/dim]"
        if self.is_default:
            text += " [bold](default)[/bold]"
        return text

    def set_selected(self, selected: bool) -> None:
        if self.is_selected == selected:
            return
        self.is_selected = selected
        if selected:
            self.add_class("cloud-option-selected")
        else:
            self.remove_class("cloud-option-selected")
        self.update(self.render_label_text())


class CloudProfileSelectorScreen(ModalScreen[tuple[str, str | None] | None]):
    """Full-screen modal for interactive cloud profile selection."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_up", "Up", show=False, priority=True),
        Binding("down", "move_down", "Down", show=False, priority=True),
        Binding("pageup", "page_up", "Page Up", show=False, priority=True),
        Binding("pagedown", "page_down", "Page Down", show=False, priority=True),
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
        self._dismissed: bool = False
        self._loaded: bool = False
        self._rebuild_counter: int = 0

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
                "↑/↓ navigate • PgUp/PgDn page • Tab complete • Enter select • Ctrl+S default • Esc cancel",
                classes="cloud-selector-help",
            )

    def on_mount(self) -> None:
        try:
            self.query_one("#cloud-filter", Input).focus()
        except NoMatches:
            pass
        self.call_after_refresh(self._fit_cloud_list)

        try:
            has_app = self.app is not None
        except Exception:
            has_app = False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if has_app and loop and loop.is_running():
            self.run_worker(self._async_load_profiles(), exclusive=True)
        else:
            self._sync_load_profiles()

    def on_resize(self) -> None:
        self.call_after_refresh(self._fit_cloud_list)

    def _fit_cloud_list(self) -> None:
        try:
            container = self.query_one(Vertical)
            body = self.query_one(".cloud-list", VerticalScroll)
        except NoMatches:
            return
        non_body_height = max(0, container.region.height - body.region.height)
        available_height = self.size.height - non_body_height
        max_height = max(1, min(16, available_height))
        current = body.styles.max_height
        if current is not None and current.cells == max_height:
            return
        body.styles.max_height = max_height

    def _load_profile_data(self) -> list[AWSProfileInfo]:
        return list_aws_profiles()

    async def _async_load_profiles(self) -> None:
        try:
            profiles = await asyncio.to_thread(self._load_profile_data)
        except Exception:
            logger.exception("Failed to load AWS profiles in background")
            profiles = []
        if getattr(self, "_dismissed", False):
            return
        self._all_profiles = profiles
        self._loaded = True
        self._apply_filter()
        await self._rebuild_options()
        self._update_info()

    def _sync_load_profiles(self) -> None:
        self._all_profiles = self._load_profile_data()
        self._loaded = True
        self._apply_filter()
        self._rebuild_options_sync()
        self._update_info()

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
            tokens = query.split()
            try:
                matchers = [Matcher(tok, case_sensitive=False) for tok in tokens]
                scored: list[tuple[float, AWSProfileInfo]] = []
                for p in self._all_profiles:
                    haystack = f"{p.name} {p.region or ''} {p.account_id or ''} {p.role_arn or ''}".lower()
                    scores = [m.match(haystack) for m in matchers]
                    if all(s > 0 for s in scores) or all(tok in haystack for tok in tokens):
                        score = min(scores) if scores and all(s > 0 for s in scores) else 0.5
                        scored.append((score, p))
                scored.sort(key=lambda x: x[0], reverse=True)
                self._filtered_profiles = [p for _, p in scored]
            except Exception:
                self._filtered_profiles = [
                    p for p in self._all_profiles
                    if all(t in f"{p.name} {p.region or ''} {p.account_id or ''} {p.role_arn or ''}".lower() for t in tokens)
                ]
        else:
            self._filtered_profiles = list(self._all_profiles)

        if self._selected_index >= len(self._filtered_profiles):
            self._selected_index = max(0, len(self._filtered_profiles) - 1)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "cloud-filter":
            self._filter_text = event.value
            self._selected_index = 0
            self._update_info()
            if not self._loaded:
                return
            self._apply_filter()
            self._schedule_rebuild()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_select()

    def _schedule_rebuild(self) -> None:
        self._rebuild_counter += 1
        gen = self._rebuild_counter
        self.call_after_refresh(self._debounced_rebuild, gen)

    async def _debounced_rebuild(self, gen: int) -> None:
        if gen != self._rebuild_counter:
            return
        await self._rebuild_options()

    # ── Rebuild Options ──────────────────────────────────

    def _build_widgets(self) -> list[Static]:
        self._option_widgets.clear()
        self._flat_profiles.clear()

        if not self._filtered_profiles:
            if not self._loaded:
                return [Static("Loading AWS profiles...", classes="cloud-option")]
            return [Static("No AWS profiles match your filter.", classes="cloud-option")]

        has_filter = bool(self._filter_text.strip())

        # Resolve pinned Recent / Active entries when unfiltered
        recent_matches: list[AWSProfileInfo] = []
        if not has_filter:
            prof_map = {p.name: p for p in self._all_profiles}
            if self._current_profile in prof_map:
                recent_matches.append(prof_map[self._current_profile])
            for rname in self._recent_profiles:
                if rname in prof_map and prof_map[rname] not in recent_matches:
                    recent_matches.append(prof_map[rname])

        main_profiles = (
            [p for p in self._filtered_profiles if p not in recent_matches]
            if not has_filter
            else self._filtered_profiles
        )

        widgets: list[Static] = []
        current_flat_index = 0

        # 1. Pinned Active / Recent Section
        if recent_matches and not has_filter:
            widgets.append(
                Static(
                    f"[bold]{_RECENT_SECTION_LABEL}[/bold]",
                    classes="cloud-provider-header",
                    markup=True,
                )
            )
            for prof in recent_matches:
                opt = self._create_option_widget(prof, current_flat_index)
                widgets.append(opt)
                self._option_widgets.append(opt)
                self._flat_profiles.append(prof)
                current_flat_index += 1

        # 2. AWS Profiles Section
        if main_profiles:
            header_label = f"AWS Profiles ({len(self._all_profiles)})"
            widgets.append(
                Static(
                    f"[bold]{header_label}[/bold]",
                    classes="cloud-provider-header",
                    markup=True,
                )
            )
            for prof in main_profiles:
                opt = self._create_option_widget(prof, current_flat_index)
                widgets.append(opt)
                self._option_widgets.append(opt)
                self._flat_profiles.append(prof)
                current_flat_index += 1

        return widgets

    async def _rebuild_options(self) -> None:
        if not self._options_container:
            return

        self._rebuild_counter += 1
        gen = self._rebuild_counter

        widgets = self._build_widgets()
        await self._options_container.remove_children()

        if gen != self._rebuild_counter:
            return

        if widgets:
            await self._options_container.mount(*widgets)

        if not self._filtered_profiles:
            self._update_detail_footer(None)
        else:
            self._update_detail_footer(
                self._flat_profiles[self._selected_index]
                if self._flat_profiles and self._selected_index < len(self._flat_profiles)
                else None
            )
            self._scroll_to_selected()

    def _rebuild_options_sync(self) -> None:
        if not self._options_container:
            return
        widgets = self._build_widgets()
        self._options_container.remove_children()
        if widgets:
            self._options_container.mount(*widgets)
        if not self._filtered_profiles:
            self._update_detail_footer(None)
        else:
            self._update_detail_footer(
                self._flat_profiles[self._selected_index]
                if self._flat_profiles and self._selected_index < len(self._flat_profiles)
                else None
            )
            self._scroll_to_selected()

    def _create_option_widget(
        self,
        prof: AWSProfileInfo,
        flat_index: int,
    ) -> CloudProfileOption:
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

        return CloudProfileOption(
            text,
            prof,
            flat_index,
            is_selected=is_selected,
            is_current=is_current,
            is_default=is_default,
            classes=classes,
        )

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

    def _set_selected_index(self, new_index: int, scroll: bool = False) -> None:
        if not self._option_widgets:
            return
        new_index = max(0, min(new_index, len(self._option_widgets) - 1))
        old_index = self._selected_index
        if new_index == old_index and not scroll:
            return

        # Deselect previous
        if 0 <= old_index < len(self._option_widgets):
            self._option_widgets[old_index].set_selected(False)

        # Select new
        self._selected_index = new_index
        if 0 <= new_index < len(self._option_widgets):
            self._option_widgets[new_index].set_selected(True)

        # Update footer
        if self._flat_profiles and new_index < len(self._flat_profiles):
            self._update_detail_footer(self._flat_profiles[new_index])

        if scroll:
            self._scroll_to_selected()

    def _move_selection(self, delta: int) -> None:
        if not self._option_widgets:
            return
        count = len(self._option_widgets)
        new_index = (self._selected_index + delta) % count
        self._set_selected_index(new_index, scroll=True)

    def _scroll_to_selected(self) -> None:
        if not self._option_widgets or self._selected_index >= len(self._option_widgets):
            return
        widget = self._option_widgets[self._selected_index]
        if self._selected_index == 0:
            try:
                scroll = self.query_one(".cloud-list", VerticalScroll)
                scroll.scroll_home(animate=False)
            except Exception:
                try:
                    widget.scroll_visible(animate=False)
                except Exception:
                    pass
        elif self._selected_index == len(self._option_widgets) - 1:
            try:
                scroll = self.query_one(".cloud-list", VerticalScroll)
                scroll.scroll_end(animate=False)
            except Exception:
                try:
                    widget.scroll_visible(animate=False)
                except Exception:
                    pass
        else:
            try:
                widget.scroll_visible(animate=False)
            except Exception:
                pass

    # ── Actions ──────────────────────────────────────────

    def action_move_up(self) -> None:
        self._move_selection(-1)

    def action_move_down(self) -> None:
        self._move_selection(1)

    def _visible_page_size(self) -> int:
        default_page_size = 10
        try:
            scroll = self.query_one(".cloud-list", VerticalScroll)
            height = scroll.size.height
        except Exception:
            return default_page_size
        if height <= 0:
            return default_page_size
        total_options = len(self._option_widgets)
        if total_options == 0:
            return default_page_size
        num_headers = len(self.query(".cloud-provider-header"))
        header_rows = max(0, num_headers * 2 - 1) if num_headers else 0
        total_rows = total_options + header_rows
        return max(1, int(height * total_options / total_rows))

    def action_page_up(self) -> None:
        if not self._option_widgets:
            return
        page = self._visible_page_size()
        target = max(0, self._selected_index - page)
        delta = target - self._selected_index
        if delta != 0:
            self._move_selection(delta)

    def action_page_down(self) -> None:
        if not self._option_widgets:
            return
        count = len(self._option_widgets)
        page = self._visible_page_size()
        target = min(count - 1, self._selected_index + page)
        delta = target - self._selected_index
        if delta != 0:
            self._move_selection(delta)

    def action_select(self) -> None:
        if getattr(self, "_dismissed", False):
            return
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

    def dismiss(
        self,
        result: tuple[str, str | None] | None = None,
    ) -> Any:
        """Safely dismiss the modal screen with idempotency protection."""
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
                        "CloudProfileSelectorScreen.dismiss bypassed: not in stack or stack size <= 1"
                    )
                    return None

        try:
            return super().dismiss(result)
        except Exception as exc:
            logger.warning("Error dismissing CloudProfileSelectorScreen: %s", exc)
            return None

    def action_cancel(self) -> None:
        if getattr(self, "_dismissed", False):
            return
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
        for w in self._option_widgets:
            w.is_default = (w.profile_name == self._default_profile)
            w.update(w.render_label_text())

    def on_cloud_profile_option_clicked(self, event: CloudProfileOption.Clicked) -> None:
        if getattr(self, "_dismissed", False):
            return
        opt = self._option_widgets[event.index] if 0 <= event.index < len(self._option_widgets) else None
        region = opt.profile_info.region if opt else None
        self.dismiss((event.profile_name, region))


__all__ = ["CloudProfileOption", "CloudProfileSelectorScreen"]
