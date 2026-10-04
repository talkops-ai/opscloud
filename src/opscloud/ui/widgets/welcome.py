"""Welcome banner & first-run onboarding widget for OpsCloud TUI.

Displays a Claude-Code-inspired two-panel split layout:
  Left  — OpsCloud ASCII art logo, version, project directory, connection status.
  Right — Prompt help, discovered skills list, loaded subagents list.

Lists that exceed MAX_DISPLAY_ITEMS show a clickable "…more" span that
opens a WelcomeDetailPopup modal with the full list and descriptions.

The left panel status indicator transitions from yellow "starting" to
green "connected" when the agent server becomes ready.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rich.console import ConsoleRenderable, Group
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from opscloud.ui.theme import DC_CYAN, DC_TEAL
from opscloud.ui.widgets.welcome_popup import WelcomeDetailPopup
from opscloud.utils.logger import get_logger

logger = get_logger(__name__)

# Maximum number of items shown inline before truncating with "…more".
MAX_DISPLAY_ITEMS = 5

# Maximum character width for the inline list before truncating.
# Applied after MAX_DISPLAY_ITEMS to catch long names that wrap.
MAX_INLINE_CHARS = 38


# ── OpsCloud ASCII Art Logo ───────────────────────────────────────
# Multi-line ASCII art wordmark for OpsCloud in classic FIGlet lettering.
# Rendered with brand teal & cyan accents, with a travelling light beam
# that animates across the wordmark from 'O' to 'D'.

_LOGO_LINES: list[str] = [
    r"  ___  ____  ____   ____ _     ___  _   _ ____  ",
    r" / _ \|  _ \/ ___| / ___| |   / _ \| | | |  _ \ ",
    r"| | | | |_) \___ \| |   | |  | | | | | | | | | |",
    r"| |_| |  __/ ___) | |___| |__| |_| | |_| | |_| |",
    r" \___/|_|   |____/ \____|_____\___/ \___/|____/ ",
]


def _build_logo_text(beam_x: int = -10, status: str = "connected") -> Text:
    """Return an ASCII art wordmark for OpsCloud with an animated light beam.

    When status is 'connected', a vibrant green ray sweeps from 'O' to 'D'.
    When 'starting', a warm amber ray pulses across the letters.
    When 'error', an alert red beam scans across.
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
                    style = f"bold {DC_TEAL}" if x < 19 else f"bold {DC_CYAN}"
                    t.append(ch, style=style)
            elif status == "starting":
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


def _collect_skills() -> list[tuple[str, str]]:
    """Return (name, description) for all discovered skills."""
    try:
        from pathlib import Path
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


# ── Left panel widget ─────────────────────────────────────────────


class _LeftPanel(Widget):
    """Reactive left panel: animated logo + tagline + connection status.

    The ``status`` reactive property drives the status indicator colour and
    light-beam animation:
    ``"starting"``  → warm amber pulse, yellow dot.
    ``"connected"`` → vibrant green stream travelling 'O' to 'D', green dot.
    ``"error"``     → red alert scan, red dot.
    """

    DEFAULT_CSS = """
    _LeftPanel {
        width: 51;
        height: auto;
        padding: 0 0 0 1;
    }
    """

    status: reactive[str] = reactive("starting")
    beam_x: reactive[int] = reactive(-6)

    def on_mount(self) -> None:
        """Start the light-beam travelling animation."""
        self._anim_timer = self.set_interval(0.08, self._advance_beam)

    def on_unmount(self) -> None:
        """Stop animation timer on unmount."""
        if hasattr(self, "_anim_timer"):
            self._anim_timer.stop()

    def _advance_beam(self) -> None:
        """Advance the light beam position from left to right."""
        next_x = self.beam_x + 2
        # Logo is 48 chars wide. Travel past the end (up to 64) for a short pause.
        if next_x > 64:
            self.beam_x = -6
        else:
            self.beam_x = next_x

    def render(self) -> ConsoleRenderable:
        """Build the left-panel content."""
        logo = _build_logo_text(beam_x=self.beam_x, status=self.status)

        # Tagline aligned directly under the 'OUD' in OPSCLOUD (cols 34-47)
        tagline = Text()
        tagline.append(" " * 34, style="")
        tagline.append("by talkops.ai\n", style=f"dim italic {DC_CYAN}")

        return Group(logo, tagline)

    def watch_status(self, _old: str, _new: str) -> None:
        """Auto-refresh when status changes."""
        self.refresh()


# ── Main banner ───────────────────────────────────────────────────


class WelcomeBanner(Widget):
    """Two-panel welcome banner card shown on app startup.

    Left panel: OpsCloud ASCII art logo, version, project dir, status.
    Right panel: prompt help, skills list, subagents list.

    The banner auto-dismisses on first user submission; no explicit
    dismiss button.
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
        width: 114;
        max-width: 100%;
        height: auto;
        border: round $primary;
        background: transparent;
    }

    WelcomeBanner _LeftPanel {
        width: 51;
        height: auto;
        padding: 0 0 0 1;
    }

    WelcomeBanner #welcome-right {
        width: 1fr;
        height: auto;
        padding: 0 1 0 1;
        border-left: solid $primary;
    }
    """

    # Store item data for popup actions
    _skills_data: list[tuple[str, str]]
    _subagents_data: list[tuple[str, str]]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.border_title = None
        self._skills_data = []
        self._subagents_data = []

    def compose(self) -> ComposeResult:
        # Collect data
        self._skills_data = _collect_skills()
        self._subagents_data = _collect_subagents()

        with Horizontal(id="welcome-card"):
            yield _LeftPanel(id="welcome-left")
            # Use _ClickablePanel so @click actions resolve on the widget
            # that actually renders the text with clickable "…more" spans.
            yield _ClickablePanel(
                self._build_right_panel(),
                skills_data=self._skills_data,
                subagents_data=self._subagents_data,
                id="welcome-right",
            )

    # ── Public API ────────────────────────────────────────────────

    def update_cloud(self, profile: str = "", region: str = "") -> None:
        """No-op: cloud context is displayed on the status bar."""
        pass

    def set_connected(self) -> None:
        """Transition the status indicator to green 'connected'."""
        try:
            left = self.query_one(_LeftPanel)
            left.status = "connected"
        except Exception:
            logger.debug("Could not update banner status", exc_info=True)

    def set_error(self) -> None:
        """Transition the status indicator to red 'error'."""
        try:
            left = self.query_one(_LeftPanel)
            left.status = "error"
        except Exception:
            logger.debug("Could not update banner status", exc_info=True)

    # ── Private builders ──────────────────────────────────────────

    def _build_right_panel(self) -> Text:
        """Prompt help + skills + subagents + cloud profile."""
        text = Text()

        # Quick-start help
        text.append("Type a prompt", style="bold")
        text.append(" or use ", style="dim")
        text.append("/help", style="bold cyan")
        text.append(" for commands. ", style="dim")
        text.append("!cmd", style="bold green")
        text.append(" runs shell.\n", style="dim")

        # Skills section
        text.append("\n")
        text.append("Skills   ", style="bold #BB9AF7")
        if self._skills_data:
            names = [n for n, _ in self._skills_data]
            shown, remaining = _truncate_to_width(names, MAX_INLINE_CHARS, MAX_DISPLAY_ITEMS)
            text.append(", ".join(shown), style="#9ECE6A")
            if remaining:
                text.append("  ", style="")
                text.append(
                    f"…{remaining} more",
                    style=Style(
                        bold=True, underline=True, color="rgb(122,162,247)",
                        meta={"@click": "show_skills_popup()"},
                    ),
                )
        else:
            text.append("none discovered", style="dim italic")
        text.append("\n", style="")

        # Agents section
        text.append("\n")
        text.append("Agents   ", style="bold #BB9AF7")
        if self._subagents_data:
            names = [n for n, _ in self._subagents_data]
            shown, remaining = _truncate_to_width(names, MAX_INLINE_CHARS, MAX_DISPLAY_ITEMS)
            text.append(", ".join(shown), style="#7AA2F7")
            if remaining:
                text.append("  ", style="")
                text.append(
                    f"…{remaining} more",
                    style=Style(
                        bold=True, underline=True, color="rgb(122,162,247)",
                        meta={"@click": "show_agents_popup()"},
                    ),
                )
        else:
            text.append("none loaded", style="dim italic")
        text.append("\n", style="")

        return text


class _ClickablePanel(Static):
    """Static widget that handles '…more' click actions.

    Textual dispatches @click actions from Rich text meta on the widget
    that *renders* the text.  Since the right panel text lives inside a
    Static child (not the parent WelcomeBanner), the action methods must
    be defined here so Textual can find them.
    """

    def __init__(
        self,
        content: ConsoleRenderable | str,
        *,
        skills_data: list[tuple[str, str]],
        subagents_data: list[tuple[str, str]],
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._skills_data = skills_data
        self._subagents_data = subagents_data

    async def action_show_skills_popup(self) -> None:
        """Push the skills detail popup."""
        await self.app.push_screen(
            WelcomeDetailPopup(title="Discovered Skills", items=self._skills_data)
        )

    async def action_show_agents_popup(self) -> None:
        """Push the subagents detail popup."""
        await self.app.push_screen(
            WelcomeDetailPopup(title="Loaded Subagents", items=self._subagents_data)
        )
