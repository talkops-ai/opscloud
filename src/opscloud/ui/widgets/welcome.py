"""Welcome banner & first-run onboarding widget for OpsCloud TUI.

Displays a two-panel "launch console" card:
  Left  — OpsCloud ASCII art wordmark with a travelling light beam whose
          colour reflects server health, plus a health line and version.
  Right — Environment (model, cloud, project) and capabilities (skills,
          agents, MCP) with health dots on rows that have a live state.

Health colour language (shared by the beam and the row dots):
  amber  → starting / needs attention
  green  → healthy / ready
  red    → error

Lists that exceed the available width show a clickable "+N more" span that
opens a WelcomeDetailPopup modal with the full list and descriptions.

The banner stays in the scrollback and never collapses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import ConsoleRenderable, Group
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget

from opscloud.ui.theme import DC_CYAN, DC_PURPLE, DC_TEAL, ThemeColors, get_theme_colors
from opscloud.ui.widgets.welcome_popup import WelcomeDetailPopup
from opscloud.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opscloud.mcp.mcp_info import MCPServerInfo

logger = get_logger(__name__)

# Maximum number of items shown inline before truncating with "+N more".
MAX_DISPLAY_ITEMS = 5

# Maximum character width for the inline list before truncating.
# Applied after MAX_DISPLAY_ITEMS to catch long names that wrap.
MAX_INLINE_CHARS = 38

# Terminal width below which the two panels stack vertically.
_STACK_BELOW_WIDTH = 104

# Label column width in the info panel ("project  ").
_LABEL_W = 9

# Count column width in the capabilities section.
_COUNT_W = 4

# Fallback info-panel width when rendering before layout (e.g. in tests).
_DEFAULT_INFO_WIDTH = 56

# AWS brand orange for the cloud provider tag.
_AWS_ORANGE = "#FF9900"


def _is_ascii() -> bool:
    try:
        from opscloud.config.settings import is_ascii_mode

        return is_ascii_mode()
    except Exception:
        return False


def _dot() -> str:
    """Return the health-dot glyph (geometric, never an emoji)."""
    return "*" if _is_ascii() else "●"


def _sep() -> str:
    return "|" if _is_ascii() else "·"


# ── Colour helpers ────────────────────────────────────────────────


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _blend(a: str, b: str, t: float) -> str:
    """Linear blend from colour ``a`` (t=0) to colour ``b`` (t=1)."""
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    r = round(ra + (rb - ra) * t)
    g = round(ga + (gb - ga) * t)
    bl = round(ba + (bb - ba) * t)
    return f"#{r:02X}{g:02X}{bl:02X}"


def _gradient(stops: Sequence[str], t: float) -> str:
    """Sample a multi-stop gradient at position ``t`` in [0, 1]."""
    if t <= 0:
        return stops[0]
    if t >= 1:
        return stops[-1]
    span = len(stops) - 1
    idx = min(int(t * span), span - 1)
    local = t * span - idx
    return _blend(stops[idx], stops[idx + 1], local)


# ── OpsCloud ASCII Art Logo ───────────────────────────────────────
# Multi-line ASCII art wordmark for OpsCloud in classic FIGlet lettering.
# In the healthy state the wordmark carries the brand gradient
# (teal → blue → violet); the travelling beam's colour signals health.

_LOGO_LINES: list[str] = [
    r"  ___  ____  ____   ____ _     ___  _   _ ____  ",
    r" / _ \|  _ \/ ___| / ___| |   / _ \| | | |  _ \ ",
    r"| | | | |_) \___ \| |   | |  | | | | | | | | | |",
    r"| |_| |  __/ ___) | |___| |__| |_| | |_| | |_| |",
    r" \___/|_|   |____/ \____|_____\___/ \___/|____/ ",
]

_LOGO_WIDTH = max(len(line) for line in _LOGO_LINES)

_BRAND_STOPS: tuple[str, ...] = (DC_TEAL, DC_CYAN, DC_PURPLE)

# Pre-computed per-column brand gradient for the healthy wordmark.
_BRAND_COLUMN_COLORS: list[str] = [
    _gradient(_BRAND_STOPS, x / max(_LOGO_WIDTH - 1, 1)) for x in range(_LOGO_WIDTH)
]


def _build_logo_text(beam_x: int = -10, status: str = "connected") -> Text:
    """Return an ASCII art wordmark for OpsCloud with an animated light beam.

    When status is 'connected', a vibrant green ray sweeps from 'O' to 'D'
    across the brand gradient. When 'starting', a warm amber ray pulses
    across the letters. When 'error', an alert red beam scans across.
    """
    t = Text()
    for row_idx, line in enumerate(_LOGO_LINES):
        for x, ch in enumerate(line):
            if ch == " ":
                t.append(" ")
                continue
            dist = abs(x - beam_x)
            if status == "connected":
                if dist <= 1:
                    t.append(ch, style="bold #FFFFFF")
                elif dist <= 2:
                    t.append(ch, style="bold #4ADE80")
                elif dist <= 3:
                    t.append(ch, style="bold #22C55E")
                elif dist <= 4:
                    t.append(ch, style="#16A34A")
                else:
                    t.append(ch, style=f"bold {_BRAND_COLUMN_COLORS[x]}")
            elif status in ("starting", "connecting", "warning", "degraded"):
                if dist <= 1:
                    t.append(ch, style="bold #FFFFFF")
                elif dist <= 2:
                    t.append(ch, style="bold #FEF08A")
                elif dist <= 3:
                    t.append(ch, style="bold #FACC15")
                elif dist <= 4:
                    t.append(ch, style="#CA8A04")
                else:
                    t.append(ch, style="dim #D97706")
            else:  # error
                if dist <= 1:
                    t.append(ch, style="bold #FFFFFF")
                elif dist <= 2:
                    t.append(ch, style="bold #FCA5A5")
                elif dist <= 3:
                    t.append(ch, style="bold #EF4444")
                elif dist <= 4:
                    t.append(ch, style="#DC2626")
                else:
                    t.append(ch, style="dim #991B1B")
        if row_idx < len(_LOGO_LINES) - 1:
            t.append("\n")
    return t


# ── Data collection ───────────────────────────────────────────────


def _collect_skills() -> list[tuple[str, str]]:
    """Return (name, description) for all discovered skills."""
    try:
        import opscloud
        from opscloud.config.settings import settings
        from opscloud.skills.loader import list_skills

        built_in_dir = Path(opscloud.__file__).parent / "built_in_skills"
        user_skills_dir = settings.get_user_skills_dir()
        project_skills_dir = settings.get_project_skills_dir()
        project_root = settings.project_root

        skills = list_skills(
            built_in_skills_dir=built_in_dir,
            user_skills_dir=user_skills_dir,
            project_skills_dir=project_skills_dir,
            include_plugins=True,
            project_root=project_root,
        )
        return sorted(
            [(s.get("name", ""), s.get("description", "")) for s in skills if s.get("name")],
            key=lambda x: x[0],
        )
    except Exception:
        logger.debug("Could not discover skills for welcome banner", exc_info=True)
        return []


def _collect_subagents() -> list[tuple[str, str]]:
    """Return (name, description) for all loaded subagents."""
    try:
        from opscloud.config.settings import settings
        from opscloud.subagents import list_subagents

        effective_root = settings.effective_project_root
        user_dir = settings.get_user_agents_dir() if hasattr(settings, "get_user_agents_dir") else None
        proj_dir = settings.get_project_agents_dir() if hasattr(settings, "get_project_agents_dir") else None

        metas = list_subagents(
            user_agents_dir=user_dir,
            project_agents_dir=proj_dir,
            project_root=effective_root,
            include_plugins=True,
        )
        seen = {meta["name"]: meta.get("description", "") for meta in metas if meta.get("name")}
        return sorted(seen.items(), key=lambda x: x[0])
    except Exception:
        logger.debug("Could not discover subagents for welcome banner", exc_info=True)
        return []


def _project_display() -> str:
    """Return the project directory with the home prefix collapsed to ``~``."""
    try:
        from opscloud.config.settings import settings

        root = Path(getattr(settings, "project_root", None) or Path.cwd())
    except Exception:
        root = Path.cwd()
    text = str(root)
    home = os.path.expanduser("~")
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def _version() -> str:
    try:
        from opscloud._version import __version__

        return __version__
    except Exception:
        return ""


def _truncate_to_width(
    names: list[str], max_chars: int, max_items: int,
) -> tuple[list[str], int]:
    """Return (shown_names, remaining_count) fitting within both limits.

    Truncates by *item count* first (MAX_DISPLAY_ITEMS) then by cumulative
    *character width* so long agent names don't wrap the line.
    """
    candidates = names[:max_items]
    result: list[str] = []
    current_len = 0
    for name in candidates:
        sep = 2 if result else 0  # ", " separator
        if current_len + sep + len(name) > max_chars and result:
            break
        result.append(name)
        current_len += sep + len(name)
    remaining = len(names) - len(result)
    return result, remaining


# ── Banner state ──────────────────────────────────────────────────


@dataclass
class _BannerInfo:
    """Everything the info panel renders. Mutated in place by the banner."""

    model_spec: str = ""
    effort: str = ""
    cloud_profile: str = ""
    cloud_region: str = ""
    project: str = ""
    skills: list[tuple[str, str]] = field(default_factory=list)
    subagents: list[tuple[str, str]] = field(default_factory=list)
    mcp_servers: list[MCPServerInfo] | None = None
    """``None`` while discovery is still running; ``[]`` when none configured."""
    mcp_disabled: bool = False


def _health_style(state: str, colors: ThemeColors) -> str:
    return {
        "ok": colors.success,
        "warn": colors.warning,
        "error": colors.error,
    }.get(state, colors.muted)


def _fit(text: Text, width: int) -> Text:
    """Truncate ``text`` to ``width`` cells with an ellipsis."""
    if width <= 0:
        return Text("")
    if text.cell_len > width:
        text = text.copy()
        text.truncate(width, overflow="ellipsis")
    return text


def _short_name(name: str) -> str:
    """Strip plugin/marketplace qualifiers for compact inline display.

    ``aws-compute@talkops-devops-plugins:amazon-ec2-image-builder``
    becomes ``amazon-ec2-image-builder``; ``agent@marketplace`` becomes
    ``agent``. The full identifier is still shown in the detail popup.
    """
    if ":" in name:
        tail = name.rsplit(":", 1)[1]
        if tail:
            return tail
    if "@" in name:
        head = name.split("@", 1)[0]
        if head:
            return head
    return name


def _name_list(
    names: list[str],
    width: int,
    name_style: str,
    more_style: Style,
) -> Text:
    """Comma-joined names that fit ``width``, with a clickable ``+N more``."""
    out = Text()
    if not names:
        return out
    shown: list[str] = []
    used = 0
    for idx, name in enumerate(names):
        remaining_after = len(names) - (idx + 1)
        more_len = len(f"  +{remaining_after} more") if remaining_after else 0
        sep = 2 if shown else 0
        if used + sep + len(name) + more_len > width:
            break
        shown.append(name)
        used += sep + len(name)
    if not shown:
        # Not even one full name fits: show a truncated first name.
        remaining = len(names) - 1
        more_len = len(f"  +{remaining} more") if remaining else 0
        first = _fit(Text(names[0], style=name_style), max(width - more_len, 4))
        out.append_text(first)
        shown = [names[0]]
    else:
        for i, name in enumerate(shown):
            if i:
                out.append(", ", style=name_style)
            out.append(name, style=name_style)
    remaining = len(names) - len(shown)
    if remaining:
        out.append("  ")
        out.append(f"+{remaining} more", style=more_style)
    return out


def _build_info_text(
    info: _BannerInfo,
    *,
    width: int,
    status: str,
    colors: ThemeColors,
) -> Text:
    """Render the environment + capabilities panel as a single ``Text``."""
    dot = _dot()
    sep = _sep()
    label_style = _blend(colors.foreground, colors.background, 0.45)
    header_style = f"bold {colors.primary}"
    value_style = colors.foreground
    secondary_style = _blend(colors.foreground, colors.background, 0.35)

    value_w = max(width - 2 - _LABEL_W, 8)
    out = Text(no_wrap=True, overflow="ellipsis")

    def header(title: str) -> None:
        out.append(title, style=header_style)
        out.append("\n")

    def row(health: str | None, label: str, value: Text) -> None:
        if health is None:
            out.append("  ")
        else:
            out.append(f"{dot} ", style=_health_style(health, colors))
        out.append(f"{label:<{_LABEL_W}}", style=label_style)
        out.append_text(_fit(value, value_w))
        out.append("\n")

    # ── Environment ────────────────────────────────────────────
    header("Environment")

    # Model — health follows server connection state.
    model_health = {"connected": "ok", "error": "error"}.get(status, "warn")
    model_val = Text()
    if info.model_spec:
        provider, _, model = info.model_spec.partition(":")
        if not model:
            provider, model = "", provider
        if provider:
            model_val.append(f"{provider}:", style=secondary_style)
        model_val.append(model, style=f"bold {value_style}")
        if info.effort:
            model_val.append(f"  {info.effort}", style=colors.skill)
    else:
        model_health = "warn"
        model_val.append("not configured", style=colors.warning)
        model_val.append(f"  {sep}  /model to choose", style=secondary_style)
    row(model_health, "model", model_val)

    # Cloud — healthy when a profile is active.
    cloud_val = Text()
    if info.cloud_profile:
        cloud_health = "ok"
        cloud_val.append("aws ", style=f"bold {_AWS_ORANGE}")
        cloud_val.append(info.cloud_profile, style=value_style)
        if info.cloud_region:
            cloud_val.append(f" {sep} {info.cloud_region}", style=secondary_style)
    else:
        cloud_health = "warn"
        cloud_val.append("no profile", style=colors.warning)
        cloud_val.append(f"  {sep}  /cloud to select", style=secondary_style)
    row(cloud_health, "cloud", cloud_val)

    # Project — static, no health.
    project = info.project or _project_display()
    if len(project) > value_w:
        project = "…" + project[-(value_w - 1):]
    row(None, "project", Text(project, style=secondary_style))

    out.append("\n")

    # ── Capabilities ───────────────────────────────────────────
    header("Capabilities")
    list_w = max(value_w - _COUNT_W, 8)

    def count_cell(n: int) -> Text:
        return Text(f"{n:<{_COUNT_W}}", style=f"bold {value_style}")

    skills_val = count_cell(len(info.skills))
    if info.skills:
        skills_val.append_text(
            _name_list(
                [_short_name(n) for n, _ in info.skills],
                list_w,
                colors.skill,
                Style(underline=True, color=colors.secondary, meta={"@click": "show_skills_popup()"}),
            )
        )
    else:
        skills_val.append("none discovered", style=secondary_style)
    row(None, "skills", skills_val)

    agents_val = count_cell(len(info.subagents))
    if info.subagents:
        agents_val.append_text(
            _name_list(
                [_short_name(n) for n, _ in info.subagents],
                list_w,
                colors.secondary,
                Style(underline=True, color=colors.secondary, meta={"@click": "show_agents_popup()"}),
            )
        )
    else:
        agents_val.append("none loaded", style=secondary_style)
    row(None, "agents", agents_val)

    # MCP — health reflects server probe results.
    if info.mcp_disabled:
        mcp_health: str | None = None
        mcp_val = Text("disabled", style=secondary_style)
    elif info.mcp_servers is None:
        mcp_health = "warn"
        mcp_val = Text(f"{'':<{_COUNT_W}}", style=value_style)
        mcp_val.append("discovering servers…", style=secondary_style)
    elif not info.mcp_servers:
        mcp_health = None
        mcp_val = count_cell(0)
        mcp_val.append("none configured", style=secondary_style)
    else:
        active = [s for s in info.mcp_servers if s.enabled]
        attention = [s for s in active if s.needs_attention()]
        tools = sum(s.tool_count for s in active if not s.needs_attention())
        mcp_health = "warn" if attention else "ok"
        mcp_val = count_cell(len(active))
        mcp_val.append("servers", style=secondary_style)
        mcp_val.append(f" {sep} ", style=secondary_style)
        mcp_val.append(str(tools), style=f"bold {value_style}")
        mcp_val.append(" tools", style=secondary_style)
        if attention:
            mcp_val.append(f" {sep} ", style=secondary_style)
            mcp_val.append(f"{len(attention)} need attention", style=colors.warning)
    row(mcp_health, "mcp", mcp_val)

    out.rstrip()
    return out


# ── Left panel widget ─────────────────────────────────────────────


class _LeftPanel(Widget):
    """Reactive left panel: animated logo + health line + version.

    The ``status`` reactive property drives the health colour and
    light-beam animation:
    ``"starting"``  → warm amber pulse, amber dot.
    ``"connected"`` → green beam over the brand gradient, green dot.
    ``"error"``     → red alert scan, red dot.
    """

    DEFAULT_CSS = """
    _LeftPanel {
        width: 51;
        height: auto;
        padding: 1 0 0 1;
    }
    """

    status: reactive[str] = reactive("starting")
    beam_x: reactive[int] = reactive(-6)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cycle_count = 0
        self._anim_timer: Timer | None = None

    def on_mount(self) -> None:
        """Start the light-beam travelling animation."""
        self._anim_timer = self.set_interval(0.08, self._advance_beam)

    def on_unmount(self) -> None:
        """Stop animation timer on unmount."""
        if self._anim_timer is not None:
            self._anim_timer.stop()
            self._anim_timer = None

    def _advance_beam(self) -> None:
        """Advance the light beam position from left to right."""
        next_x = self.beam_x + 2
        # Logo is 48 chars wide. Travel past the end (up to 64) for a short pause between sweeps.
        if next_x > 64:
            self.beam_x = -6
            self._cycle_count += 1
        else:
            self.beam_x = next_x
        self.refresh()

    def _health_line(self) -> Text:
        """``● ready`` on the left, ``v0.x · by talkops.ai`` on the right."""
        colors = get_theme_colors(self) if self.is_attached else get_theme_colors(None)
        label, color = {
            "connected": ("ready", colors.success),
            "error": ("startup failed", colors.error),
            "warning": ("degraded", colors.warning),
            "degraded": ("degraded", colors.warning),
            "starting": ("connecting", colors.warning),
            "connecting": ("connecting", colors.warning),
        }.get(self.status, ("connecting", colors.warning))

        left = Text()
        left.append(f"{_dot()} ", style=color)
        left.append(label, style=f"bold {color}")

        right = Text()
        version = _version()
        muted = _blend(colors.foreground, colors.background, 0.45)
        if version:
            right.append(f"v{version}", style=muted)
            right.append(f" {_sep()} ", style=muted)
        right.append("by talkops.ai", style=f"italic {DC_CYAN}")

        gap = max(_LOGO_WIDTH - left.cell_len - right.cell_len, 2)
        line = Text()
        line.append_text(left)
        line.append(" " * gap)
        line.append_text(right)
        return line

    def render(self) -> ConsoleRenderable:
        """Build the left-panel content."""
        logo = _build_logo_text(beam_x=self.beam_x, status=self.status)
        return Group(logo, Text(""), self._health_line())

    def watch_status(self, _old: str, new: str) -> None:
        """Auto-refresh when status changes and ensure animation timer is active."""
        if self._anim_timer is None and self.is_attached:
            try:
                self._anim_timer = self.set_interval(0.08, self._advance_beam)
            except Exception:
                pass
        self.refresh()


# ── Right panel widget ────────────────────────────────────────────


class _InfoPanel(Widget):
    """Environment + capabilities panel with health dots.

    Renders at its actual width so long lists never wrap mid-word; overflow
    collapses into a clickable ``+N more`` that opens a detail popup.
    """

    DEFAULT_CSS = """
    _InfoPanel {
        width: 1fr;
        height: auto;
        padding: 1 2 1 2;
    }
    """

    status: reactive[str] = reactive("starting")

    def __init__(self, info: _BannerInfo, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._info = info

    def render(self) -> Text:
        width = self.content_size.width or _DEFAULT_INFO_WIDTH
        return _build_info_text(
            self._info,
            width=width,
            status=self.status,
            colors=get_theme_colors(self),
        )

    def get_content_height(self, container: Any, viewport: Any, width: int) -> int:
        return len(
            _build_info_text(
                self._info,
                width=width or _DEFAULT_INFO_WIDTH,
                status=self.status,
                colors=get_theme_colors(self),
            ).plain.splitlines()
        )

    def watch_status(self, _old: str, _new: str) -> None:
        self.refresh()

    async def action_show_skills_popup(self) -> None:
        """Push the skills detail popup."""
        await self.app.push_screen(
            WelcomeDetailPopup(title="Discovered Skills", items=self._info.skills)
        )

    async def action_show_agents_popup(self) -> None:
        """Push the subagents detail popup."""
        await self.app.push_screen(
            WelcomeDetailPopup(title="Loaded Subagents", items=self._info.subagents)
        )


# ── Main banner ───────────────────────────────────────────────────


class WelcomeBanner(Widget):
    """Two-panel welcome banner card shown on app startup.

    Left panel: animated OpsCloud wordmark, health line, version.
    Right panel: environment and capabilities with health dots.

    The banner stays in the scrollback; there is no dismiss button.
    """

    DEFAULT_CSS = """
    WelcomeBanner {
        width: 100%;
        height: auto;
        margin: 0 0 1 0;
        align-horizontal: center;
        background: transparent;
    }

    WelcomeBanner #welcome-card {
        width: 116;
        max-width: 100%;
        height: auto;
        border: round $primary 55%;
        background: transparent;
        border-subtitle-align: left;
        border-subtitle-color: $text-muted;
    }

    WelcomeBanner _LeftPanel {
        width: 51;
        height: auto;
        padding: 1 0 0 1;
    }

    WelcomeBanner #welcome-right {
        width: 1fr;
        height: auto;
        border-left: solid $primary 25%;
    }

    WelcomeBanner #welcome-card.-stacked {
        layout: vertical;
    }

    WelcomeBanner #welcome-card.-stacked #welcome-right {
        width: 100%;
        border-left: none;
        border-top: solid $primary 25%;
    }
    """

    def __init__(
        self,
        *,
        model_spec: str = "",
        effort: str = "",
        cloud_profile: str = "",
        cloud_region: str = "",
        mcp_servers: Sequence[MCPServerInfo] | None = None,
        mcp_disabled: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.border_title = None
        self._info = _BannerInfo(
            model_spec=model_spec,
            effort=effort,
            cloud_profile=cloud_profile,
            cloud_region=cloud_region,
            mcp_servers=list(mcp_servers) if mcp_servers else None,
            mcp_disabled=mcp_disabled,
        )
        self._status = "starting"

    # Backwards-compatible accessors (used by tests / callers).
    @property
    def _skills_data(self) -> list[tuple[str, str]]:
        return self._info.skills

    @_skills_data.setter
    def _skills_data(self, value: list[tuple[str, str]]) -> None:
        self._info.skills = list(value)

    @property
    def _subagents_data(self) -> list[tuple[str, str]]:
        return self._info.subagents

    @_subagents_data.setter
    def _subagents_data(self, value: list[tuple[str, str]]) -> None:
        self._info.subagents = list(value)

    def compose(self) -> ComposeResult:
        self._info.skills = _collect_skills()
        self._info.subagents = _collect_subagents()
        self._info.project = _project_display()

        card = Horizontal(id="welcome-card")
        card.border_subtitle = self._hint_markup()
        with card:
            yield _LeftPanel(id="welcome-left")
            yield _InfoPanel(self._info, id="welcome-right")

    def on_resize(self, event: events.Resize) -> None:
        """Stack the panels on narrow terminals so nothing gets clipped."""
        try:
            card = self.query_one("#welcome-card", Horizontal)
        except Exception:
            return
        card.set_class(event.size.width < _STACK_BELOW_WIDTH, "-stacked")

    @staticmethod
    def _hint_markup() -> str:
        sep = " [dim]" + _sep() + "[/dim] "
        items = [
            ("/", "commands"),
            ("!", "shell"),
            ("ctrl+o", "expand output"),
            ("esc", "interrupt"),
        ]
        body = sep.join(f"[b]{key}[/b] {label}" for key, label in items)
        return f" {body} "

    # ── Public API ────────────────────────────────────────────────

    def update_cloud(self, profile: str = "", region: str = "") -> None:
        """Reflect the active cloud profile / region in the environment rows."""
        self._info.cloud_profile = profile or ""
        self._info.cloud_region = region or ""
        self._refresh_info()

    def update_model(self, model_spec: str, effort: str = "") -> None:
        """Reflect the active model (``provider:model``) and reasoning effort."""
        self._info.model_spec = model_spec or ""
        self._info.effort = effort or ""
        self._refresh_info()

    def update_mcp(self, servers: Sequence[MCPServerInfo] | None) -> None:
        """Reflect probed MCP servers. ``None`` keeps the 'discovering' state."""
        self._info.mcp_servers = list(servers) if servers is not None else None
        self._refresh_info()

    def set_connected(self) -> None:
        """Transition the health indicators to green 'ready'."""
        self._set_status("connected")

    def set_starting(self) -> None:
        """Transition the health indicators to yellow 'connecting'."""
        self._set_status("starting")

    def set_warning(self) -> None:
        """Transition the health indicators to yellow 'warning'."""
        self._set_status("warning")

    def set_error(self) -> None:
        """Transition the health indicators to red 'error'."""
        self._set_status("error")

    # ── Private helpers ───────────────────────────────────────────

    def _set_status(self, status: str) -> None:
        self._status = status
        for widget_type in (_LeftPanel, _InfoPanel):
            try:
                self.query_one(widget_type).status = status
            except Exception:
                logger.debug("Could not update banner status", exc_info=True)

    def _refresh_info(self) -> None:
        try:
            self.query_one(_InfoPanel).refresh(layout=True)
        except Exception:
            pass

    def _build_right_panel(self, width: int = _DEFAULT_INFO_WIDTH) -> Text:
        """Render the info panel text (used by tests and for previews)."""
        return _build_info_text(
            self._info,
            width=width,
            status=self._status,
            colors=get_theme_colors(self) if self.is_attached else get_theme_colors(None),
        )
